from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from PySide6.QtWidgets import QApplication
from agent.victoria3_model import report_model_payload
from agent.api import ModelResponse
from tools.configurations import (ConfigurationRepository,comparison_rows,comparison_snapshot,
                                  configuration_html,comparison_html)
from tools.victoria3_report import new_report,ReportRepository
from tools.victoria3_local import LocalVictoria3
from victoria3_workspace import ConfigurationWindow


class ConfigurationTests(unittest.TestCase):
    def fixture(self,path):
        repo=ConfigurationRepository(path)
        a=repo.current({}, {'Graphics':{'resolution':'3200x2000'}}, name='配置 A')
        b=repo.current({}, {'Graphics':{'resolution':'2560x1600'}}, name='配置 B')
        return repo,a,b

    def capture(self,fps):
        return {'metrics':{'avg_fps':fps,'low_fps':fps*.6},'seconds':60,'context':'同一场景','csv':'fixture'}

    def test_migration_splits_records_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=ConfigurationRepository(Path(folder)/'db')
            old=new_report({}, {'Graphics':{'resolution':'3200x2000'}},{'Graphics':{'resolution':'2560x1600'}},phase='applied')
            old['observations']={'baseline':{'avg_fps':60},'candidate':{'avg_fps':90}}
            untouched=deepcopy(old)
            repo.migrate([old]);repo.migrate([old])
            records=repo.list()
            self.assertEqual(len(records),2)
            self.assertEqual(sorted(row['observations']['avg_fps'] for row in records),[60,90])
            self.assertEqual(old,untouched)
            self.assertTrue(all('before' not in row and 'after' not in row for row in records))

    def test_captures_only_update_the_selected_actual_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            repo,a,b=self.fixture(Path(folder)/'db')
            repo.append_capture(a['id'],a['fingerprint'],self.capture(60))
            repo.append_capture(b['id'],b['fingerprint'],self.capture(90))
            self.assertEqual(repo.get(a['id'])['observations']['avg_fps'],60)
            self.assertEqual(repo.get(b['id'])['observations']['avg_fps'],90)
            with self.assertRaises(ValueError):repo.append_capture(a['id'],b['fingerprint'],self.capture(100))
            bad=self.capture(100);bad['context']='另一个场景'
            with self.assertRaises(ValueError):repo.append_capture(a['id'],a['fingerprint'],bad)
            self.assertEqual(repo.get(a['id'])['observations']['run_count'],1)

    def test_compare_arbitrary_records_and_swapping_does_not_move_data(self):
        with tempfile.TemporaryDirectory() as folder:
            repo,a,b=self.fixture(Path(folder)/'db')
            a=repo.append_capture(a['id'],a['fingerprint'],self.capture(60))
            b=repo.append_capture(b['id'],b['fingerprint'],self.capture(90))
            original=repo.list()
            self.assertEqual(comparison_rows(a,b)[0][3],'+50.00%')
            self.assertEqual(comparison_rows(b,a)[0][3],'-33.33%')
            self.assertEqual(repo.list(),original)
            self.assertNotIn('优化前',configuration_html(a))
            self.assertIn('配置 A',comparison_html(a,b))
            self.assertNotEqual(comparison_snapshot(a,b)['id'],comparison_snapshot(b,a)['id'])

    def test_stale_model_explanation_does_not_replace_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            repo,a,b=self.fixture(Path(folder)/'db')
            repo.append_capture(a['id'],a['fingerprint'],self.capture(60))
            self.assertFalse(repo.save_explanation(a,{'source':'model','summary':'旧','steps':[]}))
            payload=report_model_payload(comparison_snapshot(repo.get(a['id']),b))
            self.assertIn('configuration_a',payload)
            self.assertNotIn('settings_before',payload)


class ConfigurationWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_capture_apply_compare_restore_and_model_are_independent(self):
        class Provider:
            name='fake'
            def complete(self,request):
                return ModelResponse('{"summary":"配置解读","steps":["继续复测"]}','fake','fake')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'pdx_settings.json').write_text(json.dumps({'Graphics':{'resolution':'3200x2000'}}),encoding='utf-8')
            window=ConfigurationWindow(root/'test.db',Provider())
            window.quick.service=LocalVictoria3(root,running_check=lambda:False)
            window.quick.scan()
            a=window.capture_page.current()
            result={'metrics':{'avg_fps':60,'low_fps':40},'seconds':60,'csv':'fixture'}
            window.capture_page.save_configuration_capture(a,'场景',deepcopy(result))
            window.quick.preview();window.quick.apply()
            b=window.capture_page.current()
            self.assertNotEqual(a['id'],b['id'])
            window.capture_page.save_configuration_capture(b,'场景',{**result,'metrics':{'avg_fps':90,'low_fps':50}})
            self.assertEqual(window.configurations.get(a['id'])['observations']['avg_fps'],60)
            self.assertEqual(window.configurations.get(b['id'])['observations']['avg_fps'],90)
            self.assertEqual(ReportRepository(root/'test.db').list(),[])
            window.comparison.refresh()
            window.comparison.a.setCurrentIndex(window.comparison.a.findData(a['id']))
            window.comparison.b.setCurrentIndex(window.comparison.b.findData(b['id']))
            window.config_tabs.setCurrentIndex(1)
            before=deepcopy(window.configurations.list())
            window.consent.setChecked(True)
            window.explain()
            limit=time.monotonic()+3
            while window.job and time.monotonic()<limit:self.app.processEvents();time.sleep(.001)
            self.assertIsNone(window.job)
            self.assertIn('配置解读',window.model_text.toPlainText())
            self.assertEqual(window.configurations.list(),before)
            self.assertTrue(window.configurations.comparison_explanation(window.model_snapshot()['id']))
            window.quick.restore_latest()
            self.assertEqual(window.configurations.get(b['id'])['observations']['avg_fps'],90)
            self.assertEqual(window.configurations.get(a['id'])['observations']['avg_fps'],60)
            window.close()
