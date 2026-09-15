from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QAbstractSpinBox, QDoubleSpinBox,
    QCheckBox,
)

from agent.controller import GameTuneController
from agent.api import (
    APIConfig, ModelAPIError, ModelMessage, ModelRequest, ModelResponse,
    OpenAICompatibleProvider, provider_from_env,
)
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from tools.import_benchmark import import_benchmark_csv
from tools.analyze_evidence import analyze_evidence, dominant_hypothesis
from tools.calculate_metrics import normalized_score
from tools.resolution import RESOLUTION_PRESETS, aspect_ratio_name, normalize_resolution
from tools.hardware_catalog import HardwareCatalog, HardwareEntry
from agent.planner import plan_next_experiment


APP_DIR = Path(__file__).resolve().parent
HARDWARE_CATALOG = HardwareCatalog(APP_DIR / "knowledge" / "hardware.json")


@dataclass
class TaskState:
    name: str = "CS2 竞技帧率优化"
    game: str = "Counter-Strike 2"
    cpu: str = "Intel Core Ultra 9 275HX"
    gpu: str = "NVIDIA RTX 5070 Laptop"
    resolution: str = "2560 × 1440"
    refresh_rate: str = "300 Hz"
    test_scene: str = "CS2 Workshop Benchmark"
    target_avg: int = 300
    target_low: int = 210
    avg_fps: int = 284
    low_fps: int = 168
    gpu_usage: int = 57
    has_task: bool = True
    round_number: int = 1
    step: str = "分析完成"
    mode: str = "competitive"
    fps_cap_checked: bool = True
    vsync_checked: bool = True
    is_laptop: bool = True
    gpu_power_w: int | None = 100
    graphics_mode: str = "hybrid"
    power_mode: str = "ac_performance"
    cooling_mode: str = "performance"
    # Baseline values are entered by the user.  Keep the execution marker
    # separate from candidate configuration confirmation so a typed summary
    # cannot be presented as instrument-backed evidence by default.
    baseline_repeats: int = 1
    baseline_duration_seconds: int = 60
    baseline_confirmed: bool = False


STYLESHEET = """
* { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #202422; }
QMainWindow, QWidget#root, QStackedWidget, QScrollArea,
QScrollArea > QWidget > QWidget { background: #f5f6f2; }
QScrollArea { border: none; }
QFrame#sidebar { background: #202421; border: none; }
QLabel#brandMark { background: #d7ff62; color: #172018; border-radius: 10px; font-size: 18px; font-weight: 800; }
QLabel#brandName { color: #f6f7f3; font-size: 17px; font-weight: 800; }
QLabel#brandSub, QLabel#sidebarLabel { color: #909791; font-size: 10px; font-weight: 700; }
QPushButton#navButton { color: #c8cdc8; background: transparent; border: none; border-radius: 9px; text-align: left; padding: 10px 13px; font-size: 13px; }
QPushButton#navButton:hover { background: #2b302c; color: #ffffff; }
QPushButton#navButton:checked { color: #ffffff; background: #343a35; font-weight: 700; border-left: 3px solid #d7ff62; }
QFrame#sideTask { background: #292e2a; border: 1px solid #3b413c; border-radius: 11px; }
QLabel#sideTaskTitle { color: #f1f3ef; font-weight: 700; font-size: 12px; }
QLabel#sideTaskText { color: #959c96; font-size: 10px; }
QLabel#eyebrow { color: #737a75; font-size: 10px; font-weight: 800; }
QLabel#pageTitle { color: #171a18; font-size: 28px; font-weight: 800; }
QLabel#subtitle { color: #6d746f; font-size: 12px; }
QLabel#sectionTitle { color: #202422; font-size: 17px; font-weight: 800; }
QLabel#sectionMeta { color: #858b87; font-size: 10px; }
QLabel#statusPill { color: #36501d; background: #e9f7c4; border-radius: 13px; padding: 6px 12px; font-size: 10px; font-weight: 800; }
QFrame#card, QFrame#metricCard, QFrame#formCard { background: #ffffff; border: 1px solid #e2e5e1; border-radius: 14px; }
QLabel#cardKicker { color: #777e79; font-size: 10px; font-weight: 800; }
QLabel#metricValue { color: #181b19; font-size: 27px; font-weight: 800; }
QLabel#metricNote { color: #777e79; font-size: 10px; }
QProgressBar { background: #edf0eb; border: none; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #b6eb32; border-radius: 3px; }
QFrame#heroCard { background: #234f3d; border: none; border-radius: 16px; }
QFrame#heroCard QLabel#cardKicker { color: #b8c9bf; }
QLabel#heroTitle { color: #ffffff; font-size: 20px; font-weight: 800; }
QLabel#heroBody { color: #c9d5cd; font-size: 12px; }
QLabel#confidence { color: #d7ff62; background: #315d4a; border: 1px solid #47705f; border-radius: 8px; padding: 7px 10px; font-size: 11px; font-weight: 700; }
QLabel#tagGreen, QLabel#tagOrange, QLabel#tagNeutral { border-radius: 9px; padding: 4px 8px; font-size: 10px; font-weight: 700; }
QLabel#tagGreen { color: #426220; background: #e9f4d2; }
QLabel#tagOrange { color: #95501f; background: #ffefe2; }
QLabel#tagNeutral { color: #565d58; background: #eff1ed; }
QLabel#actionNumber { color: #d7ff62; background: #222723; border-radius: 7px; padding: 5px 7px; font-size: 10px; font-weight: 800; }
QLabel#actionTitle { font-size: 15px; font-weight: 800; }
QLabel#settingOld, QLabel#settingNew { border-radius: 8px; padding: 7px 10px; font-size: 11px; font-weight: 700; }
QLabel#settingOld { color: #4d544f; background: #f0f2ee; }
QLabel#settingNew { color: #304719; background: #e9f7c4; }
QLabel#ruleName { color: #222623; font-size: 11px; font-weight: 800; }
QLabel#ruleText { color: #686f6a; font-size: 11px; }
QFrame#ruleLine { background: #eceeeb; max-height: 1px; }
QPushButton#primaryButton { color: #ffffff; background: #222723; border: none; border-radius: 9px; padding: 9px 15px; font-weight: 700; }
QPushButton#primaryButton:hover { background: #343a35; }
QPushButton#secondaryButton { color: #333834; background: #ffffff; border: 1px solid #d9ddd8; border-radius: 9px; padding: 8px 14px; font-weight: 700; }
QPushButton#secondaryButton:hover { background: #f1f3ef; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    background-color: #ffffff;
    color: #202422;
    border: 1px solid #dce0db;
    border-radius: 8px;
    padding: 7px 9px;
    selection-background-color: #badf54;
    selection-color: #172018;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus { border: 1px solid #8caf38; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QTextEdit:disabled,
QLineEdit[readOnly="true"], QTextEdit[readOnly="true"] {
    background-color: #ffffff;
    color: #737a75;
    border-color: #e0e3df;
}
QComboBox QAbstractItemView { background: #ffffff; color: #202422; border: 1px solid #cfd5cf; outline: none; selection-background-color: #b8e64a; selection-color: #172018; }
QComboBox QAbstractItemView::item { min-height: 30px; padding: 4px 9px; }
QMessageBox { background-color: #ffffff; }
QMessageBox QLabel { background: transparent; color: #202422; min-width: 300px; }
QMessageBox QPushButton {
    min-width: 88px;
    min-height: 30px;
    padding: 4px 14px;
    color: #202422;
    background-color: #ffffff;
    border: 1px solid #cfd5cf;
    border-radius: 7px;
}
QMessageBox QPushButton:hover { background-color: #f0f3ed; border-color: #9ca69d; }
QMessageBox QPushButton:default { color: #ffffff; background-color: #202421; border-color: #202421; }
QToolTip { color: #202422; background-color: #ffffff; border: 1px solid #cfd5cf; padding: 5px; }
QFormLayout QLabel { color: #505752; font-size: 11px; font-weight: 600; }
QTableWidget { background: #ffffff; alternate-background-color: #f8f9f6; border: 1px solid #e2e5e1; border-radius: 12px; gridline-color: #edf0ec; selection-background-color: #e9f7c4; }
QHeaderView::section { background: #eef1ec; color: #626964; border: none; border-bottom: 1px solid #dfe3de; padding: 9px; font-size: 10px; font-weight: 800; }
QStatusBar { background: #f5f6f2; color: #7b817d; border-top: 1px solid #e4e6e2; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 3px; }
QScrollBar::handle:vertical { background: #cdd2cc; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def set_margins(layout, left=0, top=0, right=0, bottom=0, spacing=0):
    layout.setContentsMargins(left, top, right, bottom)
    layout.setSpacing(spacing)
    return layout


def label(text: str, object_name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("ruleLine")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    return line


class ModelWorker(QObject):
    """Run a model-provider call away from Qt's GUI thread.

    The worker deliberately accepts a callable instead of knowing about the
    controller.  That keeps the UI fallback path identical to the online path
    and makes stale-response tests deterministic with a small fake callable.
    """

    finished = Signal(object)
    failed = Signal(object)
    finished_with_id = Signal(int, object)
    failed_with_id = Signal(int, object)

    def __init__(self, operation, job_id: int = 0):
        super().__init__()
        self.operation = operation
        self.job_id = job_id

    @Slot()
    def run(self):
        try:
            result = self.operation()
            self.finished.emit(result)
            self.finished_with_id.emit(self.job_id, result)
        except Exception as error:  # provider errors are sanitized by agent.api
            self.failed.emit(error)
            self.failed_with_id.emit(self.job_id, error)


class SectionHeader(QWidget):
    def __init__(self, title: str, meta: str = ""):
        super().__init__()
        row = set_margins(QHBoxLayout(self), spacing=12)
        row.addWidget(label(title, "sectionTitle"))
        row.addStretch()
        row.addWidget(label(meta, "sectionMeta"))


class PageHeader(QWidget):
    def __init__(self, eyebrow: str, title: str, subtitle: str, status: str = ""):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = set_margins(QHBoxLayout(self), spacing=20)
        text_area = QWidget()
        text_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        text_col = set_margins(QVBoxLayout(text_area), spacing=4)
        text_col.addWidget(label(eyebrow.upper(), "eyebrow"))
        text_col.addWidget(label(title, "pageTitle"))
        sub = label(subtitle, "subtitle", True)
        sub.setMaximumWidth(760)
        text_col.addWidget(sub)
        row.addWidget(text_area, 1)
        if status:
            pill = label(f"●  {status}", "statusPill")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str, note: str, progress: int):
        super().__init__()
        self.setObjectName("metricCard")
        self.setMinimumHeight(125)
        layout = set_margins(QVBoxLayout(self), 15, 13, 15, 13, 7)
        layout.addWidget(label(title.upper(), "cardKicker"))
        layout.addWidget(label(value, "metricValue"))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(max(0, min(progress, 100)))
        bar.setTextVisible(False)
        layout.addWidget(bar)
        layout.addWidget(label(note, "metricNote"))


class RuleRow(QWidget):
    def __init__(self, name: str, value: str, name_width: int = 82):
        super().__init__()
        row = set_margins(QHBoxLayout(self), 0, 4, 0, 4, 9)
        name_label = label(name, "ruleName")
        name_label.setFixedWidth(name_width)
        row.addWidget(name_label)
        row.addWidget(label(value, "ruleText", True), 1)


class DashboardPage(QScrollArea):
    experiment_requested = Signal()

    def __init__(self, state: TaskState, evidence: list[Evidence] | None = None, plan: ExperimentPlan | None = None):
        super().__init__()
        self.state = state
        self.evidence = evidence or []
        self.plan = plan
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("优化工作台", "让每一次调校都有证据", "GameTune 根据相同条件下的基准测试逐轮提出建议、验证收益，并保留效果最好的配置。", "智能体就绪"))
        root.addWidget(SectionHeader("当前表现", "基线 · 3 次测试中位数"))
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        cards = [
            MetricCard("平均帧率", f"{state.avg_fps} FPS", f"目标 {state.target_avg} FPS", round(state.avg_fps / state.target_avg * 100)),
            MetricCard("1% LOW", f"{state.low_fps} FPS", "优先改善项", round(state.low_fps / state.target_low * 100)),
            MetricCard("GPU 占用", f"{state.gpu_usage}%", "存在 CPU 侧线索", state.gpu_usage),
            MetricCard("性能得分", f"{self._score():.0f} / 100", f"{self._mode_name()}权重", round(self._score())),
        ]
        for index, card in enumerate(cards):
            metrics.addWidget(card, 0, index)
        root.addLayout(metrics)
        root.addWidget(SectionHeader("本轮判断", "只基于当前已记录证据"))
        diagnosis = QHBoxLayout()
        diagnosis.setSpacing(14)
        diagnosis.addWidget(self._hero(), 3)
        diagnosis.addWidget(self._timeline(), 2)
        root.addLayout(diagnosis)
        root.addWidget(SectionHeader("建议的下一步", "本轮只改变一个主要变量"))
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(self._action_card(), 3)
        bottom.addWidget(self._environment_card(), 2)
        root.addLayout(bottom)
        root.addStretch()

    def _hero(self) -> QFrame:
        card = QFrame()
        card.setObjectName("heroCard")
        card.setMinimumHeight(220)
        layout = set_margins(QVBoxLayout(card), 22, 18, 22, 18, 9)
        layout.addWidget(label("最可能的瓶颈", "cardKicker"))
        hypothesis, confidence = dominant_hypothesis(self.evidence)
        names = {"cpu": "偏向 CPU 侧限制", "gpu": "偏向 GPU 侧限制", "frametime": "帧时间稳定性不足", "platform": "笔记本平台设置可能限速", "insufficient": "证据仍不充分"}
        layout.addWidget(label(names.get(hypothesis, "需要补充证据"), "heroTitle", True))
        body_text = "；".join(item.detail for item in self.evidence[:3]) or "当前为示例任务，创建或更新任务后会显示实际分析证据。"
        layout.addWidget(label(body_text, "heroBody", True))
        tags = QHBoxLayout()
        tags.setSpacing(7)
        for item in self.evidence[:3]:
            tags.addWidget(label(item.title, "tagOrange" if item.supports == "insufficient" else "tagGreen"))
        tags.addStretch()
        layout.addLayout(tags)
        layout.addStretch()
        confidence = label(f"判断置信度　{'中等' if confidence < .75 else '较高'} · {confidence:.0%}", "confidence")
        confidence.setFixedWidth(190)
        layout.addWidget(confidence)
        return card

    def _timeline(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = set_margins(QVBoxLayout(card), 18, 16, 18, 16, 8)
        layout.addWidget(label("优化进度", "cardKicker"))
        hypothesis, _ = dominant_hypothesis(self.evidence)
        hypothesis_name = {"cpu": "CPU 侧限制", "gpu": "GPU 侧限制", "frametime": "帧时间稳定性", "platform": "笔记本平台设置", "insufficient": "待补充证据"}.get(hypothesis, "待补充证据")
        active_round = self.plan.round_number if self.plan else 0
        items = [
            ("✓", "任务设定", "硬件、目标与约束已记录", True),
            ("✓", "基线测试", "数据通过完整性检查", True),
            (str(active_round or 3), "证据分析", f"当前假设：{hypothesis_name}", bool(self.evidence)),
            (str((active_round or 0) + 1), "复测评估", "等待执行建议并导入结果", bool(self.plan)),
            ("5", "完成报告", "输出最佳已验证配置", self.state.step == "优化完成"),
        ]
        for mark, title, desc, active in items:
            row = QHBoxLayout()
            badge = label(mark, "tagGreen" if active else "tagNeutral")
            badge.setFixedWidth(27)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(label(title, "ruleName"))
            text.addWidget(label(desc, "metricNote", True))
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            row.addLayout(text, 1)
            layout.addLayout(row)
        layout.addStretch()
        return card

    def _action_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = set_margins(QVBoxLayout(card), 20, 17, 20, 17, 8)
        plan = self.plan
        layout.addWidget(label(f"诊断实验 {plan.round_number:02d}" if plan else "等待任务分析", "cardKicker"))
        title_row = QHBoxLayout()
        title_row.addWidget(label("01", "actionNumber"))
        title_row.addWidget(label(plan.title if plan else "创建任务后生成下一步", "actionTitle"))
        title_row.addStretch()
        layout.addLayout(title_row)
        settings = QHBoxLayout()
        settings.addWidget(label(plan.old_value if plan else "当前值", "settingOld"))
        settings.addWidget(label("→", "sectionMeta"))
        settings.addWidget(label(plan.new_value if plan else "待生成", "settingNew"))
        settings.addStretch()
        layout.addLayout(settings)
        rows = [("验证假设", plan.hypothesis), ("测试方法", plan.method), ("有效标准", plan.success_criteria)] if plan else [("说明", "尚未生成实验计划。")]
        for name, text in rows:
            layout.addWidget(hline())
            layout.addWidget(RuleRow(name, text))
        buttons = QHBoxLayout()
        primary = QPushButton("我已完成，录入结果")
        primary.setObjectName("primaryButton")
        primary.clicked.connect(self.experiment_requested)
        secondary = QPushButton("查看完整步骤")
        secondary.setObjectName("secondaryButton")
        secondary.clicked.connect(self._show_steps)
        buttons.addWidget(primary)
        buttons.addWidget(secondary)
        buttons.addStretch()
        layout.addLayout(buttons)
        return card

    def _environment_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = set_margins(QVBoxLayout(card), 18, 16, 18, 16, 4)
        layout.addWidget(label("任务环境", "cardKicker"))
        rows = [("处理器", self.state.cpu), ("显卡", self.state.gpu), ("分辨率", self.state.resolution), ("优化模式", self._mode_name()), ("测试场景", self.state.test_scene)]
        if self.state.is_laptop:
            rows.extend([
                ("GPU 功耗", f"{self.state.gpu_power_w} W" if self.state.gpu_power_w else "未知"),
                ("输出模式", {"dgpu": "独显直连", "hybrid": "混合输出", "advanced_optimus": "自动切换", "unknown": "不确定"}.get(self.state.graphics_mode, "不确定")),
            ])
        for name, value in rows:
            layout.addWidget(RuleRow(name, value, 68))
            layout.addWidget(hline())
        layout.addStretch()
        return card

    def _show_steps(self):
        if not self.plan:
            QMessageBox.information(self, "诊断实验步骤", "请先创建任务并完成基线分析。")
            return
        QMessageBox.information(self, "诊断实验步骤", f"调整：{self.plan.setting}，{self.plan.old_value} → {self.plan.new_value}\n\n{self.plan.method}\n\n恢复方式：{self.plan.rollback}")

    def _score(self) -> float:
        return normalized_score(self.state.avg_fps, self.state.low_fps, self.state.target_avg, self.state.target_low, self.state.mode)

    def _mode_name(self) -> str:
        return {"competitive": "竞技模式", "balanced": "平衡模式", "visual": "视觉模式"}.get(self.state.mode, "竞技模式")


class NewTaskPage(QScrollArea):
    task_created = Signal(object)

    def __init__(self, state: TaskState):
        super().__init__()
        self.state = state
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        root.addWidget(PageHeader("任务设置", "创建优化任务", "先固定目标、画质底线与测试条件。信息越明确，后续实验越容易得出可靠结论。"))
        columns = QHBoxLayout()
        columns.setSpacing(15)
        basic_card, basic_form = self._form_card("基本信息")
        goal_card, goal_form = self._form_card("优化目标与基线")
        columns.addWidget(basic_card, 1)
        columns.addWidget(goal_card, 1)
        root.addLayout(columns)
        self.name_input = QLineEdit(state.name)
        self.game_input = QComboBox()
        # Keep unsupported titles out of the selectable task domain until a
        # corresponding rule set and validator are shipped.
        self.game_input.addItem("Counter-Strike 2")
        cpu_field, self.cpu_input, self.custom_cpu_input = self._hardware_field("cpu", state.cpu, state.is_laptop)
        gpu_field, self.gpu_input, self.custom_gpu_input = self._hardware_field("gpu", state.gpu, state.is_laptop)
        self.resolution_input = QComboBox()
        for preset in RESOLUTION_PRESETS:
            self.resolution_input.addItem(f"{aspect_ratio_name(preset)}  ·  {preset}", preset)
        self.resolution_input.addItem("自定义分辨率…", None)
        self.resolution_input.setToolTip("选择常用 16:9、16:10、4:3 分辨率，或选择最后一项自行输入")
        self.custom_resolution_input = QLineEdit()
        self.custom_resolution_input.setPlaceholderText("输入宽 × 高，例如 1728 × 1080")
        self.custom_resolution_input.setVisible(False)
        resolution_field = QWidget()
        resolution_layout = set_margins(QVBoxLayout(resolution_field), spacing=6)
        resolution_layout.addWidget(self.resolution_input)
        resolution_layout.addWidget(self.custom_resolution_input)
        if state.resolution in RESOLUTION_PRESETS:
            self.resolution_input.setCurrentIndex(self.resolution_input.findData(state.resolution))
        else:
            self.resolution_input.setCurrentIndex(self.resolution_input.count() - 1)
            self.custom_resolution_input.setText(state.resolution)
            self.custom_resolution_input.setVisible(True)
        self.resolution_input.currentIndexChanged.connect(self._resolution_mode_changed)
        self.refresh_input = QComboBox()
        self.refresh_input.addItems(["300 Hz", "240 Hz", "165 Hz", "144 Hz"])
        self.scene_input = QLineEdit(state.test_scene)
        self.cap_input = QComboBox()
        self.cap_input.addItems(["已检查并关闭", "尚未确认"])
        self.vsync_input = QComboBox()
        self.vsync_input.addItems(["已检查并关闭", "尚未确认"])
        for name, widget in [("任务名称", self.name_input), ("游戏", self.game_input), ("处理器", cpu_field), ("显卡", gpu_field), ("测试分辨率", resolution_field), ("显示器刷新率", self.refresh_input), ("测试场景", self.scene_input), ("帧率上限", self.cap_input), ("垂直同步", self.vsync_input)]:
            basic_form.addRow(name, widget)
        self.target_avg = self._spin(30, 1000, state.target_avg, 10)
        self.target_low = self._spin(30, 1000, state.target_low, 10)
        self.mode_input = QComboBox()
        self.mode_input.addItems(["竞技模式 · 流畅优先", "平衡模式", "视觉模式 · 画质优先"])
        self.avg_input = self._spin(1, 1000, state.avg_fps)
        self.low_input = self._spin(1, 1000, state.low_fps)
        self.gpu_usage_input = self._spin(0, 100, state.gpu_usage)
        self.baseline_repeats_input = self._spin(1, 20, state.baseline_repeats)
        self.baseline_duration_input = self._spin(10, 3600, state.baseline_duration_seconds)
        self.baseline_confirmed_input = QCheckBox("已按固定场景完成基线采集并核对条件")
        self.baseline_confirmed_input.setChecked(state.baseline_confirmed)
        for name, widget in [("目标平均 FPS", self.target_avg), ("目标 1% Low", self.target_low), ("优化偏好", self.mode_input), ("基线平均 FPS", self.avg_input), ("基线 1% Low", self.low_input), ("GPU 平均占用率", self.gpu_usage_input), ("基线重复次数", self.baseline_repeats_input), ("基线单次时长（秒）", self.baseline_duration_input), ("基线证据", self.baseline_confirmed_input)]:
            goal_form.addRow(name, widget)
        self.laptop_card, laptop_form = self._form_card("笔记本补充信息")
        self.gpu_power_input = self._spin(0, 250, state.gpu_power_w or 0, 5)
        self.gpu_power_input.setSpecialValueText("未知")
        self.graphics_mode_input = QComboBox()
        self.graphics_mode_input.addItem("独显直连 / dGPU only", "dgpu")
        self.graphics_mode_input.addItem("混合输出 / Optimus", "hybrid")
        self.graphics_mode_input.addItem("Advanced Optimus / 自动切换", "advanced_optimus")
        self.graphics_mode_input.addItem("不确定", "unknown")
        self.power_mode_input = QComboBox()
        self.power_mode_input.addItem("接通电源 · 性能模式", "ac_performance")
        self.power_mode_input.addItem("接通电源 · 平衡模式", "ac_balanced")
        self.power_mode_input.addItem("电池供电", "battery")
        self.power_mode_input.addItem("不确定", "unknown")
        self.cooling_mode_input = QComboBox()
        self.cooling_mode_input.addItem("性能 / 狂暴模式", "performance")
        self.cooling_mode_input.addItem("平衡模式", "balanced")
        self.cooling_mode_input.addItem("安静模式", "quiet")
        self.cooling_mode_input.addItem("不确定", "unknown")
        for combo, value in ((self.graphics_mode_input, state.graphics_mode), (self.power_mode_input, state.power_mode), (self.cooling_mode_input, state.cooling_mode)):
            combo.setCurrentIndex(max(0, combo.findData(value)))
        laptop_form.addRow("持续 GPU 功耗（W）", self.gpu_power_input)
        laptop_form.addRow("显卡输出模式", self.graphics_mode_input)
        laptop_form.addRow("供电 / 性能模式", self.power_mode_input)
        laptop_form.addRow("散热模式", self.cooling_mode_input)
        root.addWidget(self.laptop_card)
        self._update_laptop_fields()
        root.addWidget(label("硬件型号用于区分具体平台，性能判断以当前设备的实测数据为准，不使用固定等级。分辨率支持 16:9、16:10、4:3 预设和自定义输入。", "metricNote", True))
        buttons = QHBoxLayout()
        buttons.addStretch()
        submit = QPushButton("创建并开始分析")
        submit.setObjectName("primaryButton")
        submit.setMinimumWidth(190)
        submit.clicked.connect(self._submit)
        buttons.addWidget(submit)
        root.addLayout(buttons)
        root.addStretch()

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSingleStep(step)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        return spin

    @staticmethod
    def _form_card(title: str):
        card = QFrame()
        card.setObjectName("formCard")
        outer = set_margins(QVBoxLayout(card), 20, 18, 20, 20, 13)
        outer.addWidget(label(title, "sectionTitle"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(form)
        outer.addStretch()
        return card, form

    def _resolution_mode_changed(self, index: int):
        custom = index == self.resolution_input.count() - 1
        self.custom_resolution_input.setVisible(custom)
        if custom:
            self.custom_resolution_input.setFocus()

    def _hardware_field(self, kind: str, current: str, current_is_laptop: bool):
        combo = QComboBox()
        entries = HARDWARE_CATALOG.entries(kind)
        for entry in entries:
            combo.addItem(entry.display_name, entry)
        combo.addItem("自定义桌面型号…", "custom_desktop")
        combo.addItem("自定义 Laptop 型号…", "custom_laptop")
        custom_input = QLineEdit()
        custom_input.setPlaceholderText("输入完整型号，建议注明 Laptop 或桌面版")
        custom_input.setVisible(False)
        field = QWidget()
        field_layout = set_margins(QVBoxLayout(field), spacing=6)
        field_layout.addWidget(combo)
        field_layout.addWidget(custom_input)
        matched = HARDWARE_CATALOG.find(kind, current)
        if matched:
            index = next(index for index in range(combo.count() - 1) if combo.itemData(index).name == matched.name)
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(combo.count() - (1 if current_is_laptop else 2))
            custom_input.setText(current)
            custom_input.setVisible(True)
        combo.currentIndexChanged.connect(lambda index: self._hardware_mode_changed(combo, custom_input, index))
        return field, combo, custom_input

    def _hardware_mode_changed(self, combo: QComboBox, custom_input: QLineEdit, index: int):
        custom = isinstance(combo.itemData(index), str) and combo.itemData(index).startswith("custom_")
        custom_input.setVisible(custom)
        if custom:
            custom_input.setFocus()
        if hasattr(self, "laptop_card"):
            self._update_laptop_fields()

    def _update_laptop_fields(self):
        entries = (self.cpu_input.currentData(), self.gpu_input.currentData())
        is_laptop = any(
            (isinstance(entry, HardwareEntry) and entry.segment == "Laptop") or entry == "custom_laptop"
            for entry in entries
        )
        self.laptop_card.setVisible(is_laptop)

    def _submit(self):
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "信息不完整", "请填写任务名称。")
            return
        cpu_data = self.cpu_input.currentData()
        gpu_data = self.gpu_input.currentData()
        cpu_entry = cpu_data if isinstance(cpu_data, HardwareEntry) else None
        gpu_entry = gpu_data if isinstance(gpu_data, HardwareEntry) else None
        cpu_name = cpu_entry.name if cpu_entry else self.custom_cpu_input.text().strip()
        gpu_name = gpu_entry.name if gpu_entry else self.custom_gpu_input.text().strip()
        if not cpu_name or not gpu_name:
            QMessageBox.warning(self, "硬件型号不完整", "请选择 CPU 和 GPU，或在自定义输入框中填写完整型号。")
            return
        self.state.name = self.name_input.text().strip()
        self.state.game = self.game_input.currentText()
        self.state.cpu = cpu_name
        self.state.gpu = gpu_name
        self.state.is_laptop = any(
            (isinstance(entry, HardwareEntry) and entry.segment == "Laptop") or entry == "custom_laptop"
            for entry in (cpu_data, gpu_data)
        )
        if self.state.is_laptop:
            self.state.gpu_power_w = self.gpu_power_input.value() or None
            self.state.graphics_mode = str(self.graphics_mode_input.currentData())
            self.state.power_mode = str(self.power_mode_input.currentData())
            self.state.cooling_mode = str(self.cooling_mode_input.currentData())
        else:
            self.state.gpu_power_w = None
            self.state.graphics_mode = "unknown"
            self.state.power_mode = "unknown"
            self.state.cooling_mode = "unknown"
        try:
            selected_resolution = (
                self.custom_resolution_input.text()
                if self.resolution_input.currentIndex() == self.resolution_input.count() - 1
                else str(self.resolution_input.currentData())
            )
            self.state.resolution = normalize_resolution(selected_resolution)
        except ValueError as error:
            QMessageBox.warning(self, "分辨率无效", str(error))
            return
        self.state.refresh_rate = self.refresh_input.currentText()
        self.state.test_scene = self.scene_input.text().strip()
        self.state.target_avg = self.target_avg.value()
        self.state.target_low = self.target_low.value()
        self.state.avg_fps = self.avg_input.value()
        self.state.low_fps = self.low_input.value()
        self.state.gpu_usage = self.gpu_usage_input.value()
        self.state.baseline_repeats = self.baseline_repeats_input.value()
        self.state.baseline_duration_seconds = self.baseline_duration_input.value()
        self.state.baseline_confirmed = self.baseline_confirmed_input.isChecked()
        if self.state.baseline_repeats < 3 or not self.state.baseline_confirmed:
            QMessageBox.warning(
                self,
                "基线证据不足",
                "请先完成至少 3 次相同条件的基线测试，并勾选“已按固定场景完成基线采集并核对条件”。",
            )
            return
        self.state.mode = ("competitive", "balanced", "visual")[self.mode_input.currentIndex()]
        self.state.fps_cap_checked = self.cap_input.currentIndex() == 0
        self.state.vsync_checked = self.vsync_input.currentIndex() == 0
        self.state.has_task = True
        self.state.step = "分析完成"
        self.task_created.emit(self.state)


class ExperimentPage(QScrollArea):
    result_saved = Signal(dict)
    rollback_requested = Signal()

    def __init__(self, state: TaskState, plan: ExperimentPlan | None = None):
        super().__init__()
        self.state = state
        self.plan = plan
        self.completed = False
        self._csv_metrics: dict | None = None
        self._csv_values: tuple[int, int, int, int] | None = None
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        round_number = plan.round_number if plan else state.round_number
        root.addWidget(PageHeader("实验工作台", f"记录第 {round_number} 轮复测", "只有测试条件一致，前后数据才具有可比性。导入前请核对场景、时长与主要变量。", "等待数据"))
        columns = QHBoxLayout()
        columns.setSpacing(15)
        input_card = QFrame()
        input_card.setObjectName("formCard")
        input_layout = set_margins(QVBoxLayout(input_card), 20, 18, 20, 20, 12)
        input_layout.addWidget(label("复测结果", "sectionTitle"))
        file_row = QHBoxLayout()
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("选择 PresentMon 导出的 CSV 文件（可选）")
        browse = QPushButton("浏览文件")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._choose_file)
        file_row.addWidget(self.file_input, 1)
        file_row.addWidget(browse)
        input_layout.addLayout(file_row)
        form = QFormLayout()
        form.setVerticalSpacing(11)
        self.avg = NewTaskPage._spin(1, 1000, 296)
        self.low = NewTaskPage._spin(1, 1000, 174)
        self.gpu = NewTaskPage._spin(0, 100, 59)
        self.repeats = NewTaskPage._spin(1, 10, 3)
        self.duration = NewTaskPage._spin(10, 3600, 60)
        self.condition = QComboBox()
        self.condition.addItems(["与基线一致", "存在差异，需要说明"])
        self.actual_cpu = QLineEdit(state.cpu)
        self.actual_gpu = QLineEdit(state.gpu)
        self.actual_resolution = QLineEdit(state.resolution)
        self.actual_refresh_rate = QLineEdit(state.refresh_rate)
        self.actual_scene = QLineEdit(state.test_scene)
        self.actual_setting = QLineEdit(self._setting_value_from_state())
        self.actual_setting.setPlaceholderText("填写实际执行值，不要只保留建议值")
        self._actual_fields = {
            "cpu": self.actual_cpu,
            "gpu": self.actual_gpu,
            "resolution": self.actual_resolution,
            "refresh_rate": self.actual_refresh_rate,
            "test_scene": self.actual_scene,
        }
        for field in self._actual_fields.values():
            field.textChanged.connect(self._clear_csv_if_manual_edit)
        self.actual_setting.textChanged.connect(self._clear_csv_if_manual_edit)
        self.configuration_confirmed = QCheckBox("已按本轮方案执行，并确认实际配置")
        # A proposed value is not evidence that the user executed it.
        self.configuration_confirmed.setChecked(False)
        for name, widget in [("平均 FPS", self.avg), ("1% Low FPS", self.low), ("GPU 平均占用率", self.gpu), ("重复次数", self.repeats), ("单次时长（秒）", self.duration), ("测试条件", self.condition)]:
            form.addRow(name, widget)
        form.addRow("实际 CPU", self.actual_cpu)
        form.addRow("实际 GPU", self.actual_gpu)
        form.addRow("实际分辨率", self.actual_resolution)
        form.addRow("实际刷新率", self.actual_refresh_rate)
        form.addRow("实际测试场景", self.actual_scene)
        setting_label = getattr(self.plan, "setting", "本轮设置") if self.plan else "本轮设置"
        form.addRow(f"实际 {setting_label}", self.actual_setting)
        input_layout.addLayout(form)
        input_layout.addWidget(self.configuration_confirmed)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("备注：例如关闭了帧率上限；三次测试均使用相同路线……")
        self.notes.setMaximumHeight(85)
        input_layout.addWidget(self.notes)
        self.is_data_request = bool(self.plan and self.plan.action == AgentAction.REQUEST_DATA)
        self.save_button = QPushButton("提交补充信息" if self.is_data_request else "保存结果并评估")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self._save)
        input_layout.addWidget(self.save_button)
        self.rollback_button = QPushButton("确认恢复上一组已验证配置")
        self.rollback_button.setObjectName("secondaryButton")
        self.rollback_button.setVisible(False)
        self.rollback_button.clicked.connect(self.rollback_requested.emit)
        input_layout.addWidget(self.rollback_button)
        guide = QFrame()
        guide.setObjectName("card")
        guide_layout = set_margins(QVBoxLayout(guide), 20, 18, 20, 20, 7)
        guide_layout.addWidget(label("本轮变量", "cardKicker"))
        change_title = f"{plan.setting}：{plan.old_value} → {plan.new_value}" if plan else "等待生成实验计划"
        guide_layout.addWidget(label(change_title, "actionTitle", True))
        guide_layout.addWidget(label("其余游戏设置保持不变", "metricNote"))
        for name, value in [("基线场景", state.test_scene), ("目标时长", "60 秒 × 3 次"), ("当前参考", f"{state.avg_fps} / {state.low_fps} FPS"), ("判断阈值", "平均帧率变化 5%")]:
            guide_layout.addWidget(hline())
            guide_layout.addWidget(RuleRow(name, value))
        guide_layout.addSpacing(8)
        guide_layout.addWidget(label("注意：GPU 占用率不能单独证明 CPU 瓶颈。系统会同时检查限帧、垂直同步与分辨率变化后的帧率响应。", "tagOrange", True))
        guide_layout.addStretch()
        columns.addWidget(input_card, 3)
        columns.addWidget(guide, 2)
        root.addLayout(columns)
        root.addStretch()

    def _setting_value_from_state(self) -> str:
        """Return the confirmed current value for the planned setting."""
        plan = self.plan
        setting_id = str(getattr(plan, "setting_id", "") or "") if plan else ""
        setting = str(getattr(plan, "setting", "") or "") if plan else ""
        values = {
            "resolution": self.state.resolution,
            "refresh_rate": self.state.refresh_rate,
            "test_scene": self.state.test_scene,
            "gpu_power": "" if self.state.gpu_power_w is None else str(self.state.gpu_power_w),
            "gpu_power_w": "" if self.state.gpu_power_w is None else str(self.state.gpu_power_w),
            "graphics_mode": self.state.graphics_mode,
            "power_mode": self.state.power_mode,
            "cooling_mode": self.state.cooling_mode,
        }
        if setting_id in values:
            return values[setting_id]
        labels = {
            "分辨率": self.state.resolution,
            "刷新率": self.state.refresh_rate,
            "测试场景": self.state.test_scene,
            "GPU 功耗": values["gpu_power_w"],
            "显卡输出模式": self.state.graphics_mode,
            "供电 / 性能模式": self.state.power_mode,
            "散热模式": self.state.cooling_mode,
        }
        return labels.get(setting, "")

    def _clear_csv_if_manual_edit(self, *_args):
        if self._csv_metrics is not None:
            self._csv_metrics = None
            self._csv_values = None

    def set_completed(self, completed: bool = True):
        self.completed = completed
        self.save_button.setEnabled(not completed)
        for widget in [self.file_input, self.avg, self.low, self.gpu, self.repeats, self.duration, self.condition,
                        self.actual_cpu, self.actual_gpu, self.actual_resolution, self.actual_refresh_rate,
                        self.actual_scene, self.actual_setting, self.configuration_confirmed, self.notes]:
            widget.setEnabled(not completed)
        self.rollback_button.setEnabled(not completed)

    def set_rollback_pending(self, pending: bool = True):
        self.rollback_button.setVisible(pending)
        self.save_button.setEnabled(not pending and not self.completed)

    def _actual_configuration(self) -> dict[str, str]:
        config = {key: field.text().strip() for key, field in self._actual_fields.items()}
        config["planned_setting"] = str(getattr(self.plan, "setting", "") or "") if self.plan else ""
        config["planned_setting_id"] = str(getattr(self.plan, "setting_id", "") or "") if self.plan else ""
        config["actual_setting_value"] = self.actual_setting.text().strip()
        return {key: value for key, value in config.items() if value}

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择基准测试 CSV", str(APP_DIR), "CSV 文件 (*.csv);;所有文件 (*.*)")
        if path:
            self.file_input.setText(path)
            try:
                metrics = import_benchmark_csv(path)
            except ValueError as error:
                QMessageBox.warning(self, "CSV 无法导入", str(error))
                return
            self.avg.setValue(round(float(metrics["avg_fps"])))
            self.low.setValue(round(float(metrics["low_fps"])))
            if "gpu_usage" in metrics:
                self.gpu.setValue(round(float(metrics["gpu_usage"])))
            self.repeats.setValue(int(metrics.get("run_count", 1)))
            self._csv_metrics = metrics
            self._csv_values = (self.avg.value(), self.low.value(), self.gpu.value(), self.repeats.value())
            self.notes.setPlainText(f"已从 CSV 导入（{metrics['format']}）；保存前请核对测试条件。")

    def _save(self):
        if self.completed:
            return
        if self.is_data_request and not self.actual_setting.text().strip():
            QMessageBox.warning(self, "需要补充信息", "请填写实际设置值后再提交。")
            self.actual_setting.setFocus()
            return
        try:
            resolution = normalize_resolution(self.actual_resolution.text().strip())
        except ValueError as error:
            QMessageBox.warning(self, "实际配置无效", str(error))
            return
        values = (self.avg.value(), self.low.value(), self.gpu.value(), self.repeats.value())
        metrics = self._csv_metrics if values == self._csv_values else None
        self.result_saved.emit({
            "avg": self.avg.value(),
            "low": self.low.value(),
            "gpu": self.gpu.value(),
            "repeats": self.repeats.value(),
            "duration": self.duration.value(),
            "condition": self.condition.currentText(),
            "configuration_confirmed": self.configuration_confirmed.isChecked(),
            "notes": self.notes.toPlainText().strip(),
            "file": self.file_input.text().strip(),
            "resolution": resolution,
            "configuration": self._actual_configuration(),
            "runs": list(metrics.get("runs", [])) if metrics else [],
            "avg_spread_pct": metrics.get("avg_spread_pct") if metrics else None,
            "low_spread_pct": metrics.get("low_spread_pct") if metrics else None,
            "supplement": self.is_data_request,
            "supplement_value": self.actual_setting.text().strip(),
        })


class HistoryPage(QWidget):
    task_restore_requested = Signal(str)

    def __init__(self, state: TaskState, tasks: list[dict] | None = None):
        super().__init__()
        self.state = state
        root = set_margins(QVBoxLayout(self), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("实验记忆", "每一次尝试都留下依据", "历史记录区分“智能体建议”和“用户已执行”，防止未实施的调整污染后续判断。"))
        restore_row = QHBoxLayout()
        restore_row.addWidget(label("恢复已有任务", "ruleName"))
        self.task_selector = QComboBox()
        self.task_selector.setMinimumWidth(320)
        self.task_selector.addItem("当前会话任务", "")
        restore_row.addWidget(self.task_selector, 1)
        self.restore_button = QPushButton("恢复任务")
        self.restore_button.setObjectName("secondaryButton")
        self.restore_button.clicked.connect(self._restore_selected)
        restore_row.addWidget(self.restore_button)
        root.addLayout(restore_row)
        root.addWidget(SectionHeader("实验记录", "按时间倒序"))
        self.table = QTableWidget(1, 7)
        self.table.setHorizontalHeaderLabels(["记录", "分辨率", "平均 FPS", "1% Low", "GPU 占用", "执行状态", "评估"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(250)
        rows = [["基线", state.resolution, str(state.avg_fps), str(state.low_fps), f"{state.gpu_usage}%", "已执行", "当前最佳"]]
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)
            self.table.setRowHeight(row_idx, 46)
        root.addWidget(self.table)
        root.addStretch()
        self.set_tasks(tasks or [])

    def set_tasks(self, tasks: list[dict]):
        self.task_selector.blockSignals(True)
        self.task_selector.clear()
        self.task_selector.addItem("当前会话任务", "")
        for task in tasks:
            task_id = str(task.get("task_id", ""))
            if task_id:
                title = str(task.get("name") or task.get("task_name") or task_id[:8])
                created = str(task.get("created_at", ""))[:16].replace("T", " ")
                self.task_selector.addItem(f"{title} · {created}" if created else title, task_id)
        self.task_selector.blockSignals(False)
        self.restore_button.setEnabled(self.task_selector.count() > 1)

    def _restore_selected(self):
        task_id = str(self.task_selector.currentData() or "")
        if task_id:
            self.task_restore_requested.emit(task_id)

    def add_result(self, result: dict, decision: Decision, round_number: int):
        row = self.table.rowCount()
        self.table.insertRow(row)
        decision_names = {AgentAction.RETEST: "待复测", AgentAction.KEEP_CHANGE: "保留", AgentAction.REVERT_CHANGE: "撤销", AgentAction.FINISH: "完成"}
        confirmed = "已执行" if result.get("configuration_confirmed", False) else "待确认"
        values = [f"实验 {round_number:02d}", result["resolution"], str(result["avg"]), str(result["low"]), f'{result["gpu"]}%', confirmed, decision_names.get(decision.action, "已评估")]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, col, item)
        self.table.setRowHeight(row, 46)

    def load_history(self, records: list[dict]):
        """Render persisted benchmark records after a task is restored."""
        if not records:
            return
        self.table.setRowCount(0)
        for record in records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            role = str(record.get("role", "candidate"))
            round_number = int(record.get("round_number", 0))
            values = [
                "基线" if role == "baseline" else f"实验 {round_number:02d}",
                str(record.get("resolution", "")),
                str(record.get("avg_fps", "")),
                str(record.get("low_fps", "")),
                f'{record.get("gpu_usage", "")}%',
                "已执行" if record.get("configuration_confirmed", role == "baseline") else "待确认",
                "基准记录" if role == "baseline" else "已记录",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 46)


class ReportPage(QWidget):
    def __init__(
        self,
        state: TaskState,
        result: dict | None = None,
        decision: Decision | None = None,
        explanation: dict[str, str | list[str]] | None = None,
        best_record: BenchmarkRecord | None = None,
        baseline_record: BenchmarkRecord | None = None,
        completed_count: int | None = None,
        max_rounds: int = 4,
        last_decision: dict | None = None,
    ):
        super().__init__()
        root = set_margins(QVBoxLayout(self), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("优化报告", "最佳已验证配置", "报告只总结真实录入且通过可比性检查的实验，不把建议配置表述为已验证结果。"))
        empty = QFrame()
        empty.setObjectName("card")
        empty_layout = set_margins(QVBoxLayout(empty), 30, 35, 30, 35, 8)
        icon = label("↗", "brandMark")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        title_text = "本轮评估结果" if result and decision else ("最近一次评估" if last_decision else "完成更多实验后生成报告")
        title = label(title_text, "sectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title)
        body_text = str(explanation["summary"]) if explanation else (
            decision.summary if decision else str((last_decision or {}).get("summary") or
            "达到目标、连续两轮无明确改善或候选动作耗尽时，GameTune 会汇总最佳配置、性能变化和判断依据。")
        )
        body = label(body_text, "ruleText", True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setFixedWidth(610)
        body.setMinimumHeight(42)
        empty_layout.addWidget(body, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(empty)
        if explanation:
            summary_card = QFrame()
            summary_card.setObjectName("card")
            summary_layout = set_margins(QVBoxLayout(summary_card), 22, 18, 22, 18, 8)
            source = "大模型总结" if explanation.get("source") == "model" else "本地规则总结"
            summary_layout.addWidget(SectionHeader(source, "不改变确定性评估结果"))
            for reason in explanation.get("reasoning", []):
                summary_layout.addWidget(label(f"• {reason}", "ruleText", True))
            summary_layout.addWidget(label(f"下一步：{explanation['next_step']}", "ruleName", True))
            if explanation.get("warning"):
                summary_layout.addWidget(label(str(explanation["warning"]), "tagOrange", True))
            root.addWidget(summary_card)
        if best_record:
            snapshot = QFrame()
            snapshot.setObjectName("card")
            snapshot_layout = set_margins(QVBoxLayout(snapshot), 22, 18, 22, 18, 7)
            snapshot_layout.addWidget(SectionHeader("最佳已验证配置", "来自已执行并通过评估的记录"))
            configuration = best_record.configuration or {"resolution": best_record.resolution, "test_scene": best_record.test_scene}
            for key, value in configuration.items():
                if value:
                    snapshot_layout.addWidget(RuleRow(key, value))
            snapshot_layout.addWidget(RuleRow("验证结果", f"{best_record.avg_fps:.0f} FPS / {best_record.low_fps:.0f} 1% Low"))
            root.addWidget(snapshot)
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        completed = max(0, int(completed_count if completed_count is not None else 0))
        if best_record and baseline_record:
            avg_delta = (best_record.avg_fps - baseline_record.avg_fps) / baseline_record.avg_fps * 100
            low_delta = (best_record.low_fps - baseline_record.low_fps) / baseline_record.low_fps * 100
        else:
            # A decision compares against the current accepted record.  It is
            # not the total improvement over the baseline, so leave the
            # baseline metric empty until the durable best snapshot is known.
            avg_delta = 0.0
            low_delta = 0.0
        status = "已评估" if decision else "等待实验"
        denominator = max(1, int(max_rounds))
        metrics.addWidget(MetricCard("已完成实验", f"{completed} / {denominator}", "独立实验轮数", min(100, round(completed / denominator * 100))))
        metrics.addWidget(MetricCard("平均 FPS 改善", f"{avg_delta:+.1f}%", status, min(100, max(0, 50 + round(avg_delta * 5)))))
        metrics.addWidget(MetricCard("1% LOW 改善", f"{low_delta:+.1f}%", status, min(100, max(0, 50 + round(low_delta * 5)))))
        root.addLayout(metrics)
        root.addStretch()


class APISettingsPage(QScrollArea):
    provider_applied = Signal(object)

    THINKING_OPTIONS = [
        ("关闭（不发送思考参数）", None),
        ("低", "low"),
        ("中", "medium"),
        ("高", "high"),
    ]

    def __init__(self, config: APIConfig | None = None):
        super().__init__()
        self._request_generation = 0
        self._model_generation = 0
        self._model_jobs: dict[int, tuple[QThread, ModelWorker]] = {}
        self._model_callbacks: dict[int, tuple[object, object]] = {}
        self._model_thread: QThread | None = None
        self._model_worker: ModelWorker | None = None
        self.setWidgetResizable(True)
        content = QWidget()
        self.setWidget(content)
        root = set_margins(QVBoxLayout(content), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("模型设置", "API 与思考模式", "模型只总结已测数据和确定性结论；连接失败时自动回退到本地规则。"))
        card = QFrame()
        card.setObjectName("formCard")
        form = QFormLayout(card)
        form.setContentsMargins(24, 22, 24, 22)
        form.setSpacing(13)
        self.base_url = QLineEdit(config.base_url if config else "")
        self.base_url.setPlaceholderText("https://your-service.example/v1")
        self.api_key = QLineEdit(config.api_key if config else "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("仅保留在当前运行会话中")
        self.model = QLineEdit(config.model if config else "")
        self.model.setPlaceholderText("模型名称，例如 your-model-name")
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setDecimals(0)
        self.timeout.setSuffix(" 秒")
        self.timeout.setValue(config.timeout_seconds if config else 30)
        self.timeout.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.reasoning = QComboBox()
        for title, value in self.THINKING_OPTIONS:
            self.reasoning.addItem(title, value)
        current_reasoning = config.reasoning_effort if config else None
        self.reasoning.setCurrentIndex(next((i for i, item in enumerate(self.THINKING_OPTIONS) if item[1] == current_reasoning), 0))
        form.addRow("API 地址", self.base_url)
        form.addRow("API 密钥", self.api_key)
        form.addRow("模型名称", self.model)
        form.addRow("请求超时", self.timeout)
        form.addRow("思考模式", self.reasoning)
        root.addWidget(card)
        root.addWidget(label("兼容说明：接口按 OpenAI Chat Completions 格式调用。部分兼容服务不支持 reasoning_effort，此时请选择“关闭”。密钥不会写入数据库或配置文件。", "ruleText", True))
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用配置")
        self.apply_button.setObjectName("primaryButton")
        self.test_button = QPushButton("测试连接")
        self.test_button.setObjectName("secondaryButton")
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.test_button)
        buttons.addStretch()
        root.addLayout(buttons)
        self.status = label("尚未应用模型配置", "sectionMeta", True)
        root.addWidget(self.status)
        root.addStretch()
        self.apply_button.clicked.connect(self._apply)
        self.test_button.clicked.connect(self._test)

    def _config(self) -> APIConfig:
        return APIConfig(
            self.base_url.text().strip().rstrip("/"), self.api_key.text().strip(),
            self.model.text().strip(), self.timeout.value(), self.reasoning.currentData(),
        )

    def _apply(self):
        try:
            provider = OpenAICompatibleProvider(self._config())
        except ValueError as error:
            QMessageBox.warning(self, "配置无效", str(error))
            return
        self.provider_applied.emit(provider)
        self.status.setText(f"已应用：{provider.config.model} · 思考模式 {self.reasoning.currentText()}")

    def _test(self):
        try:
            provider = OpenAICompatibleProvider(self._config())
            request = ModelRequest(
                messages=(ModelMessage("user", "请只回复：连接成功"),), temperature=0,
                max_output_tokens=32, response_format="text", metadata={"purpose": "connection_test"},
            )
        except ValueError as error:
            self.status.setText(f"连接失败：{error}")
            return
        self._request_generation += 1
        generation = self._request_generation
        self.test_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status.setText("正在测试连接……")
        self._start_model_worker(
            lambda: provider.complete(request),
            lambda response: self._test_succeeded(generation, provider, response),
            lambda error: self._test_failed(generation, error),
        )

    def _test_succeeded(self, generation: int, provider: OpenAICompatibleProvider, response: ModelResponse):
        if generation != self._request_generation:
            return
        self.test_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        self.provider_applied.emit(provider)
        preview = response.content.strip().replace("\n", " ")[:120]
        self.status.setText(f"连接成功 · {response.model} · {preview}")
        QMessageBox.information(self, "连接测试", f"连接成功\n模型：{response.model}\n响应：{preview}")

    def _test_failed(self, generation: int, error: Exception):
        if generation != self._request_generation:
            return
        self.test_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        message = str(error) if isinstance(error, ModelAPIError) else "模型服务暂不可用，请检查地址、密钥和网络。"
        self.status.setText(f"连接失败：{message}")
        QMessageBox.warning(self, "连接测试失败", message)

    def _start_model_worker(self, operation, on_success, on_failure):
        self._model_generation += 1
        job_id = self._model_generation
        thread = QThread(self)
        worker = ModelWorker(operation, job_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished_with_id.connect(self._model_job_succeeded)
        worker.failed_with_id.connect(self._model_job_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda token=job_id, running_thread=thread: self._model_call_finished(token, running_thread))
        self._model_jobs[job_id] = (thread, worker)
        self._model_callbacks[job_id] = (on_success, on_failure)
        self._model_thread = thread
        self._model_worker = worker
        thread.start()

    @Slot(int, object)
    def _model_job_succeeded(self, job_id: int, result):
        callbacks = self._model_callbacks.get(job_id)
        if callbacks and job_id in self._model_jobs:
            callbacks[0](result)

    @Slot(int, object)
    def _model_job_failed(self, job_id: int, error: Exception):
        callbacks = self._model_callbacks.get(job_id)
        if callbacks and job_id in self._model_jobs:
            callbacks[1](error)

    def _model_call_finished(self, job_id: int, thread: QThread):
        self._model_jobs.pop(job_id, None)
        self._model_callbacks.pop(job_id, None)
        if self._model_thread is thread:
            self._model_worker = None
            self._model_thread = None
        thread.deleteLater()

    def stop_workers(self, wait_ms: int = 1000):
        """Stop connection-test workers before the owning window is closed."""
        self._request_generation += 1
        self._model_callbacks.clear()
        for thread, _worker in list(self._model_jobs.values()):
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(wait_ms):
                # urllib calls are not cancellable through QThread.  On
                # application shutdown, terminate the remaining worker so no
                # background thread survives the window.
                thread.terminate()
                thread.wait(wait_ms)
        self._model_jobs.clear()
        self._model_thread = None
        self._model_worker = None

    def closeEvent(self, event):
        self.stop_workers()
        super().closeEvent(event)

    def closeEvent(self, event):
        for thread, _worker in list(self._model_jobs.values()):
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning():
                thread.wait(100)
        super().closeEvent(event)


class Sidebar(QFrame):
    navigate = Signal(int)

    def __init__(self, state: TaskState):
        super().__init__()
        self.state = state
        self.setObjectName("sidebar")
        self.setFixedWidth(225)
        root = set_margins(QVBoxLayout(self), 17, 20, 17, 18, 8)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = label("G", "brandMark")
        mark.setFixedSize(38, 38)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(label("GameTune", "brandName"))
        brand_text.addWidget(label("PERFORMANCE AGENT", "brandSub"))
        brand.addWidget(mark)
        brand.addLayout(brand_text)
        brand.addStretch()
        root.addLayout(brand)
        root.addSpacing(22)
        root.addWidget(label("工作区", "sidebarLabel"))
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for text, index in [("概览", 0), ("新建任务", 1), ("当前实验", 2), ("历史记录", 3), ("最终报告", 4), ("模型设置", 5)]:
            button = QPushButton(text)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setMinimumHeight(39)
            self.group.addButton(button, index)
            root.addWidget(button)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.navigate)
        root.addSpacing(16)
        root.addWidget(hline())
        root.addSpacing(12)
        root.addWidget(label("当前任务", "sidebarLabel"))
        task = QFrame()
        task.setObjectName("sideTask")
        task_layout = set_margins(QVBoxLayout(task), 12, 11, 12, 11, 4)
        self.task_title = label(f"●  {state.name}", "sideTaskTitle", True)
        self.task_text = label(f"第 {state.round_number} 轮 · {state.step}\n{state.game}", "sideTaskText", True)
        task_layout.addWidget(self.task_title)
        task_layout.addWidget(self.task_text)
        root.addWidget(task)
        root.addStretch()
        root.addWidget(label("本地原型 · 数据仅保存在当前会话", "sideTaskText", True))

    def update_task(self):
        self.task_title.setText(f"●  {self.state.name}")
        self.task_text.setText(f"第 {self.state.round_number} 轮 · {self.state.step}\n{self.state.game}")


class MainWindow(QMainWindow):
    def __init__(
        self,
        database_path: str | Path | None = None,
        controller: GameTuneController | None = None,
    ):
        super().__init__()
        self.state = TaskState()
        self.api_config_error = ""
        try:
            model_provider = provider_from_env()
        except ValueError as error:
            model_provider = None
            self.api_config_error = str(error)
        self.controller = controller or GameTuneController(
            database_path or APP_DIR / ".data" / "gametune.db",
            model_provider=model_provider,
        )
        initial_api_config = model_provider.config if isinstance(model_provider, OpenAICompatibleProvider) else None
        self.current_explanation: dict[str, str | list[str]] | None = None
        self.core_task: OptimizationTask | None = None
        self.baseline_record: BenchmarkRecord | None = None
        self._task_generation = 0
        self._explanation_generation = 0
        self._explanation_jobs: dict[int, tuple[QThread, ModelWorker, dict]] = {}
        self._explanation_thread: QThread | None = None
        self._explanation_worker: ModelWorker | None = None
        self._pending_rollback: dict | None = None
        preview_task, preview_baseline = self._core_objects()
        self.current_evidence = analyze_evidence(preview_task, preview_baseline, fps_cap_known=True, vsync_known=True)
        self.current_plan = plan_next_experiment(preview_task, preview_baseline, self.current_evidence, 1)
        self.setWindowTitle("GameTune Agent · 游戏性能优化智能体")
        self.resize(1280, 820)
        self.setMinimumSize(1050, 700)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = set_margins(QHBoxLayout(root), spacing=0)
        self.sidebar = Sidebar(self.state)
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(self.state, self.current_evidence, self.current_plan)
        self.new_task = NewTaskPage(self.state)
        self.experiment = ExperimentPage(self.state, self.current_plan)
        self.history = HistoryPage(self.state, self._available_tasks())
        self.report = ReportPage(self.state)
        self.api_settings = APISettingsPage(initial_api_config)
        for page in [self.dashboard, self.new_task, self.experiment, self.history, self.report, self.api_settings]:
            self.stack.addWidget(page)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.sidebar.navigate.connect(self.set_page)
        self.dashboard.experiment_requested.connect(lambda: self.set_page(2))
        self.new_task.task_created.connect(self._task_created)
        self.experiment.result_saved.connect(self._result_saved)
        self.experiment.rollback_requested.connect(self._confirm_pending_rollback)
        self.history.task_restore_requested.connect(self._restore_task)
        self.api_settings.provider_applied.connect(self._apply_model_provider)
        if self.api_config_error:
            self.statusBar().showMessage(f"API 配置未启用 · {self.api_config_error}", 0)
        elif model_provider:
            self.statusBar().showMessage(f"就绪 · 已连接模型适配器 {model_provider.name}", 0)
        else:
            self.statusBar().showMessage("就绪 · 离线模式，模型说明自动使用本地规则", 0)

    def _apply_model_provider(self, provider: OpenAICompatibleProvider):
        self.controller.model_provider = provider
        mode = provider.config.reasoning_effort or "关闭"
        self.statusBar().showMessage(f"模型配置已应用 · {provider.config.model} · 思考模式 {mode}", 5000)

    def set_page(self, index: int):
        self.stack.setCurrentIndex(index)
        button = self.sidebar.group.button(index)
        if button:
            button.setChecked(True)

    def _available_tasks(self) -> list[dict]:
        """Read task summaries when the repository exposes the list API."""
        lister = getattr(self.controller.repository, "list_tasks", None)
        if not callable(lister):
            return []
        try:
            return list(lister())
        except Exception:
            return []

    def _refresh_task_selector(self):
        if hasattr(self.history, "set_tasks"):
            self.history.set_tasks(self._available_tasks())

    def _restore_task(self, task_id: str):
        try:
            task = self.controller.restore_task(task_id)
            if task is None:
                raise ValueError("任务记录不存在，或缺少可恢复的基线与运行状态。")
            bundle_loader = getattr(self.controller.repository, "load_task_bundle", None)
            bundle = bundle_loader(task_id) if callable(bundle_loader) else None
            baseline = self.controller.repository.benchmark_for_role(task_id, "baseline")
            if baseline is None:
                raise ValueError("任务缺少基线记录，无法恢复。")
            current = self.controller.current_record(task_id) or baseline
            runtime = self.controller.repository.runtime_state(task_id) or {}
            self.core_task, self.baseline_record = task, baseline
            self._task_generation += 1
            self._load_state_from_task(task, current)
            self.state.baseline_repeats = baseline.repeats
            self.state.baseline_duration_seconds = baseline.duration_seconds or 60
            self.state.baseline_confirmed = baseline.configuration_confirmed
            self.current_evidence = analyze_evidence(
                task,
                current,
                baseline,
                fps_cap_known=True,
                vsync_known=True,
            )
            self.current_plan = self._restored_plan(task, current, baseline, runtime)
            self._rebuild_task_pages(reset_results=True)
            # Restoring is initiated from the visible history page.  Showing
            # here also keeps programmatic restore flows consistent with the
            # interactive path and makes the pending rollback affordance
            # observable before the caller processes another event loop turn.
            self.show()
            app_instance = QApplication.instance()
            if app_instance is not None:
                app_instance.processEvents()
            self.history.load_history(self.controller.repository.task_history(task_id))
            self._refresh_task_selector()
            self.sidebar.update_task()
            self._replace_report(
                best_record=self.controller.best_record(task_id),
                last_decision=(bundle or {}).get("decisions", [])[-1]
                if (bundle or {}).get("decisions") else None,
            )
            runtime_state = str(runtime.get("workflow_state", ""))
            self.experiment.set_rollback_pending(runtime_state == "awaiting_rollback")
            if runtime_state in {"complete", "completed", "finished"}:
                self.experiment.set_completed(True)
            self.statusBar().showMessage(f"已恢复任务 · {task.name}", 5000)
            # A pending rollback is an actionable workflow state; open the
            # experiment page so the recovery button is immediately visible.
            self.set_page(2 if runtime_state == "awaiting_rollback" else 0)
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "恢复任务失败", str(error))

    def _load_state_from_task(self, task: OptimizationTask, current: BenchmarkRecord):
        for field_name in ("name", "game", "cpu", "gpu", "resolution", "refresh_rate", "test_scene",
                           "target_avg_fps", "target_low_fps", "mode", "is_laptop", "gpu_power_w",
                           "graphics_mode", "power_mode", "cooling_mode"):
            state_name = {"target_avg_fps": "target_avg", "target_low_fps": "target_low"}.get(field_name, field_name)
            if hasattr(task, field_name) and hasattr(self.state, state_name):
                setattr(self.state, state_name, getattr(task, field_name))
        self.state.avg_fps = round(current.avg_fps)
        self.state.low_fps = round(current.low_fps)
        self.state.gpu_usage = round(current.gpu_usage or self.state.gpu_usage)
        runtime = self.controller.repository.runtime_state(task.task_id) or {}
        self.state.round_number = int(runtime.get("round_number", 1))
        self.state.step = "优化完成" if str(runtime.get("workflow_state", "")) in {"complete", "completed", "finished"} else "等待复测"
        self.state.has_task = True

    def _restored_plan(self, task: OptimizationTask, current: BenchmarkRecord, baseline: BenchmarkRecord, runtime: dict):
        workflow_state = str(runtime.get("workflow_state", ""))
        if workflow_state in {"complete", "completed", "finished"}:
            return None
        plan = self.controller.pending_plan(task.task_id)
        if plan is not None:
            return plan
        round_number = int(runtime.get("round_number", 1))
        try:
            _evidence, plan = self.controller.plan_after_result(
                task, current, baseline, round_number,
                fps_cap_known=True, vsync_known=True,
            )
            return plan
        except (ValueError, TypeError):
            return None

    def _task_created(self, _state: TaskState):
        try:
            self.core_task, self.baseline_record = self._core_objects()
            evidence, plan = self.controller.create_task(
                self.core_task,
                self.baseline_record,
                fps_cap_known=self.state.fps_cap_checked,
                vsync_known=self.state.vsync_checked,
            )
        except ValueError as error:
            QMessageBox.warning(self, "任务数据无效", str(error))
            return
        self._task_generation += 1
        self._explanation_generation += 1
        self.current_evidence, self.current_plan = evidence, plan
        self._rebuild_task_pages(reset_results=True)
        if plan.action == AgentAction.FINISH:
            self.state.step = "优化完成"
            self.experiment.set_completed(True)
        self._refresh_task_selector()
        self.sidebar.update_task()
        self.statusBar().showMessage(f"任务已保存 · 已生成 {len(evidence)} 条证据", 5000)
        QMessageBox.information(self, "任务已创建", f"硬件、目标与基线数据已写入本地记录。\n\n下一步：{plan.title}")
        self.set_page(0)

    def _rebuild_task_pages(self, reset_results: bool = False):
        """Rebuild pages whose labels are derived from the newly saved task."""
        old_dashboard = self.dashboard
        self.stack.removeWidget(old_dashboard)
        old_dashboard.deleteLater()
        self.dashboard = DashboardPage(self.state, self.current_evidence, self.current_plan)
        self.dashboard.experiment_requested.connect(lambda: self.set_page(2))
        self.stack.insertWidget(0, self.dashboard)

        old_experiment = self.experiment
        self.stack.removeWidget(old_experiment)
        old_experiment.deleteLater()
        self.experiment = ExperimentPage(self.state, self.current_plan)
        self.experiment.result_saved.connect(self._result_saved)
        self.experiment.rollback_requested.connect(self._confirm_pending_rollback)
        self.stack.insertWidget(2, self.experiment)

        if reset_results:
            old_history = self.history
            self.stack.removeWidget(old_history)
            old_history.deleteLater()
            self.history = HistoryPage(self.state, self._available_tasks())
            self.history.task_restore_requested.connect(self._restore_task)
            self.stack.insertWidget(3, self.history)
            self._replace_report()

    def _record_from_result(self, result: dict) -> BenchmarkRecord:
        configuration = dict(result.get("configuration") or {})
        configuration.setdefault("cpu", self.state.cpu)
        configuration.setdefault("gpu", self.state.gpu)
        configuration.setdefault("resolution", result["resolution"])
        configuration.setdefault("refresh_rate", self.state.refresh_rate)
        configuration.setdefault("test_scene", self.state.test_scene)
        optional = {
            "runs": result.get("runs") or [],
            "avg_spread_pct": result.get("avg_spread_pct"),
            "low_spread_pct": result.get("low_spread_pct"),
        }
        fields = getattr(BenchmarkRecord, "__dataclass_fields__", {})
        kwargs = {
            "avg_fps": result["avg"],
            "low_fps": result["low"],
            "gpu_usage": result.get("gpu"),
            "resolution": result["resolution"],
            "test_scene": configuration.get("test_scene", self.state.test_scene),
            "repeats": result["repeats"],
            "duration_seconds": result["duration"],
            "comparable": result["condition"] == "与基线一致",
            "notes": result.get("notes", ""),
            "source": "csv" if result.get("file") else "manual",
            "configuration": configuration,
            "configuration_confirmed": bool(result.get("configuration_confirmed", False)),
        }
        for key, value in optional.items():
            if key in fields and value is not None:
                kwargs[key] = value
        return BenchmarkRecord(**kwargs)

    def _result_saved(self, result: dict):
        if result.get("supplement"):
            self._supplement_task_data(result)
            return
        if self.core_task is None or self.baseline_record is None:
            self.core_task, self.baseline_record = self._core_objects()
            self.controller.create_task(
                self.core_task,
                self.baseline_record,
                fps_cap_known=self.state.fps_cap_checked,
                vsync_known=self.state.vsync_checked,
            )
        try:
            candidate = self._record_from_result(result)
            evaluated_round = self.state.round_number
            reference = self.controller.current_record(self.core_task.task_id) or self.baseline_record
            decision = self.controller.evaluate(self.core_task, self.baseline_record, candidate, evaluated_round, reference=reference)
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "结果无法评估", str(error))
            return
        context = {"result": result, "candidate": candidate, "decision": decision, "round": evaluated_round}
        if decision.action == AgentAction.REVERT_CHANGE:
            answer = QMessageBox.question(
                self,
                "确认撤销调整",
                "本轮候选配置表现退步。是否确认恢复上一组已验证配置并继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._pending_rollback = context
                self.experiment.set_rollback_pending(True)
                self.statusBar().showMessage("已暂停：等待确认恢复上一组已验证配置", 0)
                self._start_explanation(context)
                return
            try:
                self._confirm_controller_rollback(True)
            except (ValueError, TypeError, KeyError) as error:
                QMessageBox.warning(self, "恢复配置失败", str(error))
                self._pending_rollback = context
                self.experiment.set_rollback_pending(True)
                return
            context["rollback_confirmed"] = True
        self._complete_decision(context)

    def _supplement_task_data(self, result: dict):
        """Submit a REQUEST_DATA value without creating a benchmark row.

        The controller owns validation and persistence of the task update.  A
        missing or malformed response is surfaced as an error instead of
        silently falling back to a guessed setting.
        """
        if self.core_task is None or self.current_plan is None:
            QMessageBox.warning(self, "无法补充信息", "当前没有等待补充信息的实验计划。")
            return
        setting_id = str(self.current_plan.setting_id or self.current_plan.setting).strip()
        value = str(result.get("supplement_value", "")).strip()
        if not setting_id or not value:
            QMessageBox.warning(self, "信息不完整", "请填写当前设置的实际值。")
            return
        try:
            evidence, plan = self.controller.supplement_data(
                self.core_task,
                {setting_id: value},
            )
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "补充信息无效", str(error))
            return
        if not isinstance(evidence, list) or not isinstance(plan, ExperimentPlan):
            QMessageBox.warning(self, "补充信息失败", "核心未返回有效的下一步实验计划。")
            return
        self.current_evidence, self.current_plan = evidence, plan
        self._sync_state_from_core_task()
        self.state.step = "分析完成"
        self.sidebar.update_task()
        self._rebuild_task_pages()
        self.statusBar().showMessage(f"已记录 {self.current_plan.setting} · 已生成下一步计划", 5000)
        self.set_page(0)

    def _sync_state_from_core_task(self):
        if not self.core_task:
            return
        for field_name in (
            "name", "game", "cpu", "gpu", "resolution", "refresh_rate", "test_scene",
            "target_avg_fps", "target_low_fps", "mode", "is_laptop", "gpu_power_w",
            "graphics_mode", "power_mode", "cooling_mode",
        ):
            state_name = {"target_avg_fps": "target_avg", "target_low_fps": "target_low"}.get(field_name, field_name)
            if hasattr(self.core_task, field_name) and hasattr(self.state, state_name):
                setattr(self.state, state_name, getattr(self.core_task, field_name))

    def _confirm_pending_rollback(self):
        context = self._pending_rollback
        if not context:
            # A restored task can be waiting for rollback without retaining a
            # transient in-memory candidate context.  The persisted workflow
            # is still authoritative, so confirm it directly and regenerate
            # the next plan from the accepted record.
            if not self.core_task:
                return
            answer = QMessageBox.question(
                self,
                "确认恢复配置",
                "该任务等待恢复上一组已验证配置。确认恢复并进入下一轮实验？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                self._confirm_controller_rollback(True)
                current = self.controller.current_record(self.core_task.task_id) or self.baseline_record
                if current is None:
                    raise ValueError("找不到上一组已验证配置")
                runtime = self.controller.repository.runtime_state(self.core_task.task_id) or {}
                self._apply_record_to_state(current)
                self.state.round_number = int(runtime.get("round_number", self.state.round_number + 1))
                workflow_state = str(runtime.get("workflow_state", ""))
                if workflow_state in {"complete", "completed", "finished"} or self.state.round_number > self.core_task.max_rounds:
                    self.current_evidence, self.current_plan = [], None
                    self.state.step = "优化完成"
                else:
                    self.current_evidence, self.current_plan = self.controller.plan_after_result(
                        self.core_task,
                        current,
                        self.baseline_record,
                        self.state.round_number,
                        fps_cap_known=self.state.fps_cap_checked,
                        vsync_known=self.state.vsync_checked,
                    )
                    self.state.step = "等待复测"
                self.sidebar.update_task()
                self._rebuild_task_pages()
                self.experiment.set_rollback_pending(False)
                if workflow_state in {"complete", "completed", "finished"} or self.current_plan is None:
                    self.experiment.set_completed(True)
                self._replace_report(best_record=self.controller.best_record(self.core_task.task_id))
                self.statusBar().showMessage("已确认恢复配置 · 已生成下一轮计划", 5000)
            except (ValueError, TypeError, KeyError) as error:
                QMessageBox.warning(self, "恢复配置失败", str(error))
            return
        answer = QMessageBox.question(
            self,
            "确认恢复配置",
            "确认恢复上一组已验证配置，并进入下一轮实验？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._confirm_controller_rollback(True)
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "恢复配置失败", str(error))
            return
        context["rollback_confirmed"] = True
        self._pending_rollback = None
        self.experiment.set_rollback_pending(False)
        self._complete_decision(context)

    def _confirm_controller_rollback(self, confirmed: bool):
        """Apply the explicit rollback confirmation through the core API."""
        if not self.core_task:
            raise ValueError("当前没有可恢复的任务")
        return self.controller.confirm_rollback(self.core_task, confirmed=confirmed)

    def _apply_record_to_state(self, record: BenchmarkRecord):
        self.state.avg_fps = round(record.avg_fps)
        self.state.low_fps = round(record.low_fps)
        if record.gpu_usage is not None:
            self.state.gpu_usage = round(record.gpu_usage)
        config = record.configuration or {}
        for key, state_name in (("resolution", "resolution"), ("refresh_rate", "refresh_rate"), ("test_scene", "test_scene"),
                                ("cpu", "cpu"), ("gpu", "gpu")):
            if config.get(key):
                setattr(self.state, state_name, str(config[key]))

    def _same_retest_plan(self, round_number: int) -> ExperimentPlan | None:
        if self.current_plan is None:
            return None
        title = self.current_plan.title
        if "复测" not in title:
            title = f"复测同一配置：{title}"
        try:
            return replace(self.current_plan, title=title, round_number=round_number)
        except TypeError:
            return self.current_plan

    def _complete_decision(self, context: dict):
        result, candidate = context["result"], context["candidate"]
        decision, evaluated_round = context["decision"], context["round"]
        accepted = bool(decision.accepted)
        if decision.action == AgentAction.REVERT_CHANGE:
            # A rollback restores the previous accepted record.  The
            # regression candidate remains rejected even after the user
            # confirms the physical restoration.
            accepted = False
        if accepted and decision.action in {AgentAction.KEEP_CHANGE, AgentAction.FINISH}:
            accepted_record = self.controller.current_record(self.core_task.task_id) if self.core_task else None
            self._apply_record_to_state(accepted_record or candidate)
        elif decision.action == AgentAction.REVERT_CHANGE:
            current = self.controller.current_record(self.core_task.task_id) if self.core_task else None
            if current:
                self._apply_record_to_state(current)
        task_complete = bool(getattr(decision, "task_complete", False)) or decision.action == AgentAction.FINISH
        if task_complete:
            self.state.step = "优化完成"
            self.state.round_number = evaluated_round
        elif decision.action == AgentAction.RETEST:
            # The state machine deliberately keeps RETEST in the same round;
            # the pending plan and actual configuration must remain identical.
            self.state.round_number = evaluated_round
            self.state.step = "等待同配置复测"
        else:
            self.state.round_number = evaluated_round + 1
            self.state.step = "复测评估完成"
        self.sidebar.update_task()
        self.history.add_result(result, decision, evaluated_round)
        if decision.action == AgentAction.RETEST:
            current = self.controller.current_record(self.core_task.task_id) if self.core_task else candidate
            if current:
                self._apply_record_to_state(current)
            self.current_plan = self._same_retest_plan(self.state.round_number)
        elif not task_complete and self.state.round_number <= self.core_task.max_rounds:
            current = self.controller.current_record(self.core_task.task_id) or self.baseline_record
            try:
                self.current_evidence, next_plan = self.controller.plan_after_result(
                    self.core_task, current, self.baseline_record, self.state.round_number,
                    fps_cap_known=self.state.fps_cap_checked,
                    vsync_known=self.state.vsync_checked,
                )
                self.current_plan = next_plan
                if next_plan.action == AgentAction.FINISH:
                    task_complete = True
                    self.state.step = "优化完成"
            except (ValueError, TypeError) as error:
                self.current_plan = None
                self.statusBar().showMessage(f"下一步计划生成失败：{error}", 0)
            self._rebuild_task_pages()
        if task_complete:
            self.experiment.set_completed(True)
        else:
            self.experiment.set_rollback_pending(False)
        self._start_explanation(context)

    def _start_explanation(self, context: dict):
        self._explanation_generation += 1
        job_id = self._explanation_generation
        if self.core_task is None:
            return
        task_snapshot = deepcopy(self.core_task)
        decision_snapshot = deepcopy(context["decision"])
        evidence_snapshot = deepcopy(self.current_evidence)
        plan_snapshot = deepcopy(self.current_plan)
        job_context = {
            **context,
            "generation": job_id,
            "task_generation": self._task_generation,
            "task_id": self.core_task.task_id,
            "task": task_snapshot,
            "decision_snapshot": decision_snapshot,
            "evidence_snapshot": evidence_snapshot,
            "plan_snapshot": plan_snapshot,
        }
        thread = QThread(self)
        worker = ModelWorker(lambda: self.controller.explain(
            task_snapshot,
            decision_snapshot,
            evidence=evidence_snapshot,
            plan=plan_snapshot,
        ), job_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished_with_id.connect(self._explanation_finished)
        worker.failed_with_id.connect(self._explanation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda token=job_id, running_thread=thread: self._explanation_thread_finished(token, running_thread))
        self._explanation_jobs[job_id] = (thread, worker, job_context)
        self._explanation_thread, self._explanation_worker = thread, worker
        self.statusBar().showMessage("正在生成说明（界面仍可操作）……", 0)
        thread.start()

    def _explanation_context(self, job_id: int) -> dict | None:
        entry = self._explanation_jobs.get(job_id)
        if not entry:
            return None
        context = entry[2]
        if job_id != self._explanation_generation:
            return None
        if context.get("task_generation") != self._task_generation:
            return None
        if not self.core_task or context.get("task_id") != self.core_task.task_id:
            return None
        return context

    @Slot(int, object)
    def _explanation_finished(self, job_id: int, explanation):
        context = self._explanation_context(job_id)
        if context is None:
            return
        self.current_explanation = explanation
        self._replace_report(
            context["result"], context["decision"], explanation,
            self.controller.best_record(context["task_id"]),
        )
        decision = context["decision"]
        action_names = {
            AgentAction.RETEST: "需要复测", AgentAction.KEEP_CHANGE: "保留调整",
            AgentAction.REVERT_CHANGE: "撤销调整", AgentAction.FINISH: "完成优化",
        }
        action = action_names.get(decision.action, decision.action.value)
        details = "\n".join(f"• {item}" for item in explanation.get("reasoning", []))
        warning = f"\n\n{explanation['warning']}" if explanation.get("warning") else ""
        QMessageBox.information(
            self, "评估已生成",
            f"结论：{action}\n{explanation['summary']}\n\n{details}\n\n下一步：{explanation['next_step']}{warning}",
        )
        self.statusBar().showMessage(f"复测结果已保存 · {action}", 6000)
        self.set_page(3)

    @Slot(int, object)
    def _explanation_failed(self, job_id: int, error: Exception):
        context = self._explanation_context(job_id)
        if context is None:
            return
        decision = context["decision"]
        fallback = {
            "summary": decision.summary,
            "reasoning": list(getattr(decision, "reasons", []))[:4],
            "next_step": "请按当前实验计划继续。",
            "warning": "模型说明暂不可用，已使用本地规则。",
            "source": "local",
        }
        self._explanation_finished(job_id, fallback)

    def _explanation_thread_finished(self, job_id: int, thread: QThread):
        self._explanation_jobs.pop(job_id, None)
        if self._explanation_thread is thread:
            self._explanation_worker = None
            self._explanation_thread = None
        thread.deleteLater()

    def _replace_report(
        self,
        result: dict | None = None,
        decision: Decision | None = None,
        explanation: dict[str, str | list[str]] | None = None,
        best_record: BenchmarkRecord | None = None,
        last_decision: dict | None = None,
    ):
        old_report = self.report
        self.stack.removeWidget(old_report)
        old_report.deleteLater()
        completed_count = 0
        max_rounds = self.core_task.max_rounds if self.core_task else 4
        if self.core_task:
            try:
                statistics = self.controller.repository.task_statistics(self.core_task.task_id)
                completed_count = int(statistics.get("experiment_count", 0))
            except Exception:
                completed_count = 0
        self.report = ReportPage(
            self.state, result, decision, explanation, best_record,
            self.baseline_record, completed_count, max_rounds, last_decision,
        )
        self.stack.insertWidget(4, self.report)

    def _core_objects(self) -> tuple[OptimizationTask, BenchmarkRecord]:
        task = OptimizationTask(
            name=self.state.name,
            game=self.state.game,
            cpu=self.state.cpu,
            gpu=self.state.gpu,
            resolution=self.state.resolution,
            refresh_rate=self.state.refresh_rate,
            test_scene=self.state.test_scene,
            target_avg_fps=self.state.target_avg,
            target_low_fps=self.state.target_low,
            mode=self.state.mode,
            is_laptop=self.state.is_laptop,
            gpu_power_w=self.state.gpu_power_w,
            graphics_mode=self.state.graphics_mode,
            power_mode=self.state.power_mode,
            cooling_mode=self.state.cooling_mode,
        )
        baseline = BenchmarkRecord(
            avg_fps=self.state.avg_fps,
            low_fps=self.state.low_fps,
            gpu_usage=self.state.gpu_usage,
            resolution=self.state.resolution,
            test_scene=self.state.test_scene,
            repeats=self.state.baseline_repeats,
            duration_seconds=self.state.baseline_duration_seconds,
            source="manual",
            notes="用户录入的基线摘要；未勾选基线采集确认时不视为仪器已核验数据。"
            if not self.state.baseline_confirmed else "",
            configuration_confirmed=self.state.baseline_confirmed,
        )
        return task, baseline

    def closeEvent(self, event):
        # Stop both the explanation workers and the API settings workers.  A
        # provider call may be blocking in urllib, so use a bounded wait and
        # terminate only as a last resort during application shutdown.
        self.api_settings.stop_workers()
        self._explanation_generation += 1
        for thread, _worker, _context in list(self._explanation_jobs.values()):
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(1000):
                thread.terminate()
                thread.wait(1000)
        self._explanation_jobs.clear()
        super().closeEvent(event)


def run_app(screenshot: Path | None = None, page_index: int = 0) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("GameTune Agent")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    if screenshot:
        screenshot.parent.mkdir(parents=True, exist_ok=True)

        def capture():
            window.repaint()
            app.processEvents()
            pixmap = QPixmap(window.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            window.render(pixmap)
            pixmap.save(str(screenshot))
            app.quit()

        QTimer.singleShot(100, lambda: window.set_page(page_index))
        QTimer.singleShot(900, capture)
    else:
        window.set_page(page_index)
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="GameTune Agent desktop UI")
    parser.add_argument("--screenshot", type=Path, help="Render the dashboard to a PNG and exit")
    parser.add_argument("--page", type=int, choices=range(6), default=0, help="Page index used by screenshot mode (0-5)")
    args = parser.parse_args()
    return run_app(args.screenshot, args.page)


if __name__ == "__main__":
    raise SystemExit(main())
