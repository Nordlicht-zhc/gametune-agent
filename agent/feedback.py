from __future__ import annotations

from typing import Any

from models.schemas import AgentAction


def summarize_feedback(history: list[dict]) -> dict[str, Any]:
    """Summarize program decisions, never reinterpret invalid measurements as gains."""
    attempts = []
    unresolved = []
    for item in history:
        if not isinstance(item, dict):
            continue
        decision = item.get("decision") or {}
        valid = item.get("valid") is True
        diagnostic = item.get("purpose") == "diagnostic"
        action = decision.get("action", "")
        if decision.get("stop_reason") == "player_profile" and not decision.get("accepted"):
            outcome = "adopted_unverified"
        elif not valid:
            outcome = "invalid"
        elif decision.get("retry_same_experiment") or action == AgentAction.RETEST.value:
            outcome = "needs_retest"
        elif diagnostic:
            outcome = "diagnostic_only"
        elif decision.get("accepted"):
            outcome = "accepted"
        elif action == AgentAction.REVERT_CHANGE.value:
            outcome = "no_verified_benefit"
        else:
            outcome = "unconfirmed"
        setting_id = str(item.get("setting_id", ""))
        rollback_required = bool(decision.get("rollback_required"))
        restored = bool(item.get("restoration_confirmed"))
        attempt = {
            "record_id": item.get("record_id", ""),
            "round_number": item.get("round_number"),
            "setting_id": setting_id,
            "purpose": item.get("purpose", "optimization"),
            "changed_settings": list(item.get("changed_settings") or []),
            "reference_record_id": item.get("reference_record_id", ""),
            "outcome": outcome,
            "valid": valid,
            "avg_delta_pct": item.get("avg_delta_pct") if valid else None,
            "low_delta_pct": item.get("low_delta_pct") if valid else None,
            "accepted": bool(decision.get("accepted")),
            "rollback_status": "restored" if restored else ("pending" if rollback_required else "not_required"),
            "conclusion": str(decision.get("summary", "")),
            "reasons": list(decision.get("reasons") or []),
        }
        attempts.append(attempt)
    # Same-round submissions describe one experiment. Only its latest result
    # remains unresolved; successful later repeats resolve earlier bad samples.
    latest = {}
    for attempt in attempts:
        latest[(attempt["round_number"], attempt["setting_id"])] = attempt
    for attempt in latest.values():
        if attempt["rollback_status"] == "pending":
            unresolved.append({"setting_id": attempt["setting_id"], "issue": "awaiting_restoration", "detail": "先确认已恢复配置，再开启新实验。"})
        elif attempt["outcome"] in {"invalid", "needs_retest", "unconfirmed"}:
            unresolved.append({"setting_id": attempt["setting_id"], "issue": attempt["outcome"], "detail": attempt["conclusion"]})
    return {
        "attempts": attempts,
        "unresolved": unresolved,
        "avoid_repeating": [item for item in latest.values() if item["outcome"] == "no_verified_benefit"],
        "rules": [
            "无效测量不代表该调整没有收益；先处理配置、可比性或波动问题。",
            "诊断结果只提供线索，恢复后仍需正式验证。",
            "同一条件下已验证无收益的动作应跳过；补测沿用原方案。",
        ],
    }
