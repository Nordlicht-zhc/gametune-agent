from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from agent.api import ModelResponse
from agent.controller import GameTuneController
from agent.state_machine import WorkflowState
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask


class DiagnosisProgressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tests.db"
        self.controller = GameTuneController(self.path)
        self.task = OptimizationTask(
            "诊断验收", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440",
            "144 Hz", "scene", 300, 200, max_rounds=8,
        )

    def record(self, avg=144, low=100, gpu=60, config=None, resolution=None):
        return BenchmarkRecord(avg, low, gpu, resolution or self.task.resolution,
                               self.task.test_scene, repeats=3, configuration=config or {})

    def start_platform(self, setting="power_mode"):
        original = {"power_mode": "ac_balanced", "cooling_mode": "quiet", "graphics_mode": "hybrid"}
        config = {"fps_cap": "0", "vsync": "disabled", "power_mode": "ac_performance",
                  "cooling_mode": "performance", "graphics_mode": "dgpu"}
        config[setting] = original[setting]
        self.task = replace(self.task, is_laptop=True, gpu_power_w=100,
                            **{key: config[key] for key in original})
        baseline = self.record(200, 100, 85, config)
        _, plan = self.controller.create_task(self.task, baseline)
        self.assertEqual(plan.setting_id, setting)
        target = {"power_mode": "ac_performance", "cooling_mode": "performance", "graphics_mode": "dgpu"}[setting]
        candidate = self.record(230, 120, 85, {**config, setting: target})
        return baseline, candidate, plan

    def test_reopen_invalid_diagnostic_survives_restart(self):
        baseline = self.record(config={"fps_cap": "144", "vsync": "enabled"})
        self.controller.create_task(self.task, baseline)
        for _ in range(2):
            wrong = replace(self.record(config=dict(baseline.configuration)), configuration_confirmed=False)
            self.controller.evaluate(self.task, baseline, wrong, 1)
        self.controller.confirm_rollback(self.task)
        _, stopped = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertEqual(stopped.action, AgentAction.FINISH)
        plan = self.controller.retry_invalid_diagnostic(self.task)
        self.assertEqual(plan.setting_id, "frame_limits")
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.AWAITING_TEST)
        self.assertEqual(self.controller._retry_counts[self.task.task_id], 0)
        self.assertEqual(self.controller.pending_plan(self.task.task_id).round_number, 2)
        valid = self.record(200, 120, config={"fps_cap": "0", "vsync": "disabled"})
        result = self.controller.evaluate(self.task, baseline, valid, 2)
        self.assertTrue(result.measurement_valid)
        self.assertTrue(result.rollback_required)

    def test_known_cap_and_vsync_only_offer_limit_diagnostic(self):
        for config in ({"fps_cap": "144", "vsync": "disabled"},
                       {"fps_cap": "0", "vsync": "enabled"}):
            with self.subTest(config=config):
                task = replace(self.task)
                baseline = self.record(config=config)
                evidence, plan = self.controller.create_task(task, baseline)
                self.assertEqual(plan.setting_id, "frame_limits")
                candidates = self.controller.next_step_candidates(task, baseline, evidence, 1)
                self.assertEqual({item.setting_id for item in candidates}, {"frame_limits"})
                self.assertTrue(all(item.supports != "cpu_or_other" for item in evidence))

    def test_uncapped_reference_survives_restore_and_resolution_uses_it(self):
        baseline = self.record(config={"fps_cap": "144", "vsync": "enabled"})
        self.controller.create_task(self.task, baseline)
        uncapped = self.record(210, 130, config={"fps_cap": "0", "vsync": "disabled", "frame_limits": "不限帧且关闭垂直同步"})
        decision = self.controller.evaluate(self.task, baseline, uncapped, 1)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, baseline.record_id)
        with self.assertRaisesRegex(ValueError, "恢复"):
            self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertEqual(self.controller.confirm_rollback(self.task, confirmed=False), WorkflowState.AWAITING_ROLLBACK)
        self.controller.confirm_rollback(self.task)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        evidence, plan = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertFalse(any(item.supports in {"cpu", "gpu"} for item in evidence))
        self.assertEqual(plan.setting_id, "resolution")
        self.assertEqual(plan.reference_avg_fps, uncapped.avg_fps)
        self.assertIn("不限帧参考", plan.method)
        changed = self.record(252, 150, config={"fps_cap": "0", "vsync": "disabled", "resolution": plan.new_value}, resolution=plan.new_value)
        result = self.controller.evaluate(self.task, baseline, changed, 2)
        self.assertEqual(result.avg_delta_pct, 20.0)
        history = self.controller.experiment_history(self.task.task_id)
        self.assertEqual(history[-1]["reference_record_id"], uncapped.record_id)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, baseline.record_id)

    def test_limits_not_actually_removed_cannot_become_reference(self):
        baseline = self.record(config={"fps_cap": "144", "vsync": "enabled"})
        self.controller.create_task(self.task, baseline)
        for _ in range(4):
            candidate = self.record(200, 120, config={"fps_cap": "144", "vsync": "enabled"})
            decision = self.controller.evaluate(self.task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.RETEST)
            self.assertTrue(decision.measurement_valid)
            self.assertEqual(decision.stop_reason, "unchanged_observation")
        self.assertIsNone(self.controller._uncapped_reference(self.task.task_id))
        self.assertEqual(self.controller._retry_counts[self.task.task_id], 0)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.AWAITING_TEST)

    def test_exhausted_limit_diagnostic_does_not_restart_after_restore(self):
        baseline = self.record(config={"fps_cap": "144", "vsync": "enabled"})
        self.controller.create_task(self.task, baseline)
        wrong = replace(self.record(200, 120, config={"fps_cap": "144", "vsync": "enabled"}), configuration_confirmed=False)
        self.controller.evaluate(self.task, baseline, wrong, 1)
        self.controller.evaluate(self.task, baseline, replace(wrong, record_id=uuid4().hex), 1)
        self.controller.confirm_rollback(self.task)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        _, plan = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertEqual(plan.action, AgentAction.FINISH)
        self.assertIsNone(self.controller._uncapped_reference(self.task.task_id))
        self.assertNotIn("frame_limits", self.controller._diagnosed_setting_ids(self.task.task_id))

    def test_limit_diagnostic_does_not_use_resolution_ambiguity_threshold(self):
        for avg in (144, 154):
            with self.subTest(avg=avg):
                self.task = replace(self.task, task_id=uuid4().hex)
                baseline = self.record(config={"fps_cap": "144", "vsync": "enabled"})
                self.controller.create_task(self.task, baseline)
                uncapped = self.record(avg, 100, config={"fps_cap": "0", "vsync": "disabled"})
                uncapped = replace(uncapped, repeats=2)
                result = self.controller.evaluate(self.task, baseline, uncapped, 1)
                self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
                self.assertTrue(result.measurement_valid)
                self.assertFalse(result.accepted)
                self.controller.confirm_rollback(self.task)
                self.assertIsNotNone(self.controller._uncapped_reference(self.task.task_id))

    def test_platform_improvement_promotes_only_after_restore(self):
        for setting in ("power_mode", "cooling_mode", "graphics_mode"):
            with self.subTest(setting=setting):
                self.controller = GameTuneController(self.path)
                baseline, candidate, _ = self.start_platform(setting)
                result = self.controller.evaluate(self.task, baseline, candidate, 1)
                self.assertFalse(result.accepted)
                self.assertEqual(self.controller._promoted_candidates(self.task, baseline, 2), [])
                self.controller.confirm_rollback(self.task)
                self.controller = GameTuneController(self.path)
                self.controller.restore_task(self.task.task_id)
                _, formal = self.controller.plan_after_result(self.task, baseline, baseline, 2)
                self.assertEqual((formal.setting_id, formal.purpose), (setting, "optimization"))
                formal_record = replace(candidate, record_id=uuid4().hex)
                accepted = self.controller.evaluate(self.task, baseline, formal_record, 2)
                self.assertTrue(accepted.accepted)
                self.assertEqual(self.controller.best_record(self.task.task_id).record_id, formal_record.record_id)
                self.task = replace(self.task, task_id=uuid4().hex)

    def test_regression_or_conflicting_diagnostic_does_not_promote(self):
        for avg, low in ((180, 85), (230, 80), (200, 100)):
            with self.subTest(avg=avg, low=low):
                baseline, candidate, _ = self.start_platform()
                candidate = replace(candidate, avg_fps=avg, low_fps=low)
                self.controller.evaluate(self.task, baseline, candidate, 1)
                self.controller.confirm_rollback(self.task)
                self.assertEqual(self.controller._promoted_candidates(self.task, baseline, 2), [])
                self.task = replace(self.task, task_id=uuid4().hex)

    def test_formal_failure_is_not_reoffered(self):
        baseline, candidate, _ = self.start_platform()
        self.controller.evaluate(self.task, baseline, replace(candidate, record_id=uuid4().hex), 1)
        self.controller.confirm_rollback(self.task)
        self.controller.plan_after_result(self.task, baseline, baseline, 2)
        neutral = replace(candidate, avg_fps=200, low_fps=100, record_id=uuid4().hex,
                          runs=[{"avg_fps": 200, "low_fps": 100}] * 3)
        result = self.controller.evaluate(self.task, baseline, neutral, 2)
        self.assertFalse(result.accepted)
        self.assertEqual(result.action, AgentAction.REVERT_CHANGE)
        self.controller.confirm_rollback(self.task)
        self.assertEqual(self.controller._promoted_candidates(self.task, baseline, 3), [])

    def test_model_receives_reference_decision_and_chooses_from_results(self):
        baseline, candidate, _ = self.start_platform()
        baseline.gpu_usage = 99
        baseline.configuration.update({"dynamic_shadows": "all", "shadow_quality": "高"})
        candidate.configuration.update({"dynamic_shadows": "all", "shadow_quality": "高"})
        self.controller.evaluate(self.task, baseline, replace(candidate, record_id=uuid4().hex), 1)
        self.controller.confirm_rollback(self.task)
        evidence, _ = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        options = self.controller.next_step_candidates(self.task, baseline, evidence, 2)
        self.assertGreaterEqual(len(options), 2)
        class Provider:
            def complete(inner, request):
                payload = json.loads(request.messages[-1].content)
                item = payload["completed_experiments"][-1]
                self.assertEqual(item["reference_record_id"], baseline.record_id)
                self.assertEqual(item["before_configuration"]["power_mode"], "ac_balanced")
                self.assertEqual(item["after_configuration"]["power_mode"], "ac_performance")
                self.assertEqual(item["avg_delta_pct"], 15.0)
                self.assertTrue(item["valid"])
                self.assertTrue(item["restoration_confirmed"])
                self.assertEqual(item["decision"]["action"], "revert_change")
                self.assertTrue(item["decision"]["reasons"])
                chosen = next(c for c in payload["candidates"] if c["setting"] == "供电 / 性能模式")
                return ModelResponse(json.dumps({"candidate_id": chosen["candidate_id"], "reason": "诊断有收益，原配置已恢复，继续正式验证"}), "mock", "mock")
        self.controller.model_provider = Provider()
        chosen = self.controller.choose_next_step(self.task, baseline, evidence, options)
        self.assertEqual(chosen.source, "model")
        self.assertEqual(chosen.plan.purpose, "optimization")

    def test_player_graphics_edit_does_not_become_platform_diagnostic(self):
        baseline, candidate, _ = self.start_platform()
        baseline.configuration["shadow_quality"] = "高"
        candidate.configuration["shadow_quality"] = "低"
        decision = self.controller.evaluate(self.task, baseline, candidate, 1)
        self.assertEqual(decision.action, AgentAction.KEEP_CHANGE)
        self.assertEqual(decision.stop_reason, "player_profile")
        self.assertFalse(decision.diagnostic_summary)
        self.assertFalse(decision.rollback_required)
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, candidate.record_id)
        self.assertEqual(self.controller._promoted_candidates(self.task, baseline, 2), [])


if __name__ == "__main__":
    unittest.main()
