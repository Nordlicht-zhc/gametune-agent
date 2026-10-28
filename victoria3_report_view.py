import json
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                              QLabel, QPushButton, QTextBrowser, QVBoxLayout)
from tools.import_benchmark import import_benchmark_csv
from tools.victoria3_report import attach_benchmark, report_html


class Victoria3ReportView(QDialog):
    def __init__(self, repository, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.report = None
        self.setWindowTitle("Victoria 3 · 优化报告")
        self.resize(1050, 800)
        layout = QVBoxLayout(self)
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.select_report)
        layout.addWidget(self.history)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        layout.addWidget(self.browser, 1)
        row = QHBoxLayout()
        for text, callback in [("导入优化前 FPS CSV", lambda: self.import_metrics("baseline")),
                               ("导入优化后 FPS CSV", lambda: self.import_metrics("candidate")),
                               ("导出 HTML 报告", lambda: self.export("html")),
                               ("导出 JSON 数据", lambda: self.export("json"))]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.confirm = QCheckBox("确认导入数据分别来自本报告的优化前/后配置，且测试场景与采集方法一致")
        self.confirm.toggled.connect(self.confirm_comparison)
        layout.addWidget(self.confirm)
        self.status = QLabel("未采集指标会显示为未采集，不根据硬件型号推算 FPS。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def load(self, report):
        self.report = report
        self.history.blockSignals(True)
        self.history.clear()
        for item in self.repository.list():
            self.history.addItem(item["created_at"] + " · " + item["id"][:8], item)
        index = next((i for i in range(self.history.count()) if self.history.itemData(i)["id"] == report["id"]), -1)
        self.history.setCurrentIndex(index)
        self.history.blockSignals(False)
        self.render()

    def select_report(self, index):
        if index >= 0:
            self.report = self.history.itemData(index)
            self.render()

    def render(self):
        if self.report:
            self.confirm.blockSignals(True)
            self.confirm.setChecked(self.report["comparison_confirmed"])
            self.confirm.blockSignals(False)
            self.browser.setHtml(report_html(self.report))

    def persist(self):
        self.repository.save(self.report)
        self.load(self.report)

    def import_metrics(self, side):
        if not self.report:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择同场景 FPS 采集文件", "", "CSV (*.csv)")
        if path:
            try:
                self.report = attach_benchmark(self.report, side, import_benchmark_csv(path), Path(path).name)
                self.persist()
                self.status.setText("已导入实测数据；请确认配置归属和场景一致性后再查看变化。")
            except (OSError, ValueError) as error:
                self.status.setText(str(error))

    def confirm_comparison(self, checked):
        if self.report:
            self.report["comparison_confirmed"] = checked
            self.persist()

    def export(self, kind):
        if not self.report:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出报告", f"victoria3-report.{kind}", f"{kind.upper()} (*.{kind})")
        if path:
            try:
                content = report_html(self.report) if kind == "html" else json.dumps(self.report, ensure_ascii=False, indent=2)
                Path(path).write_text(content, encoding="utf-8")
                self.status.setText("报告已导出。")
            except OSError as error:
                self.status.setText(str(error))
