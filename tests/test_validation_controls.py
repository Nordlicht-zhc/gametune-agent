from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.controller import GameTuneController
from agent.evaluator import evaluate_diagnostic_result, evaluate_result
from agent.planner import plan_candidate_id
from agent.state_machine import WorkflowState
from models.schemas import AgentAction, BenchmarkRecord, Decision, OptimizationTask
from tools.configuration import unexpected_configuration_changes


class ValidationControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "controls.db"
        self.controller = GameTuneController(self.path)
        self.task = OptimizationTask("流程验收", "Counter-Strike 2", "CPU", "GPU",
            "2560 × 1440", "144 Hz", "scene", 1000, 600, max_rounds=2)
        self.configuration = {"fps_cap": "144", "vsync": "enabled", "shadow_quality": "高",
                              "dynamic_shadows": "all", "msaa_mode": "8x_msaa"}

    def record(self, avg=144, low=100, *, config=None, repeats=3, gpu=99, resolution=None):
        return BenchmarkRecord(avg, low, gpu, resolution or self.task.resolution, self.task.test_scene,
                               repeats=repeats, configuration=dict(config if config is not None else self.configuration))

    def uncapped_choice(self):
        baseline = self.record()
        self.controller.create_task(self.task, baseline)
        reference = self.record(300, 150, config={**self.configuration, "fps_cap": "0", "vsync": "disabled"}, repeats=2)
        self.controller.evaluate(self.task, baseline, reference, 1)
        self.controller.confirm_rollback(self.task)
        _, choice = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertEqual(choice.setting_id, "uncapped_validation")
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, baseline.record_id)
        return baseline, reference

    def test_diagnostic_and_formal_share_single_variable_guard(self):
        baseline = self.record(200, 100, config={"shadow_quality": "高", "dynamic_shadows": "all", "fps_cap": "0"})
        candidate = self.record(250, 140, config={"shadow_quality": "中", "dynamic_shadows": "sun_only", "fps_cap": "0"})
        for purpose in ("diagnostic", "optimization"):
            with self.subTest(purpose=purpose):
                if purpose == "diagnostic":
                    decision = evaluate_diagnostic_result(self.task, candidate, round_number=1,
                        reference=baseline, setting_id="shadow_quality")
                else:
                    decision = evaluate_result(self.task, baseline, candidate, round_number=1, setting_id="shadow_quality")
                self.assertEqual(decision.action, AgentAction.RETEST)
                self.assertFalse(decision.accepted)
                self.assertIn("动态阴影", " ".join(decision.reasons))
                self.assertIn("重新建立比较基准", " ".join(decision.reasons))

    def test_missing_known_setting_is_not_assumed_unchanged(self):
        baseline = self.record(200, 100, config={"msaa_mode": "none", "shadow_quality": "高"})
        candidate = self.record(250, 140, config={"shadow_quality": "中"})
        self.assertEqual(unexpected_configuration_changes(baseline, candidate, "shadow_quality"), ["msaa_mode"])

    def test_equivalent_labels_and_metadata_do_not_count_as_changes(self):
        baseline = self.record(config={"vsync": "disabled", "msaa_mode": "4x_msaa", "refresh_rate": "144 Hz"})
        candidate = self.record(config={"vsync": "已关闭", "msaa_mode": "4x MSAA", "refresh_rate": "144Hz", "planned_setting_id": "shadow_quality"})
        self.assertEqual(unexpected_configuration_changes(baseline, candidate, "shadow_quality"), [])

    def test_choice_unlocks_gpu_candidates_and_preserves_reference_after_restart(self):
        baseline, reference = self.uncapped_choice()
        evidence, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终保留不限帧"})
        self.assertEqual(plan.reference_record_id, reference.record_id)
        self.assertEqual(plan.required_configuration, {"fps_cap": "0", "vsync": "disabled"})
        options = self.controller.next_step_candidates(self.task, baseline, evidence, 2)
        self.assertTrue({"shadow_quality", "msaa_mode"} <= {item.setting_id for item in options})
        self.assertEqual(self.controller.current_record(self.task.task_id).record_id, baseline.record_id)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        self.assertEqual(self.controller.planning_record(self.task.task_id).record_id, reference.record_id)
        self.assertEqual(self.controller.pending_plan(self.task.task_id).reference_record_id, reference.record_id)

    def test_old_single_run_reference_has_a_supported_upgrade_path(self):
        self.task = replace(self.task, max_rounds=4)
        baseline = self.record()
        self.controller.create_task(self.task, baseline)
        old = self.record(300, 150, config={**self.configuration, "fps_cap": "0", "vsync": "disabled"}, repeats=1)
        old_decision = Decision(AgentAction.REVERT_CHANGE, "旧诊断完成，请恢复", diagnostic_summary="旧诊断记录",
                               measurement_valid=True, rollback_required=True)
        with patch("agent.controller.evaluate_diagnostic_result", return_value=old_decision):
            self.controller.evaluate(self.task, baseline, old, 1)
        self.controller.confirm_rollback(self.task)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        _, upgrade = self.controller.plan_after_result(self.task, baseline, baseline, 2)
        self.assertEqual(upgrade.setting_id, "frame_limits")
        self.assertIn("至少两次", upgrade.method)
        fresh = self.record(300, 150, config=old.configuration, repeats=2)
        self.controller.evaluate(self.task, baseline, fresh, 2)
        self.controller.confirm_rollback(self.task)
        _, choice = self.controller.plan_after_result(self.task, baseline, baseline, 3)
        self.assertEqual(choice.setting_id, "uncapped_validation")
        self.assertEqual(choice.reference_record_id, fresh.record_id)

    def test_validated_msaa_choice_accepts_the_ui_display_value(self):
        baseline, reference = self.uncapped_choice()
        evidence, initial = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终保留不限帧"})
        options = self.controller.next_step_candidates(self.task, baseline, evidence, 2)
        selected = next(item for item in options if item.setting_id == "msaa_mode")
        self.assertTrue(self.controller.accept_model_plan(self.task, plan_candidate_id(initial), selected,
            {plan_candidate_id(item) for item in options}))
        candidate = self.record(330, 180, config={**reference.configuration, "msaa_mode": selected.new_value,
            "planned_setting_id": "msaa_mode", "actual_setting_value": "4x MSAA"})
        decision = self.controller.evaluate(self.task, baseline, candidate, 2)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.avg_delta_pct, 10)

    def test_user_can_end_diagnosis_without_uncapped_optimization(self):
        baseline, _ = self.uncapped_choice()
        _, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "恢复原限帧，结束诊断"})
        self.assertEqual(plan.action, AgentAction.FINISH)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.COMPLETE)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, baseline.record_id)

    def test_user_choice_cannot_be_injected_before_diagnostic_restore(self):
        baseline = self.record()
        self.controller.create_task(self.task, baseline)
        with self.assertRaisesRegex(ValueError, "等待选择"):
            self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终保留不限帧"})
        self.assertEqual(self.controller.limit_context(self.task.task_id), {})

    def test_final_restore_keeps_uncapped_measurement_separate_from_final_limits(self):
        baseline, reference = self.uncapped_choice()
        _, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终恢复原限帧"})
        candidate = self.record(330, 180, config={**reference.configuration, plan.setting_id: plan.new_value})
        decision = self.controller.evaluate(self.task, baseline, candidate, 2)
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.task_complete)
        self.assertTrue(decision.rollback_required)
        self.assertEqual(decision.avg_delta_pct, 10)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.AWAITING_ROLLBACK)
        self.assertIsNotNone(self.controller.pending_plan(self.task.task_id))
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        self.controller.confirm_rollback(self.task)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.COMPLETE)
        self.assertEqual(self.controller.limit_context(self.task.task_id)["status"], "restored")
        self.assertEqual(self.controller.best_record(self.task.task_id).configuration["fps_cap"], "0")
        self.assertIn("尚未重新测量", self.controller.final_limit_note(self.task.task_id))
        history = self.controller.experiment_history(self.task.task_id)
        self.assertEqual(history[-1]["reference_record_id"], reference.record_id)

    def test_exhausted_candidates_require_final_limit_restore_even_before_round_cap(self):
        self.task = replace(self.task, max_rounds=4)
        baseline, reference = self.uncapped_choice()
        _, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终恢复原限帧"})
        candidate = self.record(330, 180, config={**reference.configuration, plan.setting_id: plan.new_value})
        decision = self.controller.evaluate(self.task, baseline, candidate, 2)
        self.assertTrue(decision.accepted)
        self.controller._failed_actions[self.task.task_id] = {"shadow_quality\x1f中\x1f低", "msaa_mode\x1f8x_msaa\x1f4x_msaa"}
        _, terminal = self.controller.plan_after_result(self.task, candidate, baseline, 3)
        self.assertEqual(terminal.action, AgentAction.FINISH)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.AWAITING_ROLLBACK)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        self.controller.confirm_rollback(self.task)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.COMPLETE)
        self.assertEqual(self.controller.limit_context(self.task.task_id)["status"], "restored")

    def test_final_keep_unlimited_does_not_require_original_limit_restore(self):
        baseline, reference = self.uncapped_choice()
        _, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终保留不限帧"})
        decision = self.controller.evaluate(self.task, baseline,
            self.record(330, 180, config={**reference.configuration, plan.setting_id: plan.new_value}), 2)
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.rollback_required)
        self.assertEqual(self.controller.workflow_state(self.task.task_id), WorkflowState.COMPLETE)
        self.assertIn("最终保留不限帧", self.controller.final_limit_note(self.task.task_id))

    def test_limits_must_actually_match_uncapped_reference(self):
        baseline, _ = self.uncapped_choice()
        _, plan = self.controller.supplement_data(self.task, {"uncapped_validation": "不限帧验证，最终保留不限帧"})
        candidate = self.record(330, 180, config={**self.configuration, plan.setting_id: plan.new_value})
        decision = self.controller.evaluate(self.task, baseline, candidate, 2)
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertFalse(decision.task_complete)
        self.assertFalse(self.controller.experiment_history(self.task.task_id)[-1]["valid"])

    def test_last_round_accumulates_three_samples_and_cannot_start_new_plan(self):
        self.task = replace(self.task, max_rounds=1)
        config = {**self.configuration, "fps_cap": "0", "vsync": "disabled"}
        baseline = self.record(200, 100, config=config)
        _, plan = self.controller.create_task(self.task, baseline)
        changed = {**config, plan.setting_id: plan.new_value}
        for index in range(3):
            candidate = self.record(220, 130, config=changed, repeats=1)
            decision = self.controller.evaluate(self.task, baseline, candidate, 1)
            if index < 2:
                self.assertEqual(decision.action, AgentAction.RETEST)
                self.assertFalse(decision.task_complete)
                self.controller = GameTuneController(self.path)
                self.controller.restore_task(self.task.task_id)
                _, same_plan = self.controller.plan_after_result(self.task, baseline, baseline, 1)
                self.assertEqual(same_plan.setting_id, plan.setting_id)
            else:
                self.assertTrue(decision.accepted)
                self.assertTrue(decision.task_complete)
        self.assertEqual(self.controller.best_record(self.task.task_id).repeats, 3)
        self.assertEqual(self.controller.next_step_candidates(self.task, baseline, [], 2), [])

    def test_last_round_retest_limit_still_reverts_unconfirmed_result(self):
        self.task = replace(self.task, max_rounds=1)
        config = {**self.configuration, "fps_cap": "0", "vsync": "disabled"}
        baseline = self.record(200, 100, config=config)
        _, plan = self.controller.create_task(self.task, baseline)
        for _ in range(3):
            candidate = replace(self.record(220, 130, config={**config, plan.setting_id: plan.new_value}, repeats=1), configuration_confirmed=False)
            decision = self.controller.evaluate(self.task, baseline, candidate, 1)
        self.assertTrue(decision.task_complete)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertTrue(decision.rollback_required)
        self.assertEqual(self.controller.best_record(self.task.task_id).record_id, baseline.record_id)

    def test_noisy_formal_measurement_is_marked_invalid_in_model_history(self):
        config = {**self.configuration, "fps_cap": "0", "vsync": "disabled"}
        baseline = self.record(200, 100, config=config)
        _, plan = self.controller.create_task(self.task, baseline)
        candidate = replace(self.record(230, 150, config={**config, plan.setting_id: plan.new_value}),
                            runs=[{"avg_fps": 200, "low_fps": 100}, {"avg_fps": 260, "low_fps": 200}])
        decision = self.controller.evaluate(self.task, baseline, candidate, 1)
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertFalse(self.controller.experiment_history(self.task.task_id)[-1]["valid"])

    def test_nonadaptive_retests_are_bounded_independently_of_round_cap(self):
        baseline = self.record(200, 100)
        candidate = self.record(200, 100)
        decision = evaluate_result(self.task, baseline, candidate, round_number=1, retry_count=2, max_retries=2)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertTrue(decision.rollback_required)

    def test_invalid_sample_does_not_replace_accumulated_valid_sample_on_restart(self):
        config = {**self.configuration, "fps_cap": "0", "vsync": "disabled"}
        baseline = self.record(200, 100, config=config)
        _, plan = self.controller.create_task(self.task, baseline)
        changed = {**config, plan.setting_id: plan.new_value}
        valid = self.record(220, 130, config=changed, repeats=1)
        self.controller.evaluate(self.task, baseline, valid, 1)
        invalid = self.record(330, 180, config={**changed, "fps_cap": "144"}, repeats=1)
        self.controller.evaluate(self.task, baseline, invalid, 1)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        runs = self.controller._round_runs[self.task.task_id][1]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["avg_fps"], 220)


if __name__ == "__main__":
    unittest.main()
