"""Low-input Victoria 3 setup. Measurements remain optional and separate."""
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QTextEdit,
                              QVBoxLayout, QWidget)
from tools.victoria3_local import LocalVictoria3
from tools.victoria3_report import ReportRepository, new_report, attach_simulation
from victoria3_report_view import Victoria3ReportView


LABELS = {"resolution": "分辨率", "quality": "画质模式", "multi_sampling": "多重采样",
          "anisotropic_filtering": "各向异性过滤", "refraction_quality": "折射质量",
          "shadowmap_resolution": "阴影分辨率"}


class Victoria3Quick(QWidget):
    discovered = Signal(dict)
    save_selected = Signal(str)

    def __init__(self, service=None, database_path=None):
        super().__init__()
        self.service = service or LocalVictoria3()
        self.reports = ReportRepository(database_path or self.service.directory / "gametune-reports.sqlite")
        self.report_view = Victoria3ReportView(self.reports, self)
        self.machine = {}
        self.current_report = None
        self.plan = None
        layout = QVBoxLayout(self)
        title = QLabel("Victoria 3 · 一键设置优化")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = QLabel("自动读取本机设置，无需寻找系统文件或填写测试表格。\n"
                      "选择方案 → 查看改动 → 应用并启动游戏。不满意可一键恢复。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.summary = QLabel("正在识别本机游戏…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        scan = QPushButton("重新扫描本机")
        scan.clicked.connect(self.scan)
        layout.addWidget(scan)
        self.mode = QComboBox()
        self.mode.addItem("温和调整 · 只降低一档分辨率（保持画面比例）", "resolution")
        self.mode.addItem("轻量画面 · 降低分辨率并关闭部分渲染效果", "performance")
        self.mode.currentIndexChanged.connect(self.preview)
        layout.addWidget(self.mode)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(150)
        self.preview_text.setMaximumHeight(210)
        layout.addWidget(self.preview_text)
        row = QHBoxLayout()
        self.apply_button = QPushButton("备份并应用方案")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        launch = QPushButton("启动 Victoria 3")
        launch.clicked.connect(self.launch)
        row.addWidget(self.apply_button)
        row.addWidget(launch)
        layout.addLayout(row)
        self.status = QLabel("方案尚未应用；未进行性能测试。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        report_button = QPushButton("查看优化报告 / 性能对比 / 历史报告")
        report_button.clicked.connect(self.show_report)
        layout.addWidget(report_button)
        layout.addWidget(QLabel("自动备份 · 可选择任一次应用前的设置"))
        self.backups = QComboBox()
        layout.addWidget(self.backups)
        restore = QPushButton("恢复所选备份")
        restore.clicked.connect(self.restore)
        layout.addWidget(restore)
        layout.addWidget(QLabel("已发现的存档 · 仅供高级测试选择，不修改存档"))
        self.saves = QComboBox()
        self.saves.currentIndexChanged.connect(self.select_save)
        layout.addWidget(self.saves)
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
            self.summary.setText(f"暂时无法自动识别：{error}\n请先运行一次游戏，再点击重新扫描。")

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
            if previous and previous["phase"] == "assessment" and previous["before"] == before:
                self.current_report["observations"]["baseline"] = previous["observations"]["baseline"]
                self.current_report["sources"] = {key: value for key, value in previous["sources"].items() if key.startswith("baseline_")}
            self.reports.save(self.current_report)
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
            self.report_view.show()
            self.report_view.raise_()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(f"报告生成失败：{error}")

    def attach_experiment(self, experiment):
        if not self.current_report:
            return
        current = next((item for item in self.reports.list() if item["id"] == self.current_report["id"]), self.current_report)
        self.current_report = attach_simulation(current, experiment)
        self.reports.save(self.current_report)
        self.show_report()
