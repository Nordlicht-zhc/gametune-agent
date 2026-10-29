"""Victoria 3 controlled experiments. Simulation throughput is independent of FPS."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class SimulationConditions:
    save_sha256: str
    version: str
    mods: str
    date_range: str
    speed: str
    environment: str

    def __post_init__(self):
        if any(not value.strip() for value in asdict(self).values()):
            raise ValueError("请完整记录存档、版本、模组、日期区间、速度档位及测试环境")


def save_fingerprint(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_runs(runs: list[dict]) -> dict:
    if len(runs) < 3:
        raise ValueError("每组至少需要三次独立测试；每次重新载入同一存档")
    rates, fps, lows = [], [], []
    has_fps = [bool(row.get("avg_fps") or row.get("low_fps")) for row in runs]
    if any(has_fps) and not all(has_fps):
        raise ValueError("FPS 可不填写；填写时每次测试都必须同时记录平均 FPS 和 1% Low")
    for row in runs:
        try:
            days, seconds = float(row["game_days"]), float(row["elapsed_seconds"])
            if not all(isfinite(v) and v > 0 for v in (days, seconds)):
                raise ValueError
            rates.append(days / seconds)
            if any(has_fps):
                avg, low = float(row["avg_fps"]), float(row["low_fps"])
                if not all(isfinite(v) and v > 0 for v in (avg, low)) or low > avg * 1.05:
                    raise ValueError
                fps.append(avg)
                lows.append(low)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("游戏天数、耗时和可选 FPS 必须是有限正数，且 1% Low 不应高于平均 FPS") from error
    if len({float(row["game_days"]) for row in runs}) != 1:
        raise ValueError("同一组测试必须覆盖相同游戏天数")
    centre = median(rates)
    result = {"days_per_second": centre, "spread_pct": (max(rates) - min(rates)) / centre * 100,
              "repeats": len(runs), "game_days": float(runs[0]["game_days"])}
    if fps:
        result.update(avg_fps=median(fps), low_fps=median(lows))
        result["fps_spread_pct"] = max((max(v) - min(v)) / median(v) * 100 for v in (fps, lows))
    return result


def compare_runs(baseline: dict, candidate: dict, confirmed: bool) -> dict:
    before, after = summarize_runs(baseline["runs"]), summarize_runs(candidate["runs"])
    if baseline["conditions"] != candidate["conditions"] or before["game_days"] != after["game_days"]:
        raise ValueError("前后测试条件不同：必须固定存档、版本、模组、日期区间、速度和环境")
    delta = (after["days_per_second"] / before["days_per_second"] - 1) * 100
    result = {"simulation_delta_pct": delta, "baseline": before, "candidate": after}
    if not confirmed:
        action, reason = "retest", "尚未确认实际执行调整，不能接受候选配置"
    elif max(before["spread_pct"], after["spread_pct"], before.get("fps_spread_pct", 0), after.get("fps_spread_pct", 0)) > 5:
        action, reason = "retest", "组内波动超过 5%，请检查条件后重新采集"
    elif ("avg_fps" in before) != ("avg_fps" in after):
        action, reason = "retest", "前后 FPS 记录不完整，请补齐或均不填写"
    elif "avg_fps" in before and any(after[key] / before[key] <= .95 for key in ("avg_fps", "low_fps")):
        action, reason = "revert", "画面性能下降至少 5%，建议恢复基线配置"
    elif delta <= -5:
        action, reason = "revert", "模拟推进速度下降至少 5%，建议恢复基线配置"
    elif delta >= 5:
        action, reason = "keep", "模拟推进速度提升至少 5%，可保留本轮设置"
    else:
        action, reason = "retest", "变化不足 5%，暂不能确认收益"
    result.update(action=action, reason=reason)
    return result


def import_simulation_csv(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    summarize_runs(rows)
    return rows


def experiment_guidance(summary: dict) -> str:
    if summary["spread_pct"] > 5:
        return "先排除测试波动：每次重新载入同一存档，固定预热时间，记录自动存档触发情况。"
    return ("下一轮建议：只降低一级同宽高比分辨率，其他设置保持一致，重新测试模拟速度和可选 FPS。"
            "若 FPS 提高而游戏天/秒基本不变，仅说明画面渲染改善；不能据此断言 CPU 瓶颈。"
            "跑日速度未改善时，先核对后台负载、供电和散热记录，再设计单变量实验。")


class SimulationRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS victoria3_experiments (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")

    def save(self, payload: dict) -> int:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute("INSERT INTO victoria3_experiments(created_at,payload) VALUES (?,?)",
                                        (datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)))
            return cursor.lastrowid

    def history(self) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as connection:
            return [{"id": row[0], "created_at": row[1], **json.loads(row[2])} for row in
                    connection.execute("SELECT id,created_at,payload FROM victoria3_experiments ORDER BY id DESC")]
