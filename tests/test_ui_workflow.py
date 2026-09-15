from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from app import ExperimentPage, MainWindow, TaskState
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from agent.controller import GameTuneController


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def task() -> OptimizationTask:
    return OptimizationTask(
        name="ui-test",
        game="Counter-Strike 2",
        cpu="CPU",
        gpu="GPU",
        resolution="2560 × 1440",
        refresh_rate="300 Hz",
        test_scene="Workshop",
        target_avg_fps=300,
        target_low_fps=210,
    )


def record(avg: float = 284, low: float = 168) -> BenchmarkRecord:
    return BenchmarkRecord(
        avg,
        low,
        57,
        "2560 × 1440",
        "Workshop",
        repeats=3,
        duration_seconds=60,
        configuration_confirmed=True,
    )


def rollback_task() -> OptimizationTask:
    return replace(task(), max_rounds=1)


class FakeRepository:
    def list_tasks(self):
        return []

    def task_statistics(self, _task_id):
        return {"candidate_count": 0, "experiment_count": 0}


class FakeController:
    def __init__(self):
        self.repository = FakeRepository()
        self.current = None
        self.best = None
        self.supplement_values = None

    def current_record(self, _task_id):
        return self.current

    def best_record(self, _task_id):
        return self.best or self.current

    def evaluate(self, *_args, **_kwargs):
        return Decision(
            AgentAction.RETEST,
            "请在同一配置下复测",
            retry_same_experiment=True,
        )

    def supplement_data(self, current_task, values):
        self.supplement_values = (current_task, values)
        return [Evidence("supplemented", "已补充", "已记录真实设置", 1.0, "supports")], ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "继续验证",
            "分辨率",
            "2560 × 1440",
            "1920 × 1080",
            "测试",
            "假设",
            "方法",
            "标准",
            "恢复",
            1,
            setting_id="resolution",
        )


class UiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = application()

    def test_preview_does_not_create_a_persisted_task(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db")
            self.assertIsNone(window.core_task)
            self.assertEqual(window.controller.repository.list_tasks(), [])
            window.close()

    def test_actual_configuration_is_unchecked_by_default(self):
        page = ExperimentPage(TaskState())
        self.assertFalse(page.configuration_confirmed.isChecked())
        self.assertTrue(page.save_button.isEnabled())
        page.set_completed()
        self.assertFalse(page.save_button.isEnabled())
        page.deleteLater()

    def test_retest_keeps_the_same_round_and_plan(self):
        controller = FakeController()
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db", controller=controller)
            current_task = task()
            baseline = record()
            controller.current = baseline
            controller.best = baseline
            window.core_task = current_task
            window.baseline_record = baseline
            window.state.round_number = 1
            window.current_plan = ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "降低分辨率",
                "分辨率",
                "2560 × 1440",
                "1920 × 1080",
                "原因",
                "假设",
                "方法",
                "标准",
                "恢复",
                1,
                setting_id="resolution",
            )
            window._start_explanation = Mock()
            window._result_saved({
                "avg": 285,
                "low": 169,
                "gpu": 56,
                "repeats": 3,
                "duration": 60,
                "condition": "与基线一致",
                "configuration_confirmed": True,
                "resolution": "2560 × 1440",
                "configuration": {"resolution": "2560 × 1440"},
            })
            self.assertEqual(window.state.round_number, 1)
            self.assertEqual(window.current_plan.setting_id, "resolution")
            self.assertEqual(window.current_plan.round_number, 1)
            window.close()

    def test_request_data_calls_supplement_data_instead_of_evaluate(self):
        controller = FakeController()
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db", controller=controller)
            window.core_task = task()
            window.baseline_record = record()
            window.current_plan = ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "确认阴影质量",
                "全局阴影质量",
                "未知",
                "记录当前值",
                "原因",
                "假设",
                "方法",
                "标准",
                "恢复",
                1,
                setting_id="shadow_quality",
            )
            window._rebuild_task_pages = Mock()
            controller.evaluate = Mock(side_effect=AssertionError("REQUEST_DATA must not evaluate a result"))
            window._result_saved({"supplement": True, "supplement_value": "高"})
            self.assertEqual(controller.supplement_values[1], {"shadow_quality": "高"})
            window.close()

    def test_restore_rollback_at_round_cap_disables_submission_and_keeps_best(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workflow.db"
            controller = GameTuneController(path)
            current_task = rollback_task()
            baseline = replace(
                record(200, 100),
                gpu_usage=99,
                configuration={"shadow_quality": "高", "resolution": current_task.resolution},
            )
            controller.create_task(current_task, baseline, fps_cap_known=True, vsync_known=True)
            candidate = replace(
                record(100, 50),
                configuration={
                    "shadow_quality": "中",
                    "resolution": current_task.resolution,
                    "planned_setting_id": "shadow_quality",
                    "actual_setting_value": "中",
                },
            )
            decision = controller.evaluate(current_task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)

            restored_controller = GameTuneController(path)
            window = MainWindow(database_path=path, controller=restored_controller)
            with patch("app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._restore_task(current_task.task_id)
                self.assertTrue(window.experiment.rollback_button.isVisible())
                window._confirm_pending_rollback()
            self.assertFalse(window.experiment.save_button.isEnabled())
            self.assertEqual(window.state.step, "优化完成")
            self.assertIsNotNone(window.report)
            self.assertIn("1 / 1", " ".join(item.text() for item in window.report.findChildren(QLabel)))
            window.close()


if __name__ == "__main__":
    unittest.main()
