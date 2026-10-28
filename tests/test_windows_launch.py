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
