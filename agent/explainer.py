from __future__ import annotations

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelAPIError, ModelProvider
from tools.history_impacts import historical_lead
from .prompts import decision_explanation_request, final_report_request, parse_explanation, _next_step_guard


def explain_decision(
    provider: ModelProvider | None,
    task: OptimizationTask,
    decision: Decision,
    *,
    evidence: list[Evidence] | None = None,
    plan: ExperimentPlan | None = None,
    workflow: dict | None = None,
) -> dict[str, str | list[str]]:
    """Use a model when configured; always provide a deterministic fallback."""
    fallback: dict[str, str | list[str]] = {
        "summary": decision.summary,
        "reasoning": decision.reasons[:2],
        "next_step": _next_step_guard(decision, plan),
        "warning": "",
        "source": "local",
    }
    if workflow and workflow.get("rollback_confirmed"):
        fallback["next_step"] = (f"已确认恢复配置。执行下一项：{plan.method}" if plan else "已确认恢复配置，按页面提示继续。")
    history_reason = historical_lead((workflow or {}).get("historical_impacts") or {})
    if history_reason:
        fallback["reasoning"] = [history_reason, *list(fallback["reasoning"])[:1]]
    if provider is None:
        return fallback
    try:
        request_factory = (
            final_report_request
            if decision.task_complete or decision.action.value == "finish"
            else decision_explanation_request
        )
        response = provider.complete(request_factory(task, decision, evidence, plan, workflow))
        explanation = parse_explanation(response.content)
        # Concrete actions belong to the workflow, never to generated prose.
        explanation["next_step"] = fallback["next_step"]
        if workflow and ((workflow.get("submitted_result") or {}).get("configuration_confirmed") is False
                         or (workflow.get("submitted_result") or {}).get("comparable") is False
                         or decision.measurement_valid is False):
            explanation["summary"] = fallback["summary"]
            explanation["reasoning"] = fallback["reasoning"]
            explanation["warning"] = ""
        if decision.stop_reason in {"unchanged_observation", "player_profile"}:
            explanation["summary"] = fallback["summary"]
            explanation["reasoning"] = fallback["reasoning"]
            explanation["warning"] = ""
        if history_reason:
            explanation["reasoning"] = [history_reason, *[reason for reason in explanation["reasoning"] if reason != history_reason][:1]]
        explanation["source"] = "model"
        explanation["model"] = response.model
        return explanation
    except (ModelAPIError, ValueError, KeyError, TypeError):
        return fallback
