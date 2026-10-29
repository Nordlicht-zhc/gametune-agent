from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from app import APISettingsPage, ExperimentPage, NewTaskPage, TaskState
from models.schemas import AgentAction, ExperimentPlan


class TextInputUsabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            page.close()
            page.deleteLater()
        self.application.processEvents()

    def show(self, page):
        self.pages.append(page)
        page.resize(1280, 900)
        page.show()
        self.application.processEvents()
        return page

    def replace_text(self, widget, text):
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(widget, text)

    def test_unknown_power_and_cap_allow_direct_input_and_clear(self):
        page = self.show(NewTaskPage(TaskState(fps_cap=None, gpu_power_w=None)))
        for spin, value in ((page.cap_input, "240"), (page.gpu_power_input, "120")):
            self.assertEqual(spin.text(), "")
            spin.setFocus()
            spin.lineEdit().deselect()
            QTest.keyClicks(spin, value)
            self.assertEqual(spin.value(), int(value))
            QTest.keyClick(spin, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(spin, Qt.Key.Key_Backspace)
            QTest.keyClick(spin, Qt.Key.Key_Tab)
            self.assertEqual(spin.text(), "")
            self.assertEqual(spin.value(), spin.minimum())
        self.replace_text(page.cap_input, "0")
        self.assertEqual(page.cap_input.text(), "0")
        self.assertEqual(page.cap_input.value(), 0)

    def test_numeric_controls_accept_multi_digit_replacement(self):
        setup = self.show(NewTaskPage(TaskState()))
        experiment = self.show(ExperimentPage(TaskState()))
        settings = self.show(APISettingsPage())
        for widget, text in ((setup.target_avg, "360"), (setup.target_low, "120"),
                (setup.menu_cap_input, "60"), (setup.baseline_duration_input, "120"),
                (experiment.avg, "1250"), (experiment.low, "750"), (experiment.repeats, "3"),
                (experiment.duration, "120"), (settings.timeout, "60")):
            with self.subTest(widget=type(widget).__name__, text=text):
                self.replace_text(widget, text)
                widget.interpretText()
                self.assertEqual(widget.value(), int(text))

    def test_text_fields_support_replace_clear_and_chinese_input(self):
        setup = self.show(NewTaskPage(TaskState()))
        experiment = self.show(ExperimentPage(TaskState()))
        settings = self.show(APISettingsPage())
        fields = [setup.name_input, setup.scene_input, setup.custom_cpu_input, setup.custom_gpu_input,
                  setup.custom_resolution_input, experiment.actual_cpu, experiment.actual_gpu,
                  experiment.actual_resolution, experiment.actual_refresh_rate, experiment.actual_scene,
                  experiment.adjustment_setting_name, experiment.adjustment_current_value,
                  experiment.adjustment_new_value, settings.base_url, settings.api_key, settings.model.lineEdit()]
        for field in fields:
            with self.subTest(field=fields.index(field)):
                self.replace_text(field, "sample-value")
                self.assertEqual(field.text(), "sample-value")
                QTest.keyClick(field, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                QTest.keyClick(field, Qt.Key.Key_Backspace)
                self.assertEqual(field.text(), "")
                event = QInputMethodEvent()
                event.setCommitString("测试输入")
                editor = field.lineEdit() if hasattr(field, "lineEdit") and field.lineEdit() else field
                self.application.sendEvent(editor, event)
                self.assertEqual(field.text(), "测试输入")
        self.assertEqual(settings.api_key.echoMode(), QLineEdit.EchoMode.Password)
        experiment.notes.setFocus()
        QTest.keyClicks(experiment.notes, "line one")
        QTest.keyClick(experiment.notes, Qt.Key.Key_Return)
        QTest.keyClicks(experiment.notes, "line two")
        self.assertEqual(experiment.notes.toPlainText(), "line one\nline two")

    def test_refresh_rate_can_be_customized_and_preserves_initial_value(self):
        state = TaskState(refresh_rate="75 Hz")
        page = self.show(NewTaskPage(state))
        self.assertEqual(page.refresh_input.currentText(), "75 Hz")
        self.replace_text(page.refresh_input, "60 Hz")
        page._begin_baseline()
        self.assertEqual(state.refresh_rate, "60 Hz")

    def test_empty_scene_and_invalid_targets_are_reported_before_baseline(self):
        page = self.show(NewTaskPage(TaskState()))
        page.scene_input.clear()
        with patch("app.QMessageBox.warning") as warning:
            page._begin_baseline()
        warning.assert_called_once()
        self.assertEqual(page.stage_stack.currentIndex(), 0)
        page.scene_input.setText("scene")
        page.target_avg.setValue(60)
        page.target_low.setValue(120)
        with patch("app.QMessageBox.warning") as warning:
            page._begin_baseline()
        warning.assert_called_once()

    def test_file_path_enter_imports_and_submission_imports_without_enter(self):
        page = self.show(ExperimentPage(TaskState()))
        saved = []
        page.result_saved.connect(saved.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.csv"
            path.write_text("avg_fps,1%_low\n1250,750\n", encoding="utf-8")
            page.file_input.setText(str(path))
            page.file_input.setFocus()
            QTest.keyClick(page.file_input, Qt.Key.Key_Return)
            self.assertEqual(page.avg.value(), 1250)
            self.assertEqual(page.gpu.text(), "")
            page._save()
            self.assertEqual(saved[-1]["gpu"], None)
            self.assertEqual(saved[-1]["runs"][0]["avg_fps"], 1250)
            path2 = Path(directory) / "second.csv"
            path2.write_text("avg_fps,1%_low,gpu_usage\n400,200,98\n", encoding="utf-8")
            page.file_input.setText(str(path2))
            self.assertIsNone(page._csv_metrics)
            page._save()
            self.assertEqual(saved[-1]["avg"], 400)
            self.assertEqual(saved[-1]["gpu"], 98)

    def test_bad_csv_path_blocks_submission_instead_of_using_old_data(self):
        page = self.show(ExperimentPage(TaskState()))
        saved = []
        page.result_saved.connect(saved.append)
        page.file_input.setText("missing-file.csv")
        with patch("app.QMessageBox.warning") as warning:
            page._save()
        warning.assert_called_once()
        self.assertEqual(saved, [])

    def test_experiment_requires_measurements_instead_of_prefilled_examples(self):
        page = self.show(ExperimentPage(TaskState()))
        self.assertEqual(page.avg.text(), "")
        self.assertEqual(page.low.text(), "")
        saved = []
        page.result_saved.connect(saved.append)
        with patch("app.QMessageBox.warning") as warning:
            page._save()
        warning.assert_called_once()
        self.assertEqual(saved, [])
        self.replace_text(page.avg, "300")
        self.replace_text(page.low, "200")
        page._save()
        self.assertEqual(saved[-1]["avg"], 300)
        self.replace_text(page.avg, "")
        QTest.keyClick(page.avg, Qt.Key.Key_Backspace)
        with patch("app.QMessageBox.warning") as warning:
            page._save()
        warning.assert_called_once()
        self.assertEqual(len(saved), 1)

    def test_input_locks_and_request_only_fields_stay_consistent(self):
        plan = ExperimentPlan(AgentAction.REQUEST_DATA, "确认", "设置", "未知", "记录",
            "依据", "假设", "方法", "标准", "无需恢复", 1, setting_id="power_mode")
        page = self.show(ExperimentPage(TaskState(), plan))
        page.set_planning(True)
        page.set_planning(False)
        self.assertFalse(page.avg.isEnabled())
        self.assertFalse(page.file_input.isEnabled())
        self.assertTrue(page.actual_setting.isEnabled())
        page.set_rollback_pending(True)
        for widget in (page.notes, page.configuration_confirmed, page.actual_setting, page.actual_scene, page.save_button):
            self.assertFalse(widget.isEnabled())
        saved = []
        page.result_saved.connect(saved.append)
        page._save()
        self.assertEqual(saved, [])
        page.set_completed(False)
        self.assertFalse(page.actual_setting.isEnabled())
        page.set_rollback_pending(False)
        self.assertTrue(page.actual_setting.isEnabled())
        self.assertFalse(page.avg.isEnabled())


if __name__ == "__main__":
    unittest.main()
