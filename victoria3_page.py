"""Dedicated Victoria 3 workflow embedded in the GameTune desktop shell."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
                              QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
                              QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)
from tools.victoria3 import (SimulationConditions, SimulationRepository, compare_runs,
                             experiment_guidance, import_simulation_csv, save_fingerprint, summarize_runs)
from victoria3_quick import Victoria3Quick


class Victoria3Page(QScrollArea):
    def __init__(self, database_path):
        super().__init__()
        self.repository = SimulationRepository(database_path)
        self.baseline = None
        self.report = None
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        layout = QVBoxLayout(body)
        self.quick = Victoria3Quick(database_path=database_path)
        layout.addWidget(self.quick)
        self.advanced_toggle = QCheckBox("展开高级性能测试（可选）")
        layout.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        layout.addWidget(self.advanced)
        layout.addStretch()
        layout = QVBoxLayout(self.advanced)
        title = QLabel("Victoria 3 · 模拟速度与画面性能")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        help_text = QLabel("每次重新载入同一存档，固定日期区间、速度档位、视角和缩放，预热后计时。\n"
                           "记录推进相同游戏天数的实际秒数，每组至少三次。FPS 为可选辅助指标。\n"
                           "一次只改一个变量；接受设置前，请自行确认画质仍可接受。")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        form = QFormLayout()
        self.fields = {}
        for key, label, placeholder in [("save", "起始存档文件", "选择本机 .v3 存档"),
                ("version", "游戏版本", "填写实际版本及校验码"), ("mods", "模组与 DLC", "无模组 / 有序模组及 DLC 列表"),
                ("date_range", "起止游戏日期", "例如 1900.1.1 至 1900.2.1"),
                ("speed", "速度档位", "填写实际档位"),
                ("environment", "固定测试环境", "硬件、供电、散热、视角、缩放、自动存档规则"),
                ("configuration", "实际配置快照", "填写分辨率及本轮相关设置的实际值"),
                ("change", "本轮单项调整与恢复方法", "记录原值、新值及如何恢复")]:
            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            self.fields[key] = field
            form.addRow(label, field)
        layout.addLayout(form)
        browse = QPushButton("选择起始存档")
        browse.clicked.connect(self.choose_save)
        layout.addWidget(browse)
        inspect = QPushButton("读取本机设置参考")
        inspect.clicked.connect(self.inspect_settings)
        layout.addWidget(inspect)
        self.settings_note = QLabel("")
        self.settings_note.setWordWrap(True)
        layout.addWidget(self.settings_note)
        self.table = QTableWidget(3, 4)
        self.table.setHorizontalHeaderLabels(["推进游戏天数", "实际耗时（秒）", "平均 FPS（可选）", "1% Low（可选）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(165)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        for text, callback in [("增加测试行", self.add_row), ("导入 CSV", self.import_csv),
                               ("保存为基线", self.set_baseline), ("评估并保存复测", self.evaluate)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.confirmed = QCheckBox("我已执行所记录的单项调整，并确认画质可接受")
        layout.addWidget(self.confirmed)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(150)
        layout.addWidget(self.output)
        self.history = QComboBox()
        layout.addWidget(self.history)
        restore = QPushButton("载入历史基线与条件")
        restore.clicked.connect(self.restore)
        layout.addWidget(restore)
        export = QPushButton("导出当前报告 JSON")
        export.clicked.connect(self.export)
        layout.addWidget(export)
        self.refresh_history()
        self.quick.save_selected.connect(lambda path: self.fields["save"].setText(path))
        self.quick.discovered.connect(self.populate_detected)

    def populate_detected(self, data):
        if self.baseline is not None:
            return
        self.fields["version"].setText(data["version"])
        self.fields["configuration"].setText(json.dumps(data["settings"]["Graphics"], ensure_ascii=False))
        # Only actual observations are copied; speed, date range and mods remain explicit.

    def add_row(self):
        self.table.insertRow(self.table.rowCount())

    def choose_save(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择起始存档", "", "Victoria 3 (*.v3);;所有文件 (*)")
        if path:
            self.fields["save"].setText(path)

    def inspect_settings(self):
        # Resolve the Windows Documents folder, including redirected/OneDrive folders.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:
            self.settings_note.setText("无法定位文档目录，请在游戏内查看实际设置。")
            return
        path = Path(buffer.value) / "Paradox Interactive/Victoria 3/pdx_settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            self.settings_note.setText("本机设置参考（只读）：" + json.dumps(
                {key: data.get(key, {}) for key in ("Graphics", "game")}, ensure_ascii=False))
        except (OSError, ValueError) as error:
            self.settings_note.setText(f"无法读取本机设置：{error}")

    def snapshot(self):
        values = {key: field.text().strip() for key, field in self.fields.items()}
        conditions = SimulationConditions(save_fingerprint(values["save"]), **{
            key: values[key] for key in ("version", "mods", "date_range", "speed", "environment")})
        if not values["configuration"]:
            raise ValueError("请记录实际配置快照")
        runs = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, col).text().strip() if self.table.item(row, col) else "" for col in range(4)]
            if any(cells):
                runs.append(dict(zip(("game_days", "elapsed_seconds", "avg_fps", "low_fps"), cells)))
        summarize_runs(runs)
        return {"conditions": asdict(conditions), "runs": runs, "fields": values}

    def show_error(self, error):
        QMessageBox.warning(self, "请检查测试数据", str(error))

    def set_baseline(self):
        try:
            snapshot = self.snapshot()
            self.repository.save({"kind": "baseline", "baseline": snapshot})
            self.baseline = snapshot
            self.report = None
            self.confirmed.setChecked(False)
            self.output.setPlainText("基线已保存。请记录一项调整，清空或替换表格中的数据，再提交复测。\n"
                                     + experiment_guidance(summarize_runs(snapshot["runs"])))
            self.refresh_history()
        except (ValueError, OSError, sqlite3.Error) as error:
            self.show_error(error)

    def evaluate(self):
        try:
            if self.baseline is None:
                raise ValueError("请先保存或载入基线")
            candidate = self.snapshot()
            if not candidate["fields"]["change"] or candidate["fields"]["configuration"] == self.baseline["fields"]["configuration"]:
                raise ValueError("请记录本轮单项调整、恢复方法和变更后的实际配置")
            result = compare_runs(self.baseline, candidate, self.confirmed.isChecked())
            report = {"kind": "comparison", "baseline": self.baseline, "candidate": candidate,
                      "configuration_confirmed": self.confirmed.isChecked(), "result": result}
            self.repository.save(report)
            self.report = report
            try:
                self.quick.attach_experiment(report)
            except ValueError as error:
                self.quick.status.setText(str(error))
            self.output.setPlainText(f"{result['reason']}\n模拟速度变化：{result['simulation_delta_pct']:+.2f}%\n"
                + json.dumps(result, ensure_ascii=False, indent=2))
            self.confirmed.setChecked(False)
            self.refresh_history()
        except (ValueError, OSError, sqlite3.Error) as error:
            self.show_error(error)

    def refresh_history(self):
        self.history.clear()
        for item in self.repository.history():
            self.history.addItem(f"#{item['id']} · {item['created_at']} · {item['kind']}", item)

    def restore(self):
        item = self.history.currentData()
        if not item:
            return
        self.baseline = item["baseline"]
        self.report = item if item["kind"] == "comparison" else None
        for key, value in self.baseline["fields"].items():
            self.fields[key].setText(value)
        self.fill_table(self.baseline["runs"])
        self.confirmed.setChecked(False)
        self.output.setPlainText("已载入历史基线；复测前请更新配置和三次测试数据。")

    def fill_table(self, rows):
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, key in enumerate(("game_days", "elapsed_seconds", "avg_fps", "low_fps")):
                self.table.setItem(row, col, QTableWidgetItem(str(values.get(key, ""))))

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入模拟测试 CSV", "", "CSV (*.csv)")
        if path:
            try:
                self.fill_table(import_simulation_csv(path))
            except (OSError, ValueError) as error:
                self.show_error(error)

    def export(self):
        if self.report is None:
            self.show_error("请先评估一组复测数据或载入历史报告")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出报告", "victoria3-report.json", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as error:
                self.show_error(error)
