"""Run synthetic workflow and UI scenarios without touching user data or APIs."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    root = Path(__file__).resolve().parent
    output = root / ".qa"
    output.mkdir(exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # Keep temporary SQLite files inside the workspace, including in sandboxes.
    with tempfile.TemporaryDirectory(prefix="scenarios-", dir=output) as directory:
        previous = tempfile.tempdir
        tempfile.tempdir = directory
        try:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames([
                "tests.test_user_scenarios",
                "tests.test_ui_workflow.UiWorkflowTests.test_limit_diagnostic_inputs_submit_actual_values_and_block_during_restore",
                "tests.test_ui_workflow.UiWorkflowTests.test_experiment_belongs_to_overview_and_can_return",
                "tests.test_ui_workflow.UiWorkflowTests.test_assessment_returns_to_action_page_until_task_is_complete",
            ])
            report = io.StringIO()
            print("正在运行模拟流程与界面操作样例，不连接模型接口，不读取真实测试数据库……", flush=True)
            with patch("app.provider_from_env", return_value=None), patch("app.load_saved_api_config", return_value=None):
                result = unittest.TextTestRunner(stream=report, verbosity=2).run(suite)
            text = report.getvalue()
            destination = output / "scenario-results.txt"
            destination.write_text(text, encoding="utf-8")
            print(text)
            print(f"测试报告：{destination}")
            return 0 if result.wasSuccessful() else 1
        finally:
            tempfile.tempdir = previous


if __name__ == "__main__":
    raise SystemExit(main())
