from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from app import ExperimentPage, TaskState
from tools import cs2_benchmark as bench
from tools.cs2_log import CS2LogTail


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.installation = bench.Installation(self.root / "steam.exe", self.root / "console.log")
        self.page = ExperimentPage(TaskState(has_task=True))
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(self.page._log_timer.stop)

    def append(self, text):
        with self.installation.console_log.open("ab") as stream:
            stream.write(text.encode("ascii"))

    def test_startup_arguments_are_fixed_and_no_shell_is_used(self):
        with patch("tools.cs2_benchmark.subprocess.Popen") as popen:
            bench.launch_benchmark(self.installation)
        args = popen.call_args.args[0]
        self.assertEqual(args, [str(self.installation.steam), "-applaunch", "730", "-condebug", "-console",
                                "+map_workshop", "3240880604", "de_dust2"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_click_arms_listener_before_launch_and_reads_new_file(self):
        def launch(_):
            self.assertIsNotNone(self.page._log_tail)
            self.append('Map: "de_dust2"\n[VProf] FPS: Avg=542.37, P1=171.25\n')
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=None), \
             patch("app.cs2_benchmark.launch_benchmark", side_effect=launch) as run:
            self.page.log_benchmark.click()
            self.page._poll_cs2_log()
            self.assertEqual(self.page.avg.value(), 542)
            self.assertEqual(run.call_count, 1)
        self.assertIsNone(self.page._benchmark_macro)

    def test_running_game_waits_for_echo_before_sending_map(self):
        self.append("old log\n")
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console") as open_console, \
             patch("app.cs2_benchmark.console_command") as command:
            self.page.log_benchmark.click()
            token = self.page._benchmark_macro["token"]
            self.page._send_cs2_macro_echo(token)
            self.assertEqual(command.call_args.args, (123, "echo " + token))
            self.append('> echo ' + token + '\n[VProf] FPS: Avg=200, P1=100\n')
            self.page._poll_cs2_log()
            self.assertEqual(command.call_count, 1)
            self.assertEqual(self.page.avg.value(), 0)
            self.append("[Console] " + token + "\n")
            self.page._poll_cs2_log()
            self.assertEqual(command.call_args.args, (123, bench.MAP_COMMAND))
            self.append('Map: "de_dust2"\n[VProf] FPS: Avg=542.37, P1=171.25\n')
            self.page._poll_cs2_log()
            self.assertEqual(self.page.avg.value(), 542)
            self.assertEqual(open_console.call_count, 1)

    def test_cancel_prevents_delayed_keystrokes(self):
        self.append("old log\n")
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console"), \
             patch("app.cs2_benchmark.console_command") as command:
            self.page.log_benchmark.click()
            token = self.page._benchmark_macro["token"]
            self.page.log_toggle.click()
            self.page._send_cs2_macro_echo(token)
            command.assert_not_called()

    def test_focus_failure_does_not_send_map(self):
        self.append("old log\n")
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console", side_effect=ValueError("CS2 未处于前台")), \
             patch("app.cs2_benchmark.console_command") as command:
            self.page.log_benchmark.click()
            self.assertIsNone(self.page._benchmark_macro)
            command.assert_not_called()
            self.assertIn("前台", self.page.log_status.text())

    def test_missing_log_in_running_game_requires_logged_launch(self):
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console") as open_console:
            self.page.log_benchmark.click()
            open_console.assert_not_called()
            self.assertIn("退出", self.page.log_status.text())

    def test_arbitrary_commands_are_rejected_before_input(self):
        with patch("tools.cs2_benchmark._send") as send:
            with self.assertRaises(ValueError):
                bench.console_command(123, "quit")
            send.assert_not_called()
            bench.console_command(123, bench.MAP_COMMAND)
            self.assertEqual(send.call_count, 1)

    def test_no_echo_timeout_never_sends_map(self):
        self.append("old log\n")
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console"), \
             patch("app.cs2_benchmark.console_command") as command:
            self.page.log_benchmark.click()
            self.page._benchmark_macro["deadline"] = 0
            self.page._poll_cs2_log()
            command.assert_not_called()
            self.assertIsNone(self.page._benchmark_macro)
            self.assertIn("未收到", self.page.log_status.text())

    def test_closing_page_cancels_pending_console_step(self):
        self.append("old log\n")
        with patch("app.cs2_benchmark.find_installation", return_value=self.installation), \
             patch("app.cs2_benchmark.game_window", return_value=123), \
             patch("app.cs2_benchmark.open_console"), \
             patch("app.cs2_benchmark.console_command") as command:
            self.page.log_benchmark.click()
            token = self.page._benchmark_macro["token"]
            self.page.close()
            self.page._send_cs2_macro_echo(token)
            command.assert_not_called()
            self.assertFalse(self.page._log_timer.isActive())
