from __future__ import annotations

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelAPIError, ModelProvider
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
        "reasoning": decision.reasons[:4],
        "next_step": _next_step_guard(decision, plan),
        "warning": "当前未使用模型 API，说明由本地规则生成。" if provider is None else "模型说明不可用，已回退到本地规则。",
        "source": "local",
    }
    if workflow and workflow.get("rollback_confirmed"):
        fallback["next_step"] = (f"已确认恢复配置。执行下一项：{plan.method}" if plan else "已确认恢复配置，按页面提示继续。")
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
            explanation["warning"] = "当前记录尚不能用于判断收益或瓶颈，请先处理页面指出的配置或测试条件问题。"
        if decision.stop_reason in {"unchanged_observation", "player_profile"}:
            explanation["summary"] = fallback["summary"]
            explanation["reasoning"] = fallback["reasoning"]
            explanation["warning"] = ("本次按整组实际配置继续规划，不能拆分为单项设置的收益。" if decision.stop_reason == "player_profile" else "本次是原配置复测，未用于判断建议调整的收益或硬件瓶颈。")
        explanation["source"] = "model"
        explanation["model"] = response.model
        return explanation
    except (ModelAPIError, ValueError, KeyError, TypeError):
        return fallback
