from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import NewTaskPage, TaskState


class MeasurementInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = TaskState(has_task=False)
        self.page = NewTaskPage(self.state)
        self.page.stage_stack.setCurrentIndex(1)
        self.page.resize(1200, 800)
        self.page.show()
        self.application.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.application.processEvents()

    def type_into(self, spin, text):
        self.page.ensureWidgetVisible(spin)
        QTest.mouseClick(spin.lineEdit(), Qt.MouseButton.LeftButton)
        spin.lineEdit().deselect()
        spin.lineEdit().setCursorPosition(len(spin.lineEdit().text()))
        QTest.keyClicks(spin, text)

    def test_hints_are_placeholders_and_direct_typing_works(self):
        for inputs in self.page.baseline_run_inputs[:2]:
            for key, value in (("avg_fps", "298"), ("low_fps", "176"), ("gpu_usage", "98")):
                with self.subTest(key=key):
                    spin = inputs[key]
                    self.assertEqual(spin.lineEdit().text(), "")
                    self.assertTrue(spin.lineEdit().placeholderText())
                    self.type_into(spin, value)
                    self.assertEqual(spin.value(), int(value))
                    self.assertEqual(spin.text(), value)

    def test_clearing_value_keeps_it_missing_after_focus_change(self):
        gpu = self.page.baseline_run_inputs[0]["gpu_usage"]
        self.type_into(gpu, "98")
        QTest.keyClick(gpu, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(gpu, Qt.Key.Key_Backspace)
        QTest.keyClick(gpu, Qt.Key.Key_Tab)
        self.assertEqual(gpu.value(), 0)
        self.assertEqual(gpu.lineEdit().text(), "")
        self.type_into(gpu, "100")
        self.assertEqual(gpu.value(), 100)

    def test_paste_and_replacement_preserve_numeric_limits(self):
        gpu = self.page.baseline_run_inputs[0]["gpu_usage"]
        self.application.clipboard().setText("97")
        gpu.setFocus()
        QTest.keyClick(gpu, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(gpu.value(), 97)
        gpu.lineEdit().selectAll()
        QTest.keyClicks(gpu, "85")
        self.assertEqual(gpu.value(), 85)
        gpu.lineEdit().selectAll()
        QTest.keyClicks(gpu, "abc")
        self.assertEqual(gpu.value(), 85)
        self.assertEqual(gpu.maximum(), 100)

    def test_blank_gpu_is_omitted_from_submitted_runs(self):
        for inputs in self.page.baseline_run_inputs[:2]:
            self.type_into(inputs["avg_fps"], "200")
            self.type_into(inputs["low_fps"], "140")
        self.page.baseline_confirmed_input.setChecked(True)
        self.page._finish_baseline()
        self.assertTrue(self.state.baseline_confirmed)
        self.assertTrue(all("gpu_usage" not in run for run in self.state.baseline_runs))

    def test_cleared_required_fps_is_rejected_on_submit(self):
        self.page.baseline_confirmed_input.setChecked(True)
        with patch("app.QMessageBox.warning") as warning:
            self.page._finish_baseline()
        warning.assert_called_once()
        self.assertFalse(self.state.baseline_confirmed)


if __name__ == "__main__":
    unittest.main()
