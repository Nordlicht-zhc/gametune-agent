import tempfile
import unittest
from pathlib import Path

from agent.controller import GameTuneController
from agent.state_machine import WorkflowState
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask
from tools.import_benchmark import import_benchmark_csv


class OptimizationAcceptanceTests(unittest.TestCase):
    """Exercise imported measurements across the real controller and database."""

    def test_supplemented_shadow_does_not_override_later_accepted_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "supplement.db")
            task = OptimizationTask("补充信息", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(200, 100, 99, task.resolution, task.test_scene, repeats=3, configuration={"dynamic_shadows": "all"})
            _, missing = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(missing.action, AgentAction.REQUEST_DATA)
            _, first = controller.supplement_data(task, {"shadow_quality": "高"})
            self.assertEqual(first.new_value, "中")
            candidate = BenchmarkRecord(220, 150, 99, task.resolution, task.test_scene, repeats=3, configuration={"shadow_quality": "中", "dynamic_shadows": "all"})
            self.assertTrue(controller.evaluate(task, baseline, candidate, 1).accepted)
            _, second = controller.plan_after_result(task, controller.current_record(task.task_id), baseline, 2)
            self.assertEqual((second.old_value, second.new_value), ("中", "低"))

    def test_restore_does_not_double_count_cumulative_retests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counts.db"
            controller = GameTuneController(path)
            task = OptimizationTask("重复计数", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(200, 100, 99, task.resolution, task.test_scene, repeats=3, configuration={"shadow_quality": "高", "dynamic_shadows": "all"})
            controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            for _ in range(2):
                candidate = BenchmarkRecord(220, 150, 99, task.resolution, task.test_scene, configuration={"shadow_quality": "中", "dynamic_shadows": "all"}, runs=[{"avg_fps": 220, "low_fps": 150}])
                self.assertEqual(controller.evaluate(task, baseline, candidate, 1).action, AgentAction.RETEST)
            restored = GameTuneController(path)
            restored.restore_task(task.task_id)
            candidate = BenchmarkRecord(220, 150, 99, task.resolution, task.test_scene, configuration={"shadow_quality": "中", "dynamic_shadows": "all"}, runs=[{"avg_fps": 220, "low_fps": 150}])
            self.assertTrue(restored.evaluate(task, baseline, candidate, 1).accepted)
            self.assertEqual(restored.current_record(task.task_id).repeats, 3)

    def test_import_retest_accept_restore_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            controller = GameTuneController(folder / "workflow.db")
            task = OptimizationTask("验收", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "same-scene", 300, 210)
            baseline = BenchmarkRecord(200, 100, 99, task.resolution, task.test_scene, repeats=3, configuration={"shadow_quality": "高", "dynamic_shadows": "all", "resolution": task.resolution})
            _, plan = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(plan.new_value, "中")

            first = BenchmarkRecord(220, 150, 99, task.resolution, task.test_scene, repeats=1, configuration={"shadow_quality": "中", "dynamic_shadows": "all", "resolution": task.resolution}, runs=[{"avg_fps": 220, "low_fps": 150}])
            retry = controller.evaluate(task, baseline, first, 1)
            self.assertEqual(retry.action, AgentAction.RETEST)
            self.assertEqual(controller.workflows[task.task_id].round_number, 1)

            restored = GameTuneController(folder / "workflow.db")
            self.assertIsNotNone(restored.restore_task(task.task_id))
            self.assertEqual(restored.pending_plan(task.task_id).new_value, "中")
            csv_path = folder / "repeated.csv"
            csv_path.write_text("avg_fps,1%_low,gpu_usage\n220,150,99\n221,151,99\n219,149,99\n", encoding="utf-8")
            data = import_benchmark_csv(csv_path)
            candidate = BenchmarkRecord(data["avg_fps"], data["low_fps"], data["gpu_usage"], task.resolution, task.test_scene, repeats=data["run_count"], configuration=dict(first.configuration), runs=data["runs"])
            kept = restored.evaluate(task, baseline, candidate, 1)
            self.assertTrue(kept.accepted)
            self.assertGreater(restored.best_record(task.task_id).low_fps, baseline.low_fps)

            _, second_plan = restored.plan_after_result(task, restored.current_record(task.task_id), baseline, 2)
            self.assertEqual(second_plan.new_value, "低")
            worse = BenchmarkRecord(190, 95, 99, task.resolution, task.test_scene, repeats=3, configuration={"shadow_quality": "低", "dynamic_shadows": "all", "resolution": task.resolution})
            rejected = restored.evaluate(task, baseline, worse, 2)
            self.assertEqual(rejected.action, AgentAction.REVERT_CHANGE)
            self.assertEqual(restored.workflow_state(task.task_id), WorkflowState.AWAITING_ROLLBACK)
            with self.assertRaises(ValueError):
                restored.evaluate(task, baseline, worse, 2)

            resumed = GameTuneController(folder / "workflow.db")
            resumed.restore_task(task.task_id)
            resumed.confirm_rollback(task, confirmed=True)
            self.assertEqual(resumed.current_record(task.task_id).configuration["shadow_quality"], "中")
            if resumed.workflow_state(task.task_id) != WorkflowState.COMPLETE:
                _, next_plan = resumed.plan_after_result(task, resumed.current_record(task.task_id), baseline, resumed.workflows[task.task_id].round_number)
                self.assertTrue(next_plan.action == AgentAction.FINISH or (next_plan.setting_id, next_plan.new_value) != (second_plan.setting_id, second_plan.new_value))


if __name__ == "__main__":
    unittest.main()
