import json
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone
from PySide6.QtCore import Signal

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                              QLabel, QPushButton, QTextBrowser, QVBoxLayout, QMessageBox, QInputDialog)
from tools.import_benchmark import import_benchmark_csv
from tools.victoria3_report import attach_benchmark, report_html, transfer_baseline


class Victoria3ReportView(QDialog):
    report_selected = Signal(dict)
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
        recovery = QPushButton('找回已有优化前数据（保留原始历史记录）')
        recovery.clicked.connect(self.restore_baseline)
        layout.addWidget(recovery)
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
            before = item['observations']['baseline'].get('run_count', 0)
            after = item['observations']['candidate'].get('run_count', 0)
            self.history.addItem(f"{item['created_at']} · {item['id'][:8]} · 优化前 {before} 次 / 优化后 {after} 次", item)
        index = next((i for i in range(self.history.count()) if self.history.itemData(i)["id"] == report["id"]), -1)
        self.history.setCurrentIndex(index)
        self.history.blockSignals(False)
        self.render()
        self.report_selected.emit(self.report)

    def select_report(self, index):
        if index >= 0:
            self.report = self.history.itemData(index)
            self.render()
            self.report_selected.emit(self.report)

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

    def restore_baseline(self):
        if not self.report or not self.report['observations']['candidate']:
            self.status.setText('请先在上方选择一份包含优化后实测的报告。列表中显示了前后各有几次测试。')
            return
        if self.report['observations']['baseline']:
            self.status.setText('此报告已经包含优化前数据，无需找回。')
            return
        choices = []
        for source in self.repository.list():
            target = transfer_baseline(source, deepcopy(self.report))
            if target.get('baseline_report_id') == source['id']:
                choices.append((source, target))
        if not choices:
            self.status.setText('没有找到图形配置、设备和版本匹配的优化前记录；历史记录均保留。')
            return
        index = 0
        if len(choices) > 1:
            names = [f"{source['created_at']} · {source['id'][:8]} · {source['observations']['baseline'].get('avg_fps', '—')} FPS" for source, _ in choices]
            selection, ok = QInputDialog.getItem(self, '选择优化前记录', '请选择同一场景的优化前测试：', names, 0, False)
            if not ok:
                return
            index = names.index(selection)
        source, report = choices[index]
        baseline = source['observations']['baseline']
        answer = QMessageBox.question(self, '关联历史优化前测试',
            f"找到配置匹配的优化前记录：{baseline.get('avg_fps', '—')} FPS / {baseline.get('low_fps', '—')} 1% Low。\n"
            '确认它与当前优化后测试使用相同存档、视角、速度档位和采集方法后，选择“是”来关联。\n'
            '原始记录保留；重复次数不足的比较仍只作参考。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if report.get('model_explanation'):
            report.setdefault('previous_model_explanations', []).append(report.pop('model_explanation'))
        report['comparison_confirmed'] = True
        report['events'].append({'phase': '关联历史优化前记录', 'at': datetime.now(timezone.utc).isoformat()})
        self.report = report
        self.persist()
        self.status.setText('优化前数据已找回并与优化后并排显示，原始历史记录保持保留。')
