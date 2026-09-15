import tempfile
import unittest
from pathlib import Path

from agent.planner import plan_next_experiment
from agent.validator import KnowledgeBase, PlanValidationError, validate_plan
from models.schemas import AgentAction, BenchmarkRecord, OptimizationTask
from tools.analyze_evidence import analyze_evidence
from tools.import_benchmark import import_benchmark_csv
from tools.resolution import lower_resolution_same_aspect, parse_resolution


class PlanningImportTests(unittest.TestCase):
    def setUp(self):
        self.task = OptimizationTask("test", "Counter-Strike 2", "CPU", "GPU", "2560 × 1440", "300Hz", "scene", 300, 210)
        self.knowledge = KnowledgeBase(Path(__file__).resolve().parents[1] / "knowledge")

    def gpu_plan(self, settings):
        record = BenchmarkRecord(200, 150, 99, "2560 × 1440", "scene", repeats=3, configuration=settings)
        evidence = analyze_evidence(self.task, record, fps_cap_known=True, vsync_known=True)
        return plan_next_experiment(self.task, record, evidence, 1)

    def test_gpu_unknown_setting_requests_actual_value(self):
        plan = self.gpu_plan({})
        validate_plan(self.task, plan, self.knowledge)
        self.assertEqual(plan.action, AgentAction.REQUEST_DATA)
        self.assertEqual(plan.setting_id, "shadow_quality")

    def test_gpu_known_setting_changes_exactly_one_level(self):
        plan = self.gpu_plan({"shadow_quality": "高"})
        validate_plan(self.task, plan, self.knowledge)
        self.assertEqual((plan.old_value, plan.new_value), ("高", "中"))
        plan.setting = "本地化显示名称"
        validate_plan(self.task, plan, self.knowledge)
        plan.new_value = "低"
        with self.assertRaises(PlanValidationError):
            validate_plan(self.task, plan, self.knowledge)

    def test_lowest_setting_finishes_without_noop_experiment(self):
        plan = self.gpu_plan({"shadow_quality": "低"})
        validate_plan(self.task, plan, self.knowledge)
        self.assertEqual(plan.action, AgentAction.FINISH)

    def test_unsupported_game_cannot_enter_diagnostic_workflow(self):
        plan = self.gpu_plan({})
        self.task.game = "Unsupported Game"
        with self.assertRaises(PlanValidationError):
            validate_plan(self.task, plan, self.knowledge)

    def import_text(self, content):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runs.csv"
            path.write_text(content, encoding="utf-8")
            return import_benchmark_csv(path)

    def test_bad_rows_are_reported_instead_of_silently_discarded(self):
        for value in ("nan", "inf", "0", "broken"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "第 3 行"):
                self.import_text(f"avg_fps,1%_low\n300,200\n{value},180\n")

    def test_summary_retains_runs_and_dispersion(self):
        result = self.import_text("avg_fps,1%_low\n290,190\n300,200\n310,210\n")
        self.assertEqual(len(result["runs"]), 3)
        self.assertAlmostEqual(result["avg_spread_pct"], 6.67)
        self.assertEqual(result["low_spread_pct"], 10.0)

    def test_busy_duration_is_not_imported_as_utilization(self):
        result = self.import_text("avg_fps,1%_low,gpu_busy\n300,200,3.5\n")
        self.assertNotIn("gpu_usage", result)

    def test_mixed_process_capture_requires_filtering(self):
        with self.assertRaisesRegex(ValueError, "多个进程"):
            self.import_text("processid,avg_fps,1%_low\n1,300,200\n2,60,30\n")

    def test_frame_capture_counts_as_one_run(self):
        result = self.import_text("frame_time_ms\n" + "10\n" * 100)
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["duration_seconds"], 1.0)

    def test_small_resolution_does_not_change_aspect_to_reach_minimum(self):
        width, height = parse_resolution(lower_resolution_same_aspect("960 × 540"))
        self.assertAlmostEqual(width / height, 16 / 9, delta=0.025)
        with self.assertRaises(ValueError):
            lower_resolution_same_aspect("854 × 480")

    def test_resolution_evidence_requires_controlled_reduction(self):
        baseline = BenchmarkRecord(200, 150, 60, "2560 × 1440", "scene", configuration={"shadow_quality": "高"})
        current = BenchmarkRecord(201, 151, 60, "1920 × 1080", "scene", configuration={"shadow_quality": "高"})
        evidence = analyze_evidence(self.task, current, baseline)
        self.assertIn("resolution_flat", {item.code for item in evidence})
        current.configuration["shadow_quality"] = "低"
        evidence = analyze_evidence(self.task, current, baseline)
        self.assertNotIn("resolution_flat", {item.code for item in evidence})
        current.configuration["shadow_quality"] = "高"
        current.configuration["power_mode"] = "ac_performance"
        self.assertNotIn("resolution_flat", {item.code for item in analyze_evidence(self.task, current, baseline)})
        current.configuration.pop("power_mode")
        current.comparable = False
        self.assertNotIn("resolution_flat", {item.code for item in analyze_evidence(self.task, current, baseline)})


if __name__ == "__main__":
    unittest.main()
