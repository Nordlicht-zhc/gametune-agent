from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from agent.api import ModelAPIError, ModelResponse
from agent.controller import GameTuneController
from agent.feedback import summarize_feedback
from agent.next_step import choose_next_step, next_step_request
from agent.planner import plan_candidate_id
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask
from tools.planning_evaluation import compare_scenario, export_scenario


class RecordingProvider:
    name = "mock-history-aware-model"

    def __init__(self, select=None, fail=False):
        self.requests = []
        self.select = select
        self.fail = fail

    def complete(self, request):
        self.requests.append(request)
        if self.fail:
            raise ModelAPIError("mock unavailable")
        payload = json.loads(request.messages[-1].content)
        selected = self.select(payload) if self.select else payload["candidates"][-1]
        return ModelResponse(json.dumps({"candidate_id": selected["candidate_id"],
            "reason": "结合已完成实验，选择尚未验证的合法候选。"}), "mock", self.name)


class PlanningFeedbackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "feedback.db"
        self.controller = GameTuneController(self.path)
        self.task = OptimizationTask("模拟回放，非真实采集", "Counter-Strike 2", "CPU", "GPU",
            "2560 × 1440", "300 Hz", "scene", 1000, 600, fps_cap=0, vsync="disabled",
            shadow_quality="高", dynamic_shadows="all")
        self.config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高",
                       "dynamic_shadows": "all", "msaa_mode": "8x_msaa"}
        self.baseline = BenchmarkRecord(200, 100, 99, self.task.resolution, self.task.test_scene,
            repeats=2, configuration=dict(self.config),
            runs=[{"avg_fps": 200, "low_fps": 100}, {"avg_fps": 200, "low_fps": 100}])

    def start(self):
        self.evidence, self.plan = self.controller.create_task(self.task, self.baseline)
        self.options = self.controller.next_step_candidates(self.task, self.baseline, self.evidence, 1)

    def submit(self, avg, low, *, extra=None):
        plan = self.controller.pending_plan(self.task.task_id)
        configuration = {**self.config, plan.setting_id: plan.new_value, **(extra or {})}
        record = BenchmarkRecord(avg, low, 99, self.task.resolution, self.task.test_scene,
            configuration=configuration, runs=[{"avg_fps": avg, "low_fps": low}])
        return self.controller.evaluate(self.task, self.baseline, record, 1)

    def test_single_candidate_skips_api_even_when_provider_exists(self):
        self.start()
        provider = RecordingProvider()
        selection = choose_next_step(provider, self.task, self.baseline, self.evidence, [self.plan])
        self.assertEqual(provider.requests, [])
        self.assertEqual(selection.plan.planning_context["status"], "single_candidate")
        self.assertEqual(selection.source, "local")

    def test_model_choice_changes_plan_and_persists_explanation_and_options(self):
        self.start()
        self.controller.model_provider = RecordingProvider()
        selection = self.controller.choose_next_step(self.task, self.baseline, self.evidence, self.options)
        self.assertNotEqual(selection.plan.setting_id, self.plan.setting_id)
        self.assertTrue(self.controller.accept_model_plan(self.task, plan_candidate_id(self.plan),
            selection.plan, {plan_candidate_id(item) for item in self.options}))
        restored = GameTuneController(self.path)
        restored.restore_task(self.task.task_id)
        plan = restored.pending_plan(self.task.task_id)
        self.assertEqual(plan.planning_source, "model")
        self.assertEqual(plan.planning_context, selection.plan.planning_context)
        self.assertGreaterEqual(plan.planning_context["elapsed_ms"], 0)
        self.assertGreater(len(plan.planning_context["candidate_options"]), 1)
        self.assertEqual(restored.repository.runtime_metadata(self.task.task_id)["planning_selections"][-1]["source"], "model")

    def test_failure_and_out_of_menu_response_fall_back_to_safe_plan(self):
        self.start()
        for provider in (RecordingProvider(fail=True), RecordingProvider(select=lambda payload: {"candidate_id": "x" * 16})):
            with self.subTest(provider=provider):
                selection = choose_next_step(provider, self.task, self.baseline, self.evidence, self.options)
                self.assertEqual(selection.source, "local")
                self.assertEqual(selection.plan.planning_context["status"], "model_fallback")
                self.assertEqual(plan_candidate_id(selection.plan), plan_candidate_id(self.plan))

    def test_gpu_evidence_precedes_balanced_power_mode(self):
        self.task.is_laptop = True
        self.task.power_mode = "ac_balanced"
        self.task.gpu_power_w = 100
        self.task.cooling_mode = "performance"
        self.task.graphics_mode = "discrete"
        self.baseline.configuration["power_mode"] = "ac_balanced"
        self.start()
        self.assertEqual(self.plan.setting_id, "shadow_quality")
        self.assertIn("power_mode", {item.setting_id for item in self.options})
        self.assertEqual(self.plan.planning_context["source"], "local")

    def test_successful_feedback_changes_current_reference_and_next_action(self):
        self.start()
        for _ in range(3):
            decision = self.submit(230, 115)
        self.assertTrue(decision.accepted)
        current = self.controller.current_record(self.task.task_id)
        self.assertEqual(current.configuration["shadow_quality"], "中")
        evidence, plan = self.controller.plan_after_result(self.task, current, self.baseline, 2)
        self.assertEqual(plan.old_value, "中")
        self.assertEqual(plan.new_value, "低")
        history = self.controller.experiment_history(self.task.task_id)
        summary = summarize_feedback(history)
        self.assertEqual(summary["attempts"][-1]["outcome"], "accepted")
        self.assertEqual(summary["unresolved"], [])
        self.assertEqual(summary["attempts"][-1]["avg_delta_pct"], 15)
        payload = json.loads(next_step_request(self.task, current, evidence,
            self.controller.next_step_candidates(self.task, current, evidence, 2), history=history).messages[-1].content)
        self.assertEqual(payload["feedback_summary"], summary)
        self.assertEqual(history[-1]["selection"]["selected_candidate_id"], plan_candidate_id(self.plan))

    def test_no_benefit_rollback_is_used_after_restart_and_not_repeated(self):
        self.start()
        for _ in range(3):
            decision = self.submit(200, 100)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertEqual(summarize_feedback(self.controller.experiment_history(self.task.task_id))["unresolved"][-1]["issue"], "awaiting_restoration")
        self.controller.confirm_rollback(self.task)
        self.controller = GameTuneController(self.path)
        self.controller.restore_task(self.task.task_id)
        evidence, plan = self.controller.plan_after_result(self.task, self.baseline, self.baseline, 2)
        self.assertEqual(plan.setting_id, "msaa_mode")
        options = self.controller.next_step_candidates(self.task, self.baseline, evidence, 2)
        self.assertNotIn("shadow_quality", {item.setting_id for item in options})
        summary = plan.planning_context["feedback"]
        self.assertEqual(summary["avoid_repeating"][-1]["setting_id"], "shadow_quality")
        self.assertEqual(summary["avoid_repeating"][-1]["rollback_status"], "restored")
        provider = RecordingProvider()
        choice = choose_next_step(provider, self.task, self.baseline, evidence, options,
            self.controller.experiment_history(self.task.task_id))
        self.assertNotEqual(choice.plan.setting_id, "shadow_quality")

    def test_invalid_measurement_is_not_remembered_as_no_benefit(self):
        self.start()
        for _ in range(3):
            decision = self.submit(240, 120, extra={"fps_cap": "144"})
        self.assertFalse(decision.measurement_valid)
        summary = summarize_feedback(self.controller.experiment_history(self.task.task_id))
        self.assertEqual(summary["attempts"][-1]["outcome"], "invalid")
        self.assertIsNone(summary["attempts"][-1]["avg_delta_pct"])
        self.assertEqual(summary["avoid_repeating"], [])
        self.controller.confirm_rollback(self.task)
        self.assertNotIn("shadow_quality", self.controller._failed_setting_ids(self.task.task_id))

    def test_invalid_then_valid_retests_resolve_the_unfinished_issue(self):
        self.start()
        self.submit(240, 120, extra={"fps_cap": "144"})
        self.submit(230, 115)
        # Supply a trustworthy three-run record before exhausting retry budget.
        plan = self.controller.pending_plan(self.task.task_id)
        candidate = replace(self.baseline, record_id="accepted-after-invalid", avg_fps=230, low_fps=115,
            repeats=2, configuration={**self.config, plan.setting_id: plan.new_value},
            runs=[{"avg_fps": 230, "low_fps": 115}] * 2)
        decision = self.controller.evaluate(self.task, self.baseline, candidate, 1)
        self.assertTrue(decision.accepted)
        summary = summarize_feedback(self.controller.experiment_history(self.task.task_id))
        self.assertEqual(summary["unresolved"], [])
        self.assertEqual(summary["attempts"][0]["outcome"], "invalid")

    def test_same_checkpoint_comparison_reports_choice_cost_and_manual_judgment(self):
        self.start()
        scenario = export_scenario(self.controller, self.task.task_id)
        scenario["data_kind"] = "synthetic_test"
        scenario["acceptable_candidate_ids"] = [plan_candidate_id(item) for item in self.options]
        report = compare_scenario(scenario, RecordingProvider())
        self.assertEqual(report["evaluation_kind"], "planning_replay")
        self.assertTrue(report["model_comparison_available"])
        self.assertFalse(report["same_choice"])
        self.assertTrue(report["local"]["legal"])
        self.assertTrue(report["model"]["reviewer_acceptable"])
        self.assertFalse(report["model"]["repeats_known_no_benefit"])
        self.assertEqual(report["judgment_source"], "scenario_reviewer")
        self.assertGreaterEqual(report["model"]["api_elapsed_ms"], 0)

    def test_comparison_does_not_count_fallback_as_model_success(self):
        self.start()
        scenario = export_scenario(self.controller, self.task.task_id)
        report = compare_scenario(scenario, RecordingProvider(fail=True))
        self.assertFalse(report["model_comparison_available"])
        self.assertIsNone(report["model"]["reviewer_acceptable"])
        self.assertEqual(report["model"]["status"], "model_fallback")

    def test_model_can_use_feedback_to_change_choice_on_the_same_menu(self):
        self.start()
        scenario = export_scenario(self.controller, self.task.task_id)

        def based_on_history(payload):
            avoided = {item["setting_id"] for item in payload["feedback_summary"]["avoid_repeating"]}
            return next(item for item in payload["candidates"] if item["setting_id"] not in avoided)

        provider = RecordingProvider(select=based_on_history)
        before = compare_scenario(scenario, provider)
        self.assertEqual(before["model"]["setting_id"], "shadow_quality")
        for _ in range(3):
            self.submit(200, 100)
        self.controller.confirm_rollback(self.task)
        # Freeze the same legal menu to measure whether the history affects
        # selection. The live controller additionally removes failed actions.
        scenario["history"] = self.controller.experiment_history(self.task.task_id)
        after = compare_scenario(scenario, provider)
        self.assertEqual(after["model"]["setting_id"], "msaa_mode")
        self.assertFalse(after["model"]["repeats_known_no_benefit"])
        self.assertTrue(after["local"]["repeats_known_no_benefit"])

    def test_replay_rejects_illegal_candidate_and_invalid_human_label(self):
        self.start()
        scenario = export_scenario(self.controller, self.task.task_id)
        scenario["acceptable_candidate_ids"] = ["outside"]
        with self.assertRaises(ValueError):
            compare_scenario(scenario)
        scenario.pop("acceptable_candidate_ids")
        scenario["candidates"][0]["new_value"] = "invented"
        with self.assertRaises(ValueError):
            compare_scenario(scenario)

    def test_export_during_retest_preserves_the_current_experiment(self):
        self.start()
        self.submit(230, 115)
        scenario = export_scenario(self.controller, self.task.task_id)
        self.assertEqual(len(scenario["candidates"]), 1)
        provider = RecordingProvider()
        report = compare_scenario(scenario, provider)
        self.assertEqual(provider.requests, [])
        self.assertEqual(report["local"]["status"], "single_candidate")

    def test_retest_cannot_be_replaced_by_a_late_model_selection(self):
        self.start()
        provider = RecordingProvider()
        late = choose_next_step(provider, self.task, self.baseline, self.evidence, self.options)
        self.submit(230, 115)
        options = self.controller.next_step_candidates(self.task, self.baseline, self.evidence, 1)
        self.assertEqual([plan_candidate_id(item) for item in options], [plan_candidate_id(self.plan)])
        self.assertFalse(self.controller.accept_model_plan(self.task, plan_candidate_id(self.plan), late.plan,
            {plan_candidate_id(item) for item in self.options}))
        self.assertEqual(self.controller.pending_plan(self.task.task_id).setting_id, "shadow_quality")


if __name__ == "__main__":
    unittest.main()
