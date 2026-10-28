from copy import deepcopy
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton,
                              QSpinBox, QVBoxLayout, QWidget)
from tools.presentmon import PresentMon, summarize_captures
from tools.victoria3_report import attach_benchmark, new_report
from tools.resolution import normalize_resolution


def graphics_differences(actual, expected):
    def normalized(key, value):
        if key == 'resolution' and isinstance(value, str):
            try:
                return normalize_resolution(value)
            except ValueError:
                pass
        return value
    return [f'{key}：报告 {value}，当前 {actual.get(key, "未显式保存")}'
            for key, value in expected.items() if normalized(key, actual.get(key)) != normalized(key, value)]


class CaptureJob(QThread):
    completed = Signal(object)
    failed = Signal(str)
    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel = Event()
    def run(self):
        try:
            self.completed.emit(self.operation(self.cancel))
        except Exception as error:
            self.failed.emit(str(error))


class CapturePage(QWidget):
    def __init__(self, quick):
        super().__init__()
        self.quick = quick
        self.tool = PresentMon(quick.reports.path.parent / 'presentmon')
        self.job = None
        root = QVBoxLayout(self)
        title = QLabel('自动性能采集')
        title.setObjectName('pageTitle')
        root.addWidget(title)
        note = QLabel('无需寻找或导入 CSV。先采集优化前，再应用方案并重启游戏，最后采集优化后。\n'
                      '每次开始后有 8 秒切回游戏；同一组可重复采集三次。每次请重新载入同一存档并固定视角、缩放与速度。\n'
                      '此处采集平均 FPS 和 1% Low，不采集跑日速度、温度或功耗。')
        note.setWordWrap(True)
        root.addWidget(note)
        self.prepare_button = QPushButton('一键准备开源采集工具（官方 PresentMon 2.6.0，SHA-256 校验）')
        self.prepare_button.clicked.connect(self.prepare)
        root.addWidget(self.prepare_button)
        self.side = QComboBox()
        self.side.addItem('采集优化前', 'baseline')
        self.side.addItem('采集优化后', 'candidate')
        root.addWidget(self.side)
        self.seconds = QSpinBox()
        self.seconds.setRange(10, 300)
        self.seconds.setValue(60)
        self.seconds.setSuffix(' 秒 / 次')
        root.addWidget(self.seconds)
        self.confirm = QCheckBox('我已载入同一测试存档，保持相同场景与采集条件，并确认本次游戏使用对应的优化前/后配置')
        root.addWidget(self.confirm)
        row = QHBoxLayout()
        self.start = QPushButton('开始采集并自动写入报告')
        self.start.setObjectName('primaryButton')
        self.start.clicked.connect(self.capture)
        row.addWidget(self.start)
        self.stop = QPushButton('取消采集')
        self.stop.clicked.connect(lambda: self.job.cancel.set() if self.job else None)
        row.addWidget(self.stop)
        root.addLayout(row)
        self.status = QLabel('首次使用准备一次工具即可。不安装驱动，不修改系统权限，不上传采集文件。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.elapsed = 0

    def tick(self):
        self.elapsed += 1
        self.status.setText(f'采集任务进行中 · {self.elapsed} 秒。请保持游戏场景不变，完成后自动保存。')

    def run_job(self, operation, callback, timed=False):
        if self.job:
            return
        self.start.setEnabled(False)
        self.prepare_button.setEnabled(False)
        self.job = CaptureJob(operation, self)
        self.job.completed.connect(callback)
        self.job.failed.connect(self.status.setText)
        self.job.finished.connect(self.finished)
        if timed:
            self.elapsed = 0
            self.timer.start()
        self.job.start()

    def finished(self):
        self.timer.stop()
        self.job.deleteLater()
        self.job = None
        self.start.setEnabled(True)
        self.prepare_button.setEnabled(True)

    def prepare(self):
        self.status.setText('正在从官方 GitHub 下载并校验工具，请稍候…')
        self.run_job(lambda cancel: self.tool.prepare(cancel), lambda _: self.status.setText('采集工具已就绪。载入游戏后即可开始。'))

    def suggest_stage(self):
        if self.job:
            return
        try:
            reports = self.quick.reports.list()
            report = next((r for r in reports if self.quick.current_report and r['id'] == self.quick.current_report['id']), None)
            if report and report['phase'] == 'applied':
                settings = self.quick.service.read_settings()['Graphics']
                if not graphics_differences(settings, report['after']['Graphics']):
                    self.side.setCurrentIndex(1)
                    self.confirm.setChecked(False)
                    self.status.setText('已识别当前使用优化后的设置，已选择“采集优化后”。如需采集优化前，请先在一键优化页恢复备份。')
        except (OSError, ValueError):
            pass

    def capture(self):
        try:
            if not self.confirm.isChecked():
                raise ValueError('请先载入游戏存档，再勾选开始按钮上方的确认框；无需查找文件')
            side = self.side.currentData()
            records = self.quick.reports.list()
            report = next((r for r in records if self.quick.current_report and r['id'] == self.quick.current_report['id']), None)
            if report is None:
                machine = self.quick.service.discover()
                report = new_report(machine, machine['settings'])
                self.quick.reports.save(report)
            settings = self.quick.service.read_settings()
            switched = False
            if side == 'baseline' and report['phase'] == 'applied' and not graphics_differences(settings['Graphics'], report['after']['Graphics']):
                side = 'candidate'
                switched = True
                self.side.setCurrentIndex(1)
            if side == 'candidate' and report['phase'] != 'applied':
                raise ValueError('尚未应用优化方案，请先采集优化前，或应用方案后再采集优化后')
            if side == 'baseline' and report['phase'] == 'restored':
                machine = self.quick.service.discover()
                report = new_report(machine, settings)
                self.quick.reports.save(report)
            expected = report['before'] if side == 'baseline' else report['after']
            differences = graphics_differences(settings['Graphics'], expected['Graphics'])
            if differences and side == 'baseline':
                machine = self.quick.service.discover()
                report = new_report(machine, settings)
                self.quick.reports.save(report)
            elif differences:
                raise ValueError('当前设置还不是本报告的优化后配置：\n' + '\n'.join(differences) + '\n请先在一键优化页应用对应方案，再重启游戏。')
            self.tool.game_pid()
            self.quick.current_report = report
            context = deepcopy(report)
            seconds = self.seconds.value()
            self.confirm.setChecked(False)
            self.run_job(lambda cancel: self.tool.capture(self.quick.reports.path.parent / 'captures', seconds, cancel),
                         lambda result: self.save_capture(context, side, settings, result), timed=True)
            self.status.setText(('当前已应用优化，自动切换为采集优化后。' if switched else '') + '采集将在 8 秒后开始，请切回 Victoria 3。')
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def save_capture(self, snapshot, side, settings, result):
        try:
            if self.quick.service.read_settings() != settings:
                raise ValueError('采集中设置发生变化，本次数据未关联报告')
            current = next(r for r in self.quick.reports.list() if r['id'] == snapshot['id'])
            if current['phase'] != snapshot['phase'] or current['before'] != snapshot['before'] or current['after'] != snapshot['after']:
                raise ValueError('采集中报告配置发生变化，本次未关联')
            captures = current.setdefault('captures', {}).setdefault(side, [])
            if captures and captures[0]['seconds'] != result['seconds']:
                raise ValueError('同组采集时长不同，请保持一致')
            captures.append(result)
            current = attach_benchmark(current, side, summarize_captures(captures), '内置 PresentMon 2.6.0')
            current['comparison_confirmed'] = bool(current.get('captures', {}).get('baseline') and current.get('captures', {}).get('candidate'))
            self.quick.reports.save(current)
            self.quick.current_report = current
            self.status.setText(f'已自动写入报告：本组 {len(captures)} 次，平均 FPS {result["metrics"]["avg_fps"]:.1f}，1% Low {result["metrics"]["low_fps"]:.1f}。')
            self.quick.show_report()
        except (OSError, ValueError, StopIteration) as error:
            self.status.setText(f'采集文件已保留，但未关联报告：{error}')
