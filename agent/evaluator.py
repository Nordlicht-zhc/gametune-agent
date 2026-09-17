from __future__ import annotations

from statistics import median

from models.schemas import AgentAction, BenchmarkRecord, Decision, OptimizationTask
from tools.calculate_metrics import normalized_score


_DISPERSION_LIMIT_PCT = 5.0


def _run_dispersion(record: BenchmarkRecord) -> tuple[int, float | None, float | None]:
    """Return run count and spread percentages for an evidence-backed record.

    ``BenchmarkRecord.runs`` is optional. A manually entered summary has no
    individual observations, so it can still be evaluated by ``repeats``
    without inventing a dispersion value.
    """
    if not record.runs:
        return record.repeats, None, None
    avg_values = [float(run["avg_fps"]) for run in record.runs]
    low_values = [float(run["low_fps"]) for run in record.runs]

    def spread(values: list[float]) -> float:
        centre = median(values)
        return (max(values) - min(values)) / centre * 100 if centre else float("inf")

    return len(record.runs), spread(avg_values), spread(low_values)


def _decision(
    action: AgentAction,
    summary: str,
    *,
    avg_delta: float = 0.0,
    low_delta: float = 0.0,
    score_delta: float = 0.0,
    reasons: list[str] | None = None,
    accepted: bool = False,
    task_complete: bool = False,
    stop_reason: str = "",
    retry_same_experiment: bool = False,
    rollback_required: bool = False,
) -> Decision:
    return Decision(
        action=action,
        summary=summary,
        avg_delta_pct=round(avg_delta, 2),
        low_delta_pct=round(low_delta, 2),
        score_delta=round(score_delta, 2),
        needs_retest=action == AgentAction.RETEST,
        reasons=reasons or [],
        accepted=accepted,
        task_complete=task_complete,
        stop_reason=stop_reason,
        retry_same_experiment=retry_same_experiment,
        rollback_required=rollback_required,
    )


def evaluate_result(
    task: OptimizationTask,
    baseline: BenchmarkRecord,
    candidate: BenchmarkRecord,
    *,
    round_number: int,
    minimum_change_pct: float = 5.0,
    reference: BenchmarkRecord | None = None,
    minimum_repeats: int = 3,
    retry_count: int = 0,
    max_retries: int = 2,
    accept_neutral_change: bool = False,
) -> Decision:
    """Evaluate one candidate against the currently accepted configuration.

    The action describes only this candidate (keep, revert, or retest). Task
    completion and the reason for stopping are explicit fields, so reaching a
    round cap cannot accidentally turn an unverified candidate into the best
    configuration.
    """
    if round_number < 1:
        raise ValueError("实验轮次必须大于 0")
    if minimum_repeats < 1:
        raise ValueError("最少重复次数必须大于 0")
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise ValueError("复测次数必须是非负整数")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("复测上限必须是非负整数")

    def bounded_retest(summary: str, reasons: list[str]) -> Decision:
        """Return a RETEST while it is useful, then stop safely.

        A RETEST deliberately does not advance the optimization round.  The
        controller supplies the number of retries already submitted for the
        same plan, so a noisy or unchanged experiment cannot keep a task in
        ``AWAITING_TEST`` forever.
        """
        if retry_count >= max_retries:
            return _decision(
                AgentAction.RETEST,
                f"{summary} 已达到本轮复测上限，保留最后一组已验证配置并停止。",
                reasons=reasons + ["同一方案复测次数已达到上限。"],
                task_complete=True,
                stop_reason="retry_limit",
                retry_same_experiment=False,
            )
        return _decision(
            AgentAction.RETEST,
            summary,
            reasons=reasons,
            retry_same_experiment=True,
        )

    if not candidate.configuration_confirmed:
        return bounded_retest(
            "还没有确认本轮设置已经执行，因此暂不评估结果。",
            ["请先确认已经完成调整，并填写实际使用的配置。"],
        )
    if not candidate.comparable or candidate.test_scene.strip() != baseline.test_scene.strip():
        return bounded_retest(
            "测试条件与基线不同，当前结果不能直接比较。",
            ["请使用与基线相同的场景、路线、供电模式和采集方法重新测试。"],
        )

    actual_runs, avg_spread, low_spread = _run_dispersion(candidate)
    if actual_runs < minimum_repeats:
        return bounded_retest(
            f"本轮只有 {actual_runs} 次有效测试，至少需要 {minimum_repeats} 次。",
            ["请保持配置和测试条件不变，再补充测试。"],
        )
    if avg_spread is not None and low_spread is not None and (
        avg_spread > _DISPERSION_LIMIT_PCT or low_spread > _DISPERSION_LIMIT_PCT
    ):
        return bounded_retest(
            "同一配置的测试结果相差超过 5%，暂时无法确认是否有效。",
            [
                f"平均 FPS 波动 {avg_spread:.1f}%",
                f"1% Low 波动 {low_spread:.1f}%",
                "请保持测试条件一致，再测试几次。",
            ],
        )

    comparison = reference or baseline
    avg_delta = (candidate.avg_fps - comparison.avg_fps) / comparison.avg_fps * 100
    low_delta = (candidate.low_fps - comparison.low_fps) / comparison.low_fps * 100
    before = normalized_score(comparison.avg_fps, comparison.low_fps, task.target_avg_fps, task.target_low_fps, task.mode)
    after = normalized_score(candidate.avg_fps, candidate.low_fps, task.target_avg_fps, task.target_low_fps, task.mode)
    score_delta = after - before
    target_met = candidate.avg_fps >= task.target_avg_fps and candidate.low_fps >= task.target_low_fps

    if target_met:
        # FINISH is retained for callers using the historical action enum;
        # explicit fields make acceptance and completion unambiguous.
        return _decision(
            AgentAction.FINISH,
            "平均 FPS 和 1% Low 都已达到目标，可以结束优化。",
            avg_delta=avg_delta,
            low_delta=low_delta,
            score_delta=score_delta,
            accepted=True,
            task_complete=True,
            stop_reason="target_reached",
            reasons=[
                f"平均 FPS 变化 {avg_delta:+.1f}%",
                f"1% Low 变化 {low_delta:+.1f}%",
                f"综合得分变化 {score_delta:+.1f}",
            ],
        )

    if avg_delta <= -minimum_change_pct or low_delta <= -minimum_change_pct:
        action = AgentAction.REVERT_CHANGE
        summary = "本轮设置使性能明显下降，建议恢复上一组配置。"
        accepted = False
        rollback_required = True
    elif max(abs(avg_delta), abs(low_delta)) < minimum_change_pct and accept_neutral_change:
        action = AgentAction.KEEP_CHANGE
        summary = "本轮性能变化处于正常波动范围，保留已确认的平台配置并继续分析。"
        accepted = True
        rollback_required = False
    elif max(abs(avg_delta), abs(low_delta)) < minimum_change_pct:
        action = AgentAction.RETEST
        summary = "变化不够明显，需要保持相同设置再测一次。"
        accepted = False
        rollback_required = False
    elif score_delta > 0:
        action = AgentAction.KEEP_CHANGE
        summary = "整体表现有所提高，建议保留本轮设置并继续优化。"
        accepted = True
        rollback_required = False
    else:
        action = AgentAction.REVERT_CHANGE
        summary = "整体表现没有改善，建议恢复上一组配置。"
        accepted = False
        rollback_required = True

    reasons = [
        f"平均 FPS 变化 {avg_delta:+.1f}%",
        f"1% Low 变化 {low_delta:+.1f}%",
        f"综合得分变化 {score_delta:+.1f}",
    ]
    if avg_spread is not None:
        reasons.extend([f"平均 FPS 波动 {avg_spread:.1f}%", f"1% Low 波动 {low_spread:.1f}%"])

    # A round cap ends the task but never changes a RETEST into acceptance.
    # A useful KEEP may be accepted as the final verified result; a regression
    # still requires rollback confirmation before the task is frozen.
    task_complete = round_number >= task.max_rounds
    stop_reason = "max_rounds" if task_complete else ""
    if task_complete:
        if action == AgentAction.RETEST:
            summary = "已达到实验轮数上限。当前结果仍不明确，保留上一组已验证配置。"
        elif action == AgentAction.KEEP_CHANGE:
            summary = "已达到实验轮数上限。本轮确有改善，保留设置并结束任务。"
        elif action == AgentAction.REVERT_CHANGE:
            summary = "已达到实验轮数上限。请恢复上一组配置，然后结束任务。"

    return _decision(
        action,
        summary,
        avg_delta=avg_delta,
        low_delta=low_delta,
        score_delta=score_delta,
        reasons=reasons,
        accepted=accepted,
        task_complete=task_complete,
        stop_reason=stop_reason,
        retry_same_experiment=action == AgentAction.RETEST and not task_complete,
        rollback_required=rollback_required,
    )
