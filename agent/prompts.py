from __future__ import annotations

import json
from dataclasses import asdict

from models.schemas import Decision, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelMessage, ModelRequest


SYSTEM_PROMPT = """你是 GameTune 的玩家助手。确定性分析模块已经作出结论，你不能修改数值、动作或虚构测试数据。

请把结果写给不熟悉性能测试的普通游戏玩家：
- 使用日常、直接的中文，先说“这次调整有没有用”，再说原因和下一步。
- 不要使用“综合得分”“非退化”“置信区间”“方差”“显著性”“归因”“证据指向”等研究或统计术语。
- 必须提到专有名词时，紧跟一句白话解释。首次出现 1% Low 时，可写成“1% Low（卡顿时的帧率表现）”。
- 不要复述内部字段名、英文动作名或计算公式，不要自行增加复测次数或新的优化动作。
- 每条理由只表达一个重点，下一步要写成玩家可以直接照做的操作。

只返回符合要求的 JSON 对象。"""

FINAL_REPORT_SYSTEM_PROMPT = """你是 GameTune 的最终报告撰写器。任务是否结束、最终动作和已验证结论均由确定性分析模块决定，你不能修改这些结论。你只能依据提供的任务、最终决策、证据和最后计划撰写报告；不得虚构测试数据或硬件瓶颈，不得把未验证建议、失败动作或待回滚配置写成最佳已验证配置。

报告要写给普通游戏玩家：用日常中文说清“最后保留什么设置、表现变好还是变差、玩家现在要做什么”。禁止使用“综合得分”“非退化”“置信区间”“方差”“显著性”“归因”“证据指向”等研究或统计术语；专有名词无法避免时要立即用白话解释。不复述内部字段名和英文动作名，不自行增加复测次数或新建议。只返回 JSON 对象。"""


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
            "summary": "不超过 80 字；用白话说明这次调整是保留、撤销还是需要继续测",
            "reasoning": ["2 到 3 条普通玩家能看懂的原因，每条一句"],
            "next_step": "只写玩家接下来需要完成的具体操作",
            "warning": "用白话说明尚不能确定的事，没有则返回空字符串",
        },
        "audience": "不熟悉性能分析和统计术语的普通游戏玩家",
        "language": "简体中文，口语化，不使用学术术语或内部字段名",
        "must_follow": _next_step_guard(decision, plan),
        "constraints": [
            "summary、reasoning 和 next_step 不能互相矛盾",
            "next_step 必须严格遵守 must_follow，不得自行要求重复本轮实验",
            "decision.action 为 keep_change 时表示保留调整并进入下一步，不是重复本轮复测",
            "decision.action 为 retest 时才能要求保持同一配置重新测试",
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
) -> ModelRequest:
    payload = {
        "report_type": "final_report",
        "task": task.to_dict(),
        "final_decision": decision.to_dict(),
        "evidence": [asdict(item) for item in (evidence or [])],
        "last_plan": _plan_dict(plan),
        "required_output": {
            "summary": "不超过 120 字；用白话说明最后保留或恢复哪类设置，以及任务为什么结束",
            "reasoning": ["3 到 4 条普通玩家能看懂的关键发现，每条一句"],
            "next_step": "用白话写清保存设置、恢复设置或继续测试的具体操作",
            "warning": "用白话说明未确认的事，没有则返回空字符串",
        },
        "audience": "不熟悉性能分析和统计术语的普通游戏玩家",
        "language": "简体中文，口语化，不使用学术术语或内部字段名",
        "constraints": [
            "最终动作、数值变化和任务完成状态以 final_decision 为准",
            "不能把 last_plan 中尚未验证的建议描述为已验证结果",
            "final_decision.rollback_required 为真时，next_step 必须先要求恢复上一组已验证配置",
            "证据不足时应在 warning 中明确说明，不能自行补充结论",
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


def _next_step_guard(decision: Decision, plan: ExperimentPlan | None) -> str:
    """Give the model a plain-language action boundary it must not exceed."""
    if decision.rollback_required or decision.action.value == "revert_change":
        return "先恢复上一组已验证配置，不能保留本轮调整。"
    if decision.action.value == "retest":
        return "保持本轮配置和测试条件不变，重复本轮测试。"
    if decision.task_complete or decision.action.value == "finish":
        return "任务已结束，保存最佳已验证配置和报告。"
    if plan is not None:
        return f"保留本轮已接受的配置，然后执行下一项：{plan.method}"
    return "保留本轮已接受的配置，等待程序生成下一项调整；不要重复本轮测试。"
