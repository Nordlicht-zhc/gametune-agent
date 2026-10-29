from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from agent.controller import GameTuneController
from app import ExperimentPage, STYLESHEET, TaskState
from models.schemas import BenchmarkRecord, OptimizationTask
from tests.test_planning_feedback import RecordingProvider


class PlanningFeedbackUITests(unittest.TestCase):
    def setUp(self):
        self.application = QApplication.instance() or QApplication([])
        self.old_style = self.application.styleSheet()
        self.application.setStyleSheet(STYLESHEET)
        self.folder = tempfile.TemporaryDirectory()
        self.controller = GameTuneController(Path(self.folder.name) / "ui.db")
        self.task = OptimizationTask("回放 UI", "Counter-Strike 2", "CPU", "GPU",
            "2560 × 1440", "300 Hz", "scene", 1000, 600, fps_cap=0, vsync="disabled")
        self.baseline = BenchmarkRecord(200, 100, 99, self.task.resolution, self.task.test_scene,
            repeats=2, configuration={"fps_cap": "0", "vsync": "disabled", "dynamic_shadows": "all",
                                      "shadow_quality": "高", "msaa_mode": "8x_msaa"})
        evidence, plan = self.controller.create_task(self.task, self.baseline)
        options = self.controller.next_step_candidates(self.task, self.baseline, evidence, 1)
        self.selection = self.controller.choose_next_step(self.task, self.baseline, evidence, options)
        self.page = ExperimentPage(TaskState(has_task=True), self.selection.plan)
        self.page.resize(1280, 940)
        self.page.show()
        QTest.qWait(50)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.application.processEvents()
        self.folder.cleanup()
        self.application.setStyleSheet(self.old_style)

    def test_local_candidate_details_expand_and_collapse_without_changing_plan(self):
        page = self.page
        original = page.plan
        self.assertFalse(page.planning_details.isVisible())
        page.ensureWidgetVisible(page.planning_details_button)
        QTest.mouseClick(page.planning_details_button, Qt.MouseButton.LeftButton)
        self.assertTrue(page.planning_details.isVisible())
        texts = " ".join(item.text() for item in page.planning_details.findChildren(QLabel))
        self.assertIn("已选：", texts)
        self.assertIn("候选：", texts)
        self.assertIn("已记录 0 次实验反馈", texts)
        all_text = " ".join(item.text() for item in page.findChildren(QLabel))
        self.assertIn("本地规则", all_text)
        self.assertNotIn("AI 选择理由", all_text)
        QTest.mouseClick(page.planning_details_button, Qt.MouseButton.LeftButton)
        self.assertFalse(page.planning_details.isVisible())
        self.assertIs(page.plan, original)

    def test_model_details_show_source_and_call_duration(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.model_provider = RecordingProvider()
        evidence, _ = self.controller.plan_after_result(self.task, self.baseline, self.baseline, 1)
        options = self.controller.next_step_candidates(self.task, self.baseline, evidence, 1)
        selection = self.controller.choose_next_step(self.task, self.baseline, evidence, options)
        self.page = ExperimentPage(TaskState(has_task=True), selection.plan)
        self.page.resize(1280, 940)
        self.page.show()
        QTest.qWait(50)
        self.page.ensureWidgetVisible(self.page.planning_details_button)
        QTest.mouseClick(self.page.planning_details_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.page.planning_details.isVisible())
        texts = " ".join(item.text() for item in self.page.findChildren(QLabel))
        self.assertIn("AI 比较候选", texts)
        self.assertIn("模型调用耗时", texts)
        self.assertIn(selection.plan.selection_reason, texts)


if __name__ == "__main__":
    unittest.main()
