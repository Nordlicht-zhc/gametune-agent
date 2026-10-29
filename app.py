from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

from PySide6.QtCore import QObject, QEvent, Qt, QThread, QTimer, Signal, Slot, Property, QPropertyAnimation, QEasingCurve, QRectF, QSize, QSequentialAnimationGroup, QParallelAnimationGroup, QPauseAnimation
from PySide6.QtGui import QFont, QPixmap, QPainter, QIcon, QPen, QColor, QBrush, QPainterPath, QRegion, QValidator
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QBoxLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QDialog, QAbstractSpinBox, QDoubleSpinBox, QStyle, QStyleOptionComboBox,
    QCheckBox,
    QGraphicsOpacityEffect,
)

from agent.explainer import explain_decision
from agent.prompts import _plan_dict
from agent.controller import GameTuneController, UNCAPPED_CHOICES
from agent.api import (
    APIConfig, ModelAPIError, ModelMessage, ModelRequest, ModelResponse,
    OpenAICompatibleProvider, provider_from_env,
)
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from tools.configuration import CONFIGURATION_LABELS, canonical_value
from tools.import_benchmark import import_benchmark_csv
from tools.analyze_evidence import analyze_evidence, dominant_hypothesis, evidence_support_level
from tools.resolution import RESOLUTION_PRESETS, aspect_ratio_name, normalize_resolution
from tools.hardware_catalog import HardwareCatalog, HardwareEntry
from tools.api_settings import (
    LocalAPISettingsError,
    clear_saved_api_config,
    load_saved_api_config,
    save_saved_api_config,
)
from agent.planner import GENERIC_OPTIMIZATION_DIRECTIONS, plan_candidate_id, plan_next_experiment
from ui_theme import STYLESHEET
from ui_charts import RunChart


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR) / ".data"
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
    fps_cap: int | None = 0
    vsync: str = "disabled"
    display_mode: str = "fullscreen"
    shadow_quality: str = "高"
    dynamic_shadows: str = "all"
    video_settings: dict[str, str] | None = None
    is_laptop: bool = True
    gpu_power_w: int | None = 100
    graphics_mode: str = "hybrid"
    power_mode: str = "ac_performance"
    cooling_mode: str = "performance"
    # Baseline values are entered by the user.  Keep the execution marker
    # separate from candidate configuration confirmation so a typed summary
    # cannot be presented as instrument-backed evidence by default.
    baseline_repeats: int = 2
    baseline_duration_seconds: int = 60
    baseline_confirmed: bool = False
    baseline_runs: list[dict[str, float]] | None = None

    def __post_init__(self):
        if self.video_settings is None:
            self.video_settings = {
                "video_preset": "custom", "player_contrast": "enabled",
                "menu_fps_cap": "0", "msaa_mode": "4x_msaa",
                "texture_detail": "medium", "texture_filtering": "anisotropic_16x",
                "shader_detail": "low", "particle_detail": "low",
                "ambient_occlusion": "disabled", "hdr": "quality",
                "fsr": "disabled", "nvidia_reflex": "enabled",
                "nvidia_gsync": "not_available", "amd_anti_lag": "not_available",
            }


class MeasurementSpinBox(QSpinBox):
    """Keep missing measurements empty so hint text never blocks numeric typing."""

    def textFromValue(self, value: int) -> str:
        return "" if value == self.minimum() else super().textFromValue(value)

    def valueFromText(self, text: str) -> int:
        return self.minimum() if not text.strip() else super().valueFromText(text)

    def validate(self, text: str, position: int):
        if not text.strip():
            return QValidator.State.Acceptable, text, position
        return super().validate(text, position)


class RoundedComboBox(QComboBox):
    """Clip the popup window as well as painting a rounded list border."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, self,
        )
        center = QRectF(arrow_rect).center()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#756f66" if self.isEnabled() else "#b3aa9e")
        painter.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath()
        path.moveTo(center.x() - 4, center.y() - 2)
        path.lineTo(center.x(), center.y() + 2)
        path.lineTo(center.x() + 4, center.y() - 2)
        painter.drawPath(path)

    def showPopup(self):
        view = self.view()
        popup = view.window()
        # The native popup frame/shadow sits outside the rounded list border.
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        if isinstance(popup, QFrame):
            popup.setFrameShape(QFrame.Shape.NoFrame)
        view.setFrameShape(QFrame.Shape.NoFrame)
        popup.setObjectName("comboPopup")
        popup.setStyleSheet("QWidget#comboPopup { background: #ffffff; border: none; border-radius: 10px; }")
        popup.installEventFilter(self)
        super().showPopup()
        self._round_popup(popup)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            if watched is self.view().window():
                self._round_popup(watched)
        return super().eventFilter(watched, event)

    @staticmethod
    def _round_popup(popup: QWidget):
        path = QPainterPath()
        path.addRoundedRect(QRectF(popup.rect()), 10, 10)
        popup.setMask(QRegion(path.toFillPolygon().toPolygon()))


class ActualSettingInput(RoundedComboBox):
    """A selectable setting editor with QLineEdit-compatible text helpers."""

    def __init__(self, value: str, options: list[str]):
        super().__init__()
        self.setEditable(not bool(options))
        if options:
            self.addItems(options)
        self.setText(value)
        if self.isEditable() and self.lineEdit() is not None:
            self.lineEdit().setPlaceholderText("填写你实际使用的值")

    def text(self) -> str:
        return self.currentText()

    def setText(self, value: str) -> None:
        index = self.findText(value)
        if index >= 0:
            self.setCurrentIndex(index)
        elif self.isEditable():
            self.setEditText(value)


class EditableChoiceInput(ActualSettingInput):
    """Preserve text editing while offering known values and custom entries."""

    textChanged = Signal(str)

    def __init__(self, value: str, options: list[str]):
        super().__init__(value, options)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(12)
        self.setText(value)
        self.currentTextChanged.connect(self.textChanged.emit)

    def selectAll(self):
        self.lineEdit().selectAll()

    def clear(self):
        self.clearEditText()

    def setPlaceholderText(self, text: str):
        self.lineEdit().setPlaceholderText(text)



def set_margins(layout, left=0, top=0, right=0, bottom=0, spacing=0):
    layout.setContentsMargins(left, top, right, bottom)
    layout.setSpacing(spacing)
    return layout


class TransitionOverlay(QWidget):
    def __init__(self, snapshot: QPixmap, parent: QWidget):
        super().__init__(parent)
        self._snapshot = snapshot
        self._opacity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def _set_opacity(self, value: float):
        self._opacity = value
        self.update()

    opacity = Property(float, lambda self: self._opacity, _set_opacity)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(self.rect(), self._snapshot)


def fade_stack_to(stack: QStackedWidget | CurrentPageStack, index: int, parent: QObject, duration: int = 165) -> None:
    if not 0 <= index < stack.count() or stack.currentIndex() == index:
        return
    # Capture the currently composited frame before cancelling an interrupted fade.
    snapshot = stack.grab() if stack.isVisible() else None
    current = getattr(stack, "_fade_animation", None)
    if current is not None:
        current.stop()
        current.deleteLater()
    old_overlay = getattr(stack, "_fade_overlay", None)
    if old_overlay is not None:
        old_overlay.hide()
        old_overlay.deleteLater()
    stack._fade_animation = None
    stack._fade_overlay = None
    # Animate a snapshot, not the live scroll areas and their child effects.
    stack.setCurrentIndex(index)
    if snapshot is None:
        return
    overlay = TransitionOverlay(snapshot, stack)
    overlay.setGeometry(stack.rect())
    overlay.show()
    overlay.raise_()
    animation = QPropertyAnimation(overlay, b"opacity", parent)
    animation.setDuration(duration)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    stack._fade_animation = animation
    stack._fade_overlay = overlay

    def finish():
        if stack._fade_animation is animation:
            stack._fade_animation = None
            stack._fade_overlay = None
        overlay.hide()
        overlay.deleteLater()
        animation.deleteLater()

    animation.finished.connect(finish)
    animation.start()


class CurrentPageStack(QWidget):
    """Only the active form participates in layout and height-for-width sizing."""

    currentChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self._pages: list[QWidget] = []
        self._index = -1
        self._layout = set_margins(QVBoxLayout(self))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def addWidget(self, widget: QWidget) -> int:
        widget.setParent(self)
        widget.hide()
        self._pages.append(widget)
        if self._index < 0:
            self.setCurrentIndex(0)
        return len(self._pages) - 1

    def count(self) -> int:
        return len(self._pages)

    def currentIndex(self) -> int:
        return self._index

    def currentWidget(self) -> QWidget | None:
        return self._pages[self._index] if self._index >= 0 else None

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < self.count() or index == self._index:
            return
        previous = self.currentWidget()
        if previous is not None:
            self._layout.removeWidget(previous)
            previous.hide()
        self._index = index
        current = self.currentWidget()
        self._layout.addWidget(current)
        current.show()
        self._layout.invalidate()
        self.updateGeometry()
        self.currentChanged.emit(index)


def animate_visibility(widget: QWidget, visible: bool, duration: int = 180) -> None:
    target = bool(visible)
    current_target = getattr(widget, "_visibility_target", not widget.isHidden())
    if target == current_target and (target == (not widget.isHidden()) or getattr(widget, "_visibility_animation", None)):
        return
    previous = getattr(widget, "_visibility_animation", None)
    if previous is not None:
        previous.stop()
        previous.deleteLater()
    widget._visibility_animation = None

    widget._visibility_target = target
    if not widget.parentWidget() or not widget.parentWidget().isVisible():
        limits = getattr(widget, "_visibility_limits", None)
        if limits is not None:
            widget.setMinimumHeight(limits[0])
            widget.setMaximumHeight(limits[1])
        widget.setVisible(target)
        widget._visibility_animation = None
        return
    limits = getattr(widget, "_visibility_limits", (widget.minimumHeight(), widget.maximumHeight()))
    widget._visibility_limits = limits
    widget.setMinimumHeight(0)
    if target:
        if widget.isHidden():
            widget.setMaximumHeight(0)
            widget.show()
        layout = widget.layout()
        if layout is not None:
            layout.activate()
        start = min(widget.maximumHeight(), max(0, widget.height()))
        end = min(limits[1], max(limits[0], widget.sizeHint().height(), widget.minimumSizeHint().height(), 1))
    else:
        if widget.isHidden():
            widget.setMinimumHeight(limits[0])
            widget.setMaximumHeight(limits[1])
            return
        start = min(widget.maximumHeight(), widget.height())
        end = 0

    animation = QPropertyAnimation(widget, b"maximumHeight", widget)
    animation.setDuration(duration)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def finish():
        if getattr(widget, "_visibility_animation", None) is not animation:
            return
        if not target:
            widget.hide()
        widget.setMinimumHeight(limits[0])
        widget.setMaximumHeight(limits[1])
        widget._visibility_animation = None
        animation.deleteLater()

    widget._visibility_animation = animation
    animation.finished.connect(finish)
    animation.start()


class BusyIndicator(QWidget):
    def __init__(self, color: str = "#a9513c", parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self._angle = 0
        self.setFixedSize(18, 18)
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._advance)
        self.hide()

    def set_running(self, running: bool):
        if running:
            self._angle = 0
            self._timer.start()
            self.show()
        else:
            self._timer.stop()
            self.hide()

    def _advance(self):
        self._angle = (self._angle + 24) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._color, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(3, 3, 12, 12), self._angle * 16, 255 * 16)


class ToastNotice(QFrame):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toastNotice")
        self.setProperty("variant", "success")
        row = set_margins(QHBoxLayout(self), 15, 11, 16, 11, 8)
        self.message = label("", "toastText", True)
        row.addWidget(self.message)
        self.setMinimumWidth(270)
        self.setMaximumWidth(430)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._animation = QPropertyAnimation(self._opacity, b"opacity", self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._opacity_finished)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, message: str, variant: str = "success"):
        self._hide_timer.stop()
        self._animation.stop()
        self.setProperty("variant", variant if variant in {"success", "info", "error"} else "info")
        self.style().unpolish(self)
        self.style().polish(self)
        self.message.setText(message)
        self.adjustSize()
        self._position()
        if self.isHidden():
            self._opacity.setOpacity(0.0)
        self.show()
        self.raise_()
        self._animate_opacity(1.0, 160)
        self._hide_timer.start(2400)

    def _position(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(max(16, parent.width() - self.width() - 24), 20)

    def _animate_opacity(self, target: float, duration: int):
        self._animation.stop()
        self._animation.setDuration(duration)
        self._animation.setStartValue(self._opacity.opacity())
        self._animation.setEndValue(target)
        self._animation.start()

    def _opacity_finished(self):
        if self._opacity.opacity() == 0.0:
            self.hide()

    def _fade_out(self):
        self._animate_opacity(0.0, 180)


def label(text: str, object_name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


ADVANCED_SETTING_LABELS = {
    "msaa_mode": {"none": "无", "cmaa2": "CMAA2 抗锯齿", "2x_msaa": "2x MSAA", "4x_msaa": "4x MSAA", "8x_msaa": "8x MSAA"},
    "texture_detail": {"low": "低", "medium": "中", "high": "高"},
    "texture_filtering": {"bilinear": "双线性", "trilinear": "三线性", "anisotropic_2x": "异向 2X", "anisotropic_4x": "异向 4X", "anisotropic_8x": "异向 8X", "anisotropic_16x": "异向 16X"},
    "shader_detail": {"low": "低", "high": "高"},
    "particle_detail": {"low": "低", "medium": "中", "high": "高", "very_high": "非常高"},
    "ambient_occlusion": {"disabled": "已禁用", "medium": "中", "high": "高"},
    "hdr": {"quality": "品质", "performance": "性能"},
    "fsr": {"disabled": "已禁用（最高品质）", "ultra_quality": "超高品质", "quality": "品质", "balanced": "均衡", "performance": "性能"},
}
ADVANCED_SETTING_OPTIONS = {
    "msaa_mode": ["none", "cmaa2", "2x_msaa", "4x_msaa", "8x_msaa"],
    "texture_detail": ["low", "medium", "high"],
    "texture_filtering": ["bilinear", "trilinear", "anisotropic_2x", "anisotropic_4x", "anisotropic_8x", "anisotropic_16x"],
    "shader_detail": ["low", "high"],
    "particle_detail": ["low", "medium", "high", "very_high"],
    "ambient_occlusion": ["disabled", "medium", "high"],
    "hdr": ["quality", "performance"],
    "fsr": ["disabled", "ultra_quality", "quality", "balanced", "performance"],
}


def setting_value_label(setting_id: str, value: str) -> str:
    return ADVANCED_SETTING_LABELS.get(setting_id, {}).get(value, value)


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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = set_margins(QHBoxLayout(self), spacing=12)
        title_label = label(title, "sectionTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        meta_label = label(meta, "sectionMeta", True)
        meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Keep the font's ascent/descent inside the row.  Without a little
        # vertical room, some Windows fonts render only the top of Chinese
        # glyphs when a card is laid out at its minimum height.
        self.setMinimumHeight(24)
        row.addWidget(title_label)
        row.addStretch()
        row.addWidget(meta_label)


class PageHeader(QWidget):
    def __init__(self, eyebrow: str, title: str, subtitle: str, status: str = ""):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = set_margins(QHBoxLayout(self), spacing=20)
        text_area = QWidget()
        text_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        text_col = set_margins(QVBoxLayout(text_area), spacing=4)
        self.setToolTip(eyebrow)
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
        self._progress_value = max(0, min(progress, 100))
        layout = set_margins(QVBoxLayout(self), 15, 13, 15, 13, 7)
        layout.addWidget(label(title, "cardKicker"))
        layout.addWidget(label(value, "metricValue"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        layout.addWidget(label(note, "metricNote"))
        self._progress_animation = QPropertyAnimation(self.progress_bar, b"value", self)
        self._progress_animation.setDuration(330)
        self._progress_animation.setStartValue(0)
        self._progress_animation.setEndValue(self._progress_value)
        self._progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._progress_started = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._progress_started:
            self._progress_started = True
            self._progress_animation.start()


class RuleRow(QWidget):
    def __init__(self, name: str, value: str, name_width: int = 82):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = set_margins(QHBoxLayout(self), 0, 4, 0, 4, 9)
        name_label = label(name, "ruleName")
        name_label.setFixedWidth(name_width)
        name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        value_label = label(value, "ruleText", True)
        value_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(name_label)
        row.addWidget(value_label, 1)
        # The layout adds 4 px above and below the text.  Honour the wrapped
        # label's natural height so a two-line value is never squeezed into
        # the one-line minimum used by QWidget.
        self.setMinimumHeight(max(19, value_label.sizeHint().height() + 8))


class GuidancePanel(QFrame):
    def __init__(self, title: str, text: str):
        super().__init__()
        self.setObjectName("guidePanel")
        layout = set_margins(QVBoxLayout(self), 16, 12, 16, 12, 5)
        layout.addWidget(label(title, "guideTitle", True))
        layout.addWidget(label(text, "guideText", True))


class UsageGuideDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("GameTune 使用说明")
        self.setObjectName("usageGuide")
        self.resize(680, 580)
        layout = set_margins(QVBoxLayout(self), 20, 20, 20, 20, 12)
        layout.addWidget(label("让每次设置变化都有测试依据", "sectionTitle", True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = set_margins(QVBoxLayout(body), 0, 0, 10, 0, 12)
        for title, text in [
            ("1. 记录环境", "在“新建调校”记录设备、游戏内设置、测试场景和目标。下一步才测基线，创建前无需知道帧率。"),
            ("2. 建立基线", "同一场景、相同设置先测 2 次，每次至少 60 秒。波动超过 5% 时补测第 3 次。填写实际结果并确认测试条件。"),
            ("3. 按实际设置继续", "建议可供参考，也可以同时调整多项画质。按实际配置录入后，程序会评估整组表现并更新后续建议；单项诊断只在满足对应条件时使用。"),
            ("4. 录入与恢复", "在“当前实验”导入 CSV 或填写测量值，核对实际设置。优化候选先测 1 次，没有明显退步时继续累计到 3 次；诊断参考按页面要求复测。出现恢复提示时，先在游戏里恢复，再确认。"),
            ("5. 查看已验证结果", "“测试记录”保存每次测量和执行状态；“配置与报告”显示已验证配置。平均 FPS 反映整体流畅度，1% Low 帮助观察慢帧表现；GPU 占用只作辅助证据。"),
            ("模型连接是可选项", "模型可以解释结果、从校验过的候选中选择建议。指标、有效性与保留或回滚由程序判断。连接失败时使用本地规则；记录保存在本机。"),
        ]:
            content.addWidget(GuidancePanel(title, text))
        content.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        close_button = QPushButton("知道了")
        close_button.setObjectName("primaryButton")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)


class WorkflowStrip(QWidget):
    """A presentation of the real workflow phase, never a state transition."""

    def __init__(self, state: TaskState, plan: ExperimentPlan | None):
        super().__init__()
        row = set_margins(QHBoxLayout(self), 0, 9, 0, 9, 12)
        if state.step == "优化完成":
            active = 3
        elif "恢复" in state.step or "回滚" in state.step:
            active = 3
        elif plan and plan.action == AgentAction.REQUEST_DATA:
            active = 0
        else:
            active = 1
        for index, title in enumerate(("基线测试", "调整与复测", "结果比较", "保留或恢复")):
            step = label(f"{'✓' if index < active else index + 1}  {title}",
                         "settingNew" if index == active else "sectionMeta")
            step.setAccessibleName(f"{title}，{'当前阶段' if index == active else '已完成' if index < active else '后续阶段'}")
            row.addWidget(step)
            if index < 3:
                row.addWidget(hline(), 1)


class DashboardPage(QScrollArea):
    experiment_requested = Signal()
    task_requested = Signal()

    def __init__(self, state: TaskState, evidence: list[Evidence] | None = None,
                 plan: ExperimentPlan | None = None, record: BenchmarkRecord | None = None):
        super().__init__()
        self.state = state
        self.evidence = evidence or []
        self.plan = plan
        self.record = record
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 40, 30, 40, 36, 20)
        if state.has_task:
            root.addWidget(label(state.game, "eyebrow"))
        root.addWidget(PageHeader("当前调校", "让每一次调整，都有依据" if state.has_task else "让游戏运行得更从容",
                                  f"{state.name} · {state.step}" if state.has_task else "记录当前表现，再逐项验证值得尝试的调整。",
                                  state.step if state.has_task else "尚未开始"))
        if not state.has_task:
            root.addSpacing(30)
            welcome = QFrame()
            welcome.setObjectName("welcomePanel")
            welcome_layout = set_margins(QVBoxLayout(welcome), 32, 36, 32, 36, 18)
            welcome_layout.addWidget(label("从一次可靠的基线开始", "heroTitle", True))
            welcome_layout.addWidget(label("记录电脑配置与游戏设置，再在同一场景完成基线测试。保存后，这里会显示真实的性能数据、判断依据和下一步建议。", "ruleText", True))
            self.create_button = QPushButton("新建调校")
            self.create_button.setObjectName("primaryButton")
            self.create_button.clicked.connect(self.task_requested)
            welcome_layout.addWidget(self.create_button, 0, Qt.AlignmentFlag.AlignLeft)
            root.addWidget(welcome)
            root.addWidget(GuidancePanel("测试前准备", "使用同一种采集工具、同一段游戏场景和相同设置。基线先测 2 次，波动超过 5% 时补测第 3 次。"))
            root.addWidget(GuidancePanel("已有任务？", "到“测试记录”选择以前保存的任务并打开，继续上次的测试。结果不足时，程序会提示补测或补充设置。"))
            root.addStretch()
            return
        root.addWidget(WorkflowStrip(state, plan))
        root.addWidget(self._measurement_card())
        self._work_columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._work_columns.setSpacing(24)
        action = QWidget()
        action_layout = set_margins(QVBoxLayout(action), spacing=8)
        action_layout.addWidget(SectionHeader("本次调整", "建议尚未代表已执行"))
        action_layout.addWidget(self._action_card())
        action_layout.addStretch()
        self._work_columns.addWidget(action, 3)
        self._work_columns.addWidget(self._environment_card(), 2)
        root.addLayout(self._work_columns)
        self.evidence_button = QPushButton("展开分析依据")
        self.evidence_button.setObjectName("evidenceButton")
        self.evidence_button.setCheckable(True)
        root.addWidget(self.evidence_button)
        self.evidence_panel = self._hero()
        self.evidence_panel.hide()
        root.addWidget(self.evidence_panel)
        self.evidence_button.toggled.connect(self._toggle_evidence)
        root.addStretch()

    def _toggle_evidence(self, checked: bool):
        self.evidence_panel.setVisible(checked)
        self.evidence_button.setText("收起分析依据" if checked else "展开分析依据")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_work_columns"):
            self._work_columns.setDirection(QBoxLayout.Direction.TopToBottom if self.viewport().width() < 850
                                            else QBoxLayout.Direction.LeftToRight)

    def _measurement_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("measurementCard")
        layout = set_margins(QVBoxLayout(card), 22, 18, 22, 16, 10)
        layout.addWidget(SectionHeader("当前测试表现", "逐次帧率 / FPS"))
        legend = QHBoxLayout()
        for text, color in (("平均帧率", "#607e99"), ("1% Low", "#a9513c")):
            item = label(f"▪  {text}", "sectionMeta")
            item.setStyleSheet(f"color: {color};")
            legend.addWidget(item)
        legend.addStretch()
        layout.addLayout(legend)
        self.run_chart = RunChart(self.record.runs if self.record else [])
        layout.addWidget(self.run_chart)
        layout.addWidget(hline())
        metrics = QHBoxLayout()
        record = self.record
        values = [("平均帧率", f"{record.avg_fps:g} FPS" if record else f"{self.state.avg_fps} FPS"),
                  ("1% Low", f"{record.low_fps:g} FPS" if record else f"{self.state.low_fps} FPS"),
                  ("单次测试时长", f"{record.duration_seconds} 秒" if record and record.duration_seconds else "未记录")]
        for title, value in values:
            group = QWidget()
            column = set_margins(QVBoxLayout(group), 8, 4, 8, 4, 3)
            for text, name in ((title, "cardKicker"), (value, "metricValue")):
                item = label(text, name)
                item.setAlignment(Qt.AlignmentFlag.AlignCenter)
                column.addWidget(item)
            metrics.addWidget(group, 1)
        layout.addLayout(metrics)
        layout.addWidget(label("图表按实际测试次数展示；汇总值使用当前记录，不表示连续帧时间。", "metricNote", True))
        return card

    def _hero(self) -> QFrame:
        card = QFrame()
        card.setObjectName("heroCard")
        card.setMinimumHeight(220)
        layout = set_margins(QVBoxLayout(card), 22, 18, 22, 18, 9)
        layout.addWidget(label("当前判断", "cardKicker"))
        hypothesis, _ = dominant_hypothesis(self.evidence)
        names = {"cpu": "更可能受 CPU 限制", "cpu_or_other": "GPU 渲染负担可能不是主要限制", "gpu": "更可能受 GPU 限制", "frametime": "帧率稳定性需要改善", "platform": "笔记本平台状态可能影响性能", "insufficient": "数据还不够，先补充测试"}
        layout.addWidget(label(names.get(hypothesis, "需要补充证据"), "heroTitle", True))
        body_text = "；".join(item.detail for item in self.evidence[:2]) or "创建任务后，这里会显示最重要的两条判断依据。"
        layout.addWidget(label(body_text, "heroBody", True))
        if self.plan:
            if self.plan.selection_reason:
                layout.addWidget(RuleRow("选择理由", self.plan.selection_reason))
            for name, text in (("为什么做", self.plan.hypothesis), ("怎么测试", self.plan.method),
                               ("有效标准", self.plan.success_criteria)):
                layout.addWidget(hline())
                layout.addWidget(RuleRow(name, text))
        tags = QHBoxLayout()
        tags.setSpacing(7)
        for item in self.evidence[:3]:
            tags.addWidget(label(item.title, "tagOrange" if item.supports in {"insufficient", "cpu_or_other"} else "tagGreen"))
        tags.addStretch()
        layout.addLayout(tags)
        layout.addStretch()
        confidence = label(f"证据支持：{evidence_support_level(self.evidence)}", "confidence")
        confidence.setFixedWidth(190)
        layout.addWidget(confidence)
        return card

    def _action_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("actionCard")
        layout = set_margins(QVBoxLayout(card), 0, 6, 0, 12, 10)
        plan = self.plan
        if plan:
            purpose = "诊断测试" if plan.purpose == "diagnostic" else "优化验证"
            source = "AI 按本轮证据选择" if plan.planning_source == "model" else "本地规则建议"
            kicker = f"{purpose} {plan.round_number:02d} · {source}"
        else:
            kicker = "等待任务分析"
        layout.addWidget(label(kicker, "cardKicker"))
        title_row = QHBoxLayout()
        title_row.addWidget(label(plan.title if plan else "创建任务后生成下一步", "actionTitle", True))
        title_row.addStretch()
        layout.addLayout(title_row)
        settings = QHBoxLayout()
        settings.addWidget(label(setting_value_label(plan.setting_id, plan.old_value) if plan else "当前值", "settingOld"))
        settings.addWidget(label("→", "sectionMeta"))
        settings.addWidget(label(setting_value_label(plan.setting_id, plan.new_value) if plan else "待生成", "settingNew"))
        settings.addStretch()
        layout.addLayout(settings)
        layout.addWidget(label(plan.hypothesis if plan else "当前没有待执行建议，可查看配置与报告。", "ruleText", True))
        buttons = QHBoxLayout()
        primary = QPushButton("补充当前设置" if plan and plan.action == AgentAction.REQUEST_DATA else "选择验证目标" if plan and plan.setting_id == "uncapped_validation" else "录入测试结果")
        primary.setObjectName("primaryButton")
        primary.clicked.connect(self.experiment_requested)
        secondary = QPushButton("查看测试步骤")
        secondary.setObjectName("secondaryButton")
        secondary.clicked.connect(self._show_steps)
        buttons.addWidget(primary)
        buttons.addWidget(secondary)
        buttons.addStretch()
        layout.addLayout(buttons)
        return card

    def _environment_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("actionCard")
        layout = set_margins(QVBoxLayout(card), 18, 16, 18, 16, 4)
        layout.addWidget(label("测试条件", "sectionTitle"))
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

    def _mode_name(self) -> str:
        return {"competitive": "竞技模式", "balanced": "平衡模式", "visual": "视觉模式"}.get(self.state.mode, "竞技模式")


class NewTaskPage(QScrollArea):
    task_created = Signal(object)
    notice_requested = Signal(str, str)

    def __init__(self, state: TaskState):
        super().__init__()
        self.state = state
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        root.addWidget(PageHeader("任务设置", "新建调校", "先记录设备与目标。下一步才采集基线，不需要提前知道帧率。"))
        root.addWidget(GuidancePanel("建立可比较的起点", "按游戏内当前设置填写；不确定的选项如实标记。硬件型号用于记录设备，性能是否受限由后续实测判断。"))
        self.setup_stage = QWidget()
        self.stage_stack = CurrentPageStack()
        self.stage_stack.addWidget(self.setup_stage)
        root.addWidget(self.stage_stack)
        setup_layout = set_margins(QVBoxLayout(self.setup_stage), spacing=18)
        columns = QHBoxLayout()
        self._columns = columns
        columns.setSpacing(15)
        basic_card, basic_form = self._form_card("设备与测试环境")
        goal_card, goal_form = self._form_card("优化目标")
        columns.addWidget(basic_card, 1, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(goal_card, 1, Qt.AlignmentFlag.AlignTop)
        setup_layout.addLayout(columns)
        self.name_input = QLineEdit(state.name)
        self.game_input = RoundedComboBox()
        # Keep unsupported titles out of the selectable task domain until a
        # corresponding rule set and validator are shipped.
        self.game_input.addItem("Counter-Strike 2")
        cpu_field, self.cpu_input, self.custom_cpu_input = self._hardware_field("cpu", state.cpu, state.is_laptop)
        gpu_field, self.gpu_input, self.custom_gpu_input = self._hardware_field("gpu", state.gpu, state.is_laptop)
        self.resolution_input = RoundedComboBox()
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
        self.refresh_input = RoundedComboBox()
        self.refresh_input.addItems(["300 Hz", "240 Hz", "165 Hz", "144 Hz"])
        self.refresh_input.setEditable(True)
        self.refresh_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.refresh_input.setCurrentText(state.refresh_rate)
        self.scene_input = QLineEdit(state.test_scene)
        self.display_mode_input = RoundedComboBox()
        self.display_mode_input.addItem("全屏", "fullscreen")
        self.display_mode_input.addItem("全屏窗口模式", "borderless")
        self.display_mode_input.addItem("窗口模式", "windowed")
        self.display_mode_input.setCurrentIndex(max(0, self.display_mode_input.findData(state.display_mode)))
        self.cap_input = self._spin(-1, 1000, state.fps_cap if state.fps_cap is not None else -1, 10, placeholder="未确认")
        self.cap_input.setToolTip("填写游戏内“最大 FPS”；0 表示不限制")
        self.vsync_input = RoundedComboBox()
        self.vsync_input.addItem("已禁用", "disabled")
        self.vsync_input.addItem("已启用", "enabled")
        self.vsync_input.addItem("未确认", "unknown")
        self.vsync_input.setCurrentIndex(max(0, self.vsync_input.findData(state.vsync)))
        self.shadow_quality_input = RoundedComboBox()
        self.shadow_quality_input.addItems(["低", "中", "高", "非常高", "未确认"])
        self.shadow_quality_input.setCurrentText(state.shadow_quality if state.shadow_quality != "unknown" else "未确认")
        self.dynamic_shadows_input = RoundedComboBox()
        self.dynamic_shadows_input.addItem("全部", "all")
        self.dynamic_shadows_input.addItem("仅限日光", "sun_only")
        self.dynamic_shadows_input.addItem("未确认", "unknown")
        self.dynamic_shadows_input.setCurrentIndex(max(0, self.dynamic_shadows_input.findData(state.dynamic_shadows)))
        for name, widget in [("任务名称", self.name_input), ("游戏", self.game_input), ("处理器", cpu_field), ("显卡", gpu_field), ("分辨率（常用候选）", resolution_field), ("显示模式", self.display_mode_input), ("刷新率", self.refresh_input), ("测试场景", self.scene_input)]:
            basic_form.addRow(name, widget)
        self.target_avg = self._spin(30, 1000, state.target_avg, 10)
        self.target_low = self._spin(30, 1000, state.target_low, 10)
        self.mode_input = RoundedComboBox()
        self.mode_input.addItems(["竞技模式 · 流畅优先", "平衡模式", "视觉模式 · 画质优先"])
        for name, widget in [("平均 FPS 目标", self.target_avg), ("1% Low 目标", self.target_low), ("优化模式", self.mode_input)]:
            goal_form.addRow(name, widget)
        self.laptop_card, laptop_form = self._form_card("笔记本运行状态")
        self.gpu_power_input = self._spin(0, 250, state.gpu_power_w or 0, 5, placeholder="未知（可不填）")
        self.graphics_mode_input = RoundedComboBox()
        self.graphics_mode_input.addItem("独显直连 / dGPU only", "dgpu")
        self.graphics_mode_input.addItem("混合输出 / Optimus", "hybrid")
        self.graphics_mode_input.addItem("Advanced Optimus / 自动切换", "advanced_optimus")
        self.graphics_mode_input.addItem("不确定", "unknown")
        self.power_mode_input = RoundedComboBox()
        self.power_mode_input.addItem("接通电源 · 性能模式", "ac_performance")
        self.power_mode_input.addItem("接通电源 · 平衡模式", "ac_balanced")
        self.power_mode_input.addItem("电池供电", "battery")
        self.power_mode_input.addItem("不确定", "unknown")
        self.cooling_mode_input = RoundedComboBox()
        self.cooling_mode_input.addItem("性能 / 狂暴模式", "performance")
        self.cooling_mode_input.addItem("平衡模式", "balanced")
        self.cooling_mode_input.addItem("安静模式", "quiet")
        self.cooling_mode_input.addItem("不确定", "unknown")
        for combo, value in ((self.graphics_mode_input, state.graphics_mode), (self.power_mode_input, state.power_mode), (self.cooling_mode_input, state.cooling_mode)):
            combo.setCurrentIndex(max(0, combo.findData(value)))
        laptop_form.addRow("测试时 GPU 功耗（W）", self.gpu_power_input)
        laptop_form.addRow("显卡输出方式", self.graphics_mode_input)
        laptop_form.addRow("供电模式", self.power_mode_input)
        laptop_form.addRow("风扇 / 散热模式", self.cooling_mode_input)
        setup_layout.addWidget(self.laptop_card)
        self._update_laptop_fields(animate=False)
        advanced_card, advanced_form = self._form_card("CS2 高级视频设置")
        self.advanced_video_inputs: dict[str, QComboBox] = {}
        advanced_definitions = [
            ("video_preset", "当前视频值预设", [("自定义", "custom"), ("低", "low"), ("中", "medium"), ("高", "high"), ("非常高", "very_high")]),
            ("player_contrast", "增强角色对比度", [("已启用", "enabled"), ("已禁用", "disabled")]),
            ("nvidia_gsync", "NVIDIA G-Sync", [("已禁用", "disabled"), ("已启用", "enabled"), ("此设备不可用", "not_available")]),
            ("nvidia_reflex", "NVIDIA Reflex 低延迟", [("已禁用", "disabled"), ("已启用", "enabled"), ("已启用 + 加速", "enabled_boost"), ("此设备不可用", "not_available")]),
            ("amd_anti_lag", "AMD Anti-Lag 2.0", [("已禁用", "disabled"), ("已启用", "enabled"), ("此设备不可用", "not_available")]),
            ("msaa_mode", "多重采样抗锯齿模式", [("无", "none"), ("CMAA2 抗锯齿", "cmaa2"), ("2x MSAA", "2x_msaa"), ("4x MSAA", "4x_msaa"), ("8x MSAA", "8x_msaa")]),
            ("texture_detail", "模型/贴图细节", [("低", "low"), ("中", "medium"), ("高", "high")]),
            ("texture_filtering", "贴图过滤模式", [("双线性", "bilinear"), ("三线性", "trilinear"), ("异向 2X", "anisotropic_2x"), ("异向 4X", "anisotropic_4x"), ("异向 8X", "anisotropic_8x"), ("异向 16X", "anisotropic_16x")]),
            ("shader_detail", "光影细节", [("低", "low"), ("高", "high")]),
            ("particle_detail", "粒子细节", [("低", "low"), ("中", "medium"), ("高", "high"), ("非常高", "very_high")]),
            ("ambient_occlusion", "环境光遮蔽", [("已禁用", "disabled"), ("中", "medium"), ("高", "high")]),
            ("hdr", "高动态范围", [("品质", "quality"), ("性能", "performance")]),
            ("fsr", "Fidelity FX 超级分辨率", [("已禁用（最高品质）", "disabled"), ("超高品质", "ultra_quality"), ("品质", "quality"), ("均衡", "balanced"), ("性能", "performance")]),
        ]
        for key, name, options in advanced_definitions:
            combo = RoundedComboBox()
            for text, value in options:
                combo.addItem(text, value)
            current = (state.video_settings or {}).get(key, options[0][1])
            combo.setCurrentIndex(max(0, combo.findData(current)))
            self.advanced_video_inputs[key] = combo
            advanced_form.addRow(name, combo)
        self.menu_cap_input = self._spin(0, 1000, int((state.video_settings or {}).get("menu_fps_cap", "0")), 10)
        advanced_form.addRow("菜单最高FPS", self.menu_cap_input)
        advanced_form.addRow("游戏最高FPS", self.cap_input)
        advanced_form.addRow("V-Sync", self.vsync_input)
        advanced_form.addRow("全局阴影效果", self.shadow_quality_input)
        advanced_form.addRow("动态阴影", self.dynamic_shadows_input)
        setup_layout.addWidget(advanced_card)
        setup_layout.addWidget(label("电脑性能要测过才知道；不清楚的设备选项可以选“不确定”。", "metricNote", True))
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.begin_baseline_button = QPushButton("记录环境并进入基线测试")
        self.begin_baseline_button.setObjectName("primaryButton")
        self.begin_baseline_button.setMinimumWidth(230)
        self.begin_baseline_button.clicked.connect(self._begin_baseline)
        buttons.addWidget(self.begin_baseline_button)
        setup_layout.addLayout(buttons)
        self.baseline_stage = QWidget()
        baseline_layout = set_margins(QVBoxLayout(self.baseline_stage), spacing=16)
        baseline_layout.addWidget(PageHeader(
            "基线采集",
            "先测试，再录入结果",
            "保持同一场景与设置，先测 2 次，每次至少 60 秒。波动超过 5% 时，页面会提示补测第 3 次。",
        ))
        baseline_card, baseline_form = self._form_card("逐次测试结果")
        self.baseline_run_inputs: list[dict[str, QSpinBox]] = []
        self.baseline_run_rows: list[QWidget] = []
        self.baseline_run_labels: list[QWidget] = []
        for index in range(3):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            avg_input = self._spin(0, 2000, 0, placeholder="未填写")
            low_input = self._spin(0, 2000, 0, placeholder="未填写")
            gpu_input = self._spin(0, 100, 0, placeholder="未记录（可不填）")
            row_layout.addWidget(QLabel("平均 FPS"))
            row_layout.addWidget(avg_input, 1)
            row_layout.addWidget(QLabel("1% Low"))
            row_layout.addWidget(low_input, 1)
            row_layout.addWidget(QLabel("GPU 占用 %"))
            row_layout.addWidget(gpu_input, 1)
            baseline_form.addRow(f"第 {index + 1} 次", row)
            self.baseline_run_labels.append(baseline_form.labelForField(row))
            self.baseline_run_inputs.append({"avg_fps": avg_input, "low_fps": low_input, "gpu_usage": gpu_input})
            self.baseline_run_rows.append(row)
            for widget in (avg_input, low_input, gpu_input):
                widget.valueChanged.connect(self._refresh_baseline_guidance)
            if index == 2:
                row.hide()
                self.baseline_run_labels[index].hide()
        self.baseline_duration_input = self._spin(60, 3600, max(60, state.baseline_duration_seconds))
        baseline_form.addRow("每次测试时长（秒）", self.baseline_duration_input)
        self.baseline_guidance = label("先填前两次的测试结果。", "metricNote", True)
        baseline_layout.addWidget(baseline_card)
        baseline_layout.addWidget(self.baseline_guidance)
        self.baseline_confirmed_input = QCheckBox("以上数据来自实际测试；测试期间场景、分辨率和其他设置保持一致")
        baseline_layout.addWidget(self.baseline_confirmed_input)
        baseline_buttons = QHBoxLayout()
        back_button = QPushButton("返回检查环境设置")
        back_button.clicked.connect(self._return_to_setup)
        baseline_buttons.addWidget(back_button)
        baseline_buttons.addStretch()
        finish_baseline_button = QPushButton("录入基线并开始分析")
        finish_baseline_button.setObjectName("primaryButton")
        finish_baseline_button.setMinimumWidth(210)
        finish_baseline_button.clicked.connect(self._finish_baseline)
        baseline_buttons.addWidget(finish_baseline_button)
        baseline_layout.addLayout(baseline_buttons)
        self.stage_stack.addWidget(self.baseline_stage)
        root.addStretch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_columns"):
            self._columns.setDirection(QBoxLayout.Direction.TopToBottom if self.viewport().width() < 900 else QBoxLayout.Direction.LeftToRight)

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, step: int = 1, *, placeholder: str = "") -> QSpinBox:
        spin = MeasurementSpinBox() if placeholder else QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        if placeholder:
            spin.lineEdit().setPlaceholderText(placeholder)
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
        animate_visibility(self.custom_resolution_input, custom)
        if custom:
            self.custom_resolution_input.setFocus()

    def _hardware_field(self, kind: str, current: str, current_is_laptop: bool):
        combo = RoundedComboBox()
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
        animate_visibility(custom_input, custom)
        if custom:
            custom_input.setFocus()
        if hasattr(self, "laptop_card"):
            self._update_laptop_fields()

    def _update_laptop_fields(self, animate: bool = True):
        entries = (self.cpu_input.currentData(), self.gpu_input.currentData())
        is_laptop = any(
            (isinstance(entry, HardwareEntry) and entry.segment == "Laptop") or entry == "custom_laptop"
            for entry in entries
        )
        if animate:
            animate_visibility(self.laptop_card, is_laptop)
        else:
            self.laptop_card.setVisible(is_laptop)
            self.laptop_card._visibility_target = is_laptop

    def _begin_baseline(self):
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "信息不完整", "请填写任务名称。")
            return
        if not self.scene_input.text().strip() or not self.refresh_input.currentText().strip():
            QMessageBox.warning(self, "信息不完整", "请填写测试场景和刷新率。")
            return
        if self.target_low.value() > self.target_avg.value():
            QMessageBox.warning(self, "目标无效", "目标 1% Low 不能高于目标平均 FPS。")
            return
        cpu_data = self.cpu_input.currentData()
        gpu_data = self.gpu_input.currentData()
        cpu_entry = cpu_data if isinstance(cpu_data, HardwareEntry) else None
        gpu_entry = gpu_data if isinstance(gpu_data, HardwareEntry) else None
        cpu_name = cpu_entry.name if cpu_entry else self.custom_cpu_input.text().strip()
        gpu_name = gpu_entry.name if gpu_entry else self.custom_gpu_input.text().strip()
        if not cpu_name or not gpu_name:
            QMessageBox.warning(self, "缺少硬件信息", "请选择处理器和显卡；如果列表中没有，请填写完整型号。")
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
            if 0 < self.gpu_power_input.value() < 20:
                QMessageBox.warning(self, "GPU 功耗无效", "GPU 功耗应在 20W 到 250W 之间；未知时请留空。")
                return
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
        self.state.mode = ("competitive", "balanced", "visual")[self.mode_input.currentIndex()]
        self.state.fps_cap = None if self.cap_input.value() < 0 else self.cap_input.value()
        self.state.fps_cap_checked = self.state.fps_cap is not None
        self.state.vsync = str(self.vsync_input.currentData())
        self.state.vsync_checked = self.state.vsync != "unknown"
        self.state.display_mode = str(self.display_mode_input.currentData())
        self.state.shadow_quality = self.shadow_quality_input.currentText()
        if self.state.shadow_quality == "未确认":
            self.state.shadow_quality = "unknown"
        self.state.dynamic_shadows = str(self.dynamic_shadows_input.currentData())
        self.state.video_settings = {
            key: str(combo.currentData()) for key, combo in self.advanced_video_inputs.items()
        }
        self.state.video_settings["menu_fps_cap"] = str(self.menu_cap_input.value())
        self._switch_stage(1)
        self._refresh_baseline_guidance()

    def _return_to_setup(self):
        self._switch_stage(0)

    def _switch_stage(self, index: int):
        if self.stage_stack.currentIndex() == index:
            return
        fade_stack_to(self.stage_stack, index, self)
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(0))

    def _refresh_baseline_guidance(self, *_args):
        first_two = self.baseline_run_inputs[:2]
        complete = all(
            run["avg_fps"].value() > 0 and run["low_fps"].value() > 0
            for run in first_two
        )
        if not complete:
            animate_visibility(self.baseline_run_rows[2], False)
            self.baseline_run_labels[2].hide()
            self.baseline_guidance.setText("先录入前两次实际结果；GPU 占用可不填。")
            return
        averages = [run["avg_fps"].value() for run in first_two]
        lows = [run["low_fps"].value() for run in first_two]

        def spread(values: list[int]) -> float:
            centre = median(values)
            return (max(values) - min(values)) / centre * 100 if centre else float("inf")

        needs_third = max(spread(averages), spread(lows)) > 5.0
        animate_visibility(self.baseline_run_rows[2], needs_third)
        self.baseline_run_labels[2].setVisible(needs_third)
        if needs_third:
            third_complete = (
                self.baseline_run_inputs[2]["avg_fps"].value() > 0
                and self.baseline_run_inputs[2]["low_fps"].value() > 0
            )
            status = "第 1、2 次结果相差超过 5%，请补做第 3 次。"
            if third_complete:
                status += "汇总时将采用 3 次结果的中位数。"
            self.baseline_guidance.setText(status)
        else:
            self.baseline_guidance.setText("前两次结果波动在 5% 以内，可以使用这两次结果的中位数作为基线。")

    def _finish_baseline(self):
        if not self.baseline_confirmed_input.isChecked():
            QMessageBox.warning(self, "请确认测试条件", "请确认以上数据来自实际测试，且测试条件保持一致。")
            return
        runs: list[dict[str, float]] = []
        for widgets in self.baseline_run_inputs[:2]:
            avg = widgets["avg_fps"].value()
            low = widgets["low_fps"].value()
            if avg <= 0 or low <= 0:
                QMessageBox.warning(self, "基线数据不完整", "请先填写前两次测试的平均 FPS 和 1% Low。")
                return
            if low > avg * 1.05:
                QMessageBox.warning(self, "基线数据无效", "1% Low 不应高于平均 FPS，请检查逐次录入的数据。")
                return
            run = {"avg_fps": float(avg), "low_fps": float(low)}
            gpu = widgets["gpu_usage"].value()
            if gpu > 0:
                run["gpu_usage"] = float(gpu)
            runs.append(run)

        def spread(values: list[float]) -> float:
            centre = median(values)
            return (max(values) - min(values)) / centre * 100 if centre else float("inf")

        needs_third = max(
            spread([run["avg_fps"] for run in runs]),
            spread([run["low_fps"] for run in runs]),
        ) > 5.0
        if needs_third:
            widgets = self.baseline_run_inputs[2]
            avg = widgets["avg_fps"].value()
            low = widgets["low_fps"].value()
            if avg <= 0 or low <= 0:
                QMessageBox.warning(self, "需要第 3 次基线测试", "前两次结果相差超过 5%，请完成并录入第 3 次测试。")
                return
            if low > avg * 1.05:
                QMessageBox.warning(self, "基线数据无效", "第 3 次测试的 1% Low 不应高于平均 FPS，请检查录入数据。")
                return
            run = {"avg_fps": float(avg), "low_fps": float(low)}
            gpu = widgets["gpu_usage"].value()
            if gpu > 0:
                run["gpu_usage"] = float(gpu)
            runs.append(run)

        self.state.baseline_runs = runs
        self.state.avg_fps = round(median(run["avg_fps"] for run in runs))
        self.state.low_fps = round(median(run["low_fps"] for run in runs))
        gpu_values = [run["gpu_usage"] for run in runs if "gpu_usage" in run]
        self.state.gpu_usage = round(median(gpu_values)) if gpu_values else 0
        self.state.baseline_repeats = len(runs)
        self.state.baseline_duration_seconds = self.baseline_duration_input.value()
        self.state.baseline_confirmed = True
        self.state.has_task = True
        self.state.step = "分析完成"
        self.task_created.emit(self.state)


class ExperimentPage(QScrollArea):
    result_saved = Signal(dict)
    task_requested = Signal()
    retry_diagnostic_requested = Signal()
    rollback_requested = Signal()
    notice_requested = Signal(str, str)
    overview_requested = Signal()

    def __init__(self, state: TaskState, plan: ExperimentPlan | None = None):
        super().__init__()
        self.state = state
        self.plan = plan
        self.completed = False
        self.planning = False
        self.rollback_pending = False
        self._csv_metrics: dict | None = None
        self._csv_values: tuple[int, int, int, int] | None = None
        self._loaded_csv_path = ""
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        self.overview_button = QPushButton("返回当前调校总览")
        self.overview_button.setObjectName("secondaryButton")
        self.overview_button.clicked.connect(self.overview_requested)
        root.addWidget(self.overview_button, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(label(f"{state.name} / 当前实验" if state.has_task else "新建调校 / 保存基线 / 当前实验", "eyebrow", True))
        round_number = plan.round_number if plan else state.round_number
        if plan and plan.setting_id == "uncapped_validation":
            subtitle = "选择是否在不限帧条件下继续验证，以及正式验证结束后是否恢复原限帧。此步骤不采集帧率。"
        elif plan and plan.action == AgentAction.REQUEST_DATA:
            subtitle = "填一下你现在使用的设置，这一步不用测帧率。测试环境变了也请注明。"
        elif plan and plan.action == AgentAction.FINISH:
            subtitle = "暂时没有新的测试建议；补充程序支持的游戏设置后，可以继续优化。"
        elif plan and plan.purpose == "diagnostic":
            subtitle = "这次测试是为了找出性能受限的原因。测完后，按提示改回原设置或再测一次。"
        else:
            subtitle = "先测 1 次，帧率没明显变差就继续测到累计 3 次。测试环境变了请注明。"
        entry_type = "诊断测试" if plan and plan.purpose == "diagnostic" else "优化验证"
        root.addWidget(PageHeader(entry_type, "当前实验" if not state.has_task else "选择后续验证目标" if plan and plan.setting_id == "uncapped_validation" else f"录入第 {round_number} 轮结果", "先创建任务并保存基线，再开始诊断或优化验证。" if not state.has_task else subtitle, "尚未开始" if not state.has_task else "等待选择" if plan and plan.setting_id == "uncapped_validation" else "等待录入"))
        if not state.has_task:
            root.addWidget(GuidancePanel("尚无待执行实验", "先进入“新建调校”记录环境并采集基线；已有任务可从“测试记录”打开。这里不会使用预填值代替测量结果。"))
        elif plan and plan.setting_id == "uncapped_validation":
            root.addWidget(GuidancePanel("先选择，再继续", "选择后续验证使用的限帧条件。这里的选择不会替你修改游戏设置。"))
        elif plan and plan.action == AgentAction.REQUEST_DATA:
            root.addWidget(GuidancePanel("先补齐信息", "本轮只记录实际设置，不需要重新测帧率。补充后，程序会根据已知条件继续规划。"))
        else:
            root.addWidget(GuidancePanel("按实际配置提交", "填写本次测试真正使用的设置，未执行建议也可保存。原设置复测会单独记录；计划之外的变化不会直接算作建议调整的收益。需要恢复配置时，按提示处理。"))
        self.execution_guide = label("", "guideText", True)
        root.addWidget(self.execution_guide)
        self.result_feedback = label("", "warningBox", True)
        self.result_feedback.hide()
        root.addWidget(self.result_feedback)
        self._update_execution_guide()
        columns = QHBoxLayout()
        self._columns = columns
        columns.setSpacing(15)
        input_card = QFrame()
        input_card.setObjectName("formCard")
        input_layout = set_margins(QVBoxLayout(input_card), 20, 18, 20, 20, 12)
        input_layout.addWidget(label("验证目标" if plan and plan.setting_id == "uncapped_validation" else "复测结果", "sectionTitle"))
        if plan and plan.action not in {AgentAction.REQUEST_DATA, AgentAction.FINISH}:
            input_layout.addWidget(label("每次完成测试后录入一次结果；测试次数填本次提交包含的次数，程序会累计同配置记录。CSV 与手填任选一种。", "metricNote", True))
        file_row = QHBoxLayout()
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("导入 PresentMon CSV（可选）")
        self.file_input.returnPressed.connect(self._import_entered_file)
        self.file_input.textChanged.connect(self._csv_path_changed)
        browse = QPushButton("浏览文件")
        self.browse_button = browse
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._choose_file)
        file_row.addWidget(self.file_input, 1)
        file_row.addWidget(browse)
        input_layout.addLayout(file_row)
        form = QFormLayout()
        form.setVerticalSpacing(11)
        self.avg = NewTaskPage._spin(0, 2000, 0, placeholder="填写实测平均 FPS")
        self.low = NewTaskPage._spin(0, 2000, 0, placeholder="填写实测 1% Low")
        self.gpu = NewTaskPage._spin(0, 100, 0, placeholder="未记录（可不填）")
        self.repeats = NewTaskPage._spin(1, 10, 1)
        self.duration = NewTaskPage._spin(10, 3600, 60)
        self.condition = RoundedComboBox()
        self.condition.addItems(["与基线一致", "有变化，需要说明"])
        self.condition.setToolTip("仅按本轮计划修改设置时保持“与基线一致”；画质改动在下方如实记录，不算测试条件变化；仅场景、路线、采集方式等改变时选择“有变化”。")
        self.actual_cpu = EditableChoiceInput(state.cpu, [entry.name for entry in HARDWARE_CATALOG.entries("cpu")])
        self.actual_gpu = EditableChoiceInput(state.gpu, [entry.name for entry in HARDWARE_CATALOG.entries("gpu")])
        self.actual_resolution = EditableChoiceInput(state.resolution, list(RESOLUTION_PRESETS))
        self.actual_refresh_rate = EditableChoiceInput(state.refresh_rate, ["60 Hz", "75 Hz", "120 Hz", "144 Hz", "165 Hz", "240 Hz", "300 Hz", "360 Hz"])
        self.actual_scene = QLineEdit(state.test_scene)
        setting_editor = EditableChoiceInput if plan and plan.setting_id in {"resolution", "refresh_rate"} else ActualSettingInput
        self.actual_setting = setting_editor(
            self._setting_value_from_state(),
            self._setting_options(),
        )
        self.is_user_adjustment_request = bool(self.plan and self.plan.setting_id == "user_adjustment")
        self.adjustment_direction = RoundedComboBox()
        self.adjustment_direction.addItems(["请选择优化方向", *GENERIC_OPTIMIZATION_DIRECTIONS])
        self.adjustment_setting_name = QLineEdit()
        self.adjustment_setting_name.setPlaceholderText("例如：全局阴影效果")
        self.adjustment_current_value = QLineEdit()
        self.adjustment_current_value.setPlaceholderText("例如：高")
        self.adjustment_new_value = QLineEdit()
        self.adjustment_new_value.setPlaceholderText("例如：中")
        if self.plan and self.plan.setting_id == "user_defined" and self.actual_setting.lineEdit():
            self.actual_setting.lineEdit().setPlaceholderText(f"调整后应为：{self.plan.new_value}")
        self._actual_fields = {
            "cpu": self.actual_cpu,
            "gpu": self.actual_gpu,
            "resolution": self.actual_resolution,
            "refresh_rate": self.actual_refresh_rate,
            "test_scene": self.actual_scene,
        }
        if plan and plan.required_configuration:
            self._actual_fields["fps_cap"] = EditableChoiceInput("" if state.fps_cap is None else str(state.fps_cap), ["0", "120", "144", "240", "300", "500"])
            self._actual_fields["vsync"] = EditableChoiceInput({"disabled": "已关闭", "enabled": "已开启", "unknown": "未确认"}.get(state.vsync, "未确认"), ["已关闭", "已开启", "未确认"])
            self._actual_fields["vsync"].setPlaceholderText("填写已关闭或已开启")
            form.addRow("实际最高 FPS（0 为不限帧）", self._actual_fields["fps_cap"])
            form.addRow("实际垂直同步", self._actual_fields["vsync"])
        for field in self._actual_fields.values():
            field.textChanged.connect(self._configuration_edited)
        self.actual_setting.currentTextChanged.connect(self._configuration_edited)
        if plan and plan.setting_id in {"resolution", "refresh_rate"}:
            related = self._actual_fields[plan.setting_id]
            self.actual_setting.currentTextChanged.connect(related.setText)
            related.textChanged.connect(self.actual_setting.setText)
        if plan and plan.action == AgentAction.REQUEST_DATA:
            confirmation_label = "我已核对并记录当前实际状态"
        elif plan and plan.purpose == "diagnostic":
            confirmation_label = "已提前核对实际配置（可选；保存时按表单提交）"
        else:
            confirmation_label = "已提前核对实际配置（可选；保存时按表单提交）"
        self.configuration_confirmed = QCheckBox(confirmation_label)
        # A proposed value is not evidence that the user executed it.
        self.configuration_confirmed.setChecked(False)
        for name, widget in [("平均 FPS", self.avg), ("1% Low", self.low), ("GPU 占用", self.gpu), ("测试次数", self.repeats), ("每次时长（秒）", self.duration), ("测试条件", self.condition)]:
            form.addRow(name, widget)
        form.addRow("处理器", self.actual_cpu)
        form.addRow("显卡", self.actual_gpu)
        form.addRow("分辨率", self.actual_resolution)
        form.addRow("刷新率", self.actual_refresh_rate)
        form.addRow("测试场景", self.actual_scene)
        setting_label = getattr(self.plan, "setting", "本轮设置") if self.plan else "本轮设置"
        if self.is_user_adjustment_request:
            form.addRow("优化方向", self.adjustment_direction)
            form.addRow("设置名称", self.adjustment_setting_name)
            form.addRow("当前值", self.adjustment_current_value)
            form.addRow("调整后值", self.adjustment_new_value)
            for field in (self.adjustment_setting_name, self.adjustment_current_value, self.adjustment_new_value):
                field.textChanged.connect(self._clear_csv_if_manual_edit)
        else:
            form.addRow(f"本轮{setting_label}", self.actual_setting)
        input_layout.addLayout(form)
        self.changed_settings_panel = QWidget()
        changed_layout = set_margins(QVBoxLayout(self.changed_settings_panel), spacing=10)
        changed_layout.addWidget(label("记录实际画面与运行设置", "sectionTitle"))
        changed_layout.addWidget(label("这里记录本次测试时的实际值，不会替你修改游戏。可同时修改多项画质，程序按整组实际配置评估，并据此生成下一步建议。", "metricNote", True))
        changed_form = QFormLayout()
        changed_form.setVerticalSpacing(10)
        changed_layout.addLayout(changed_form)
        self.actual_video_fields: dict[str, ActualSettingInput] = {}
        self._video_value_maps = {
            "video_preset": {"custom": "自定义", "low": "低", "medium": "中", "high": "高", "very_high": "非常高"},
            "display_mode": {"fullscreen": "全屏", "borderless": "全屏窗口模式", "windowed": "窗口模式"},
            "vsync": {"disabled": "已关闭", "enabled": "已开启"},
            "shadow_quality": {value: value for value in ("低", "中", "高", "非常高")},
            "dynamic_shadows": {"all": "全部", "sun_only": "仅限日光"},
            **ADVANCED_SETTING_LABELS,
            "player_contrast": {"enabled": "已启用", "disabled": "已禁用"},
            "nvidia_gsync": {"enabled": "已启用", "disabled": "已禁用", "not_available": "此设备不可用"},
            "nvidia_reflex": {"enabled": "已启用", "enabled_boost": "已启用 + 加速", "disabled": "已禁用", "not_available": "此设备不可用"},
            "amd_anti_lag": {"enabled": "已启用", "disabled": "已禁用", "not_available": "此设备不可用"},
        }
        for key, values in self._video_value_maps.items():
            if key in self._actual_fields or (plan and key == plan.setting_id):
                continue
            current = str(getattr(state, key, (state.video_settings or {}).get(key, "unknown")))
            display = values.get(current, current if current not in {"", "unknown"} else "未确认")
            choices = list(dict.fromkeys([*values.values(), "未确认", display]))
            field = ActualSettingInput(display, choices)
            self.actual_video_fields[key] = field
            changed_form.addRow(CONFIGURATION_LABELS.get(key, key), field)
            field.currentTextChanged.connect(self._configuration_edited)
        if "fps_cap" not in self._actual_fields and not (plan and plan.setting_id == "fps_cap"):
            field = EditableChoiceInput("" if state.fps_cap is None else str(state.fps_cap), ["0", "120", "144", "240", "300", "500"])
            field.setPlaceholderText("未确认；0 为不限帧")
            self.actual_video_fields["fps_cap"] = field
            changed_form.addRow("游戏最高 FPS", field)
            field.textChanged.connect(self._configuration_edited)
        if not (plan and plan.setting_id == "menu_fps_cap"):
            field = EditableChoiceInput(str((state.video_settings or {}).get("menu_fps_cap", "")), ["60", "120", "144", "240", "300"])
            field.setPlaceholderText("未确认")
            self.actual_video_fields["menu_fps_cap"] = field
            changed_form.addRow("菜单最高 FPS", field)
            field.textChanged.connect(self._configuration_edited)
        form.insertRow(form.getWidgetPosition(self.condition)[0] + 1, self.changed_settings_panel)
        self.changed_settings_panel.show()
        self.condition.currentIndexChanged.connect(self._condition_changed)
        input_layout.addWidget(self.configuration_confirmed)
        input_layout.addWidget(label("点击“保存并分析”即按以上实际配置提交；未执行建议也可以保存，程序将区分原配置复测与调整验证。", "metricNote", True))
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("备注（可选）：记录测试中的异常或变化")
        self.notes.setMaximumHeight(85)
        input_layout.addWidget(self.notes)
        if plan and plan.setting_id == "uncapped_validation":
            for widget in (self.avg, self.low, self.gpu, self.repeats, self.duration, self.condition,
                           *self._actual_fields.values(), self.configuration_confirmed,
                           self.file_input, self.browse_button, self.notes):
                if form.getWidgetPosition(widget)[0] >= 0:
                    form.setRowVisible(widget, False)
                widget.hide()
                field_label = form.labelForField(widget)
                if field_label:
                    field_label.hide()
        self.is_data_request = bool(self.plan and self.plan.action == AgentAction.REQUEST_DATA)
        self.save_button = QPushButton("提交信息" if self.is_data_request else "保存并分析")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self._save)
        input_layout.addWidget(self.save_button)
        self.rollback_button = QPushButton("确认已恢复上一组配置")
        self.rollback_button.setObjectName("secondaryButton")
        self.rollback_button.setVisible(False)
        self.rollback_button.clicked.connect(self.rollback_requested.emit)
        root.addWidget(self.rollback_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.retry_diagnostic_button = QPushButton("重新核对实际配置并继续诊断")
        self.retry_diagnostic_button.setVisible(bool(plan and plan.action == AgentAction.FINISH
                                                   and plan.setting_id in {"no_supported_action", "frame_limit_result"}))
        self.retry_diagnostic_button.clicked.connect(self.retry_diagnostic_requested.emit)
        root.addWidget(self.retry_diagnostic_button)

        guide_box = QFrame()
        guide_box.setObjectName("card")
        guide_layout = set_margins(QVBoxLayout(guide_box), 20, 18, 20, 20, 7)
        guide_layout.addWidget(label("临时排除帧率限制" if plan and plan.setting_id == "frame_limits" else "下一步建议（可按偏好调整）", "cardKicker"))
        change_title = f"{plan.setting}：{setting_value_label(plan.setting_id, plan.old_value)} → {setting_value_label(plan.setting_id, plan.new_value)}" if plan else "等待生成实验计划"
        guide_layout.addWidget(label(change_title, "actionTitle", True))
        guide_layout.addWidget(label("按建议单项测试便于判断原因；自行调整多项时，将按整组配置重新规划。", "metricNote", True))
        if plan and plan.setting_id == "uncapped_validation":
            test_requirement = "只选择目标，本步骤不会生成测试记录"
        elif plan and plan.action == AgentAction.REQUEST_DATA:
            test_requirement = "仅记录设置，不需要测试帧率"
        elif plan and plan.action == AgentAction.FINISH:
            test_requirement = "当前不需要新测试"
        elif plan and plan.purpose == "diagnostic":
            test_requirement = plan.method
        else:
            test_requirement = "先测 1 次，帧率没明显变差就继续测到累计 3 次"
        reference_text = f"{plan.reference_avg_fps:g} / {plan.reference_low_fps:g} FPS（不限帧诊断记录）" if plan and plan.reference_avg_fps is not None and plan.reference_low_fps is not None else f"{state.avg_fps} / {state.low_fps} FPS"
        for name, value in [("测试场景", state.test_scene), ("测试要求", test_requirement), ("比较基准", reference_text), ("判断标准", plan.success_criteria if plan else "等待实验计划")]:
            if plan and plan.action in {AgentAction.REQUEST_DATA, AgentAction.FINISH} and name == "判断标准":
                continue
            guide_layout.addWidget(hline())
            guide_layout.addWidget(RuleRow(name, value))
        if plan:
            guide_layout.addWidget(hline())
            guide_layout.addWidget(RuleRow("实验用途", "选择后续验证目标" if plan.setting_id == "uncapped_validation" else ("排查原因，结束后恢复原设置" if plan.purpose == "diagnostic" else "验证用户选定的优化调整")))
            if plan.diagnostic_record_id:
                guide_layout.addWidget(hline())
                guide_layout.addWidget(RuleRow("诊断线索", plan.rationale))
            if plan.purpose == "diagnostic" or plan.diagnostic_record_id:
                guide_layout.addWidget(hline())
                guide_layout.addWidget(RuleRow("恢复方法", plan.rollback))
            if plan.selection_reason:
                guide_layout.addWidget(hline())
                guide_layout.addWidget(RuleRow("选择来源", "AI 比较候选" if plan.planning_source == "model" else "本地规则"))
                guide_layout.addWidget(RuleRow("选择依据", plan.selection_reason))
            if plan.planning_context:
                context = plan.planning_context
                guide_layout.addWidget(RuleRow("准备验证", plan.hypothesis))
                self.planning_details_button = QPushButton("查看候选与历史反馈")
                self.planning_details_button.setCheckable(True)
                self.planning_details = QWidget()
                detail_layout = set_margins(QVBoxLayout(self.planning_details), 0, 4, 0, 4, 6)
                options = context.get("candidate_options", [])
                for option in options:
                    selected = option.get("candidate_id") == context.get("selected_candidate_id")
                    detail_layout.addWidget(label(("已选：" if selected else "候选：") + option.get("title", ""), "ruleText", True))
                feedback = context.get("feedback", {})
                attempts = feedback.get("attempts", [])
                avoid = feedback.get("avoid_repeating", [])
                unresolved = feedback.get("unresolved", [])
                detail_layout.addWidget(label(f"已记录 {len(attempts)} 次实验反馈；跳过 {len(avoid)} 项无收益调整；待处理 {len(unresolved)} 项问题。", "metricNote", True))
                if attempts:
                    detail_layout.addWidget(RuleRow("最近结论", attempts[-1].get("conclusion", "")))
                if context.get("status") in {"model_selected", "model_fallback"}:
                    detail_layout.addWidget(label(f"模型调用耗时：{context.get('elapsed_ms', 0):g} ms", "metricNote"))
                self.planning_details.hide()
                self.planning_details_button.toggled.connect(self.planning_details.setVisible)
                guide_layout.addWidget(self.planning_details_button)
                guide_layout.addWidget(self.planning_details)
        guide_layout.addSpacing(8)
        guide_label = label(plan.rationale if plan and plan.setting_id == "uncapped_validation" else "提示：GPU 占用低不一定是 CPU 瓶颈，也可能是限帧或垂直同步造成的。", "warningBox", True)
        guide_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        guide_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        guide_label.setMinimumHeight(max(30, guide_label.sizeHint().height()))
        guide_layout.addWidget(guide_label)
        guide_layout.addStretch()
        columns.addWidget(input_card, 3)
        columns.addWidget(guide_box, 2)
        root.addLayout(columns)
        if not state.has_task:
            input_card.hide()
            guide_box.hide()
            self.create_button = QPushButton("新建调校")
            self.create_button.setObjectName("primaryButton")
            self.create_button.clicked.connect(self.task_requested)
            root.addWidget(self.create_button, 0, Qt.AlignmentFlag.AlignLeft)
        root.addStretch()
        self._sync_input_enabled()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_columns"):
            self._columns.setDirection(QBoxLayout.Direction.TopToBottom if self.viewport().width() < 900 else QBoxLayout.Direction.LeftToRight)

    def _setting_value_from_state(self) -> str:
        """Return the confirmed current value for the planned setting."""
        plan = self.plan
        setting_id = str(getattr(plan, "setting_id", "") or "") if plan else ""
        if setting_id == "uncapped_validation":
            return "请选择验证目标"
        if setting_id == "frame_limits":
            return "未确认"
        if setting_id == "user_defined" and plan is not None:
            return plan.new_value
        setting = str(getattr(plan, "setting", "") or "") if plan else ""
        display_values = {
            "graphics_mode": {"dgpu": "独显直连", "hybrid": "混合输出", "advanced_optimus": "自动切换", "unknown": "不确定"},
            "power_mode": {"ac_performance": "接通电源 · 性能模式", "ac_balanced": "接通电源 · 平衡模式", "battery": "电池供电", "unknown": "不确定"},
            "cooling_mode": {"performance": "性能 / 狂暴模式", "balanced": "平衡模式", "quiet": "安静模式", "unknown": "不确定"},
        }
        values = {
            "resolution": self.state.resolution,
            "refresh_rate": self.state.refresh_rate,
            "test_scene": self.state.test_scene,
            "gpu_power": "" if self.state.gpu_power_w is None else str(self.state.gpu_power_w),
            "gpu_power_w": "" if self.state.gpu_power_w is None else str(self.state.gpu_power_w),
            "graphics_mode": display_values["graphics_mode"].get(self.state.graphics_mode, self.state.graphics_mode),
            "power_mode": display_values["power_mode"].get(self.state.power_mode, self.state.power_mode),
            "cooling_mode": display_values["cooling_mode"].get(self.state.cooling_mode, self.state.cooling_mode),
            "shadow_quality": "" if self.state.shadow_quality == "unknown" else self.state.shadow_quality,
            "dynamic_shadows": {"all": "全部", "sun_only": "仅限日光", "unknown": ""}.get(self.state.dynamic_shadows, ""),
            "fps_cap": "" if self.state.fps_cap is None else str(self.state.fps_cap),
            "display_mode": {"fullscreen": "全屏", "borderless": "全屏窗口模式", "windowed": "窗口模式"}.get(self.state.display_mode, self.state.display_mode),
            "vsync": {"disabled": "已禁用", "enabled": "已启用", "unknown": ""}.get(self.state.vsync, self.state.vsync),
        }
        if setting_id in ADVANCED_SETTING_LABELS:
            value = str((self.state.video_settings or {}).get(setting_id, ""))
            return setting_value_label(setting_id, value)
        if setting_id in values:
            return values[setting_id]
        labels = {
            "分辨率": self.state.resolution,
            "刷新率": self.state.refresh_rate,
            "测试场景": self.state.test_scene,
            "GPU 功耗": values["gpu_power_w"],
            "显卡输出模式": values["graphics_mode"],
            "供电 / 性能模式": values["power_mode"],
            "散热模式": values["cooling_mode"],
            "全局阴影效果": values["shadow_quality"],
            "全局阴影质量": values["shadow_quality"],
            "动态阴影": values["dynamic_shadows"],
            "游戏最高FPS": values["fps_cap"],
            "游戏内最大 FPS": values["fps_cap"],
            "显示模式": values["display_mode"],
        }
        return labels.get(setting, "")

    def _setting_options(self) -> list[str]:
        """Return controlled choices for settings with a finite domain."""
        setting_id = str(getattr(self.plan, "setting_id", "") or "") if self.plan else ""
        return {
            "resolution": list(RESOLUTION_PRESETS),
            "refresh_rate": ["60 Hz", "75 Hz", "120 Hz", "144 Hz", "165 Hz", "240 Hz", "300 Hz", "360 Hz"],
            "uncapped_validation": ["请选择验证目标", *UNCAPPED_CHOICES],
            "frame_limits": ["未确认", "不限帧且关闭垂直同步", "仍有限制"],
            "graphics_mode": ["独显直连", "混合输出", "自动切换", "不确定"],
            "power_mode": ["接通电源 · 性能模式", "接通电源 · 平衡模式", "电池供电", "不确定"],
            "cooling_mode": ["性能 / 狂暴模式", "平衡模式", "安静模式", "不确定"],
            "shadow_quality": ["低", "中", "高", "非常高", "未确认"],
            "dynamic_shadows": ["全部", "仅限日光", "未确认"],
            "display_mode": ["全屏", "全屏窗口模式", "窗口模式"],
            "vsync": ["已禁用", "已启用", "未确认"],
            **{
                key: [labels.get(value, value) for value in ADVANCED_SETTING_OPTIONS[key]]
                for key, labels in ADVANCED_SETTING_LABELS.items()
            },
        }.get(setting_id, [])

    def _supplement_value(self) -> str:
        """Convert friendly UI labels back to stable domain values."""
        if self.is_user_adjustment_request:
            return " | ".join((
                self.adjustment_direction.currentText().strip(),
                self.adjustment_setting_name.text().strip(),
                self.adjustment_current_value.text().strip(),
                self.adjustment_new_value.text().strip(),
            ))
        value = self.actual_setting.text().strip()
        setting_id = str(getattr(self.plan, "setting_id", "") or "") if self.plan else ""
        aliases = {
            "graphics_mode": {"独显直连": "dgpu", "混合输出": "hybrid", "自动切换": "advanced_optimus", "不确定": "unknown"},
            "power_mode": {"接通电源 · 性能模式": "ac_performance", "接通电源 · 平衡模式": "ac_balanced", "电池供电": "battery", "不确定": "unknown"},
            "cooling_mode": {"性能 / 狂暴模式": "performance", "平衡模式": "balanced", "安静模式": "quiet", "不确定": "unknown"},
            "dynamic_shadows": {"全部": "all", "仅限日光": "sun_only", "仅太阳": "sun_only", "未确认": "unknown"},
            "display_mode": {"全屏": "fullscreen", "全屏窗口模式": "borderless", "无边框窗口": "borderless", "窗口模式": "windowed", "窗口化": "windowed"},
            "vsync": {"已禁用": "disabled", "已启用": "enabled", "未确认": "unknown"},
        }
        advanced_aliases = {
            key: {label: value for value, label in labels.items()}
            for key, labels in ADVANCED_SETTING_LABELS.items()
        }
        aliases.update(advanced_aliases)
        return aliases.get(setting_id, {}).get(value, value)

    def _configuration_edited(self, *_args):
        if hasattr(self, "configuration_confirmed"):
            self.configuration_confirmed.setChecked(False)

    def _condition_changed(self, *_args):
        self.changed_settings_panel.setVisible(True)
        self._configuration_edited()

    def _clear_csv_if_manual_edit(self, *_args):
        if self._csv_metrics is not None:
            self._csv_metrics = None
            self._csv_values = None

    def set_completed(self, completed: bool = True):
        self.completed = completed
        self._update_execution_guide()
        self._sync_input_enabled()

    def set_rollback_pending(self, pending: bool = True):
        self.rollback_pending = pending
        self.rollback_button.setVisible(pending)
        self._update_execution_guide()
        self._sync_input_enabled()

    def _update_execution_guide(self):
        plan = self.plan
        if not self.state.has_task:
            text = "先创建任务并保存基线，再执行本轮实验。"
        elif self.rollback_pending:
            text = "当前步骤：恢复配置。" + (plan.rollback if plan else "恢复上一组已验证配置。") + " 完成后点击“确认已恢复上一组配置”；此时不用再提交帧率。"
        elif self.completed or (plan and plan.action == AgentAction.FINISH):
            text = "本轮测试已结束。返回当前调校查看结论，或在配置与报告中查看已有验证结果。"
        elif plan and plan.action == AgentAction.REQUEST_DATA:
            text = "当前步骤：补充信息。" + plan.method + " 提交后查看新生成的实验要求。"
        elif plan:
            count = "至少 2 次" if plan.setting_id == "frame_limits" else "先测 1 次，按评估提示补测" if plan.purpose == "diagnostic" else "先测 1 次，无明显退步后累计到 3 次"
            target = setting_value_label(plan.setting_id, plan.new_value)
            text = f"1. 将“{plan.setting}”调整为“{target}”，其余设置不变。 2. 在“{self.state.test_scene}”{count}。 3. 录入并核对实际配置，再保存评估；要求补测时保持当前设置，要求恢复时再改回。"
        else:
            text = "返回当前调校查看待办事项。"
        self.execution_guide.setText(text)
        pill = self.findChild(QLabel, "statusPill")
        if pill:
            status = "等待恢复配置" if self.rollback_pending else "本轮已结束" if self.completed or (plan and plan.action == AgentAction.FINISH) else "等待补充信息" if plan and plan.action == AgentAction.REQUEST_DATA else "等待录入" if self.state.has_task else "尚未开始"
            pill.setText(f"●  {status}")

    def show_evaluation(self, decision: Decision):
        self.result_feedback.setText("本次评估：" + decision.summary + " " + "；".join(decision.reasons))
        self.result_feedback.show()

    def set_planning(self, planning: bool = True):
        self.planning = planning
        self._sync_input_enabled()

    def _sync_input_enabled(self):
        editable = self.state.has_task and not self.completed and not self.rollback_pending and not self.planning
        for widget in (self.file_input, self.browse_button, self.avg, self.low, self.gpu, self.repeats, self.duration, self.condition):
            widget.setEnabled(editable and not self.is_data_request)
        for field in (*self._actual_fields.values(), *self.actual_video_fields.values(), self.actual_setting, self.configuration_confirmed, self.notes,
                      self.adjustment_direction, self.adjustment_setting_name, self.adjustment_current_value,
                      self.adjustment_new_value):
            field.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.rollback_button.setEnabled(not self.completed and self.rollback_pending and not self.planning)

    def _actual_configuration(self) -> dict[str, str]:
        config = {key: field.text().strip() for key, field in self._actual_fields.items()}
        config.update({
            "display_mode": self.state.display_mode,
            "fps_cap": "unknown" if self.state.fps_cap is None else str(self.state.fps_cap),
            "vsync": self.state.vsync,
            "shadow_quality": self.state.shadow_quality,
            "dynamic_shadows": self.state.dynamic_shadows,
        })
        config.update(self.state.video_settings or {})
        config.update({key: field.text().strip() for key, field in self._actual_fields.items()})
        for key, field in self.actual_video_fields.items():
            value = field.text().strip()
            reverse = {label: stable for stable, label in self._video_value_maps.get(key, {}).items()}
            config[key] = reverse.get(value, canonical_value(key, value)) if value else "unknown"
        config["vsync"] = {"已关闭": "disabled", "已开启": "enabled", "未确认": "unknown"}.get(config.get("vsync"), config.get("vsync", "unknown"))
        config["planned_setting"] = str(getattr(self.plan, "setting", "") or "") if self.plan else ""
        config["planned_setting_id"] = str(getattr(self.plan, "setting_id", "") or "") if self.plan else ""
        config["actual_setting_value"] = self._supplement_value() if self.is_user_adjustment_request else self.actual_setting.text().strip()
        setting_id = config["planned_setting_id"]
        if setting_id and config["actual_setting_value"]:
            config[setting_id] = self._supplement_value()
        return {key: value for key, value in config.items() if value}

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择基准测试 CSV", str(APP_DIR), "CSV 文件 (*.csv);;所有文件 (*.*)")
        if path:
            self.file_input.setText(path)
            self._import_entered_file()

    def _csv_path_changed(self, *_args):
        self._clear_csv_if_manual_edit()
        self._loaded_csv_path = ""

    def _import_entered_file(self) -> bool:
        path = self.file_input.text().strip().strip('"')
        if not path:
            return False
        try:
            metrics = import_benchmark_csv(path)
        except ValueError as error:
            QMessageBox.warning(self, "CSV 无法导入", str(error))
            return False
        self.avg.setValue(round(float(metrics["avg_fps"])))
        self.low.setValue(round(float(metrics["low_fps"])))
        self.gpu.setValue(round(float(metrics.get("gpu_usage", 0))))
        self.repeats.setValue(int(metrics.get("run_count", 1)))
        self._csv_metrics = metrics
        self._loaded_csv_path = self.file_input.text().strip()
        self._csv_values = (self.avg.value(), self.low.value(), self.gpu.value(), self.repeats.value())
        self.notice_requested.emit("测试数据已导入，请核对测试条件", "success")
        return True

    def _save(self):
        if self.completed or self.planning or self.rollback_pending:
            return
        if not self.is_data_request and self.file_input.text().strip() and self.file_input.text().strip() != self._loaded_csv_path:
            if not self._import_entered_file():
                return
        if not self.is_data_request:
            if self.avg.value() <= 0 or self.low.value() <= 0:
                QMessageBox.warning(self, "测试数据不完整", "请填写实测平均 FPS 和 1% Low，或导入测试 CSV。")
                return
            if self.low.value() > self.avg.value() * 1.05:
                QMessageBox.warning(self, "测试数据无效", "1% Low 不应高于平均 FPS，请检查录入数据。")
                return
        if self.is_data_request and self.is_user_adjustment_request:
            direction = self.adjustment_direction.currentText().strip()
            setting_name = self.adjustment_setting_name.text().strip()
            old_value = self.adjustment_current_value.text().strip()
            new_value = self.adjustment_new_value.text().strip()
            if direction == "请选择优化方向" or not setting_name or not old_value or not new_value:
                QMessageBox.warning(self, "需要补充信息", "请选择优化方向，并填写设置名称、当前值和调整后值。")
                return
            if old_value == new_value:
                QMessageBox.warning(self, "设置没有变化", "当前值与调整后值相同，请填写一个实际可执行的单项变化。")
                self.adjustment_new_value.setFocus()
                return
        elif self.is_data_request and not self.actual_setting.text().strip():
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
        # Saving is the explicit confirmation of the actual form snapshot.
        # The optional checkbox must not prevent recording a real observation.
        self.configuration_confirmed.setChecked(True)
        self.result_saved.emit({
            "avg": self.avg.value(),
            "low": self.low.value(),
            "gpu": self.gpu.value() if self.gpu.value() > 0 else None,
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
            "supplement_value": self._supplement_value(),
        })


class HistoryPage(QWidget):
    task_restore_requested = Signal(str)

    def __init__(self, state: TaskState, tasks: list[dict] | None = None):
        super().__init__()
        self.state = state
        self._highlight_generation = 0
        root = set_margins(QVBoxLayout(self), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("实验记录", "测试记录", "从基线到每轮实验，查看实际执行的设置、测量结果和评估结论。"))
        restore_row = QHBoxLayout()
        restore_row.addWidget(label("打开以前的任务", "ruleName"))
        self.task_selector = RoundedComboBox()
        self.task_selector.setMinimumWidth(320)
        self.task_selector.addItem("当前会话任务", "")
        restore_row.addWidget(self.task_selector, 1)
        self.restore_button = QPushButton("打开任务")
        self.restore_button.setObjectName("secondaryButton")
        self.restore_button.clicked.connect(self._restore_selected)
        restore_row.addWidget(self.restore_button)
        root.addLayout(restore_row)
        root.addWidget(SectionHeader("测试记录", "最近的测试在最下方"))
        self.table = QTableWidget(1, 7)
        self.table.setHorizontalHeaderLabels(["记录", "分辨率", "平均 FPS", "1% Low", "GPU 占用", "执行状态", "评估"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(250)
        rows = [["基线", state.resolution, str(state.avg_fps), str(state.low_fps), f"{state.gpu_usage}%", "已执行", "当前最佳"]] if state.has_task else []
        self.table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)
            self.table.setRowHeight(row_idx, 46)
        root.addWidget(self.table)
        root.addWidget(GuidancePanel("读懂记录", "“已执行”表示你确认过实际设置；“保留”需要有效且可比较的测试支持。测试条件有变化时，先核对记录，再判断收益。"))
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
            item.setBackground(QColor("#eff7dc"))
            if col == 0:
                self._highlight_generation += 1
                item.setData(Qt.ItemDataRole.UserRole, self._highlight_generation)
            self.table.setItem(row, col, item)
        self.table.setRowHeight(row, 46)
        generation = self._highlight_generation
        QTimer.singleShot(1200, lambda target_row=row, token=generation: self._clear_row_highlight(target_row, token))

    def _clear_row_highlight(self, row: int, generation: int):
        if row >= self.table.rowCount():
            return
        first = self.table.item(row, 0)
        if first is None or first.data(Qt.ItemDataRole.UserRole) != generation:
            return
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is not None:
                item.setBackground(QBrush())

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


class ReportPage(QScrollArea):
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
        limit_note: str = "",
    ):
        super().__init__()
        self.setWidgetResizable(True)
        page_body = QWidget()
        self.setWidget(page_body)
        root = set_margins(QVBoxLayout(page_body), 34, 27, 34, 36, 16)
        reveal_targets: list[QWidget] = []
        root.addWidget(PageHeader("优化结果", "验证报告", "查看已验证配置与基线的差异；尚未完成验证的建议不会作为收益。"))
        empty = QFrame()
        empty.setObjectName("card")
        empty_layout = set_margins(QVBoxLayout(empty), 30, 35, 30, 35, 8)
        icon = label("↗", "brandMark")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        title_text = "本轮结果" if result and decision else ("最近一次结果" if last_decision else "完成一次复测后生成报告")
        title = label(title_text, "sectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title)
        body_text = str(explanation["summary"]) if explanation else (
            decision.summary if decision else str((last_decision or {}).get("summary") or
            "录入复测结果后，这里会显示性能变化和当前最佳配置。")
        )
        summary_text = label(body_text, "ruleText", True)
        summary_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary_text.setMaximumWidth(610)
        summary_text.setMinimumHeight(42)
        empty_layout.addWidget(summary_text, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(empty)
        reveal_targets.append(empty)
        if limit_note:
            note = label(limit_note, "warningBox", True)
            note.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            root.addWidget(note)
        if explanation:
            summary_card = QFrame()
            summary_card.setObjectName("card")
            summary_layout = set_margins(QVBoxLayout(summary_card), 22, 18, 22, 18, 8)
            model_name = str(explanation.get("model", "")).strip()
            source = f"AI 说明 · {model_name}" if explanation.get("source") == "model" and model_name else (
                "AI 说明" if explanation.get("source") == "model" else "本地说明"
            )
            summary_layout.addWidget(SectionHeader(source, "帮你理解结果，结论以测试为准"))
            for reason in explanation.get("reasoning", []):
                reason_label = label(f"• {reason}", "ruleText", True)
                reason_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                reason_label.setMinimumHeight(max(20, reason_label.sizeHint().height()))
                summary_layout.addWidget(reason_label)
            next_step = label(f"下一步：{explanation['next_step']}", "ruleName", True)
            next_step.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            next_step.setMinimumHeight(max(22, next_step.sizeHint().height()))
            summary_layout.addWidget(next_step)
            if explanation.get("warning"):
                warning = label(str(explanation["warning"]), "warningBox", True)
                warning.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                warning.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                warning.setMinimumHeight(max(30, warning.sizeHint().height()))
                summary_layout.addWidget(warning)
            root.addWidget(summary_card)
            reveal_targets.append(summary_card)
        if best_record:
            snapshot = QFrame()
            snapshot.setObjectName("card")
            snapshot_layout = set_margins(QVBoxLayout(snapshot), 22, 18, 22, 18, 7)
            snapshot_layout.addWidget(SectionHeader("已验证配置（不限帧测试条件）" if limit_note else "当前最佳配置", "已经测过并完成比较"))
            configuration = best_record.configuration or {"resolution": best_record.resolution, "test_scene": best_record.test_scene}
            for key, value in configuration.items():
                if value and key in CONFIGURATION_LABELS:
                    displayed = setting_value_label(key, str(value))
                    displayed = {"enabled": "已开启", "disabled": "已关闭", "fullscreen": "全屏", "borderless": "无边框窗口", "windowed": "窗口模式", "all": "全部", "sun_only": "仅限日光", "not_available": "此设备不可用"}.get(displayed, displayed)
                    if key == "fps_cap" and str(value) == "0":
                        displayed = "不限帧（0）"
                    snapshot_layout.addWidget(RuleRow(CONFIGURATION_LABELS[key], displayed))
            snapshot_layout.addWidget(RuleRow("验证结果", f"{best_record.avg_fps:.0f} FPS / {best_record.low_fps:.0f} 1% Low"))
            root.addWidget(snapshot)
            reveal_targets.append(snapshot)
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
        status = "相对基线" if best_record and baseline_record else "等待可比数据"
        denominator = max(1, int(max_rounds))
        metric_cards = [
            MetricCard("已完成实验", f"{completed} / {denominator}" if state.has_task else "尚未开始", "实验进度", min(100, round(completed / denominator * 100))),
            MetricCard("平均 FPS 改善", f"{avg_delta:+.1f}%" if best_record and baseline_record else "待验证", status, min(100, max(0, 50 + round(avg_delta * 5))) if best_record and baseline_record else 0),
            MetricCard("1% LOW 改善", f"{low_delta:+.1f}%" if best_record and baseline_record else "待验证", status, min(100, max(0, 50 + round(low_delta * 5))) if best_record and baseline_record else 0),
        ]
        for card in metric_cards:
            metrics.addWidget(card)
        root.addLayout(metrics)
        root.addStretch()
        if explanation or best_record or last_decision or result:
            reveal_targets.extend(metric_cards)
            for widget in reveal_targets:
                effect = QGraphicsOpacityEffect(widget)
                effect.setOpacity(0.0)
                widget.setGraphicsEffect(effect)
            self._reveal_targets = reveal_targets
            self._reveal_animation: QParallelAnimationGroup | None = None
            self._reveal_started = False

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_reveal_targets") and not self._reveal_started:
            self._reveal_started = True
            self._start_reveal()

    def _start_reveal(self):
        group = QParallelAnimationGroup(self)
        for index, widget in enumerate(self._reveal_targets):
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                continue
            sequence = QSequentialAnimationGroup(group)
            if index:
                sequence.addAnimation(QPauseAnimation(min(index * 32, 160), sequence))
            animation = QPropertyAnimation(effect, b"opacity", sequence)
            animation.setDuration(160)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            sequence.addAnimation(animation)
            group.addAnimation(sequence)
        self._reveal_animation = group
        group.finished.connect(self._finish_reveal)
        group.start()

    def _finish_reveal(self):
        for widget in self._reveal_targets:
            widget.setGraphicsEffect(None)
        self._reveal_animation.deleteLater()
        self._reveal_animation = None


class APISettingsPage(QScrollArea):
    provider_applied = Signal(object)
    notice_requested = Signal(str, str)

    THINKING_OPTIONS = [
        ("关闭（兼容性最好）", None),
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
        root.addWidget(PageHeader("模型设置", "模型设置", "可选连接 AI，帮助选择测试建议、解释结果。未连接时使用本地规则。"))
        root.addWidget(GuidancePanel("连接后会改变什么", "AI 可以解释结果、从已校验的候选中选择建议。指标计算、结果是否有效以及保留或回滚，仍由程序判断。"))
        card = QFrame()
        card.setObjectName("formCard")
        form = QFormLayout(card)
        form.setContentsMargins(24, 22, 24, 22)
        form.setSpacing(13)
        self.base_url = QLineEdit(config.base_url if config else "")
        self.base_url.setPlaceholderText("https://your-service.example/v1")
        self.api_key = QLineEdit(config.api_key if config else "")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("密钥只在本次运行中使用；记住时使用 Windows 账户保护")
        self.model = RoundedComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setPlaceholderText("模型名称，例如 your-model-name")
        self.model.lineEdit().setPlaceholderText("模型名称，例如 your-model-name")
        if config and config.model:
            self.model.addItem(config.model)
            self.model.setCurrentText(config.model)
        self.fetch_models_button = QPushButton("获取模型")
        self.fetch_models_button.setObjectName("secondaryButton")
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setDecimals(0)
        self.timeout.setSuffix(" 秒")
        self.timeout.setValue(config.timeout_seconds if config else 30)
        self.timeout.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.reasoning = RoundedComboBox()
        for title, value in self.THINKING_OPTIONS:
            self.reasoning.addItem(title, value)
        current_reasoning = config.reasoning_effort if config else None
        self.reasoning.setCurrentIndex(next((i for i, item in enumerate(self.THINKING_OPTIONS) if item[1] == current_reasoning), 0))
        form.addRow("API 地址", self.base_url)
        form.addRow("API 密钥", self.api_key)
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(8)
        model_layout.addWidget(self.model, 1)
        model_layout.addWidget(self.fetch_models_button)
        form.addRow("模型名称", model_row)
        form.addRow("请求超时", self.timeout)
        form.addRow("思考模式", self.reasoning)
        root.addWidget(card)
        self.remember_config = QCheckBox("记住本机 API 配置（密钥使用 Windows 账户保护）")
        self.remember_config.setChecked(True)
        root.addWidget(self.remember_config)
        root.addWidget(label("请使用支持 OpenAI Chat Completions 的服务。不支持“思考模式”时，选“关闭”。密钥不会存入数据库。", "ruleText", True))
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用配置")
        self.apply_button.setObjectName("primaryButton")
        self.test_button = QPushButton("测试连接")
        self.test_button.setObjectName("secondaryButton")
        self.clear_saved_button = QPushButton("清除已保存配置")
        self.clear_saved_button.setObjectName("secondaryButton")
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.clear_saved_button)
        buttons.addStretch()
        root.addLayout(buttons)
        self.status = label("尚未启用模型；当前使用本地说明", "sectionMeta", True)
        status_row = QWidget()
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        self.request_indicator = BusyIndicator()
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        status_layout.addWidget(self.status, 1)
        status_layout.addWidget(self.request_indicator)
        root.addWidget(status_row)
        root.addStretch()
        self.apply_button.clicked.connect(self._apply)
        self.test_button.clicked.connect(self._test)
        self.fetch_models_button.clicked.connect(self._discover_models)
        self.clear_saved_button.clicked.connect(self._clear_saved)

    def _config(self) -> APIConfig:
        return APIConfig(
            self.base_url.text().strip().rstrip("/"), self.api_key.text().strip(),
            self.model.currentText().strip(), self.timeout.value(), self.reasoning.currentData(),
        )

    def _discover_models(self):
        base_url = self.base_url.text().strip().rstrip("/")
        api_key = self.api_key.text().strip()
        timeout = self.timeout.value()
        if not base_url or not api_key:
            message = "获取模型前请先填写 API 地址和 API 密钥"
            self.status.setText(f"获取模型失败：{message}")
            QMessageBox.warning(self, "获取模型失败", message)
            return
        self._request_generation += 1
        generation = self._request_generation
        self.fetch_models_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status.setText("正在获取模型列表……")
        self._start_model_worker(
            lambda: OpenAICompatibleProvider.list_models(base_url, api_key, timeout),
            lambda models: self._models_succeeded(generation, models),
            lambda error: self._models_failed(generation, error),
        )

    def _models_succeeded(self, generation: int, models: list[str]):
        if generation != self._request_generation:
            return
        self.fetch_models_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        current = self.model.currentText().strip()
        values = list(models)
        if current and current not in values:
            values.insert(0, current)
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(values)
        self.model.setCurrentText(current or values[0])
        self.model.blockSignals(False)
        self.status.setText(f"已获取 {len(models)} 个模型，可从下拉框选择或继续手动输入。")
        self.notice_requested.emit(f"已获取 {len(models)} 个可用模型", "success")

    def _models_failed(self, generation: int, error: Exception):
        if generation != self._request_generation:
            return
        self.fetch_models_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        message = str(error) if isinstance(error, (ModelAPIError, ValueError)) else "无法获取模型列表。请检查地址、密钥和网络。"
        self.status.setText(f"获取模型失败：{message}")
        QMessageBox.warning(self, "获取模型失败", message)

    def _persist_config(self, config: APIConfig) -> str:
        if not self.remember_config.isChecked():
            try:
                clear_saved_api_config()
            except LocalAPISettingsError as error:
                QMessageBox.warning(self, "清除本机配置失败", str(error))
                return " · 本机配置清除失败"
            return " · 不保存本机配置"
        try:
            save_saved_api_config(config)
        except LocalAPISettingsError as error:
            QMessageBox.warning(self, "保存本机配置失败", str(error))
            return " · 本机配置保存失败"
        return " · 已保存本机配置"

    def _clear_saved(self):
        try:
            clear_saved_api_config()
        except LocalAPISettingsError as error:
            QMessageBox.warning(self, "清除本机配置失败", str(error))
            return
        self.remember_config.setChecked(False)
        self.status.setText("已清除本机保存的 API 配置；当前输入仍保留在页面中。")
        self.notice_requested.emit("已清除本机保存的 API 配置", "success")

    def _apply(self):
        try:
            provider = OpenAICompatibleProvider(self._config())
        except ValueError as error:
            QMessageBox.warning(self, "配置无效", str(error))
            return
        self.provider_applied.emit(provider)
        persistence = self._persist_config(provider.config)
        self.status.setText(f"已启用 {provider.config.model} · 思考模式：{self.reasoning.currentText()}{persistence}")
        self.notice_requested.emit("模型配置已应用", "success")

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
        persistence = self._persist_config(provider.config)
        self.status.setText(f"连接成功 · {response.model} · {preview}{persistence}")
        self.notice_requested.emit(f"模型连接成功 · {response.model}", "success")

    def _test_failed(self, generation: int, error: Exception):
        if generation != self._request_generation:
            return
        self.test_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        message = str(error) if isinstance(error, ModelAPIError) else "无法连接模型服务。请检查地址、密钥和网络。"
        self.status.setText(f"连接失败：{message}")
        QMessageBox.warning(self, "连接测试失败", message)

    def _start_model_worker(self, operation, on_success, on_failure):
        self.request_indicator.set_running(True)
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
        self.request_indicator.set_running(bool(self._model_jobs))
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


def navigation_icon(index: int) -> QIcon:
    """Small native line icons, with no external font or image dependency."""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    painter.setPen(QPen(QColor("#625b52"), 1.4, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if index == 0:
        for x, height in ((4, 6), (9, 10), (14, 14)):
            painter.drawRoundedRect(QRectF(x, 17 - height, 2, height), .5, .5)
    elif index == 2:
        path = QPainterPath()
        path.moveTo(2, 10)
        for x, y in ((5, 10), (7, 4), (10, 16), (13, 7), (15, 10), (18, 10)):
            path.lineTo(x, y)
        painter.drawPath(path)
    elif index == 3:
        painter.drawRoundedRect(QRectF(4, 2, 12, 16), 2, 2)
        for y in (7, 11, 15):
            painter.drawLine(7, y, 13, y)
    elif index == 4:
        painter.drawRoundedRect(QRectF(2, 5, 16, 12), 2, 2)
        painter.drawLine(4, 3, 9, 3)
        painter.drawLine(9, 3, 11, 5)
    else:
        for y, x in ((5, 7), (10, 13), (15, 6)):
            painter.drawLine(2, y, 18, y)
            painter.setBrush(QColor("#f0eee8"))
            painter.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
    painter.end()
    return QIcon(pixmap)


class Sidebar(QFrame):
    navigate = Signal(int)
    help_requested = Signal()

    def __init__(self, state: TaskState):
        super().__init__()
        self.state = state
        self.setObjectName("sidebar")
        self.setFixedWidth(224)
        root = set_margins(QVBoxLayout(self), 20, 25, 20, 20, 8)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(label("GameTune", "brandName"))
        brand.addLayout(brand_text)
        brand.addStretch()
        root.addLayout(brand)
        root.addSpacing(18)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.new_task_button = QPushButton("＋  新建调校")
        self.new_task_button.setObjectName("newTaskButton")
        self.new_task_button.setCheckable(True)
        self.new_task_button.setMinimumHeight(38)
        self.group.addButton(self.new_task_button, 1)
        root.addWidget(self.new_task_button)
        root.addSpacing(8)
        root.addWidget(label("当前任务", "sidebarLabel"))
        for text, index in [("当前调校", 0), ("当前实验", 2), ("测试记录", 3), ("配置与报告", 4)]:
            if index == 3:
                root.addSpacing(10)
                root.addWidget(label("记录与结果", "sidebarLabel"))
            button = QPushButton(text)
            if index == 2:
                button.setText("    执行本轮实验")
                button.setToolTip("属于当前调校：查看步骤、录入结果、补测或确认恢复")
            elif index == 0:
                button.setToolTip("当前任务总览：查看表现、判断依据和下一步")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setMinimumHeight(39)
            button.setIcon(navigation_icon(index))
            button.setIconSize(QSize(19, 19))
            self.group.addButton(button, index)
            root.addWidget(button)
        self.nav_indicator = QFrame(self)
        self.nav_indicator.setObjectName("navIndicator")
        self.nav_indicator.setFixedWidth(3)
        self.nav_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Reuse one animation for the lifetime of the sidebar.  A cancelled
        # animation must never leave a Python reference to a deleted Qt object.
        self._indicator_animation = QPropertyAnimation(self.nav_indicator, b"geometry", self)
        self._indicator_animation.setDuration(180)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.navigate)
        QTimer.singleShot(0, self._sync_indicator)
        root.addSpacing(16)
        root.addWidget(hline())
        root.addSpacing(12)
        root.addWidget(label("当前任务", "sidebarLabel"))
        task = QFrame()
        task.setObjectName("sideTask")
        task_layout = set_margins(QVBoxLayout(task), 12, 11, 12, 11, 4)
        self.task_title = label(state.name if state.has_task else "尚未创建任务", "sideTaskTitle", True)
        self.task_text = label(f"第 {state.round_number} 轮 · {state.step}\n{state.game}" if state.has_task else "先记录环境，再开始测试", "sideTaskText", True)
        task_layout.addWidget(self.task_title)
        task_layout.addWidget(self.task_text)
        root.addWidget(task)
        root.addStretch()
        model_button = QPushButton("模型设置")
        model_button.setObjectName("navButton")
        model_button.setCheckable(True)
        model_button.setIcon(navigation_icon(5))
        model_button.setIconSize(QSize(19, 19))
        self.group.addButton(model_button, 5)
        root.addWidget(model_button)
        self.help_button = QPushButton("使用说明")
        self.help_button.setObjectName("secondaryButton")
        self.help_button.clicked.connect(self.help_requested)
        root.addWidget(self.help_button)
        root.addWidget(label("记录保存在本机\n模型连接为可选功能", "sideTaskText", True))

    def update_task(self):
        self.task_title.setText(self.state.name if self.state.has_task else "尚未创建任务")
        self.task_text.setText(f"第 {self.state.round_number} 轮 · {self.state.step}\n{self.state.game}"
                               if self.state.has_task else "先记录环境，再开始测试")

    def set_active(self, index: int, animated: bool = True):
        button = self.group.button(index)
        if button is None:
            return
        button.setChecked(True)
        position = button.mapTo(self, button.rect().topLeft())
        target = button.geometry().translated(position - button.geometry().topLeft())
        target.setWidth(3)
        self._indicator_animation.stop()
        if not self.nav_indicator.isVisible() or not animated:
            self.nav_indicator.setGeometry(target)
            self.nav_indicator.show()
            self.nav_indicator.raise_()
            return
        self._indicator_animation.setStartValue(self.nav_indicator.geometry())
        self._indicator_animation.setEndValue(target)
        self._indicator_animation.start()

    def _sync_indicator(self):
        self.set_active(self.group.checkedId(), animated=False)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_indicator)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_indicator)


class MainWindow(QMainWindow):
    def __init__(
        self,
        database_path: str | Path | None = None,
        controller: GameTuneController | None = None,
    ):
        super().__init__()
        self.state = TaskState(has_task=False)
        self.api_config_error = ""
        loaded_saved_config = False
        try:
            model_provider = provider_from_env()
        except ValueError as error:
            model_provider = None
            self.api_config_error = str(error)
        if model_provider is None and not self.api_config_error:
            saved_config = load_saved_api_config()
            if saved_config is not None:
                model_provider = OpenAICompatibleProvider(saved_config)
                loaded_saved_config = True
        self.controller = controller or GameTuneController(
            database_path or DATA_DIR / "gametune.db",
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
        self._planning_generation = 0
        self._planning_jobs: dict[int, tuple[QThread, ModelWorker, dict]] = {}
        self._pending_rollback: dict | None = None
        preview_task, preview_baseline = self._core_objects()
        self.current_evidence = analyze_evidence(preview_task, preview_baseline, fps_cap_known=True, vsync_known=True)
        self.current_plan = plan_next_experiment(preview_task, preview_baseline, self.current_evidence, 1)
        self.setWindowTitle("GameTune Agent · 游戏性能优化智能体")
        self.resize(1360, 900)
        self.setMinimumSize(1050, 700)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = set_margins(QHBoxLayout(root), spacing=0)
        self.toast_notice = ToastNotice(root)
        self.sidebar = Sidebar(self.state)
        self.stack = QStackedWidget()
        self._busy_operations: set[tuple[str, int]] = set()
        self.busy_indicator = BusyIndicator(parent=self)
        self.statusBar().addPermanentWidget(self.busy_indicator)
        current_record = (self.controller.current_record(self.core_task.task_id) or self.baseline_record
                          if self.core_task else None)
        self.dashboard = DashboardPage(self.state, self.current_evidence, self.current_plan, current_record)
        self.new_task = NewTaskPage(self.state)
        self.experiment = ExperimentPage(self.state, self.current_plan)
        self.experiment.set_completed()
        self.history = HistoryPage(self.state, self._available_tasks())
        self.report = ReportPage(self.state)
        self.api_settings = APISettingsPage(initial_api_config)
        for page in [self.dashboard, self.new_task, self.experiment, self.history, self.report, self.api_settings]:
            self.stack.addWidget(page)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.sidebar.navigate.connect(self.set_page)
        self.sidebar.help_requested.connect(self._show_usage_guide)
        self.dashboard.experiment_requested.connect(lambda: self.set_page(2))
        self.dashboard.task_requested.connect(lambda: self.set_page(1))
        self.new_task.task_created.connect(self._task_created)
        self.new_task.notice_requested.connect(self._show_toast)
        self.experiment.result_saved.connect(self._result_saved)
        self.experiment.task_requested.connect(lambda: self.set_page(1))
        self.experiment.overview_requested.connect(lambda: self.set_page(0))
        self.experiment.notice_requested.connect(self._show_toast)
        self.experiment.rollback_requested.connect(self._confirm_pending_rollback)
        self.experiment.retry_diagnostic_requested.connect(self._retry_invalid_diagnostic)
        self.history.task_restore_requested.connect(self._restore_task)
        self.api_settings.provider_applied.connect(self._apply_model_provider)
        self.api_settings.notice_requested.connect(self._show_toast)
        if self.api_config_error:
            self.statusBar().showMessage(f"模型未启用 · {self.api_config_error}", 0)
        elif model_provider:
            source = "已载入本机配置" if loaded_saved_config else "已连接模型"
            self.statusBar().showMessage(f"就绪 · {source} {model_provider.name}", 0)
        else:
            self.statusBar().showMessage("就绪 · 当前使用本地规则", 0)

    def _show_usage_guide(self):
        if not hasattr(self, "_usage_guide"):
            self._usage_guide = UsageGuideDialog(self)
        self._usage_guide.show()
        self._usage_guide.raise_()
        self._usage_guide.activateWindow()

    def _apply_model_provider(self, provider: OpenAICompatibleProvider):
        self.controller.model_provider = provider
        mode = provider.config.reasoning_effort or "关闭"
        self.statusBar().showMessage(f"模型配置已应用 · {provider.config.model} · 思考模式 {mode}", 5000)
        if self.core_task and self.current_plan and self.current_plan.action != AgentAction.FINISH:
            self._start_plan_selection()

    def set_page(self, index: int):
        if not 0 <= index < self.stack.count():
            return
        fade_stack_to(self.stack, index, self)
        self.sidebar.set_active(index)

    def _show_toast(self, message: str, variant: str = "success"):
        self.toast_notice.show_message(message, variant)

    def _set_busy_operation(self, category: str, job_id: int, active: bool):
        key = (category, job_id)
        if active:
            self._busy_operations.add(key)
        else:
            self._busy_operations.discard(key)
        self.busy_indicator.set_running(bool(self._busy_operations))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toast_notice") and self.toast_notice.isVisible():
            self.toast_notice._position()

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
            self._apply_record_to_state(current)
            current_configuration = current.configuration or {}
            cap_value = current_configuration.get("fps_cap", task.fps_cap)
            vsync_value = current_configuration.get("vsync", task.vsync)
            self.state.fps_cap_checked = cap_value not in (None, "", "unknown", "未确认")
            self.state.vsync_checked = vsync_value not in (None, "", "unknown", "未确认")
            self.state.baseline_repeats = baseline.repeats
            self.state.baseline_duration_seconds = baseline.duration_seconds or 60
            self.state.baseline_confirmed = baseline.configuration_confirmed
            self.current_evidence = analyze_evidence(
                task,
                current,
                baseline,
                fps_cap_known=self.state.fps_cap_checked,
                vsync_known=self.state.vsync_checked,
                diagnostic_results=self.controller.diagnostic_results(task_id),
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
                last_decision=self.controller.last_decisions[task_id].to_dict() if task_id in self.controller.last_decisions else (
                    (bundle or {}).get("decisions", [])[-1] if (bundle or {}).get("decisions") else None
                ),
            )
            runtime_state = str(runtime.get("workflow_state", ""))
            self.experiment.set_rollback_pending(runtime_state == "awaiting_rollback")
            if runtime_state in {"complete", "completed", "finished"}:
                self.experiment.set_completed(True)
            self.statusBar().showMessage(f"已恢复任务 · {task.name}", 5000)
            # A pending rollback is an actionable workflow state; open the
            # experiment page so the recovery button is immediately visible.
            self.set_page(2 if runtime_state == "awaiting_rollback" else 0)
            last_decision = self.controller.last_decisions.get(task_id)
            if (
                runtime_state == "awaiting_test"
                and self.current_plan is not None
                and self.current_plan.planning_source == "local"
                and not (last_decision and last_decision.retry_same_experiment)
            ):
                self._start_plan_selection()
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "恢复任务失败", str(error))

    def _load_state_from_task(self, task: OptimizationTask, current: BenchmarkRecord):
        for field_name in ("name", "game", "cpu", "gpu", "resolution", "refresh_rate", "test_scene",
                           "target_avg_fps", "target_low_fps", "mode", "is_laptop", "gpu_power_w",
                           "graphics_mode", "power_mode", "cooling_mode", "display_mode", "fps_cap",
                           "vsync", "shadow_quality", "dynamic_shadows", "video_settings"):
            state_name = {"target_avg_fps": "target_avg", "target_low_fps": "target_low"}.get(field_name, field_name)
            if hasattr(task, field_name) and hasattr(self.state, state_name):
                setattr(self.state, state_name, getattr(task, field_name))
        self.state.avg_fps = round(current.avg_fps)
        self.state.low_fps = round(current.low_fps)
        self.state.gpu_usage = round(current.gpu_usage or self.state.gpu_usage)
        self._apply_record_to_state(current)
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
                fps_cap_known=self.state.fps_cap_checked,
                vsync_known=self.state.vsync_checked,
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
        self._planning_generation += 1
        self.current_evidence, self.current_plan = evidence, plan
        self._rebuild_task_pages(reset_results=True)
        if plan.action == AgentAction.FINISH:
            self.state.step = "优化完成"
            self.experiment.set_completed(True)
        self._refresh_task_selector()
        self.sidebar.update_task()
        self.statusBar().showMessage(f"任务已保存 · 已生成 {len(evidence)} 条证据", 5000)
        if plan.action != AgentAction.FINISH:
            self._start_plan_selection()
        self.set_page(0)
        self._show_toast(f"任务已保存 · 下一步：{plan.title}")

    def _sync_limit_state(self):
        if not self.core_task or not hasattr(self.controller, "limit_context"):
            return
        context = self.controller.limit_context(self.core_task.task_id)
        if context.get("status") == "restored":
            original = context.get("original_limits", {})
            cap = str(original.get("fps_cap", ""))
            if cap.isdigit():
                self.state.fps_cap = int(cap)
            if original.get("vsync") in {"enabled", "disabled"}:
                self.state.vsync = original["vsync"]
        elif context.get("status") == "active" and context.get("applied"):
            self.state.fps_cap = 0
            self.state.vsync = "disabled"

    def _rebuild_task_pages(self, reset_results: bool = False):
        """Rebuild pages whose labels are derived from the newly saved task."""
        self._sync_limit_state()
        old_dashboard = self.dashboard
        self.stack.removeWidget(old_dashboard)
        old_dashboard.deleteLater()
        current_record = (self.controller.current_record(self.core_task.task_id) or self.baseline_record
                          if self.core_task else None)
        self.dashboard = DashboardPage(self.state, self.current_evidence, self.current_plan, current_record)
        self.dashboard.experiment_requested.connect(lambda: self.set_page(2))
        self.dashboard.task_requested.connect(lambda: self.set_page(1))
        self.stack.insertWidget(0, self.dashboard)

        old_experiment = self.experiment
        self.stack.removeWidget(old_experiment)
        old_experiment.deleteLater()
        self.experiment = ExperimentPage(self.state, self.current_plan)
        self.experiment.result_saved.connect(self._result_saved)
        self.experiment.task_requested.connect(lambda: self.set_page(1))
        self.experiment.overview_requested.connect(lambda: self.set_page(0))
        self.experiment.notice_requested.connect(self._show_toast)
        self.experiment.rollback_requested.connect(self._confirm_pending_rollback)
        self.experiment.retry_diagnostic_requested.connect(self._retry_invalid_diagnostic)
        self.stack.insertWidget(2, self.experiment)
        last = getattr(self.controller, "last_decisions", {}).get(self.core_task.task_id) if self.core_task else None
        if last and (last.retry_same_experiment or last.rollback_required):
            self.experiment.show_evaluation(last)
        if self.core_task and getattr(getattr(self.controller, "workflows", {}).get(self.core_task.task_id), "state", "") == "awaiting_rollback":
            self.experiment.set_rollback_pending(True)

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

    def _retry_invalid_diagnostic(self):
        if self.core_task is None:
            return
        try:
            self.current_plan = self.controller.retry_invalid_diagnostic(self.core_task)
        except (ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, "无法继续诊断", str(error))
            return
        self._task_generation += 1
        self._pending_rollback = None
        self.state.step = "等待核对实际配置"
        self._rebuild_task_pages()
        self.set_page(2)

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
            evaluated_plan = deepcopy(self.current_plan)
            decision = self.controller.evaluate(self.core_task, self.baseline_record, candidate, evaluated_round, reference=reference)
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "结果无法评估", str(error))
            return
        context = {"result": result, "candidate": candidate, "decision": decision, "round": evaluated_round, "evaluated_plan": evaluated_plan}
        if decision.rollback_required:
            if decision.accepted:
                self.current_plan = self.controller.pending_plan(self.core_task.task_id) or self.current_plan
                self._rebuild_task_pages()
            diagnostic = bool(decision.diagnostic_summary)
            title = "恢复诊断前的配置" if diagnostic else ("按目标恢复原限帧" if decision.accepted else "确认恢复配置")
            message = (
                f"{decision.diagnostic_summary}\n\n这轮只用于排查原因，不会作为优化结果保留。是否已经恢复原配置？"
                if diagnostic
                else decision.summary + ("\n\n是否已按所选目标恢复帧率限制？" if decision.accepted else "\n\n请按计划中的恢复方法操作。是否已经恢复？")
            )
            answer = QMessageBox.question(
                self,
                title,
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._pending_rollback = context
                self.experiment.show_evaluation(decision)
                self.experiment.set_rollback_pending(True)
                self.statusBar().showMessage("已暂停 · 请先恢复上一组配置", 0)
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
        if plan.action != AgentAction.FINISH:
            self._start_plan_selection()
        self.set_page(0)
        self._show_toast("已记录补充信息，并更新下一步计划")

    def _sync_state_from_core_task(self):
        if not self.core_task:
            return
        for field_name in (
            "name", "game", "cpu", "gpu", "resolution", "refresh_rate", "test_scene",
            "target_avg_fps", "target_low_fps", "mode", "is_laptop", "gpu_power_w", "fps_cap",
            "graphics_mode", "power_mode", "cooling_mode", "vsync", "display_mode",
            "shadow_quality", "dynamic_shadows", "video_settings",
        ):
            state_name = {"target_avg_fps": "target_avg", "target_low_fps": "target_low"}.get(field_name, field_name)
            if hasattr(self.core_task, field_name) and hasattr(self.state, state_name):
                setattr(self.state, state_name, getattr(self.core_task, field_name))
        self.state.fps_cap_checked = self.core_task.fps_cap is not None
        self.state.vsync_checked = self.core_task.vsync != "unknown"

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
                elif self.current_plan.planning_source == "local":
                    self._start_plan_selection()
                self._replace_report(best_record=self.controller.best_record(self.core_task.task_id))
                self.statusBar().showMessage("已确认恢复配置 · 已生成下一轮计划", 5000)
            except (ValueError, TypeError, KeyError) as error:
                QMessageBox.warning(self, "恢复配置失败", str(error))
            return
        answer = QMessageBox.question(
            self,
            "确认恢复配置",
            "确认已经恢复上一组配置，并开始下一轮测试？",
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
        if str(config.get("fps_cap", "")).isdigit():
            self.state.fps_cap = int(config["fps_cap"])
        for key, state_name in (("resolution", "resolution"), ("refresh_rate", "refresh_rate"), ("test_scene", "test_scene"),
                                ("cpu", "cpu"), ("gpu", "gpu"), ("display_mode", "display_mode"),
                                ("vsync", "vsync"), ("shadow_quality", "shadow_quality"),
                                ("dynamic_shadows", "dynamic_shadows"), ("graphics_mode", "graphics_mode"),
                                ("power_mode", "power_mode"), ("cooling_mode", "cooling_mode")):
            if config.get(key):
                setattr(self.state, state_name, str(config[key]))
        known_video_keys = set((self.state.video_settings or {}).keys()) | set(ADVANCED_SETTING_LABELS) | {"video_preset", "player_contrast", "nvidia_gsync", "nvidia_reflex", "amd_anti_lag", "menu_fps_cap"}
        updated_video = {
            key: str(value) for key, value in config.items()
            if key in known_video_keys and value not in (None, "")
        }
        if updated_video:
            self.state.video_settings = {**(self.state.video_settings or {}), **updated_video}

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
        if decision.stop_reason == "player_profile":
            current = self.controller.current_record(self.core_task.task_id) if self.core_task else candidate
            self._apply_record_to_state(current or candidate)
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
        self._sync_limit_state()
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
            self.state.step = "已更新实际配置，继续优化" if decision.stop_reason == "player_profile" else "诊断实验完成" if decision.diagnostic_summary else "复测评估完成"
        self.sidebar.update_task()
        self.history.add_result(result, decision, evaluated_round)
        self._show_toast("复测结果已保存，正在整理分析")
        if decision.action == AgentAction.RETEST:
            current = self.controller.current_record(self.core_task.task_id) if self.core_task else candidate
            if current:
                self._apply_record_to_state(current)
            self.current_plan = self._same_retest_plan(self.state.round_number)
            self.experiment.show_evaluation(decision)
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
        awaiting_restore = bool(self.core_task and getattr(getattr(self.controller, "workflows", {}).get(self.core_task.task_id), "state", "") == "awaiting_rollback")
        if awaiting_restore:
            task_complete = False
            self.state.step = "等待恢复原限帧"
            self.experiment.set_rollback_pending(True)
        elif task_complete:
            self.experiment.set_completed(True)
        else:
            self.experiment.set_rollback_pending(False)
        if not task_complete and self.current_plan is not None:
            self._start_plan_selection(explanation_context=context)
        else:
            self._start_explanation(context)

    def _start_plan_selection(self, explanation_context: dict | None = None):
        """Choose among safe local plans without blocking the desktop UI."""
        self._planning_generation += 1
        self.experiment.set_planning(False)
        provider = getattr(self.controller, "model_provider", None)
        if provider is None or self.core_task is None or self.current_plan is None:
            if explanation_context is not None:
                self._start_explanation(explanation_context)
            return
        workflow = self.controller.workflows.get(self.core_task.task_id)
        if workflow is None or workflow.state.value != "awaiting_test":
            if explanation_context is not None:
                self._start_explanation(explanation_context)
            return
        current = self.controller.current_record(self.core_task.task_id) or self.baseline_record
        if current is None:
            if explanation_context is not None:
                self._start_explanation(explanation_context)
            return
        try:
            candidates = self.controller.next_step_candidates(
                self.core_task, current, self.current_evidence, workflow.round_number,
            )
        except (ValueError, TypeError):
            candidates = []
        if len(candidates) < 2:
            if explanation_context is not None:
                self._start_explanation(explanation_context)
            return

        job_id = self._planning_generation
        task_snapshot = deepcopy(self.core_task)
        if hasattr(self.controller, "planning_record"):
            current = self.controller.planning_record(self.core_task.task_id) or current
        current_snapshot = deepcopy(current)
        evidence_snapshot = deepcopy(self.current_evidence)
        candidate_snapshot = deepcopy(candidates)
        history_snapshot = deepcopy(self.controller.experiment_history(task_snapshot.task_id))
        expected_plan_id = plan_candidate_id(self.current_plan)
        allowed_ids = {plan_candidate_id(plan) for plan in candidate_snapshot}
        job_context = {
            "task_generation": self._task_generation,
            "task_id": task_snapshot.task_id,
            "expected_plan_id": expected_plan_id,
            "allowed_candidate_ids": allowed_ids,
            "candidates": candidate_snapshot,
            "explanation_context": explanation_context,
        }
        thread = QThread(self)
        worker = ModelWorker(
            lambda: self.controller.choose_next_step(
                task_snapshot,
                current_snapshot,
                evidence_snapshot,
                candidate_snapshot,
                history_snapshot,
            ),
            job_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished_with_id.connect(self._planning_finished)
        worker.failed_with_id.connect(self._planning_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda token=job_id, running_thread=thread: self._planning_thread_finished(token, running_thread))
        self._planning_jobs[job_id] = (thread, worker, job_context)
        self._set_busy_operation("planning", job_id, True)
        self.experiment.set_planning(True)
        self.statusBar().showMessage("AI 正在比较可执行的下一步，当前计划已暂时锁定……", 0)
        thread.start()

    def _planning_context(self, job_id: int) -> dict | None:
        entry = self._planning_jobs.get(job_id)
        if not entry or job_id != self._planning_generation:
            return None
        context = entry[2]
        if context.get("task_generation") != self._task_generation:
            return None
        if not self.core_task or context.get("task_id") != self.core_task.task_id:
            return None
        return context

    @Slot(int, object)
    def _planning_finished(self, job_id: int, selection):
        context = self._planning_context(job_id)
        if context is None:
            return
        applied = False
        try:
            applied = self.controller.accept_model_plan(
                self.core_task,
                context["expected_plan_id"],
                selection.plan,
                context["allowed_candidate_ids"],
            )
        except (ValueError, TypeError, KeyError):
            applied = False
        if applied:
            page_index = self.stack.currentIndex()
            self.current_plan = selection.plan
            self._rebuild_task_pages()
            self.experiment.set_planning(False)
            self.set_page(page_index)
            if selection.source == "model":
                message = f"AI 已选择下一步：{selection.plan.title}"
                if selection.plan.selection_reason:
                    message += f" · {selection.plan.selection_reason}"
            else:
                message = selection.warning or "AI 规划暂不可用，已保留本地规则建议。"
            self.statusBar().showMessage(message, 7000)
        else:
            self.experiment.set_planning(False)
            self.statusBar().showMessage("计划状态已变化，保留当前有效计划。", 5000)
        explanation_context = context.get("explanation_context")
        if explanation_context is not None:
            self._start_explanation(explanation_context)

    @Slot(int, object)
    def _planning_failed(self, job_id: int, error: Exception):
        context = self._planning_context(job_id)
        if context is None:
            return
        self.experiment.set_planning(False)
        self.statusBar().showMessage("AI 规划暂不可用，继续使用本地规则建议。", 6000)
        explanation_context = context.get("explanation_context")
        if explanation_context is not None:
            self._start_explanation(explanation_context)

    def _planning_thread_finished(self, job_id: int, thread: QThread):
        self._planning_jobs.pop(job_id, None)
        self._set_busy_operation("planning", job_id, False)
        thread.deleteLater()

    def _start_explanation(self, context: dict):
        self._explanation_generation += 1
        job_id = self._explanation_generation
        if self.core_task is None:
            return
        task_snapshot = deepcopy(self.core_task)
        decision_snapshot = deepcopy(context["decision"])
        evidence_snapshot = deepcopy(self.current_evidence)
        plan_snapshot = deepcopy(self.current_plan)
        workflow_snapshot = {
            "submitted_result": context["candidate"].to_dict() if context.get("candidate") else None,
            "evaluated_plan": _plan_dict(context.get("evaluated_plan")),
            "rollback_confirmed": bool(context.get("rollback_confirmed")),
        }
        job_context = {
            **context,
            "generation": job_id,
            "task_generation": self._task_generation,
            "task_id": self.core_task.task_id,
            "task": task_snapshot,
            "decision_snapshot": decision_snapshot,
            "evidence_snapshot": evidence_snapshot,
            "plan_snapshot": plan_snapshot,
            "workflow_snapshot": workflow_snapshot,
        }
        thread = QThread(self)
        worker = ModelWorker(lambda: self.controller.explain(
            task_snapshot,
            decision_snapshot,
            evidence=evidence_snapshot,
            plan=plan_snapshot,
            workflow=workflow_snapshot,
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
        self._set_busy_operation("explanation", job_id, True)
        self._explanation_thread, self._explanation_worker = thread, worker
        self.statusBar().showMessage("正在整理结果，界面仍可操作……", 0)
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
        action = "按实际配置继续" if decision.stop_reason == "player_profile" else action_names.get(decision.action, decision.action.value)
        details = "\n".join(f"• {item}" for item in explanation.get("reasoning", []))
        warning = f"\n\n{explanation['warning']}" if explanation.get("warning") else ""
        QMessageBox.information(
            self, "评估已生成",
            f"结论：{action}\n{explanation['summary']}\n\n{details}\n\n下一步：{explanation['next_step']}{warning}",
        )
        self.statusBar().showMessage(f"复测结果已保存 · {action}", 6000)
        self.set_page(4 if decision.task_complete else 2)

    @Slot(int, object)
    def _explanation_failed(self, job_id: int, error: Exception):
        context = self._explanation_context(job_id)
        if context is None:
            return
        decision = context["decision"]
        fallback = explain_decision(
            None, context["task"], decision,
            plan=context.get("plan_snapshot"),
            workflow=context.get("workflow_snapshot"),
        )
        fallback["warning"] = "模型暂不可用，已改用本地说明。"
        self._explanation_finished(job_id, fallback)

    def _explanation_thread_finished(self, job_id: int, thread: QThread):
        self._explanation_jobs.pop(job_id, None)
        self._set_busy_operation("explanation", job_id, False)
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
        report_baseline = self.baseline_record
        limit_note = ""
        if self.core_task and hasattr(self.controller, "limit_context"):
            context = self.controller.limit_context(self.core_task.task_id)
            limit_note = self.controller.final_limit_note(self.core_task.task_id)
            if context.get("reference_record") and best_record and best_record.record_id != context.get("origin_record_id"):
                report_baseline = BenchmarkRecord(**context["reference_record"])
        self.report = ReportPage(
            self.state, result, decision, explanation, best_record,
            report_baseline, completed_count, max_rounds, last_decision, limit_note,
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
            display_mode=self.state.display_mode,
            fps_cap=self.state.fps_cap,
            vsync=self.state.vsync,
            shadow_quality=self.state.shadow_quality,
            dynamic_shadows=self.state.dynamic_shadows,
            video_settings=dict(self.state.video_settings or {}),
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
            configuration={
                "resolution": self.state.resolution,
                "refresh_rate": self.state.refresh_rate,
                "test_scene": self.state.test_scene,
                "display_mode": self.state.display_mode,
                "fps_cap": "unknown" if self.state.fps_cap is None else str(self.state.fps_cap),
                "vsync": self.state.vsync,
                "shadow_quality": self.state.shadow_quality,
                "dynamic_shadows": self.state.dynamic_shadows,
                **(self.state.video_settings or {}),
            },
            runs=[dict(run) for run in (self.state.baseline_runs or [])],
        )
        return task, baseline

    def closeEvent(self, event):
        # Stop the planning, explanation, and API settings workers.  A
        # provider call may be blocking in urllib, so use a bounded wait and
        # terminate only as a last resort during application shutdown.
        self.api_settings.stop_workers()
        self._explanation_generation += 1
        self._planning_generation += 1
        active_jobs = list(self._planning_jobs.values()) + list(self._explanation_jobs.values())
        for thread, _worker, _context in active_jobs:
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(1000):
                thread.terminate()
                thread.wait(1000)
        self._planning_jobs.clear()
        self._explanation_jobs.clear()
        super().closeEvent(event)


def run_app(screenshot: Path | None = None, page_index: int = 0) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("GameTune Agent")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
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
