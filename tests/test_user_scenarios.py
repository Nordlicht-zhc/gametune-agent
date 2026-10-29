"""Synthetic, repeatable user journeys; never measurements of a real game."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.controller import GameTuneController
from agent.state_machine import WorkflowState
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask


class UserScenarioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "synthetic-scenarios.sqlite"
        self.controller = GameTuneController(self.path, model_provider=None)
        self.task = OptimizationTask(
            "自动测试样例（模拟数据）", "Counter-Strike 2", "测试 CPU", "测试 GPU",
            "2560 × 1440", "144 Hz", "固定回放 60 秒", 300, 200,
            fps_cap=144, vsync="enabled", max_rounds=8,
        )
        self.baseline = self.record(144, 100, capped=True, repeats=2)

    def record(self, avg, low, *, capped=False, repeats=1, gpu=60, runs=None):
        return BenchmarkRecord(
            avg, low, gpu, self.task.resolution, self.task.test_scene,
            repeats=repeats, duration_seconds=60,
            configuration={"fps_cap": "144" if capped else "0",
                           "vsync": "enabled" if capped else "disabled"},
            runs=runs or [],
        )

    def restart(self):
        self.controller = GameTuneController(self.path, model_provider=None)
        self.assertIsNotNone(self.controller.restore_task(self.task.task_id))

    def start_limits(self):
        _, plan = self.controller.create_task(self.task, self.baseline)
        self.assertEqual(plan.setting_id, "frame_limits")

    def submit(self, candidate):
        return self.controller.evaluate(self.task, self.baseline, candidate, 1)

    def test_01_two_separate_runs_survive_restart(self):
        """01 限帧排查：逐次录入，中途重开，累计两次后进入下一项。"""
        self.start_limits()
        first = self.submit(self.record(210, 130))
        self.assertEqual(first.action, AgentAction.RETEST)
        self.restart()
        self.assertEqual(self.controller.pending_plan(self.task.task_id).round_number, 1)
        second = self.submit(self.record(212, 132))
        self.assertTrue(second.measurement_valid)
        self.assertEqual(second.action, AgentAction.REVERT_CHANGE)
        self.assertEqual(self.controller.workflows[self.task.task_id].state, WorkflowState.AWAITING_ROLLBACK)
        with self.assertRaisesRegex(ValueError, "恢复"):
            self.controller.plan_after_result(self.task, self.baseline, self.baseline, 2)
        self.controller.confirm_rollback(self.task)
        self.restart()
        reference = self.controller._uncapped_reference(self.task.task_id)
        self.assertEqual(reference.repeats, 2)
        self.assertEqual(reference.avg_fps, 211)
        _, plan = self.controller.plan_after_result(self.task, self.baseline, self.baseline, 2)
        self.assertEqual(plan.setting_id, "resolution")
        self.assertEqual(plan.reference_record_id, reference.record_id)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_02_no_improvement_is_still_valid(self):
        """02 没有提升：144/100 → 144/100，诊断有效，不强迫继续补测。"""
        self.start_limits()
        result = self.submit(self.record(144, 100, repeats=2))
        self.assertTrue(result.measurement_valid)
        self.assertFalse(result.retry_same_experiment)
        self.assertFalse(result.accepted)
        self.controller.confirm_rollback(self.task)
        self.assertIsNotNone(self.controller._uncapped_reference(self.task.task_id))

    def test_03_seven_percent_is_not_resolution_ambiguity(self):
        """03 约 7% 提升：144/100 → 154/107，不误用分辨率模糊区间规则。"""
        self.start_limits()
        result = self.submit(self.record(154, 107, repeats=2))
        self.assertTrue(result.measurement_valid)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.assertFalse(result.retry_same_experiment)

    def assert_invalid_diagnostic_stops(self, candidate):
        self.start_limits()
        self.assertEqual(self.submit(candidate).action, AgentAction.RETEST)
        result = self.submit(replace(candidate, record_id="second-invalid-observation"))
        self.assertFalse(result.measurement_valid)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.controller.confirm_rollback(self.task)
        self.restart()
        self.assertIsNone(self.controller._uncapped_reference(self.task.task_id))
        _, next_plan = self.controller.plan_after_result(self.task, self.baseline, self.baseline, 2)
        self.assertEqual(next_plan.action, AgentAction.FINISH)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_04_unchanged_settings_remain_observations_after_restart(self):
        """未解除限帧的真实复测保留为观察，不消耗诊断预算。"""
        self.start_limits()
        for _ in range(3):
            decision = self.controller.evaluate(self.task, self.baseline,
                self.record(200, 120, capped=True, repeats=2), 1)
            self.assertEqual(decision.stop_reason, "unchanged_observation")
            self.assertTrue(decision.measurement_valid)
            self.assertFalse(decision.rollback_required)
            self.restart()
        self.assertEqual(self.controller._retry_counts[self.task.task_id], 0)
        self.assertIsNone(self.controller._uncapped_reference(self.task.task_id))

    def test_05_noisy_samples_never_become_reference(self):
        """05 波动过大：210/130 与 250/170，无效数据不能成为比较基准。"""
        self.assert_invalid_diagnostic_stops(self.record(
            230, 150, repeats=2,
            runs=[{"avg_fps": 210, "low_fps": 130}, {"avg_fps": 250, "low_fps": 170}],
        ))

    def start_shadow_optimization(self):
        self.task = replace(self.task, fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
        self.baseline = self.record(200, 100, repeats=2, gpu=99)
        self.baseline.configuration.update(shadow_quality="高", dynamic_shadows="all")
        _, plan = self.controller.create_task(self.task, self.baseline)
        self.assertEqual((plan.setting_id, plan.new_value), ("shadow_quality", "中"))

    def shadow_record(self, avg, low):
        candidate = self.record(avg, low, gpu=99)
        candidate.configuration.update(shadow_quality="中", dynamic_shadows="all")
        return candidate

    def test_06_optimization_requires_three_observations(self):
        """06 正式优化：220/120、222/121、221/120，累计三次才保留。"""
        self.start_shadow_optimization()
        for avg, low in ((220, 120), (222, 121)):
            result = self.submit(self.shadow_record(avg, low))
            self.assertEqual(result.action, AgentAction.RETEST)
            self.assertFalse(result.accepted)
        self.restart()
        result = self.submit(self.shadow_record(221, 120))
        self.assertTrue(result.accepted)
        current = self.controller.current_record(self.task.task_id)
        self.assertEqual(current.repeats, 3)
        self.assertEqual(current.configuration["shadow_quality"], "中")
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, current.record_id)

    def test_07_regression_does_not_replace_best_configuration(self):
        """07 明显退步：200/100 → 160/80，首轮即恢复，不污染最佳配置。"""
        self.start_shadow_optimization()
        result = self.submit(self.shadow_record(160, 80))
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.assertFalse(result.accepted)
        self.controller.confirm_rollback(self.task)
        self.restart()
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)
        self.assertEqual(self.controller.current_record(self.task.task_id).configuration["shadow_quality"], "高")

    def test_11_only_low_fps_is_noisy(self):
        """11 平均 FPS 完全稳定，只有 1% Low 剧烈波动，仍应拦截。"""
        self.assert_invalid_diagnostic_stops(self.record(
            220, 120, repeats=2,
            runs=[{"avg_fps": 220, "low_fps": 100}, {"avg_fps": 220, "low_fps": 140}],
        ))

    def test_12_exact_five_percent_is_allowed(self):
        """12 波动恰好 5%：195 与 205 的中位数为 200，不误判超限。"""
        self.start_limits()
        result = self.submit(self.record(
            200, 120, repeats=2,
            runs=[{"avg_fps": 195, "low_fps": 120}, {"avg_fps": 205, "low_fps": 120}],
        ))
        self.assertTrue(result.measurement_valid)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)

    def test_13_just_over_five_percent_is_blocked(self):
        """13 波动 5.01%：194.99 与 205.01，不因显示取整而放行。"""
        self.assert_invalid_diagnostic_stops(self.record(
            200, 120, repeats=2,
            runs=[{"avg_fps": 194.99, "low_fps": 120}, {"avg_fps": 205.01, "low_fps": 120}],
        ))

    def test_14_formal_noise_stays_invalid_after_restart(self):
        """14 正式优化连续波动：中途重开不重置重试预算，第三次后恢复。"""
        self.start_shadow_optimization()
        for avg in (220, 240):
            result = self.submit(self.shadow_record(avg, 120))
            self.assertEqual(result.action, AgentAction.RETEST)
            self.assertFalse(result.accepted)
        self.restart()
        result = self.submit(self.shadow_record(230, 120))
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.assertFalse(result.measurement_valid)
        self.assertFalse(result.accepted)
        self.controller.confirm_rollback(self.task)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_15_single_extreme_outlier_cannot_be_hidden_by_median(self):
        """15 两次正常、一次极高值：中位数看似合理，也不能确认收益。"""
        self.start_shadow_optimization()
        for avg in (220, 221):
            self.submit(self.shadow_record(avg, 120))
        result = self.submit(self.shadow_record(400, 120))
        self.assertFalse(result.measurement_valid)
        self.assertFalse(result.accepted)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, self.baseline.record_id)

    def test_16_invalid_numbers_are_rejected_before_storage(self):
        """16 零、负值、NaN、无穷大和异常 1% Low，在入库前拒绝。"""
        for field in ("avg_fps", "low_fps"):
            for value in (0, -1, float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        replace(self.baseline, **{field: value})
                    with self.assertRaises(ValueError):
                        replace(self.baseline, runs=[{"avg_fps": 200, "low_fps": 100, field: value}])
        with self.assertRaises(ValueError):
            replace(self.baseline, low_fps=200)
        self.assertEqual(self.controller.repository.list_tasks(), [])

    def test_17_three_noisy_baseline_runs_do_not_start_optimization(self):
        """17 第三次基线仍不稳定：不能仅因次数足够就开始正式调校。"""
        for runs in (
            [{"avg_fps": avg, "low_fps": 100} for avg in (140, 200, 260)],
            [{"avg_fps": 200, "low_fps": low} for low in (80, 100, 120)],
        ):
            with self.subTest(runs=runs):
                noisy = replace(self.baseline, avg_fps=200, repeats=3, runs=runs)
                with self.assertRaisesRegex(ValueError, "波动|不稳定"):
                    self.controller.create_task(self.task, noisy)
                self.assertEqual(self.controller.repository.list_tasks(), [])

    def test_18_faster_different_scene_is_not_improvement(self):
        """18 帧率大涨但换了测试场景：数据稳定也不能认定收益。"""
        self.start_limits()
        candidate = replace(self.record(300, 180, repeats=2), test_scene="另一个更简单的场景")
        self.assertEqual(self.submit(candidate).action, AgentAction.RETEST)
        result = self.submit(replace(candidate, record_id="other-scene-second-run"))
        self.assertFalse(result.measurement_valid)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.assertIsNone(self.controller._uncapped_reference(self.task.task_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
