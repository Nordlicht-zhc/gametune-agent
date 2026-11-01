import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit
from victoria3_motion import MotionStack, MotionTabs, motion_enabled, reveal
from victoria3_workspace import ConfigurationWindow


class VictoriaMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rapid_navigation_remains_interactive_and_clears_overlay(self):
        with patch('victoria3_motion.motion_enabled', return_value=True):
            stack = MotionStack()
            for _ in range(3):
                stack.addWidget(QLineEdit())
            stack.resize(500, 300)
            stack.show()
            try:
                stack.setCurrentIndex(1)
                self.assertIsNotNone(stack.transition.overlay)
                stack.setCurrentIndex(2)
                self.assertEqual(stack.currentIndex(), 2)
                self.assertTrue(stack.transition.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
                QTest.keyClicks(stack.currentWidget(), 'test')
                self.assertEqual(stack.currentWidget().text(), 'test')
                QTest.qWait(220)
                self.assertIsNone(stack.transition.overlay)
                self.assertIsNone(stack.transition.animation)
                self.assertIsNone(stack.currentWidget().graphicsEffect())
            finally:
                stack.close()

    def test_resize_and_hide_cancel_transition(self):
        with patch('victoria3_motion.motion_enabled', return_value=True):
            stack = MotionStack()
            stack.addWidget(QLineEdit()); stack.addWidget(QLineEdit())
            stack.show()
            try:
                stack.setCurrentIndex(1)
                stack.resize(650, 400)
                self.assertIsNone(stack.transition.overlay)
                stack.setCurrentIndex(0)
                stack.hide()
                self.assertIsNone(stack.transition.overlay)
            finally:
                stack.close()

    def test_tabs_rapid_changes_and_reduced_motion(self):
        tabs = MotionTabs()
        for _ in range(3):
            tabs.addTab(QLineEdit(), 'test')
        tabs.show()
        try:
            with patch('victoria3_motion.motion_enabled', return_value=True):
                tabs.setCurrentIndex(1); tabs.setCurrentIndex(2)
                self.assertIsNotNone(tabs.transition.overlay)
                QTest.qWait(190)
                self.assertIsNone(tabs.transition.overlay)
            with patch.dict(os.environ, {'GAMETUNE_REDUCED_MOTION': '1'}):
                self.assertFalse(motion_enabled())
                tabs.setCurrentIndex(0)
                self.assertIsNone(tabs.transition.overlay)
        finally:
            tabs.close()

    def test_workspace_marker_tracks_last_choice_and_resize(self):
        with tempfile.TemporaryDirectory() as folder:
            window = ConfigurationWindow(Path(folder) / 'test.db')
            window.show()
            try:
                QTest.qWait(30)
                for index in (1, 2, 4, 6, 3):
                    window.group.button(index).click()
                window.resize(1100, 760)
                QTest.qWait(240)
                self.assertEqual(window.stack.currentIndex(), 3)
                self.assertEqual(window.group.checkedId(), 3)
                self.assertEqual(window.navigation_marker.marker.y(), window.group.button(3).y())
                self.assertIsNone(window.stack.transition.overlay)
            finally:
                window.close()

    def test_disclosure_reverses_without_leaving_height_constraint(self):
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
        host = QWidget(); box = QVBoxLayout(host)
        content = QWidget(); inside = QVBoxLayout(content)
        inside.addWidget(QLabel('测试条件')); box.addWidget(content)
        content.hide(); host.show()
        try:
            with patch('victoria3_motion.motion_enabled', return_value=True):
                reveal(content, True); reveal(content, False); reveal(content, True)
                QTest.qWait(240)
                self.assertFalse(content.isHidden())
                self.assertEqual(content.maximumHeight(), 16777215)
                reveal(content, False)
                QTest.qWait(240)
                self.assertTrue(content.isHidden())
        finally:
            host.close()
