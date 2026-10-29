from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from agent.api import ModelResponse
from tools.victoria3_report import new_report, transfer_baseline, ReportRepository
from tools.victoria3_local import LocalVictoria3
from victoria3_quick import Victoria3Quick
from victoria3_app import Victoria3Window


class ReportRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_apply_keeps_baseline_despite_audio_and_ui_bookkeeping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before = {'Graphics': {'resolution': '3200x2000', 'quality': 'custom'},
                      'Audio': {'volume': .5}, 'Theme': {'seen': 'old'}}
            source = new_report({}, before)
            source['observations']['baseline'] = {'avg_fps': 60, 'low_fps': 40, 'run_count': 1}
            source['captures'] = {'baseline': [{'csv': 'original.csv', 'metrics': {'avg_fps': 60, 'low_fps': 40}}]}
            source['sensor_status'] = {'baseline': {'gpu_usage': {'value': 75, 'status': 'ok'}}}
            source['sources']['baseline_fps'] = 'PresentMon'
            current = deepcopy(before)
            current['Audio']['volume'] = .8
            current['Theme']['seen'] = 'new'
            (root / 'pdx_settings.json').write_text(json.dumps(current), encoding='utf-8')
            quick = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            quick.reports.save(source)
            quick.current_report = source
            quick.preview()
            quick.apply()
            report = quick.reports.list()[0]
            self.assertEqual(report['baseline_report_id'], source['id'])
            self.assertEqual(report['observations']['baseline'], source['observations']['baseline'])
            self.assertEqual(report['captures']['baseline'][0]['csv'], 'original.csv')
            self.assertEqual(report['sensor_status'], source['sensor_status'])
            self.assertEqual(ReportRepository(quick.reports.path).list()[0]['observations']['baseline']['avg_fps'], 60)
            self.assertEqual(quick.reports.list()[1]['id'], source['id'])
            quick.restore_latest()
            quick.preview()
            quick.apply()
            self.assertEqual(quick.reports.list()[0]['observations']['baseline']['avg_fps'], 60)
            quick.close()

    def test_incompatible_graphics_and_hardware_are_not_silently_joined(self):
        source = new_report({'gpu': 'A'}, {'Graphics': {'resolution': '3200x2000'}})
        source['observations']['baseline'] = {'avg_fps': 60}
        for machine, resolution in (({'gpu': 'B'}, '3200x2000'), ({'gpu': 'A'}, '1920x1200')):
            target = new_report(machine, {'Graphics': {'resolution': resolution}})
            transfer_baseline(source, target)
            self.assertEqual(target['observations']['baseline'], {})
            self.assertIn('baseline_notice', target)

    def test_model_completion_runs_on_gui_thread_and_survives_repeat(self):
        class Provider:
            name = 'fake'
            calls = 0
            def complete(self, request):
                if QThread.currentThread() == QApplication.instance().thread():
                    raise AssertionError('network work must not run on GUI thread')
                self.calls += 1
                return ModelResponse('{"summary":"模型说明","steps":["先复测"]}', 'fake', 'fake')
        class Window(Victoria3Window):
            received_threads = []
            def receive_explanation(self, result, snapshot, provider):
                self.received_threads.append(QThread.currentThread())
                super().receive_explanation(result, snapshot, provider)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pdx_settings.json').write_text(json.dumps({'Graphics': {'resolution': '3200x2000'}}), encoding='utf-8')
            provider = Provider()
            window = Window(root / 'ui.db', provider)
            window.quick.service = LocalVictoria3(root, running_check=lambda: False)
            window.quick.show_report()
            for _ in range(3):
                window.consent.setChecked(True)
                window.explain()
                deadline = time.monotonic() + 3
                while window.job is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.001)
                self.assertIsNone(window.job)
                self.assertIn('模型说明', window.model_text.toPlainText())
                self.assertTrue(window.model_button.isEnabled())
            self.assertEqual(provider.calls, 3)
            self.assertTrue(all(thread == self.app.thread() for thread in window.received_threads))
            saved = window.quick.reports.list()[0]
            self.assertEqual(saved['model_explanation']['source'], 'model')
            self.assertEqual(saved['observations'], {'baseline': {}, 'candidate': {}})
            class BrokenProvider:
                name = 'broken'
                def complete(self, request):
                    raise RuntimeError('do-not-show-this-private-error')
            window.set_provider(BrokenProvider())
            window.consent.setChecked(True)
            window.explain()
            deadline = time.monotonic() + 3
            while window.job is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.001)
            self.assertIsNone(window.job)
            self.assertIn('本地建议', window.model_text.toPlainText())
            self.assertNotIn('private-error', window.model_text.toPlainText())
            self.assertEqual(window.quick.reports.list()[0]['model_explanation']['source'], 'local')
            window.close()

    def test_history_selection_updates_capture_report_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            quick = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            a, b = (new_report({}, {'Graphics': {}}) for _ in range(2))
            quick.reports.save(a)
            quick.reports.save(b)
            quick.report_view.load(b)
            index = next(i for i in range(quick.report_view.history.count()) if quick.report_view.history.itemData(i)['id'] == a['id'])
            quick.report_view.history.setCurrentIndex(index)
            self.assertEqual(quick.current_report['id'], a['id'])
            quick.close()

    def test_existing_split_records_recovered_without_deleting_original(self):
        from PySide6.QtWidgets import QMessageBox
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            quick = Victoria3Quick(LocalVictoria3(root, running_check=lambda: False))
            source = new_report({}, {'Graphics': {'resolution': '3200x2000'}})
            source['observations']['baseline'] = {'avg_fps': 100, 'low_fps': 60, 'run_count': 1}
            target = new_report({}, source['before'], {'Graphics': {'resolution': '2560x1600'}}, phase='restored')
            target['observations']['candidate'] = {'avg_fps': 120, 'low_fps': 70, 'run_count': 2}
            quick.reports.save(source)
            quick.reports.save(target)
            quick.report_view.load(target)
            with patch('victoria3_report_view.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                quick.report_view.restore_baseline()
            records = {item['id']: item for item in quick.reports.list()}
            self.assertEqual(len(records), 2)
            self.assertEqual(records[source['id']]['observations']['baseline']['avg_fps'], 100)
            recovered = records[target['id']]
            self.assertEqual(recovered['observations']['baseline']['avg_fps'], 100)
            self.assertEqual(recovered['observations']['candidate']['avg_fps'], 120)
            self.assertEqual(recovered['baseline_report_id'], source['id'])
            quick.close()

    def test_stale_model_response_does_not_overwrite_saved_measurements(self):
        from threading import Event
        gate = Event()
        class Provider:
            name = 'delayed'
            def complete(self, request):
                gate.wait(2)
                return ModelResponse('{"summary":"旧结果","steps":["建议"]}', 'fake', 'fake')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pdx_settings.json').write_text(json.dumps({'Graphics': {'resolution': '3200x2000'}}), encoding='utf-8')
            window = Victoria3Window(root / 'ui.db', Provider())
            window.quick.service = LocalVictoria3(root, running_check=lambda: False)
            window.quick.show_report()
            window.consent.setChecked(True)
            window.explain()
            record = window.quick.reports.list()[0]
            record['observations']['baseline'] = {'avg_fps': 80}
            window.quick.reports.save(record)
            gate.set()
            deadline = time.monotonic() + 3
            while window.job is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.001)
            self.assertIsNone(window.job)
            self.assertEqual(window.quick.reports.list()[0]['observations']['baseline']['avg_fps'], 80)
            self.assertIn('未覆盖', window.model_text.toPlainText())
            window.close()
