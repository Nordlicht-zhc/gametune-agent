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
) -> list[Evidence]:
    evidence: list[Evidence] = []
    if not current.comparable:
        evidence.append(Evidence("conditions_changed", "测试条件有变化", "这组结果不能和基线直接比较，请按原测试条件重测。", 1.0, "insufficient"))
    if not fps_cap_known or not vsync_known:
        evidence.append(Evidence("cap_unknown", "请先检查限帧", "限帧或垂直同步也会让 GPU 占用偏低。", 0.85, "insufficient"))
    if current.gpu_usage is not None:
        if current.gpu_usage < 75:
            evidence.append(Evidence("low_gpu", "GPU 没有跑满", f"GPU 平均占用 {current.gpu_usage:.0f}%，更可能受 CPU 或限帧影响。", 0.65, "cpu"))
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
            evidence.append(Evidence("laptop_graphics_mode", "检查显卡输出方式", detail, 0.45, "platform" if graphics_mode == "hybrid" else "insufficient"))
        if power_mode not in {"ac_performance"}:
            evidence.append(Evidence("laptop_power_mode", "检查供电模式", "请接通电源，并确认已开启性能模式。", 0.65, "platform"))
        if cooling_mode in {"quiet", "unknown"}:
            evidence.append(Evidence("laptop_cooling", "检查散热模式", "安静模式可能限制持续性能，请确认当前风扇模式。", 0.55, "platform"))
    if baseline and _is_resolution_comparison(baseline, current):
        delta = (current.avg_fps - baseline.avg_fps) / baseline.avg_fps * 100
        if abs(delta) <= 5:
            evidence.append(Evidence("resolution_flat", "降低分辨率效果不大", f"平均 FPS 只变化 {delta:+.1f}%，瓶颈可能不在显卡。", 0.9, "cpu"))
        elif delta >= 10:
            evidence.append(Evidence("resolution_response", "降低分辨率有效", f"平均 FPS 提升 {delta:.1f}%，说明显卡性能可能是主要限制。", 0.8, "gpu"))
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
    for item in evidence:
        if item.supports == "insufficient":
            missing += item.weight
        else:
            scores[item.supports] = scores.get(item.supports, 0.0) + item.weight
    if not scores:
        return "insufficient", 0.0

    causal_scores = {name: score for name, score in scores.items() if name in {"cpu", "gpu", "platform"}}
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
