from __future__ import annotations

from models.schemas import BenchmarkRecord, Evidence, OptimizationTask
from tools.resolution import parse_resolution


def analyze_evidence(
    task: OptimizationTask,
    current: BenchmarkRecord,
    baseline: BenchmarkRecord | None = None,
    *,
    fps_cap_known: bool = False,
    vsync_known: bool = False,
    diagnostic_results: list[dict] | None = None,
) -> list[Evidence]:
    evidence: list[Evidence] = []
    completed_diagnostics = {
        str(result.get("kind", "")).strip()
        for result in diagnostic_results or []
        if isinstance(result, dict) and result.get("valid") is not False
    }
    if not current.comparable:
        evidence.append(Evidence("conditions_changed", "测试条件有变化", "这组结果不能和基线直接比较，请按原测试条件重测。", 1.0, "insufficient"))
    if not fps_cap_known or not vsync_known:
        evidence.append(Evidence("cap_unknown", "请先检查限帧", "限帧或垂直同步也会让 GPU 占用偏低。", 0.85, "insufficient"))
    if current.gpu_usage is not None:
        if current.gpu_usage < 75:
            cause = "cpu_or_other" if fps_cap_known and vsync_known else "insufficient"
            detail = (
                f"GPU 平均占用 {current.gpu_usage:.0f}%；限帧和垂直同步已确认，但仍需更多证据区分 CPU 侧与其他限制。"
                if cause == "cpu_or_other"
                else f"GPU 平均占用 {current.gpu_usage:.0f}%；单凭这一项无法判断原因，限帧或垂直同步等干扰尚未完全排除。"
            )
            evidence.append(Evidence("low_gpu", "GPU 占用偏低", detail, 0.5, cause))
        elif current.gpu_usage >= 95:
            evidence.append(Evidence("high_gpu", "GPU 接近满载", f"GPU 平均占用 {current.gpu_usage:.0f}%，更可能受显卡性能限制。", 0.8, "gpu"))
    if current.low_fps < task.target_low_fps:
        ratio = current.low_fps / task.target_low_fps
        evidence.append(Evidence("low_target_miss", "1% Low 未达标", f"当前只达到目标的 {ratio:.0%}，画面稳定性仍需改善。", min(1.0, 1.0 - ratio + 0.45), "frametime"))
    if task.is_laptop:
        configuration = current.configuration or {}
        graphics_mode = str(configuration.get("graphics_mode", task.graphics_mode))
        power_mode = str(configuration.get("power_mode", task.power_mode))
        cooling_mode = str(configuration.get("cooling_mode", task.cooling_mode))
        if task.gpu_power_w is None:
            evidence.append(Evidence("laptop_power_unknown", "缺少 GPU 功耗", "同型号笔记本显卡可能有不同功耗，请补充测试时的 GPU 功耗。", 0.75, "insufficient"))
        if graphics_mode in {"hybrid", "unknown"}:
            detail = "当前为混合输出，高帧率游戏可能因此损失部分性能。" if graphics_mode == "hybrid" else "请确认当前是独显直连还是混合输出。"
            evidence.append(Evidence("laptop_graphics_mode", "显卡输出方式", detail, 0.4, "platform" if graphics_mode == "hybrid" else "insufficient"))
        if power_mode not in {"ac_performance"} and "power_mode" not in completed_diagnostics:
            known_mode = power_mode not in {"unknown", ""}
            detail = "已记录的供电或性能模式可能限制持续性能。" if known_mode else "测试时的供电与运行模式尚未确认。"
            evidence.append(Evidence("laptop_power_mode", "供电与性能模式", detail, 0.55 if known_mode else 0.4, "platform" if known_mode else "insufficient"))
        if cooling_mode in {"quiet", "unknown"} and "cooling_mode" not in completed_diagnostics:
            known_mode = cooling_mode == "quiet"
            detail = "已记录为安静模式；是否影响性能仍需结合温度和频率数据。" if known_mode else "测试时的风扇或散热模式尚未确认。"
            evidence.append(Evidence("laptop_cooling", "散热模式", detail, 0.45 if known_mode else 0.35, "platform" if known_mode else "insufficient"))
    if baseline and _is_resolution_comparison(baseline, current):
        delta = (current.avg_fps - baseline.avg_fps) / baseline.avg_fps * 100
        if abs(delta) <= 5:
            evidence.append(Evidence("resolution_flat", "降低分辨率效果不大", f"平均 FPS 只变化 {delta:+.1f}%，瓶颈可能不在显卡。", 0.9, "cpu"))
        elif delta >= 10:
            evidence.append(Evidence("resolution_response", "降低分辨率有效", f"平均 FPS 提升 {delta:.1f}%，说明显卡性能可能是主要限制。", 0.8, "gpu"))
    for result in diagnostic_results or []:
        if not isinstance(result, dict):
            continue
        summary = str(result.get("summary", "")).strip()
        status = str(result.get("status", "")).strip().lower()
        invalid_markers = ("无效", "测试条件不一致", "未确认实际配置", "波动较大", "仍有明显波动")
        if result.get("valid") is False or status == "invalid" or any(marker in summary for marker in invalid_markers):
            # A failed measurement may be retained in history for transparency,
            # but it must never influence the next hypothesis or model prompt.
            continue
        kind = str(result.get("kind", ""))
        if kind != "resolution":
            supports = str(result.get("supports", "insufficient"))
            if not summary:
                continue
            if supports not in {"cpu", "cpu_or_other", "gpu", "platform", "insufficient"}:
                supports = "insufficient"
            evidence.append(Evidence(
                f"diagnostic_{kind or 'experiment'}",
                "已完成一项诊断实验",
                summary,
                0.65 if supports != "insufficient" else 0.4,
                supports,
            ))
            continue
        delta = result.get("avg_delta_pct")
        if not isinstance(delta, (int, float)):
            continue
        supports = str(result.get("supports", ""))
        if supports == "insufficient":
            evidence.append(Evidence(
                "diagnostic_resolution_uncertain",
                "分辨率对比证据不足",
                summary or "这次分辨率对比没有得到足够可靠的数据，不能据此判断 CPU 或 GPU 方向。",
                0.4,
                "insufficient",
            ))
            continue
        if delta >= 10:
            evidence.append(Evidence(
                "diagnostic_resolution_response",
                "分辨率对比更支持 GPU 方向",
                f"临时降低分辨率后，平均 FPS 提高 {delta:.1f}%；这支持优先检查 GPU 渲染负担，但仍需用优化测试确认收益。",
                0.8,
                "gpu",
            ))
        elif abs(delta) <= 5:
            evidence.append(Evidence(
                "diagnostic_resolution_flat",
                "分辨率对比没有明显变化",
                f"临时降低分辨率后，平均 FPS 变化 {delta:+.1f}%；GPU 渲染负担可能不是当前主要限制，仍不能据此单独确认 CPU 瓶颈。",
                0.65,
                "cpu_or_other",
            ))
        else:
            evidence.append(Evidence(
                "diagnostic_resolution_uncertain",
                "分辨率对比结果不明确",
                f"临时降低分辨率后，平均 FPS 变化 {delta:+.1f}%；结果处于不明确区间，当前不宜给出确定瓶颈结论。",
                0.55,
                "insufficient",
            ))
    return evidence


def _is_resolution_comparison(baseline: BenchmarkRecord, current: BenchmarkRecord) -> bool:
    if not baseline.comparable or not current.comparable or not current.configuration_confirmed:
        return False
    if baseline.test_scene.strip() != current.test_scene.strip():
        return False
    try:
        before_w, before_h = parse_resolution(baseline.resolution)
        after_w, after_h = parse_resolution(current.resolution)
    except ValueError:
        return False
    if after_w * after_h >= before_w * before_h or abs(before_w / before_h - after_w / after_h) > 0.025:
        return False
    # Other recorded settings must remain identical for causal attribution.
    ignored = {"resolution", "planned_setting", "planned_value"}
    keys = (set(baseline.configuration) | set(current.configuration)) - ignored
    return all(baseline.configuration.get(key) == current.configuration.get(key) for key in keys)


def dominant_hypothesis(evidence: list[Evidence]) -> tuple[str, float]:
    """Return the leading cause and its evidence support strength.

    ``frametime`` describes a visible symptom, while ``cpu``, ``gpu`` and
    ``platform`` describe possible causes.  A symptom should not defeat a
    measured cause merely because the target FPS was set unusually high.  If
    a cause is available, the symptom is therefore kept out of the cause
    ranking; it is still shown as an evidence tag in the UI.

    Missing or unconfirmed inputs are included in the denominator.  The
    result is a relative support strength, not a calibrated probability.
    """
    scores: dict[str, float] = {}
    missing = 0.0
    cpu_or_other = 0.0
    for item in evidence:
        if item.supports == "insufficient":
            missing += item.weight
        elif item.supports == "cpu_or_other":
            cpu_or_other += item.weight
        else:
            scores[item.supports] = scores.get(item.supports, 0.0) + item.weight
    if not scores and cpu_or_other <= 0:
        return "insufficient", 0.0

    causal_scores = {name: score for name, score in scores.items() if name in {"cpu", "gpu", "platform"}}
    if not causal_scores:
        strength = cpu_or_other / max(cpu_or_other + missing, 1.0)
        return "cpu_or_other", round(min(strength, 0.55), 2)
    # A finding that points away from GPU rendering is not itself proof of a
    # CPU cause, so it reduces the strength of any competing cause instead of
    # being ranked as a positive CPU diagnosis.
    missing += cpu_or_other
    ranked = causal_scores or scores
    name, score = max(ranked.items(), key=lambda pair: pair[1])
    confidence = score / max(sum(ranked.values()) + missing, 1.0)
    return name, round(min(confidence, 0.95), 2)


def evidence_support_level(evidence: list[Evidence]) -> str:
    """Map relative support strength to player-facing language."""
    hypothesis, confidence = dominant_hypothesis(evidence)
    if hypothesis == "insufficient" or confidence < 0.45:
        return "不足"
    if confidence < 0.65:
        return "弱"
    if confidence < 0.80:
        return "中"
    return "强"
