from pathlib import Path
import tempfile
import unittest

from tools.victoria3 import (SimulationConditions, SimulationRepository, compare_runs,
                             import_simulation_csv, save_fingerprint, summarize_runs)


class Victoria3Tests(unittest.TestCase):
    def record(self, seconds=100):
        return {"conditions": {"save_sha256": "abc", "version": "test", "mods": "none",
                               "date_range": "test interval", "speed": "5", "environment": "fixed"},
                "runs": [{"game_days": 30, "elapsed_seconds": seconds} for _ in range(3)]}

    def test_throughput_improvement_and_confirmation(self):
        before, after = self.record(), self.record(80)
        result = compare_runs(before, after, True)
        self.assertEqual(result["action"], "keep")
        self.assertAlmostEqual(result["simulation_delta_pct"], 25)
        self.assertEqual(compare_runs(before, after, False)["action"], "retest")

    def test_regression_and_no_change(self):
        self.assertEqual(compare_runs(self.record(), self.record(120), True)["action"], "revert")
        self.assertEqual(compare_runs(self.record(), self.record(), True)["action"], "retest")

    def test_conditions_and_period_must_match(self):
        for key in self.record()["conditions"]:
            after = self.record(80)
            after["conditions"][key] = "changed"
            with self.assertRaises(ValueError):
                compare_runs(self.record(), after, True)
        after = self.record()
        for row in after["runs"]:
            row["game_days"] = 20
        with self.assertRaises(ValueError):
            compare_runs(self.record(), after, True)

    def test_noise_blocks_acceptance(self):
        after = self.record(80)
        after["runs"][0]["elapsed_seconds"] = 60
        self.assertEqual(compare_runs(self.record(), after, True)["action"], "retest")

    def test_optional_fps_guardrail(self):
        before, after = self.record(), self.record(80)
        for row in before["runs"]:
            row.update(avg_fps=60, low_fps=40)
        for row in after["runs"]:
            row.update(avg_fps=50, low_fps=30)
        self.assertEqual(compare_runs(before, after, True)["action"], "revert")
        self.assertEqual(compare_runs(before, self.record(80), True)["action"], "retest")

    def test_invalid_and_incomplete_runs(self):
        for value in [0, -1, "nan", "inf", "", None]:
            rows = self.record()["runs"]
            rows[0]["elapsed_seconds"] = value
            with self.assertRaises(ValueError):
                summarize_runs(rows)
        with self.assertRaises(ValueError):
            summarize_runs(self.record()["runs"][:2])
        rows = self.record()["runs"]
        rows[0]["avg_fps"] = 60
        with self.assertRaises(ValueError):
            summarize_runs(rows)

    def test_hash_csv_and_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.csv"
            path.write_text("game_days,elapsed_seconds\n30,100\n30,101\n30,99\n", encoding="utf-8")
            self.assertEqual(len(import_simulation_csv(path)), 3)
            digest = save_fingerprint(path)
            self.assertEqual(len(digest), 64)
            path.write_text("changed", encoding="utf-8")
            self.assertNotEqual(digest, save_fingerprint(path))
            repository = SimulationRepository(Path(folder) / "test.db")
            repository.save({"kind": "baseline", "baseline": self.record()})
            self.assertEqual(SimulationRepository(repository.path).history()[0]["baseline"], self.record())

    def test_conditions_required(self):
        with self.assertRaises(ValueError):
            SimulationConditions("", "v", "none", "dates", "5", "fixed")


if __name__ == "__main__":
    unittest.main()
