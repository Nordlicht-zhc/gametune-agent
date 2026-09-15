from __future__ import annotations

import csv
from math import isfinite
from pathlib import Path

from .calculate_metrics import median_summary, metrics_from_frame_times


FRAME_TIME_ALIASES = ("msbetweenpresents", "frametime", "frame_time_ms", "frame time (ms)")
AVG_ALIASES = ("avg_fps", "average_fps", "average fps", "avgfps")
LOW_ALIASES = ("1%_low", "1% low", "low_fps", "one_percent_low")
# GPU busy durations are not utilization percentages and require separate units.
GPU_ALIASES = ("gpu_usage", "gpu utilization", "gpu usage")


def _normalized(row: dict[str, str]) -> dict[str, str]:
    return {str(key).strip().lower(): str(value).strip() if value is not None else "" for key, value in row.items() if key is not None}


def _find(row: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        if alias in row and row[alias] != "":
            return row[alias]
    return None


def import_benchmark_csv(path: str | Path) -> dict:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ValueError(f"CSV 文件不存在：{csv_path}")
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = [_normalized(row) for row in csv.DictReader(stream)]
    except (OSError, UnicodeError, csv.Error) as error:
        raise ValueError("无法读取 CSV，请检查文件访问权限和 UTF-8 编码") from error
    if not rows:
        raise ValueError("CSV 中没有数据行")
    for field in ("processid", "process_id", "application"):
        identities = {row[field] for row in rows if row.get(field)}
        if len(identities) > 1:
            raise ValueError("CSV 包含多个进程，请先筛选同一个游戏进程再导入")

    frame_times: list[float] = []
    is_frames = any(alias in rows[0] for alias in FRAME_TIME_ALIASES)
    if is_frames:
        for row_number, row in enumerate(rows, 2):
            frame_times.append(_number(_find(row, FRAME_TIME_ALIASES), row_number, "帧时间"))
        result = metrics_from_frame_times(frame_times)
        gpu = _numeric_column(rows, GPU_ALIASES)
        if gpu:
            result["gpu_usage"] = round(sum(gpu) / len(gpu), 2)
        result["format"] = "frame_times"
        result["duration_seconds"] = sum(frame_times) / 1000
        result["run_count"] = 1
        return result

    runs: list[dict[str, float]] = []
    for row_number, row in enumerate(rows, 2):
        avg, low = _find(row, AVG_ALIASES), _find(row, LOW_ALIASES)
        if avg is None or low is None:
            raise ValueError(f"CSV 第 {row_number} 行缺少 avg_fps 或 1%_low")
        run = {"avg_fps": _number(avg, row_number, "平均 FPS"), "low_fps": _number(low, row_number, "1% Low")}
        gpu = _find(row, GPU_ALIASES)
        if gpu is not None:
            run["gpu_usage"] = _number(gpu, row_number, "GPU 占用率", percentage=True)
        runs.append(run)
    if not runs:
        raise ValueError("未找到逐帧时间列，也未找到 avg_fps 与 1%_low 汇总列")
    result = median_summary(runs)
    result["format"] = "summary_runs"
    return result


def _number(value: str | None, row_number: int, label: str, *, percentage: bool = False) -> float:
    try:
        parsed = float(value) if value is not None else float("nan")
    except ValueError as error:
        raise ValueError(f"CSV 第 {row_number} 行的{label}不是有效数字") from error
    if not isfinite(parsed) or (not 0 <= parsed <= 100 if percentage else parsed <= 0):
        raise ValueError(f"CSV 第 {row_number} 行的{label}超出有效范围")
    return parsed


def _numeric_column(rows: list[dict[str, str]], aliases: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for row_number, row in enumerate(rows, 2):
        value = _find(row, aliases)
        if value is not None:
            values.append(_number(value, row_number, "GPU 占用率", percentage=True))
    return values
