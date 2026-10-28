"""Standalone Victoria 3 workspace. Shared engine, independent desktop shell."""
import argparse
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout,
    QMainWindow, QPushButton, QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget, QTabWidget)
from victoria3_capture import CapturePage
from app import STYLESHEET, APISettingsPage, ModelWorker, PageHeader, SectionHeader, label, set_margins
from agent.api import OpenAICompatibleProvider, provider_from_env
from agent.victoria3_model import explain_victoria3
from victoria3_page import Victoria3Page
from tools.victoria3_report import metric_rows


class Victoria3Window(QMainWindow):
    def __init__(self, database_path=None, provider=None):
        super().__init__()
        self.setWindowTitle("Victoria 3 Optimizer · 维多利亚3性能优化")
        self.resize(1280, 860)
        self.setMinimumSize(1050, 720)
        self.provider = provider
        self.job = None
        self.worker = None
        self.source = Victoria3Page(database_path or Path(__file__).parent / '.data/victoria3.db')
        self.quick = self.source.quick
        self.source.tabs.blockSignals(True)
        self.quick.report_requested.disconnect()
        self.quick.report_requested.connect(lambda: self.navigate(4))
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        outer = set_margins(QHBoxLayout(root), spacing=0)
        side = QFrame()
        side.setObjectName('sidebar')
        side.setFixedWidth(225)
        sidebar = set_margins(QVBoxLayout(side), 18, 26, 18, 24, 12)
        brand = label('V3', 'brandMark')
        brand.setFixedSize(48, 48)
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar.addWidget(brand)
        sidebar.addWidget(label('Victoria 3', 'brandName'))
        sidebar.addWidget(label('PERFORMANCE OPTIMIZER', 'brandSub'))
        sidebar.addSpacing(24)
        sidebar.addWidget(label('工作区', 'sidebarLabel'))
        self.group = QButtonGroup(self)
        for index, title in enumerate(['概览', '一键优化', '性能测试', '历史记录', '最终报告', '模型设置']):
            button = QPushButton(title)
            button.setObjectName('navButton')
            button.setCheckable(True)
            self.group.addButton(button, index)
            sidebar.addWidget(button)
        self.group.idClicked.connect(self.navigate)
        sidebar.addStretch()
        sidebar.addWidget(label('本地运行 · 自动备份\n模型仅提供解释与建议', 'sideTaskText', True))
        outer.addWidget(side)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.overview = QWidget()
        self.overview_layout = set_margins(QVBoxLayout(self.overview), 30, 26, 30, 26, 18)
        self.stack.addWidget(self.overview)
        self.stack.addWidget(self.wrap(self.quick))
        self.source.advanced.setVisible(True)
        tests = QTabWidget()
        self.capture_page = CapturePage(self.quick)
        tests.addTab(self.capture_page, '自动采集 FPS')
        tests.addTab(self.wrap(self.source.advanced), '高级跑日测试')
        self.stack.addWidget(tests)
        history = QWidget()
        history_layout = set_margins(QVBoxLayout(history), 30, 26, 30, 26, 16)
        history_layout.addWidget(PageHeader('实验档案', '历史记录', '查看各次优化的配置与报告，性能数据保留来源和验证状态。'))
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(['时间', '状态', '方案', '报告编号'])
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setStretchLastSection(True)
        history_layout.addWidget(self.history)
        open_report = QPushButton('查看选中报告')
        open_report.clicked.connect(self.open_history)
        history_layout.addWidget(open_report)
        self.stack.addWidget(history)
        report_page = QWidget()
        report_layout = set_margins(QVBoxLayout(report_page), 24, 22, 24, 22, 10)
        report_layout.addWidget(PageHeader('优化结果', '最终报告', '实际改动、前后实测与下一步建议；未采集的指标不会估算。'))
        report_layout.addWidget(self.quick.report_view, 1)
        self.quick.report_view.show()
        self.consent = QCheckBox('允许将本报告的硬件型号、图形设置和实测指标发送到已配置的模型 API')
        report_layout.addWidget(self.consent)
        self.quick.report_view.history.currentIndexChanged.connect(lambda _: self.consent.setChecked(False))
        self.model_button = QPushButton('生成模型解读（未配置时使用本地建议）')
        self.model_button.clicked.connect(self.explain)
        report_layout.addWidget(self.model_button)
        self.model_text = QTextEdit()
        self.model_text.setReadOnly(True)
        self.model_text.setPlaceholderText('模型解读独立展示，不会改变报告中的数值或自动执行操作。')
        self.model_text.setMaximumHeight(110)
        report_layout.addWidget(self.model_text)
        self.stack.addWidget(report_page)
        config = provider.config if isinstance(provider, OpenAICompatibleProvider) else None
        self.settings = APISettingsPage(config)
        self.settings.provider_applied.connect(self.set_provider)
        self.stack.addWidget(self.settings)
        self.quick.discovered.connect(lambda _: self.refresh_overview())
        self.navigate(0)

    @staticmethod
    def wrap(widget):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        self.group.button(index).setChecked(True)
        if index == 0:
            self.refresh_overview()
        if index == 3:
            self.history_rows = self.quick.reports.list()
            self.history.setRowCount(len(self.history_rows))
            from tools.victoria3_report import PHASE_LABELS
            for row, record in enumerate(self.history_rows):
                for col, value in enumerate([record['created_at'], PHASE_LABELS[record['phase']], record['mode'] or '本机诊断', record['id'][:12]]):
                    self.history.setItem(row, col, QTableWidgetItem(value))
            self.history.resizeColumnsToContents()
        if index == 4 and not self.quick.report_view.report:
            self.quick.show_report()

    def refresh_overview(self):
        while self.overview_layout.count():
            item = self.overview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        layout = self.overview_layout
        layout.addWidget(PageHeader('性能概览', '让每一次调整都有依据', '自动识别设备，备份设置，比较真实测试结果。', 'Victoria 3'))
        reports = self.quick.reports.list()
        report = reports[0] if reports else None
        metrics = metric_rows(report)[:3] if report else [[name, '未采集', '未采集'] for name in ['平均 FPS', '1% Low', '模拟推进速度']]
        row = QHBoxLayout()
        for metric in metrics:
            card = QFrame()
            card.setObjectName('metricCard')
            box = set_margins(QVBoxLayout(card), 20, 20, 20, 20, 10)
            box.addWidget(label(metric[0], 'cardKicker'))
            box.addWidget(label(metric[2], 'metricValue'))
            box.addWidget(label('最新报告中的优化后实测', 'metricNote'))
            row.addWidget(card)
        layout.addLayout(row)
        card = QFrame()
        card.setObjectName('card')
        box = set_margins(QVBoxLayout(card), 24, 22, 24, 22, 12)
        box.addWidget(SectionHeader('本机环境', '来自游戏设置与最近一次日志'))
        machine = self.quick.machine
        for name, value in [('处理器', machine.get('cpu', '等待扫描')), ('显卡', machine.get('gpu', '等待扫描')),
                            ('内存', machine.get('ram', '等待扫描')), ('游戏版本', machine.get('version', '等待扫描')),
                            ('分辨率', machine.get('settings', {}).get('Graphics', {}).get('resolution', '等待扫描'))]:
            box.addWidget(label(f'{name}    {value}', 'ruleText', True))
        layout.addWidget(card)
        next_card = QFrame()
        next_card.setObjectName('card')
        box = set_margins(QVBoxLayout(next_card), 24, 22, 24, 22, 12)
        box.addWidget(SectionHeader('下一步', '一键调整 · 可恢复'))
        box.addWidget(label('先预览方案和画质取舍，再备份并应用。需要验证收益时，使用同一存档完成前后测试。', 'ruleText', True))
        button = QPushButton('查看一键优化方案 →')
        button.setObjectName('primaryButton')
        button.clicked.connect(lambda: self.navigate(1))
        box.addWidget(button)
        layout.addWidget(next_card)
        layout.addStretch()

    def open_history(self):
        row = self.history.currentRow()
        if row >= 0:
            self.quick.current_report = self.history_rows[row]
            self.quick.show_report()

    def set_provider(self, provider):
        self.provider = provider
        self.consent.setChecked(False)
        self.model_text.clear()

    def explain(self):
        report = self.quick.report_view.report
        if not report or self.job:
            return
        if self.provider and not self.consent.isChecked():
            self.model_text.setPlainText('请先确认是否允许将所列报告摘要发送到已配置接口。')
            return
        from copy import deepcopy
        snapshot = deepcopy(report)
        provider = self.provider
        self.model_button.setEnabled(False)
        self.job = QThread(self)
        self.worker = ModelWorker(lambda: explain_victoria3(provider, snapshot))
        self.worker.moveToThread(self.job)
        self.job.started.connect(self.worker.run)
        self.worker.finished.connect(lambda result: self.receive_explanation(result, snapshot, provider))
        self.worker.finished.connect(self.job.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.job.finished.connect(self.finish_job)
        self.job.start()

    def receive_explanation(self, result, snapshot, provider):
        if self.quick.report_view.report == snapshot and self.provider is provider:
            self.model_text.setPlainText(('模型建议（未经性能验证）' if result['source'] == 'model' else '本地建议') + '\n' + result['summary'] + '\n' + '\n'.join(result['steps']))

    def finish_job(self):
        self.job.deleteLater()
        self.job = None
        self.worker = None
        self.model_button.setEnabled(True)

    def closeEvent(self, event):
        if self.capture_page.job:
            self.capture_page.job.cancel.set()
            self.capture_page.status.setText('正在取消采集，请等待任务结束后关闭。')
            event.ignore()
            return
        self.settings.stop_workers()
        if self.job and self.job.isRunning():
            event.ignore()
            self.model_text.setPlainText('模型请求仍在进行，请等待请求完成或超时后关闭。')
            return
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--page', type=int, choices=range(6), default=0)
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLESHEET)
    try:
        provider = provider_from_env()
    except ValueError:
        provider = None
    window = Victoria3Window(provider=provider)
    window.show()
    QTimer.singleShot(200, lambda: window.navigate(args.page))
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))
            app.quit()
        QTimer.singleShot(1100, capture)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
