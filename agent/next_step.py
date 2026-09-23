from __future__ import annotations

import json
from dataclasses import dataclass, replace

from models.schemas import BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelAPIError, ModelMessage, ModelProvider, ModelRequest
from .planner import plan_candidate_id


SYSTEM_PROMPT = """你是 GameTune 的下一步实验规划助手。你的任务是根据本轮数据、诊断证据和玩家偏好，从程序提供的候选计划中选择最值得做的一项。

先判断当前是在排查原因还是验证优化效果。原因不清、关键设置未知或证据相互矛盾时，优先选择能排除干扰或区分原因的候选；只有现有证据足以支持某个优化方向时，才选择优化验证。GPU 占用偏低本身不能证明 CPU 瓶颈；没有温度、频率等数据时不能断言存在过热或降频。

候选计划中的设置、数值、操作方式、成功条件和恢复方法已经由程序生成。你只能选择一个候选编号并解释选择理由，不得改写或创造设置、测试次数、数值、游戏菜单或实验动作。若所有候选都不理想，也必须选择最安全、最能补充信息的一项，并在理由中说明限制。

只返回 JSON：{"candidate_id":"候选编号","reason":"不超过 120 字的简体中文理由"}。"""


@dataclass(frozen=True, slots=True)
class NextStepSelection:
    plan: ExperimentPlan
    source: str
    warning: str = ""


def choose_next_step(
    provider: ModelProvider | None,
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    candidates: list[ExperimentPlan],
    history: list[dict] | None = None,
) -> NextStepSelection:
    """Ask the model to choose only among locally generated candidate plans."""
    if not candidates:
        raise ValueError("当前没有可供选择的下一步计划")
    fallback = candidates[0]
    if provider is None:
        return NextStepSelection(fallback, "local", "当前未连接模型，使用本地安全建议。")
    request = next_step_request(task, current, evidence, candidates, history=history)
    try:
        response = provider.complete(request)
        data = parse_next_step(response.content)
        selected = next((item for item in candidates if plan_candidate_id(item) == data["candidate_id"]), None)
        if selected is None:
            raise ValueError("模型选择了候选列表之外的计划")
        return NextStepSelection(
            replace(
                selected,
                planning_source="model",
                selection_reason=data["reason"],
            ),
            "model",
        )
    except (ModelAPIError, ValueError, KeyError, TypeError, IndexError):
        return NextStepSelection(
            replace(
                fallback,
                planning_source="local",
                selection_reason="模型建议暂不可用，已使用本地规则生成的安全计划。",
            ),
            "local",
            "AI 规划不可用，已回退到本地规则。",
        )


def next_step_request(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    candidates: list[ExperimentPlan],
    *,
    history: list[dict] | None = None,
) -> ModelRequest:
    safe_configuration_keys = {
        "resolution", "display_mode", "fps_cap", "vsync", "shadow_quality",
        "dynamic_shadows", "graphics_mode", "power_mode", "cooling_mode",
        "gpu_power_w", "refresh_rate", "test_scene",
    }
    payload = {
        "task": {
            "game": task.game,
            "cpu": task.cpu,
            "gpu": task.gpu,
            "is_laptop": task.is_laptop,
            "mode": task.mode,
            "target_avg_fps": task.target_avg_fps,
            "target_low_fps": task.target_low_fps,
            "max_rounds": task.max_rounds,
        },
        "current_result": {
            "avg_fps": current.avg_fps,
            "low_fps": current.low_fps,
            "gpu_usage": current.gpu_usage,
            "repeats": current.repeats,
            "resolution": current.resolution,
            "test_scene": current.test_scene,
            "comparable": current.comparable,
            "configuration_confirmed": current.configuration_confirmed,
            "configuration": {
                key: value for key, value in (current.configuration or {}).items()
                if key in safe_configuration_keys
            },
        },
        "evidence": [
            {"code": item.code, "title": item.title, "detail": item.detail, "supports": item.supports}
            for item in evidence
        ],
        "completed_experiments": [
            {
                "round_number": item.get("round_number"),
                "role": item.get("role"),
                "avg_fps": item.get("avg_fps"),
                "low_fps": item.get("low_fps"),
                "gpu_usage": item.get("gpu_usage"),
                "planned_setting_id": (item.get("configuration") or {}).get("planned_setting_id"),
                "actual_setting_value": (item.get("configuration") or {}).get("actual_setting_value"),
                "confirmed": item.get("configuration_confirmed"),
                "comparable": item.get("comparable"),
            }
            for item in (history or [])[-8:]
            if isinstance(item, dict)
        ],
        "candidates": [
            {
                "candidate_id": plan_candidate_id(plan),
                "round_number": plan.round_number,
                "purpose": "diagnostic" if plan.purpose == "diagnostic" else "optimization",
                "title": plan.title,
                "setting": plan.setting,
                "rationale": plan.rationale,
                "hypothesis": plan.hypothesis,
                "method": plan.method,
                "success_criteria": plan.success_criteria,
                "rollback": plan.rollback,
            }
            for plan in candidates
        ],
        "constraints": [
            "逐项比较候选的诊断价值、可执行性、潜在代价和已有证据，不按列表顺序机械选择",
            "证据不足时优先排除关键干扰或选择能区分假设的诊断候选",
            "模型只提供选择和理由；数值测量、可信度判断和保留或回滚由程序负责",
        ],
    }
    return ModelRequest(
        messages=(
            ModelMessage("system", SYSTEM_PROMPT),
            ModelMessage("user", json.dumps(payload, ensure_ascii=False)),
        ),
        temperature=0.1,
        max_output_tokens=500,
        response_format="json",
        metadata={"purpose": "next_step_selection", "task_id": task.task_id},
    )


def parse_next_step(content: str) -> dict[str, str]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError("模型没有返回合法 JSON") from error
    if not isinstance(data, dict):
        raise ValueError("模型计划必须是 JSON 对象")
    candidate_id = data.get("candidate_id")
    reason = data.get("reason")
    if (
        not isinstance(candidate_id, str) or len(candidate_id) != 16
        or not isinstance(reason, str) or not reason.strip()
    ):
        raise ValueError("模型计划缺少候选编号或选择理由")
    return {"candidate_id": candidate_id, "reason": reason.strip()[:240]}
