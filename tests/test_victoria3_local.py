from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.victoria3_local import LocalVictoria3


class LocalVictoriaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = {"Graphics": {"resolution": "3200x2000", "quality": "custom"},
                         "Audio": {"volume": 0.5}, "game": {"autosave": "halfyear"}}
        self.path = self.root / "pdx_settings.json"
        self.write(self.original)
        self.service = LocalVictoria3(self.root, running_check=lambda: False)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_discovery_is_read_only_and_sorts_saves(self):
        saves = self.root / "save games"
        saves.mkdir()
        (saves / "example.v3").write_bytes(b"untouched")
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "system.log").write_text("CPU Model: Test CPU\nGPU Name: Test GPU\nExe Git Version: 1.13.11", encoding="utf-8")
        raw = self.path.read_bytes()
        data = self.service.discover()
        self.assertEqual(data["cpu"], "Test CPU")
        self.assertEqual(len(data["saves"]), 1)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(self.service.backups.exists())

    def test_apply_restore_preserves_unrelated_settings(self):
        plan = self.service.preview("resolution")
        self.assertEqual(plan["changes"], {"resolution": "2560x1600"})
        backup = self.service.apply(plan)
        self.assertEqual(self.read()["Graphics"]["resolution"], "2560x1600")
        current = self.read()
        current["Audio"]["volume"] = 0.8
        self.write(current)
        self.service.restore(backup)
        self.assertEqual(self.read()["Graphics"], self.original["Graphics"])
        self.assertEqual(self.read()["Audio"]["volume"], 0.8)
        self.assertEqual(self.read()["game"], self.original["game"])

    def test_performance_settings_and_removal_of_previously_absent_keys(self):
        backup = self.service.apply(self.service.preview("performance"))
        self.assertEqual(self.read()["Graphics"]["shadowmap_resolution"], "disabled")
        self.service.restore(backup)
        self.assertEqual(self.read(), self.original)

    def test_running_game_blocks_apply_and_restore(self):
        plan = self.service.preview("resolution")
        self.service.running_check = lambda: True
        with self.assertRaises(ValueError):
            self.service.apply(plan)
        self.assertEqual(self.read(), self.original)
        self.service.running_check = lambda: False
        backup = self.service.apply(plan)
        self.service.running_check = lambda: True
        with self.assertRaises(ValueError):
            self.service.restore(backup)

    def test_stale_or_tampered_plan_rejected(self):
        plan = self.service.preview("resolution")
        changed = deepcopy(plan)
        changed["changes"]["resolution"] = "1x1"
        with self.assertRaises(ValueError):
            self.service.apply(changed)
        self.write({**self.original, "extra": True})
        with self.assertRaises(ValueError):
            self.service.apply(plan)

    def test_restore_conflict_preserves_new_settings(self):
        backup = self.service.apply(self.service.preview("resolution"))
        current = self.read()
        current["Graphics"]["resolution"] = "1920x1200"
        self.write(current)
        with self.assertRaises(ValueError):
            self.service.restore(backup)
        self.assertEqual(self.read(), current)

    def test_invalid_settings_and_mode(self):
        with self.assertRaises(ValueError):
            self.service.preview("bad")
        self.write({"Graphics": []})
        with self.assertRaises(ValueError):
            self.service.preview("resolution")

    def test_backup_integrity(self):
        backup = self.service.apply(self.service.preview("resolution"))
        record = json.loads(backup.read_text(encoding="utf-8"))
        record["original"]["Graphics"]["resolution"] = "1x1"
        backup.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.restore(backup)


class QuickUiTests(unittest.TestCase):
    def test_apply_and_restore_from_buttons(self):
        from PySide6.QtWidgets import QApplication
        from victoria3_quick import Victoria3Quick
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = {"Graphics": {"resolution": "3200x2000"}}
            (root / "pdx_settings.json").write_text(json.dumps(original), encoding="utf-8")
            page = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            page.scan()
            self.assertTrue(page.apply_button.isEnabled())
            page.apply_button.click()
            self.assertIn("设置已写入", page.status.text())
            self.assertFalse(page.apply_button.isEnabled())
            self.assertEqual(page.reports.list()[0]["phase"], "applied")
            self.assertEqual(page.report_view.report["changes"][0]["after"], "2560x1600")
            self.assertTrue(page.restore_latest_button.isEnabled())
            self.assertIn('3200x2000', page.original_note.text())
            page.restore_latest_button.click()
            self.assertIn("已恢复", page.status.text())
            self.assertEqual(page.reports.list()[0]["phase"], "restored")
            self.assertFalse(page.restore_latest_button.isEnabled())
            self.assertEqual(json.loads((root / "pdx_settings.json").read_text(encoding="utf-8")), original)
            page.close()
