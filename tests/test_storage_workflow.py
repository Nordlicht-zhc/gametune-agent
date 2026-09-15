from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from models.schemas import AgentAction, BenchmarkRecord, Decision, OptimizationTask
from storage.repository import SQLiteRepository


def make_task() -> OptimizationTask:
    return OptimizationTask(
        name="storage-test",
        game="Counter-Strike 2",
        cpu="CPU",
        gpu="GPU",
        resolution="2560 × 1440",
        refresh_rate="300 Hz",
        test_scene="Workshop",
        target_avg_fps=300,
        target_low_fps=210,
    )


def make_record(avg: float = 280, low: float = 170) -> BenchmarkRecord:
    return BenchmarkRecord(
        avg,
        low,
        90,
        "2560 × 1440",
        "Workshop",
        repeats=3,
    )


class RepositoryWorkflowTests(unittest.TestCase):
    def test_task_and_baseline_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            with patch.object(
                SQLiteRepository,
                "_save_benchmark",
                side_effect=RuntimeError("simulate baseline write failure"),
            ):
                with self.assertRaises(RuntimeError):
                    repository.save_task_with_baseline(current_task, baseline)
            self.assertIsNone(repository.task_payload(current_task.task_id))

    def test_initial_runtime_and_evaluation_commit_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            repository.save_task_with_baseline(
                current_task,
                baseline,
                workflow_state="awaiting_test",
                runtime_round_number=1,
                extra={"pending_plan": {"setting": "分辨率"}},
            )
            candidate = make_record(300, 190)
            decision = Decision(AgentAction.KEEP_CHANGE, "保留")
            with patch.object(
                SQLiteRepository,
                "_save_decision",
                side_effect=RuntimeError("simulate decision write failure"),
            ):
                with self.assertRaises(RuntimeError):
                    repository.commit_evaluation(
                        current_task.task_id,
                        candidate,
                        decision,
                        round_number=1,
                        runtime_round_number=2,
                        current_record_id=candidate.record_id,
                        best_record_id=candidate.record_id,
                        workflow_state="awaiting_test",
                    )
            self.assertIsNone(repository.benchmark_by_id(current_task.task_id, candidate.record_id))
            self.assertEqual(repository.decisions(current_task.task_id), [])
            self.assertEqual(repository.runtime_state(current_task.task_id)["round_number"], 1)
            self.assertEqual(
                repository.runtime_metadata(current_task.task_id)["pending_plan"]["setting"],
                "分辨率",
            )

    def test_legacy_runtime_schema_migrates_and_preserves_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            task = make_task()
            baseline = make_record()
            db = sqlite3.connect(path)
            try:
                db.executescript(
                    """
                    CREATE TABLE tasks (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE benchmarks (record_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                        round_number INTEGER NOT NULL, role TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                        round_number INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE task_runtime (task_id TEXT PRIMARY KEY, current_record_id TEXT,
                        best_record_id TEXT, workflow_state TEXT NOT NULL, round_number INTEGER NOT NULL);
                    """
                )
                db.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?)",
                    (task.task_id, json.dumps(task.to_dict(), ensure_ascii=False), task.created_at),
                )
                db.execute(
                    "INSERT INTO benchmarks VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        baseline.record_id,
                        task.task_id,
                        0,
                        "baseline",
                        json.dumps(baseline.to_dict(), ensure_ascii=False),
                        baseline.created_at,
                    ),
                )
                db.execute(
                    "INSERT INTO task_runtime VALUES (?, ?, ?, ?, ?)",
                    (task.task_id, baseline.record_id, baseline.record_id, "awaiting_test", 1),
                )
                db.commit()
            finally:
                db.close()
            repository = SQLiteRepository(path)
            state = repository.runtime_state(task.task_id)
            self.assertEqual(state["workflow_state"], "awaiting_test")
            self.assertEqual(state["extra"], {})
            repository.save_runtime_state(
                task.task_id,
                current_record_id=baseline.record_id,
                best_record_id=baseline.record_id,
                workflow_state="awaiting_test",
                round_number=1,
                pending_plan={"round": 1},
            )
            # Reopening must not attempt the ALTER a second time or lose metadata.
            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.runtime_metadata(task.task_id)["pending_plan"]["round"], 1)
            self.assertEqual(reopened.task_payload(task.task_id)["task_id"], task.task_id)

    def test_history_restore_decisions_and_retest_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            repository.save_task_with_baseline(current_task, baseline)
            repository.save_runtime_state(
                current_task.task_id,
                current_record_id=baseline.record_id,
                best_record_id=baseline.record_id,
                workflow_state="awaiting_test",
                round_number=1,
                extra={"rollback": {"setting": "分辨率", "value": "2560 × 1440"}},
            )
            first = make_record(281, 171)
            second = make_record(282, 172)
            repository.commit_evaluation(
                current_task.task_id,
                first,
                Decision(AgentAction.RETEST, "复测"),
                round_number=1,
                runtime_round_number=1,
                current_record_id=baseline.record_id,
                best_record_id=baseline.record_id,
                workflow_state="awaiting_test",
                extra={"retest": {"same_configuration": True}},
            )
            repository.commit_evaluation(
                current_task.task_id,
                second,
                Decision(AgentAction.KEEP_CHANGE, "保留"),
                round_number=1,
                runtime_round_number=2,
                current_record_id=second.record_id,
                best_record_id=second.record_id,
                workflow_state="awaiting_test",
            )
            self.assertEqual(len(repository.task_history(current_task.task_id)), 3)
            self.assertEqual(repository.task_statistics(current_task.task_id)["candidate_count"], 2)
            self.assertEqual(repository.task_statistics(current_task.task_id)["experiment_count"], 1)
            self.assertEqual(repository.last_decision(current_task.task_id)["action"], "keep_change")
            self.assertEqual(repository.runtime_metadata(current_task.task_id)["rollback"]["setting"], "分辨率")
            bundle = repository.load_task_bundle(current_task.task_id)
            self.assertEqual(len(bundle["decisions"]), 2)
            self.assertEqual(bundle["runtime"]["round_number"], 2)
            self.assertEqual(len(repository.list_tasks()), 1)
            self.assertEqual(repository.list_tasks()[0]["statistics"]["experiment_count"], 1)
            summary = repository.list_tasks()[0]
            self.assertEqual(summary["name"], current_task.name)
            self.assertEqual(summary["task"]["task_id"], current_task.task_id)

    def test_duplicate_candidate_submission_is_rejected_without_new_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            repository.save_task_with_baseline(
                current_task,
                baseline,
                workflow_state="awaiting_test",
                runtime_round_number=1,
            )
            candidate = make_record(300, 190)
            decision = Decision(AgentAction.KEEP_CHANGE, "保留", accepted=True)
            repository.commit_evaluation(
                current_task.task_id,
                candidate,
                decision,
                round_number=1,
                next_round_number=2,
                current_record_id=candidate.record_id,
                best_record_id=candidate.record_id,
                workflow_state="awaiting_test",
            )
            with self.assertRaisesRegex(ValueError, "测试记录已提交"):
                repository.commit_evaluation(
                    current_task.task_id,
                    candidate,
                    decision,
                    round_number=1,
                    next_round_number=2,
                    current_record_id=candidate.record_id,
                    best_record_id=candidate.record_id,
                    workflow_state="awaiting_test",
                )
            self.assertEqual(len(repository.task_history(current_task.task_id)), 2)
            self.assertEqual(len(repository.decisions(current_task.task_id)), 1)
            self.assertEqual(repository.runtime_state(current_task.task_id)["round_number"], 2)

    def test_commit_persists_latest_decision_context_in_runtime_extra(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            repository.save_task_with_baseline(
                current_task,
                baseline,
                workflow_state="awaiting_test",
                runtime_round_number=1,
                extra={"failed_attempts": 1},
            )
            candidate = make_record(281, 171)
            decision = Decision(
                AgentAction.RETEST,
                "复测",
                retry_same_experiment=True,
                rollback_required=False,
            )
            repository.commit_evaluation(
                current_task.task_id,
                candidate,
                decision,
                round_number=1,
                next_round_number=1,
                current_record_id=baseline.record_id,
                best_record_id=baseline.record_id,
                workflow_state="awaiting_test",
                failed_attempts=2,
            )
            metadata = repository.runtime_metadata(current_task.task_id)
            self.assertEqual(metadata["last_decision"]["action"], "retest")
            self.assertTrue(metadata["retry"])
            self.assertTrue(metadata["last_decision"]["retry_same_experiment"])
            self.assertFalse(metadata["rollback"])
            self.assertEqual(metadata["failed_attempts"], 2)

    def test_supplemental_task_update_is_atomic_and_keeps_current_records(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "state.db")
            current_task = make_task()
            baseline = make_record()
            repository.save_task_with_baseline(
                current_task,
                baseline,
                workflow_state="awaiting_test",
                runtime_round_number=1,
                extra={"pending_plan": {"setting": "分辨率"}},
            )
            updated = replace(current_task, refresh_rate="360 Hz")
            repository.save_task_with_runtime(
                updated,
                extra={"supplement": {"refresh_rate": "360 Hz"}},
            )
            self.assertEqual(repository.task_payload(current_task.task_id)["refresh_rate"], "360 Hz")
            runtime = repository.runtime_state(current_task.task_id)
            self.assertEqual(runtime["current_record_id"], baseline.record_id)
            self.assertEqual(runtime["best_record_id"], baseline.record_id)
            self.assertEqual(repository.benchmark_for_role(current_task.task_id, "baseline").record_id, baseline.record_id)
            self.assertEqual(repository.runtime_metadata(current_task.task_id)["pending_plan"]["setting"], "分辨率")
            self.assertEqual(repository.runtime_metadata(current_task.task_id)["supplement"]["refresh_rate"], "360 Hz")

            failing = replace(updated, refresh_rate="500 Hz")
            with patch.object(
                SQLiteRepository,
                "_upsert_runtime",
                side_effect=RuntimeError("simulate runtime write failure"),
            ):
                with self.assertRaises(RuntimeError):
                    repository.save_task_with_runtime(failing, extra={"supplement": {"refresh_rate": "500 Hz"}})
            self.assertEqual(repository.task_payload(current_task.task_id)["refresh_rate"], "360 Hz")
            self.assertEqual(repository.runtime_metadata(current_task.task_id)["supplement"]["refresh_rate"], "360 Hz")


if __name__ == "__main__":
    unittest.main()
