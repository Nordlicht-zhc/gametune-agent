from copy import deepcopy
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal, QTimer, QElapsedTimer, Qt
from math import ceil
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton,
                              QSpinBox, QVBoxLayout, QWidget, QApplication)
from tools.presentmon import PresentMon, summarize_captures
from tools.victoria3_report import attach_benchmark, new_report
from tools.resolution import normalize_resolution
from tools.windows_launch import is_admin, restart_as_admin
from admin_handoff import AdminHandoff
from tools.app_version import APP_VERSION
from tools.hardware_telemetry import TelemetrySession, attach_telemetry
from tools.victoria3_local import atomic_write, encode
from victoria3_dates import DatePanel
from tools.game_date_ocr import prepare_ocr


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
    countdown_started = Signal()
    def __init__(self, quick):
        super().__init__()
        self.quick = quick
        self.tool = PresentMon(quick.reports.path.parent / 'presentmon')
        self.job = None
        self.handoff = None
        root = QVBoxLayout(self)
        title = QLabel('自动性能采集')
        title.setObjectName('pageTitle')
        root.addWidget(title)
        note = QLabel('无需寻找或导入 CSV。先采集优化前，再应用方案并重启游戏，最后采集优化后。\n'
                      '每次开始后有 8 秒切回游戏；同一组可重复采集三次。每次请重新载入同一存档并固定视角、缩放与速度。\n'
                      '同步采集 FPS、整机 CPU/GPU 占用及可用温度/功耗；跑日速度可用下方日期识别，或高级受控测试。')
        note.setWordWrap(True)
        root.addWidget(note)
        self.dates = DatePanel(quick.reports.path.parent)
        self.dates.prepare_button.clicked.connect(self.prepare_dates)
        root.addWidget(self.dates)
        self.permission = QLabel(f'程序版本：v{APP_VERSION}（已支持 msBetweenPresents 表头）\n当前权限：' + ('管理员，可尝试启动 ETW 采集' if is_admin() else '普通用户；ETW 权限不足时需管理员启动'))
        self.permission.setWordWrap(True)
        root.addWidget(self.permission)
        self.admin_button = QPushButton('以管理员权限打开采集页（需要确认 Windows 弹窗）')
        self.admin_button.clicked.connect(self.open_admin)
        self.admin_button.setVisible(not is_admin())
        root.addWidget(self.admin_button)
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
        self.countdown = QLabel('准备就绪 · 开始后显示 8 秒倒计时')
        self.countdown.setObjectName('metricValue')
        self.countdown.setWordWrap(True)
        self.countdown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        root.addWidget(self.countdown)
        self.status = QLabel('首次使用准备一次工具即可。不安装驱动，不修改系统权限，不上传采集文件。')
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        root.addWidget(self.status)
        copy_button = QPushButton('复制当前提示 / 报错')
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(f'Victoria 3 Optimizer v{APP_VERSION}\n' + self.status.text()))
        root.addWidget(copy_button)
        self.copy_button = copy_button
        root.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.clock = QElapsedTimer()
        self.capture_window = None
        self.capture_was_maximized = False
        self.overlay = QLabel('', None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.setStyleSheet('background:#202421;color:#d7ff62;padding:16px;border-radius:10px;font-size:24px;')
        self.countdown_started.connect(self.begin_countdown)

    def begin_countdown(self):
        self.clock.start()
        self.dates.start_session(getattr(self, 'capture_pid', 0))
        self.capture_window = self.window()
        self.capture_was_maximized = self.capture_window.isMaximized()
        self.capture_window.showMinimized()
        self.countdown.setText('8 秒后开始采集 · 请切回游戏')
        self.overlay.setText('8 秒后开始采集')
        self.overlay.adjustSize()
        screen = self.screen().availableGeometry()
        self.overlay.move(screen.center().x() - self.overlay.width() // 2, screen.top() + 24)
        self.overlay.show()
        self.timer.start()

    def tick(self):
        if not self.clock.isValid():
            return
        milliseconds = self.clock.elapsed()
        self.dates.observe(milliseconds, self.seconds.value())
        if milliseconds < 8000:
            self.countdown.setText(f'{ceil((8000 - milliseconds) / 1000)} 秒后开始采集 · 请切回游戏')
            self.overlay.setText(f'{ceil((8000 - milliseconds) / 1000)} 秒后开始采集')
        else:
            remaining = max(0, ceil((self.seconds.value() * 1000 - (milliseconds - 8000)) / 1000))
            self.countdown.setText(f'正在采集 · 剩余约 {remaining} 秒' if remaining else '采集结束 · 正在计算并保存报告…')
            self.overlay.setText(f'采集中 · 剩余 {remaining} 秒' if remaining else '正在计算并保存报告…')

    def open_admin(self):
        if self.dates.worker:
            self.status.setText('请等待日期识别结束后再切换权限。')
            return
        if self.job:
            self.status.setText('请先等待当前任务结束或取消，再打开管理员窗口。')
            return
        if getattr(self.window(), 'job', None):
            self.status.setText('请先等待模型请求结束，再切换管理员窗口。')
            return
        try:
            if self.handoff:
                return
            self.handoff = AdminHandoff(self)
            self.handoff.ready.connect(self.admin_ready)
            self.handoff.expired.connect(self.admin_timeout)
            self.admin_button.setEnabled(False)
            self.window().setEnabled(False)
            restart_as_admin(self.handoff.port, self.handoff.token)
            self.handoff.timer.start(60000)
            self.status.setText('正在等待管理员窗口就绪；成功后本窗口会自动关闭。取消或启动失败时保留本窗口。')
        except (OSError, ValueError) as error:
            self.clear_handoff()
            self.status.setText(str(error))

    def clear_handoff(self):
        if self.handoff:
            self.handoff.stop()
            self.handoff.deleteLater()
            self.handoff = None
        self.admin_button.setEnabled(True)
        self.window().setEnabled(True)

    def admin_ready(self):
        self.clear_handoff()
        self.window().close()

    def admin_timeout(self):
        self.clear_handoff()
        self.status.setText('未收到管理员窗口就绪确认，当前窗口已保留。请检查 Windows 提示后重试。')

    def run_job(self, operation, callback, timed=False):
        if self.job:
            return
        self.start.setEnabled(False)
        self.prepare_button.setEnabled(False)
        self.dates.prepare_button.setEnabled(False)
        self.dates.select_button.setEnabled(False)
        self.seconds.setEnabled(False)
        self.side.setEnabled(False)
        self.job = CaptureJob(operation, self)
        self.job.completed.connect(callback)
        self.job.failed.connect(self.status.setText)
        self.job.finished.connect(self.finished)
        self.job.start()

    def finished(self):
        self.timer.stop()
        self.dates.active = False
        self.overlay.hide()
        self.restore_capture_window()
        self.countdown.setText('本次任务已结束 · 结果或报错见下方（可复制）')
        self.job.deleteLater()
        self.job = None
        self.start.setEnabled(True)
        self.prepare_button.setEnabled(True)
        self.dates.prepare_button.setEnabled(True)
        self.dates.select_button.setEnabled(True)
        self.seconds.setEnabled(True)
        self.side.setEnabled(True)

    def restore_capture_window(self):
        if self.capture_window is not None:
            window = self.capture_window
            self.capture_window = None
            if self.capture_was_maximized:
                window.showMaximized()
            else:
                window.showNormal()
            window.raise_()
            window.activateWindow()

    def prepare(self):
        self.status.setText('正在从官方 GitHub 下载并校验工具，请稍候…')
        self.run_job(lambda cancel: self.tool.prepare(cancel), lambda _: self.status.setText('采集工具已就绪。载入游戏后即可开始。'))

    def prepare_dates(self):
        self.status.setText('正在准备本地 Windows OCR 组件，不修改系统脚本策略…')
        self.run_job(prepare_ocr, lambda _: self.status.setText('日期识别组件已就绪，请框选一次游戏日期。'))

    def suggest_stage(self):
        if self.job:
            return
        try:
            reports = self.quick.reports.list()
            report = next((r for r in reports if self.quick.current_report and r['id'] == self.quick.current_report['id']), None)
            if report and report['phase'] in ('assessment', 'restored'):
                self.side.setCurrentIndex(0)
                self.confirm.setChecked(False)
                self.status.setText('当前尚未优化或已恢复原设置，已选择“采集优化前”。')
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
            if self.dates.worker or self.dates.pick_window:
                raise ValueError('请先等待日期识别结束，再开始采集')
            if self.dates.enabled.isChecked() and self.dates.config is None:
                raise ValueError('请先框选一次游戏日期区域，或取消同时识别日期')
            self.capture_pid = self.tool.game_pid()
            self.quick.current_report = report
            context = deepcopy(report)
            seconds = self.seconds.value()
            self.confirm.setChecked(False)
            self.countdown.setText('正在启动采集工具…')
            self.run_job(lambda cancel: self.capture_with_sensors(context, seconds, cancel),
                         lambda result: self.save_capture(context, side, settings, result), timed=True)
            self.status.setText(('当前已应用优化，自动切换为采集优化后。' if switched else '') + '采集将在 8 秒后开始，请切回 Victoria 3。')
        except (OSError, ValueError) as error:
            self.status.setText(str(error))

    def capture_with_sensors(self, report, seconds, cancel):
        telemetry = TelemetrySession(report['machine'].get('gpu', ''))
        def started():
            telemetry.start()
            self.countdown_started.emit()
        try:
            result = self.tool.capture(self.quick.reports.path.parent / 'captures', seconds, cancel, on_started=started)
        finally:
            summary = telemetry.stop()
        result['telemetry'] = summary
        path = Path(result['csv']).with_suffix('.sensors.json')
        atomic_write(path, encode({'summary': summary, 'samples': telemetry.samples,
                                  'timing': 'PresentMon 启动后排除 8 秒准备期，仅统计采集期间返回的样本'}))
        result['sensor_file'] = str(path)
        return result

    def save_capture(self, snapshot, side, settings, result):
        try:
            date_summary = self.dates.finish_session()
            if 'telemetry' in result:
                result['telemetry']['days_per_second'] = date_summary
                result['date_samples'] = list(self.dates.samples)
                if result.get('sensor_file'):
                    import json
                    sensor_path = Path(result['sensor_file'])
                    saved = json.loads(sensor_path.read_text(encoding='utf-8'))
                    saved['summary'] = result['telemetry']
                    saved['date_samples'] = result['date_samples']
                    atomic_write(sensor_path, encode(saved))
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
            current = attach_telemetry(current, side, captures)
            current['comparison_confirmed'] = bool(current.get('captures', {}).get('baseline') and current.get('captures', {}).get('candidate'))
            self.quick.reports.save(current)
            self.quick.current_report = current
            self.status.setText(f'已自动写入报告：本组 {len(captures)} 次，平均 FPS {result["metrics"]["avg_fps"]:.1f}，1% Low {result["metrics"]["low_fps"]:.1f}。')
            self.quick.show_report()
        except (OSError, ValueError, StopIteration) as error:
            self.status.setText(f'采集文件已保留，但未关联报告：{error}')
