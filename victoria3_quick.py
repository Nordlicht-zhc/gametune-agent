"""Low-input Victoria 3 setup. Measurements remain optional and separate."""
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QPushButton, QTextEdit,
                              QVBoxLayout, QWidget)
from victoria3_ui import text, section, button
from tools.victoria3_local import LocalVictoria3
from tools.victoria3_report import ReportRepository, new_report, attach_simulation, transfer_baseline
from victoria3_report_view import Victoria3ReportView


LABELS = {"resolution": "分辨率", "quality": "画质模式", "multi_sampling": "多重采样",
          "anisotropic_filtering": "各向异性过滤", "refraction_quality": "折射质量",
          "shadowmap_resolution": "阴影分辨率"}


class Victoria3Quick(QWidget):
    discovered = Signal(dict)
    save_selected = Signal(str)
    report_requested = Signal()

    def __init__(self, service=None, database_path=None):
        super().__init__()
        self.service = service or LocalVictoria3()
        self.reports = ReportRepository(database_path or self.service.directory / "gametune-reports.sqlite")
        self.report_view = Victoria3ReportView(self.reports, self)
        self.report_embedded = False
        self.machine = {}
        self.current_report = None
        self.report_view.report_selected.connect(self.select_current_report)
        self.plan = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(text("维多利亚 3", "pageTitle"))
        layout.addWidget(text("从一项画面调整开始，用相同存档的实测结果判断是否保留。"))
        machine, machine_layout = section("本机游戏", "读取当前设置与本地存档，应用前请退出游戏。")
        self.summary = text("正在识别本机游戏…")
        machine_layout.addWidget(self.summary)
        scan_row = QHBoxLayout()
        scan_row.addWidget(button("重新扫描本机", self.scan))
        scan_row.addStretch()
        machine_layout.addLayout(scan_row)
        layout.addWidget(machine)
        plan, plan_layout = section("调整方案", "先查看实际改动。应用设置后，仍需测试才能确认收益。")
        self.mode = QComboBox()
        self.mode.addItem("温和调整 · 降低一档分辨率，保持画面比例", "resolution")
        self.mode.addItem("轻量画面 · 降低分辨率与部分渲染效果", "performance")
        self.mode.currentIndexChanged.connect(self.preview)
        plan_layout.addWidget(self.mode)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText("识别游戏后，这里会显示设置原值与计划值。")
        self.preview_text.setMinimumHeight(110)
        self.preview_text.setMaximumHeight(130)
        plan_layout.addWidget(self.preview_text)
        row = QHBoxLayout()
        self.apply_button = button("备份并应用方案", self.apply, True)
        row.addWidget(self.apply_button)
        row.addWidget(button("启动 Victoria 3", self.launch))
        row.addStretch()
        plan_layout.addLayout(row)
        self.status = text("方案尚未应用；未进行性能测试。", "guideText")
        plan_layout.addWidget(self.status)
        status_row = QHBoxLayout()
        status_row.addWidget(button("复制当前提示 / 报错", lambda: QApplication.clipboard().setText(self.status.text())))
        status_row.addStretch()
        status_row.addWidget(button("查看优化报告", self.show_report))
        plan_layout.addLayout(status_row)
        layout.addWidget(plan)
        recovery, recovery_layout = section("备份与恢复", "不满意时恢复原设置，再启动游戏。")
        self.original_note = text("应用前会自动保存原设置，无需手动记录。")
        recovery_layout.addWidget(self.original_note)
        self.restore_latest_button = button("恢复最近一次优化前的设置", self.restore_latest)
        recovery_layout.addWidget(self.restore_latest_button, 0, Qt.AlignmentFlag.AlignLeft)
        recovery_layout.addWidget(text("其他备份", "cardKicker"))
        row = QHBoxLayout()
        self.backups = QComboBox()
        self.backups.setMinimumWidth(0)
        row.addWidget(self.backups, 1)
        row.addWidget(button("恢复所选备份", self.restore))
        recovery_layout.addLayout(row)
        recovery_layout.addWidget(text("测试存档", "cardKicker"))
        self.saves = QComboBox()
        self.saves.currentIndexChanged.connect(self.select_save)
        recovery_layout.addWidget(self.saves)
        recovery_layout.addWidget(text("用于高级测试，不会修改存档。", "sectionMeta"))
        layout.addWidget(recovery)
        layout.addStretch()
        QTimer.singleShot(0, self.scan)

    def scan(self):
        try:
            data = self.service.discover()
            self.machine = data
            reports = self.reports.list()
            if self.current_report:
                self.current_report = next((item for item in reports if item["id"] == self.current_report["id"]), self.current_report)
            if self.current_report is None and reports:
                self.current_report = reports[0]
            if self.current_report and self.current_report["phase"] == "applied" and data["settings"]["Graphics"] != self.current_report["after"]["Graphics"]:
                self.current_report["phase"] = "external_change"
                self.reports.save(self.current_report)
            graphics = data["settings"]["Graphics"]
            self.summary.setText(f"已识别 Victoria 3 · {data['version']}\n"
                                 f"分辨率 {graphics.get('resolution', '未识别')} · 画质 {graphics.get('quality', '默认')}\n"
                                 f"{data['cpu']} / {data['gpu']}\n"
                                 f"发现 {len(data['saves'])} 个本地存档。硬件信息来自最近一次游戏日志。")
            self.saves.clear()
            for path in data["saves"]:
                self.saves.addItem(path.stem, str(path))
            self.discovered.emit(data)
            self.select_save()
            self.refresh_backups()
            self.preview()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.plan = None
            self.apply_button.setEnabled(False)
            self.preview_text.setPlainText("尚未读取游戏设置。请先运行一次游戏，退出后重新扫描本机。")
            self.summary.setToolTip(str(error))
            self.summary.setText("未找到游戏设置。请先运行一次 Victoria 3，退出后重新扫描。"
                                 if isinstance(error, FileNotFoundError) else
                                 f"暂时无法读取游戏设置：{error}\n请检查文件后重新扫描。")

    def select_save(self):
        path = self.saves.currentData()
        if path:
            self.save_selected.emit(path)

    def preview(self, *_):
        try:
            self.plan = self.service.preview(self.mode.currentData())
            lines = [f"{LABELS.get(key, key)}：{self.plan['before'][key] or '游戏默认'} → {value}"
                     for key, value in self.plan["changes"].items()]
            self.preview_text.setPlainText("本次计划调整：\n" + "\n".join(lines)
                + "\n\n降低渲染负担的试用方案，可能降低画质；跑日速度是否改善需要实测。"
                  "\n应用前请退出游戏。工具只修改上述图形设置，并保留其他选项。")
            self.apply_button.setEnabled(bool(lines))
        except (OSError, ValueError, sqlite3.Error) as error:
            self.plan = None
            self.apply_button.setEnabled(False)
            self.preview_text.setPlainText(f"无法生成方案：{error}")

    def refresh_backups(self):
        self.backups.clear()
        for path in self.service.backup_list():
            self.backups.addItem(path.stem, str(path))
        pending = next((r for r in self.reports.list() if r.get('backup') and r['phase'] in ('applied', 'external_change')), None)
        self.restore_latest_button.setEnabled(pending is not None)
        if pending:
            original = pending['before']['Graphics']
            self.original_note.setText('最近一次优化前的游戏设置：' + json.dumps(original, ensure_ascii=False)
                                      + '\n这些原值已保存；恢复只处理本次修改的图形设置，不改变电脑硬件。')

    def restore_latest(self):
        try:
            report = next((r for r in self.reports.list() if r.get('backup') and r['phase'] in ('applied', 'external_change')), None)
            if report is None:
                raise ValueError('没有待恢复的优化，当前无需恢复。')
            self.refresh_backups()
            index = self.backups.findData(report['backup'])
            if index < 0:
                raise ValueError('未找到该次优化的备份文件，无法自动恢复；请不要手动猜测原值。')
            self.backups.setCurrentIndex(index)
            self.restore()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(str(error))

    def apply(self):
        try:
            if self.plan is None:
                raise ValueError("请先重新扫描")
            before = self.service.read_settings()
            machine = self.service.discover()
            mode = self.plan["mode"]
            backup = self.service.apply(self.plan)
            self.plan = None
            self.apply_button.setEnabled(False)
            self.refresh_backups()
            self.status.setText("设置已写入并自动备份。点击“启动 Victoria 3”即可体验。\n"
                                "当前只是设置已应用，尚未验证性能提升；恢复时请选择对应备份。")
            previous = next((item for item in self.reports.list() if self.current_report and item["id"] == self.current_report["id"]), None)
            self.current_report = new_report(machine, before, self.service.read_settings(),
                                             phase="applied", backup=str(backup), mode=mode)
            self.current_report = transfer_baseline(previous, self.current_report)
            self.reports.save(self.current_report)
            self.refresh_backups()
            self.show_report()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(str(error))

    def restore(self):
        try:
            path = self.backups.currentData()
            if not path:
                raise ValueError("还没有可恢复的备份")
            self.service.restore(Path(path))
            self.plan = None
            self.apply_button.setEnabled(False)
            self.status.setText("已恢复所选备份中的图形设置。启动游戏后生效。")
            for report in self.reports.list():
                if report["backup"] == path:
                    report["phase"] = "restored"
                    report["events"].append({"phase": "restored", "at": datetime.now(timezone.utc).isoformat()})
                    self.reports.save(report)
                    self.current_report = report
                    self.refresh_backups()
                    self.show_report()
                    break
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            self.status.setText(f"恢复未完成：{error}")

    def launch(self):
        try:
            os.startfile("steam://rungameid/529340")
            self.status.setText("已向 Steam 发送启动请求。请在游戏内体验，工具不会代替你操作存档。")
        except (OSError, AttributeError) as error:
            self.status.setText(f"无法启动 Steam：{error}")

    def show_report(self):
        try:
            # Reload saved observations so a rescan cannot overwrite imported results.
            if self.current_report:
                self.current_report = next((item for item in self.reports.list() if item["id"] == self.current_report["id"]), self.current_report)
            else:
                data = self.service.discover()
                self.current_report = new_report(data, data["settings"])
                self.reports.save(self.current_report)
            self.report_view.load(self.current_report)
            if self.report_embedded:
                self.report_requested.emit()
            else:
                self.report_view.show()
                self.report_view.raise_()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(f"报告生成失败：{error}")

    def select_current_report(self, report):
        self.current_report = report

    def attach_experiment(self, experiment):
        if not self.current_report:
            return
        current = next((item for item in self.reports.list() if item["id"] == self.current_report["id"]), self.current_report)
        self.current_report = attach_simulation(current, experiment)
        self.reports.save(self.current_report)
        self.show_report()
