import json
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QComboBox,QPushButton,QTextBrowser,QFileDialog,QInputDialog,QLabel
from pathlib import Path
from tools.configurations import configuration_html,comparison_html


class ConfigurationReport(QWidget):
    selected = Signal(dict)
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        self.record = None
        root = QVBoxLayout(self)
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self.select)
        root.addWidget(self.selector)
        self.browser = QTextBrowser()
        root.addWidget(self.browser,1)
        row = QHBoxLayout()
        for text,callback in [('重命名配置',self.rename),('导出此配置报告',lambda:self.export('html')),('导出此配置数据',lambda:self.export('json'))]:
            button=QPushButton(text);button.clicked.connect(callback);row.addWidget(button)
        root.addLayout(row)
        self.status=QLabel('每份报告仅属于一套配置；不同配置请到“自由对比”查看。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def refresh(self, identifier=None):
        identifier=identifier or (self.record['id'] if self.record else None)
        self.selector.blockSignals(True)
        self.selector.clear()
        for record in self.repository.list():
            self.selector.addItem(f"{record['name']} · {record['observations'].get('run_count',0)} 次 · {record['id'][:6]}",record['id'])
        index=self.selector.findData(identifier)
        self.selector.setCurrentIndex(index if index>=0 else 0)
        self.selector.blockSignals(False)
        self.select(self.selector.currentIndex())

    def select(self,index):
        record=self.repository.get(self.selector.itemData(index)) if index>=0 else None
        self.record=record
        self.browser.setHtml(configuration_html(record) if record else '<h2>还没有配置记录</h2>')
        if record:self.selected.emit(record)

    def rename(self):
        if not self.record:return
        name,ok=QInputDialog.getText(self,'配置名称','输入便于识别的名称：',text=self.record['name'])
        if ok and name.strip():
            self.repository.rename(self.record['id'],name.strip());self.refresh(self.record['id'])

    def export(self,kind):
        if not self.record:return
        path,_=QFileDialog.getSaveFileName(self,'导出单个配置',f"configuration-{self.record['id'][:8]}.{kind}",f'{kind.upper()} (*.{kind})')
        if path:
            try:Path(path).write_text(configuration_html(self.record) if kind=='html' else json.dumps(self.record,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as error:self.status.setText(str(error))


class ConfigurationComparison(QWidget):
    changed=Signal()
    def __init__(self,repository):
        super().__init__()
        self.repository=repository
        root=QVBoxLayout(self)
        root.addWidget(QLabel('自由选择配置 A 和 B，不改变它们各自的报告和测试记录。'))
        row=QHBoxLayout();self.a=QComboBox();self.b=QComboBox()
        row.addWidget(QLabel('配置 A'));row.addWidget(self.a,1)
        row.addWidget(QLabel('配置 B'));row.addWidget(self.b,1)
        swap=QPushButton('交换 A / B');swap.clicked.connect(self.swap);row.addWidget(swap)
        root.addLayout(row)
        self.browser=QTextBrowser();root.addWidget(self.browser,1)
        export=QPushButton('导出这次对比（独立文件）');export.clicked.connect(self.export);root.addWidget(export)
        self.status=QLabel('可自由比较数值；是否可归因于设置，需核对场景和设备条件。');self.status.setWordWrap(True);root.addWidget(self.status)
        self.a.currentIndexChanged.connect(self.render);self.b.currentIndexChanged.connect(self.render)

    def refresh(self):
        ids=[self.a.currentData(),self.b.currentData()]
        records=self.repository.list()
        for combo,identifier in zip((self.a,self.b),ids):
            combo.blockSignals(True);combo.clear()
            for record in records:combo.addItem(record['name']+' · '+record['id'][:6],record['id'])
            index=combo.findData(identifier);combo.setCurrentIndex(index if index>=0 else 0);combo.blockSignals(False)
        if ids[1] is None and len(records)>1:
            initial=self.repository.get(self.a.currentData())
            index=next((i for i,record in enumerate(records) if record['fingerprint']!=initial['fingerprint']),1)
            self.b.setCurrentIndex(index)
        self.render()

    def swap(self):
        a,b=self.a.currentIndex(),self.b.currentIndex()
        self.a.blockSignals(True);self.b.blockSignals(True)
        self.a.setCurrentIndex(b);self.b.setCurrentIndex(a)
        self.a.blockSignals(False);self.b.blockSignals(False);self.render()

    def render(self,*_):
        a,b=self.repository.get(self.a.currentData()),self.repository.get(self.b.currentData())
        self.browser.setHtml(comparison_html(a,b) if a and b else '<h2>至少保存两套配置后即可选择对比</h2>')
        self.changed.emit()

    def export(self):
        a,b=self.repository.get(self.a.currentData()),self.repository.get(self.b.currentData())
        if not a or not b:return
        path,_=QFileDialog.getSaveFileName(self,'导出配置对比','configuration-comparison.html','HTML (*.html)')
        if path:
            try:Path(path).write_text(comparison_html(a,b),encoding='utf-8')
            except OSError as error:self.status.setText(str(error))
