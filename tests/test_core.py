from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.controller import GameTuneController
from agent.evaluator import evaluate_result
from agent.api import APIConfig, ModelMessage, ModelRequest, ModelResponse, OpenAICompatibleProvider
from agent.explainer import explain_decision
from agent.planner import plan_next_experiment
from agent.state_machine import Workflow, WorkflowState
from agent.validator import KnowledgeBase, PlanValidationError, validate_plan
from models.schemas import ExperimentPlan
from models.schemas import AgentAction, BenchmarkRecord, Decision, OptimizationTask
from tools.calculate_metrics import metrics_from_frame_times, normalized_score, weighted_relative_change
from tools.import_benchmark import import_benchmark_csv
from tools.resolution import aspect_ratio_name, lower_resolution_same_aspect, normalize_resolution
from tools.hardware_catalog import HardwareCatalog
from tools.analyze_evidence import analyze_evidence, dominant_hypothesis, evidence_support_level


def task() -> OptimizationTask:
    return OptimizationTask(
        name="CS2 test",
        game="Counter-Strike 2",
        cpu="CPU",
        gpu="GPU",
        resolution="2560 × 1440",
        refresh_rate="300 Hz",
        test_scene="Workshop",
        target_avg_fps=300,
        target_low_fps=210,
    )


def record(avg: float, low: float, *, resolution: str = "2560 × 1440", comparable: bool = True) -> BenchmarkRecord:
    return BenchmarkRecord(avg, low, 60, resolution, "Workshop", repeats=3, comparable=comparable)


class MetricsTests(unittest.TestCase):
    def test_constant_frame_times(self):
        result = metrics_from_frame_times([10.0] * 1000)
        self.assertEqual(result["avg_fps"], 100.0)
        self.assertEqual(result["low_fps"], 100.0)

    def test_score_is_capped_at_target(self):
        self.assertEqual(normalized_score(600, 500, 300, 210, "competitive"), 100.0)

    def test_weighted_change_is_not_capped_at_target(self):
        self.assertAlmostEqual(weighted_relative_change(12.5, 0.0, "competitive"), 4.375)
        self.assertAlmostEqual(weighted_relative_change(0.0, 10.0, "competitive"), 6.5)


class ResolutionTests(unittest.TestCase):
    def test_normalizes_custom_resolution(self):
        self.assertEqual(normalize_resolution(" 1728x1080 "), "1728 × 1080")

    def test_16_10_uses_lower_16_10_preset(self):
        self.assertEqual(aspect_ratio_name("2560 × 1600"), "16:10")
        self.assertEqual(lower_resolution_same_aspect("2560 × 1600"), "1920 × 1200")

    def test_4_3_uses_lower_4_3_preset(self):
        self.assertEqual(aspect_ratio_name("1600 × 1200"), "4:3")
        self.assertEqual(lower_resolution_same_aspect("1600 × 1200"), "1440 × 1080")

    def test_custom_ultrawide_preserves_ratio_by_scaling(self):
        self.assertEqual(lower_resolution_same_aspect("3440 × 1440"), "2580 × 1080")

    def test_rejects_invalid_resolution(self):
        with self.assertRaises(ValueError):
            normalize_resolution("1920p")


class HardwareCatalogTests(unittest.TestCase):
    def test_catalog_resolves_desktop_and_laptop_models(self):
        catalog = HardwareCatalog(Path(__file__).resolve().parents[1] / "knowledge" / "hardware.json")
        self.assertEqual(catalog.find("cpu", "Intel Core Ultra 9 275HX").segment, "Laptop")
        self.assertEqual(catalog.find("gpu", "NVIDIA GeForce RTX 4060").vendor, "NVIDIA")

    def test_catalog_has_broad_laptop_coverage(self):
        catalog = HardwareCatalog(Path(__file__).resolve().parents[1] / "knowledge" / "hardware.json")
        laptop_cpus = [entry for entry in catalog.cpus if entry.segment == "Laptop"]
        laptop_gpus = [entry for entry in catalog.gpus if entry.segment == "Laptop"]
        self.assertGreaterEqual(len(laptop_cpus), 35)
        self.assertGreaterEqual(len(laptop_gpus), 25)

    def test_laptop_missing_power_is_requested_before_resolution_test(self):
        current_task = task()
        current_task.is_laptop = True
        current_task.gpu_power_w = None
        current_task.graphics_mode = "hybrid"
        current_task.power_mode = "ac_performance"
        current_task.cooling_mode = "performance"
        baseline = record(180, 110)
        evidence = analyze_evidence(current_task, baseline, fps_cap_known=True, vsync_known=True)
        plan = plan_next_experiment(current_task, baseline, evidence, 1)
        self.assertEqual(plan.setting, "GPU 功耗")


class EvidenceWeightTests(unittest.TestCase):
    def test_missing_conditions_reduce_support_strength(self):
        current_task = task()
        current = BenchmarkRecord(180, 100, 57, current_task.resolution, current_task.test_scene, repeats=3)
        known = analyze_evidence(current_task, current, fps_cap_known=True, vsync_known=True)
        unknown = analyze_evidence(current_task, current, fps_cap_known=False, vsync_known=False)
        known_hypothesis, known_strength = dominant_hypothesis(known)
        unknown_hypothesis, unknown_strength = dominant_hypothesis(unknown)
        self.assertEqual(known_hypothesis, "cpu")
        self.assertEqual(unknown_hypothesis, "cpu")
        self.assertLess(unknown_strength, known_strength)
        self.assertEqual(evidence_support_level(unknown), "不足")

    def test_measured_cause_is_not_overruled_by_target_miss_symptom(self):
        current_task = task()
        current = BenchmarkRecord(180, 50, 98, current_task.resolution, current_task.test_scene, repeats=3)
        evidence = analyze_evidence(current_task, current, fps_cap_known=True, vsync_known=True)
        hypothesis, _ = dominant_hypothesis(evidence)
        self.assertEqual(hypothesis, "gpu")


class ImportTests(unittest.TestCase):
    def test_imports_summary_runs_and_uses_median(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runs.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["avg_fps", "1%_low", "gpu_usage"])
                writer.writeheader()
                writer.writerows([
                    {"avg_fps": 290, "1%_low": 170, "gpu_usage": 58},
                    {"avg_fps": 300, "1%_low": 180, "gpu_usage": 60},
                    {"avg_fps": 500, "1%_low": 190, "gpu_usage": 62},
                ])
            result = import_benchmark_csv(path)
            self.assertEqual(result["avg_fps"], 300.0)
            self.assertEqual(result["low_fps"], 180.0)
            self.assertEqual(result["run_count"], 3)


class EvaluationTests(unittest.TestCase):
    def test_small_change_requests_retest(self):
        decision = evaluate_result(task(), record(284, 168), record(296, 174), round_number=1)
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertTrue(decision.needs_retest)

    def test_regression_reverts(self):
        decision = evaluate_result(task(), record(284, 168), record(250, 140), round_number=1)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)

    def test_targets_finish(self):
        decision = evaluate_result(task(), record(284, 168), record(310, 215), round_number=1)
        self.assertEqual(decision.action, AgentAction.FINISH)

    def test_above_target_average_gain_is_not_hidden_by_capped_score(self):
        decision = evaluate_result(task(), record(320, 180), record(360, 180), round_number=1)
        self.assertEqual(decision.action, AgentAction.KEEP_CHANGE)
        self.assertGreater(decision.weighted_delta_pct, 1.0)

    def test_regression_is_checked_before_target_completion(self):
        decision = evaluate_result(task(), record(400, 205), record(310, 210), round_number=1)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertTrue(decision.rollback_required)

    def test_tiny_weighted_gain_requests_retest(self):
        decision = evaluate_result(task(), record(200, 150), record(210, 146.25), round_number=1)
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertLess(decision.weighted_delta_pct, 1.0)

    def test_visual_mode_rejects_unconfirmed_quality_drop(self):
        visual_task = OptimizationTask(
            "视觉模式", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "144 Hz", "Workshop", 300, 210,
            mode="visual",
        )
        baseline = BenchmarkRecord(
            250, 170, 95, visual_task.resolution, visual_task.test_scene, repeats=3,
            configuration={"shadow_quality": "高"},
        )
        candidate = BenchmarkRecord(
            300, 220, 95, visual_task.resolution, visual_task.test_scene, repeats=3,
            configuration={"shadow_quality": "中"},
        )
        decision = evaluate_result(visual_task, baseline, candidate, round_number=1)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertIn("画质下降", decision.reasons[0])

    def test_best_record_ranking_keeps_above_target_weighted_gain(self):
        best = record(320, 220)
        candidate = record(400, 215)
        selected = GameTuneController._better_record(task(), candidate, best)
        self.assertIs(selected, candidate)

    def test_changed_conditions_are_not_evaluated(self):
        decision = evaluate_result(task(), record(284, 168), record(330, 220, comparable=False), round_number=1)
        self.assertEqual(decision.action, AgentAction.RETEST)

    def test_compares_against_current_accepted_configuration(self):
        current = record(350, 205)
        candidate = record(320, 200)
        decision = evaluate_result(task(), record(284, 168), candidate, round_number=2, reference=current)
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertLess(decision.score_delta, 0)

    def test_requires_repeated_runs_before_confirming_target(self):
        candidate = record(310, 215)
        candidate.repeats = 1
        decision = evaluate_result(task(), record(284, 168), candidate, round_number=1)
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertIn("至少需要 3 次", decision.summary)

    def test_unconfirmed_configuration_is_not_evaluated(self):
        candidate = record(310, 215)
        candidate.configuration_confirmed = False
        decision = evaluate_result(task(), record(284, 168), candidate, round_number=1)
        self.assertEqual(decision.action, AgentAction.RETEST)

    def test_adaptive_screen_reverts_obvious_regression_after_one_run(self):
        candidate = record(250, 140)
        candidate.repeats = 1
        candidate.runs = [{"avg_fps": 250, "low_fps": 140}]
        decision = evaluate_result(
            task(),
            record(284, 168),
            candidate,
            round_number=1,
            minimum_repeats=1,
            acceptance_repeats=3,
        )
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertTrue(decision.rollback_required)
        self.assertFalse(decision.task_complete)

    def test_adaptive_screen_accumulates_promising_runs_before_acceptance(self):
        candidate = record(296, 174)
        candidate.repeats = 1
        candidate.runs = [{"avg_fps": 296, "low_fps": 174}]
        decision = evaluate_result(
            task(),
            record(284, 168),
            candidate,
            round_number=1,
            minimum_repeats=1,
            acceptance_repeats=3,
        )
        self.assertEqual(decision.action, AgentAction.RETEST)
        self.assertIn("累计达到 3 次", decision.summary)

    def test_adaptive_screen_does_not_repeat_three_evidence_backed_neutral_runs(self):
        candidate = record(285, 169)
        candidate.runs = [
            {"avg_fps": 285, "low_fps": 169},
            {"avg_fps": 285, "low_fps": 169},
            {"avg_fps": 285, "low_fps": 169},
        ]
        candidate.repeats = 3
        decision = evaluate_result(
            task(),
            record(284, 168),
            candidate,
            round_number=1,
            minimum_repeats=1,
            acceptance_repeats=3,
        )
        self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)
        self.assertIn("下一项设置", decision.summary)


class PersistenceTests(unittest.TestCase):
    def test_controller_persists_history(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = GameTuneController(Path(folder) / "state.db")
            current_task = task()
            baseline = record(284, 168)
            evidence, plan = controller.create_task(current_task, baseline)
            self.assertTrue(evidence)
            self.assertEqual(plan.round_number, 1)
            controller.evaluate(current_task, baseline, record(300, 180), 1)
            history = controller.repository.task_history(current_task.task_id)
            self.assertEqual(len(history), 2)
            runtime = controller.repository.runtime_state(current_task.task_id)
            self.assertEqual(runtime["workflow_state"], "awaiting_test")
            self.assertIsNotNone(controller.repository.benchmark_by_id(current_task.task_id, baseline.record_id))

    def test_controller_retest_keeps_same_round_and_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = GameTuneController(Path(folder) / "state.db")
            current_task = task()
            baseline = record(284, 168)
            _, first_plan = controller.create_task(current_task, baseline, fps_cap_known=True, vsync_known=True)
            candidate = record(296, 174, resolution="1920 × 1080")
            decision = controller.evaluate(current_task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.RETEST)
            evidence, plan = controller.plan_after_result(current_task, candidate, baseline, 1)
            self.assertTrue(evidence)
            self.assertEqual(plan.round_number, 1)
            self.assertEqual((plan.setting, plan.old_value, plan.new_value), (first_plan.setting, first_plan.old_value, first_plan.new_value))
            self.assertNotEqual(plan.old_value, plan.new_value)

    def test_controller_tracks_current_and_best_only_after_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = GameTuneController(Path(folder) / "state.db")
            current_task = task()
            baseline = record(284, 168)
            controller.create_task(current_task, baseline, fps_cap_known=True, vsync_known=True)
            candidate = record(320, 190)
            decision = controller.evaluate(current_task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.KEEP_CHANGE)
            self.assertEqual(controller.current_record(current_task.task_id).record_id, candidate.record_id)
            runtime = controller.repository.runtime_state(current_task.task_id)
            self.assertEqual(runtime["current_record_id"], candidate.record_id)
            restored = GameTuneController(Path(folder) / "state.db")
            restored_task = restored.restore_task(current_task.task_id)
            self.assertEqual(restored_task.task_id, current_task.task_id)
            self.assertEqual(restored.current_record(current_task.task_id).record_id, candidate.record_id)


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.last_request: ModelRequest | None = None

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.last_request = request
        return ModelResponse(
            '{"summary":"建议复测","reasoning":["变化不足 5%"],"next_step":"相同条件再测三次","warning":""}',
            "fake-model",
            self.name,
        )


class ModelInterfaceTests(unittest.TestCase):
    def test_api_config_accepts_optional_reasoning_effort(self):
        config = APIConfig("https://example.test/v1", "secret", "model", 20, "high")
        self.assertEqual(config.reasoning_effort, "high")

    def test_api_config_rejects_unknown_reasoning_effort(self):
        with self.assertRaises(ValueError):
            APIConfig("https://example.test/v1", "secret", "model", 20, "extreme")

    def test_api_config_requires_complete_values(self):
        with self.assertRaises(ValueError):
            APIConfig("", "secret", "model")

    def test_provider_only_sends_reasoning_effort_when_enabled(self):
        class HTTPResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}],"model":"test"}'

        request = ModelRequest((ModelMessage("user", "test"),), response_format="text")
        sent_payloads = []

        def fake_urlopen(http_request, timeout):
            sent_payloads.append(json.loads(http_request.data.decode("utf-8")))
            self.assertEqual(timeout, 20)
            return HTTPResponse()

        with patch("agent.api.urllib.request.urlopen", side_effect=fake_urlopen):
            OpenAICompatibleProvider(APIConfig("https://example.test/v1", "secret", "model", 20)).complete(request)
            OpenAICompatibleProvider(APIConfig("https://example.test/v1", "secret", "model", 20, "high")).complete(request)
        self.assertNotIn("reasoning_effort", sent_payloads[0])
        self.assertEqual(sent_payloads[1]["reasoning_effort"], "high")

    def test_openai_compatible_lists_models(self):
        class HTTPResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "alpha"}]}).encode("utf-8")

        def fake_urlopen(http_request, timeout):
            self.assertTrue(http_request.full_url.endswith("/v1/models"))
            self.assertEqual(timeout, 7.0)
            return HTTPResponse()

        with patch("agent.api.urllib.request.urlopen", side_effect=fake_urlopen):
            models = OpenAICompatibleProvider.list_models("https://example.test/v1/", "secret", 7)
        self.assertEqual(models, ["alpha", "zeta"])

    def test_provider_receives_structured_request(self):
        provider = FakeProvider()
        current_task = task()
        decision = evaluate_result(current_task, record(284, 168), record(296, 174), round_number=1)
        explanation = explain_decision(provider, current_task, decision)
        self.assertEqual(explanation["summary"], "建议复测")
        self.assertEqual(explanation["source"], "model")
        self.assertEqual(explanation["model"], "fake-model")
        self.assertIsNotNone(provider.last_request)
        self.assertEqual(provider.last_request.response_format, "json")
        self.assertEqual(provider.last_request.metadata["purpose"], "decision_explanation")
        self.assertEqual(provider.last_request.metadata["task_id"], current_task.task_id)
        self.assertIn("普通游戏玩家", provider.last_request.messages[0].content)
        self.assertIn("综合得分", provider.last_request.messages[0].content)
        payload = json.loads(provider.last_request.messages[1].content)
        self.assertIn("不熟悉性能分析", payload["audience"])
        self.assertIn("不使用学术术语", payload["language"])
        self.assertIn("重复本轮测试", payload["must_follow"])
        self.assertTrue(any("keep_change" in item for item in payload["constraints"]))

    def test_completed_task_uses_final_report_prompt(self):
        provider = FakeProvider()
        current_task = task()
        decision = Decision(
            AgentAction.REVERT_CHANGE,
            "已达到实验轮数上限，请恢复上一组配置后结束任务。",
            reasons=["候选配置的平均 FPS 明显下降"],
            task_complete=True,
            rollback_required=True,
            stop_reason="max_rounds",
        )
        explanation = explain_decision(provider, current_task, decision)
        self.assertEqual(explanation["source"], "model")
        self.assertIsNotNone(provider.last_request)
        self.assertEqual(provider.last_request.metadata["purpose"], "final_report")
        self.assertEqual(provider.last_request.max_output_tokens, 3000)
        self.assertIn("最终报告撰写器", provider.last_request.messages[0].content)
        self.assertIn("普通游戏玩家", provider.last_request.messages[0].content)
        payload = json.loads(provider.last_request.messages[1].content)
        self.assertEqual(payload["report_type"], "final_report")
        self.assertTrue(payload["final_decision"]["rollback_required"])
        self.assertTrue(any("先要求恢复" in item for item in payload["constraints"]))

    def test_missing_provider_uses_local_fallback(self):
        current_task = task()
        decision = evaluate_result(current_task, record(284, 168), record(296, 174), round_number=1)
        explanation = explain_decision(None, current_task, decision)
        self.assertEqual(explanation["summary"], decision.summary)
        self.assertEqual(explanation["source"], "local")
        self.assertIn("未使用模型 API", explanation["warning"])


class WorkflowTests(unittest.TestCase):
    def test_full_feedback_loop(self):
        workflow = Workflow()
        workflow.start()
        workflow.validated()
        workflow.wait_for_test()
        self.assertEqual(workflow.state, WorkflowState.AWAITING_TEST)
        self.assertEqual(workflow.round_number, 1)
        workflow.begin_evaluation()
        workflow.continue_testing()
        self.assertEqual(workflow.round_number, 2)

    def test_illegal_transition_is_rejected(self):
        with self.assertRaises(ValueError):
            Workflow().finish()

    def test_unknown_recommendation_is_rejected(self):
        plan = ExperimentPlan(
            AgentAction.RECOMMEND_CHANGE,
            "非法设置测试",
            "不存在的设置",
            "高",
            "低",
            "测试",
            "测试",
            "测试三次",
            "性能提升",
            "恢复原值",
            1,
        )
        with self.assertRaises(PlanValidationError):
            validate_plan(task(), plan, KnowledgeBase(Path(__file__).resolve().parents[1] / "knowledge"))


if __name__ == "__main__":
    unittest.main()
