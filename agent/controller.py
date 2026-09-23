from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from statistics import median
from typing import Any

from agent.api import ModelProvider
from agent.evaluator import evaluate_diagnostic_result, evaluate_result
from agent.explainer import explain_decision
from agent.next_step import NextStepSelection, choose_next_step
from agent.planner import (
    GENERIC_OPTIMIZATION_DIRECTIONS,
    gpu_optimization_candidates,
    plan_candidate_id,
    plan_candidates,
    plan_next_experiment,
)
from agent.state_machine import Workflow, WorkflowPhase, WorkflowState
from agent.validator import KnowledgeBase, validate_plan
from models.schemas import AgentAction, BenchmarkRecord, Decision, Evidence, ExperimentPlan, OptimizationTask
from storage.repository import SQLiteRepository
from tools.analyze_evidence import analyze_evidence
from tools.calculate_metrics import weighted_performance_score


class GameTuneController:
    """Coordinate deterministic tools and persist workflow bookkeeping.

    The controller owns the accepted and best records in memory while the
    repository owns their durable IDs. A pending plan is retained for a
    ``RETEST`` or rollback confirmation, so the next UI render cannot silently
    replace the experiment with a different one.
    """

    def __init__(
        self,
        database_path: str | Path,
        model_provider: ModelProvider | None = None,
        knowledge_path: str | Path | None = None,
    ):
        self.repository = SQLiteRepository(database_path)
        self.model_provider = model_provider
        self.knowledge = KnowledgeBase(knowledge_path or Path(__file__).resolve().parent.parent / "knowledge")
        self.workflows: dict[str, Workflow] = {}
        self.current_records: dict[str, BenchmarkRecord] = {}
        self.best_records: dict[str, BenchmarkRecord] = {}
        self.pending_plans: dict[str, ExperimentPlan] = {}
        self.last_decisions: dict[str, Decision] = {}
        # Runs submitted after a RETEST belong to the same optimization round
        # and must be accumulated before they can confirm a change.
        self._round_runs: dict[str, dict[int, list[dict[str, float]]]] = {}
        self._round_fingerprints: dict[str, dict[int, tuple[Any, ...]]] = {}
        self._retry_counts: dict[str, int] = {}
        # A reverted setting is kept in this ledger so planning cannot offer
        # the same failed action immediately after rollback.
        self._failed_actions: dict[str, set[str]] = {}
        self._supplements: dict[str, dict[str, Any]] = {}
        self._diagnostic_results: dict[str, list[dict[str, Any]]] = {}

    def create_task(
        self,
        task: OptimizationTask,
        baseline: BenchmarkRecord,
        *,
        fps_cap_known: bool = False,
        vsync_known: bool = False,
    ) -> tuple[list[Evidence], ExperimentPlan]:
        """Validate the initial plan before writing any task data."""
        if not baseline.configuration_confirmed:
            raise ValueError("请先确认基线配置和测试条件，再创建优化任务")
        baseline_runs = len(baseline.runs) if baseline.runs else baseline.repeats
        if baseline_runs < 2:
            raise ValueError("基线至少需要 2 次有效测试后才能创建优化任务")
        if len(baseline.runs) == 2:
            avg_values = [float(run["avg_fps"]) for run in baseline.runs]
            low_values = [float(run["low_fps"]) for run in baseline.runs]

            def spread(values: list[float]) -> float:
                centre = median(values)
                return (max(values) - min(values)) / centre * 100 if centre else float("inf")

            if spread(avg_values) > 5.0 or spread(low_values) > 5.0:
                raise ValueError("两次基线结果差异超过 5%，请补充第 3 次基线测试")
        workflow = Workflow()
        workflow.start()
        workflow.validated()
        baseline_configuration = baseline.configuration or {}
        cap_value = baseline_configuration.get("fps_cap", task.fps_cap)
        vsync_value = baseline_configuration.get("vsync", task.vsync)
        fps_cap_known = bool(fps_cap_known or task.fps_cap is not None or str(cap_value).strip().lower() not in {"", "none", "unknown", "未确认"})
        vsync_known = bool(vsync_known or task.vsync != "unknown" or str(vsync_value).strip().lower() not in {"", "none", "unknown", "未确认"})
        evidence = analyze_evidence(task, baseline, fps_cap_known=fps_cap_known, vsync_known=vsync_known)
        plan = self._local_next_plan(task, baseline, evidence, 1)
        # Validation intentionally happens before save_task/save_benchmark so
        # an unsupported or malformed plan cannot leave a half-created task.
        validate_plan(task, plan, self.knowledge)

        if plan.action != AgentAction.FINISH:
            workflow.set_phase(plan.purpose, action=plan.action.value, setting_id=plan.setting_id)
            workflow.wait_for_test()
        else:
            workflow.complete_without_test()
        initial_supplement = {"fps_cap_known": fps_cap_known, "vsync_known": vsync_known}
        initial_extra = {"workflow_phase": workflow.phase.value, "supplement": initial_supplement}
        if plan.action != AgentAction.FINISH:
            initial_extra["pending_plan"] = asdict(plan)
        save_bundle = getattr(self.repository, "save_task_with_baseline", None)
        if callable(save_bundle):
            save_bundle(
                task,
                baseline,
                workflow_state=workflow.state.value,
                runtime_round_number=workflow.round_number,
                extra=initial_extra,
            )
        else:
            self.repository.save_task(task)
            self.repository.save_benchmark(task.task_id, baseline, 0, "baseline")
        self.current_records[task.task_id] = baseline
        self.best_records[task.task_id] = baseline
        self._round_runs[task.task_id] = {}
        self._round_fingerprints[task.task_id] = {}
        self._retry_counts[task.task_id] = 0
        self._failed_actions[task.task_id] = set()
        self._supplements[task.task_id] = initial_supplement
        self._diagnostic_results[task.task_id] = []
        if plan.action == AgentAction.FINISH:
            self._clear_pending_plan(task.task_id)
        else:
            self.pending_plans[task.task_id] = plan
        self.workflows[task.task_id] = workflow
        return evidence, plan

    def evaluate(
        self,
        task: OptimizationTask,
        baseline: BenchmarkRecord,
        candidate: BenchmarkRecord,
        round_number: int,
        *,
        reference: BenchmarkRecord | None = None,
    ) -> Decision:
        workflow = self.workflows.get(task.task_id)
        if workflow is None:
            raise ValueError("任务尚未初始化，不能直接评估结果")
        if workflow.state != WorkflowState.AWAITING_TEST:
            raise ValueError(f"当前状态不能评估：{workflow.state.value}")
        if round_number != workflow.round_number:
            raise ValueError(f"实验轮次不匹配：应为 {workflow.round_number}，收到 {round_number}")

        pending = self.pending_plan(task.task_id)
        candidate = self._mark_configuration_mismatch(candidate, pending)
        candidate, combined_runs = self._candidate_with_accumulated_runs(
            task.task_id, round_number, candidate
        )
        # The controller's accepted record is authoritative.  A UI may hold
        # a stale reference while a restored task has already accepted a
        # previous candidate in this round.
        comparison = self.current_records.get(task.task_id) or reference or baseline
        retry_count = self._retry_counts.get(task.task_id, 0)
        diagnostic_plan = pending is not None and pending.purpose == "diagnostic" and pending.action not in {
            AgentAction.REQUEST_DATA,
            AgentAction.FINISH,
        }
        adaptive_plan = pending is not None and pending.action not in {
            AgentAction.REQUEST_DATA,
            AgentAction.FINISH,
        }
        if diagnostic_plan:
            decision = evaluate_diagnostic_result(
                task,
                candidate,
                round_number=round_number,
                reference=comparison,
                setting_id=pending.setting_id if pending else "",
                retry_count=retry_count,
                max_retries=1,
            )
        else:
            decision = evaluate_result(
                task,
                baseline,
                candidate,
                round_number=round_number,
                reference=comparison,
                minimum_repeats=1 if adaptive_plan else 3,
                acceptance_repeats=3 if adaptive_plan else None,
                retry_count=retry_count,
                max_retries=2 if adaptive_plan else task.max_rounds,
                accept_neutral_change=self._accept_neutral_platform_change(candidate, pending),
            )
        next_workflow = Workflow(workflow.state, workflow.round_number, workflow.phase)
        next_workflow.begin_evaluation()
        self._transition_after_decision(next_workflow, decision, pending)
        accepted_current = candidate if decision.accepted else self.current_records.get(task.task_id, baseline)
        best = self.best_records.get(task.task_id, baseline)
        accepted_best = self._better_record(task, candidate, best) if decision.accepted else best
        keep_pending = decision.action in {AgentAction.RETEST, AgentAction.REVERT_CHANGE} and not (
            decision.task_complete and decision.action == AgentAction.RETEST
        )
        pending_payload = asdict(pending) if keep_pending and pending else None
        failed_actions = set(self._failed_actions.get(task.task_id, set()))
        if decision.action == AgentAction.REVERT_CHANGE and pending is not None and not diagnostic_plan:
            failed_actions.add(self._action_key(pending))
        diagnostic_results = list(self._diagnostic_results.get(task.task_id, []))
        if diagnostic_plan and decision.diagnostic_summary:
            invalid_markers = ("未确认实际配置", "测试条件不一致", "仍有明显波动", "缺少逐次测试数据")
            diagnostic_valid = not any(word in decision.diagnostic_summary for word in invalid_markers)
            if not diagnostic_valid or any(word in decision.diagnostic_summary for word in ("不明确", "方向不一致")):
                support = "insufficient"
            elif pending.setting_id in {"power_mode", "cooling_mode", "graphics_mode"}:
                support = (
                    "platform"
                    if abs(decision.avg_delta_pct) >= 5 or abs(decision.low_delta_pct) >= 5
                    else "insufficient"
                )
            elif decision.avg_delta_pct >= 10:
                support = "gpu"
            elif abs(decision.avg_delta_pct) <= 5:
                support = "cpu_or_other"
            else:
                support = "insufficient"
            diagnostic_results.append({
                "kind": pending.setting_id or pending.setting,
                "summary": decision.diagnostic_summary,
                "avg_delta_pct": decision.avg_delta_pct,
                "low_delta_pct": decision.low_delta_pct,
                "supports": support,
                "valid": diagnostic_valid,
            })
        if decision.action != AgentAction.RETEST:
            next_retry_count = 0
        else:
            next_retry_count = retry_count + 1 if not decision.task_complete else retry_count
        self._commit_evaluation(
            task,
            candidate,
            round_number,
            decision,
            next_workflow,
            current_record=accepted_current,
            best_record=accepted_best,
            pending_payload=pending_payload,
            retry_count=next_retry_count,
            failed_actions=failed_actions,
            diagnostic_results=diagnostic_results,
        )

        # Memory is changed only after the transactional repository write.
        self.workflows[task.task_id] = next_workflow
        self.last_decisions[task.task_id] = decision
        if combined_runs is not None and not decision.task_complete:
            self._round_runs.setdefault(task.task_id, {})[round_number] = combined_runs
            self._round_fingerprints.setdefault(task.task_id, {})[round_number] = self._record_fingerprint(candidate)
        elif decision.accepted or decision.action == AgentAction.REVERT_CHANGE or decision.task_complete:
            self._round_runs.setdefault(task.task_id, {}).pop(round_number, None)
            self._round_fingerprints.setdefault(task.task_id, {}).pop(round_number, None)
        self._retry_counts[task.task_id] = next_retry_count
        self._failed_actions[task.task_id] = failed_actions
        self._diagnostic_results[task.task_id] = diagnostic_results
        if decision.accepted:
            self.current_records[task.task_id] = candidate
            if accepted_best.record_id == candidate.record_id:
                self.best_records[task.task_id] = candidate
        if keep_pending and pending is not None:
            self.pending_plans[task.task_id] = pending
        else:
            self._clear_pending_plan(task.task_id)
        return decision

    @staticmethod
    def _transition_after_decision(
        workflow: Workflow,
        decision: Decision,
        pending: ExperimentPlan | None = None,
    ) -> None:
        if decision.action == AgentAction.RETEST:
            workflow.retest() if not decision.task_complete else workflow.finish()
        elif decision.action == AgentAction.REVERT_CHANGE:
            workflow.request_rollback()
        elif decision.task_complete or decision.action == AgentAction.FINISH:
            workflow.finish()
        else:
            workflow.continue_testing(
                pending.purpose if pending else None,
                action=pending.action.value if pending else "",
                setting_id=pending.setting_id if pending else "",
            )

    def _commit_evaluation(
        self,
        task: OptimizationTask,
        candidate: BenchmarkRecord,
        round_number: int,
        decision: Decision,
        workflow: Workflow,
        *,
        current_record: BenchmarkRecord,
        best_record: BenchmarkRecord,
        pending_payload: dict[str, Any] | None,
        retry_count: int = 0,
        failed_actions: set[str] | None = None,
        diagnostic_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write candidate and decision, using the storage transaction if available."""
        commit = getattr(self.repository, "commit_evaluation", None)
        if callable(commit):
            commit(
                task.task_id,
                candidate,
                decision,
                round_number=round_number,
                current_record_id=current_record.record_id if current_record else None,
                best_record_id=best_record.record_id if best_record else None,
                workflow_state=workflow.state.value,
                runtime_round_number=workflow.round_number,
                extra={
                    "pending_plan": pending_payload,
                    "retest": {"count": retry_count, "same_round": decision.action == AgentAction.RETEST},
                    "failed_actions": sorted(failed_actions or set()),
                    "diagnostic_results": list(diagnostic_results or []),
                    "workflow_phase": workflow.phase.value,
                },
            )
            return
        self.repository.save_task(task)
        self.repository.save_benchmark(task.task_id, candidate, round_number, "candidate")
        self.repository.save_decision(task.task_id, round_number, decision)

    def _update_best(self, task: OptimizationTask, candidate: BenchmarkRecord) -> None:
        best = self.best_records.get(task.task_id)
        if best is None:
            self.best_records[task.task_id] = candidate
            return
        if self._better_record(task, candidate, best).record_id == candidate.record_id:
            self.best_records[task.task_id] = candidate

    @staticmethod
    def _action_key(plan: ExperimentPlan) -> str:
        """Identify a setting/value action independently of its display text."""
        setting = plan.setting_id or plan.setting
        return f"{setting}\x1f{plan.old_value}\x1f{plan.new_value}"

    @staticmethod
    def _better_record(
        task: OptimizationTask,
        candidate: BenchmarkRecord,
        best: BenchmarkRecord,
    ) -> BenchmarkRecord:
        """Return the higher uncapped weighted verified record, with ties."""
        candidate_score = weighted_performance_score(
            candidate.avg_fps,
            candidate.low_fps,
            task.target_avg_fps,
            task.target_low_fps,
            task.mode,
        )
        best_score = weighted_performance_score(
            best.avg_fps,
            best.low_fps,
            task.target_avg_fps,
            task.target_low_fps,
            task.mode,
        )
        if candidate_score > best_score or (
            candidate_score == best_score
            and (candidate.low_fps, candidate.avg_fps) > (best.low_fps, best.avg_fps)
        ):
            return candidate
        return best

    def _candidate_with_accumulated_runs(
        self,
        task_id: str,
        round_number: int,
        candidate: BenchmarkRecord,
    ) -> tuple[BenchmarkRecord, list[dict[str, float]] | None]:
        """Merge actual runs from same-round RETEST submissions.

        ``repeats`` on a manually entered summary is not evidence of separate
        observations. A one-run manual submission is the exception: it is an
        explicit observation and can be accumulated with later one-run
        submissions. When ``runs`` are present, only those finite samples
        count and the summary is recalculated from their medians.
        """
        if not candidate.configuration_confirmed or not candidate.comparable:
            return candidate, None
        fingerprint = self._record_fingerprint(candidate)
        known_fingerprint = self._round_fingerprints.get(task_id, {}).get(round_number)
        if known_fingerprint is not None and known_fingerprint != fingerprint:
            # A different actual configuration is a new observation, never a
            # continuation of the previous RETEST sample set.
            return replace(candidate, configuration_confirmed=False), None
        previous = list(self._round_runs.get(task_id, {}).get(round_number, []))
        submitted_runs = [dict(run) for run in candidate.runs]
        if not submitted_runs:
            if candidate.repeats != 1:
                return candidate, None
            observation: dict[str, float] = {
                "avg_fps": float(candidate.avg_fps),
                "low_fps": float(candidate.low_fps),
            }
            if candidate.gpu_usage is not None:
                observation["gpu_usage"] = float(candidate.gpu_usage)
            submitted_runs = [observation]
        combined = previous + submitted_runs
        avg_values = [float(run["avg_fps"]) for run in combined]
        low_values = [float(run["low_fps"]) for run in combined]
        gpu_values = [float(run["gpu_usage"]) for run in combined if run.get("gpu_usage") is not None]
        updated = replace(
            candidate,
            avg_fps=round(median(avg_values), 2),
            low_fps=round(median(low_values), 2),
            gpu_usage=round(median(gpu_values), 2) if gpu_values else candidate.gpu_usage,
            repeats=len(combined),
            runs=combined,
        )
        return updated, combined

    @staticmethod
    def _record_fingerprint(record: BenchmarkRecord) -> tuple[Any, ...]:
        configuration = record.configuration or {}
        return (
            str(record.resolution),
            str(record.test_scene).strip(),
            tuple(sorted((str(key), str(value)) for key, value in configuration.items())),
        )

    @staticmethod
    def _canonical_setting_value(setting_id: str, value: Any) -> str:
        text = str(value).strip()
        aliases = {
            "graphics_mode": {"独显直连": "dgpu", "混合输出": "hybrid", "自动切换": "advanced_optimus", "不确定": "unknown"},
            "power_mode": {"接通电源 · 性能模式": "ac_performance", "接通电源 · 平衡模式": "ac_balanced", "电池供电": "battery", "不确定": "unknown"},
            "cooling_mode": {"性能 / 狂暴模式": "performance", "平衡模式": "balanced", "安静模式": "quiet", "不确定": "unknown"},
            "dynamic_shadows": {"全部": "all", "仅限日光": "sun_only", "仅太阳": "sun_only", "未确认": "unknown"},
            "display_mode": {"全屏": "fullscreen", "全屏窗口模式": "borderless", "无边框窗口": "borderless", "窗口模式": "windowed", "窗口化": "windowed"},
        }
        return aliases.get(setting_id, {}).get(text, text)

    @classmethod
    def _accept_neutral_platform_change(
        cls,
        candidate: BenchmarkRecord,
        pending: ExperimentPlan | None,
    ) -> bool:
        if pending is None or pending.setting_id != "power_mode":
            return False
        configuration = candidate.configuration or {}
        return (
            candidate.configuration_confirmed
            and candidate.comparable
            and cls._canonical_setting_value("power_mode", configuration.get("power_mode", "")) == "ac_performance"
            and cls._canonical_setting_value("power_mode", pending.new_value) == "ac_performance"
        )

    @staticmethod
    def _mark_configuration_mismatch(
        candidate: BenchmarkRecord,
        pending: ExperimentPlan | None,
    ) -> BenchmarkRecord:
        """Reject a confirmed result whose actual setting differs from plan."""
        if pending is None or not candidate.configuration_confirmed:
            return candidate
        if pending.action in {AgentAction.REQUEST_DATA, AgentAction.FINISH}:
            return candidate
        configuration = candidate.configuration or {}
        expected_id = pending.setting_id
        actual_id = str(configuration.get("planned_setting_id", "")).strip()
        actual_value = str(configuration.get("actual_setting_value", "")).strip()
        if actual_id and expected_id and actual_id != expected_id:
            return replace(candidate, configuration_confirmed=False)
        canonical_expected = GameTuneController._canonical_setting_value(expected_id, pending.new_value)
        if actual_value and pending.new_value and GameTuneController._canonical_setting_value(expected_id, actual_value) != canonical_expected:
            return replace(candidate, configuration_confirmed=False)
        expected_keys = [key for key in (expected_id, pending.setting) if key]
        for key in expected_keys:
            if key in configuration and GameTuneController._canonical_setting_value(expected_id, configuration[key]) != canonical_expected:
                return replace(candidate, configuration_confirmed=False)
        return candidate

    def _persist_runtime(self, task_id: str, workflow: Workflow) -> None:
        current = self.current_records.get(task_id)
        best = self.best_records.get(task_id)
        self.repository.save_runtime_state(
            task_id,
            current_record_id=current.record_id if current else None,
            best_record_id=best.record_id if best else None,
            workflow_state=workflow.state.value,
            round_number=workflow.round_number,
            extra={"workflow_phase": workflow.phase.value},
        )

    def _save_pending_plan(self, task_id: str, plan: ExperimentPlan) -> None:
        saver = getattr(self.repository, "save_pending_plan", None)
        if callable(saver):
            saver(task_id, plan)

    def _clear_pending_plan(self, task_id: str) -> None:
        self.pending_plans.pop(task_id, None)
        remover = getattr(self.repository, "clear_pending_plan", None)
        if callable(remover):
            remover(task_id)

    def pending_plan(self, task_id: str) -> ExperimentPlan | None:
        plan = self.pending_plans.get(task_id)
        if plan is not None:
            return plan
        loader = getattr(self.repository, "pending_plan", None)
        if not callable(loader):
            return None
        payload = loader(task_id)
        if not payload:
            return None
        if isinstance(payload, ExperimentPlan):
            plan = payload
        else:
            values = dict(payload)
            if isinstance(values.get("action"), str):
                values["action"] = AgentAction(values["action"])
            plan = ExperimentPlan(**values)
        self.pending_plans[task_id] = plan
        return plan

    def confirm_rollback(self, task_or_id: OptimizationTask | str, *, confirmed: bool = True) -> WorkflowState:
        """Confirm restoration and unlock the next experiment round.

        Returning ``AWAITING_ROLLBACK`` for ``confirmed=False`` lets a UI keep
        the action pending without changing any records.
        """
        task_id = task_or_id.task_id if isinstance(task_or_id, OptimizationTask) else task_or_id
        workflow = self.workflows.get(task_id)
        if workflow is None:
            raise ValueError("任务尚未初始化")
        if workflow.state != WorkflowState.AWAITING_ROLLBACK:
            raise ValueError(f"当前状态不能确认回滚：{workflow.state.value}")
        if not confirmed:
            return workflow.state
        payload = task_or_id.to_dict() if isinstance(task_or_id, OptimizationTask) else self.repository.task_payload(task_id)
        if not payload:
            raise ValueError("找不到任务数据")
        task = task_or_id if isinstance(task_or_id, OptimizationTask) else OptimizationTask(**payload)
        self._clear_pending_plan(task_id)
        workflow.confirm_rollback(continue_testing=workflow.round_number < task.max_rounds)
        self._retry_counts[task_id] = 0
        self._round_runs.setdefault(task_id, {}).clear()
        self._round_fingerprints.setdefault(task_id, {}).clear()
        self._persist_runtime(task_id, workflow)
        return workflow.state

    def restore_task(self, task_id: str) -> OptimizationTask | None:
        """Restore a persisted task, workflow bookkeeping, and pending plan."""
        payload = self.repository.task_payload(task_id)
        runtime = self.repository.runtime_state(task_id)
        if not payload or not runtime:
            return None
        task = OptimizationTask(**payload)
        baseline = self.repository.benchmark_for_role(task_id, "baseline")
        current = self.repository.benchmark_by_id(task_id, runtime.get("current_record_id")) or baseline
        best = self.repository.benchmark_by_id(task_id, runtime.get("best_record_id")) or baseline
        if baseline is None or current is None or best is None:
            return None
        self.current_records[task_id] = current
        self.best_records[task_id] = best
        metadata = runtime.get("extra", {}) or {}
        try:
            phase = WorkflowPhase(str(metadata.get("workflow_phase", "diagnosis")))
        except ValueError:
            phase = WorkflowPhase.DIAGNOSIS
        self.workflows[task_id] = Workflow(WorkflowState(runtime["workflow_state"]), int(runtime["round_number"]), phase)
        supplement = metadata.get("supplement", {})
        self._supplements[task_id] = dict(supplement) if isinstance(supplement, dict) else {}
        diagnostics = metadata.get("diagnostic_results", [])
        self._diagnostic_results[task_id] = [dict(item) for item in diagnostics if isinstance(item, dict)] if isinstance(diagnostics, list) else []
        retest = metadata.get("retest", {})
        self._retry_counts[task_id] = int(retest.get("count", 0)) if isinstance(retest, dict) else 0
        failed = metadata.get("failed_actions", [])
        self._failed_actions[task_id] = {str(item) for item in failed} if isinstance(failed, list) else set()
        self.last_decisions.pop(task_id, None)
        last_payload = self.repository.last_decision(task_id)
        if last_payload:
            values = dict(last_payload)
            values.pop("id", None)
            values.pop("round_number", None)
            values.pop("created_at", None)
            if isinstance(values.get("action"), str):
                values["action"] = AgentAction(values["action"])
            self.last_decisions[task_id] = Decision(**values)
        self._round_runs[task_id] = {}
        self._round_fingerprints[task_id] = {}
        current_round_entry: dict[str, Any] | None = None
        for entry in self.repository.task_history(task_id):
            if entry.get("role") == "candidate" and int(entry.get("round_number", -1)) == int(runtime["round_number"]):
                # The latest candidate already contains the accumulated list;
                # replaying every historical row would duplicate samples.
                current_round_entry = entry
        if current_round_entry is not None:
            runs = current_round_entry.get("runs") or []
            if isinstance(runs, list):
                self._round_runs[task_id][int(runtime["round_number"])] = [dict(run) for run in runs]
                if runs:
                    self._round_fingerprints[task_id][int(runtime["round_number"])] = (
                        str(current_round_entry.get("resolution", "")),
                        str(current_round_entry.get("test_scene", "")).strip(),
                        tuple(sorted((str(key), str(value)) for key, value in (current_round_entry.get("configuration") or {}).items())),
                    )
        self.pending_plan(task_id)
        pending = self.pending_plans.get(task_id)
        if pending is not None:
            self.workflows[task_id].set_phase(
                pending.purpose, action=pending.action.value, setting_id=pending.setting_id
            )
        return task

    def current_record(self, task_id: str) -> BenchmarkRecord | None:
        return self.current_records.get(task_id)

    def best_record(self, task_id: str) -> BenchmarkRecord | None:
        return self.best_records.get(task_id)

    def diagnostic_results(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._diagnostic_results.get(task_id, [])]

    def workflow_state(self, task_id: str) -> WorkflowState:
        workflow = self.workflows.get(task_id)
        return workflow.state if workflow else WorkflowState.WAITING_INPUT

    def supplement_data(
        self,
        task_or_id: OptimizationTask | str,
        values: dict[str, Any],
    ) -> tuple[list[Evidence], ExperimentPlan]:
        """Persist missing diagnostic inputs and produce the next plan.

        This is the completion path for ``REQUEST_DATA`` plans.  Platform
        fields are applied to an effective task snapshot and saved before the
        next evidence pass; benchmark records remain immutable.
        """
        if not isinstance(values, dict):
            raise TypeError("补充信息必须是对象")
        task_id = task_or_id.task_id if isinstance(task_or_id, OptimizationTask) else task_or_id
        task = task_or_id if isinstance(task_or_id, OptimizationTask) else None
        if task is None:
            payload = self.repository.task_payload(task_id)
            if not payload:
                raise ValueError("找不到任务数据")
            task = OptimizationTask(**payload)
        workflow = self.workflows.get(task_id)
        if workflow is None:
            raise ValueError("任务尚未初始化")
        if workflow.state == WorkflowState.AWAITING_ROLLBACK:
            raise ValueError("请先确认已恢复上一组已验证配置")
        if workflow.state == WorkflowState.COMPLETE:
            raise ValueError("任务已经完成")

        allowed = {
            "fps_cap_known",
            "vsync_known",
            "gpu_power_w",
            "graphics_mode",
            "power_mode",
            "cooling_mode",
            "is_laptop",
            "shadow_quality",
            "dynamic_shadows",
            "cpu_metrics",
            "fps_cap",
            "vsync",
            "user_adjustment",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不支持的补充字段：{', '.join(sorted(unknown))}")
        custom_adjustment: tuple[str, str, str, str] | None = None
        if "user_adjustment" in values:
            parts = tuple(part.strip() for part in str(values["user_adjustment"]).split("|"))
            if len(parts) != 4 or any(not part for part in parts):
                raise ValueError("请按“优化方向 | 设置名称 | 当前值 | 调整后值”填写四项内容")
            if parts[0] not in GENERIC_OPTIMIZATION_DIRECTIONS:
                raise ValueError("请选择列表中的通用优化方向")
            if parts[2] == parts[3]:
                raise ValueError("当前值与调整值相同，无法形成可验证的单项实验")
            custom_adjustment = (parts[0], parts[1], parts[2], parts[3])
        task_fields = {
            "gpu_power_w",
            "fps_cap",
            "vsync",
            "graphics_mode",
            "power_mode",
            "cooling_mode",
            "is_laptop",
        }
        updates: dict[str, Any] = {}
        for field_name in task_fields:
            if field_name in values:
                value = values[field_name]
                if field_name == "gpu_power_w" and value not in (None, ""):
                    try:
                        value = int(value)
                    except (TypeError, ValueError) as error:
                        raise ValueError("GPU 功耗必须是整数瓦数") from error
                if field_name == "fps_cap" and value not in (None, ""):
                    try:
                        value = int(value)
                    except (TypeError, ValueError) as error:
                        raise ValueError("游戏内最高 FPS 必须是 0 到 1000 的整数") from error
                    if not 0 <= value <= 1000:
                        raise ValueError("游戏内最高 FPS 必须是 0 到 1000 的整数")
                if field_name == "vsync":
                    value = {"已禁用": "disabled", "已启用": "enabled", "未确认": "unknown"}.get(str(value), value)
                updates[field_name] = value
        effective_task = replace(task, **updates) if updates else task
        supplement_payload = dict(self._supplements.get(task_id, {}))
        supplement_payload.update(values)
        save_with_runtime = getattr(self.repository, "save_task_with_runtime", None)
        if callable(save_with_runtime):
            save_with_runtime(effective_task, extra={"supplement": supplement_payload})
        else:
            self.repository.save_task(effective_task)
            saver = getattr(self.repository, "save_runtime_metadata", None)
            if callable(saver):
                saver(task_id, supplement=supplement_payload)
        # Keep the caller's task object in sync only after the atomic write;
        # benchmark snapshots remain immutable.
        for field_name, value in updates.items():
            setattr(task, field_name, value)
        self._supplements[task_id] = supplement_payload

        current = self.current_records.get(task_id)
        if current is None:
            current = self.repository.benchmark_for_role(task_id, "baseline")
        if current is None:
            raise ValueError("任务缺少当前基线记录")
        current_for_planning = self._apply_supplements(task_id, current)
        cap_value = (current_for_planning.configuration or {}).get("fps_cap", effective_task.fps_cap)
        vsync_value = (current_for_planning.configuration or {}).get("vsync", effective_task.vsync)
        fps_cap_known = bool(values.get(
            "fps_cap_known",
            supplement_payload.get("fps_cap_known", effective_task.fps_cap is not None or str(cap_value).strip().lower() not in {"", "none", "unknown", "未确认"}),
        ))
        vsync_known = bool(values.get(
            "vsync_known",
            supplement_payload.get("vsync_known", effective_task.vsync != "unknown" or str(vsync_value).strip().lower() not in {"", "none", "unknown", "未确认"}),
        ))
        supplement_payload["fps_cap_known"] = fps_cap_known
        supplement_payload["vsync_known"] = vsync_known
        evidence = analyze_evidence(
            effective_task,
            current_for_planning,
            self.repository.benchmark_for_role(task_id, "baseline"),
            fps_cap_known=fps_cap_known,
            vsync_known=vsync_known,
            diagnostic_results=self._diagnostic_results.get(task_id, []),
        )
        self._add_supplement_evidence(task_id, evidence)
        if custom_adjustment is not None:
            direction, setting, old_value, new_value = custom_adjustment
            plan = ExperimentPlan(
                AgentAction.RECOMMEND_CHANGE,
                f"验证{direction}方向的一项调整",
                setting,
                old_value,
                new_value,
                f"根据诊断结果，由你选择“{direction}”方向中的一项已确认游戏设置。",
                "只检验本轮列出的一个设置；结果只适用于当前设备、游戏版本和测试条件。",
                f"只把“{setting}”从“{old_value}”调整为“{new_value}”，其他设置、场景和采集方式保持不变；先测 1 次筛选，出现改善迹象时再累计确认。",
                "设置执行已确认、前后测试条件一致，并且累计结果达到可靠收益门槛；否则恢复原值。",
                f"将“{setting}”恢复为“{old_value}”。",
                workflow.round_number,
                setting_id="user_defined",
                purpose="optimization",
            )
            validate_plan(task, plan, self.knowledge)
            self.pending_plans[task_id] = plan
            self._save_pending_plan(task_id, plan)
            workflow.set_phase(plan.purpose, action=plan.action.value, setting_id=plan.setting_id)
            self.repository.save_runtime_metadata(task_id, workflow_phase=workflow.phase.value)
            return evidence, plan
        plan = self._local_next_plan(effective_task, current_for_planning, evidence, workflow.round_number)
        validate_plan(effective_task, plan, self.knowledge)
        plan = self._skip_failed_plan(effective_task, plan, workflow.round_number)
        if plan.action == AgentAction.FINISH:
            self._clear_pending_plan(task_id)
            if workflow.state == WorkflowState.AWAITING_TEST:
                workflow.complete_without_test()
                self._persist_runtime(task_id, workflow)
        else:
            self.pending_plans[task_id] = plan
            self._save_pending_plan(task_id, plan)
            workflow.set_phase(plan.purpose, action=plan.action.value, setting_id=plan.setting_id)
            self.repository.save_runtime_metadata(task_id, workflow_phase=workflow.phase.value)
        return evidence, plan

    def _apply_supplements(self, task_id: str, record: BenchmarkRecord) -> BenchmarkRecord:
        """Overlay recorded setting facts when planning from an old record."""
        supplement = self._supplements.get(task_id, {})
        if not supplement:
            return record
        config = dict(record.configuration or {})
        # A supplement fills a previously unknown setting.  Once a later
        # accepted benchmark contains the real setting, that snapshot is
        # authoritative and must not be overwritten by the older supplement.
        if supplement.get("shadow_quality") not in (None, "") and str(config.get("shadow_quality", "")).strip() in {"", "unknown", "未确认"}:
            config["shadow_quality"] = str(supplement["shadow_quality"])
        if supplement.get("dynamic_shadows") not in (None, "") and str(config.get("dynamic_shadows", "")).strip() in {"", "unknown", "未确认"}:
            config["dynamic_shadows"] = str(supplement["dynamic_shadows"])
        if supplement.get("fps_cap") not in (None, "") and str(config.get("fps_cap", "")).strip() in {"", "unknown", "未确认"}:
            config["fps_cap"] = str(supplement["fps_cap"])
        if supplement.get("vsync") not in (None, "") and str(config.get("vsync", "")).strip() in {"", "unknown", "未确认"}:
            config["vsync"] = str(supplement["vsync"])
        return replace(record, configuration=config) if config != (record.configuration or {}) else record

    def plan_after_result(
        self,
        task: OptimizationTask,
        current: BenchmarkRecord,
        baseline: BenchmarkRecord,
        round_number: int,
        *,
        fps_cap_known: bool = True,
        vsync_known: bool = True,
    ) -> tuple[list[Evidence], ExperimentPlan]:
        workflow = self.workflows.get(task.task_id)
        if workflow is None:
            raise ValueError("任务尚未初始化")
        if workflow.state == WorkflowState.AWAITING_ROLLBACK:
            raise ValueError("请先确认已恢复上一组配置")
        if workflow.state == WorkflowState.COMPLETE:
            raise ValueError("任务已经完成")
        if round_number > task.max_rounds:
            raise ValueError("已达到最大实验轮数")

        accepted_current = self.current_records.get(task.task_id) or current
        accepted_current = self._apply_supplements(task.task_id, accepted_current)
        evidence = analyze_evidence(
            task,
            accepted_current,
            baseline,
            fps_cap_known=fps_cap_known,
            vsync_known=vsync_known,
            diagnostic_results=self._diagnostic_results.get(task.task_id, []),
        )
        self._add_supplement_evidence(task.task_id, evidence)
        pending = self.pending_plan(task.task_id)
        last = self.last_decisions.get(task.task_id)
        if pending is not None and (last is None or last.retry_same_experiment):
            # Do not regenerate or renumber a RETEST plan. Its setting/value
            # pair is the experiment identity and must remain stable across
            # UI refreshes and process restore.
            validate_plan(task, pending, self.knowledge)
            return evidence, pending

        plan = self._local_next_plan(task, accepted_current, evidence, round_number)
        validate_plan(task, plan, self.knowledge)
        plan = self._skip_failed_plan(task, plan, round_number)
        if plan.action == AgentAction.FINISH:
            self._clear_pending_plan(task.task_id)
            if workflow.state == WorkflowState.AWAITING_TEST:
                workflow.complete_without_test()
                self._persist_runtime(task.task_id, workflow)
        else:
            self.pending_plans[task.task_id] = plan
            self._save_pending_plan(task.task_id, plan)
            workflow.set_phase(plan.purpose, action=plan.action.value, setting_id=plan.setting_id)
            self.repository.save_runtime_metadata(task.task_id, workflow_phase=workflow.phase.value)
        return evidence, plan

    def _local_next_plan(
        self,
        task: OptimizationTask,
        current: BenchmarkRecord,
        evidence: list[Evidence],
        round_number: int,
    ) -> ExperimentPlan:
        candidates = self.next_step_candidates(task, current, evidence, round_number)
        if candidates:
            return candidates[0]
        return plan_next_experiment(
            task,
            current,
            evidence,
            round_number,
            blocked_setting_ids=self._failed_setting_ids(task.task_id) | self._diagnosed_setting_ids(task.task_id),
        )

    def _add_supplement_evidence(self, task_id: str, evidence: list[Evidence]) -> None:
        note = str(self._supplements.get(task_id, {}).get("cpu_metrics", "")).strip()
        if note:
            evidence.append(Evidence(
                "cpu_metrics_note",
                "玩家补充的 CPU 监控记录",
                f"测试期间 CPU 监控记录：{note}。该文本仅作参考，程序不会据此自动确认 CPU 或温度限制。",
                0.25,
                "insufficient",
            ))

    def next_step_candidates(
        self,
        task: OptimizationTask,
        current: BenchmarkRecord,
        evidence: list[Evidence],
        round_number: int,
    ) -> list[ExperimentPlan]:
        """Return only locally generated and rule-validated model options."""
        blocked = self._failed_setting_ids(task.task_id) | self._diagnosed_setting_ids(task.task_id)
        candidates = plan_candidates(
            task,
            current,
            evidence,
            round_number,
            blocked_setting_ids=blocked,
        )
        try:
            rules = self.knowledge.game_rules(task.game).get("settings", [])
        except ValueError:
            rules = []
        candidates.extend(gpu_optimization_candidates(
            task,
            current,
            evidence,
            round_number,
            rules,
            blocked_setting_ids=blocked,
        ))
        evidence_codes = {item.code for item in evidence}
        if "conditions_changed" in evidence_codes:
            candidates = [plan for plan in candidates if plan.setting_id == "test_conditions"]
        elif "cap_unknown" in evidence_codes:
            candidates = [plan for plan in candidates if plan.setting_id in {"fps_cap", "vsync"}]
        elif task.game == "Counter-Strike 2" and evidence_codes & {"high_gpu", "resolution_response", "diagnostic_resolution_response"}:
            config = current.configuration or {}
            dynamic_shadows = str(config.get("dynamic_shadows", task.dynamic_shadows)).strip()
            shadow_quality = str(config.get("shadow_quality", task.shadow_quality)).strip()
            if dynamic_shadows not in {"all", "sun_only"}:
                candidates = [plan for plan in candidates if plan.setting_id == "dynamic_shadows"]
            elif shadow_quality not in {"低", "中", "高", "非常高"}:
                candidates = [plan for plan in candidates if plan.setting_id == "shadow_quality"]
        if len(candidates) > 1:
            candidates = [plan for plan in candidates if plan.setting_id != "repeats"] or candidates
        valid: list[ExperimentPlan] = []
        seen: set[str] = set()
        for plan in candidates:
            identity = plan_candidate_id(plan)
            if identity in seen:
                continue
            try:
                validate_plan(task, plan, self.knowledge)
            except (ValueError, TypeError):
                continue
            valid.append(plan)
            seen.add(identity)
        return valid

    def choose_next_step(
        self,
        task: OptimizationTask,
        current: BenchmarkRecord,
        evidence: list[Evidence],
        candidates: list[ExperimentPlan],
        history: list[dict] | None = None,
    ) -> NextStepSelection:
        """Select a validated option in a worker thread, falling back locally."""
        selection = choose_next_step(self.model_provider, task, current, evidence, candidates, history=history)
        validate_plan(task, selection.plan, self.knowledge)
        return selection

    def accept_model_plan(
        self,
        task: OptimizationTask,
        expected_plan_id: str,
        selected_plan: ExperimentPlan,
        allowed_candidate_ids: set[str],
    ) -> bool:
        """Atomically replace the displayed local plan if the task is unchanged."""
        workflow = self.workflows.get(task.task_id)
        pending = self.pending_plan(task.task_id)
        if (
            workflow is None
            or workflow.state != WorkflowState.AWAITING_TEST
            or pending is None
            or workflow.round_number != selected_plan.round_number
            or plan_candidate_id(pending) != expected_plan_id
            or plan_candidate_id(selected_plan) not in allowed_candidate_ids
        ):
            return False
        validate_plan(task, selected_plan, self.knowledge)
        if self._action_key(selected_plan) in self._failed_actions.get(task.task_id, set()):
            return False
        self.pending_plans[task.task_id] = selected_plan
        self._save_pending_plan(task.task_id, selected_plan)
        workflow.set_phase(
            selected_plan.purpose, action=selected_plan.action.value, setting_id=selected_plan.setting_id
        )
        self.repository.save_runtime_metadata(task.task_id, workflow_phase=workflow.phase.value)
        return True

    def _skip_failed_plan(
        self,
        task: OptimizationTask,
        plan: ExperimentPlan,
        round_number: int,
    ) -> ExperimentPlan:
        """Avoid immediately proposing a setting that was just reverted."""
        failed = self._failed_actions.get(task.task_id, set())
        if plan.action == AgentAction.FINISH or self._action_key(plan) not in failed:
            return plan
        return ExperimentPlan(
            action=AgentAction.FINISH,
            title="停止重复失败调整",
            setting=plan.setting,
            old_value=plan.old_value,
            new_value=plan.new_value,
            rationale="该设置在本任务中已被实测为退步，跳过同一调整以避免重复失败动作。",
            hypothesis="保留上一组已验证配置，不再重复本轮已失败的设置调整。",
            method="汇总当前已验证配置与历史实验结果。",
            success_criteria="输出已验证配置，不宣称达到性能目标。",
            rollback="无需恢复配置。",
            round_number=round_number,
            setting_id=plan.setting_id,
        )

    def _failed_setting_ids(self, task_id: str) -> set[str]:
        """Return setting identifiers that have already failed in this task."""
        setting_ids: set[str] = set()
        for action_key in self._failed_actions.get(task_id, set()):
            setting_ids.add(str(action_key).split("\x1f", 1)[0])
        return setting_ids

    def _diagnosed_setting_ids(self, task_id: str) -> set[str]:
        """Do not offer a completed diagnostic experiment again in this task."""
        invalid_markers = ("无效", "测试条件不一致", "未确认实际配置", "波动较大", "仍有明显波动")
        return {
            str(item.get("kind", "")).strip()
            for item in self._diagnostic_results.get(task_id, [])
            if isinstance(item, dict)
            and item.get("valid") is not False
            and str(item.get("status", "")).strip().lower() != "invalid"
            and not any(marker in str(item.get("summary", "")) for marker in invalid_markers)
            and str(item.get("kind", "")).strip()
        }

    def explain(
        self,
        task: OptimizationTask,
        decision: Decision,
        *,
        evidence: list[Evidence] | None = None,
        plan: ExperimentPlan | None = None,
    ) -> dict[str, str | list[str]]:
        return explain_decision(self.model_provider, task, decision, evidence=evidence, plan=plan)
