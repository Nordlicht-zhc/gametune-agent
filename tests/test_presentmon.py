from pathlib import Path
import tempfile
import unittest
from threading import Event
from unittest.mock import patch
from tools.presentmon import PresentMon, summarize_captures, load_capture


class PresentMonTests(unittest.TestCase):
    def test_settings_compare_normalizes_resolution_and_ignores_added_defaults(self):
        from victoria3_capture import graphics_differences
        self.assertEqual(graphics_differences({'resolution': '2560 × 1600', 'new_default': True}, {'resolution': '2560x1600'}), [])
        self.assertIn('3200x2000', graphics_differences({'resolution': '2560x1600'}, {'resolution': '3200x2000'})[0])
    def test_command_is_bounded_and_targets_pid(self):
        tool = PresentMon(Path('tool'))
        command = tool.command(123, Path('path with spaces.csv'), 60)
        self.assertIn('--v1_metrics', command)
        self.assertEqual(command[command.index('--process_id') + 1], '123')
        self.assertIn('path with spaces.csv', command)
        self.assertNotIn('--restart_as_admin', command)
        with self.assertRaises(ValueError):
            tool.command(123, Path('x'), 0)

    def test_frames_are_not_independent_runs(self):
        metrics = summarize_captures([{'metrics': {'avg_fps': 60, 'low_fps': 40}}])
        self.assertEqual(metrics['run_count'], 1)
        metrics = summarize_captures([{'metrics': {'avg_fps': value, 'low_fps': 40}} for value in (59, 60, 61)])
        self.assertEqual(metrics['run_count'], 3)
        self.assertEqual(metrics['avg_fps'], 60)

    def test_missing_or_corrupt_tool_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = PresentMon(Path(folder))
            tool.executable.write_bytes(b'not an executable')
            with patch('tools.presentmon.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    tool.capture(Path(folder), 60, Event())
                launch.assert_not_called()

    def test_download_checksum_failure(self):
        import io
        with tempfile.TemporaryDirectory() as folder:
            tool = PresentMon(Path(folder))
            with patch('tools.presentmon.urllib.request.urlopen', return_value=io.BytesIO(b'bad download')):
                with self.assertRaises(ValueError):
                    tool.prepare(Event())
            self.assertFalse(tool.executable.exists())
            self.assertEqual(list(Path(folder).glob('*.download')), [])

    def test_real_csv_shape_first_zero_and_multiple_chains(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.csv'
            path.write_text('Application,ProcessID,SwapChainAddress,MsBetweenPresents\n'
                + 'victoria3.exe,123,main,0\n'
                + 'victoria3.exe,123,main,16\n' * 120
                + 'victoria3.exe,123,secondary,100\n' * 110
                + 'other.exe,999,x,1\n' * 200, encoding='utf-8')
            result = load_capture(path, 123)
            self.assertAlmostEqual(result['avg_fps'], 62.5)
            self.assertEqual(result['selected_swap_chain'], 'main')
            self.assertEqual(result['run_count'], 1)

    def test_capture_uses_native_csv_and_validates_duration(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            tool = PresentMon(Path(folder))
            def launch(command, **kwargs):
                output = Path(command[command.index('--output_file') + 1])
                output.write_text('Application,ProcessID,SwapChainAddress,MsBetweenPresents\n'
                    + 'victoria3.exe,123,main,16\n' * 650, encoding='utf-8')
                return SimpleNamespace(poll=lambda: 0, returncode=0)
            with patch.object(tool, 'ready', return_value=True), patch.object(tool, 'game_pid', return_value=123), patch('tools.presentmon.subprocess.Popen', side_effect=launch):
                result = tool.capture(Path(folder), 10, Event())
                self.assertEqual(result['metrics']['avg_fps'], 62.5)
                self.assertEqual(result['pid'], 123)

    def test_capture_report_pipeline_retains_baseline_on_apply(self):
        import json
        from copy import deepcopy
        from PySide6.QtWidgets import QApplication
        from tools.victoria3_local import LocalVictoria3
        from tools.victoria3_report import new_report
        from victoria3_quick import Victoria3Quick
        from victoria3_capture import CapturePage
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {'Graphics': {'resolution': '3200x2000'}}
            (root / 'pdx_settings.json').write_text(json.dumps(settings), encoding='utf-8')
            quick = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            report = new_report({}, settings)
            quick.reports.save(report)
            quick.current_report = report
            page = CapturePage(quick)
            result = {'metrics': {'avg_fps': 60, 'low_fps': 40}, 'seconds': 60, 'csv': 'fixture'}
            page.save_capture(report, 'baseline', settings, result)
            quick.preview()
            quick.apply()
            current = quick.current_report
            page.suggest_stage()
            self.assertEqual(page.side.currentData(), 'candidate')
            self.assertIn('优化后', page.status.text())
            self.assertEqual(len(current['captures']['baseline']), 1)
            after = quick.service.read_settings()
            page.save_capture(current, 'candidate', after, {**result, 'metrics': {'avg_fps': 70, 'low_fps': 45}})
            saved = quick.reports.list()[0]
            self.assertTrue(saved['comparison_confirmed'])
            self.assertEqual(saved['observations']['baseline']['run_count'], 1)
            quick.close()
