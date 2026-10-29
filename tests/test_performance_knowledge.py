from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.api import ModelResponse
from agent.controller import GameTuneController
from agent.next_step import next_step_request
from agent.validator import KnowledgeBase
from models.schemas import BenchmarkRecord, OptimizationTask
from tools.performance_knowledge import load_performance_knowledge, performance_context


KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"


class PerformanceKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.controller = GameTuneController(Path(self.folder.name) / "knowledge.db")
        self.task = OptimizationTask("知识参考", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440",
            "144 Hz", "scene", 1000, 600, fps_cap=0, vsync="disabled")
        self.current = BenchmarkRecord(200, 100, 99, self.task.resolution, "scene", repeats=2,
            configuration={"fps_cap": "0", "vsync": "disabled", "dynamic_shadows": "all",
                "shadow_quality": "高", "msaa_mode": "8x_msaa"})
        self.evidence, _ = self.controller.create_task(self.task, self.current)
        self.options = self.controller.next_step_candidates(self.task, self.current, self.evidence, 1)

    def test_ten_setting_families_have_traceable_measurements(self):
        data = KnowledgeBase(KNOWLEDGE).performance_evidence(self.task.game)
        self.assertEqual(len(data["measurements"]), 10)
        sources = {item["id"]: item for item in data["sources"]}
        for item in data["measurements"]:
            self.assertTrue(sources[item["source_id"]]["url"].startswith("https://"))
        self.assertIn("not P1", sources["erza_graphics_202604"]["low_metric"])
        msaa = next(item for item in data["measurements"] if item["setting_id"] == "msaa_mode")
        self.assertEqual(msaa["levels"][-1]["avg_fps"], 865.6)
        filtering = next(item for item in data["measurements"] if item["setting_id"] == "texture_filtering")
        self.assertNotIn("trilinear", {item["value"] for item in filtering["levels"]})

    def test_exact_candidate_transition_uses_its_own_reference_denominator(self):
        context = performance_context(self.task, self.current, self.options, [])
        msaa = next(item for item in context["setting_references"] if item["setting_id"] == "msaa_mode")
        change = msaa["comparisons"][0]
        self.assertTrue(change["exact_transition_available"])
        self.assertEqual(change["old_value"], "8x_msaa")
        self.assertEqual(change["new_value"], "4x_msaa")
        self.assertEqual(change["source_avg_delta_pct"], 6.83)
        self.assertNotIn("predicted_local_fps", context)
        self.assertEqual(self.current.avg_fps, 200)

    def test_user_changes_are_included_even_when_not_current_candidates(self):
        history = [{"before_configuration": {"particle_detail": "low", "fsr": "disabled"},
                    "after_configuration": {"particle_detail": "very_high", "fsr": "performance"}}]
        context = performance_context(self.task, self.current, self.options, history)
        particle = next(item for item in context["setting_references"] if item["setting_id"] == "particle_detail")
        self.assertEqual(particle["comparisons"][0]["origin"], "user_recorded_change")
        self.assertAlmostEqual(particle["comparisons"][0]["source_avg_delta_pct"], -9.57)
        self.assertEqual(context["uncovered_changes"][0]["setting_id"], "fsr")

    def test_missing_levels_are_not_interpolated_and_labels_are_normalized(self):
        history = [{"before_configuration": {"texture_detail": "高", "msaa_mode": "8x MSAA"},
                    "after_configuration": {"texture_detail": "中", "msaa_mode": "4x MSAA"}}]
        context = performance_context(self.task, self.current, self.options, history)
        texture = next(item for item in context["setting_references"] if item["setting_id"] == "texture_detail")
        self.assertFalse(texture["comparisons"][0]["exact_transition_available"])
        self.assertNotIn("source_avg_delta_pct", texture["comparisons"][0])
        msaa = next(item for item in context["setting_references"] if item["setting_id"] == "msaa_mode")
        self.assertTrue(msaa["comparisons"][-1]["exact_transition_available"])

    def test_ai_request_contains_sources_limits_and_only_relevant_references(self):
        request = next_step_request(self.task, self.current, self.evidence, self.options)
        payload = json.loads(request.messages[-1].content)
        context = payload["external_performance_reference"]
        self.assertTrue(context["available"])
        self.assertEqual(context["evidence_type"], "external_reference_not_local_measurement")
        self.assertTrue(context["usage_rules"])
        ids = {item["setting_id"] for item in context["setting_references"]}
        self.assertEqual(ids, {"shadow_quality", "msaa_mode"})
        self.assertNotIn("computerbase_cs2_202404", {item["id"] for item in context["sources"]})

    def test_controller_passes_configured_knowledge_directory_to_model(self):
        data = load_performance_knowledge(KNOWLEDGE, self.task.game)
        data["reviewed_on"] = "custom-directory-marker"
        path = Path(self.folder.name) / "cs2_performance.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        (Path(self.folder.name) / "cs2.json").write_text((KNOWLEDGE / "cs2.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.controller.knowledge = KnowledgeBase(self.folder.name)
        class Provider:
            def complete(inner, request):
                payload = json.loads(request.messages[-1].content)
                self.assertTrue(payload["external_performance_reference"]["available"])
                self.assertEqual(payload["external_performance_reference"]["reviewed_on"], "custom-directory-marker")
                chosen = payload["candidates"][-1]
                return ModelResponse(json.dumps({"candidate_id": chosen["candidate_id"], "reason": "参考设置负载与本机证据，选择合法候选。"}), "mock", "mock")
        self.controller.model_provider = Provider()
        result = self.controller.choose_next_step(self.task, self.current, self.evidence, self.options)
        self.assertEqual(result.source, "model")
        custom_request = next_step_request(self.task, self.current, self.evidence, self.options,
            knowledge_directory=self.folder.name)
        custom = json.loads(custom_request.messages[-1].content)["external_performance_reference"]
        self.assertEqual(custom["reviewed_on"], "custom-directory-marker")

    def test_unknown_configuration_fields_are_not_sent_as_uncovered_changes(self):
        history = [{"before_configuration": {"private_field": "old"},
                    "after_configuration": {"private_field": "new"}}]
        context = performance_context(self.task, self.current, self.options, history)
        self.assertEqual(context["uncovered_changes"], [])

    def test_missing_corrupt_and_unsupported_optional_data_fall_back(self):
        self.assertEqual(load_performance_knowledge(self.folder.name, self.task.game), {})
        self.assertEqual(load_performance_knowledge(KNOWLEDGE, "Other Game"), {})
        path = Path(self.folder.name) / "cs2_performance.json"
        for content in ("broken json", "[]", '{"schema_version":1}', '{"schema_version":2}'):
            path.write_text(content, encoding="utf-8")
            self.assertEqual(load_performance_knowledge(self.folder.name, self.task.game), {})
        context = performance_context(self.task, self.current, self.options, [], directory=self.folder.name)
        self.assertFalse(context["available"])

    def test_invalid_measurement_does_not_enter_optional_knowledge(self):
        data = load_performance_knowledge(KNOWLEDGE, self.task.game)
        path = Path(self.folder.name) / "cs2_performance.json"
        for value in (0, -1, float("nan"), float("inf"), True):
            data["measurements"][0]["levels"][0]["avg_fps"] = value
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_performance_knowledge(self.folder.name, self.task.game), {})


if __name__ == "__main__":
    unittest.main()
