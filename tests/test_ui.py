import unittest

from app import STYLESHEET


class ThemeRegressionTests(unittest.TestCase):
    def test_editable_text_controls_use_white_background(self):
        self.assertIn("QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit", STYLESHEET)
        self.assertIn("background-color: #ffffff;", STYLESHEET)

    def test_combo_popup_has_high_contrast_text_and_selection(self):
        self.assertIn("QComboBox QAbstractItemView", STYLESHEET)
        self.assertIn("color: #202422", STYLESHEET)
        self.assertIn("selection-color: #172018", STYLESHEET)

    def test_message_boxes_override_system_dark_content_area(self):
        self.assertIn("QMessageBox { background-color: #ffffff; }", STYLESHEET)
        self.assertIn("QMessageBox QLabel { background: transparent; color: #202422;", STYLESHEET)
        self.assertIn("QMessageBox QPushButton:default", STYLESHEET)


if __name__ == "__main__":
    unittest.main()
