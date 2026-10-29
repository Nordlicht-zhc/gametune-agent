"""User-selected, local-only game-date OCR. No game input or memory access."""
import ctypes
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QRect, QThread, QTimer, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QApplication, QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from tools.presentmon import PresentMon
from tools.game_date_ocr import recognize_date, summarize_dates


def foreground_pid():
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


class RegionPicker(QWidget):
    selected = Signal(object)
    canceled = Signal()

    def __init__(self, screen, pixmap):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.image = pixmap
        self.setGeometry(screen.geometry())
        self.origin = None
        self.selection = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.image)
        painter.setPen(QColor('#d7ff62'))
        painter.drawText(24, 40, '仅框选游戏日期，例如 1900年1月15日。按 Esc 取消。截图仅在本机识别。')
        painter.drawRect(self.selection)

    def mousePressEvent(self, event):
        self.origin = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            self.selection = QRect(self.origin, event.position().toPoint()).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.origin is None:
            return
        self.selection = QRect(self.origin, event.position().toPoint()).normalized().intersected(self.rect())
        if self.selection.width() >= 20 and self.selection.height() >= 10:
            self.selected.emit([self.selection.x() / self.width(), self.selection.y() / self.height(),
                                self.selection.width() / self.width(), self.selection.height() / self.height()])
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.canceled.emit()
            self.close()


class OcrJob(QThread):
    result = Signal(object)

    def __init__(self, path, at_seconds, calibration, parent=None):
        super().__init__(parent)
        self.path, self.at_seconds, self.calibration = path, at_seconds, calibration

    def run(self):
        try:
            value = {'date': recognize_date(self.path), 'at_seconds': self.at_seconds}
        except Exception as error:
            value = {'error': str(error)}
        finally:
            self.path.unlink(missing_ok=True)
        self.result.emit({'calibration': self.calibration, **value})


class DatePanel(QWidget):
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.config = None
        self.config_path = self.directory / 'date-region.json'
        self.worker = None
        self.active = False
        self.samples = []
        self.last_sample = -10000
        self.error = ''
        self.picker = None
        self.pick_window = None
        root = QVBoxLayout(self)
        self.enabled = QCheckBox('同时自动识别游戏日期，计算跑日速度（首次需框选日期）')
        root.addWidget(self.enabled)
        row = QHBoxLayout()
        self.prepare_button = QPushButton('准备日期识别组件')
        row.addWidget(self.prepare_button)
        select = QPushButton('框选游戏日期区域（一次）')
        self.select_button = select
        select.clicked.connect(self.select_region)
        row.addWidget(select)
        root.addLayout(row)
        self.status = QLabel('本地 Windows OCR，不上传截图。只保存选区和识别出的日期；无法识别时不估算。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        try:
            config = json.loads(self.config_path.read_text(encoding='utf-8'))
            x, y, width, height = config['region']
            if min(x, y) >= 0 and width > 0 and height > 0 and x + width <= 1 and y + height <= 1:
                self.config = config
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def select_region(self):
        if self.worker or self.active:
            self.status.setText('请先等待当前采集或识别结束。')
            return
        try:
            self.pid = PresentMon.game_pid()
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.status.setText('3 秒后显示框选界面，请切回 Victoria 3，再拖动框选游戏日期。')
        self.pick_window = self.window()
        self.pick_maximized = self.pick_window.isMaximized()
        self.pick_screen = self.screen()
        self.pick_window.showMinimized()
        QTimer.singleShot(3000, self.show_picker)

    def restore_picker_window(self):
        if self.pick_window:
            self.pick_window.showMaximized() if self.pick_maximized else self.pick_window.showNormal()
            self.pick_window.activateWindow()
            self.pick_window = None

    def show_picker(self):
        if foreground_pid() != self.pid:
            self.status.setText('未检测到前台 Victoria 3。请重试，并在 3 秒内切回游戏。')
            self.restore_picker_window()
            return
        self.picker = RegionPicker(self.pick_screen, self.pick_screen.grabWindow(0))
        self.picker.selected.connect(self.region_selected)
        self.picker.canceled.connect(self.restore_picker_window)
        self.picker.show()

    def region_selected(self, region):
        try:
            self.config = {'screen': self.pick_screen.name(), 'region': region}
            path = self.crop(self.picker.image)
            self.restore_picker_window()
            self.start_ocr(path, 0, True)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            self.restore_picker_window()

    def crop(self, image):
        x, y, width, height = self.config['region']
        cropped = image.copy(QRect(round(x * image.width()), round(y * image.height()),
                                  round(width * image.width()), round(height * image.height())))
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (uuid4().hex + '.date.png')
        if not cropped.save(str(path)):
            raise ValueError('无法保存本地日期选区')
        return path

    def start_ocr(self, path, at_seconds, calibration=False):
        self.worker = OcrJob(path, at_seconds, calibration, self)
        self.worker.result.connect(self.on_result)
        self.worker.finished.connect(self.worker_done)
        self.worker.start()

    def worker_done(self):
        self.worker.deleteLater()
        self.worker = None

    def on_result(self, result):
        if 'error' in result:
            self.error = result['error']
            self.status.setText(self.error)
            return
        if result['calibration']:
            self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
            self.enabled.setChecked(True)
            self.status.setText(f'识别日期：{result["date"]}。选区已保存；请核对正确后开始采集。')
        elif self.active:
            self.samples.append({'date': result['date'], 'at_seconds': result['at_seconds']})
            self.status.setText(f'已识别 {len(self.samples)} 次日期，最近：{result["date"]}')

    def start_session(self, pid):
        self.samples = []
        self.error = ''
        self.last_sample = -10000
        self.pid = pid
        self.active = self.enabled.isChecked() and self.config is not None

    def observe(self, milliseconds, seconds):
        if not self.active or self.worker or milliseconds < 8000 or milliseconds >= 8000 + seconds * 1000:
            return
        if milliseconds - self.last_sample < 5000:
            return
        self.last_sample = milliseconds
        try:
            if foreground_pid() != self.pid:
                self.error = '日期采样时游戏不在前台，未读取其他程序屏幕'
                return
            screen = next((screen for screen in QApplication.screens() if screen.name() == self.config['screen']), None)
            if screen is None:
                raise ValueError('游戏日期所在显示器已改变，请重新框选')
            self.start_ocr(self.crop(screen.grabWindow(0)), (milliseconds - 8000) / 1000)
        except (OSError, ValueError) as error:
            self.error = str(error)

    def finish_session(self):
        self.active = False
        if not self.enabled.isChecked() or self.config is None:
            return {'value': None, 'status': 'not_sampled', 'reason': '未启用游戏日期识别，或尚未框选日期区域'}
        result = summarize_dates(self.samples)
        if result['status'] != 'ok' and self.error:
            result['reason'] = self.error
        return result
