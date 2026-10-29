from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from agent.api import ModelAPIError, ModelResponse
from agent.controller import GameTuneController
from app import MainWindow
from models.schemas import BenchmarkRecord, OptimizationTask


class ExplanationProvider:
    name = "integration-mock"

    def __init__(self, fail=False, crash=False):
        self.fail = fail
        self.crash = crash
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.crash:
            raise RuntimeError("simulated worker failure")
        if self.fail:
            raise ModelAPIError("simulated unavailable")
        payload = json.loads(request.messages[-1].content)
        if request.metadata["purpose"] == "next_step_selection":
            data = {"candidate_id": payload["candidates"][0]["candidate_id"], "reason": "选择合法候选"}
        else:
            data = {"summary": "模型说明", "reasoning": ["模型原因"],
                    "next_step": "错误操作：恢复 1680 × 1050", "warning": ""}
        return ModelResponse(json.dumps(data), "mock", self.name)


class ExplanationPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def run_path(self, provider):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "path.db"
            controller = GameTuneController(db, model_provider=provider)
            task = OptimizationTask("path", "Counter-Strike 2", "CPU", "GPU",
                                    "2560 × 1440", "300 Hz", "Workshop", 1000, 600,
                                    fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
            config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高", "dynamic_shadows": "all"}
            baseline = BenchmarkRecord(300, 150, 95, task.resolution, task.test_scene,
                                       repeats=3, configuration=config)
            evidence, plan = controller.create_task(task, baseline)
            window = MainWindow(database_path=db, controller=controller)
            window.core_task, window.baseline_record = task, baseline
            window._load_state_from_task(task, baseline)
            window.current_evidence, window.current_plan = evidence, plan
            window._rebuild_task_pages()
            result = {"avg": 596, "low": 208, "gpu": 95, "repeats": 1, "duration": 110,
                      "condition": "有变化，需要说明", "configuration_confirmed": True,
                      "resolution": task.resolution, "configuration": dict(config)}
            try:
                with patch("app.QMessageBox.information") as dialog:
                    window.experiment.result_saved.emit(result)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        self.qt.processEvents()
                        if dialog.called and not window._explanation_jobs and not window._planning_jobs:
                            break
                        QTest.qWait(10)
                    self.assertEqual(dialog.call_count, 1)
                    self.assertFalse(window._explanation_jobs)
                    self.assertFalse(window._planning_jobs)
                    self.assertEqual(window.stack.currentIndex(), 2)
                    self.assertNotEqual("模型说明", window.current_explanation["summary"])
                    self.assertNotIn("1680", window.current_explanation["next_step"])
                    report_text = " ".join(label.text() for label in window.report.findChildren(QLabel))
                    self.assertIn(window.current_explanation["summary"], report_text)
                    self.assertEqual(len(controller.experiment_history(task.task_id)), 1)
                    if provider is not None and not provider.fail and not provider.crash:
                        requests = [r for r in provider.requests if r.metadata["purpose"] == "decision_explanation"]
                        self.assertEqual(len(requests), 1)
                        payload = json.loads(requests[0].messages[-1].content)
                        self.assertEqual(payload["workflow"]["submitted_result"]["gpu_usage"], 95)
                        self.assertEqual(payload["workflow"]["evaluated_plan"]["setting_id"], plan.setting_id)
                    else:
                        self.assertEqual(window.current_explanation["source"], "local")
            finally:
                for thread, *_ in list(window._planning_jobs.values()) + list(window._explanation_jobs.values()):
                    thread.quit()
                    thread.wait(5000)
                window.close()
                self.qt.processEvents()

    def test_signal_to_async_model_to_report_and_dialog(self):
        self.run_path(ExplanationProvider())

    def test_api_error_reaches_local_report_and_dialog(self):
        self.run_path(ExplanationProvider(fail=True))

    def test_offline_reaches_local_report_and_dialog(self):
        self.run_path(None)

    def test_worker_exception_reaches_specific_local_report_and_dialog(self):
        self.run_path(ExplanationProvider(crash=True))

    def test_save_confirms_actual_form_without_checkbox_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "preflight.db")
            task = OptimizationTask("check", "Counter-Strike 2", "CPU", "GPU",
                                    "2560 × 1440", "300 Hz", "Workshop", 1000, 600,
                                    fps_cap=0, vsync="disabled")
            baseline = BenchmarkRecord(200, 100, 60, task.resolution, task.test_scene,
                                       repeats=3, configuration={"fps_cap": "0", "vsync": "disabled"})
            evidence, plan = window.controller.create_task(task, baseline)
            window.core_task, window.baseline_record = task, baseline
            window._load_state_from_task(task, baseline)
            window.current_evidence, window.current_plan = evidence, plan
            window._rebuild_task_pages()
            window.experiment.avg.setValue(233)
            window.experiment.low.setValue(140)
            window._start_explanation = Mock()
            window._start_plan_selection = Mock()
            with patch("app.QMessageBox.warning") as warning:
                window.experiment.save_button.click()
            self.assertFalse(warning.called)
            self.assertEqual(window.experiment.avg.value(), 233)
            self.assertTrue(window.experiment.avg.isEnabled())
            history = window.controller.experiment_history(task.task_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["purpose"], "observation")
            self.assertTrue(history[0]["configuration_confirmed"])
            self.assertTrue(history[0]["valid"])
            self.assertEqual(window.controller._retry_counts[task.task_id], 0)
            window.close()

    def test_stopped_invalid_diagnostic_can_be_reopened_from_button(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "reopen.db"
            controller = GameTuneController(db)
            task = OptimizationTask("reopen", "Counter-Strike 2", "CPU", "GPU",
                                    "1680 × 1050", "300 Hz", "Workshop", 1000, 600,
                                    fps_cap=144, vsync="enabled", max_rounds=8)
            baseline = BenchmarkRecord(233, 140, 60, task.resolution, task.test_scene,
                                       repeats=3, configuration={"fps_cap": "144", "vsync": "enabled"})
            controller.create_task(task, baseline)
            for _ in range(2):
                controller.evaluate(task, baseline, replace(baseline, record_id=uuid4().hex, configuration_confirmed=False), 1)
            controller.confirm_rollback(task)
            evidence, stopped = controller.plan_after_result(task, baseline, baseline, 2)
            window = MainWindow(database_path=db, controller=controller)
            window.core_task, window.baseline_record = task, baseline
            window._load_state_from_task(task, baseline)
            window.state.round_number = 2
            window.current_evidence, window.current_plan = evidence, stopped
            window._rebuild_task_pages()
            window.experiment.set_completed(True)
            self.assertFalse(window.experiment.avg.isEnabled())
            with patch("app.QMessageBox.warning") as warning:
                window.experiment.retry_diagnostic_button.click()
            self.assertFalse(warning.called)
            self.assertTrue(window.experiment.avg.isEnabled())
            self.assertTrue(window.experiment.save_button.isEnabled())
            self.assertEqual(window.state.round_number, 2)
            self.assertEqual(window.current_plan.setting_id, "frame_limits")
            self.assertEqual(len(controller.experiment_history(task.task_id)), 2)
            window.close()
