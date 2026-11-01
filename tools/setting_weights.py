"""Observed impact weights, separately normalized for external and local data."""
from __future__ import annotations

from pathlib import Path
from tools.calculate_metrics import weighted_relative_change
from tools.configuration import CONFIGURATION_LABELS, GRAPHICS_SETTINGS, canonical_value
from tools.history_impacts import historical_impacts
from tools.performance_knowledge import load_performance_knowledge


def _normalize(rows: list[dict]) -> list[dict]:
    total = sum(row["impact_score"] for row in rows)
    for row in rows:
        row["weight_pct"] = round(row["impact_score"] / total * 100, 2) if total else 0.0
    return sorted(rows, key=lambda row: row["weight_pct"], reverse=True)


def setting_weights(game: str, mode: str, history: list[dict], *, directory=None) -> dict:
    directory = directory or Path(__file__).resolve().parents[1] / "knowledge"
    data = load_performance_knowledge(directory, game)
    # Do not mix benchmark platforms. Each source is its own normalized group.
    groups = {}
    for item in data.get("measurements", []):
        levels = item["levels"]
        avg = [level["avg_fps"] for level in levels]
        low = [level["low_fps"] for level in levels]
        avg_span = (max(avg) - min(avg)) / max(avg) * 100
        low_span = (max(low) - min(low)) / max(low) * 100
        row = {"setting_id": item["setting_id"], "label": CONFIGURATION_LABELS.get(item["setting_id"], item["setting_id"]),
               "source_id": item["source_id"], "avg_span_pct": round(avg_span, 4),
               "low_span_pct": round(low_span, 4), "impact_score": weighted_relative_change(avg_span, low_span, mode),
               "levels": levels, "status": "external_reference"}
        groups.setdefault(item["source_id"], []).append(row)
    external = []
    for rows in groups.values():
        external.extend(_normalize(rows))
    local = {}
    for item in historical_impacts(history, mode)["single_setting_effects"]:
        key = item["changed_settings"][0]
        score = weighted_relative_change(abs(item["avg_delta_pct"]), abs(item["low_delta_pct"]), mode)
        if key not in local or score > local[key]["impact_score"]:
            local[key] = {"setting_id": key, "label": item["label"], "impact_score": score,
                          "record_id": item["record_id"], "status": item["status"],
                          "avg_delta_pct": item["avg_delta_pct"], "low_delta_pct": item["low_delta_pct"]}
    local_rows = _normalize(list(local.values()))
    covered = {row["setting_id"] for row in external + local_rows}
    settings = GRAPHICS_SETTINGS | {"fps_cap", "vsync", "graphics_mode", "power_mode", "cooling_mode", "gpu_power_w"}
    return {"mode": mode, "external_weights": external, "local_weights": local_rows,
            "unmeasured": [{"setting_id": key, "label": CONFIGURATION_LABELS.get(key, key), "weight_pct": None}
                           for key in sorted(settings - covered)],
            "sources": [source for source in data.get("sources", []) if source["id"] in groups],
            "method": "平均 FPS 跨度与低帧跨度按当前模式加权，再在同一来源的已测设置中归一为 100%。本机使用各项最大已观察变化幅度单独归一。",
            "limits": ["权重表示相对影响程度，不是本机预期提升百分比，也不是组合中的贡献占比。",
                       "本机与各外部测试来源分别归一，不混用、不相加；无数据记为未测，不记零权重。",
                       "外部档位间的低帧变化也可能包含测量波动；单一平台结果仅供优先级参考。"]}


def candidate_reference_weight(weights: dict, plan) -> float:
    """A weight can break ties only for an explicitly measured improving step."""
    for row in weights.get("external_weights", []):
        if row["setting_id"] != plan.setting_id:
            continue
        levels = {canonical_value(plan.setting_id, item["value"]): item for item in row["levels"]}
        before = levels.get(canonical_value(plan.setting_id, plan.old_value))
        after = levels.get(canonical_value(plan.setting_id, plan.new_value))
        if before and after:
            delta = weighted_relative_change((after["avg_fps"] / before["avg_fps"] - 1) * 100,
                                             (after["low_fps"] / before["low_fps"] - 1) * 100, weights["mode"])
            if delta > 0:
                return row["weight_pct"]
    return 0.0
