"""Temporary snapshot hotkey. No keyboard recording; removed after use/cancel."""
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
from PySide6.QtWidgets import QApplication


class SnapshotHotkey(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.identifier = 0x5633
        self.registered = False

    def start(self):
        for key, code in [('F8', 0x77), ('F9', 0x78), ('F10', 0x79)]:
            if ctypes.windll.user32.RegisterHotKey(None, self.identifier, 0x4000, code):
                QApplication.instance().installNativeEventFilter(self)
                self.registered = True
                return key
        raise ValueError('F8/F9/F10 快捷键被占用，请关闭其他截图工具后重试')

    def stop(self):
        if self.registered:
            ctypes.windll.user32.UnregisterHotKey(None, self.identifier)
            QApplication.instance().removeNativeEventFilter(self)
            self.registered = False

    def nativeEventFilter(self, event_type, message):
        if self.registered:
            data = wintypes.MSG.from_address(int(message))
            if data.message == 0x0312 and data.wParam == self.identifier:
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0
