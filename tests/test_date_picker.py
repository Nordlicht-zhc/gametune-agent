from unittest.mock import MagicMock, patch
import unittest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import QApplication
from date_hotkey import SnapshotHotkey
from victoria3_dates import RegionPicker


class DatePickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_picker_does_not_accept_focus_and_keeps_frozen_image(self):
        image = QPixmap(200, 100)
        image.fill(QColor('red'))
        picker = RegionPicker(self.app.primaryScreen(), image)
        image.fill(QColor('blue'))
        self.assertTrue(picker.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(picker.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(picker.image.toImage().pixelColor(0, 0), QColor('red'))
        picker.close()

    def test_hotkey_is_temporary_and_uses_available_key(self):
        user32 = MagicMock()
        user32.RegisterHotKey.side_effect = [0, 1]
        with patch('date_hotkey.ctypes.windll.user32', user32):
            hotkey = SnapshotHotkey(lambda: None)
            self.assertEqual(hotkey.start(), 'F9')
            self.assertTrue(hotkey.registered)
            hotkey.stop()
            self.assertFalse(hotkey.registered)
            user32.UnregisterHotKey.assert_called_once()

    def test_occupied_keys_do_not_leave_hook_registered(self):
        user32 = MagicMock()
        user32.RegisterHotKey.return_value = 0
        with patch('date_hotkey.ctypes.windll.user32', user32):
            hotkey = SnapshotHotkey(lambda: None)
            with self.assertRaises(ValueError):
                hotkey.start()
            self.assertFalse(hotkey.registered)
