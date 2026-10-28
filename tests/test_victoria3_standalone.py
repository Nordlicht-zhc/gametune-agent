import json
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from agent.api import ModelResponse
from agent.victoria3_model import explain_victoria3, report_model_payload
from tools.victoria3_report import new_report
from tools.victoria3_local import LocalVictoria3
from victoria3_app import Victoria3Window


class StandaloneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_workspace_navigation_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pdx_settings.json').write_text(json.dumps({'Graphics': {'resolution': '3200x2000'}}), encoding='utf-8')
            window = Victoria3Window(root / 'test.db')
            window.quick.service = LocalVictoria3(root, running_check=lambda: False)
            window.quick.scan()
            self.assertNotIn('GameTune', window.windowTitle())
            for index in range(6):
                window.navigate(index)
                self.assertEqual(window.stack.currentIndex(), index)
            window.quick.apply()
            self.assertEqual(window.stack.currentIndex(), 4)
            self.assertFalse(window.quick.report_view.isHidden())
            window.navigate(3)
            self.assertGreater(window.history.rowCount(), 0)
            window.close()

    def test_model_protocol_fallback_and_payload(self):
        report = new_report({'cpu': 'test', 'directory': 'private path'}, {'Graphics': {'resolution': '3200x2000'}})
        self.assertNotIn('private path', json.dumps(report_model_payload(report)))
        original = json.dumps(report, sort_keys=True)
        class FakeProvider:
            name = 'fake'
            def complete(self, request):
                return ModelResponse('{"summary":"说明","steps":["测试建议"]}', 'fake', 'fake')
        self.assertEqual(explain_victoria3(FakeProvider(), report)['source'], 'model')
        class InvalidProvider:
            name = 'invalid'
            def complete(self, request):
                raise RuntimeError('private credential must not appear')
        result = explain_victoria3(InvalidProvider(), report)
        self.assertEqual(result['source'], 'local')
        self.assertNotIn('credential', str(result))
        self.assertEqual(json.dumps(report, sort_keys=True), original)
