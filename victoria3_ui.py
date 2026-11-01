"""Small presentation helpers shared by Victoria 3 desktop pages."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QPushButton


def text(value, role='subtitle'):
    widget = QLabel(value)
    widget.setObjectName(role)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def section(title, description=''):
    frame = QFrame()
    frame.setObjectName('formCard')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(12)
    layout.addWidget(text(title, 'sectionTitle'))
    if description:
        layout.addWidget(text(description))
    return frame, layout


def button(value, callback, primary=False):
    widget = QPushButton(value)
    widget.setObjectName('primaryButton' if primary else 'secondaryButton')
    widget.clicked.connect(callback)
    return widget
