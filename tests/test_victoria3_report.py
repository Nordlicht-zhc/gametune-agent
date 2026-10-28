import tempfile
from pathlib import Path
import unittest

from tools.victoria3_report import (new_report, attach_benchmark, metric_rows, report_html,
                                    ReportRepository, attach_simulation)


class ReportTests(unittest.TestCase):
    def report(self):
        return new_report({"cpu": "<script>test</script>", "gpu": "test"},
                          {"Graphics": {"resolution": "3200x2000"}},
                          {"Graphics": {"resolution": "2560x1600"}}, phase="applied")

    def test_missing_metrics_and_escape(self):
        report = self.report()
        self.assertTrue(all(row[1] == "未采集" for row in metric_rows(report)))
        html = report_html(report)
        self.assertNotIn("<script>test</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("3200x2000", html)
        self.assertIn("高级优化建议", html)

    def test_comparison_requires_confirmation_and_resets_on_import(self):
        report = self.report()
        metrics = {"avg_fps": 50, "low_fps": 30, "run_count": 3, "gpu_usage": 80}
        report = attach_benchmark(report, "baseline", metrics, "before.csv")
        report = attach_benchmark(report, "candidate", {**metrics, "avg_fps": 60, "gpu_usage": 90}, "after.csv")
        self.assertEqual(metric_rows(report)[0][3], "—")
        report["comparison_confirmed"] = True
        self.assertEqual(metric_rows(report)[0][3], "+20.00%")
        self.assertEqual(metric_rows(report)[3][3], "+10.00 个百分点")
        report = attach_benchmark(report, "candidate", metrics, "new.csv")
        self.assertFalse(report["comparison_confirmed"])

    def test_assessment_rejects_candidate(self):
        report = new_report({}, {"Graphics": {}})
        with self.assertRaises(ValueError):
            attach_benchmark(report, "candidate", {}, "test")

    def test_restored_data_is_historical(self):
        report = self.report()
        report["phase"] = "restored"
        report["observations"]["candidate"] = {"avg_fps": 50}
        self.assertIn("历史配置", metric_rows(report)[0][4])

    def test_repository_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = ReportRepository(Path(folder) / "test.db")
            report = self.report()
            repo.save(report)
            report["phase"] = "restored"
            repo.save(report)
            self.assertEqual(repo.list(), [report])

    def test_wrong_simulation_config_rejected(self):
        experiment = {"baseline": {"fields": {"configuration": '{}'}},
                      "candidate": {"fields": {"configuration": '{}'}}}
        with self.assertRaises(ValueError):
            attach_simulation(self.report(), experiment)

    def test_simulation_matches_settings_and_is_not_fps(self):
        import json
        report = self.report()
        def record(graphics, seconds):
            return {"fields": {"configuration": json.dumps(graphics)}, "conditions": {"save": "same"},
                    "runs": [{"game_days": 30, "elapsed_seconds": seconds} for _ in range(3)]}
        result = attach_simulation(report, {"baseline": record(report["before"]["Graphics"], 100),
            "candidate": record(report["after"]["Graphics"], 80), "configuration_confirmed": True})
        rows = metric_rows(result)
        self.assertEqual(rows[2][3], "+25.00%")
        self.assertEqual(rows[0][1], "未采集")
