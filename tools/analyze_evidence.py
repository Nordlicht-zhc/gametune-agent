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
        evidence.append(Evidence("conditions_changed", "测试条件不一致", "当前结果不能直接与基线比较。", 1.0, "insufficient"))
    if not fps_cap_known or not vsync_known:
        evidence.append(Evidence("cap_unknown", "限帧状态待确认", "低 GPU 占用也可能由帧率上限或垂直同步造成。", 0.85, "insufficient"))
    if current.gpu_usage is not None:
        if current.gpu_usage < 75:
            evidence.append(Evidence("low_gpu", "GPU 占用偏低", f"平均 GPU 占用为 {current.gpu_usage:.0f}%，更偏向 CPU 侧或人为限帧。", 0.65, "cpu"))
        elif current.gpu_usage >= 95:
            evidence.append(Evidence("high_gpu", "GPU 接近满载", f"平均 GPU 占用为 {current.gpu_usage:.0f}%，支持 GPU 侧限制。", 0.8, "gpu"))
    if current.low_fps < task.target_low_fps:
        ratio = current.low_fps / task.target_low_fps
        evidence.append(Evidence("low_target_miss", "1% Low 未达标", f"当前为目标的 {ratio:.0%}。", min(1.0, 1.0 - ratio + 0.45), "frametime"))
    if task.is_laptop:
        if task.gpu_power_w is None:
            evidence.append(Evidence("laptop_power_unknown", "GPU 功耗信息缺失", "同型号 Laptop GPU 的功耗配置可能不同，建议补充监控到的持续 GPU 功耗。", 0.75, "insufficient"))
        if task.graphics_mode in {"hybrid", "unknown"}:
            detail = "当前使用混合输出，显示链路可能影响高帧率场景。" if task.graphics_mode == "hybrid" else "尚未确认独显直连或混合输出模式。"
            evidence.append(Evidence("laptop_graphics_mode", "笔记本输出模式", detail, 0.45, "platform" if task.graphics_mode == "hybrid" else "insufficient"))
        if task.power_mode not in {"ac_performance"}:
            evidence.append(Evidence("laptop_power_mode", "供电模式可能限速", "笔记本未确认处于接通电源的性能模式。", 0.65, "platform"))
        if task.cooling_mode in {"quiet", "unknown"}:
            evidence.append(Evidence("laptop_cooling", "散热模式需要确认", "安静模式或未知风扇策略可能压低持续功耗。", 0.55, "platform"))
    if baseline and _is_resolution_comparison(baseline, current):
        delta = (current.avg_fps - baseline.avg_fps) / baseline.avg_fps * 100
        if abs(delta) <= 5:
            evidence.append(Evidence("resolution_flat", "降低分辨率收益有限", f"平均 FPS 仅变化 {delta:+.1f}%，进一步支持非 GPU 侧限制。", 0.9, "cpu"))
        elif delta >= 10:
            evidence.append(Evidence("resolution_response", "分辨率响应明显", f"降低分辨率后平均 FPS 提升 {delta:.1f}%，支持 GPU 侧限制。", 0.8, "gpu"))
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
    scores: dict[str, float] = {}
    for item in evidence:
        if item.supports != "insufficient":
            scores[item.supports] = scores.get(item.supports, 0.0) + item.weight
    if not scores:
        return "insufficient", 0.0
    name, score = max(scores.items(), key=lambda pair: pair[1])
    confidence = score / max(sum(scores.values()), 1.0)
    return name, round(min(confidence, 0.95), 2)
