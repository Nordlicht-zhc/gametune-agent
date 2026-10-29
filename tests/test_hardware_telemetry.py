import unittest
from tools.hardware_telemetry import (CpuUsage, gpu_readings, reading, summarize,
                                      attach_telemetry, cpu_temperature_from_rows)
from tools.victoria3_report import new_report, metric_rows, report_html
from tools.game_date_ocr import parse_game_date, summarize_dates
from pathlib import Path
import tempfile
from unittest.mock import patch


class TelemetryTests(unittest.TestCase):
    def test_capture_wrapper_starts_and_persists_telemetry(self):
        from PySide6.QtWidgets import QApplication
        from victoria3_quick import Victoria3Quick
        from victoria3_capture import CapturePage
        from tools.victoria3_local import LocalVictoria3
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            quick = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            page = CapturePage(quick)
            page.blockSignals(True)
            summary = summarize([{'at_seconds': 1, 'readings': {'gpu_usage': reading(50, 'ok', 'GPU')}}])
            def fake_capture(*args, **kwargs):
                kwargs['on_started']()
                return {'csv': str(root / 'test.csv'), 'metrics': {'avg_fps': 60, 'low_fps': 40}}
            with patch('victoria3_capture.TelemetrySession') as session, patch.object(page.tool, 'capture', side_effect=fake_capture):
                session.return_value.stop.return_value = summary
                session.return_value.samples = []
                report = new_report({}, {'Graphics': {}})
                result = page.capture_with_sensors(report, 60, None)
                session.return_value.start.assert_called_once()
                session.return_value.stop.assert_called_once()
                self.assertEqual(result['telemetry']['gpu_usage']['value'], 50)
                self.assertTrue((root / 'test.sensors.json').is_file())
            page.close()
            quick.close()

    def test_gpu_values_units_and_na(self):
        rows = gpu_readings('NVIDIA Test, 62, 86, 78.77, GPU-test\n')
        self.assertEqual(rows['gpu_usage']['value'], 62)
        self.assertEqual(rows['gpu_temperature']['value'], 86)
        self.assertEqual(rows['gpu_power']['value'], 78.77)
        self.assertEqual(gpu_readings('GPU, 0, 40, [N/A], uuid')['gpu_power']['status'], 'unsupported')
        self.assertEqual(gpu_readings('GPU, 0, 40, 0, uuid')['gpu_power']['value'], 0)

    def test_gpu_selection_is_not_average_of_multiple_devices(self):
        output = 'GPU A, 10, 50, 10, uuid-a\nGPU B, 90, 80, 100, uuid-b\n'
        self.assertIsNone(gpu_readings(output)['gpu_usage']['value'])
        self.assertEqual(gpu_readings(output, 'GPU B')['gpu_usage']['value'], 90)

    def test_nonfinite_values_rejected(self):
        for raw in ('NaN', 'inf', '-1', '200'):
            self.assertEqual(gpu_readings(f'GPU, {raw}, 40, 20, uuid')['gpu_usage']['status'], 'unsupported')

    def test_cpu_kernel_includes_idle(self):
        self.assertEqual(CpuUsage.percentage((10, 20, 30), (20, 40, 50)), 75)
        self.assertIsNone(CpuUsage.percentage((10, 20, 30), (10, 20, 30)))
        self.assertIsNone(CpuUsage.percentage((10, 20, 30), (0, 1, 1)))

    def test_cpu_temperature_is_not_acpi_or_mainboard(self):
        rows = [{'Name': 'Motherboard', 'SensorType': 'Temperature', 'Value': 60, 'Identifier': '/lpc/temperature/0'}]
        self.assertEqual(cpu_temperature_from_rows(rows, 'test')['status'], 'unsupported')
        rows += [{'Name': 'CPU Package', 'SensorType': 'Temperature', 'Value': 83, 'Identifier': '/intelcpu/0/temperature/0'}]
        self.assertEqual(cpu_temperature_from_rows(rows, 'test')['value'], 83)

    def test_sample_aggregation_does_not_invent_zeros(self):
        samples = [{'at_seconds': n, 'readings': {'gpu_usage': reading(value, 'ok', 'GPU'),
                                                'cpu_temperature': reading(reason='无接口')}} for n, value in enumerate((10, 30))]
        result = summarize(samples)
        self.assertEqual(result['gpu_usage']['value'], 20)
        self.assertEqual(result['gpu_usage']['peak'], 30)
        self.assertIsNone(result['cpu_temperature']['value'])
        self.assertEqual(result['cpu_temperature']['status'], 'unsupported')

    def test_reports_include_sources_and_sensor_status(self):
        report = new_report({}, {'Graphics': {}})
        capture = {'telemetry': summarize([{'at_seconds': 1, 'readings': {
            'gpu_usage': reading(0, 'ok', 'gpu'), 'cpu_temperature': reading(reason='无接口')}}])}
        attach_telemetry(report, 'baseline', [capture])
        self.assertEqual(report['observations']['baseline']['gpu_usage'], 0)
        self.assertIn('接口不支持', metric_rows(report)[6][1])
        self.assertIn('传感器来源', report_html(report))
        self.assertNotIn('cpu_temperature', report['observations']['baseline'])

    def test_temperature_delta_is_degrees_and_zero_safe(self):
        report = new_report({}, {'Graphics': {}})
        report['observations']['baseline'] = {'cpu_temperature': 80, 'gpu_usage': 0}
        report['observations']['candidate'] = {'cpu_temperature': 75, 'gpu_usage': 20}
        report['comparison_confirmed'] = True
        self.assertEqual(metric_rows(report)[6][3], '-5.00 ℃')
        self.assertEqual(metric_rows(report)[3][3], '+20.00 个百分点')


class GameDateTests(unittest.TestCase):
    def test_chinese_and_numeric_dates(self):
        for text in ('1900年 1月 15日', '1900.1.15', '1900-01-15'):
            self.assertEqual(parse_game_date(text).isoformat(), '1900-01-15')
        for text in ('1900.2.30', '没有日期', '1900.1.1 1900.1.2'):
            with self.assertRaises(ValueError):
                parse_game_date(text)

    def test_simulation_speed_uses_dates_and_elapsed_time(self):
        result = summarize_dates([{'date': '1900-01-01', 'at_seconds': 0},
                                  {'date': '1900-01-31', 'at_seconds': 60}])
        self.assertEqual(result['value'], .5)
        self.assertEqual(result['game_days'], 30)
        self.assertEqual(summarize_dates([])['status'], 'not_sampled')
        self.assertEqual(summarize_dates([{'date': '1900-01-31', 'at_seconds': 0},
                                          {'date': '1900-01-01', 'at_seconds': 60}])['status'], 'error')
        self.assertEqual(summarize_dates([{'date': '1900-01-01', 'at_seconds': 0},
                                          {'date': '1900-01-01', 'at_seconds': 60}])['status'], 'not_sampled')
