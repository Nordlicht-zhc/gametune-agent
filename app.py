from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

from PySide6.QtCore import QObject, QEvent, Qt, QThread, QTimer, Signal, Slot, Property, QPropertyAnimation, QEasingCurve, QRectF, QSequentialAnimationGroup, QParallelAnimationGroup, QPauseAnimation
from PySide6.QtGui import QFont, QPixmap, QPainter, QPen, QColor, QBrush, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QAbstractSpinBox, QDoubleSpinBox, QStyle, QStyleOptionComboBox,
    QCheckBox,
    QGraphicsOpacityEffect,
)

from agent.controller import GameTuneController
from agent.api import (
    APIConfig, ModelAPIError, ModelMessage, ModelRequest, ModelResponse,
    OpenAICompatibleProvider, provider_from_env,
)
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from tools.import_benchmark import import_benchmark_csv
from tools.analyze_evidence import analyze_evidence, dominant_hypothesis, evidence_support_level
from tools.calculate_metrics import normalized_score
from tools.resolution import RESOLUTION_PRESETS, aspect_ratio_name, normalize_resolution
from tools.hardware_catalog import HardwareCatalog, HardwareEntry
from tools.api_settings import (
    LocalAPISettingsError,
    clear_saved_api_config,
    load_saved_api_config,
    save_saved_api_config,
)
from agent.planner import GENERIC_OPTIMIZATION_DIRECTIONS, plan_candidate_id, plan_next_experiment


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


class RoundedComboBox(QComboBox):
    """Clip the popup window as well as painting a rounded list border."""

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
        color = QColor("#505752" if self.isEnabled() else "#a5aca6")
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
QPushButton#navButton:checked { color: #ffffff; background: #343a35; font-weight: 700; }
QPushButton#navButton:pressed { background: #414842; }
QFrame#navIndicator { background: #d7ff62; border-radius: 2px; }
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
QLabel#warningBox { color: #95501f; background: #ffefe2; border: 1px solid #f5d8c1; border-radius: 7px; padding: 6px 10px; min-height: 28px; }
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
QPushButton#primaryButton:pressed { background: #151916; }
QPushButton#secondaryButton { color: #333834; background: #ffffff; border: 1px solid #d9ddd8; border-radius: 9px; padding: 8px 14px; font-weight: 700; }
QPushButton#secondaryButton:hover { background: #f1f3ef; }
QPushButton#secondaryButton:pressed { background: #e5e9e2; border-color: #aeb7ae; }
QFrame#toastNotice { background: #202421; border: 1px solid #3b413c; border-radius: 10px; }
QFrame#toastNotice[variant="success"] { border-left: 3px solid #b6eb32; }
QFrame#toastNotice[variant="info"] { border-left: 3px solid #85b6ee; }
QFrame#toastNotice[variant="error"] { border-left: 3px solid #f29a73; }
QLabel#toastText { color: #f6f7f3; font-size: 12px; font-weight: 700; }
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
QComboBox { border-radius: 10px; padding-right: 30px; }
QComboBox QLineEdit { border: none; border-radius: 0; padding: 0; background: transparent; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; border-top-right-radius: 10px; border-bottom-right-radius: 10px; background: transparent; }
QComboBox::down-arrow { image: none; width: 0; height: 0; }
QComboBox QAbstractItemView { border-radius: 10px; padding: 5px; background: #ffffff; color: #202422; border: 1px solid #cfd5cf; outline: none; selection-background-color: #b8e64a; selection-color: #172018; }
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


def fade_stack_to(stack: QStackedWidget, index: int, parent: QObject, duration: int = 165) -> None:
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


class CurrentPageStack(QStackedWidget):
    """Let a scrollable form size itself to the visible step only."""

    def __init__(self):
        super().__init__()
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def sizeHint(self):
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else super().sizeHint()

    def minimumSizeHint(self):
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget is not None else super().minimumSizeHint()


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
    def __init__(self, color: str = "#b6eb32", parent: QWidget | None = None):
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
        meta_label = label(meta, "sectionMeta")
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
        self._progress_value = max(0, min(progress, 100))
        layout = set_margins(QVBoxLayout(self), 15, 13, 15, 13, 7)
        layout.addWidget(label(title.upper(), "cardKicker"))
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
        root.addWidget(PageHeader("性能概览", "当前性能与下一步", "看看游戏现在跑得怎么样，以及接下来该做什么。", "可以开始"))
        root.addWidget(SectionHeader("当前表现", "优化前的测试结果（中位数）"))
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        cards = [
            MetricCard("平均帧率", f"{state.avg_fps} FPS", f"目标 {state.target_avg} FPS", round(state.avg_fps / state.target_avg * 100)),
            MetricCard("1% LOW", f"{state.low_fps} FPS", "当前重点", round(state.low_fps / state.target_low * 100)),
            MetricCard("GPU 占用", f"{state.gpu_usage}%", "帮助分析性能受限的原因", state.gpu_usage),
            MetricCard("目标完成度", f"{self._score():.0f} / 100", self._mode_name(), round(self._score())),
        ]
        for index, card in enumerate(cards):
            metrics.addWidget(card, 0, index)
        root.addLayout(metrics)
        root.addWidget(SectionHeader("当前判断", "根据你提供的测试结果分析"))
        diagnosis = QHBoxLayout()
        diagnosis.setSpacing(14)
        diagnosis.addWidget(self._hero(), 3)
        diagnosis.addWidget(self._timeline(), 2)
        root.addLayout(diagnosis)
        root.addWidget(SectionHeader("下一步", "每次只改一项设置"))
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
        layout.addWidget(label("当前判断", "cardKicker"))
        hypothesis, _ = dominant_hypothesis(self.evidence)
        names = {"cpu": "更可能受 CPU 限制", "cpu_or_other": "GPU 渲染负担可能不是主要限制", "gpu": "更可能受 GPU 限制", "frametime": "帧率稳定性需要改善", "platform": "笔记本平台状态可能影响性能", "insufficient": "数据还不够，先补充测试"}
        layout.addWidget(label(names.get(hypothesis, "需要补充证据"), "heroTitle", True))
        body_text = "；".join(item.detail for item in self.evidence[:2]) or "创建任务后，这里会显示最重要的两条判断依据。"
        layout.addWidget(label(body_text, "heroBody", True))
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

    def _timeline(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = set_margins(QVBoxLayout(card), 18, 16, 18, 16, 8)
        layout.addWidget(label("优化进度", "cardKicker"))
        hypothesis, _ = dominant_hypothesis(self.evidence)
        hypothesis_name = {"cpu": "CPU 侧限制", "cpu_or_other": "CPU 侧或其他限制待区分", "gpu": "GPU 侧限制", "frametime": "帧时间稳定性", "platform": "笔记本平台状态待核对", "insufficient": "待补充证据"}.get(hypothesis, "待补充证据")
        items = [
            ("✓", "任务信息", "硬件和目标已保存", True),
            ("✓", "基线数据", "测试数据已检查", True),
            ("3", "分析原因", f"当前判断：{hypothesis_name}", bool(self.evidence)),
            ("4", "验证建议", "等待录入复测结果", bool(self.plan)),
            ("5", "生成报告", "保存最佳已验证配置", self.state.step == "优化完成"),
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
        if plan:
            purpose = "诊断测试" if plan.purpose == "diagnostic" else "优化验证"
            source = "AI 按本轮证据选择" if plan.planning_source == "model" else "本地规则建议"
            kicker = f"{purpose} {plan.round_number:02d} · {source}"
        else:
            kicker = "等待任务分析"
        layout.addWidget(label(kicker, "cardKicker"))
        title_row = QHBoxLayout()
        title_row.addWidget(label("01", "actionNumber"))
        title_row.addWidget(label(plan.title if plan else "创建任务后生成下一步", "actionTitle"))
        title_row.addStretch()
        layout.addLayout(title_row)
        settings = QHBoxLayout()
        settings.addWidget(label(setting_value_label(plan.setting_id, plan.old_value) if plan else "当前值", "settingOld"))
        settings.addWidget(label("→", "sectionMeta"))
        settings.addWidget(label(setting_value_label(plan.setting_id, plan.new_value) if plan else "待生成", "settingNew"))
        settings.addStretch()
        layout.addLayout(settings)
        rows = [("为什么做", plan.hypothesis), ("怎么测试", plan.method), ("什么算有效", plan.success_criteria)] if plan else [("说明", "创建任务后，这里会显示下一步。")]
        if plan and plan.selection_reason:
            rows.insert(0, ("AI 选择理由", plan.selection_reason))
        for name, text in rows:
            layout.addWidget(hline())
            layout.addWidget(RuleRow(name, text))
        buttons = QHBoxLayout()
        primary = QPushButton("录入测试结果")
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
        card.setObjectName("card")
        layout = set_margins(QVBoxLayout(card), 18, 16, 18, 16, 4)
        layout.addWidget(label("当前环境", "cardKicker"))
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
    notice_requested = Signal(str, str)

    def __init__(self, state: TaskState):
        super().__init__()
        self.state = state
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        root.addWidget(PageHeader("任务设置", "准备优化任务", "先填电脑配置和游戏设置，再测一下优化前的帧率。"))
        self.setup_stage = QWidget()
        self.stage_stack = CurrentPageStack()
        self.stage_stack.addWidget(self.setup_stage)
        root.addWidget(self.stage_stack)
        setup_layout = set_margins(QVBoxLayout(self.setup_stage), spacing=18)
        columns = QHBoxLayout()
        columns.setSpacing(15)
        basic_card, basic_form = self._form_card("设备与测试环境")
        goal_card, goal_form = self._form_card("优化目标")
        columns.addWidget(basic_card, 1)
        columns.addWidget(goal_card, 1)
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
        self.scene_input = QLineEdit(state.test_scene)
        self.display_mode_input = RoundedComboBox()
        self.display_mode_input.addItem("全屏", "fullscreen")
        self.display_mode_input.addItem("全屏窗口模式", "borderless")
        self.display_mode_input.addItem("窗口模式", "windowed")
        self.display_mode_input.setCurrentIndex(max(0, self.display_mode_input.findData(state.display_mode)))
        self.cap_input = self._spin(-1, 1000, state.fps_cap if state.fps_cap is not None else -1, 10)
        self.cap_input.setSpecialValueText("未确认")
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
        self.gpu_power_input = self._spin(0, 250, state.gpu_power_w or 0, 5)
        self.gpu_power_input.setSpecialValueText("未知")
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
            "用相同的场景和设置测 2 次，每次至少 60 秒。两次结果差得多，就再测一次。",
        ))
        baseline_card, baseline_form = self._form_card("逐次测试结果")
        self.baseline_run_inputs: list[dict[str, QSpinBox]] = []
        self.baseline_run_rows: list[QWidget] = []
        for index in range(3):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            avg_input = self._spin(0, 2000, 0)
            avg_input.setSpecialValueText("未填写")
            low_input = self._spin(0, 2000, 0)
            low_input.setSpecialValueText("未填写")
            gpu_input = self._spin(0, 100, 0)
            gpu_input.setSpecialValueText("未记录")
            row_layout.addWidget(QLabel("平均 FPS"))
            row_layout.addWidget(avg_input, 1)
            row_layout.addWidget(QLabel("1% Low"))
            row_layout.addWidget(low_input, 1)
            row_layout.addWidget(QLabel("GPU 占用 %"))
            row_layout.addWidget(gpu_input, 1)
            baseline_form.addRow(f"第 {index + 1} 次", row)
            self.baseline_run_inputs.append({"avg_fps": avg_input, "low_fps": low_input, "gpu_usage": gpu_input})
            self.baseline_run_rows.append(row)
            for widget in (avg_input, low_input, gpu_input):
                widget.valueChanged.connect(self._refresh_baseline_guidance)
            if index == 2:
                row.hide()
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
            self.baseline_guidance.setText("先录入前两次实际结果；GPU 占用可不填。")
            return
        averages = [run["avg_fps"].value() for run in first_two]
        lows = [run["low_fps"].value() for run in first_two]

        def spread(values: list[int]) -> float:
            centre = median(values)
            return (max(values) - min(values)) / centre * 100 if centre else float("inf")

        needs_third = max(spread(averages), spread(lows)) > 5.0
        animate_visibility(self.baseline_run_rows[2], needs_third)
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
    rollback_requested = Signal()
    notice_requested = Signal(str, str)

    def __init__(self, state: TaskState, plan: ExperimentPlan | None = None):
        super().__init__()
        self.state = state
        self.plan = plan
        self.completed = False
        self.planning = False
        self.rollback_pending = False
        self._csv_metrics: dict | None = None
        self._csv_values: tuple[int, int, int, int] | None = None
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        root = set_margins(QVBoxLayout(body), 34, 27, 34, 36, 18)
        round_number = plan.round_number if plan else state.round_number
        if plan and plan.action == AgentAction.REQUEST_DATA:
            subtitle = "填一下你现在使用的设置，这一步不用测帧率。测试环境变了也请注明。"
        elif plan and plan.action == AgentAction.FINISH:
            subtitle = "暂时没有新的测试建议；补充程序支持的游戏设置后，可以继续优化。"
        elif plan and plan.purpose == "diagnostic":
            subtitle = "这次测试是为了找出性能受限的原因。测完后，按提示改回原设置或再测一次。"
        else:
            subtitle = "先测一次，帧率没明显变差就继续测满三次。测试环境变了请注明。"
        entry_type = "诊断测试" if plan and plan.purpose == "diagnostic" else "优化验证"
        root.addWidget(PageHeader(entry_type, f"录入第 {round_number} 轮结果", subtitle, "等待录入"))
        columns = QHBoxLayout()
        columns.setSpacing(15)
        input_card = QFrame()
        input_card.setObjectName("formCard")
        input_layout = set_margins(QVBoxLayout(input_card), 20, 18, 20, 20, 12)
        input_layout.addWidget(label("复测结果", "sectionTitle"))
        file_row = QHBoxLayout()
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("导入 PresentMon CSV（可选）")
        browse = QPushButton("浏览文件")
        self.browse_button = browse
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
        self.repeats = NewTaskPage._spin(1, 10, 1)
        self.duration = NewTaskPage._spin(10, 3600, 60)
        self.condition = RoundedComboBox()
        self.condition.addItems(["与基线一致", "有变化，需要说明"])
        self.actual_cpu = QLineEdit(state.cpu)
        self.actual_gpu = QLineEdit(state.gpu)
        self.actual_resolution = QLineEdit(state.resolution)
        self.actual_refresh_rate = QLineEdit(state.refresh_rate)
        self.actual_scene = QLineEdit(state.test_scene)
        self.actual_setting = ActualSettingInput(
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
        for field in self._actual_fields.values():
            field.textChanged.connect(self._clear_csv_if_manual_edit)
        self.actual_setting.currentTextChanged.connect(self._clear_csv_if_manual_edit)
        if plan and plan.action == AgentAction.REQUEST_DATA:
            confirmation_label = "我已核对并记录当前实际状态"
        elif plan and plan.purpose == "diagnostic":
            confirmation_label = "我已完成本轮诊断测试，并核对实际配置"
        else:
            confirmation_label = "我已完成本轮调整，并核对上述实际配置"
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
        input_layout.addWidget(self.configuration_confirmed)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("备注（可选）：记录测试中的异常或变化")
        self.notes.setMaximumHeight(85)
        input_layout.addWidget(self.notes)
        self.is_data_request = bool(self.plan and self.plan.action == AgentAction.REQUEST_DATA)
        self.save_button = QPushButton("提交信息" if self.is_data_request else "保存并分析")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self._save)
        input_layout.addWidget(self.save_button)
        self.rollback_button = QPushButton("确认已恢复上一组配置")
        self.rollback_button.setObjectName("secondaryButton")
        self.rollback_button.setVisible(False)
        self.rollback_button.clicked.connect(self.rollback_requested.emit)
        input_layout.addWidget(self.rollback_button)
        guide_box = QFrame()
        guide_box.setObjectName("card")
        guide_layout = set_margins(QVBoxLayout(guide_box), 20, 18, 20, 20, 7)
        guide_layout.addWidget(label("本轮只改这一项", "cardKicker"))
        change_title = f"{plan.setting}：{setting_value_label(plan.setting_id, plan.old_value)} → {setting_value_label(plan.setting_id, plan.new_value)}" if plan else "等待生成实验计划"
        guide_layout.addWidget(label(change_title, "actionTitle", True))
        guide_layout.addWidget(label("其他设置保持不变", "metricNote"))
        if plan and plan.action == AgentAction.REQUEST_DATA:
            test_requirement = "仅记录设置，不需要测试帧率"
        elif plan and plan.action == AgentAction.FINISH:
            test_requirement = "当前不需要新测试"
        else:
            test_requirement = "先测一次，帧率没明显变差就继续测满三次"
        for name, value in [("测试场景", state.test_scene), ("测试要求", test_requirement), ("基线数据", f"{state.avg_fps} / {state.low_fps} FPS"), ("有效变化", "平均帧率变化超过 5%")]:
            guide_layout.addWidget(hline())
            guide_layout.addWidget(RuleRow(name, value))
        if plan:
            guide_layout.addWidget(hline())
            guide_layout.addWidget(RuleRow("实验用途", "排查原因，结束后恢复原设置" if plan.purpose == "diagnostic" else "验证用户选定的优化调整"))
            if plan.selection_reason:
                guide_layout.addWidget(hline())
                guide_layout.addWidget(RuleRow("AI 选择理由", plan.selection_reason))
        guide_layout.addSpacing(8)
        guide_label = label("提示：GPU 占用低不一定是 CPU 瓶颈，也可能是限帧或垂直同步造成的。", "warningBox", True)
        guide_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        guide_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        guide_label.setMinimumHeight(max(30, guide_label.sizeHint().height()))
        guide_layout.addWidget(guide_label)
        guide_layout.addStretch()
        columns.addWidget(input_card, 3)
        columns.addWidget(guide_box, 2)
        root.addLayout(columns)
        root.addStretch()

    def _setting_value_from_state(self) -> str:
        """Return the confirmed current value for the planned setting."""
        plan = self.plan
        setting_id = str(getattr(plan, "setting_id", "") or "") if plan else ""
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

    def _clear_csv_if_manual_edit(self, *_args):
        if self._csv_metrics is not None:
            self._csv_metrics = None
            self._csv_values = None

    def set_completed(self, completed: bool = True):
        self.completed = completed
        self._sync_input_enabled()
        for widget in [self.file_input, self.avg, self.low, self.gpu, self.repeats, self.duration, self.condition,
                        self.actual_cpu, self.actual_gpu, self.actual_resolution, self.actual_refresh_rate,
                        self.actual_scene, self.actual_setting, self.configuration_confirmed, self.notes,
                        self.adjustment_direction, self.adjustment_setting_name, self.adjustment_current_value,
                        self.adjustment_new_value]:
            widget.setEnabled(not completed and not self.planning)
        self.browse_button.setEnabled(not completed and not self.planning)

    def set_rollback_pending(self, pending: bool = True):
        self.rollback_pending = pending
        self.rollback_button.setVisible(pending)
        self._sync_input_enabled()

    def set_planning(self, planning: bool = True):
        self.planning = planning
        self._sync_input_enabled()
        for widget in [self.file_input, self.avg, self.low, self.gpu, self.repeats, self.duration, self.condition,
                       self.actual_cpu, self.actual_gpu, self.actual_resolution, self.actual_refresh_rate,
                       self.actual_scene, self.actual_setting, self.configuration_confirmed, self.notes,
                       self.adjustment_direction, self.adjustment_setting_name, self.adjustment_current_value,
                       self.adjustment_new_value]:
            widget.setEnabled(not planning and not self.completed)
        self.browse_button.setEnabled(not planning and not self.completed)

    def _sync_input_enabled(self):
        self.save_button.setEnabled(not self.completed and not self.rollback_pending and not self.planning)
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
            self.notes.setPlainText(f"已导入 CSV（{metrics['format']}）。保存前请核对测试条件。")
            self.notice_requested.emit("测试数据已导入，请核对测试条件", "success")

    def _save(self):
        if self.completed:
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
            "supplement_value": self._supplement_value(),
        })


class HistoryPage(QWidget):
    task_restore_requested = Signal(str)

    def __init__(self, state: TaskState, tasks: list[dict] | None = None):
        super().__init__()
        self.state = state
        self._highlight_generation = 0
        root = set_margins(QVBoxLayout(self), 34, 27, 34, 36, 16)
        root.addWidget(PageHeader("实验记录", "查看每轮测试结果", "查看每次测试的结果；测试环境相同，结果才能放在一起比较。"))
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
    ):
        super().__init__()
        self.setWidgetResizable(True)
        page_body = QWidget()
        self.setWidget(page_body)
        root = set_margins(QVBoxLayout(page_body), 34, 27, 34, 36, 16)
        reveal_targets: list[QWidget] = []
        root.addWidget(PageHeader("优化结果", "已验证的最佳配置", "查看测试后可以保留的设置，以及它们的实际表现。"))
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
        summary_text.setFixedWidth(610)
        summary_text.setMinimumHeight(42)
        empty_layout.addWidget(summary_text, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(empty)
        reveal_targets.append(empty)
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
            snapshot_layout.addWidget(SectionHeader("当前最佳配置", "已经测过并完成比较"))
            configuration = best_record.configuration or {"resolution": best_record.resolution, "test_scene": best_record.test_scene}
            for key, value in configuration.items():
                if value:
                    snapshot_layout.addWidget(RuleRow(key, value))
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
        status = "已评估" if decision else "等待实验"
        denominator = max(1, int(max_rounds))
        metric_cards = [
            MetricCard("已完成实验", f"{completed} / {denominator}", "实验进度", min(100, round(completed / denominator * 100))),
            MetricCard("平均 FPS 改善", f"{avg_delta:+.1f}%", status, min(100, max(0, 50 + round(avg_delta * 5)))),
            MetricCard("1% LOW 改善", f"{low_delta:+.1f}%", status, min(100, max(0, 50 + round(low_delta * 5)))),
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
        root.addWidget(PageHeader("模型设置", "模型说明设置", "连接 AI，帮你选择测试建议、解释结果。不连接也能使用。"))
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
        self.task_title = label(f"●  {state.name}", "sideTaskTitle", True)
        self.task_text = label(f"第 {state.round_number} 轮 · {state.step}\n{state.game}", "sideTaskText", True)
        task_layout.addWidget(self.task_title)
        task_layout.addWidget(self.task_text)
        root.addWidget(task)
        root.addStretch()
        root.addWidget(label("本地运行 · 数据保存在本机", "sideTaskText", True))

    def update_task(self):
        self.task_title.setText(f"●  {self.state.name}")
        self.task_text.setText(f"第 {self.state.round_number} 轮 · {self.state.step}\n{self.state.game}")

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
        self.state = TaskState()
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
        self._planning_generation = 0
        self._planning_jobs: dict[int, tuple[QThread, ModelWorker, dict]] = {}
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
        self.toast_notice = ToastNotice(root)
        self.sidebar = Sidebar(self.state)
        self.stack = QStackedWidget()
        self._busy_operations: set[tuple[str, int]] = set()
        self.busy_indicator = BusyIndicator(parent=self)
        self.statusBar().addPermanentWidget(self.busy_indicator)
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
        self.new_task.notice_requested.connect(self._show_toast)
        self.experiment.result_saved.connect(self._result_saved)
        self.experiment.notice_requested.connect(self._show_toast)
        self.experiment.rollback_requested.connect(self._confirm_pending_rollback)
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
        self.experiment.notice_requested.connect(self._show_toast)
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
            diagnostic = bool(decision.diagnostic_summary)
            title = "恢复诊断前的配置" if diagnostic else "确认撤销调整"
            message = (
                f"{decision.diagnostic_summary}\n\n这轮只用于排查原因，不会作为优化结果保留。是否已经恢复原配置？"
                if diagnostic
                else "本轮设置使性能下降。是否恢复上一组更好的配置？"
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
        known_video_keys = set((self.state.video_settings or {}).keys())
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
            self.state.step = "诊断实验完成" if decision.diagnostic_summary else "复测评估完成"
        self.sidebar.update_task()
        self.history.add_result(result, decision, evaluated_round)
        self._show_toast("复测结果已保存，正在整理分析")
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
        current_snapshot = deepcopy(current)
        evidence_snapshot = deepcopy(self.current_evidence)
        candidate_snapshot = deepcopy(candidates)
        history_snapshot = deepcopy(self.controller.repository.task_history(task_snapshot.task_id))
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
            "next_step": "请按当前测试建议继续。",
            "warning": "模型暂不可用，已改用本地说明。",
            "source": "local",
        }
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
