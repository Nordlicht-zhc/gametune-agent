from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from agent.controller import GameTuneController
from app import ExperimentPage, TaskState
from models.schemas import AgentAction, BenchmarkRecord, ExperimentPlan, OptimizationTask


class ActualSettingsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.plan = ExperimentPlan(AgentAction.PROPOSE_EXPERIMENT, "分辨率诊断", "分辨率",
            "2560 × 1440", "1920 × 1080", "依据", "假设", "方法", "标准", "恢复分辨率", 1,
            setting_id="resolution", purpose="diagnostic")
        self.state = TaskState(resolution="2560 × 1440", shadow_quality="高", dynamic_shadows="all",
            video_settings={"msaa_mode": "8x_msaa", "texture_detail": "high"})
        self.page = ExperimentPage(self.state, self.plan)
        self.page.resize(1280, 940)
        self.page.show()
        self.application.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.application.processEvents()

    def test_environment_choices_keep_custom_input_and_sync_resolution(self):
        for field in (self.page.actual_cpu, self.page.actual_gpu, self.page.actual_resolution, self.page.actual_refresh_rate):
            self.assertIsInstance(field, QComboBox)
            self.assertTrue(field.isEditable())
            self.assertGreater(field.count(), 2)
        self.page.actual_resolution.setCurrentIndex(self.page.actual_resolution.findText("1920 × 1080"))
        self.assertEqual(self.page.actual_setting.text(), "1920 × 1080")
        self.page.actual_setting.setText("1728 × 1080")
        self.assertEqual(self.page.actual_resolution.text(), "1728 × 1080")
        self.assertEqual(self.page._actual_configuration()["resolution"], "1728 × 1080")

    def test_changed_conditions_reveal_quality_choices_and_preserve_edits(self):
        self.assertTrue(self.page.changed_settings_panel.isVisible())
        self.page.condition.setCurrentIndex(1)
        self.assertTrue(self.page.changed_settings_panel.isVisible())
        fields = self.page.actual_video_fields
        self.assertTrue({"shadow_quality", "dynamic_shadows", "msaa_mode", "texture_detail", "fsr", "fps_cap"} <= fields.keys())
        self.assertEqual(fields["msaa_mode"].text(), "8x MSAA")
        self.page.configuration_confirmed.setChecked(True)
        fields["msaa_mode"].setText("4x MSAA")
        fields["shadow_quality"].setText("中")
        self.assertFalse(self.page.configuration_confirmed.isChecked())
        self.page.condition.setCurrentIndex(0)
        self.assertTrue(self.page.changed_settings_panel.isVisible())
        self.assertEqual(self.page._actual_configuration()["msaa_mode"], "4x_msaa")
        self.page.condition.setCurrentIndex(1)
        self.assertEqual(fields["shadow_quality"].text(), "中")

    def test_csv_runs_survive_configuration_edit_and_changed_values_are_submitted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.csv"
            path.write_text("avg_fps,1%_low\n300,180\n301,181\n", encoding="utf-8")
            self.page.file_input.setText(str(path))
            self.assertTrue(self.page._import_entered_file())
            self.page.condition.setCurrentIndex(1)
            self.page.actual_video_fields["msaa_mode"].setText("4x MSAA")
            self.page.actual_resolution.setText("1920 × 1080")
            saved = []
            self.page.result_saved.connect(saved.append)
            self.page._save()
            self.assertEqual(len(saved[-1]["runs"]), 2)
            self.assertEqual(saved[-1]["configuration"]["msaa_mode"], "4x_msaa")
            self.assertEqual(saved[-1]["condition"], "有变化，需要说明")

    def test_extra_quality_change_is_not_attributed_to_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "settings.db")
            task = OptimizationTask("诊断", "Counter-Strike 2", "CPU", "GPU", self.state.resolution,
                "300 Hz", "scene", 1000, 600, fps_cap=0, vsync="disabled")
            config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高", "dynamic_shadows": "all", "msaa_mode": "8x_msaa"}
            baseline = BenchmarkRecord(200, 100, 60, task.resolution, "scene", repeats=2, configuration=config)
            controller.create_task(task, baseline)
            self.assertEqual(controller.pending_plan(task.task_id).setting_id, "resolution")
            self.page.actual_resolution.setText("1920 × 1080")
            self.page.actual_scene.setText("scene")
            self.page.actual_video_fields["fps_cap"].setText("0")
            self.page.actual_video_fields["msaa_mode"].setText("4x MSAA")
            # Even if the user switches the condition back to 'same', the
            # recorded extra edit must still fail the single-variable check.
            result = BenchmarkRecord(300, 150, 75, "1920 × 1080", "scene", configuration=self.page._actual_configuration())
            decision = controller.evaluate(task, baseline, result, 1)
            self.assertTrue(decision.measurement_valid)
            self.assertEqual(decision.action, AgentAction.KEEP_CHANGE)
            self.assertEqual(decision.stop_reason, "player_profile")
            self.assertFalse(decision.diagnostic_summary)
            self.assertIn("抗锯齿", " ".join(decision.reasons))

    def test_new_quality_controls_follow_rollback_and_planning_locks(self):
        self.page.condition.setCurrentIndex(1)
        for lock, unlock in ((lambda: self.page.set_planning(True), lambda: self.page.set_planning(False)),
                             (lambda: self.page.set_rollback_pending(True), lambda: self.page.set_rollback_pending(False))):
            lock()
            self.assertTrue(all(not field.isEnabled() for field in self.page.actual_video_fields.values()))
            unlock()
            self.assertTrue(all(field.isEnabled() for field in self.page.actual_video_fields.values()))

    def test_mouse_save_submits_actual_values_without_checkbox_gate(self):
        self.page.state.has_task = True
        self.page._sync_input_enabled()
        self.page.avg.setValue(233)
        self.page.low.setValue(140)
        # A late configuration edit may clear the optional checkbox.
        self.page.configuration_confirmed.setChecked(True)
        self.page.actual_resolution.setText("1680 × 1050")
        self.assertFalse(self.page.configuration_confirmed.isChecked())
        saved = []
        self.page.result_saved.connect(saved.append)
        self.page.ensureWidgetVisible(self.page.save_button)
        self.application.processEvents()
        QTest.mouseClick(self.page.save_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["configuration_confirmed"])
        self.assertEqual(saved[0]["resolution"], "1680 × 1050")
        self.assertEqual(saved[0]["avg"], 233)

    def test_native_keyboard_can_select_a_quality_option(self):
        self.page.condition.setCurrentIndex(1)
        field = self.page.actual_video_fields["shadow_quality"]
        self.page.ensureWidgetVisible(field)
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_Up)
        self.assertEqual(field.text(), "中")
        self.assertEqual(self.page._actual_configuration()["shadow_quality"], "中")


if __name__ == "__main__":
    unittest.main()
