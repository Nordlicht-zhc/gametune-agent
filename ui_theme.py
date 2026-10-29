"""Warm, quiet desktop theme shared by the application and its dialogs."""

STYLESHEET = """
* { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #302d29; }
QMainWindow, QWidget#root, QStackedWidget, QScrollArea,
QScrollArea > QWidget > QWidget { background: #faf9f6; }
QScrollArea { border: none; }
QDialog { background: #faf9f6; }
QFrame#sidebar { background: #f0eee8; border: none; border-right: 1px solid #e2ded6; }
QLabel#brandName { font-family: "Noto Serif SC", "Georgia"; font-size: 25px; font-weight: 600; }
QLabel#brandSub, QLabel#sidebarLabel { color: #756f66; font-size: 12px; }
QPushButton#navButton { color: #514c45; background: transparent; border: none; border-radius: 8px; text-align: left; padding: 9px 14px; font-size: 14px; }
QPushButton#navButton:hover { background: #e8e4dc; }
QPushButton#navButton:checked { color: #703c2e; background: #e5dcd2; font-weight: 600; }
QPushButton#navButton:pressed { background: #dcd3c9; }
QPushButton#navButton:focus { border: 1px solid #a9513c; }
QPushButton#newTaskButton { text-align: left; padding: 9px 14px; border: 1px solid #b9705e; color: #984832; background: transparent; border-radius: 8px; font-size: 14px; }
QPushButton#newTaskButton:checked, QPushButton#newTaskButton:hover { background: #e9dfd5; }
QPushButton#newTaskButton:focus { border: 2px solid #a9513c; }
QFrame#navIndicator { background: #a9513c; border-radius: 1px; }
QFrame#sideTask { background: transparent; border: none; }
QLabel#sideTaskTitle { color: #514c45; font-size: 13px; }
QLabel#sideTaskText { color: #756f66; font-size: 11px; }
QLabel#eyebrow { color: #756f66; font-size: 12px; }
QLabel#pageTitle { font-family: "Noto Serif SC", "SimSun"; color: #302d29; font-size: 28px; font-weight: 600; }
QLabel#subtitle { color: #756f66; font-size: 13px; }
QLabel#sectionTitle { color: #302d29; font-size: 18px; font-weight: 600; }
QLabel#sectionMeta { color: #756f66; font-size: 11px; }
QLabel#statusPill { color: #756f66; background: #f0eee8; border: 1px solid #e2ded6; border-radius: 6px; padding: 5px 10px; font-size: 11px; }
QFrame#card, QFrame#formCard, QFrame#measurementCard { background: #ffffff; border: 1px solid #e2ded6; border-radius: 11px; }
QFrame#metricCard { background: transparent; border: none; }
QLabel#cardKicker { color: #756f66; font-size: 12px; }
QLabel#metricValue { color: #302d29; font-family: "Bahnschrift", "Microsoft YaHei UI"; font-size: 25px; font-weight: 500; }
QLabel#metricNote { color: #756f66; font-size: 11px; }
QProgressBar { background: #e8e3db; border: none; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #a9513c; border-radius: 3px; }
QFrame#heroCard { background: #f0eee8; border: none; border-radius: 10px; }
QLabel#heroTitle { color: #302d29; font-size: 20px; font-weight: 600; }
QLabel#heroBody { color: #625b52; font-size: 13px; }
QLabel#confidence { color: #756f66; border: none; font-size: 12px; padding: 5px 0; }
QLabel#tagGreen, QLabel#tagOrange, QLabel#tagNeutral { border-radius: 5px; padding: 4px 8px; font-size: 11px; }
QLabel#tagGreen { color: #376552; background: #e9f0e9; }
QLabel#tagOrange { color: #8a592b; background: #f6ecdc; }
QLabel#tagNeutral { color: #756f66; background: #f0eee8; }
QLabel#warningBox { color: #82502c; background: #f6ecdc; border: 1px solid #e8d5b8; border-radius: 7px; padding: 6px 10px; min-height: 28px; }
QLabel#actionNumber { color: #ffffff; background: #a9513c; border-radius: 7px; padding: 5px 7px; }
QLabel#actionTitle { font-size: 16px; font-weight: 600; }
QLabel#settingOld, QLabel#settingNew { border-radius: 6px; padding: 7px 10px; font-size: 13px; }
QLabel#settingOld { color: #756f66; background: #f0eee8; }
QLabel#settingNew { color: #984832; background: #f3e9e1; font-weight: 600; }
QLabel#ruleName { color: #514c45; font-size: 12px; font-weight: 600; }
QLabel#ruleText { color: #625b52; font-size: 12px; }
QFrame#ruleLine { background: #e5e1d9; max-height: 1px; }
QFrame#actionCard { background: transparent; border: none; }
QFrame#guidePanel { background: #f0eee8; border: none; border-radius: 8px; }
QLabel#guideTitle { color: #514c45; font-size: 13px; font-weight: 600; }
QLabel#guideText { color: #756f66; font-size: 12px; }
QFrame#welcomePanel { background: #ffffff; border: 1px dashed #c7bdb0; border-radius: 12px; }
QPushButton { min-height: 28px; padding: 6px 12px; border: 1px solid #d8d1c7; border-radius: 8px; background: #ffffff; }
QPushButton:hover { background: #f0eee8; }
QPushButton:pressed { background: #e5dfd6; }
QPushButton:disabled { color: #8d857a; background: #eeebe5; border-color: #ded8ce; }
QPushButton:focus { border: 2px solid #a9513c; }
QPushButton#primaryButton { color: #ffffff; background: #a9513c; border: 1px solid #a9513c; border-radius: 8px; padding: 9px 16px; font-weight: 600; }
QPushButton#primaryButton:hover { background: #93432f; border-color: #93432f; }
QPushButton#primaryButton:pressed { background: #7d3828; }
QPushButton#primaryButton:focus { border: 2px solid #532c22; }
QPushButton#primaryButton:disabled { background: #e2d6cd; color: #807267; border-color: #e2d6cd; }
QPushButton#secondaryButton { color: #514c45; background: #faf9f6; border: 1px solid #d8d1c7; border-radius: 8px; padding: 8px 14px; }
QPushButton#secondaryButton:hover { background: #eee9e0; }
QPushButton#secondaryButton:focus { border: 2px solid #a9513c; }
QPushButton#secondaryButton:disabled { color: #8d857a; background: #f0eee8; }
QPushButton#evidenceButton { text-align: left; background: transparent; padding: 12px; color: #625b52; border: 1px solid #e2ded6; }
QPushButton#evidenceButton:checked { background: #f0eee8; }
QFrame#toastNotice { background: #ffffff; border: 1px solid #d8d1c7; border-radius: 10px; }
QFrame#toastNotice[variant="success"] { border-left: 3px solid #577b64; }
QFrame#toastNotice[variant="info"] { border-left: 3px solid #607e99; }
QFrame#toastNotice[variant="error"] { border-left: 3px solid #a9513c; }
QLabel#toastText { color: #302d29; font-size: 12px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    background-color: #ffffff; color: #302d29; border: 1px solid #d8d1c7;
    border-radius: 7px; padding: 7px 9px;
    selection-background-color: #e5dcd2; selection-color: #302d29;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus { border: 1px solid #a9513c; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QTextEdit:disabled,
QLineEdit[readOnly="true"], QTextEdit[readOnly="true"] { background-color: #faf9f6; color: #756f66; border-color: #e2ded6; }
QComboBox { padding-right: 30px; }
QComboBox QLineEdit { border: none; border-radius: 0; padding: 0; background: transparent; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; background: transparent; }
QComboBox::down-arrow { image: none; width: 0; height: 0; }
QComboBox QAbstractItemView { border-radius: 10px; padding: 5px; background: #ffffff; color: #302d29; border: 1px solid #d8d1c7; outline: none; selection-background-color: #e5dcd2; selection-color: #302d29; }
QComboBox QAbstractItemView::item { min-height: 30px; padding: 4px 9px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QMessageBox { background-color: #ffffff; }
QMessageBox QLabel { background: transparent; color: #302d29; min-width: 300px; }
QMessageBox QPushButton { min-width: 88px; min-height: 30px; padding: 4px 14px; color: #302d29; background-color: #ffffff; border: 1px solid #d8d1c7; border-radius: 7px; }
QMessageBox QPushButton:hover { background-color: #f0eee8; }
QMessageBox QPushButton:default { color: #ffffff; background-color: #a9513c; border-color: #a9513c; }
QToolTip { color: #302d29; background-color: #ffffff; border: 1px solid #d8d1c7; padding: 5px; }
QTableWidget { background: #ffffff; alternate-background-color: #faf9f6; border: 1px solid #e2ded6; border-radius: 10px; gridline-color: #eeeae3; selection-background-color: #e5dcd2; selection-color: #302d29; }
QHeaderView::section { background: #f0eee8; color: #625b52; border: none; border-bottom: 1px solid #e2ded6; padding: 9px; font-size: 11px; font-weight: 600; }
QStatusBar { background: #faf9f6; color: #756f66; border-top: 1px solid #e5e1d9; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 3px; }
QScrollBar::handle:vertical { background: #cec6ba; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
