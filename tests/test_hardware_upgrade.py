import unittest
from agent.victoria3_model import explain_victoria3, report_model_payload
from tools.configurations import create_record


class UpgradeTests(unittest.TestCase):
    def record(self, goal, gpu):
        record = create_record({}, {'Graphics': {}})
        record['upgrade_goal'] = goal
        record['observations'] = {'run_count': 1, 'gpu_usage': gpu, 'cpu_usage': 12}
        return record

    def test_frame_goal_with_saturated_gpu_prioritizes_gpu(self):
        record = self.record('提高画面帧率', 98)
        result = explain_victoria3(None, record)
        self.assertEqual(result['summary'], '优先评估显卡升级')
        self.assertEqual(report_model_payload(record)['upgrade_goal'], '提高画面帧率')

    def test_simulation_goal_does_not_infer_gpu_upgrade_from_usage(self):
        result = explain_victoria3(None, self.record('加快游戏日期推进', 98))
        self.assertNotEqual(result['summary'], '优先评估显卡升级')
        self.assertIn('游戏天/秒', ''.join(result['steps']))
