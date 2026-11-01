"""Deterministic observed effects from task-local database experiment history."""
from __future__ import annotations

from math import isfinite
from tools.configuration import CONFIGURATION_LABELS, canonical_value
from tools.calculate_metrics import weighted_relative_change


def historical_impacts(history: list[dict], mode: str = "competitive") -> dict:
    latest = {}
    excluded = 0
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("valid") is not True or item.get("configuration_confirmed") is False or item.get("comparable") is False:
            excluded += 1
            continue
        key = (item.get("round_number"), item.get("reference_record_id"), item.get("setting_id"))
        latest[key] = item
    singles, bundles = [], []
    for item in latest.values():
        if (item.get("valid") is not True or item.get("configuration_confirmed") is False
                or item.get("comparable") is False
                or (item.get("test_scene") and item.get("reference_test_scene")
                    and item["test_scene"] != item["reference_test_scene"])):
            excluded += 1
            continue
        before, after = item.get("before_configuration") or {}, item.get("after_configuration") or {}
        keys = (set(before) | set(after)) & CONFIGURATION_LABELS.keys()
        unknown = {"", "unknown", "未确认", "不确定", "none"}
        missing = [key for key in keys if canonical_value(key, before.get(key, "unknown")).lower() not in unknown
                   and canonical_value(key, after.get(key, "unknown")).lower() in unknown]
        changes = [key for key in keys if canonical_value(key, before.get(key, "unknown")).lower() not in unknown
                   and canonical_value(key, after.get(key, "unknown")).lower() not in unknown
                   and canonical_value(key, before[key]) != canonical_value(key, after[key])]
        if missing or not changes or any(key in changes for key in {"cpu", "gpu", "test_scene"}):
            excluded += 1
            continue
        values = [item.get(key) for key in ("reference_avg_fps", "reference_low_fps", "avg_fps", "low_fps")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0
               for value in values):
            excluded += 1
            continue
        avg = (values[2] / values[0] - 1) * 100
        low = (values[3] / values[1] - 1) * 100
        entry = {
            "record_id": item.get("record_id", ""), "reference_record_id": item.get("reference_record_id", ""),
            "changed_settings": sorted(changes),
            "label": " + ".join(CONFIGURATION_LABELS[key] for key in sorted(changes)),
            "avg_delta_pct": round(avg, 2), "low_delta_pct": round(low, 2),
            "weighted_delta_pct": round(weighted_relative_change(avg, low, mode), 2),
            "repeats": item.get("repeats", 1),
            "status": "verified" if (item.get("decision") or {}).get("accepted") else "observed_not_verified",
            "reference_configuration": {key: value for key, value in before.items() if key in CONFIGURATION_LABELS},
            "result_configuration": {key: value for key, value in after.items() if key in CONFIGURATION_LABELS},
        }
        (singles if len(changes) == 1 else bundles).append(entry)
    singles.sort(key=lambda row: abs(row["weighted_delta_pct"]), reverse=True)
    bundles.sort(key=lambda row: abs(row["weighted_delta_pct"]), reverse=True)
    return {"single_setting_effects": singles, "profile_effects": bundles,
            "excluded_records": excluded,
            "rules": ["排序表示已观察到的变化幅度，下降也可能是影响最大的记录。",
                      "未确认收益的有效测量可以提供方向，不等于无效，也不等于已经验证。",
                      "组合变化只能评价整组，不能拆分成单项贡献；只适用于所列比较条件。"]}


def historical_lead(summary: dict) -> str:
    rows = summary.get("single_setting_effects") or summary.get("profile_effects") or []
    if not rows:
        return ""
    top = rows[0]
    kind = "单项改动" if len(top["changed_settings"]) == 1 else "整组改动"
    certainty = "已验证" if top["status"] == "verified" else "待验证"
    return (f"历史{kind}中，{top['label']}的已观察变化幅度最大：平均 FPS {top['avg_delta_pct']:+.1f}%，"
            f"1% Low {top['low_delta_pct']:+.1f}%（{certainty}）。")
