import unittest
from unittest.mock import patch, MagicMock
from tools.windows_launch import elevation_arguments, restart_as_admin


class WindowsLaunchTests(unittest.TestCase):
    def test_bootstrap_preserves_runtime_without_credentials(self):
        arguments = elevation_arguments()
        self.assertIn('victoria3_app', arguments)
        self.assertIn('--page', arguments)
        self.assertIn('sys.path', arguments)
        self.assertNotIn('API_KEY', arguments)

    def test_user_cancels_uac(self):
        shell = MagicMock()
        shell.ShellExecuteW.return_value = 5
        with patch('tools.windows_launch.os.name', 'nt'), patch('tools.windows_launch.ctypes.windll.shell32', shell):
            with self.assertRaises(ValueError):
                restart_as_admin()
            self.assertEqual(shell.ShellExecuteW.call_args.args[1], 'runas')

    def test_handoff_arguments_are_passed(self):
        arguments = elevation_arguments(12345, 'abc123')
        self.assertIn('--handoff-port', arguments)
        self.assertIn('12345', arguments)
        self.assertIn('abc123', arguments)

    def test_only_matching_ready_token_triggers_handoff(self):
        import time
        from PySide6.QtWidgets import QApplication
        from admin_handoff import AdminHandoff, signal_ready
        app = QApplication.instance() or QApplication([])
        server = AdminHandoff()
        calls = []
        server.ready.connect(lambda: calls.append(True))
        def pump():
            limit = time.monotonic() + .2
            while time.monotonic() < limit:
                app.processEvents()
        invalid = signal_ready(server, server.port, 'invalid')
        pump()
        self.assertEqual(calls, [])
        self.assertTrue(server.server.isListening())
        valid = signal_ready(server, server.port, server.token)
        pump()
        self.assertEqual(calls, [True])
        self.assertFalse(server.server.isListening())
        server.deleteLater()

    def test_timeout_keeps_original_window(self):
        from PySide6.QtWidgets import QApplication
        from admin_handoff import AdminHandoff
        app = QApplication.instance() or QApplication([])
        server = AdminHandoff()
        calls = []
        server.ready.connect(lambda: calls.append('ready'))
        server.expired.connect(lambda: calls.append('expired'))
        server.timeout()
        self.assertEqual(calls, ['expired'])
        self.assertFalse(server.server.isListening())
        server.deleteLater()
