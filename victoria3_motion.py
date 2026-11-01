"""Interruptible, snapshot-only motion for Victoria 3 workspaces."""
import os
import sys
from PySide6.QtCore import QObject, QEvent, Qt, Property, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QWidget, QStackedWidget, QTabWidget, QFrame


def motion_enabled():
    app = QApplication.instance()
    if os.environ.get('GAMETUNE_REDUCED_MOTION', '').lower() in ('1', 'true', 'yes') or (app and app.property('reduceMotion')):
        return False
    if sys.platform == 'win32':
        import ctypes
        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    return True


class Snapshot(QWidget):
    def __init__(self, parent, pixmap):
        super().__init__(parent)
        self.pixmap = pixmap
        self._opacity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(parent.rect())

    def get_opacity(self):
        return self._opacity

    def set_opacity(self, value):
        self._opacity = value
        self.update()

    opacity = Property(float, get_opacity, set_opacity)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(0, 0, self.pixmap)


class PageTransition(QObject):
    def __init__(self, host, duration=170):
        super().__init__(host)
        self.host = host
        self.duration = duration
        self.overlay = None
        self.animation = None
        host.installEventFilter(self)

    def clear(self):
        if self.animation is not None:
            self.animation.stop()
            self.animation.deleteLater()
            self.animation = None
        if self.overlay is not None:
            self.overlay.hide()
            self.overlay.deleteLater()
            self.overlay = None

    def start(self, pixmap):
        self.clear()
        if pixmap is None or pixmap.isNull() or not self.host.isVisible() or not motion_enabled():
            return
        self.overlay = Snapshot(self.host, pixmap)
        self.overlay.show()
        self.overlay.raise_()
        self.animation = QPropertyAnimation(self.overlay, b'opacity', self)
        self.animation.setDuration(self.duration)
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self.clear)
        self.animation.start()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Hide):
            self.clear()
        return super().eventFilter(watched, event)


class MotionStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.transition = PageTransition(self)

    def setCurrentIndex(self, index):
        if index == self.currentIndex() or not 0 <= index < self.count():
            return
        self.transition.clear()
        old = self.currentWidget()
        image = old.grab() if old and self.isVisible() and motion_enabled() else None
        super().setCurrentIndex(index)
        self.transition.start(image)


class MotionTabs(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('v3Tabs')
        self.setDocumentMode(True)
        self.host = self.findChild(QStackedWidget)
        self.transition = PageTransition(self.host, 140)
        self.previous_page = None
        self.currentChanged.connect(self.changed)

    def changed(self, index):
        old = self.previous_page
        self.previous_page = self.currentWidget()
        self.transition.clear()
        if old is not None and old is not self.previous_page and self.isVisible() and motion_enabled():
            self.transition.start(old.grab())


class NavigationMarker(QObject):
    def __init__(self, group, parent):
        super().__init__(parent)
        self.group = group
        self.host = group.button(0).parentWidget()
        self.marker = QFrame(self.host)
        self.marker.setObjectName('navIndicator')
        self.marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.marker.hide()
        self.animation = QPropertyAnimation(self.marker, b'geometry', self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.host.installEventFilter(self)
        QTimer.singleShot(0, lambda: self.select(group.checkedId(), False))

    def select(self, index, animated=True):
        button = self.group.button(index)
        if button is None:
            return
        self.animation.stop()
        target = button.geometry()
        target.setX(0)
        target.setWidth(3)
        if animated and self.marker.isVisible() and self.host.isVisible() and motion_enabled():
            self.animation.setStartValue(self.marker.geometry())
            self.animation.setEndValue(target)
            self.animation.start()
        else:
            self.marker.setGeometry(target)
        self.marker.show()
        self.marker.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, lambda: self.select(self.group.checkedId(), False))
        elif event.type() == QEvent.Type.Hide:
            self.animation.stop()
        return super().eventFilter(watched, event)


def reveal(widget, visible):
    # Lazy import avoids app -> Victoria3Page -> app at module initialization.
    from app import animate_visibility
    animate_visibility(widget, visible, 180 if motion_enabled() else 0)
