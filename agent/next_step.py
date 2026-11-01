from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter

from models.schemas import BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask

from .api import ModelAPIError, ModelMessage, ModelProvider, ModelRequest
from .planner import plan_candidate_id
from .feedback import summarize_feedback
from tools.history_impacts import historical_impacts
from tools.performance_knowledge import performance_context
from tools.configuration import canonical_value
from tools.setting_weights import setting_weights, candidate_reference_weight


SYSTEM_PROMPT = """你是 GameTune 的下一步实验规划助手。从程序提供的合法候选中，选择最符合玩家目标、且最有本地数据支持的一项。

先看本任务有效历史：优先考虑影响较大、与当前实际配置相符的设置；组合改动按整组看待。历史收益是否验证按记录状态表述。设置权重是相对排序，不代表预期 FPS 增幅；外部参考与本机记录分开使用。

玩家实际采用的配置就是后续规划的当前配置，直接基于它选择下一步。优先给出能推进优化的设置；只有本轮存在明确的数据缺项，且候选能直接补齐时，才选择补充或复测操作。GPU 占用偏低本身不作为 CPU 瓶颈依据。

候选里的设置、数值、操作和恢复方法由程序生成。选择一个候选编号，理由用一句话说明最相关的数据依据和下一步。只返回 JSON：{"candidate_id":"候选编号","reason":"不超过 80 字的简体中文理由"}。"""


@dataclass(frozen=True, slots=True)
class NextStepSelection:
    plan: ExperimentPlan
    source: str
    warning: str = ""


def rank_candidates(
    task: OptimizationTask, current: BenchmarkRecord, evidence: list[Evidence],
    candidates: list[ExperimentPlan], history: list[dict] | None = None, weights: dict | None = None,
) -> list[ExperimentPlan]:
    """Evidence-based local fallback; platform labels alone are weak evidence."""
    codes = {item.code for item in evidence}
    gpu_supported = any(item.supports == "gpu" for item in evidence)
    power = str(current.configuration.get("power_mode", task.power_mode))

    def priority(plan: ExperimentPlan) -> int:
        if plan.setting_id == "test_conditions":
            return 0
        if codes & {"cap_unknown", "cap_active"} and plan.setting_id in {"fps_cap", "vsync", "frame_limits", "uncapped_validation"}:
            return 1
        if plan.diagnostic_record_id:
            return 2
        if power == "battery" and plan.setting_id == "power_mode":
            return 3
        if gpu_supported and plan.setting_id not in {"power_mode", "cooling_mode", "graphics_mode", "gpu_power_w", "repeats"}:
            return 4
        if plan.setting_id in {"power_mode", "cooling_mode", "graphics_mode", "gpu_power_w"}:
            return 8 if gpu_supported else 5
        if plan.setting_id == "repeats":
            return 9
        return 5

    effects = historical_impacts(history or [], task.mode)["single_setting_effects"]
    def observed_priority(plan):
        base = priority(plan)
        if base <= 1:
            return base, 0
        for effect in effects:
            key = plan.setting_id
            before, after = effect["reference_configuration"], effect["result_configuration"]
            if (effect["changed_settings"] == [key]
                    and canonical_value(key, before.get(key)) == canonical_value(key, plan.old_value)
                    and canonical_value(key, after.get(key)) == canonical_value(key, plan.new_value)
                    and all(canonical_value(other, current.configuration.get(other, current.resolution if other == "resolution" else "unknown"))
                            == canonical_value(other, value) for other, value in before.items() if other != key)):
                gain = effect["weighted_delta_pct"]
                return (2 if effect["status"] == "verified" else 3, -gain) if gain > 0 else (9, -gain)
        return base, -candidate_reference_weight(weights or {}, plan)
    return sorted(candidates, key=observed_priority)


def _selection(
    plan: ExperimentPlan, candidates: list[ExperimentPlan], history: list[dict],
    source: str, reason: str, *, elapsed_ms: float = 0, status: str = "local", warning: str = "", weights: dict | None = None,
) -> NextStepSelection:
    context = {
        "candidate_options": [
            {"candidate_id": plan_candidate_id(item), "title": item.title,
             "setting_id": item.setting_id, "purpose": item.purpose,
             "hypothesis": item.hypothesis}
            for item in candidates
        ],
        "selected_candidate_id": plan_candidate_id(plan),
        "source": source, "status": status, "elapsed_ms": round(elapsed_ms, 3),
        "feedback": summarize_feedback(history),
        "setting_weights": weights or {},
    }
    return NextStepSelection(replace(plan, planning_source=source, selection_reason=reason,
                                     planning_context=context), source, warning)


def choose_next_step(
    provider: ModelProvider | None,
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    candidates: list[ExperimentPlan],
    history: list[dict] | None = None,
    *,
    knowledge_directory: str | Path | None = None,
) -> NextStepSelection:
    """Ask the model to choose only among locally generated candidate plans."""
    if not candidates:
        raise ValueError("当前没有可供选择的下一步计划")
    history = history or []
    weights = setting_weights(task.game, task.mode, history, directory=knowledge_directory)
    candidates = rank_candidates(task, current, evidence, candidates, history, weights)
    fallback = candidates[0]
    if len(candidates) == 1:
        return _selection(fallback, candidates, history, "local", "当前只有一个合法候选，直接采用本地计划。", status="single_candidate", weights=weights)
    if provider is None:
        return _selection(fallback, candidates, history, "local", "根据当前有效数据和本任务历史排序。", weights=weights)
    request = next_step_request(task, current, evidence, candidates, history=history, knowledge_directory=knowledge_directory)
    started = perf_counter()
    try:
        response = provider.complete(request)
        data = parse_next_step(response.content)
        selected = next((item for item in candidates if plan_candidate_id(item) == data["candidate_id"]), None)
        if selected is None:
            raise ValueError("模型选择了候选列表之外的计划")
        return _selection(selected, candidates, history, "model", data["reason"],
                          elapsed_ms=(perf_counter() - started) * 1000, status="model_selected", weights=weights)
    except (ModelAPIError, ValueError, KeyError, TypeError, IndexError):
        return _selection(fallback, candidates, history, "local",
                          "根据当前有效数据和本任务历史排序。",
                          elapsed_ms=(perf_counter() - started) * 1000, status="model_fallback",
                          weights=weights)


def next_step_request(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    candidates: list[ExperimentPlan],
    *,
    history: list[dict] | None = None,
    knowledge_directory: str | Path | None = None,
) -> ModelRequest:
    safe_configuration_keys = {
        "resolution", "display_mode", "fps_cap", "vsync", "shadow_quality",
        "dynamic_shadows", "graphics_mode", "power_mode", "cooling_mode",
        "gpu_power_w", "refresh_rate", "test_scene", "user_defined", "cpu_background_tasks", "frame_limits",
        "player_contrast", "nvidia_gsync", "nvidia_reflex", "amd_anti_lag", "menu_fps_cap",
        "video_preset", "msaa_mode", "texture_detail", "model_texture_detail", "texture_filtering", "shader_detail",
        "particle_detail", "ambient_occlusion", "hdr", "fsr",
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
        "feedback_summary": summarize_feedback(history or []),
        "historical_impacts": historical_impacts(history or [], task.mode),
        "setting_weights": setting_weights(task.game, task.mode, history or [], directory=knowledge_directory),
        "external_performance_reference": performance_context(task, current, candidates, history or [], directory=knowledge_directory),
        "completed_experiments": [
            {
                **{key: value for key, value in item.items() if key in {
                    "record_id", "round_number", "purpose", "setting_id", "reference_record_id", "changed_settings", "suggested_setting_id",
                    "reference_avg_fps", "reference_low_fps", "reference_gpu_usage", "gpu_usage", "avg_fps", "low_fps",
                    "avg_delta_pct", "low_delta_pct", "repeats", "configuration_confirmed",
                    "comparable", "valid", "test_scene", "reference_test_scene", "decision", "restoration_confirmed",
                }},
                "before_configuration": {key: value for key, value in item.get("before_configuration", {}).items() if key in safe_configuration_keys},
                "after_configuration": {key: value for key, value in item.get("after_configuration", item.get("configuration", {})).items() if key in safe_configuration_keys},
            }
            for item in (history or []) if isinstance(item, dict)
        ],
        "candidates": [
            {
                "candidate_id": plan_candidate_id(plan),
                "setting_id": plan.setting_id,
                "old_value": plan.old_value,
                "new_value": plan.new_value,
                "round_number": plan.round_number,
                "purpose": "diagnostic" if plan.purpose == "diagnostic" else "optimization",
                "reference_record_id": plan.reference_record_id,
                "reference_avg_fps": plan.reference_avg_fps,
                "reference_low_fps": plan.reference_low_fps,
                "diagnostic_record_id": plan.diagnostic_record_id,
                "required_configuration": plan.required_configuration,
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
