"""Settings operations create configuration snapshots, never paired reports."""
from hashlib import sha256
from pathlib import Path
import sqlite3
from PySide6.QtWidgets import QPushButton
from victoria3_quick import Victoria3Quick
from tools.configurations import now


class ConfigurationQuick(Victoria3Quick):
    def __init__(self, configurations, database_path=None):
        super().__init__(database_path=database_path)
        self.configurations=configurations
        for button in self.findChildren(QPushButton):
            if button.text().startswith('查看优化报告'):
                button.setText('查看当前配置报告 / 自由对比')

    def scan(self):
        try:
            data=self.service.discover();self.machine=data
            graphics=data['settings']['Graphics']
            self.summary.setText(f"已识别 Victoria 3 · {data['version']}\n分辨率 {graphics.get('resolution','未识别')}\n{data['cpu']} / {data['gpu']}\n发现 {len(data['saves'])} 个本地存档。设备信息来自最近一次游戏日志。")
            self.saves.clear()
            for path in data['saves']:self.saves.addItem(path.stem,str(path))
            self.discovered.emit(data);self.select_save();self.refresh_backups();self.preview()
        except (OSError,ValueError,sqlite3.Error) as error:
            self.plan=None;self.apply_button.setEnabled(False);self.summary.setText(str(error))

    def refresh_backups(self):
        self.backups.clear()
        for path in self.service.backup_list():self.backups.addItem(path.stem,str(path))
        action=next((row for row in self.configurations.actions() if row['phase'] in ('applied','external_change')),None)
        self.restore_latest_button.setEnabled(action is not None)
        self.original_note.setText('每套设置独立保存在配置库。恢复操作不改变任何配置的历史测试。')

    def apply(self):
        try:
            if self.plan is None:raise ValueError('请先重新扫描并预览方案')
            machine=self.service.discover()
            original=self.configurations.current(machine,machine['settings'])
            backup=self.service.apply(self.plan)
            current=self.service.discover()
            updated=self.configurations.current(current,current['settings'])
            self.configurations.save_action({'id':sha256(str(backup).encode()).hexdigest(),'created_at':now(),
                'backup':str(backup),'phase':'applied','configuration_ids':[original['id'],updated['id']]})
            self.plan=None;self.apply_button.setEnabled(False);self.refresh_backups()
            self.status.setText('方案已应用并备份。两套设置已独立保存到配置库，可自行选择配置发起对比。')
            self.discovered.emit(current);self.report_requested.emit()
        except (OSError,ValueError,sqlite3.Error) as error:self.status.setText(str(error))

    def restore_latest(self):
        action=next((row for row in self.configurations.actions() if row['phase'] in ('applied','external_change')),None)
        if not action:self.status.setText('没有待恢复的设置。');return
        self.refresh_backups();index=self.backups.findData(action['backup'])
        if index<0:self.status.setText('备份文件不存在，未修改设置。');return
        self.backups.setCurrentIndex(index);self.restore()

    def restore(self):
        try:
            path=self.backups.currentData()
            if not path:raise ValueError('没有可用备份')
            self.service.restore(Path(path))
            for action in self.configurations.actions():
                if action['backup']==path:
                    action['phase']='restored';self.configurations.save_action(action)
            machine=self.service.discover();self.configurations.current(machine,machine['settings'])
            self.plan=None;self.apply_button.setEnabled(False);self.refresh_backups()
            self.status.setText('设置已恢复。配置库中的每套设置与采集结果保持独立保留。')
            self.discovered.emit(machine);self.report_requested.emit()
        except (OSError,ValueError,sqlite3.Error) as error:self.status.setText(str(error))

    def show_report(self):
        self.report_requested.emit()
