from __future__ import annotations

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelAPIError, ModelProvider
from .prompts import decision_explanation_request, final_report_request, parse_explanation


def explain_decision(
    provider: ModelProvider | None,
    task: OptimizationTask,
    decision: Decision,
    *,
    evidence: list[Evidence] | None = None,
    plan: ExperimentPlan | None = None,
) -> dict[str, str | list[str]]:
    """Use a model when configured; always provide a deterministic fallback."""
    fallback: dict[str, str | list[str]] = {
        "summary": decision.summary,
        "reasoning": decision.reasons[:4],
        "next_step": _fallback_next_step(decision),
        "warning": "当前未使用模型 API，说明由本地规则生成。" if provider is None else "模型说明不可用，已回退到本地规则。",
        "source": "local",
    }
    if provider is None:
        return fallback
    try:
        request_factory = (
            final_report_request
            if decision.task_complete or decision.action.value == "finish"
            else decision_explanation_request
        )
        response = provider.complete(request_factory(task, decision, evidence, plan))
        explanation = parse_explanation(response.content)
        explanation["source"] = "model"
        explanation["model"] = response.model
        return explanation
    except (ModelAPIError, ValueError, KeyError, TypeError):
        return fallback


def _fallback_next_step(decision: Decision) -> str:
    if getattr(decision, "task_complete", False):
        if decision.action.value == "revert_change":
            return "恢复上一组已验证配置；任务已结束，请保存最终报告。"
        return "任务已结束，保存最佳已验证配置与最终报告。"
    steps = {
        "retest": "保持配置和测试条件不变，按页面提示补测；累计达到要求后再确认结果。",
        "keep_change": "保留本轮配置，按下一项单变量实验继续。",
        "revert_change": "恢复上一组已验证配置，再选择其他假设。",
        "finish": "保存最佳已验证配置并生成最终报告。",
    }
    return steps.get(decision.action.value, "根据当前实验计划补充数据。")
