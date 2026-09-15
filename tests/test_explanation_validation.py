import json
import unittest

from agent.prompts import parse_explanation


class ExplanationValidationTests(unittest.TestCase):
    def test_blank_or_wrong_type_reasoning_is_rejected(self):
        for reasoning in ([" "], [None], [123], "a reason"):
            with self.subTest(reasoning=reasoning), self.assertRaises(ValueError):
                parse_explanation(json.dumps({"summary": "结果", "next_step": "复测", "reasoning": reasoning}))

    def test_null_required_string_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_explanation('{"summary":null,"next_step":"复测","reasoning":["证据不足"]}')

    def test_valid_fenced_json_is_accepted(self):
        result = parse_explanation('```json\n{"summary":" 结果 ","next_step":"复测","reasoning":["证据不足"]}\n```')
        self.assertEqual(result["summary"], "结果")


if __name__ == "__main__":
    unittest.main()
