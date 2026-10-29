from copy import deepcopy
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton,QLineEdit,QLabel
from victoria3_capture import CapturePage
from tools.configurations import fingerprint
from pathlib import Path
import json
from tools.victoria3_local import atomic_write,encode


class ConfigurationCapture(CapturePage):
    saved=Signal(dict)
    def __init__(self,quick,repository):
        super().__init__(quick)
        self.repository=repository
        self.active_id=None
        self.side.clear();self.side.addItem('自动识别当前配置，独立保存测试','current')
        for label in self.findChildren(QLabel):
            if label.text().startswith('无需寻找或导入 CSV'):
                label.setText('采集结果仅保存在当前配置中。无需指定优化前或优化后；不同配置可在自由对比页任意比较。\n每次重新载入同一存档并固定场景。首次采集显示 8 秒准备倒计时。')
        self.confirm.setText('我已载入测试存档，确认游戏实际使用当前设置，并保持本配置的测试场景一致')
        self.context=QLineEdit();self.context.setPlaceholderText('场景标记（可选，例如：1900年俄罗斯、5速、固定视角）')
        self.layout().insertWidget(1,self.context)
        fresh=QPushButton('把当前设置另存为一条新配置记录')
        fresh.clicked.connect(self.new_record);self.layout().insertWidget(2,fresh)

    def current(self,force_new=False):
        machine=self.quick.service.discover()
        record=self.repository.current(machine,machine['settings'],self.active_id,force_new=force_new)
        self.active_id=record['id']
        return record

    def new_record(self):
        if self.job:return
        try:
            record=self.current(True)
            self.status.setText('已建立独立记录：'+record['name']+' · '+record['id'][:8])
        except (OSError,ValueError) as error:self.status.setText(str(error))

    def suggest_stage(self):
        if self.job:return
        try:
            record=self.current()
            self.side.setItemText(0,'当前配置：'+record['name']+' · '+record['id'][:8])
            self.status.setText('结果将独立保存到此配置；不会移动或覆盖其他配置的测试。')
        except (OSError,ValueError) as error:self.status.setText(str(error))

    def capture(self):
        try:
            if not self.confirm.isChecked():raise ValueError('请先载入存档，并勾选场景与当前设置确认框')
            if self.dates.worker or self.dates.pick_window:raise ValueError('请先完成日期区域识别')
            if self.dates.enabled.isChecked() and self.dates.config is None:raise ValueError('请先框选日期或取消日期识别')
            record=self.current()
            self.capture_pid=self.tool.game_pid()
            snapshot=deepcopy(record);context=self.context.text().strip();seconds=self.seconds.value()
            self.confirm.setChecked(False)
            self.countdown.setText('正在启动采集工具…')
            self.run_job(lambda cancel:self.capture_with_sensors(snapshot,seconds,cancel),
                         lambda result:self.save_configuration_capture(snapshot,context,result),timed=True)
            self.status.setText('8 秒后开始，测试将归属于配置 '+record['id'][:8])
        except (OSError,ValueError) as error:self.status.setText(str(error))

    def save_configuration_capture(self,snapshot,context,result):
        try:
            result['context']=context
            if 'telemetry' in result:
                result['telemetry']['days_per_second']=self.dates.finish_session()
                result['date_samples']=list(self.dates.samples)
                if result.get('sensor_file'):
                    path=Path(result['sensor_file']);data=json.loads(path.read_text(encoding='utf-8'))
                    data.update(summary=result['telemetry'],date_samples=result['date_samples'])
                    atomic_write(path,encode(data))
            machine=self.quick.service.discover()
            if fingerprint(machine['settings'],machine)!=snapshot['fingerprint']:raise ValueError('采集中配置或设备发生变化，原始文件保留但不关联')
            record=self.repository.append_capture(snapshot['id'],snapshot['fingerprint'],result)
            self.status.setText(f"已保存到 {record['name']}，共 {record['observations'].get('run_count',0)} 次测试")
            self.saved.emit(record)
        except (OSError,ValueError) as error:self.status.setText(str(error))
