from __future__ import annotations

from math import ceil, isfinite
from statistics import median
from typing import Iterable


# These weights describe the user's optimization preference.  They are used
# for comparing a candidate with the currently accepted configuration; the
# target-completion score below remains a separate, capped display metric.
MODE_WEIGHTS: dict[str, tuple[float, float]] = {
    "competitive": (0.35, 0.65),
    "balanced": (0.50, 0.50),
    "visual": (0.60, 0.40),
}


def metric_weights(mode: str) -> tuple[float, float]:
    """Return the average-FPS and 1% Low weights for an optimization mode."""
    return MODE_WEIGHTS.get(str(mode).strip(), MODE_WEIGHTS["competitive"])


def weighted_relative_change(avg_delta_pct: float, low_delta_pct: float, mode: str) -> float:
    """Combine raw percentage changes without capping values at the target.

    This is deliberately different from :func:`normalized_score`: a target
    completion score answers "how close are we to the user's target?", while
    this value answers "did the candidate actually improve the accepted
    configuration?".  Keeping those concepts separate prevents an
    above-target FPS gain from disappearing behind a 100-point cap.
    """
    avg_weight, low_weight = metric_weights(mode)
    return avg_weight * float(avg_delta_pct) + low_weight * float(low_delta_pct)


def weighted_performance_score(
    avg_fps: float,
    low_fps: float,
    target_avg: float,
    target_low: float,
    mode: str,
) -> float:
    """Return an uncapped weighted performance index for record ranking.

    Unlike :func:`normalized_score`, this index does not clamp values above
    the target.  It is used only to choose the best verified record after a
    candidate has already passed the evaluator's regression and repeat checks.
    """
    avg_weight, low_weight = metric_weights(mode)
    avg_ratio = float(avg_fps) / float(target_avg)
    low_ratio = float(low_fps) / float(target_low)
    return round((avg_weight * avg_ratio + low_weight * low_ratio) * 100, 4)


def metrics_from_frame_times(frame_times_ms: Iterable[float]) -> dict[str, float | int]:
    """Calculate FPS using the documented slowest-1%-frame definition."""
    values = [float(value) for value in frame_times_ms if isfinite(float(value)) and float(value) > 0]
    if len(values) < 100:
        raise ValueError("逐帧数据至少需要 100 个有效帧时间样本")
    total_ms = sum(values)
    avg_fps = len(values) * 1000.0 / total_ms
    slow_count = max(1, ceil(len(values) * 0.01))
    slowest = sorted(values, reverse=True)[:slow_count]
    low_fps = 1000.0 / (sum(slowest) / len(slowest))
    return {"avg_fps": round(avg_fps, 2), "low_fps": round(low_fps, 2), "frame_count": len(values)}


def median_summary(runs: Iterable[dict[str, float]]) -> dict:
    rows = [dict(row) for row in runs]
    if not rows:
        raise ValueError("至少需要一轮测试数据")
    for row in rows:
        for key in ("avg_fps", "low_fps"):
            value = float(row[key])
            if not isfinite(value) or value <= 0:
                raise ValueError("测试 FPS 必须为有限正数")
            row[key] = value
        if row["low_fps"] > row["avg_fps"] * 1.05:
            raise ValueError("1% Low 不应高于平均 FPS")
        if row.get("gpu_usage") is not None:
            value = float(row["gpu_usage"])
            if not isfinite(value) or not 0 <= value <= 100:
                raise ValueError("GPU 占用率必须在 0 到 100 之间")
            row["gpu_usage"] = value
    result = {
        "avg_fps": round(median(float(row["avg_fps"]) for row in rows), 2),
        "low_fps": round(median(float(row["low_fps"]) for row in rows), 2),
        "run_count": len(rows),
        "runs": rows,
    }
    for key, output in (("avg_fps", "avg_spread_pct"), ("low_fps", "low_spread_pct")):
        samples = [row[key] for row in rows]
        result[output] = round((max(samples) - min(samples)) / median(samples) * 100, 2)
    gpu_values = [float(row["gpu_usage"]) for row in rows if row.get("gpu_usage") is not None]
    if gpu_values:
        result["gpu_usage"] = round(median(gpu_values), 2)
    return result


def normalized_score(avg_fps: float, low_fps: float, target_avg: float, target_low: float, mode: str) -> float:
    avg_weight, low_weight = metric_weights(mode)
    avg_ratio = min(max(avg_fps / target_avg, 0.0), 1.0)
    low_ratio = min(max(low_fps / target_low, 0.0), 1.0)
    return round((avg_weight * avg_ratio + low_weight * low_ratio) * 100, 2)
