from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from app import ExperimentPage, TaskState, MainWindow
from tools.cs2_log import CS2LogTail
from models.schemas import BenchmarkRecord, OptimizationTask
from agent.controller import GameTuneController


class CS2LogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "console.log"
        self.path.write_bytes(b"[VProf] FPS: Avg=100, P1=50\n")
        self.tail = CS2LogTail(self.path)

    def append(self, content):
        with self.path.open("ab") as stream:
            stream.write(content)

    def test_old_results_skipped_partial_lines_and_precision(self):
        self.assertEqual(self.tail.poll(), [])
        self.append(b"[VProf] Summary of 10000 frames.\r\n[VProf] FPS: Avg=542.37, P1=171")
        self.assertEqual(self.tail.poll(), [])
        self.append(b".25\r\n")
        item, = self.tail.poll()
        self.assertEqual((item.avg_fps, item.p1_fps), (542.37, 171.25))
        self.assertIn("10000 frames", item.excerpt)
        self.assertEqual(self.tail.poll(), [])

    def test_equal_results_are_distinct_runs_and_invalid_lines_ignored(self):
        self.append(b"[VProf] FPS: Avg=0, P1=0\n[VProf] FPS: Avg=100, P1=200\n")
        self.assertEqual(self.tail.poll(), [])
        self.append(b"[VProf] FPS: Avg=200, P1=100\n" * 2)
        first, second = self.tail.poll()
        self.assertNotEqual(first.measurement_id, second.measurement_id)

    def test_truncation_and_rotation_are_read(self):
        self.path.write_bytes(b"x\n")
        self.tail.poll()
        self.append(b"[VProf] FPS: Avg=200, P1=100\n")
        self.assertEqual(len(self.tail.poll()), 1)
        self.path.unlink()
        self.path.write_bytes(b"[VProf] FPS: Avg=300, P1=150\n")
        self.assertEqual(self.tail.poll()[0].avg_fps, 300)

    def test_rewrite_to_larger_file_is_not_mistaken_for_append(self):
        self.path.write_bytes(b"[VProf] FPS: Avg=1234.56, P1=789.12\n")
        self.assertEqual(self.tail.poll()[0].avg_fps, 1234.56)

    def test_only_performance_excerpt_is_retained(self):
        self.append(b"player joined secret-server\n[VProf] FPS: Avg=200, P1=100\n")
        self.assertNotIn("secret", self.tail.poll()[0].excerpt)


class CS2LogUITests(unittest.TestCase):
    setUp = CS2LogTests.setUp
    append = CS2LogTests.append
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_poll_fills_values_without_overwriting_pending_and_saves_precision(self):
        page = ExperimentPage(TaskState(has_task=True))
        saved = []
        page.result_saved.connect(saved.append)
        page.log_path.setText(str(self.path))
        page.log_toggle.click()
        self.append(b"[VProf] FPS: Avg=542.37, P1=171.25\n" * 2)
        page._poll_cs2_log()
        self.assertEqual(page.avg.value(), 542)
        self.assertEqual(page.gpu.value(), 0)
        self.assertEqual(len(page._log_queue), 1)
        page._poll_cs2_log()
        self.assertEqual(len(page._log_queue), 1)
        page.save_button.click()
        self.assertEqual(saved[-1]["avg"], 542.37)
        self.assertEqual(saved[-1]["low"], 171.25)
        self.assertEqual(saved[-1]["source"], "cs2_vprof")
        self.assertEqual(len(saved[-1]["runs"]), 1)
        self.assertIsNone(saved[-1]["gpu"])
        page.close()
        page.deleteLater()

    def test_user_numbers_not_replaced_and_manual_edit_clears_log_provenance(self):
        page = ExperimentPage(TaskState(has_task=True))
        page.avg.setValue(233)
        page.low.setValue(140)
        page.log_path.setText(str(self.path))
        page.log_toggle.click()
        self.append(b"[VProf] FPS: Avg=542.37, P1=171.25\n")
        page._poll_cs2_log()
        self.assertEqual(page.avg.value(), 233)
        page.log_next.click()
        self.assertIsNotNone(page._log_measurement)
        page.avg.setValue(600)
        self.assertIsNone(page._log_measurement)
        page.close()
        page.deleteLater()

    def test_measurement_ids_do_not_break_accumulation(self):
        controller = GameTuneController(self.path.with_suffix(".db"))
        task = OptimizationTask("log", "Counter-Strike 2", "CPU", "GPU", "1680 × 1050",
            "300 Hz", "Workshop", 1000, 600, fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
        config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高", "dynamic_shadows": "all"}
        baseline = BenchmarkRecord(200, 100, 99, task.resolution, task.test_scene, repeats=3, configuration=config)
        _, plan = controller.create_task(task, baseline)
        for index in range(3):
            record = BenchmarkRecord(240.3, 130.2, None, task.resolution, task.test_scene,
                source="cs2_vprof", configuration={**config, plan.setting_id: plan.new_value,
                    "measurement_source": "cs2_vprof", "measurement_id": str(index),
                    "log_avg_fps": "240.3", "log_p1_fps": "130.2"},
                runs=[{"avg_fps": 240.3, "low_fps": 130.2}])
            result = controller.evaluate(task, baseline, record, 1)
        self.assertTrue(result.accepted)
        self.assertEqual(controller.best_record(task.task_id).repeats, 3)

    def test_real_save_deduplicates_preserves_source_and_survives_page_rebuild(self):
        qt = self.qt
        controller = GameTuneController(self.path.with_suffix(".db"))
        task = OptimizationTask("log ui", "Counter-Strike 2", "CPU", "GPU", "1680 × 1050",
            "300 Hz", "Workshop", 1000, 600, fps_cap=0, vsync="disabled", shadow_quality="高", dynamic_shadows="all")
        config = {"fps_cap": "0", "vsync": "disabled", "shadow_quality": "高", "dynamic_shadows": "all"}
        baseline = BenchmarkRecord(200, 100, 99, task.resolution, task.test_scene, repeats=3, configuration=config)
        evidence, plan = controller.create_task(task, baseline)
        window = MainWindow(database_path=self.path.with_suffix(".db"), controller=controller)
        window.core_task, window.baseline_record = task, baseline
        window._load_state_from_task(task, baseline)
        window.current_plan, window.current_evidence = plan, evidence
        window._rebuild_task_pages()
        window._start_plan_selection = lambda **kwargs: None
        window._start_explanation = lambda *args: None
        page = window.experiment
        page.actual_setting.setText(plan.new_value)
        page.log_path.setText(str(self.path))
        page.log_toggle.click()
        with patch("app.DATA_DIR", self.path.parent / "data"):
            for count in range(1, 4):
                self.append(b"[VProf] FPS: Avg=240.37, P1=130.25\n")
                page._poll_cs2_log()
                page.save_button.click()
                self.assertEqual(len(controller.experiment_history(task.task_id)), count)
                if count < 3:
                    page.save_button.click()
                    self.assertEqual(len(controller.experiment_history(task.task_id)), count)
            self.assertEqual(window.experiment._log_tail, page._log_tail)
            self.assertTrue(window.experiment._log_timer.isActive())
            self.assertEqual(controller.best_record(task.task_id).avg_fps, 240.37)
            self.assertEqual(controller.best_record(task.task_id).source, "cs2_vprof")
            self.assertEqual(controller.best_record(task.task_id).repeats, 3)
            self.assertEqual(len(list((self.path.parent / "data/captures/cs2").glob("*.txt"))), 3)
        window.close()
        qt.processEvents()
