from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from agent.controller import GameTuneController
from copy import deepcopy
from dataclasses import replace
from agent.next_step import rank_candidates, next_step_request
from agent.explainer import explain_decision
from models.schemas import AgentAction, BenchmarkRecord, Decision, ExperimentPlan, OptimizationTask
from tools.history_impacts import historical_impacts, historical_lead


def observation(setting="graphics_mode", old="hybrid", new="dgpu", avg=465.2, low=122.1, round_number=1):
    return {"record_id": f"r{round_number}", "reference_record_id": "baseline", "round_number": round_number,
            "setting_id": setting, "before_configuration": {setting: old, "fps_cap": "0", "vsync": "disabled"},
            "after_configuration": {setting: new, "fps_cap": "0", "vsync": "disabled"},
            "avg_fps": avg, "low_fps": low, "reference_avg_fps": 200, "reference_low_fps": 100,
            "valid": True, "comparable": True, "configuration_confirmed": True, "repeats": 1,
            "test_scene": "scene", "reference_test_scene": "scene",
            "decision": {"action": "retest", "accepted": False}}


class HistoryImpactTests(unittest.TestCase):
    def task(self):
        return OptimizationTask("history", "Counter-Strike 2", "CPU", "GPU", "1680 × 1050", "300 Hz", "scene", 1000, 600)

    def plan(self, setting, old, new):
        return ExperimentPlan(AgentAction.RECOMMEND_CHANGE, setting, setting, old, new,
                              "依据", "假设", "方法", "标准", "恢复", 2, setting_id=setting)

    def test_largest_observed_setting_is_not_hidden_by_retest_status(self):
        rows = [observation(), observation("shadow_quality", "高", "中", 220, 110, 2)]
        result = historical_impacts(rows)
        top = result["single_setting_effects"][0]
        self.assertEqual(top["changed_settings"], ["graphics_mode"])
        self.assertEqual(top["avg_delta_pct"], 132.6)
        self.assertEqual(top["status"], "observed_not_verified")
        self.assertIn("显卡输出方式", historical_lead(result))
        self.assertIn("待验证", historical_lead(result))

    def test_cumulative_records_are_not_counted_as_independent_experiments(self):
        first = observation()
        second = deepcopy(first)
        second.update(record_id="r-later", repeats=3)
        second["decision"]["accepted"] = True
        rows = historical_impacts([first, second])["single_setting_effects"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeats"], 3)
        self.assertEqual(rows[0]["status"], "verified")

    def test_later_invalid_record_does_not_erase_valid_observation(self):
        valid = observation()
        invalid = {**valid, "valid": False, "avg_fps": 9999}
        result = historical_impacts([valid, invalid])
        self.assertEqual(result["single_setting_effects"][0]["avg_delta_pct"], 132.6)
        self.assertEqual(result["excluded_records"], 1)

    def test_multiple_changes_are_not_split_into_single_setting_effects(self):
        item = observation()
        item["before_configuration"]["shadow_quality"] = "高"
        item["after_configuration"]["shadow_quality"] = "低"
        summary = historical_impacts([item])
        self.assertEqual(summary["single_setting_effects"], [])
        self.assertEqual(len(summary["profile_effects"]), 1)
        self.assertIn("整组", historical_lead(summary))

    def test_unknown_values_scene_changes_and_nonfinite_numbers_are_excluded(self):
        missing = observation()
        missing["after_configuration"]["vsync"] = "unknown"
        scene = {**observation(), "test_scene": "different", "round_number": 2}
        nan = {**observation(), "avg_fps": float("nan"), "round_number": 3}
        result = historical_impacts([missing, scene, nan])
        self.assertEqual(result["single_setting_effects"], [])
        self.assertEqual(result["excluded_records"], 3)

    def test_matching_history_changes_local_priority_but_keeps_condition_gate(self):
        task = self.task()
        current = BenchmarkRecord(200, 100, 99, task.resolution, "scene", configuration={"graphics_mode": "hybrid", "fps_cap": "0", "vsync": "disabled"})
        graphics = self.plan("graphics_mode", "hybrid", "dgpu")
        shadow = self.plan("shadow_quality", "高", "中")
        ranked = rank_candidates(task, current, [], [shadow, graphics], [observation()])
        self.assertIs(ranked[0], graphics)
        other_context = replace(current, configuration={**current.configuration, "fps_cap": "144"})
        self.assertIs(rank_candidates(task, other_context, [], [shadow, graphics], [observation()])[0], shadow)
        condition = self.plan("test_conditions", "old", "new")
        self.assertIs(rank_candidates(task, current, [], [graphics, condition], [observation()])[0], condition)

    def test_model_and_local_explanation_receive_same_historical_effects(self):
        task = self.task()
        current = BenchmarkRecord(200, 100, 99, task.resolution, "scene")
        payload = next_step_request(task, current, [], [self.plan("graphics_mode", "hybrid", "dgpu")], history=[observation()])
        self.assertIn('"historical_impacts"', payload.messages[-1].content)
        result = explain_decision(None, task, Decision(AgentAction.RETEST, "本轮需要确认", reasons=["继续采样"]),
            workflow={"historical_impacts": historical_impacts([observation()])})
        self.assertIn("显卡输出方式", result["reasoning"][0])
        self.assertEqual(result["summary"], "本轮需要确认")

    def test_controller_explains_database_history_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            controller = GameTuneController(path)
            task = replace(self.task(), is_laptop=True, gpu_power_w=100, graphics_mode="hybrid",
                           power_mode="ac_performance", cooling_mode="performance", fps_cap=0, vsync="disabled")
            config = {"graphics_mode": "hybrid", "power_mode": "ac_performance",
                      "cooling_mode": "performance", "fps_cap": "0", "vsync": "disabled"}
            baseline = BenchmarkRecord(200, 100, 60, task.resolution, "scene", repeats=3, configuration=config)
            controller.create_task(task, baseline)
            candidate = replace(baseline, record_id="real-history-observation", avg_fps=465.2, low_fps=122.1,
                                repeats=1, configuration={**config, "graphics_mode": "dgpu"})
            decision = controller.evaluate(task, baseline, candidate, 1)
            restored = GameTuneController(path)
            restored.restore_task(task.task_id)
            explanation = restored.explain(task, decision)
            self.assertIn("显卡输出方式", explanation["reasoning"][0])
            self.assertIn("+132.6%", explanation["reasoning"][0])
            self.assertIn("待验证", explanation["reasoning"][0])
            self.assertFalse(decision.accepted)
