from __future__ import annotations

import json
from pathlib import Path

from models.schemas import AgentAction, ExperimentPlan, OptimizationTask
from tools.resolution import parse_resolution


class PlanValidationError(ValueError):
    pass


class KnowledgeBase:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def game_rules(self, game: str) -> dict:
        candidates = {
            "Counter-Strike 2": "cs2.json",
        }
        filename = candidates.get(game)
        if filename is None:
            raise PlanValidationError(f"游戏规则库暂不支持：{game}")
        path = self.directory / filename
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as error:
            raise PlanValidationError(f"无法读取游戏规则：{filename}") from error
        if not isinstance(data.get("settings"), list):
            raise PlanValidationError(f"游戏规则格式错误：{filename}")
        return data


def validate_plan(task: OptimizationTask, plan: ExperimentPlan, knowledge: KnowledgeBase) -> None:
    rules = knowledge.game_rules(task.game)
    if plan.round_number < 1 or plan.round_number > task.max_rounds:
        raise PlanValidationError("实验轮次超出任务上限")
    required = (plan.title, plan.setting, plan.rationale, plan.hypothesis, plan.method, plan.success_criteria, plan.rollback)
    if any(not value.strip() for value in required):
        raise PlanValidationError("实验计划缺少必要说明")
    if plan.old_value == plan.new_value and plan.action not in {AgentAction.REQUEST_DATA, AgentAction.FINISH}:
        raise PlanValidationError("实验计划没有产生可验证的变化")
    if plan.action != AgentAction.RECOMMEND_CHANGE:
        return
    setting_id = getattr(plan, "setting_id", "")
    setting = next((item for item in rules["settings"] if (
        item.get("id") == setting_id if setting_id else item.get("label") == plan.setting
    )), None)
    if setting is None:
        raise PlanValidationError(f"规则库中不存在设置项：{plan.setting}")
    values = setting.get("values", [])
    custom_resolution = False
    if plan.setting == "分辨率":
        try:
            parse_resolution(plan.new_value)
            custom_resolution = True
        except ValueError:
            pass
    if values and plan.new_value not in values and not custom_resolution:
        raise PlanValidationError(f"设置值不合法：{plan.setting} = {plan.new_value}")
    if setting.get("change_limit"):
        if plan.old_value not in values:
            raise PlanValidationError(f"必须先记录实际原值：{plan.setting}")
        if values.index(plan.old_value) - values.index(plan.new_value) != 1:
            raise PlanValidationError(f"本轮只能降低一级：{plan.setting}")
