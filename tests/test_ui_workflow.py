from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from agent.api import APIConfig, OpenAICompatibleProvider
from app import APISettingsPage, ExperimentPage, MainWindow, NewTaskPage, ReportPage, TaskState
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from agent.controller import GameTuneController


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def task() -> OptimizationTask:
    return OptimizationTask(
        name="ui-test",
        game="Counter-Strike 2",
        cpu="CPU",
        gpu="GPU",
        resolution="2560 × 1440",
        refresh_rate="300 Hz",
        test_scene="Workshop",
        target_avg_fps=300,
        target_low_fps=210,
    )


def record(avg: float = 284, low: float = 168) -> BenchmarkRecord:
    return BenchmarkRecord(
        avg,
        low,
        57,
        "2560 × 1440",
        "Workshop",
        repeats=3,
        duration_seconds=60,
        configuration_confirmed=True,
    )


def rollback_task() -> OptimizationTask:
    return replace(task(), max_rounds=1)


class FakeRepository:
    def list_tasks(self):
        return []

    def task_statistics(self, _task_id):
        return {"candidate_count": 0, "experiment_count": 0}


class FakeController:
    def __init__(self):
        self.repository = FakeRepository()
        self.current = None
        self.best = None
        self.supplement_values = None

    def current_record(self, _task_id):
        return self.current

    def best_record(self, _task_id):
        return self.best or self.current

    def evaluate(self, *_args, **_kwargs):
        return Decision(
            AgentAction.RETEST,
            "请在同一配置下复测",
            retry_same_experiment=True,
        )

    def supplement_data(self, current_task, values):
        self.supplement_values = (current_task, values)
        return [Evidence("supplemented", "已补充", "已记录真实设置", 1.0, "supports")], ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "继续验证",
            "分辨率",
            "2560 × 1440",
            "1920 × 1080",
            "测试",
            "假设",
            "方法",
            "标准",
            "恢复",
            1,
            setting_id="resolution",
        )


class UiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = application()

    def test_preview_does_not_create_a_persisted_task(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db")
            self.assertIsNone(window.core_task)
            self.assertEqual(window.controller.repository.list_tasks(), [])
            window.close()

    def test_actual_configuration_is_unchecked_by_default(self):
        page = ExperimentPage(TaskState())
        self.assertFalse(page.configuration_confirmed.isChecked())
        self.assertEqual(page.repeats.value(), 1)
        self.assertTrue(page.save_button.isEnabled())
        page.set_completed()
        self.assertFalse(page.save_button.isEnabled())
        page.deleteLater()

    def test_baseline_and_experiment_copy_explain_adaptive_test_count(self):
        new_task = NewTaskPage(TaskState())
        self.assertTrue(new_task.baseline_stage.isHidden())
        self.assertEqual(len(new_task.baseline_run_inputs), 3)
        self.assertEqual(new_task.baseline_run_inputs[0]["avg_fps"].value(), 0)
        self.assertIn("实际测试", new_task.baseline_confirmed_input.text())
        new_task.deleteLater()

        plan = ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "降低分辨率",
            "分辨率",
            "2560 × 1440",
            "1920 × 1080",
            "原因", "假设", "方法", "标准", "恢复", 1,
            setting_id="resolution",
        )
        page = ExperimentPage(TaskState(), plan)
        guide_text = " ".join(item.text() for item in page.findChildren(QLabel))
        self.assertIn("先测 1 次", guide_text)
        self.assertIn("累计 3 次", guide_text)
        page.deleteLater()

    def test_model_picker_accepts_discovered_and_manual_models(self):
        page = APISettingsPage()
        self.assertTrue(page.model.isEditable())
        self.assertEqual(page.model.count(), 0)
        page._models_succeeded(0, ["gpt-zeta", "gpt-alpha"])
        self.assertEqual([page.model.itemText(i) for i in range(page.model.count())], ["gpt-zeta", "gpt-alpha"])
        self.assertEqual(page.model.currentText(), "gpt-zeta")

        page.model.setCurrentText("custom-model")
        page._models_succeeded(0, ["gpt-alpha"])
        self.assertEqual(page.model.itemText(0), "custom-model")
        self.assertEqual(page.model.currentText(), "custom-model")
        page.deleteLater()

    def test_main_window_loads_saved_api_config(self):
        config = APIConfig("https://example.test/v1", "secret-token", "model-a", 20, "low")
        with tempfile.TemporaryDirectory() as folder, patch("app.load_saved_api_config", return_value=config):
            window = MainWindow(database_path=Path(folder) / "ui.db")
            self.assertIsInstance(window.controller.model_provider, OpenAICompatibleProvider)
            self.assertEqual(window.api_settings.base_url.text(), config.base_url)
            self.assertEqual(window.api_settings.api_key.text(), config.api_key)
            self.assertEqual(window.api_settings.model.currentText(), config.model)
            self.assertEqual(window.api_settings.reasoning.currentData(), config.reasoning_effort)
            window.close()

    def test_internal_graphics_mode_is_shown_as_clear_chinese(self):
        plan = ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "测试独显直连",
            "显卡输出模式",
            "混合输出",
            "独显直连",
            "原因",
            "假设",
            "方法",
            "标准",
            "恢复",
            1,
            setting_id="graphics_mode",
        )
        page = ExperimentPage(TaskState(graphics_mode="hybrid"), plan)
        self.assertEqual(page.actual_setting.text(), "混合输出")
        self.assertEqual(page._supplement_value(), "hybrid")
        page.actual_setting.setText("独显直连")
        self.assertEqual(page._supplement_value(), "dgpu")
        page.deleteLater()

    def test_power_mode_uses_selectable_values_and_stores_domain_value(self):
        plan = ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "开启笔记本性能模式",
            "供电 / 性能模式",
            "接通电源 · 平衡模式",
            "接通电源 · 性能模式",
            "原因", "假设", "方法", "标准", "恢复", 1,
            setting_id="power_mode",
        )
        page = ExperimentPage(TaskState(power_mode="ac_balanced"), plan)
        self.assertFalse(page.actual_setting.isEditable())
        self.assertEqual(page.actual_setting.count(), 4)
        page.actual_setting.setText("接通电源 · 性能模式")
        configuration = page._actual_configuration()
        self.assertEqual(configuration["actual_setting_value"], "接通电源 · 性能模式")
        self.assertEqual(configuration["power_mode"], "ac_performance")
        page.deleteLater()

    def test_generic_optimization_request_has_direction_and_single_setting_fields(self):
        plan = ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "选择通用优化方向并指定一项设置",
            "方向与单项设置",
            "由你填写当前值",
            "由你填写调整值",
            "原因", "假设", "方法", "标准", "不修改设置", 1,
            setting_id="user_adjustment",
        )
        page = ExperimentPage(TaskState(), plan)
        self.assertEqual(
            [page.adjustment_direction.itemText(index) for index in range(page.adjustment_direction.count())],
            ["请选择优化方向", "GPU 画面负载", "CPU 后台负载", "供电与散热", "画面稳定性", "游戏内画面质量"],
        )
        page.adjustment_direction.setCurrentText("GPU 画面负载")
        page.adjustment_setting_name.setText("全局阴影效果")
        page.adjustment_current_value.setText("高")
        page.adjustment_new_value.setText("中")
        self.assertEqual(page._supplement_value(), "GPU 画面负载 | 全局阴影效果 | 高 | 中")
        page.deleteLater()

    def test_cs2_choices_match_installed_simplified_chinese_menu(self):
        page = NewTaskPage(TaskState())
        labels = {item.text() for item in page.findChildren(QLabel)}
        self.assertTrue({"游戏最高FPS", "V-Sync", "全局阴影效果", "动态阴影"}.issubset(labels))
        self.assertEqual(
            [page.display_mode_input.itemText(index) for index in range(page.display_mode_input.count())],
            ["全屏", "全屏窗口模式", "窗口模式"],
        )
        self.assertEqual(
            [page.vsync_input.itemText(index) for index in range(page.vsync_input.count())],
            ["已禁用", "已启用", "未确认"],
        )
        self.assertEqual(
            [page.dynamic_shadows_input.itemText(index) for index in range(page.dynamic_shadows_input.count())],
            ["全部", "仅限日光", "未确认"],
        )
        self.assertEqual(
            [page.advanced_video_inputs["texture_filtering"].itemText(index) for index in range(6)],
            ["双线性", "三线性", "异向 2X", "异向 4X", "异向 8X", "异向 16X"],
        )
        self.assertEqual(
            set(page.advanced_video_inputs),
            {"video_preset", "player_contrast", "nvidia_gsync", "nvidia_reflex", "amd_anti_lag", "msaa_mode",
             "texture_detail", "texture_filtering", "shader_detail", "particle_detail",
             "ambient_occlusion", "hdr", "fsr"},
        )
        page.deleteLater()

    def test_report_identifies_the_model_that_generated_ai_text(self):
        page = ReportPage(
            TaskState(),
            explanation={
                "summary": "这次调整可以保留。",
                "reasoning": ["帧率变化很小。"],
                "next_step": "继续下一项。",
                "warning": "",
                "source": "model",
                "model": "deepseek-flash",
            },
        )
        texts = {item.text() for item in page.findChildren(QLabel)}
        self.assertIn("AI 说明 · deepseek-flash", texts)
        page.deleteLater()

    def test_report_warning_text_keeps_vertical_padding(self):
        page = ReportPage(
            TaskState(),
            explanation={
                "summary": "这次调整可以保留。",
                "reasoning": ["帧率变化很小。"],
                "next_step": "继续下一项。",
                "warning": "当前只有汇总数据，请补充逐次测试记录后再下结论。",
                "source": "local",
            },
        )
        warning = page.findChild(QLabel, "warningBox")
        self.assertIsNotNone(warning)
        self.assertGreaterEqual(warning.minimumHeight(), 30)
        page.deleteLater()

    def test_retest_keeps_the_same_round_and_plan(self):
        controller = FakeController()
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db", controller=controller)
            current_task = task()
            baseline = record()
            controller.current = baseline
            controller.best = baseline
            window.core_task = current_task
            window.baseline_record = baseline
            window.state.round_number = 1
            window.current_plan = ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "降低分辨率",
                "分辨率",
                "2560 × 1440",
                "1920 × 1080",
                "原因",
                "假设",
                "方法",
                "标准",
                "恢复",
                1,
                setting_id="resolution",
            )
            window._start_explanation = Mock()
            window._result_saved({
                "avg": 285,
                "low": 169,
                "gpu": 56,
                "repeats": 3,
                "duration": 60,
                "condition": "与基线一致",
                "configuration_confirmed": True,
                "resolution": "2560 × 1440",
                "configuration": {"resolution": "2560 × 1440"},
            })
            self.assertEqual(window.state.round_number, 1)
            self.assertEqual(window.current_plan.setting_id, "resolution")
            self.assertEqual(window.current_plan.round_number, 1)
            window.close()

    def test_request_data_calls_supplement_data_instead_of_evaluate(self):
        controller = FakeController()
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(database_path=Path(folder) / "ui.db", controller=controller)
            window.core_task = task()
            window.baseline_record = record()
            window.current_plan = ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "确认阴影质量",
                "全局阴影效果",
                "未知",
                "记录当前值",
                "原因",
                "假设",
                "方法",
                "标准",
                "恢复",
                1,
                setting_id="shadow_quality",
            )
            window._rebuild_task_pages = Mock()
            controller.evaluate = Mock(side_effect=AssertionError("REQUEST_DATA must not evaluate a result"))
            window._result_saved({"supplement": True, "supplement_value": "高"})
            self.assertEqual(controller.supplement_values[1], {"shadow_quality": "高"})
            window.close()

    def test_restore_rollback_at_round_cap_disables_submission_and_keeps_best(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workflow.db"
            controller = GameTuneController(path)
            current_task = rollback_task()
            baseline = replace(
                record(200, 100),
                gpu_usage=99,
                configuration={"shadow_quality": "高", "dynamic_shadows": "all", "resolution": current_task.resolution},
            )
            controller.create_task(current_task, baseline, fps_cap_known=True, vsync_known=True)
            candidate = replace(
                record(100, 50),
                configuration={
                    "shadow_quality": "中",
                    "resolution": current_task.resolution,
                    "planned_setting_id": "shadow_quality",
                    "actual_setting_value": "中",
                },
            )
            decision = controller.evaluate(current_task, baseline, candidate, 1)
            self.assertEqual(decision.action, AgentAction.REVERT_CHANGE)

            restored_controller = GameTuneController(path)
            window = MainWindow(database_path=path, controller=restored_controller)
            with patch("app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._restore_task(current_task.task_id)
                self.assertTrue(window.experiment.rollback_button.isVisible())
                window._confirm_pending_rollback()
            self.assertFalse(window.experiment.save_button.isEnabled())
            self.assertEqual(window.state.step, "优化完成")
            self.assertIsNotNone(window.report)
            self.assertIn("1 / 1", " ".join(item.text() for item in window.report.findChildren(QLabel)))
            window.close()


if __name__ == "__main__":
    unittest.main()
