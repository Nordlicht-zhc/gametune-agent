from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from agent.controller import GameTuneController
from agent.state_machine import WorkflowPhase, WorkflowState
from app import MainWindow
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask


class PlatformRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "graphics.db"
        self.controller = GameTuneController(self.path)
        self.task = OptimizationTask("graphics", "Counter-Strike 2", "CPU", "GPU",
            "1680 × 1050", "300 Hz", "Workshop", 1000, 600, is_laptop=True,
            gpu_power_w=100, power_mode="ac_performance", cooling_mode="performance",
            graphics_mode="hybrid", fps_cap=0, vsync="disabled")
        self.config = {"power_mode": "ac_performance", "cooling_mode": "performance",
                       "graphics_mode": "hybrid", "fps_cap": "0", "vsync": "disabled"}
        self.baseline = BenchmarkRecord(233, 140, 60, self.task.resolution, "Workshop",
                                       repeats=3, configuration=self.config)
        self.evidence, self.plan = self.controller.create_task(self.task, self.baseline)
        self.assertEqual(self.plan.setting_id, "graphics_mode")

    def candidate(self, **updates):
        record = BenchmarkRecord(542, 171, 90, self.task.resolution, "Workshop",
                                 configuration={**self.config, "graphics_mode": "dgpu"})
        return replace(record, **updates)

    def test_large_improvement_stays_applied_and_accumulates_across_restart(self):
        first = self.controller.evaluate(self.task, self.baseline, self.candidate(), 1)
        self.assertEqual(first.action, AgentAction.RETEST)
        self.assertFalse(first.rollback_required)
        self.assertFalse(first.accepted)
        self.assertAlmostEqual(first.avg_delta_pct, 132.62, places=2)
        self.assertEqual(self.controller.pending_plan(self.task.task_id).purpose, "optimization")
        self.assertEqual(self.controller.workflows[self.task.task_id].phase, WorkflowPhase.OPTIMIZATION)
        self.assertEqual(len(self.controller.experiment_history(self.task.task_id)), 1)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        second = self.controller.evaluate(self.task, self.baseline, self.candidate(), 1)
        self.assertEqual(second.action, AgentAction.RETEST)
        third_record = self.candidate()
        third = self.controller.evaluate(self.task, self.baseline, third_record, 1)
        self.assertTrue(third.accepted)
        self.assertFalse(third.rollback_required)
        self.assertEqual(self.controller.current_record(self.task.task_id).configuration["graphics_mode"], "dgpu")
        self.assertEqual(self.controller.best_record(self.task.task_id).repeats, 3)
        self.assertEqual(len(self.controller.best_record(self.task.task_id).runs), 3)
        self.assertEqual(len(self.controller.experiment_history(self.task.task_id)), 3)

    def test_three_existing_runs_are_accepted_immediately(self):
        record = self.candidate(repeats=3, runs=[{"avg_fps": 542, "low_fps": 171}] * 3)
        result = self.controller.evaluate(self.task, self.baseline, record, 1)
        self.assertTrue(result.accepted)
        self.assertFalse(result.rollback_required)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, record.record_id)

    def test_changed_scene_does_not_get_promoted_as_gain(self):
        result = self.controller.evaluate(self.task, self.baseline,
            self.candidate(test_scene="other", comparable=False), 1)
        self.assertFalse(result.accepted)
        self.assertFalse(result.measurement_valid)
        self.assertEqual(self.controller.pending_plan(self.task.task_id).purpose, "diagnostic")

    def test_ui_does_not_request_restore_for_promising_platform_test(self):
        window = MainWindow(database_path=self.path, controller=self.controller)
        window.core_task, window.baseline_record = self.task, self.baseline
        window._load_state_from_task(self.task, self.baseline)
        window.current_plan, window.current_evidence = self.plan, self.evidence
        window._rebuild_task_pages()
        window._start_plan_selection = lambda **kwargs: None
        window._start_explanation = lambda *args: None
        window.show()
        window.set_page(2)
        page = window.experiment
        page.avg.setValue(542)
        page.low.setValue(171)
        page.gpu.setValue(90)
        page.actual_setting.setText("独显直连")
        page.ensureWidgetVisible(page.save_button)
        self.qt.processEvents()
        with patch("app.QMessageBox.question") as restore:
            QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
        self.assertFalse(restore.called)
        self.assertFalse(window.experiment.rollback_pending)
        self.assertTrue(window.experiment.save_button.isEnabled())
        self.assertIn("先保留当前设置", window.experiment.result_feedback.text())
        window.close()
