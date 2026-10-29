"""Configuration-centric standalone workspace; legacy pairs are only migration input."""
from copy import deepcopy
from pathlib import Path
import sqlite3
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QFrame,QTabWidget,QTableWidgetItem,QPushButton
from victoria3_app import Victoria3Window,ReportExplanationJob
from app import PageHeader,label,set_margins
from tools.configurations import ConfigurationRepository,comparison_snapshot,value_text
from configuration_views import ConfigurationReport,ConfigurationComparison
from configuration_capture import ConfigurationCapture
from configuration_quick import ConfigurationQuick
from tools.victoria3_report import ReportRepository


class ConfigurationWindow(Victoria3Window):
    def __init__(self,database_path=None,provider=None):
        path=database_path or Path(__file__).parent/'.data/victoria3.db'
        repository=ConfigurationRepository(path)
        repository.migrate(ReportRepository(path).list())
        super().__init__(path,provider,quick_factory=lambda database_path:ConfigurationQuick(repository,database_path))
        self.configurations=repository
        self.legacy_pages=[]
        self.quick.report_requested.disconnect()
        self.quick.report_requested.connect(self.show_active_configuration)
        old=self.stack.widget(2)
        self.legacy_pages.append(old)
        self.stack.removeWidget(old);old.hide()
        self.capture_page=ConfigurationCapture(self.quick,self.configurations)
        self.capture_page.saved.connect(self.capture_saved)
        self.stack.insertWidget(2,self.wrap(self.capture_page))
        old=self.stack.widget(4)
        self.legacy_pages.append(old)
        old.layout().removeWidget(self.consent);old.layout().removeWidget(self.model_button);old.layout().removeWidget(self.model_text)
        self.stack.removeWidget(old);old.hide()
        page=QWidget();layout=set_margins(QVBoxLayout(page),24,22,24,22,10)
        layout.addWidget(PageHeader('配置档案','配置报告与自由对比','每份报告只属于一套配置；选择任意 A、B 配置单独比较。'))
        self.config_tabs=QTabWidget()
        self.config_report=ConfigurationReport(self.configurations)
        self.comparison=ConfigurationComparison(self.configurations)
        self.config_tabs.addTab(self.config_report,'单个配置报告')
        self.config_tabs.addTab(self.comparison,'自由对比')
        layout.addWidget(self.config_tabs,1)
        for widget in (self.consent,self.model_button,self.model_text):layout.addWidget(widget);widget.show()
        self.consent.setText('允许将当前配置或 A/B 对比的硬件型号、设置与实测摘要发送到模型接口')
        self.stack.insertWidget(4,page)
        guide=QFrame();guide.setObjectName('card')
        guide_layout=QVBoxLayout(guide)
        guide_layout.addWidget(label('性能分析 · 按顺序完成','sectionTitle'))
        guide_layout.addWidget(label('① 选择下面的配置 → ② 采集 60 秒 → ③ 分析瓶颈 → ④ 按建议调整并对比','ruleText',True))
        actions=QHBoxLayout()
        for text,callback in [('② 开始性能测试',lambda:self.navigate(2)),('③ 分析当前配置',self.start_guided_analysis),('④ 查看配置对比',lambda:self.config_tabs.setCurrentIndex(1))]:
            button=QPushButton(text);button.clicked.connect(callback);actions.addWidget(button)
        guide_layout.addLayout(actions);layout.insertWidget(1,guide)
        self.model_button.setText('分析主要瓶颈与改进步骤')
        self.group.button(3).setText('配置库')
        self.group.button(4).setText('配置报告与对比')
        self.history.setHorizontalHeaderLabels(['配置名称','测试次数','分辨率','配置编号'])
        history_page=self.stack.widget(3)
        old_header=history_page.layout().takeAt(0).widget()
        old_header.deleteLater()
        history_page.layout().insertWidget(0,PageHeader('配置档案','配置库','每套配置与测试结果独立保存，打开报告或自由选择配置对比。'))
        self.quick.discovered.connect(self.configuration_discovered)
        self.config_report.selected.connect(self.configuration_selected)
        self.comparison.changed.connect(self.comparison_changed)
        self.config_tabs.currentChanged.connect(self.comparison_changed)
        self.refresh_config_views()
        self.navigate(0)

    def configuration_discovered(self,data):
        record=self.configurations.current(data,data['settings'],getattr(self.capture_page,'active_id',None))
        self.capture_page.active_id=record['id']
        self.refresh_overview()

    def refresh_config_views(self,identifier=None):
        self.config_report.refresh(identifier)
        self.comparison.refresh()

    def show_active_configuration(self):
        try:
            record=self.capture_page.current()
            self.refresh_config_views(record['id'])
            self.config_tabs.setCurrentIndex(0)
            self.navigate(4)
        except (OSError,ValueError) as error:self.quick.status.setText(str(error))

    def capture_saved(self,record):
        self.refresh_config_views(record['id']);self.config_tabs.setCurrentIndex(0);self.navigate(4)

    def navigate(self,index):
        if not hasattr(self,'configurations'):
            return super().navigate(index)
        self.stack.setCurrentIndex(index);self.group.button(index).setChecked(True)
        if index==0:self.refresh_overview()
        elif index==2:self.capture_page.suggest_stage()
        elif index==3:
            self.history_rows=self.configurations.list();self.history.setRowCount(len(self.history_rows))
            for row,record in enumerate(self.history_rows):
                for col,value in enumerate([record['name'],str(record['observations'].get('run_count',0)),record['settings'].get('Graphics',{}).get('resolution','未识别'),record['id'][:12]]):
                    self.history.setItem(row,col,QTableWidgetItem(value))
            self.history.resizeColumnsToContents()
        elif index==4:self.refresh_config_views()

    def open_history(self):
        if not hasattr(self,'configurations'):return super().open_history()
        row=self.history.currentRow()
        if row>=0:
            self.config_report.refresh(self.history_rows[row]['id']);self.config_tabs.setCurrentIndex(0);self.navigate(4)

    def refresh_overview(self):
        if not hasattr(self,'configurations'):return super().refresh_overview()
        while self.overview_layout.count():
            item=self.overview_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child=item.layout().takeAt(0)
                    if child.widget():child.widget().deleteLater()
        root=self.overview_layout
        root.addWidget(PageHeader('配置概览','每套配置，独立记录','保存配置、采集实测，再自由选择配置进行对比。'))
        records=self.configurations.list()
        record=self.configurations.get(getattr(self.capture_page,'active_id',None)) if isinstance(self.capture_page,ConfigurationCapture) else None
        row=QWidget();cards=QHBoxLayout(row)
        for key,title,unit in [('avg_fps','平均 FPS','FPS'),('low_fps','1% Low','FPS'),('days_per_second','模拟推进速度','游戏天/秒')]:
            card=QFrame();card.setObjectName('metricCard');box=set_margins(QVBoxLayout(card),18,18,18,18,8)
            box.addWidget(label(title,'cardKicker'));box.addWidget(label(value_text(record,key,unit) if record else '未采集','metricValue'))
            box.addWidget(label('当前配置的独立测试','metricNote'));cards.addWidget(card)
        root.addWidget(row)
        info=QFrame();info.setObjectName('card');box=set_margins(QVBoxLayout(info),24,22,24,22,12)
        box.addWidget(label(record['name'] if record else '等待识别当前配置','sectionTitle'))
        box.addWidget(label(f'配置库共 {len(records)} 条记录。不同分辨率、设置或设备的测试各自保存。','ruleText',True))
        for key in ('cpu','gpu','version'):box.addWidget(label(self.quick.machine.get(key,'等待扫描'),'ruleText',True))
        root.addWidget(info);root.addStretch()

    def model_snapshot(self):
        if self.config_tabs.currentIndex()==1:
            a=self.configurations.get(self.comparison.a.currentData());b=self.configurations.get(self.comparison.b.currentData())
            return comparison_snapshot(a,b) if a and b else None
        return deepcopy(self.config_report.record)

    def configuration_selected(self,record):
        self.consent.setChecked(False)
        self.show_explanation(record.get('model_explanation'))

    def comparison_changed(self,*_):
        self.consent.setChecked(False)
        if self.config_tabs.currentIndex()==1:
            snapshot=self.model_snapshot();saved=self.configurations.comparison_explanation(snapshot['id']) if snapshot else None
            self.show_explanation(saved['explanation'] if saved else None)
        else:self.show_explanation(self.config_report.record.get('model_explanation') if self.config_report.record else None)

    def show_explanation(self,result):
        self.model_text.setPlainText(result['summary']+'\n\n'+'\n\n'.join(f'{i}. {step}' for i,step in enumerate(result['steps'],1)) if result else '')

    def start_guided_analysis(self):
        self.config_tabs.setCurrentIndex(0)
        record=self.config_report.record
        if not record or not record['observations'].get('run_count'):
            self.navigate(2)
            self.capture_page.status.setText('下一步：载入游戏存档，勾选场景确认，然后开始 60 秒采集。')
            return
        self.explain()

    def restore_model_explanation(self,report):
        if not hasattr(self,'configurations'):return super().restore_model_explanation(report)

    def explain(self):
        snapshot=self.model_snapshot()
        if not snapshot or self.job:return
        if self.provider and not self.consent.isChecked():
            self.model_text.setPlainText('请先确认将当前配置或对比摘要发送到所配置模型接口。');return
        self.model_button.setEnabled(False);self.model_text.setPlainText('正在生成解读…')
        self.job=ReportExplanationJob(self.provider,snapshot,self)
        self.job.completed.connect(self.model_completed,Qt.ConnectionType.QueuedConnection)
        self.job.finished.connect(self.finish_job,Qt.ConnectionType.QueuedConnection)
        self.job.start()

    def receive_explanation(self,result,snapshot,provider):
        try:
            self.persist_explanation(result,snapshot,provider)
        except (OSError,ValueError,sqlite3.Error):
            self.model_text.setPlainText('解读已返回，但保存失败；原始测试数据保留。\n'+result['summary'])

    def persist_explanation(self,result,snapshot,provider):
        if self.provider is not provider or self.model_snapshot()!=snapshot:
            self.model_text.setPlainText('当前配置或对比选择已变化，本次解读未覆盖。');return
        if snapshot['schema']=='configuration_comparison':
            fresh=comparison_snapshot(self.configurations.get(snapshot['a']['id']),self.configurations.get(snapshot['b']['id']))
            if fresh!=snapshot:self.model_text.setPlainText('测试数据已更新，请重新生成对比解读。');return
            self.configurations.save_comparison_explanation(snapshot,result)
        elif not self.configurations.save_explanation(snapshot,result):
            self.model_text.setPlainText('配置测试数据已更新，未覆盖新数据。');return
        else:self.config_report.refresh(snapshot['id'])
        self.show_explanation(result)
