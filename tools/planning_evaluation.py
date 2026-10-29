"""Compare planners on frozen, validated options; never imply measured FPS gains.

Run ``python -m tools.planning_evaluation --help`` for replay/export usage.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from agent.api import ModelProvider, provider_from_env
from agent.controller import GameTuneController
from agent.feedback import summarize_feedback
from agent.next_step import choose_next_step
from agent.planner import plan_candidate_id
from agent.state_machine import WorkflowState
from agent.validator import KnowledgeBase, validate_plan
from models.schemas import AgentAction, BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask
from tools.analyze_evidence import analyze_evidence
from tools.configuration import canonical_value


def export_scenario(controller: GameTuneController, task_id: str) -> dict:
    """Export a local planning checkpoint, excluding API settings and raw captures."""
    task = controller.restore_task(task_id)
    if task is None:
        raise ValueError("找不到任务")
    workflow = controller.workflows[task_id]
    if workflow.state != WorkflowState.AWAITING_TEST:
        raise ValueError("仅能导出等待测试的规划节点；请先完成恢复确认")
    current = controller.planning_record(task_id)
    metadata = controller.repository.runtime_metadata(task_id)
    supplement = metadata.get("supplement", {})
    evidence = analyze_evidence(task, current,
        fps_cap_known=bool(supplement.get("fps_cap_known")),
        vsync_known=bool(supplement.get("vsync_known")),
        diagnostic_results=metadata.get("diagnostic_results", []))
    controller._add_supplement_evidence(task_id, evidence)
    pending = controller.pending_plan(task_id)
    if pending and controller.last_decisions.get(task_id) and controller.last_decisions[task_id].retry_same_experiment:
        candidates = [pending]
    else:
        candidates = controller.next_step_candidates(task, current, evidence, workflow.round_number)
    return {
        "name": task.name, "data_kind": "user_recorded_checkpoint",
        "notice": "用户录入的规划快照，需另行核对采集文件；不证明真实收益。",
        "task": task.to_dict(), "current": current.to_dict(),
        "evidence": [asdict(item) for item in evidence],
        "history": controller.experiment_history(task_id),
        "candidates": [{key: value for key, value in asdict(item).items() if key != "planning_context"} for item in candidates],
    }


def compare_scenario(scenario: dict, provider: ModelProvider | None = None) -> dict:
    """Both planners see the same options and history; judgments are explicit labels."""
    task = OptimizationTask(**scenario["task"])
    current = BenchmarkRecord(**scenario["current"])
    evidence = [Evidence(**item) for item in scenario["evidence"]]
    candidates = []
    knowledge = KnowledgeBase(Path(__file__).resolve().parents[1] / "knowledge")
    for item in scenario["candidates"]:
        values = dict(item)
        values["action"] = AgentAction(values["action"])
        plan = ExperimentPlan(**values)
        validate_plan(task, plan, knowledge)
        candidates.append(plan)
    if not candidates:
        raise ValueError("评估场景没有合法候选")
    history = scenario.get("history", [])
    acceptable = scenario.get("acceptable_candidate_ids")
    candidate_ids = {plan_candidate_id(plan) for plan in candidates}
    if acceptable is not None and (not isinstance(acceptable, list) or not set(acceptable) <= candidate_ids):
        raise ValueError("人工可接受候选必须来自本场景候选列表")

    def evaluate(provider):
        started = perf_counter()
        selection = choose_next_step(provider, task, current, evidence, candidates, history)
        plan = selection.plan
        repeated = False
        for item in history:
            decision = item.get("decision") or {}
            key = plan.setting_id
            before = item.get("before_configuration", {})
            after = item.get("after_configuration", {})
            # Only compare the exact action and reference condition. Invalid
            # samples and diagnostic restoration are never labeled failures.
            if (item.get("valid") is True and item.get("purpose") == "optimization"
                    and decision.get("action") == "revert_change" and not decision.get("accepted")
                    and item.get("setting_id") == key
                    and canonical_value(key, after.get(key)) == canonical_value(key, plan.new_value)
                    and canonical_value(key, before.get(key)) == canonical_value(key, plan.old_value)
                    and item.get("reference_record_id") == current.record_id):
                repeated = True
        identity = plan_candidate_id(plan)
        return {
            "candidate_id": identity, "title": plan.title, "setting_id": plan.setting_id,
            "source": selection.source, "status": plan.planning_context["status"],
            "legal": identity in candidate_ids,
            "reason": plan.selection_reason, "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "api_elapsed_ms": plan.planning_context["elapsed_ms"],
            "repeats_known_no_benefit": repeated,
            "reviewer_acceptable": identity in acceptable if acceptable is not None else None,
        }

    local = evaluate(None)
    model = evaluate(provider) if provider is not None else None
    return {
        "name": scenario.get("name", task.name), "evaluation_kind": "planning_replay",
        "data_kind": scenario.get("data_kind", "unverified_import"),
        "notice": "仅比较规划选择；回放、模拟模型及用户录入值均不能证明真实游戏收益。",
        "judgment_source": "scenario_reviewer" if acceptable is not None else "not_labeled",
        "candidate_count": len(candidates), "feedback": summarize_feedback(history),
        "local": local, "model": model,
        "same_choice": model["candidate_id"] == local["candidate_id"] if model else None,
        "model_comparison_available": bool(model and model["source"] == "model"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出规划快照或对照评估；输出不代表真实游戏收益")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="一个场景或场景列表 JSON")
    source.add_argument("--database", type=Path, help="本机任务数据库，仅导出快照，不调用模型")
    parser.add_argument("--task-id")
    parser.add_argument("--model", action="store_true", help="显式使用环境变量配置的模型调用；默认仅本地回放")
    parser.add_argument("--output", type=Path, required=True, help="本地输出 JSON，建议放 .qa/ 或 .data/")
    args = parser.parse_args()
    try:
        if args.database:
            if not args.task_id or not args.database.is_file() or args.model:
                raise ValueError("导出需要已有数据库和 --task-id，且不能同时使用 --model")
            result = export_scenario(GameTuneController(args.database), args.task_id)
        else:
            scenarios = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(scenarios, list):
                scenarios = [scenarios]
            provider = provider_from_env() if args.model else None
            if args.model and provider is None:
                raise ValueError("未配置环境变量模型接口")
            result = [compare_scenario(item, provider) for item in scenarios]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存：{args.output}；规划回放不代表真实游戏收益。")
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, f"无法完成评估：{type(error).__name__}；请检查场景格式、路径和模型配置。\n")


if __name__ == "__main__":
    main()
