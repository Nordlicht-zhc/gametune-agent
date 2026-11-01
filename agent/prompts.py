from __future__ import annotations

import json
from dataclasses import asdict

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelMessage, ModelRequest


SYSTEM_PROMPT = """你是 GameTune 的结果说明助手。把程序评估出的结论、最重要的依据和下一步操作，用简洁自然的中文告诉玩家。

直接说已知事实，省略重复提醒、泛化的“不确定”表述和免责套话。当前测试数据、历史观察和设置权重分开表达；历史影响排序先说变化最大的设置，并标明该记录已验证或待验证。组合调整只评价整组，权重表示相对影响大小。

输出 summary（一句结论）、reasoning（1 到 2 条关键依据）、next_step（一条具体操作）。以程序提供的评估结论和 must_follow 操作为准，只使用输入中的数据，不增加新的测试或调整。

使用玩家容易理解的日常用语；首次出现 1% Low 时解释为“卡顿时的帧率表现”。只返回 JSON 对象。"""

FINAL_REPORT_SYSTEM_PROMPT = """你是 GameTune 的最终结果撰写助手。用简洁自然的中文说明最终保留或恢复的设置、已测出的表现变化和玩家下一步要做的操作。

以 final_decision 中的最终动作和验证状态为准，只引用输入数据。把已验证结果与待验证观察明确区分；未完成的建议不写成收益。省略重复提醒、泛化的“不确定”表述和免责套话。

输出 summary（一句结论）、reasoning（2 到 3 条关键发现）、next_step（一条具体操作）。使用玩家容易理解的日常用语，只返回 JSON 对象。"""


def decision_explanation_request(
    task: OptimizationTask,
    decision: Decision,
    evidence: list[Evidence] | None = None,
    plan: ExperimentPlan | None = None,
    workflow: dict | None = None,
) -> ModelRequest:
    payload = {
        "task": task.to_dict(),
        "decision": decision.to_dict(),
        "evidence": [asdict(item) for item in (evidence or [])],
        "next_plan": _plan_dict(plan),
        "workflow": workflow or {},
        "required_output": {
            "summary": "不超过 60 字；一句话写明确定结论",
            "reasoning": ["1 到 2 条最重要的实测或历史依据，每条一句"],
            "next_step": "一条玩家可以直接执行的操作",
        },
        "audience": "不熟悉性能分析和统计术语的普通游戏玩家",
        "language": "简体中文，口语化",
        "must_follow": _next_step_guard(decision, plan),
        "constraints": [
            "结论以 decision 为准，next_step 严格照 must_follow 执行",
            "历史影响排序先说明变化最大的设置和幅度，并标明已验证或待验证；不把历史值说成本轮测量",
            "setting_weights 是相对影响排序，不是预期 FPS 增幅；组合调整按整组说明",
        ],
    }
    return ModelRequest(
        messages=(ModelMessage("system", SYSTEM_PROMPT), ModelMessage("user", json.dumps(payload, ensure_ascii=False))),
        temperature=0.1,
        # Reasoning-capable compatible models may count hidden reasoning tokens
        # against this limit. Leave enough room for the required JSON object.
        max_output_tokens=2000,
        response_format="json",
        metadata={"purpose": "decision_explanation", "task_id": task.task_id},
    )


def final_report_request(
    task: OptimizationTask,
    decision: Decision,
    evidence: list[Evidence] | None = None,
    plan: ExperimentPlan | None = None,
    workflow: dict | None = None,
) -> ModelRequest:
    payload = {
        "report_type": "final_report",
        "task": task.to_dict(),
        "final_decision": decision.to_dict(),
        "evidence": [asdict(item) for item in (evidence or [])],
        "last_plan": _plan_dict(plan),
        "workflow": workflow or {},
        "required_output": {
            "summary": "不超过 80 字；一句话写明最终结论",
            "reasoning": ["2 到 3 条最重要的已测发现，每条一句"],
            "next_step": "一条具体操作",
        },
        "audience": "不熟悉性能分析和统计术语的普通游戏玩家",
        "language": "简体中文，口语化",
        "constraints": [
            "最终动作、数值变化和完成状态以 final_decision 为准",
            "区分已验证结果与待验证观察",
            "需要恢复配置时，next_step 先写恢复操作",
        ],
    }
    return ModelRequest(
        messages=(
            ModelMessage("system", FINAL_REPORT_SYSTEM_PROMPT),
            ModelMessage("user", json.dumps(payload, ensure_ascii=False)),
        ),
        temperature=0.1,
        max_output_tokens=3000,
        response_format="json",
        metadata={"purpose": "final_report", "task_id": task.task_id},
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
    if (
        not isinstance(summary, str) or not summary.strip()
        or not isinstance(next_step, str) or not next_step.strip()
        or not isinstance(reasoning, list) or not reasoning
        or any(not isinstance(item, str) or not item.strip() for item in reasoning)
    ):
        raise ValueError("模型响应缺少 summary、reasoning 或 next_step")
    return {
        "summary": summary.strip()[:160],
        "reasoning": [item.strip()[:400] for item in reasoning][:3],
        "next_step": next_step.strip()[:400],
        "warning": "",
    }


def _plan_dict(plan: ExperimentPlan | None) -> dict | None:
    if plan is None:
        return None
    data = asdict(plan)
    data["action"] = plan.action.value
    return data


def _next_step_guard(decision: Decision, plan: ExperimentPlan | None) -> str:
    """Give the model a plain-language action boundary it must not exceed."""
    if decision.stop_reason == "player_profile":
        return (f"按你实际采用的配置继续，下一项：{plan.method}" if plan else "按页面提示继续下一项操作。")
    if decision.rollback_required or decision.action.value == "revert_change":
        return "恢复上一组已验证配置。"
    if decision.action.value == "retest":
        return (f"核对实际配置和测试场景后，按原计划重测：{plan.method}"
                if plan else "核对实际配置和测试场景后，重测本轮项目。")
    if decision.task_complete or decision.action.value == "finish":
        return "保存最佳已验证配置和报告。"
    if plan is not None:
        return f"保留本轮通过评估的配置，执行下一项：{plan.method}"
    return "保留本轮通过评估的配置，按页面提示继续。"
