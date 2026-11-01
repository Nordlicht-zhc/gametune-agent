from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from agent.controller import GameTuneController
from agent.feedback import summarize_feedback
from agent.next_step import next_step_request
from app import MainWindow
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask


class PlayerProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "profiles.db"
        self.controller = GameTuneController(self.path)
        self.task = OptimizationTask("player", "Counter-Strike 2", "CPU", "GPU",
            "2560 × 1440", "300 Hz", "Workshop", 1000, 600,
            fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
        self.config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高",
                       "dynamic_shadows": "all", "msaa_mode": "8x_msaa", "texture_detail": "high"}
        self.baseline = BenchmarkRecord(200, 100, 99, self.task.resolution, "Workshop",
                                       repeats=3, configuration=dict(self.config))
        self.evidence, self.plan = self.controller.create_task(self.task, self.baseline)

    def profile(self, **updates):
        candidate = BenchmarkRecord(240, 130, 98, self.task.resolution, "Workshop",
            configuration={**self.config, "shadow_quality": "低", "msaa_mode": "4x_msaa"})
        return replace(candidate, **updates)

    def test_multiple_changes_become_current_and_replan_after_restart(self):
        candidate = self.profile()
        result = self.controller.evaluate(self.task, self.baseline, candidate, 1)
        self.assertEqual(result.stop_reason, "player_profile")
        self.assertFalse(result.accepted)
        self.assertFalse(result.rollback_required)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, candidate.record_id)
        evidence, plan = self.controller.plan_after_result(self.task, candidate, self.baseline, 2)
        options = self.controller.next_step_candidates(self.task, candidate, evidence, 2)
        self.assertTrue(options)
        for option in options:
            if option.setting_id in candidate.configuration:
                self.assertEqual(option.old_value, candidate.configuration[option.setting_id])
        payload = json.loads(next_step_request(self.task, candidate, evidence, options,
            history=self.controller.experiment_history(self.task.task_id)).messages[-1].content)
        self.assertEqual(payload["current_result"]["configuration"]["shadow_quality"], "低")
        feedback = summarize_feedback(self.controller.experiment_history(self.task.task_id))
        self.assertEqual(feedback["attempts"][-1]["outcome"], "adopted_unverified")
        self.assertFalse(feedback["avoid_repeating"])
        self.assertEqual(self.controller._diagnostic_results[self.task.task_id], [])
        window = MainWindow(database_path=self.path, controller=self.controller)
        window._load_state_from_task(self.task, candidate)
        self.assertEqual(window.state.shadow_quality, "低")
        self.assertEqual(window.state.video_settings["msaa_mode"], "4x_msaa")
        window.close()


    def test_confirmed_whole_profile_can_be_best_without_single_setting_claim(self):
        candidate = self.profile(repeats=3, runs=[{"avg_fps": 240, "low_fps": 130}] * 3)
        result = self.controller.evaluate(self.task, self.baseline, candidate, 1)
        self.assertTrue(result.accepted)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, candidate.record_id)
        self.assertFalse(result.diagnostic_summary)
        self.assertIn("整组", " ".join(result.reasons))

    def test_regression_is_recorded_without_forcing_player_to_restore(self):
        candidate = self.profile(avg_fps=160, low_fps=80, repeats=3)
        result = self.controller.evaluate(self.task, self.baseline, candidate, 1)
        self.assertFalse(result.accepted)
        self.assertFalse(result.rollback_required)
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, candidate.record_id)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)
        self.assertTrue(any("-20.0%" in reason for reason in result.reasons))

    def test_missing_profile_values_cannot_be_verified(self):
        candidate = self.profile(repeats=3, configuration={**self.config,
            "shadow_quality": "低", "texture_detail": "low", "msaa_mode": "unknown"})
        result = self.controller.evaluate(self.task, self.baseline, candidate, 1)
        self.assertFalse(result.measurement_valid)
        self.assertFalse(result.accepted)
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, candidate.record_id)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_scene_change_or_noise_cannot_become_verified_gain(self):
        for candidate in (self.profile(test_scene="different", comparable=False),
                          self.profile(runs=[{"avg_fps": 240, "low_fps": 130},
                                             {"avg_fps": 500, "low_fps": 250}])):
            with self.subTest(candidate=candidate):
                # Independent controller state for each measurement.
                controller = GameTuneController(self.path.with_name(candidate.record_id + ".db"))
                controller.create_task(self.task, self.baseline)
                result = controller.evaluate(self.task, self.baseline, candidate, 1)
                self.assertFalse(result.accepted)
                self.assertFalse(result.measurement_valid)
                self.assertFalse(result.rollback_required)
                self.assertEqual(controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_mouse_submission_of_multiple_settings_reaches_next_plan_and_report(self):
        from test_explanation_path import ExplanationProvider
        qt = QApplication.instance() or QApplication([])
        provider = ExplanationProvider()
        self.controller.model_provider = provider
        window = MainWindow(database_path=self.path, controller=self.controller)
        window.core_task, window.baseline_record = self.task, self.baseline
        window._load_state_from_task(self.task, self.baseline)
        window.current_evidence, window.current_plan = self.evidence, self.plan
        window._rebuild_task_pages()
        window.show()
        window.set_page(2)
        page = window.experiment
        page.avg.setValue(240)
        page.low.setValue(130)
        page.gpu.setValue(98)
        page.actual_setting.setText("低")
        page.actual_video_fields["msaa_mode"].setText("4x MSAA")
        page.actual_video_fields["texture_detail"].setText("高")
        page.ensureWidgetVisible(page.save_button)
        qt.processEvents()
        try:
            with patch("app.QMessageBox.information") as dialog, patch("app.QMessageBox.question") as rollback:
                QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    qt.processEvents()
                    if dialog.called and not window._planning_jobs and not window._explanation_jobs:
                        break
                    QTest.qWait(10)
                self.assertFalse(rollback.called)
                self.assertEqual(dialog.call_count, 1)
                self.assertEqual(window.state.round_number, 2)
                self.assertEqual(window.state.shadow_quality, "低")
                self.assertEqual(window.state.video_settings["msaa_mode"], "4x_msaa")
                self.assertNotEqual(window.current_plan.action, AgentAction.FINISH)
                self.assertTrue(window.experiment.save_button.isEnabled())
                self.assertIn("实际采用", window.current_explanation["summary"])
                requests = [r for r in provider.requests if r.metadata["purpose"] == "next_step_selection"]
                self.assertTrue(requests, (window.current_plan.title, [e.code for e in window.current_evidence], [r.metadata for r in provider.requests]))
                payload = json.loads(requests[0].messages[-1].content)
                self.assertEqual(payload["current_result"]["configuration"]["shadow_quality"], "低")
        finally:
            for thread, *_ in list(window._planning_jobs.values()) + list(window._explanation_jobs.values()):
                thread.quit()
                thread.wait(5000)
            window.close()
            qt.processEvents()
