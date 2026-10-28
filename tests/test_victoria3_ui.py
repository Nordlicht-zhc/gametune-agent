import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from victoria3_page import Victoria3Page


class Victoria3UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_baseline_compare_restore_export(self):
        with tempfile.TemporaryDirectory() as folder:
            save = Path(folder) / "start.v3"
            save.write_bytes(b"test fixture only")
            page = Victoria3Page(Path(folder) / "ui.db")
            for key, field in page.fields.items():
                field.setText(str(save) if key == "save" else "fixed")
            page.fill_table([{"game_days": 30, "elapsed_seconds": 100} for _ in range(3)])
            page.set_baseline()
            self.assertIsNotNone(page.baseline)
            page.fields["configuration"].setText("lower resolution")
            page.fields["change"].setText("resolution only; restore original resolution")
            page.fill_table([{"game_days": 30, "elapsed_seconds": 80} for _ in range(3)])
            page.confirmed.setChecked(True)
            page.evaluate()
            self.assertEqual(page.report["result"]["action"], "keep")
            self.assertFalse(page.confirmed.isChecked())
            self.assertEqual(len(page.repository.history()), 2)
            page.restore()
            self.assertEqual(page.fields["configuration"].text(), "fixed")
            export = Path(folder) / "report.json"
            with patch("victoria3_page.QFileDialog.getSaveFileName", return_value=(str(export), "JSON")):
                page.export()
            self.assertIn('"simulation_delta_pct": 25.0', export.read_text(encoding="utf-8"))
            page.close()

    def test_invalid_data_does_not_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            page = Victoria3Page(Path(folder) / "ui.db")
            with patch("victoria3_page.QMessageBox.warning") as warning:
                page.set_baseline()
                self.assertTrue(warning.called)
            self.assertEqual(page.repository.history(), [])
            page.close()
