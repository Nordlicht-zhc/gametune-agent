from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QBoxLayout
from app import MainWindow, STYLESHEET


class RedesignWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_style = self.application.styleSheet()
        self.application.setStyleSheet(STYLESHEET)
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch("app.provider_from_env", return_value=None)
        self.saved = patch("app.load_saved_api_config", return_value=None)
        self.env.start()
        self.saved.start()
        self.window = MainWindow(database_path=Path(self.folder.name) / "redesign.sqlite")
        self.window.show()
        QTest.qWait(40)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.saved.stop()
        self.env.stop()
        self.folder.cleanup()
        self.application.setStyleSheet(self.old_style)

    def test_startup_does_not_present_prefilled_values_as_test_results(self):
        self.assertFalse(self.window.state.has_task)
        self.assertIsNone(self.window.core_task)
        self.assertEqual(self.window.history.table.rowCount(), 0)
        self.assertFalse(self.window.experiment.save_button.isEnabled())
        self.assertEqual(self.window.dashboard.findChildren(QLabel, "metricValue"), [])
        values = [item.text() for item in self.window.report.findChildren(QLabel, "metricValue")]
        self.assertEqual(values, ["尚未开始", "待验证", "待验证"])

    def test_empty_pages_have_working_creation_actions(self):
        QTest.mouseClick(self.window.dashboard.create_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.window.set_page(2)
        QTest.qWait(200)
        QTest.mouseClick(self.window.experiment.create_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.stack.currentIndex(), 1)

    def test_help_opens_closes_and_can_be_reopened(self):
        self.window.sidebar.help_button.click()
        guide = self.window._usage_guide
        self.assertTrue(guide.isVisible())
        text = " ".join(item.text() for item in guide.findChildren(QLabel))
        self.assertIn("每次至少 60 秒", text)
        self.assertIn("先在游戏里恢复", text)
        guide.accept()
        self.assertFalse(guide.isVisible())
        self.window.sidebar.help_button.click()
        self.assertIs(self.window._usage_guide, guide)
        self.assertTrue(guide.isVisible())
        guide.accept()

    def test_form_reflows_at_small_window_and_restores_two_columns(self):
        self.window.set_page(1)
        self.window.resize(1050, 700)
        QTest.qWait(220)
        page = self.window.new_task
        self.assertEqual(page._columns.direction(), QBoxLayout.Direction.TopToBottom)
        self.assertEqual(page.horizontalScrollBar().maximum(), 0)
        self.window.resize(1280, 820)
        QTest.qWait(100)
        self.assertEqual(page._columns.direction(), QBoxLayout.Direction.LeftToRight)
        self.assertEqual(page.horizontalScrollBar().maximum(), 0)

    def test_baseline_switch_releases_hidden_form_height(self):
        self.window.set_page(1)
        page = self.window.new_task
        page.begin_baseline_button.click()
        QTest.qWait(250)
        self.assertEqual(page.stage_stack.currentIndex(), 1)
        self.assertEqual(page.verticalScrollBar().maximum(), 0)
        self.assertTrue(page.baseline_run_labels[2].isHidden())
        page._return_to_setup()
        QTest.qWait(250)
        self.assertEqual(page.stage_stack.currentIndex(), 0)
        self.assertGreater(page.verticalScrollBar().maximum(), 0)

    def test_third_run_label_follows_baseline_variation(self):
        page = self.window.new_task
        page.begin_baseline_button.click()
        for inputs, avg in zip(page.baseline_run_inputs[:2], (200, 240)):
            inputs["avg_fps"].setValue(avg)
            inputs["low_fps"].setValue(140)
        QTest.qWait(200)
        self.assertFalse(page.baseline_run_rows[2].isHidden())
        self.assertFalse(page.baseline_run_labels[2].isHidden())
        page.baseline_run_inputs[1]["avg_fps"].setValue(200)
        QTest.qWait(200)
        self.assertTrue(page.baseline_run_rows[2].isHidden())
        self.assertTrue(page.baseline_run_labels[2].isHidden())

    def test_baseline_submission_replaces_empty_workspace_with_persisted_data(self):
        page = self.window.new_task
        page.name_input.setText("UI regression fixture")
        page.begin_baseline_button.click()
        self.assertEqual(page.stage_stack.currentIndex(), 1)
        self.assertIsNone(self.window.core_task)
        for inputs in page.baseline_run_inputs[:2]:
            inputs["avg_fps"].setValue(200)
            inputs["low_fps"].setValue(140)
            inputs["gpu_usage"].setValue(90)
        page.baseline_confirmed_input.setChecked(True)
        page._finish_baseline()
        self.assertTrue(self.window.state.has_task)
        self.assertIsNotNone(self.window.core_task)
        self.assertEqual(len(self.window.controller.repository.list_tasks()), 1)
        self.assertEqual(self.window.history.table.rowCount(), 1)
        self.assertTrue(self.window.dashboard.findChildren(QLabel, "metricValue"))
        self.assertEqual(self.window.stack.currentIndex(), 0)


if __name__ == "__main__":
    unittest.main()
