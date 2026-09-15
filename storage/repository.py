from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator

from models.schemas import BenchmarkRecord, Decision, OptimizationTask


_UNSET = object()


def _json_default(value: Any) -> Any:
    """Serialize domain values that can occur in runtime metadata."""
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"无法序列化运行时数据：{type(value).__name__}")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _loads_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SQLiteRepository:
    """SQLite persistence for tasks, benchmarks, decisions, and runtime state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS benchmarks (
                    record_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    round_number INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('baseline', 'candidate')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    round_number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_runtime (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                    current_record_id TEXT,
                    best_record_id TEXT,
                    workflow_state TEXT NOT NULL,
                    round_number INTEGER NOT NULL
                );
                """
            )
            # Existing databases have no runtime metadata column.  The guarded
            # ALTER makes opening the same database repeatedly idempotent.
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(task_runtime)").fetchall()
            }
            if "extra_json" not in columns:
                try:
                    db.execute(
                        "ALTER TABLE task_runtime ADD COLUMN extra_json TEXT NOT NULL DEFAULT '{}'"
                    )
                except sqlite3.OperationalError as error:
                    # Another process may have completed the same guarded
                    # migration after our PRAGMA snapshot.  Re-checking the
                    # schema makes this race harmless while preserving other
                    # migration errors.
                    if "duplicate column name" not in str(error).lower():
                        raise

    def save_task(self, task: OptimizationTask) -> None:
        with self._session() as db:
            self._save_task(db, task)

    @staticmethod
    def _save_task(db: sqlite3.Connection, task: OptimizationTask) -> None:
        db.execute(
            """INSERT INTO tasks(task_id, payload, created_at) VALUES (?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET payload = excluded.payload""",
            (task.task_id, _dump(task.to_dict()), task.created_at),
        )

    def save_benchmark(
        self, task_id: str, record: BenchmarkRecord, round_number: int, role: str
    ) -> None:
        if role not in {"baseline", "candidate"}:
            raise ValueError("role 必须是 baseline 或 candidate")
        with self._session() as db:
            self._save_benchmark(db, task_id, record, round_number, role)

    @staticmethod
    def _save_benchmark(
        db: sqlite3.Connection,
        task_id: str,
        record: BenchmarkRecord,
        round_number: int,
        role: str,
    ) -> None:
        if role not in {"baseline", "candidate"}:
            raise ValueError("role 必须是 baseline 或 candidate")
        # ``record_id`` is the submission identity.  Replacing a row here
        # would make a repeated UI submission silently rewrite history (and
        # could even detach a record belonging to another task), so reject it
        # before the transaction writes anything else.
        existing = db.execute(
            "SELECT task_id, round_number, role, payload FROM benchmarks WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] == task_id:
                if (
                    role == "baseline"
                    and existing["role"] == role
                    and existing["round_number"] == round_number
                    and existing["payload"] == _dump(record.to_dict())
                ):
                    # Retrying task creation with the same baseline is safe;
                    # candidate submissions remain strict below.
                    return
                raise ValueError(f"测试记录已提交：{record.record_id}")
            raise ValueError(f"测试记录 ID 已属于其他任务：{record.record_id}")
        db.execute(
            """INSERT INTO benchmarks
               (record_id, task_id, round_number, role, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.record_id,
                task_id,
                round_number,
                role,
                _dump(record.to_dict()),
                record.created_at,
            ),
        )

    def save_decision(self, task_id: str, round_number: int, decision: Decision) -> None:
        with self._session() as db:
            self._save_decision(db, task_id, round_number, decision)

    @staticmethod
    def _save_decision(
        db: sqlite3.Connection, task_id: str, round_number: int, decision: Decision
    ) -> None:
        db.execute(
            "INSERT INTO decisions(task_id, round_number, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, round_number, _dump(decision.to_dict()), decision.created_at),
        )

    def save_task_with_baseline(
        self,
        task: OptimizationTask,
        baseline: BenchmarkRecord,
        *,
        round_number: int = 0,
        current_record_id: str | None = None,
        best_record_id: str | None = None,
        workflow_state: str | None = None,
        runtime_round_number: int | None = None,
        extra: dict[str, Any] | None = None,
        runtime_extra: dict[str, Any] | None = None,
    ) -> None:
        """Persist task, baseline, and optional initial runtime atomically."""
        with self._session() as db:
            self._save_task(db, task)
            self._save_benchmark(db, task.task_id, baseline, round_number, "baseline")
            if workflow_state is not None:
                supplied_extra = extra if extra is not None else runtime_extra
                self._upsert_runtime(
                    db,
                    task.task_id,
                    current_record_id or baseline.record_id,
                    best_record_id or baseline.record_id,
                    workflow_state,
                    runtime_round_number if runtime_round_number is not None else round_number,
                    supplied_extra or {},
                )

    # Keep aliases for controller versions that use a different verb.
    save_task_and_baseline = save_task_with_baseline
    create_task_with_baseline = save_task_with_baseline
    save_task_with_initial_baseline = save_task_with_baseline

    def save_task_with_runtime(
        self,
        task: OptimizationTask,
        *,
        current_record_id: str | None | object = _UNSET,
        best_record_id: str | None | object = _UNSET,
        workflow_state: str | object = _UNSET,
        round_number: int | object = _UNSET,
        extra: dict[str, Any] | None = None,
        runtime_extra: dict[str, Any] | None = None,
    ) -> None:
        """Atomically update a task payload and its runtime snapshot.

        This is used when a user supplies missing task information after the
        initial baseline was recorded.  Omitted runtime pointers are retained,
        so adding a supplementary value cannot manufacture a new baseline or
        move the accepted/best record back to a fake value.
        """
        supplied_extra = extra if extra is not None else runtime_extra
        with self._session() as db:
            self._save_task(db, task)
            existing = db.execute(
                "SELECT * FROM task_runtime WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            if existing is None:
                baseline_row = db.execute(
                    """SELECT record_id FROM benchmarks
                         WHERE task_id = ? AND role = 'baseline'
                         ORDER BY round_number, created_at LIMIT 1""",
                    (task.task_id,),
                ).fetchone()
                if current_record_id is _UNSET:
                    current = baseline_row["record_id"] if baseline_row else None
                else:
                    current = current_record_id
                if best_record_id is _UNSET:
                    best = baseline_row["record_id"] if baseline_row else None
                else:
                    best = best_record_id
                state = "waiting_input" if workflow_state is _UNSET else workflow_state
                round_value = 0 if round_number is _UNSET else round_number
                merged: dict[str, Any] = {}
            else:
                current = (
                    existing["current_record_id"]
                    if current_record_id is _UNSET
                    else current_record_id
                )
                best = (
                    existing["best_record_id"]
                    if best_record_id is _UNSET
                    else best_record_id
                )
                state = (
                    existing["workflow_state"]
                    if workflow_state is _UNSET
                    else workflow_state
                )
                round_value = (
                    existing["round_number"] if round_number is _UNSET else round_number
                )
                merged = _loads_object(existing["extra_json"])
            if supplied_extra is not None:
                merged.update(supplied_extra)
            self._upsert_runtime(
                db,
                task.task_id,
                current,
                best,
                state,
                round_value,
                merged,
            )

    # Names used by different controller revisions during the supplement
    # flow.  They all retain the same atomic implementation above.
    save_task_and_runtime = save_task_with_runtime
    update_task_and_runtime = save_task_with_runtime

    def save_runtime_state(
        self,
        task_id: str,
        *,
        current_record_id: str | None,
        best_record_id: str | None,
        workflow_state: str,
        round_number: int,
        extra: dict[str, Any] | None = None,
        runtime_extra: dict[str, Any] | None = None,
        pending_plan: Any = _UNSET,
        experiment: Any = _UNSET,
        last_decision: Any = _UNSET,
        retry: Any = _UNSET,
        retry_same_experiment: Any = _UNSET,
        rollback: Any = _UNSET,
        retest: Any = _UNSET,
        failed_attempts: Any = _UNSET,
    ) -> None:
        """Upsert runtime state while preserving metadata omitted by old callers.

        ``extra`` and ``runtime_extra`` are equivalent.  Named metadata keys are
        convenience arguments for the controller and UI.
        """
        supplied_extra = extra if extra is not None else runtime_extra
        with self._session() as db:
            existing = db.execute(
                "SELECT extra_json FROM task_runtime WHERE task_id = ?", (task_id,)
            ).fetchone()
            merged = _loads_object(existing["extra_json"] if existing else None)
            if supplied_extra is not None:
                merged.update(supplied_extra)
            for key, value in (
                ("pending_plan", pending_plan),
                ("experiment", experiment),
                ("last_decision", last_decision),
                ("retry", retry),
                ("retry_same_experiment", retry_same_experiment),
                ("rollback", rollback),
                ("retest", retest),
                ("failed_attempts", failed_attempts),
            ):
                if value is not _UNSET:
                    merged[key] = value
                    if key == "retry":
                        merged["retry_same_experiment"] = value
                    elif key == "retry_same_experiment":
                        merged["retry"] = value
            self._upsert_runtime(
                db,
                task_id,
                current_record_id,
                best_record_id,
                workflow_state,
                round_number,
                merged,
            )

    @staticmethod
    def _upsert_runtime(
        db: sqlite3.Connection,
        task_id: str,
        current_record_id: str | None,
        best_record_id: str | None,
        workflow_state: str,
        round_number: int,
        extra: dict[str, Any],
    ) -> None:
        db.execute(
            """INSERT INTO task_runtime
               (task_id, current_record_id, best_record_id, workflow_state,
                round_number, extra_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 current_record_id = excluded.current_record_id,
                 best_record_id = excluded.best_record_id,
                 workflow_state = excluded.workflow_state,
                 round_number = excluded.round_number,
                 extra_json = excluded.extra_json""",
            (
                task_id,
                current_record_id,
                best_record_id,
                workflow_state,
                round_number,
                _dump(extra),
            ),
        )

    def commit_evaluation(
        self,
        task_id: str,
        candidate: BenchmarkRecord,
        *args: Any,
        round_number: int | None = None,
        current_record_id: str | None,
        best_record_id: str | None,
        workflow_state: str,
        extra: dict[str, Any] | None = None,
        runtime_extra: dict[str, Any] | None = None,
        task: OptimizationTask | None = None,
        runtime_round_number: int | None = None,
        next_round_number: int | None = None,
        pending_plan: Any = _UNSET,
        last_decision: Any = _UNSET,
        retry: Any = _UNSET,
        retry_same_experiment: Any = _UNSET,
        rollback: Any = _UNSET,
        failed_attempts: Any = _UNSET,
        decision: Decision | None = None,
    ) -> None:
        """Atomically save candidate, decision, and resulting runtime pointers.

        ``task`` can be supplied when a submission also changes task payload;
        restoring an existing task only needs ``task_id``.
        """
        # Accept both contracts used during the controller migration:
        # commit_evaluation(id, candidate, decision, *, round_number=...) and
        # commit_evaluation(id, candidate, round_number, decision, ...).
        if decision is None:
            if len(args) == 1:
                decision = args[0]
            elif len(args) == 2:
                if round_number is not None:
                    raise TypeError("round_number 不能同时作为位置参数和关键字参数传入")
                round_number, decision = args
            else:
                raise TypeError("commit_evaluation 需要 decision，或 round_number 与 decision")
        elif args:
            raise TypeError("decision 不能同时作为位置参数和关键字参数传入")
        if not isinstance(decision, Decision):
            raise TypeError("decision 必须是 Decision")
        if round_number is None:
            raise TypeError("缺少 round_number")
        supplied_extra = extra if extra is not None else runtime_extra
        effective_runtime_round = (
            next_round_number
            if next_round_number is not None
            else runtime_round_number
            if runtime_round_number is not None
            else round_number
        )
        with self._session() as db:
            if task is not None:
                if task.task_id != task_id:
                    raise ValueError("task_id 与 task.task_id 不一致")
                self._save_task(db, task)
            if db.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise ValueError(f"任务不存在：{task_id}")
            self._save_benchmark(db, task_id, candidate, round_number, "candidate")
            self._save_decision(db, task_id, round_number, decision)
            existing = db.execute(
                "SELECT extra_json FROM task_runtime WHERE task_id = ?", (task_id,)
            ).fetchone()
            merged = _loads_object(existing["extra_json"] if existing else None)
            if supplied_extra is not None:
                merged.update(supplied_extra)
            # Keep the durable runtime snapshot self-contained.  The separate
            # decisions table remains the canonical history, while these
            # fields let a restore screen recover the latest decision and its
            # retry/rollback context in one read.
            if last_decision is _UNSET:
                merged["last_decision"] = decision.to_dict()
            else:
                merged["last_decision"] = last_decision
            if retry_same_experiment is not _UNSET:
                retry_value = retry_same_experiment
                merged["retry_same_experiment"] = retry_value
                merged["retry"] = retry_value
            elif retry is not _UNSET:
                retry_value = retry
                merged["retry"] = retry_value
                merged["retry_same_experiment"] = retry_value
            else:
                retry_value = decision.retry_same_experiment
                merged["retry"] = retry_value
                merged["retry_same_experiment"] = retry_value
            if rollback is not _UNSET:
                merged["rollback"] = rollback
            elif "rollback" not in merged:
                merged["rollback"] = decision.rollback_required
            if failed_attempts is not _UNSET:
                merged["failed_attempts"] = failed_attempts
            self._upsert_runtime(
                db,
                task_id,
                current_record_id,
                best_record_id,
                workflow_state,
                effective_runtime_round,
                merged,
            )

    def runtime_state(self, task_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM task_runtime WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["extra"] = _loads_object(data.get("extra_json"))
        return data

    def runtime_metadata(self, task_id: str) -> dict[str, Any]:
        state = self.runtime_state(task_id)
        return state.get("extra", {}) if state else {}

    def save_runtime_metadata(self, task_id: str, **metadata: Any) -> None:
        """Merge arbitrary restore metadata without changing workflow pointers."""
        state = self.runtime_state(task_id)
        if state is None:
            baseline = self.benchmark_for_role(task_id, "baseline")
            if baseline is None:
                raise ValueError(f"任务没有基线记录：{task_id}")
            self.save_runtime_state(
                task_id,
                current_record_id=baseline.record_id,
                best_record_id=baseline.record_id,
                workflow_state="waiting_input",
                round_number=0,
                extra=metadata,
            )
            return
        merged = dict(state.get("extra", {}))
        merged.update(metadata)
        self.save_runtime_state(
            task_id,
            current_record_id=state.get("current_record_id"),
            best_record_id=state.get("best_record_id"),
            workflow_state=state["workflow_state"],
            round_number=int(state["round_number"]),
            extra=merged,
        )

    def save_pending_plan(self, task_id: str, plan: Any) -> None:
        self.save_runtime_metadata(task_id, pending_plan=plan)

    def pending_plan(self, task_id: str) -> dict[str, Any] | None:
        value = self.runtime_metadata(task_id).get("pending_plan")
        return value if isinstance(value, dict) else None

    def clear_pending_plan(self, task_id: str) -> None:
        state = self.runtime_state(task_id)
        if state is None:
            return
        merged = dict(state.get("extra", {}))
        merged.pop("pending_plan", None)
        self.save_runtime_state(
            task_id,
            current_record_id=state.get("current_record_id"),
            best_record_id=state.get("best_record_id"),
            workflow_state=state["workflow_state"],
            round_number=int(state["round_number"]),
            extra=merged,
        )

    def task_payload(self, task_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT payload FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _loads_object(row["payload"]) if row else None

    def benchmark_for_role(self, task_id: str, role: str) -> BenchmarkRecord | None:
        with self._session() as db:
            row = db.execute(
                "SELECT payload FROM benchmarks WHERE task_id = ? AND role = ? ORDER BY round_number, created_at LIMIT 1",
                (task_id, role),
            ).fetchone()
        return BenchmarkRecord(**json.loads(row["payload"])) if row else None

    def benchmark_by_id(self, task_id: str, record_id: str | None) -> BenchmarkRecord | None:
        if not record_id:
            return None
        with self._session() as db:
            row = db.execute(
                "SELECT payload FROM benchmarks WHERE task_id = ? AND record_id = ?",
                (task_id, record_id),
            ).fetchone()
        return BenchmarkRecord(**json.loads(row["payload"])) if row else None

    def task_history(self, task_id: str) -> list[dict[str, Any]]:
        with self._session() as db:
            rows = db.execute(
                "SELECT round_number, role, payload FROM benchmarks WHERE task_id = ? ORDER BY round_number, created_at",
                (task_id,),
            ).fetchall()
        return [
            {"round_number": row["round_number"], "role": row["role"], **json.loads(row["payload"])}
            for row in rows
        ]

    def decisions(self, task_id: str) -> list[dict[str, Any]]:
        """Return persisted decisions in insertion order for UI history."""
        with self._session() as db:
            rows = db.execute(
                "SELECT id, round_number, payload, created_at FROM decisions WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "round_number": row["round_number"],
                "created_at": row["created_at"],
                **json.loads(row["payload"]),
            }
            for row in rows
        ]

    task_decisions = decisions

    def last_decision(self, task_id: str) -> dict[str, Any] | None:
        """Return the newest decision for a completed report or restore screen."""
        entries = self.decisions(task_id)
        if entries:
            return entries[-1]
        value = self.runtime_metadata(task_id).get("last_decision")
        return value if isinstance(value, dict) else None

    decision_history = decisions
    list_decisions = decisions

    def task_statistics(self, task_id: str) -> dict[str, int]:
        """Count submitted results separately from independent experiment rounds.

        A RETEST can submit another candidate in the same round, so counting
        candidate rows alone would overstate the number of experiments.
        """
        with self._session() as db:
            row = db.execute(
                """SELECT COUNT(*) AS candidate_count,
                          COUNT(DISTINCT round_number) AS experiment_count
                     FROM benchmarks
                    WHERE task_id = ? AND role = 'candidate'""",
                (task_id,),
            ).fetchone()
            decision_row = db.execute(
                "SELECT COUNT(*) AS decision_count FROM decisions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return {
            "candidate_count": int(row["candidate_count"]),
            "experiment_count": int(row["experiment_count"]),
            "decision_count": int(decision_row["decision_count"]),
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        """List task summaries with runtime state for the restore screen."""
        with self._session() as db:
            rows = db.execute(
                """SELECT t.task_id, t.payload, t.created_at,
                          r.current_record_id, r.best_record_id,
                          r.workflow_state, r.round_number, r.extra_json
                     FROM tasks AS t
                     LEFT JOIN task_runtime AS r ON r.task_id = t.task_id
                     ORDER BY t.created_at DESC"""
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            task_payload = _loads_object(row["payload"])
            runtime = None
            if row["workflow_state"] is not None:
                runtime = {
                    "current_record_id": row["current_record_id"],
                    "best_record_id": row["best_record_id"],
                    "workflow_state": row["workflow_state"],
                    "round_number": row["round_number"],
                    "extra_json": row["extra_json"] or "{}",
                    "extra": _loads_object(row["extra_json"]),
                }
            statistics = self.task_statistics(row["task_id"])
            # Keep task fields at the top level for lightweight UI selectors,
            # while retaining ``task`` as the structured payload used by
            # restore code.
            summary = dict(task_payload)
            summary.update(
                {
                    "task_id": row["task_id"],
                    "task_name": task_payload.get("name", ""),
                    "created_at": row["created_at"],
                    "task": task_payload,
                    "runtime": runtime,
                    "statistics": statistics,
                }
            )
            if runtime is not None:
                summary["pending_plan"] = runtime["extra"].get("pending_plan")
                summary["last_decision"] = runtime["extra"].get("last_decision")
            result.append(
                summary
            )
        return result

    tasks = list_tasks
    list_task_summaries = list_tasks

    def load_task_bundle(self, task_id: str) -> dict[str, Any] | None:
        """Load all persisted pieces needed to restore a task."""
        payload = self.task_payload(task_id)
        if payload is None:
            return None
        runtime = self.runtime_state(task_id)
        baseline = self.benchmark_for_role(task_id, "baseline")
        current = self.benchmark_by_id(
            task_id, runtime.get("current_record_id") if runtime else None
        ) or baseline
        best = self.benchmark_by_id(
            task_id, runtime.get("best_record_id") if runtime else None
        ) or baseline
        return {
            "task_id": task_id,
            "task": payload,
            "task_payload": payload,
            "runtime": runtime,
            "runtime_metadata": runtime.get("extra", {}) if runtime else {},
            "baseline": baseline.to_dict() if baseline else None,
            "current": current.to_dict() if current else None,
            "best": best.to_dict() if best else None,
            "history": self.task_history(task_id),
            "decisions": self.decisions(task_id),
            "last_decision": self.last_decision(task_id),
            "statistics": self.task_statistics(task_id),
        }

    task_snapshot = load_task_bundle
    task_bundle = load_task_bundle
    load_task = load_task_bundle
