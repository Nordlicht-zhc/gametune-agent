from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.api import APIConfig
from tools.api_settings import clear_saved_api_config, load_saved_api_config, save_saved_api_config


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required for this test")
class LocalAPISettingsTests(unittest.TestCase):
    def test_round_trip_protects_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api_settings.json"
            config = APIConfig(
                "https://example.test/v1",
                "secret-token",
                "model-a",
                45,
                "medium",
            )
            self.assertEqual(save_saved_api_config(config, path), path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", raw)
            self.assertTrue(raw["api_key_protected"])
            self.assertNotEqual(raw["api_key_protected"], config.api_key)

            restored = load_saved_api_config(path)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.base_url, config.base_url)
            self.assertEqual(restored.api_key, config.api_key)
            self.assertEqual(restored.model, config.model)
            self.assertEqual(restored.timeout_seconds, config.timeout_seconds)
            self.assertEqual(restored.reasoning_effort, config.reasoning_effort)

            clear_saved_api_config(path)
            self.assertFalse(path.exists())
            self.assertIsNone(load_saved_api_config(path))


if __name__ == "__main__":
    unittest.main()
