"""Standalone hardware upgrade analysis tool."""
from copy import deepcopy
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QWidget, QVBoxLayout, QComboBox, QPushButton, QTextEdit, QCheckBox
from app import PageHeader
from victoria3_app import ReportExplanationJob


class HardwareUpgradeTool(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.job = None
        root = QVBoxLayout(self)
        root.addWidget(PageHeader('工具区', '硬件升级分析', '选择测试配置和升级目标，找出最值得优先补齐的硬件短板。'))
        self.records = QComboBox()
        root.addWidget(self.records)
        self.goal = QComboBox()
        self.goal.addItems(['提高画面帧率', '加快游戏日期推进'])
        root.addWidget(self.goal)
        capture = QPushButton('① 采集游戏性能数据')
        capture.clicked.connect(lambda: window.navigate(2))
        root.addWidget(capture)
        self.consent = QCheckBox('允许将所选配置的硬件与实测摘要发送到模型接口')
        root.addWidget(self.consent)
        self.button = QPushButton('② 分析优先升级哪项硬件')
        self.button.clicked.connect(self.analyze)
        root.addWidget(self.button)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText('这里展示：优先升级项 → 实测依据 → 主要改善方向 → 下一步验证。')
        root.addWidget(self.result, 1)
        self.records.currentIndexChanged.connect(lambda: self.consent.setChecked(False))
        self.goal.currentIndexChanged.connect(lambda: self.consent.setChecked(False))

    def refresh(self):
        identifier = self.records.currentData()
        self.records.blockSignals(True)
        self.records.clear()
        for record in self.window.configurations.list():
            self.records.addItem(record['name']+' · '+record['id'][:6], record['id'])
        index = self.records.findData(identifier)
        self.records.setCurrentIndex(max(0, index))
        self.records.blockSignals(False)

    def analyze(self):
        if self.job:
            return
        record = self.window.configurations.get(self.records.currentData())
        if not record or not record['observations'].get('run_count'):
            self.result.setPlainText('先点击①完成一次性能采集，再返回此工具分析硬件升级方向。')
            return
        provider = self.window.provider
        if provider and not self.consent.isChecked():
            self.result.setPlainText('勾选发送摘要后，即可开始模型分析。')
            return
        snapshot = deepcopy(record)
        snapshot['upgrade_goal'] = self.goal.currentText()
        self.button.setEnabled(False)
        self.records.setEnabled(False)
        self.goal.setEnabled(False)
        self.result.setPlainText('正在分析硬件短板与升级优先级…')
        self.job = ReportExplanationJob(provider, snapshot, self)
        self.job.completed.connect(self.completed, Qt.ConnectionType.QueuedConnection)
        self.job.finished.connect(self.finished, Qt.ConnectionType.QueuedConnection)
        self.job.start()

    @Slot(object)
    def completed(self, result):
        self.result.setPlainText(self.job.snapshot['name']+' · '+self.job.snapshot['upgrade_goal']+'\n\n'+result['summary']+'\n\n'+'\n\n'.join(f'{i}. {step}' for i, step in enumerate(result['steps'], 1)))

    @Slot()
    def finished(self):
        self.job.deleteLater()
        self.job = None
        for widget in (self.button, self.records, self.goal):
            widget.setEnabled(True)
