import tempfile
import unittest
from pathlib import Path

from agent.controller import GameTuneController
from agent.state_machine import WorkflowPhase, WorkflowState
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask
from tools.import_benchmark import import_benchmark_csv


class OptimizationAcceptanceTests(unittest.TestCase):
    """Exercise imported measurements across the real controller and database."""

    def test_unsupported_game_can_continue_from_general_direction_to_custom_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "general-game.db")
            task = OptimizationTask(
                "通用游戏", "Apex Legends", "CPU", "GPU", "2560 × 1440", "165 Hz",
                "训练场", 200, 120,
            )
            baseline = BenchmarkRecord(
                200, 100, 98, task.resolution, task.test_scene, repeats=3,
                configuration={"fps_cap": "0", "vsync": "disabled"},
            )
            _, direction_request = controller.create_task(task, baseline)
            self.assertEqual(direction_request.setting_id, "user_adjustment")
            self.assertEqual(controller.workflows[task.task_id].phase, WorkflowPhase.OPTIMIZATION)

            with self.assertRaisesRegex(ValueError, "优化方向"):
                controller.supplement_data(task, {
                    "user_adjustment": "任意文字 | 抗锯齿 | MSAA 4X | MSAA 2X",
                })
            _, custom_plan = controller.supplement_data(task, {
                "user_adjustment": "GPU 画面负载 | 抗锯齿 | MSAA 4X | MSAA 2X",
            })
            self.assertEqual(custom_plan.setting_id, "user_defined")
            self.assertEqual(custom_plan.purpose, "optimization")
            self.assertEqual((custom_plan.old_value, custom_plan.new_value), ("MSAA 4X", "MSAA 2X"))
            self.assertEqual(controller.repository.runtime_metadata(task.task_id)["workflow_phase"], "optimization")

            controller.restore_task(task.task_id)
            self.assertEqual(controller.workflows[task.task_id].phase, WorkflowPhase.OPTIMIZATION)

    def test_two_stable_baseline_runs_can_start_task(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "two-run-baseline.db")
            task = OptimizationTask("两次基线", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(
                201, 101, 60, task.resolution, task.test_scene, repeats=2,
                runs=[{"avg_fps": 200, "low_fps": 100}, {"avg_fps": 201, "low_fps": 101}],
            )
            _, plan = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(plan.round_number, 1)

    def test_unstable_two_run_baseline_requests_a_third_run(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "unstable-baseline.db")
            task = OptimizationTask("不稳定基线", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(
                210, 110, 60, task.resolution, task.test_scene, repeats=2,
                runs=[{"avg_fps": 200, "low_fps": 100}, {"avg_fps": 220, "low_fps": 120}],
            )
            with self.assertRaisesRegex(ValueError, "第 3 次"):
                controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)

    def test_no_benefit_rolls_back_and_plans_a_different_action(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "next-action.db")
            task = OptimizationTask("切换下一项", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(
                200, 100, 99, task.resolution, task.test_scene, repeats=3,
                configuration={"shadow_quality": "高", "dynamic_shadows": "all"},
            )
            _, first_plan = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(first_plan.setting_id, "shadow_quality")
            configuration = {
                "shadow_quality": "中",
                "dynamic_shadows": "all",
                "planned_setting_id": "shadow_quality",
                "actual_setting_value": "中",
            }
            for _ in range(2):
                candidate = BenchmarkRecord(
                    200, 100, 99, task.resolution, task.test_scene,
                    configuration=dict(configuration),
                    runs=[{"avg_fps": 200, "low_fps": 100}],
                )
                self.assertEqual(controller.evaluate(task, baseline, candidate, 1).action, AgentAction.RETEST)
            candidate = BenchmarkRecord(
                200, 100, 99, task.resolution, task.test_scene,
                configuration=dict(configuration),
                runs=[{"avg_fps": 200, "low_fps": 100}],
            )
            decision = controller.evaluate(task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
            self.assertEqual(controller.confirm_rollback(task, confirmed=True), WorkflowState.AWAITING_TEST)
            _, next_plan = controller.plan_after_result(
                task,
                controller.current_record(task.task_id),
                baseline,
                controller.workflows[task.task_id].round_number,
                fps_cap_known=True,
                vsync_known=True,
            )
            self.assertTrue(
                next_plan.action == AgentAction.FINISH
                or (next_plan.setting_id, next_plan.new_value)
                != (first_plan.setting_id, first_plan.new_value)
            )

    def test_supplemented_shadow_does_not_override_later_accepted_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "supplement.db")
            task = OptimizationTask("补充信息", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(
                200, 100, 99, task.resolution, task.test_scene, repeats=3,
                configuration={"dynamic_shadows": "all", "fps_cap": "0", "vsync": "disabled"},
            )
            _, missing = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(missing.action, AgentAction.REQUEST_DATA)
            _, first = controller.supplement_data(task, {"shadow_quality": "高"})
            self.assertEqual(first.new_value, "中")
            candidate = BenchmarkRecord(220, 150, 99, task.resolution, task.test_scene, repeats=3, configuration={"shadow_quality": "中", "dynamic_shadows": "all"})
            self.assertTrue(controller.evaluate(task, baseline, candidate, 1).accepted)
            _, second = controller.plan_after_result(task, controller.current_record(task.task_id), baseline, 2)
            self.assertEqual((second.old_value, second.new_value), ("中", "低"))

    def test_power_mode_diagnostic_is_restored_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "power-mode.db")
            task = OptimizationTask(
                "供电模式复测", "Counter-Strike 2", "CPU", "Laptop GPU",
                "1440 × 900", "300Hz", "scene", 700, 300,
                is_laptop=True, gpu_power_w=100, graphics_mode="dgpu",
                power_mode="ac_balanced", cooling_mode="performance",
            )
            baseline = BenchmarkRecord(
                598, 209, 85, task.resolution, task.test_scene, repeats=3,
                configuration={"power_mode": "ac_balanced", "graphics_mode": "dgpu", "cooling_mode": "performance"},
            )
            _, plan = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(plan.setting_id, "power_mode")
            self.assertEqual(plan.old_value, "接通电源 · 平衡模式")

            candidate = BenchmarkRecord(
                591, 202, 85, task.resolution, task.test_scene, repeats=3,
                configuration={
                    "planned_setting_id": "power_mode",
                    "actual_setting_value": "接通电源 · 性能模式",
                    "power_mode": "ac_performance",
                    "graphics_mode": "dgpu",
                    "cooling_mode": "performance",
                },
                configuration_confirmed=True,
            )
            decision = controller.evaluate(task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
            self.assertFalse(decision.accepted)
            self.assertIn("只用于判断原因", " ".join(decision.reasons))
            controller.confirm_rollback(task, confirmed=True)
            evidence, next_plan = controller.plan_after_result(
                task, controller.current_record(task.task_id), baseline, 2,
                fps_cap_known=True, vsync_known=True,
            )
            self.assertNotIn("laptop_power_mode", {item.code for item in evidence})
            self.assertNotEqual(next_plan.setting_id, "power_mode")

    def test_power_mode_regression_still_requires_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "power-regression.db")
            task = OptimizationTask(
                "供电回滚", "Counter-Strike 2", "CPU", "Laptop GPU",
                "1440 × 900", "300Hz", "scene", 700, 300,
                is_laptop=True, gpu_power_w=100, graphics_mode="dgpu",
                power_mode="ac_balanced", cooling_mode="performance",
            )
            baseline = BenchmarkRecord(600, 210, 85, task.resolution, task.test_scene, repeats=3)
            controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            candidate = BenchmarkRecord(
                540, 185, 85, task.resolution, task.test_scene, repeats=3,
                configuration={"power_mode": "ac_performance"},
            )
            decision = controller.evaluate(task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
            self.assertFalse(decision.accepted)

    def test_failed_power_mode_moves_to_next_platform_candidate_after_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "power-next.db")
            task = OptimizationTask(
                "供电失败后换下一项", "Counter-Strike 2", "CPU", "Laptop GPU",
                "1440 × 900", "300Hz", "scene", 700, 300,
                is_laptop=True, gpu_power_w=100, graphics_mode="hybrid",
                power_mode="ac_balanced", cooling_mode="quiet",
            )
            baseline = BenchmarkRecord(
                600, 210, 60, task.resolution, task.test_scene, repeats=3,
                configuration={"power_mode": "ac_balanced", "graphics_mode": "hybrid", "cooling_mode": "quiet"},
            )
            _, first_plan = controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            self.assertEqual(first_plan.setting_id, "power_mode")
            candidate = BenchmarkRecord(
                500, 150, 60, task.resolution, task.test_scene,
                configuration={
                    "planned_setting_id": "power_mode",
                    "actual_setting_value": "接通电源 · 性能模式",
                    "power_mode": "ac_performance",
                    "graphics_mode": "hybrid",
                    "cooling_mode": "quiet",
                },
                runs=[{"avg_fps": 500, "low_fps": 150}],
            )
            self.assertEqual(controller.evaluate(task, baseline, candidate, 1).action, AgentAction.REVERT_CHANGE)
            controller.confirm_rollback(task, confirmed=True)
            _, next_plan = controller.plan_after_result(
                task,
                controller.current_record(task.task_id),
                baseline,
                controller.workflows[task.task_id].round_number,
                fps_cap_known=True,
                vsync_known=True,
            )
            self.assertEqual(next_plan.setting_id, "cooling_mode")

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

    def test_manual_one_run_submissions_accumulate_without_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = GameTuneController(Path(directory) / "manual-counts.db")
            task = OptimizationTask("手动累计", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
            baseline = BenchmarkRecord(
                200, 100, 99, task.resolution, task.test_scene, repeats=3,
                configuration={"shadow_quality": "高", "dynamic_shadows": "all"},
            )
            controller.create_task(task, baseline, fps_cap_known=True, vsync_known=True)
            configuration = {
                "shadow_quality": "中",
                "dynamic_shadows": "all",
                "planned_setting_id": "shadow_quality",
                "actual_setting_value": "中",
            }
            decisions = []
            for _ in range(3):
                candidate = BenchmarkRecord(
                    220, 150, 99, task.resolution, task.test_scene, repeats=1,
                    configuration=dict(configuration),
                )
                decisions.append(controller.evaluate(task, baseline, candidate, 1))
            self.assertEqual([item.action for item in decisions[:2]], [AgentAction.RETEST, AgentAction.RETEST])
            self.assertTrue(decisions[-1].accepted)
            self.assertEqual(controller.current_record(task.task_id).repeats, 3)

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
