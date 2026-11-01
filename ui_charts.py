"""Discrete measurements only: never infer a frame-time trace from summaries."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class RunChart(QWidget):
    def __init__(self, runs: list[dict[str, float]], parent: QWidget | None = None):
        super().__init__(parent)
        self.runs = [dict(run) for run in runs]
        self.setMinimumHeight(155)
        self.setMaximumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("逐次测试帧率图")
        self.setAccessibleDescription("；".join(
            f"第 {index} 次：平均 {run['avg_fps']:g} FPS，1% Low {run['low_fps']:g} FPS"
            for index, run in enumerate(self.runs, 1)
        ) or "当前记录没有逐次测量数据")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = QRectF(48, 16, max(1, self.width() - 66), max(1, self.height() - 54))
        if not self.runs:
            painter.setPen(QColor("#756f66"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "当前记录仅有汇总值\n没有逐次数据可供绘图")
            return
        highest = max(run['avg_fps'] for run in self.runs)
        ceiling = max(50, ((int(highest * 1.15) + 49) // 50) * 50)
        for tick in range(5):
            y = plot.bottom() - plot.height() * tick / 4
            painter.setPen(QPen(QColor("#e9e5df"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#756f66"))
            painter.drawText(QRectF(0, y - 9, 39, 18), Qt.AlignmentFlag.AlignRight,
                             f"{ceiling * tick / 4:g}")
        slot = plot.width() / len(self.runs)
        bar_width = min(24, slot * .23)
        for index, run in enumerate(self.runs):
            center = plot.left() + slot * (index + .5)
            for key, color, offset in (("avg_fps", "#607e99", -bar_width - 2),
                                       ("low_fps", "#a9513c", 2)):
                height = plot.height() * run[key] / ceiling
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(QRectF(center + offset, plot.bottom() - height,
                                               bar_width, height), 3, 3)
            painter.setPen(QColor("#756f66"))
            painter.drawText(QRectF(center - slot / 2, plot.bottom() + 9, slot, 24),
                             Qt.AlignmentFlag.AlignCenter, f"第 {index + 1} 次")
