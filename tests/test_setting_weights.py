from __future__ import annotations
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QTableWidget
from app import ExperimentPage, TaskState
from models.schemas import AgentAction, ExperimentPlan
from tools.setting_weights import setting_weights, candidate_reference_weight
from tools.performance_knowledge import load_performance_knowledge
from test_history_impacts import observation

KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"


class WeightTests(unittest.TestCase):
    def weights(self, **kwargs):
        return setting_weights("Counter-Strike 2", "competitive", kwargs.get("history", []), directory=kwargs.get("directory"))

    def plan(self, setting, old, new):
        return ExperimentPlan(AgentAction.RECOMMEND_CHANGE, setting, setting, old, new,
                              "依据", "假设", "方法", "标准", "恢复", 1, setting_id=setting)

    def test_ten_weights_are_derived_and_normalized(self):
        result = self.weights()
        rows = result["external_weights"]
        self.assertEqual(len(rows), 10)
        self.assertAlmostEqual(sum(row["weight_pct"] for row in rows), 100, places=1)
        self.assertEqual(rows[0]["setting_id"], "particle_detail")
        self.assertEqual(rows[0]["weight_pct"], 26.36)
        self.assertEqual(result["local_weights"], [])

    def test_formula_preserves_metric_spans(self):
        row = self.weights()["external_weights"][0]
        self.assertAlmostEqual(row["avg_span_pct"], (979.5 - 885.8) / 979.5 * 100, places=4)
        self.assertAlmostEqual(row["impact_score"], .35 * (979.5 - 885.8) / 979.5 * 100 +
                               .65 * (284.7 - 271.8) / 284.7 * 100)

    def test_unmeasured_is_not_zero_and_local_is_separate(self):
        empty = self.weights()
        self.assertIsNone(next(row for row in empty["unmeasured"] if row["setting_id"] == "resolution")["weight_pct"])
        result = self.weights(history=[observation()])
        self.assertEqual(result["local_weights"][0]["setting_id"], "graphics_mode")
        self.assertEqual(result["local_weights"][0]["weight_pct"], 100)
        self.assertEqual(result["local_weights"][0]["status"], "observed_not_verified")
        self.assertEqual(result["external_weights"], empty["external_weights"])

    def test_combined_changes_do_not_create_local_setting_weights(self):
        row = observation()
        row["before_configuration"]["shadow_quality"] = "高"
        row["after_configuration"]["shadow_quality"] = "低"
        self.assertEqual(self.weights(history=[row])["local_weights"], [])

    def test_candidate_weight_requires_exact_improving_levels(self):
        weights = self.weights()
        self.assertGreater(candidate_reference_weight(weights, self.plan("msaa_mode", "8x_msaa", "4x_msaa")), 0)
        self.assertEqual(candidate_reference_weight(weights, self.plan("texture_detail", "high", "medium")), 0)
        self.assertEqual(candidate_reference_weight(weights, self.plan("msaa_mode", "none", "8x_msaa")), 0)

    def test_external_sources_are_normalized_independently(self):
        data = load_performance_knowledge(KNOWLEDGE, "Counter-Strike 2")
        second = deepcopy(data["sources"][0])
        second["id"] = "second-platform"
        data["sources"].append(second)
        measurement = deepcopy(data["measurements"][0])
        measurement["source_id"] = second["id"]
        data["measurements"].append(measurement)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "cs2_performance.json").write_text(json.dumps(data), encoding="utf-8")
            rows = self.weights(directory=folder)["external_weights"]
        self.assertEqual(next(row for row in rows if row["source_id"] == second["id"])["weight_pct"], 100)
        self.assertAlmostEqual(sum(row["weight_pct"] for row in rows if row["source_id"] != second["id"]), 100, places=1)

    def test_weight_dialog_can_open_and_close_with_measured_and_unknown_rows(self):
        qt = QApplication.instance() or QApplication([])
        page = ExperimentPage(TaskState(has_task=True))
        result = {}
        def inspect():
            dialog = qt.activeModalWidget()
            if dialog is not None:
                table = dialog.findChild(QTableWidget)
                result["labels"] = [table.item(row, 0).text() for row in range(table.rowCount())]
                result["weights"] = [table.item(row, 1).text() for row in range(table.rowCount())]
                dialog.accept()
        QTimer.singleShot(0, inspect)
        page.weights_button.click()
        self.assertIn("粒子细节", result["labels"])
        self.assertIn("26.36%", result["weights"])
        self.assertIn("未测", result["weights"])
        page.close()

    def test_controller_persists_weights_and_model_receives_them(self):
        from agent.api import ModelResponse
        from agent.controller import GameTuneController
        from agent.next_step import next_step_request
        from models.schemas import BenchmarkRecord, OptimizationTask, Decision
        with tempfile.TemporaryDirectory() as folder:
            controller = GameTuneController(Path(folder) / "weights.db")
            task = OptimizationTask("weights", "Counter-Strike 2", "CPU", "GPU", "1680 × 1050",
                "300 Hz", "scene", 1000, 600, fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
            record = BenchmarkRecord(200, 100, 99, task.resolution, "scene", repeats=3,
                configuration={"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高", "dynamic_shadows": "all", "msaa_mode": "8x_msaa"})
            evidence, plan = controller.create_task(task, record)
            self.assertEqual(len(plan.planning_context["setting_weights"]["external_weights"]), 10)
            payload = json.loads(next_step_request(task, record, evidence, [plan]).messages[-1].content)
            self.assertEqual(payload["setting_weights"]["external_weights"][0]["weight_pct"], 26.36)
            class Provider:
                def complete(inner, request):
                    inner.payload = json.loads(request.messages[-1].content)
                    return ModelResponse(json.dumps({"summary": "说明", "reasoning": ["原因"], "next_step": "继续", "warning": ""}), "mock", "mock")
            provider = Provider()
            controller.model_provider = provider
            controller.explain(task, Decision(AgentAction.RETEST, "继续确认", reasons=["次数不足"]), plan=plan)
            self.assertEqual(len(provider.payload["workflow"]["setting_weights"]["external_weights"]), 10)
