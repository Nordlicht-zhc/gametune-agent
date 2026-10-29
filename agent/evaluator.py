from __future__ import annotations

from math import isfinite
from statistics import median

from models.schemas import AgentAction, BenchmarkRecord, Decision, OptimizationTask
from tools.calculate_metrics import normalized_score, weighted_relative_change
from tools.resolution import parse_resolution
from tools.configuration import CONFIGURATION_LABELS, changed_graphics, unexpected_configuration_changes


_DISPERSION_LIMIT_PCT = 5.0
_MEANINGFUL_WEIGHTED_DELTA_PCT = 1.0

# Lower values mean a visibly lower-quality choice.  These are ordinal
# safeguards for visual mode, not an attempt to pretend that different
# graphics settings have a universal numerical visual-quality score.
_QUALITY_LEVELS: dict[str, dict[str, int]] = {
    "shadow_quality": {"低": 0, "low": 0, "中": 1, "medium": 1, "高": 2, "high": 2, "非常高": 3, "very_high": 3},
    "texture_detail": {"低": 0, "low": 0, "中": 1, "medium": 1, "高": 2, "high": 2},
    "shader_detail": {"低": 0, "low": 0, "高": 1, "high": 1},
    "particle_detail": {"低": 0, "low": 0, "中": 1, "medium": 1, "高": 2, "high": 2, "非常高": 3, "very_high": 3},
    "texture_filtering": {
        "双线性": 0, "bilinear": 0, "三线性": 1, "trilinear": 1,
        "异向 2x": 2, "anisotropic_2x": 2, "异向 4x": 3, "anisotropic_4x": 3,
        "异向 8x": 4, "anisotropic_8x": 4, "异向 16x": 5, "anisotropic_16x": 5,
    },
    "msaa_mode": {"无": 0, "none": 0, "cmaa2 抗锯齿": 1, "cmaa2": 1, "2x msaa": 2, "2x_msaa": 2, "4x msaa": 3, "4x_msaa": 3, "8x msaa": 4, "8x_msaa": 4},
    "ambient_occlusion": {"已禁用": 0, "disabled": 0, "中": 1, "medium": 1, "高": 2, "high": 2},
    "hdr": {"性能": 0, "performance": 0, "品质": 1, "quality": 1},
    "fsr": {"性能": 0, "performance": 0, "均衡": 1, "balanced": 1, "品质": 2, "quality": 2, "超高品质": 3, "ultra_quality": 3, "已禁用（最高品质）": 4, "disabled": 4},
    "player_contrast": {"已禁用": 0, "disabled": 0, "已启用": 1, "enabled": 1},
}

_QUALITY_LABELS = {
    "shadow_quality": "全局阴影效果",
    "texture_detail": "模型/贴图细节",
    "shader_detail": "光影细节",
    "particle_detail": "粒子细节",
    "texture_filtering": "贴图过滤模式",
    "msaa_mode": "抗锯齿模式",
    "ambient_occlusion": "环境光遮蔽",
    "hdr": "高动态范围",
    "fsr": "Fidelity FX 超级分辨率",
    "player_contrast": "增强角色对比度",
}


def _quality_level(setting: str, value: object) -> int | None:
    text = str(value).strip().lower()
    return _QUALITY_LEVELS.get(setting, {}).get(text)


def _quality_regressions(previous: BenchmarkRecord, candidate: BenchmarkRecord) -> list[str]:
    """Return quality settings that were lowered relative to *previous*.

    A missing setting is treated as unknown rather than as a quality change.
    This keeps the guard conservative for visual mode without rejecting old
    persisted records that predate the advanced-video fields.
    """
    regressions: list[str] = []
    try:
        previous_width, previous_height = parse_resolution(previous.resolution)
        candidate_width, candidate_height = parse_resolution(candidate.resolution)
    except ValueError:
        previous_width = previous_height = candidate_width = candidate_height = 0
    if previous_width and candidate_width and candidate_width * candidate_height < previous_width * previous_height:
        regressions.append("分辨率")

    previous_config = previous.configuration or {}
    candidate_config = candidate.configuration or {}
    for setting, label in _QUALITY_LABELS.items():
        if setting not in previous_config or setting not in candidate_config:
            continue
        before = _quality_level(setting, previous_config[setting])
        after = _quality_level(setting, candidate_config[setting])
        if before is not None and after is not None and after < before:
            regressions.append(label)
    return regressions


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
    weighted_delta: float = 0.0,
    reasons: list[str] | None = None,
    accepted: bool = False,
    task_complete: bool = False,
    stop_reason: str = "",
    retry_same_experiment: bool = False,
    rollback_required: bool = False,
    diagnostic_summary: str = "",
    measurement_valid: bool | None = None,
) -> Decision:
    return Decision(
        action=action,
        summary=summary,
        avg_delta_pct=round(avg_delta, 2),
        low_delta_pct=round(low_delta, 2),
        score_delta=round(score_delta, 2),
        weighted_delta_pct=round(weighted_delta, 2),
        needs_retest=action == AgentAction.RETEST,
        reasons=reasons or [],
        accepted=accepted,
        task_complete=task_complete,
        stop_reason=stop_reason,
        retry_same_experiment=retry_same_experiment,
        rollback_required=rollback_required,
        diagnostic_summary=diagnostic_summary,
        measurement_valid=measurement_valid,
    )


def evaluate_result(
    task: OptimizationTask,
    baseline: BenchmarkRecord,
    candidate: BenchmarkRecord,
    *,
    round_number: int,
    minimum_change_pct: float = 5.0,
    minimum_weighted_change_pct: float = _MEANINGFUL_WEIGHTED_DELTA_PCT,
    reference: BenchmarkRecord | None = None,
    minimum_repeats: int = 3,
    acceptance_repeats: int | None = None,
    retry_count: int = 0,
    max_retries: int = 2,
    accept_neutral_change: bool = False,
    setting_id: str = "",
    graphics_profile: bool = False,
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
    if acceptance_repeats is None:
        acceptance_repeats = minimum_repeats
    if isinstance(acceptance_repeats, bool) or not isinstance(acceptance_repeats, int) or acceptance_repeats < minimum_repeats:
        raise ValueError("确认结果所需的累计测试次数不能少于最少测试次数")
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise ValueError("复测次数必须是非负整数")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("复测上限必须是非负整数")
    if not isfinite(float(minimum_change_pct)) or minimum_change_pct < 0:
        raise ValueError("明显变化阈值必须是非负有限数")
    if not isfinite(float(minimum_weighted_change_pct)) or minimum_weighted_change_pct < 0:
        raise ValueError("加权变化阈值必须是非负有限数")

    adaptive_screening = acceptance_repeats > minimum_repeats

    def bounded_retest(
        summary: str,
        reasons: list[str],
        *,
        measurement_valid: bool = True,
    ) -> Decision:
        """Return a RETEST while it is useful, then stop safely.

        A RETEST deliberately does not advance the optimization round.  The
        controller supplies the number of retries already submitted for the
        same plan, so a noisy or unchanged experiment cannot keep a task in
        ``AWAITING_TEST`` forever.
        """
        if retry_count >= max_retries:
            return _decision(
                AgentAction.REVERT_CHANGE,
                f"{summary} 已达到本轮复测上限，请恢复上一组配置。",
                reasons=reasons + ["当前方案未确认可靠收益；复测上限与开始新方案的轮数上限分开判断。"],
                task_complete=round_number >= task.max_rounds,
                stop_reason="max_rounds" if round_number >= task.max_rounds else "retry_limit",
                rollback_required=True,
                measurement_valid=measurement_valid,
            )
        return _decision(
            AgentAction.RETEST,
            summary,
            reasons=reasons,
            retry_same_experiment=True,
            measurement_valid=measurement_valid,
        )

    if not candidate.configuration_confirmed:
        return bounded_retest(
            "还没有确认本轮设置已经执行，因此暂不评估结果。",
            ["请先确认已经完成调整，并填写实际使用的配置。"],
            measurement_valid=False,
        )
    comparison = reference or baseline
    unexpected = unexpected_configuration_changes(comparison, candidate, setting_id or str(candidate.configuration.get("planned_setting_id", "")))
    if graphics_profile:
        actual_changes = set(changed_graphics(comparison, candidate))
        unexpected = [key for key in unexpected if key not in actual_changes]
    if unexpected:
        return bounded_retest(
            "计划之外的配置发生变化或缺少记录，暂不把收益归因于本项调整。",
            [f"需要核对的设置：{'、'.join(CONFIGURATION_LABELS.get(key, key) for key in unexpected)}", "请恢复比较基准的其他设置；如果要保留这些变化，请重新建立比较基准。"],
            measurement_valid=False,
        )
    if not candidate.comparable or not comparison.comparable or not comparison.configuration_confirmed or candidate.test_scene.strip() != comparison.test_scene.strip():
        return bounded_retest(
            "测试条件与基线不同，当前结果不能直接比较。",
            ["请使用与基线相同的场景、路线、供电模式和采集方法重新测试。"],
            measurement_valid=False,
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
            measurement_valid=False,
        )

    comparison = reference or baseline
    avg_delta = (candidate.avg_fps - comparison.avg_fps) / comparison.avg_fps * 100
    low_delta = (candidate.low_fps - comparison.low_fps) / comparison.low_fps * 100
    before = normalized_score(comparison.avg_fps, comparison.low_fps, task.target_avg_fps, task.target_low_fps, task.mode)
    after = normalized_score(candidate.avg_fps, candidate.low_fps, task.target_avg_fps, task.target_low_fps, task.mode)
    score_delta = after - before
    weighted_delta = weighted_relative_change(avg_delta, low_delta, task.mode)
    target_met = candidate.avg_fps >= task.target_avg_fps and candidate.low_fps >= task.target_low_fps

    # Visual mode is quality-first.  It does not turn visual quality into a
    # made-up numeric weight; it simply refuses to call a lower-quality
    # configuration an improvement unless the user adds an explicit quality
    # policy later.
    quality_regressions = _quality_regressions(comparison, candidate) if task.mode == "visual" else []
    if quality_regressions:
        return _decision(
            AgentAction.REVERT_CHANGE,
            "视觉模式下本轮降低了画质，不能只凭 FPS 提升接受，建议恢复上一组配置。",
            avg_delta=avg_delta,
            low_delta=low_delta,
            score_delta=score_delta,
            weighted_delta=weighted_delta,
            reasons=[
                f"画质下降：{'、'.join(quality_regressions)}",
                f"平均 FPS 变化 {avg_delta:+.1f}%",
                f"1% Low 变化 {low_delta:+.1f}%",
                f"按视觉模式加权的实际变化 {weighted_delta:+.1f}%",
            ],
            accepted=False,
            task_complete=round_number >= task.max_rounds,
            stop_reason="max_rounds" if round_number >= task.max_rounds else "",
            rollback_required=True,
        )

    # Check unacceptable regression before target completion.  Otherwise a
    # candidate could lower one metric substantially, barely meet both target
    # values, and be accepted only because the target score is capped at 100.
    if avg_delta <= -minimum_change_pct or low_delta <= -minimum_change_pct:
        action = AgentAction.REVERT_CHANGE
        summary = "本轮设置使性能明显下降，建议恢复上一组配置。"
        accepted = False
        rollback_required = True
    elif adaptive_screening and actual_runs < acceptance_repeats:
        # One run is a cheap screen.  It can reject an obviously bad change,
        # but a promising or neutral change must accumulate enough comparable
        # runs before it becomes the accepted configuration.
        return bounded_retest(
            f"本次筛选未发现明显退步，已完成 {actual_runs} 次；累计达到 {acceptance_repeats} 次后再确认结果。",
            [
                f"平均 FPS 变化 {avg_delta:+.1f}%",
                f"1% Low 变化 {low_delta:+.1f}%",
                "先保留当前设置继续补测，明显下降时会立即回滚。",
            ],
        )
    elif target_met:
        # FINISH is retained for callers using the historical action enum;
        # explicit fields make acceptance and completion unambiguous.
        return _decision(
            AgentAction.FINISH,
            "平均 FPS 和 1% Low 都已达到目标，可以结束优化。",
            avg_delta=avg_delta,
            low_delta=low_delta,
            score_delta=score_delta,
            weighted_delta=weighted_delta,
            accepted=True,
            task_complete=True,
            stop_reason="target_reached",
            measurement_valid=True,
            reasons=[
                f"平均 FPS 变化 {avg_delta:+.1f}%",
                f"1% Low 变化 {low_delta:+.1f}%",
                f"按模式加权的实际变化 {weighted_delta:+.1f}%",
                f"综合得分变化 {score_delta:+.1f}",
            ],
        )
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
    elif weighted_delta >= minimum_weighted_change_pct:
        action = AgentAction.KEEP_CHANGE
        summary = "整体表现有所提高，建议保留本轮设置并继续优化。"
        accepted = True
        rollback_required = False
    elif weighted_delta <= -minimum_weighted_change_pct:
        action = AgentAction.REVERT_CHANGE
        summary = "按当前优化模式加权后整体表现没有改善，建议恢复上一组配置。"
        accepted = False
        rollback_required = True
    else:
        action = AgentAction.RETEST
        summary = "加权后的变化太小，暂时无法确认是否值得保留，需要再测一次。"
        accepted = False
        rollback_required = False

    # Once evidence-backed runs have reached the verification target, a
    # neutral result should not start an unbounded fourth/fifth run.  Manual
    # summaries remain on the historical path until their retry limit, because
    # ``repeats`` alone does not prove that individual runs were observed.
    if action == AgentAction.RETEST and (
        (adaptive_screening and candidate.runs and actual_runs >= acceptance_repeats)
        or retry_count >= max_retries
    ):
        action = AgentAction.REVERT_CHANGE
        summary = "累计测试仍未显示可靠收益，建议恢复上一组配置并尝试下一项设置。"
        accepted = False
        rollback_required = True

    reasons = [
        f"平均 FPS 变化 {avg_delta:+.1f}%",
        f"1% Low 变化 {low_delta:+.1f}%",
        f"按模式加权的实际变化 {weighted_delta:+.1f}%",
        f"综合得分变化 {score_delta:+.1f}",
    ]
    if avg_spread is not None:
        reasons.extend([f"平均 FPS 波动 {avg_spread:.1f}%", f"1% Low 波动 {low_spread:.1f}%"])

    # A round cap ends the task but never changes a RETEST into acceptance.
    # A useful KEEP may be accepted as the final verified result; a regression
    # still requires rollback confirmation before the task is frozen.
    task_complete = round_number >= task.max_rounds and action != AgentAction.RETEST
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
        weighted_delta=weighted_delta,
        reasons=reasons,
        accepted=accepted,
        task_complete=task_complete,
        stop_reason=stop_reason,
        retry_same_experiment=action == AgentAction.RETEST and not task_complete,
        rollback_required=rollback_required,
        measurement_valid=True,
    )


def evaluate_diagnostic_result(
    task: OptimizationTask,
    candidate: BenchmarkRecord,
    *,
    round_number: int,
    reference: BenchmarkRecord,
    setting_id: str = "",
    retry_count: int = 0,
    max_retries: int = 1,
) -> Decision:
    """Judge whether a diagnostic run clarified a cause, then request restore.

    Diagnostic changes are never accepted as performance improvements. A
    borderline result may be repeated once; a usable result is recorded as
    evidence and the workflow asks the player to restore the prior settings.
    """
    if round_number < 1:
        raise ValueError("实验轮次必须大于 0")
    if not candidate.configuration_confirmed:
        if retry_count < max_retries:
            return _decision(
                AgentAction.RETEST,
                "诊断设置还没有确认，请核对实际配置后重新提交。",
                reasons=["诊断测试必须确认实际改动，才能判断结果。"],
                measurement_valid=False,
                retry_same_experiment=True,
            )
        return _diagnostic_restore(
            round_number,
            "诊断结果无效",
            "本轮没有确认实际配置，未据此判断瓶颈。",
            0.0,
            0.0,
            valid=False,
        )
    unexpected = unexpected_configuration_changes(reference, candidate, setting_id)
    if (not candidate.comparable or not reference.comparable or not reference.configuration_confirmed
            or unexpected or candidate.test_scene.strip() != reference.test_scene.strip()):
        if retry_count < max_retries:
            return _decision(
                AgentAction.RETEST,
                "诊断测试条件与原设置不一致，请按相同场景和条件重测。",
                reasons=[f"计划之外的配置变化或缺少记录：{'、'.join(CONFIGURATION_LABELS.get(key, key) for key in unexpected)}" if unexpected else "测试条件与比较基准不一致。", "请恢复其他设置或重新建立比较基准，暂不把收益归因于本项调整。"],
                measurement_valid=False,
                retry_same_experiment=True,
            )
        return _diagnostic_restore(
            round_number,
            "诊断结果无效",
            "测试条件不一致，未据此判断瓶颈。",
            0.0,
            0.0,
            valid=False,
        )

    actual_runs, avg_spread, low_spread = _run_dispersion(candidate)
    if setting_id == "frame_limits" and actual_runs < 2:
        if retry_count < max_retries:
            return _decision(AgentAction.RETEST, "不限帧参考至少需要两次有效测试，请保持条件不变补测。",
                reasons=["后续画面调整需要独立、稳定的不限帧比较基准。"], retry_same_experiment=True)
        return _diagnostic_restore(round_number, "诊断结果无效", "不限帧参考测试次数不足，未用于生成优化候选。", 0.0, 0.0, valid=False)
    if actual_runs < 1 or (
        avg_spread is not None
        and low_spread is not None
        and (avg_spread > _DISPERSION_LIMIT_PCT or low_spread > _DISPERSION_LIMIT_PCT)
    ):
        if retry_count < max_retries:
            return _decision(
                AgentAction.RETEST,
                "诊断测试波动较大，再测一次有助于排除偶然变化。",
                reasons=[
                    f"平均 FPS 波动 {avg_spread:.1f}%" if avg_spread is not None else "缺少逐次测试数据",
                    f"1% Low 波动 {low_spread:.1f}%" if low_spread is not None else "缺少逐次测试数据",
                ],
                retry_same_experiment=True,
            )
        summary = "诊断结果仍有明显波动，暂时无法区分主要限制。"
        return _diagnostic_restore(round_number, summary, summary, 0.0, 0.0, valid=False)

    avg_delta = (candidate.avg_fps - reference.avg_fps) / reference.avg_fps * 100
    low_delta = (candidate.low_fps - reference.low_fps) / reference.low_fps * 100
    if setting_id == "resolution" and 5.0 < abs(avg_delta) < 10.0 and retry_count < max_retries:
        return _decision(
            AgentAction.RETEST,
            "分辨率对比的变化还不够明确，再测一次后判断原因。",
            avg_delta=avg_delta,
            low_delta=low_delta,
            reasons=["当前变化处于模糊区间，再测一次可以降低偶然波动的影响。"],
            retry_same_experiment=True,
        )

    if setting_id in {"fps_cap", "vsync", "frame_limits"}:
        if avg_delta >= 5 or low_delta >= 5:
            summary = f"临时排除限制后平均 FPS {avg_delta:+.1f}%，1% Low {low_delta:+.1f}%；原限制可能影响表现，不能据此判断硬件瓶颈。"
        else:
            summary = f"临时排除限制后平均 FPS {avg_delta:+.1f}%，1% Low {low_delta:+.1f}%；尚未证明所有限制都已排除。"
    elif setting_id == "resolution":
        if avg_delta >= 10.0:
            summary = f"降低分辨率后平均 FPS 提高 {avg_delta:.1f}%，更支持 GPU 渲染负担是重要限制。"
        elif abs(avg_delta) <= 5.0:
            summary = f"降低分辨率后平均 FPS 变化 {avg_delta:+.1f}%，GPU 渲染负担可能不是主要限制；这还不能单独确认 CPU 瓶颈。"
        else:
            summary = f"降低分辨率后的平均 FPS 变化为 {avg_delta:+.1f}%，结果仍不明确，暂不锁定瓶颈方向。"
    elif max(abs(avg_delta), abs(low_delta)) >= 5.0:
        setting_names = {
            "power_mode": "供电 / 性能模式",
            "cooling_mode": "散热模式",
            "graphics_mode": "显卡输出方式",
        }
        name = setting_names.get(setting_id, "平台设置")
        summary = f"调整{name}后帧率发生明显变化（平均 FPS {avg_delta:+.1f}%，1% Low {low_delta:+.1f}%），说明它可能影响当前表现。"
    else:
        setting_names = {
            "power_mode": "供电 / 性能模式",
            "cooling_mode": "散热模式",
            "graphics_mode": "显卡输出方式",
        }
        name = setting_names.get(setting_id, "平台设置")
        summary = f"对比{name}后帧率变化较小（平均 FPS {avg_delta:+.1f}%，1% Low {low_delta:+.1f}%），暂未发现明显影响。"
    if abs(low_delta) >= 10.0 and (low_delta > 0) != (avg_delta > 0):
        summary = f"平均 FPS 与 1% Low 变化方向不一致（{avg_delta:+.1f}% / {low_delta:+.1f}%），暂不锁定瓶颈方向。"
    return _diagnostic_restore(round_number, "诊断测试完成", summary, avg_delta, low_delta)


def _diagnostic_restore(
    round_number: int,
    title: str,
    finding: str,
    avg_delta: float,
    low_delta: float,
    *,
    valid: bool = True,
) -> Decision:
    return _decision(
        AgentAction.REVERT_CHANGE,
        f"{title}。请恢复原配置后继续。",
        avg_delta=avg_delta,
        low_delta=low_delta,
        reasons=[finding, "本轮只用于判断原因，不作为长期优化配置。"],
        rollback_required=True,
        diagnostic_summary=finding,
        measurement_valid=valid,
        task_complete=False,
    )
