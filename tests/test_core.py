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
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask
from tools.calculate_metrics import metrics_from_frame_times, normalized_score
from tools.import_benchmark import import_benchmark_csv
from tools.resolution import aspect_ratio_name, lower_resolution_same_aspect, normalize_resolution
from tools.hardware_catalog import HardwareCatalog
from tools.analyze_evidence import analyze_evidence


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

    def test_provider_receives_structured_request(self):
        provider = FakeProvider()
        current_task = task()
        decision = evaluate_result(current_task, record(284, 168), record(296, 174), round_number=1)
        explanation = explain_decision(provider, current_task, decision)
        self.assertEqual(explanation["summary"], "建议复测")
        self.assertEqual(explanation["source"], "model")
        self.assertIsNotNone(provider.last_request)
        self.assertEqual(provider.last_request.response_format, "json")
        self.assertEqual(provider.last_request.metadata["task_id"], current_task.task_id)

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
