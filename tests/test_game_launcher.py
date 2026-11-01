import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from game_launcher import GameLauncher


class GameLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_choose_games_return_and_keep_independent_workspaces(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.provider_from_env', return_value=None), patch('app.load_saved_api_config', return_value=None):
            launcher = GameLauncher(Path(folder))
            launcher.show()
            try:
                self.assertEqual(launcher.windows, {})
                launcher.buttons['cs2'].click()
                cs = launcher.windows['cs2']
                self.assertFalse(launcher.isVisible())
                self.assertTrue(cs.isVisible())
                self.assertFalse(cs.sidebar.group.button(6).isVisible())
                cs.state.name = 'retained task'
                launcher.return_to_selection('cs2')
                launcher.buttons['victoria3'].click()
                v3 = launcher.windows['victoria3']
                self.app.processEvents()
                self.assertTrue(v3.isVisible())
                self.assertEqual(v3.configurations.path, Path(folder) / 'victoria3.db')
                launcher.return_to_selection('victoria3')
                launcher.buttons['cs2'].click()
                self.assertIs(launcher.windows['cs2'], cs)
                self.assertEqual(cs.state.name, 'retained task')
                cs.close()
                self.assertTrue(launcher.isVisible())
                self.assertFalse(cs.isVisible())
            finally:
                launcher.close()

    def test_capture_blocks_return_and_exit_until_finished(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.provider_from_env', return_value=None), patch('app.load_saved_api_config', return_value=None):
            launcher = GameLauncher(Path(folder))
            launcher.open_game('victoria3')
            window = launcher.windows['victoria3']
            try:
                from threading import Event
                from types import SimpleNamespace
                window.capture_page.job = SimpleNamespace(cancel=Event())
                launcher.return_to_selection('victoria3')
                self.assertFalse(launcher.isVisible())
                self.assertTrue(window.isVisible())
                self.assertFalse(launcher.close())
                self.assertTrue(window.capture_page.job.cancel.is_set())
            finally:
                window.capture_page.job = None
                launcher.close()
