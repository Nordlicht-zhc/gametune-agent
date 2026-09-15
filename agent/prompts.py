from __future__ import annotations

import json
from dataclasses import asdict

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelMessage, ModelRequest


SYSTEM_PROMPT = """你是 GameTune 的结果解释器。确定性分析模块已经作出结论，你不能修改数值、动作或虚构测试数据。请用简洁中文解释结论，并只返回 JSON 对象。"""


def decision_explanation_request(
    task: OptimizationTask,
    decision: Decision,
    evidence: list[Evidence] | None = None,
    plan: ExperimentPlan | None = None,
) -> ModelRequest:
    payload = {
        "task": task.to_dict(),
        "decision": decision.to_dict(),
        "evidence": [asdict(item) for item in (evidence or [])],
        "next_plan": _plan_dict(plan),
        "required_output": {
            "summary": "不超过 80 字的结论",
            "reasoning": ["2 到 4 条基于输入证据的说明"],
            "next_step": "用户下一步应做什么",
            "warning": "不确定性或空字符串",
        },
    }
    return ModelRequest(
        messages=(ModelMessage("system", SYSTEM_PROMPT), ModelMessage("user", json.dumps(payload, ensure_ascii=False))),
        temperature=0.1,
        max_output_tokens=600,
        response_format="json",
        metadata={"purpose": "decision_explanation", "task_id": task.task_id},
    )


def parse_explanation(content: str) -> dict[str, str | list[str]]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError("模型没有返回合法 JSON") from error
    if not isinstance(data, dict):
        raise ValueError("模型响应必须是 JSON 对象")
    summary = data.get("summary", "")
    next_step = data.get("next_step", "")
    reasoning = data.get("reasoning", [])
    warning = data.get("warning", "")
    if (
        not isinstance(summary, str) or not summary.strip()
        or not isinstance(next_step, str) or not next_step.strip()
        or not isinstance(warning, str)
        or not isinstance(reasoning, list) or not reasoning
        or any(not isinstance(item, str) or not item.strip() for item in reasoning)
    ):
        raise ValueError("模型响应缺少 summary、reasoning 或 next_step")
    return {
        "summary": summary.strip()[:160],
        "reasoning": [item.strip()[:400] for item in reasoning][:4],
        "next_step": next_step.strip()[:400],
        "warning": warning.strip()[:400],
    }


def _plan_dict(plan: ExperimentPlan | None) -> dict | None:
    if plan is None:
        return None
    data = asdict(plan)
    data["action"] = plan.action.value
    return data
