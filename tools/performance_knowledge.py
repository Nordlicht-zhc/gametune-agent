from __future__ import annotations

import json
from math import isfinite
from pathlib import Path

from models.schemas import BenchmarkRecord, ExperimentPlan, OptimizationTask
from tools.configuration import CONFIGURATION_LABELS, canonical_value


def load_performance_knowledge(directory: str | Path, game: str) -> dict:
    """Optional external evidence must not interrupt the offline workflow."""
    if game != "Counter-Strike 2":
        return {}
    try:
        data = json.loads((Path(directory) / "cs2_performance.json").read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or data.get("game") != game:
            return {}
        if not isinstance(data.get("reviewed_on"), str) or not isinstance(data.get("usage_rules"), list):
            return {}
        if not isinstance(data.get("sources"), list) or not isinstance(data.get("measurements"), list):
            return {}
        sources = {item["id"] for item in data["sources"]}
        for item in data["measurements"]:
            if item["source_id"] not in sources or not item["setting_id"] or len(item["levels"]) < 2:
                return {}
            values = set()
            for level in item["levels"]:
                if level["value"] in values:
                    return {}
                values.add(level["value"])
                for key in ("avg_fps", "low_fps"):
                    value = level[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                        return {}
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def performance_context(
    task: OptimizationTask, current: BenchmarkRecord, candidates: list[ExperimentPlan],
    history: list[dict], *, directory: str | Path | None = None,
) -> dict:
    directory = directory or Path(__file__).resolve().parents[1] / "knowledge"
    data = load_performance_knowledge(directory, task.game)
    if not data:
        return {"available": False, "reason": "没有可用的外部性能参考；继续依据本机证据规划。"}
    relevant = {plan.setting_id for plan in candidates}
    changes = []
    if history:
        latest = history[-1]
        before = latest.get("before_configuration", {})
        after = latest.get("after_configuration", {})
        for key in before.keys() & after.keys():
            if key not in CONFIGURATION_LABELS:
                continue
            if canonical_value(key, before[key]) != canonical_value(key, after[key]):
                relevant.add(key)
                changes.append({"setting_id": key, "old_value": before[key], "new_value": after[key]})
    references = []
    for item in data["measurements"]:
        key = item["setting_id"]
        if key not in relevant:
            continue
        levels = {canonical_value(key, level["value"]): level for level in item["levels"]}
        comparisons = []
        requested = [{"old_value": plan.old_value, "new_value": plan.new_value, "origin": "candidate"}
                     for plan in candidates if plan.setting_id == key]
        requested.extend({**change, "origin": "user_recorded_change"} for change in changes if change["setting_id"] == key)
        for change in requested:
            old = levels.get(canonical_value(key, change["old_value"]))
            new = levels.get(canonical_value(key, change["new_value"]))
            comparison = {**change, "exact_transition_available": old is not None and new is not None}
            if old and new:
                comparison.update({
                    "source_avg_delta_pct": round((new["avg_fps"] / old["avg_fps"] - 1) * 100, 2),
                    "source_low_delta_pct": round((new["low_fps"] / old["low_fps"] - 1) * 100, 2),
                })
            comparisons.append(comparison)
        references.append({**item, "current_value": current.configuration.get(key), "comparisons": comparisons})
    source_ids = {item["source_id"] for item in references}
    return {
        "available": bool(references), "evidence_type": "external_reference_not_local_measurement",
        "reviewed_on": data["reviewed_on"], "usage_rules": data["usage_rules"],
        "sources": [item for item in data["sources"] if item["id"] in source_ids],
        "setting_references": references,
        "uncovered_changes": [change for change in changes if change["setting_id"] not in {item["setting_id"] for item in references}],
    }
