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
from app import DashboardPage, MainWindow, STYLESHEET, TaskState
from models.schemas import BenchmarkRecord


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

    def test_workspace_chart_uses_persisted_runs_and_evidence_expands(self):
        page = self.window.new_task
        page.begin_baseline_button.click()
        for inputs, avg in zip(page.baseline_run_inputs[:2], (200, 204)):
            inputs["avg_fps"].setValue(avg)
            inputs["low_fps"].setValue(140)
            inputs["gpu_usage"].setValue(90)
        page.baseline_confirmed_input.setChecked(True)
        page._finish_baseline()
        dashboard = self.window.dashboard
        current = self.window.controller.current_record(self.window.core_task.task_id)
        self.assertEqual(dashboard.run_chart.runs, current.runs)
        self.assertEqual([run["avg_fps"] for run in dashboard.run_chart.runs], [200, 204])
        self.assertTrue(dashboard.evidence_panel.isHidden())
        dashboard.evidence_button.click()
        self.assertFalse(dashboard.evidence_panel.isHidden())
        dashboard.evidence_button.click()
        self.assertTrue(dashboard.evidence_panel.isHidden())
        dashboard.findChildren(type(dashboard.evidence_button), "primaryButton")[0].click()
        self.assertEqual(self.window.stack.currentIndex(), 2)

    def test_summary_record_does_not_invent_measurements(self):
        record = BenchmarkRecord(200, 140, None, "1920 × 1080", "Test")
        page = DashboardPage(TaskState(), record=record)
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.run_chart.runs, [])
        self.assertIn("没有逐次", page.run_chart.accessibleDescription())
        self.assertIn("200 FPS", [item.text() for item in page.findChildren(QLabel, "metricValue")])

    def test_workspace_chart_reloads_from_saved_task(self):
        page = self.window.new_task
        page.begin_baseline_button.click()
        for inputs in page.baseline_run_inputs[:2]:
            inputs["avg_fps"].setValue(200)
            inputs["low_fps"].setValue(140)
            inputs["gpu_usage"].setValue(90)
        page.baseline_confirmed_input.setChecked(True)
        page._finish_baseline()
        task_id = self.window.core_task.task_id
        expected = self.window.dashboard.run_chart.runs
        self.window._restore_task(task_id)
        self.assertEqual(self.window.dashboard.run_chart.runs, expected)


if __name__ == "__main__":
    unittest.main()
