from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QStackedWidget, QVBoxLayout, QWidget

from app import CurrentPageStack, MainWindow, MetricCard, ReportPage, Sidebar, TaskState, ToastNotice, animate_visibility, fade_stack_to


class AnimationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_rapid_page_switches_leave_live_pages_clear_and_interactive(self):
        stack = QStackedWidget()
        self.addCleanup(stack.deleteLater)
        for _ in range(3):
            stack.addWidget(QWidget())
        stack.resize(600, 400)
        stack.show()
        QTest.qWait(20)
        fade_stack_to(stack, 1, stack, duration=40)
        fade_stack_to(stack, 2, stack, duration=40)
        QTest.qWait(100)
        self.assertEqual(stack.currentIndex(), 2)
        self.assertIsNone(stack.graphicsEffect())
        self.assertIsNone(stack._fade_overlay)
        self.assertIsNone(stack._fade_animation)
        self.assertTrue(stack.currentWidget().isVisible())
        stack.close()

    def test_sidebar_switch_after_nonanimated_sync_keeps_animation_alive(self):
        sidebar = Sidebar(TaskState())
        self.addCleanup(sidebar.deleteLater)
        sidebar.resize(225, 800)
        sidebar.show()
        QTest.qWait(20)
        animation = sidebar._indicator_animation
        sidebar.set_active(1)
        sidebar.set_active(2, animated=False)
        QTest.qWait(30)
        sidebar.set_active(3)
        QTest.qWait(250)
        self.assertIs(sidebar._indicator_animation, animation)
        self.assertEqual(sidebar.group.checkedId(), 3)
        self.assertEqual(sidebar.nav_indicator.y(), sidebar.group.button(3).y())
        sidebar.close()

    def test_sidebar_startup_resize_and_clicks_follow_current_selection(self):
        sidebar = Sidebar(TaskState())
        self.addCleanup(sidebar.deleteLater)
        sidebar.navigate.connect(sidebar.set_active)
        sidebar.resize(225, 800)
        sidebar.show()
        # The deferred initial positioning must not reset a newer selection.
        sidebar.set_active(4)
        QTest.qWait(250)
        self.assertEqual(sidebar.group.checkedId(), 4)
        self.assertEqual(sidebar.nav_indicator.y(), sidebar.group.button(4).y())
        for index in (5, 2, 1, 3, 0):
            sidebar.group.button(index).click()
            QTest.qWait(15)
        sidebar.resize(225, 900)
        QTest.qWait(250)
        self.assertEqual(sidebar.group.checkedId(), 0)
        self.assertEqual(sidebar.nav_indicator.y(), sidebar.group.button(0).y())
        sidebar.group.button(5).click()
        QTest.qWait(250)
        self.assertEqual(sidebar.group.checkedId(), 5)
        self.assertEqual(sidebar.nav_indicator.y(), sidebar.group.button(5).y())
        sidebar.close()

    def test_main_navigation_keeps_page_selection_and_marker_in_sync(self):
        with tempfile.TemporaryDirectory() as temporary, patch("app.provider_from_env", return_value=None), patch("app.load_saved_api_config", return_value=None):
            window = MainWindow(database_path=Path(temporary) / "navigation.sqlite")
            self.addCleanup(window.deleteLater)
            window.show()
            try:
                for index in (1, 4, 2, 5, 0, 3):
                    button = window.sidebar.group.button(index)
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    QTest.qWait(30)
                    self.assertEqual(window.stack.currentIndex(), index)
                    self.assertEqual(window.sidebar.group.checkedId(), index)
                QTest.qWait(250)
                self.assertEqual(window.sidebar.nav_indicator.y(), window.sidebar.group.button(3).y())
                self.assertIsNone(window.stack._fade_overlay)
            finally:
                window.close()

    def test_hidden_parent_still_updates_explicit_child_visibility(self):
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        child = QLineEdit(parent)
        child.show()
        animate_visibility(child, False)
        self.assertTrue(child.isHidden())
        animate_visibility(child, True)
        self.assertFalse(child.isHidden())

    def test_interrupted_expansion_restores_original_height_limits(self):
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        layout = QVBoxLayout(parent)
        child = QLineEdit()
        child.setMinimumHeight(28)
        child.setMaximumHeight(44)
        layout.addWidget(child)
        parent.show()
        QTest.qWait(20)
        animate_visibility(child, False, duration=40)
        QTest.qWait(10)
        animate_visibility(child, True, duration=40)
        QTest.qWait(100)
        self.assertFalse(child.isHidden())
        self.assertEqual((child.minimumHeight(), child.maximumHeight()), (28, 44))
        parent.close()

    def test_form_step_size_does_not_include_the_hidden_large_step(self):
        stack = CurrentPageStack()
        self.addCleanup(stack.deleteLater)
        for height in (700, 160):
            page = QWidget()
            layout = QVBoxLayout(page)
            field = QWidget()
            field.setFixedSize(300, height)
            layout.addWidget(field)
            stack.addWidget(page)
        stack.setCurrentIndex(1)
        self.assertLess(stack.minimumSizeHint().height(), 250)

    def test_toast_replacement_preserves_opacity_and_cancels_old_dismissal(self):
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        parent.resize(700, 400)
        parent.show()
        toast = ToastNotice(parent)
        toast.show_message("First")
        QTest.qWait(200)
        animation = toast._animation
        toast._fade_out()
        QTest.qWait(60)
        opacity = toast._opacity.opacity()
        self.assertGreater(opacity, 0)
        self.assertLess(opacity, 1)
        toast.show_message("Replacement")
        self.assertIs(toast._animation, animation)
        self.assertAlmostEqual(toast._opacity.opacity(), opacity)
        QTest.qWait(250)
        self.assertTrue(toast.isVisible())
        self.assertEqual(toast.message.text(), "Replacement")
        self.assertAlmostEqual(toast._opacity.opacity(), 1)
        toast._fade_out()
        QTest.qWait(250)
        self.assertTrue(toast.isHidden())
        toast.show_message("Again")
        QTest.qWait(200)
        self.assertTrue(toast.isVisible())
        parent.close()

    def test_metric_progress_waits_for_first_show_and_does_not_replay(self):
        card = MetricCard("FPS", "60", "Measured", 70)
        self.addCleanup(card.deleteLater)
        QTest.qWait(400)
        self.assertEqual(card.progress_bar.value(), 0)
        card.show()
        QTest.qWait(400)
        self.assertEqual(card.progress_bar.value(), 70)
        card.hide()
        card.show()
        self.assertEqual(card.progress_bar.value(), 70)
        card.close()

    def test_hidden_report_waits_for_show_and_reveal_is_bounded(self):
        page = ReportPage(TaskState(), explanation={
            "summary": "Saved", "reasoning": ["Comparable"],
            "next_step": "Repeat", "source": "local",
        })
        self.addCleanup(page.deleteLater)
        QTest.qWait(400)
        self.assertIsNone(page._reveal_animation)
        self.assertTrue(all(widget.graphicsEffect().opacity() == 0 for widget in page._reveal_targets))
        page.show()
        self.assertLessEqual(page._reveal_animation.duration(), 320)
        QTest.qWait(400)
        self.assertIsNone(page._reveal_animation)
        page.hide()
        page.show()
        self.assertIsNone(page._reveal_animation)
        page.close()

    def test_report_reveal_releases_graphics_effects(self):
        page = ReportPage(TaskState(), explanation={
            "summary": "本轮结果已保存。",
            "reasoning": ["使用相同测试条件比较结果。"],
            "next_step": "继续复测。",
            "source": "local",
        })
        self.addCleanup(page.deleteLater)
        page.resize(1000, 800)
        page.show()
        QTest.qWait(1100)
        self.assertTrue(all(widget.graphicsEffect() is None for widget in page._reveal_targets))
        self.assertIsNone(page._reveal_animation)
        page.close()


if __name__ == "__main__":
    unittest.main()
