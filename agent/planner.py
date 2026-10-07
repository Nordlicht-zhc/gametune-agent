from __future__ import annotations

import hashlib
import json

from models.schemas import AgentAction, BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask
from tools.resolution import aspect_ratio_name, lower_resolution_same_aspect


GENERIC_OPTIMIZATION_DIRECTIONS = (
    "GPU 画面负载",
    "CPU 后台负载",
    "供电与散热",
    "画面稳定性",
    "游戏内画面质量",
)


def plan_next_experiment(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    round_number: int,
    *,
    blocked_setting_ids: set[str] | None = None,
) -> ExperimentPlan:
    blocked = set(blocked_setting_ids or ())

    def available(setting_id: str) -> bool:
        return setting_id not in blocked

    codes = {item.code for item in evidence}
    if "conditions_changed" in codes and available("test_conditions"):
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "按相同条件重新测试",
            "测试环境",
            "存在差异",
            "与基线一致",
            "测试条件不同，当前结果不能用于判断设置是否有效。",
            "使用相同场景和测试方法后，前后结果才能比较。",
            "恢复基线使用的场景、路线、供电模式和时长；先完成 2 次，差异超过 5% 时再补第 3 次。",
            "测试均使用与基线相同的条件。",
            "无需更改游戏配置。",
            round_number,
            setting_id="test_conditions",
        )
    if "cap_unknown" in codes and (available("fps_cap") or available("vsync")):
        configuration = current.configuration or {}
        cap_value = configuration.get("fps_cap", task.fps_cap)
        vsync_value = configuration.get("vsync", task.vsync)
        cap_unknown = cap_value in (None, "", "unknown", "未确认")
        vsync_unknown = vsync_value in (None, "", "unknown", "未确认")
        if cap_unknown and available("fps_cap"):
            return ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "记录游戏内最高 FPS",
                "游戏最高FPS",
                "未确认",
                "记录当前值（0 表示不限制）",
                "需要确认游戏内帧率上限，避免把人为限制误认为硬件瓶颈。",
                "记录当前实际值后，才能判断是否需要进一步排除限帧影响。",
                "查看游戏设置中的“游戏最高 FPS”并记录当前值；本步骤不修改设置，也不需要测试帧率。",
                "记录当前游戏最高 FPS 数值；0 表示不限制。",
                "本步骤不修改配置。",
                round_number,
                setting_id="fps_cap",
            )
        if vsync_unknown and available("vsync"):
            return ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "确认垂直同步状态",
                "V-Sync",
                "未确认",
                "记录当前状态",
                "垂直同步可能限制帧率或改变输入延迟，需要与帧率上限分开记录。",
                "确认当前状态后，诊断时不会把垂直同步的影响误判成硬件限制。",
                "查看游戏中的 V-Sync / 等待垂直同步选项并记录启用或禁用；本步骤不修改设置，也不需要测试帧率。",
                "记录当前 V-Sync 状态。",
                "本步骤不修改配置。",
                round_number,
                setting_id="vsync",
            )
        return ExperimentPlan(
            AgentAction.FINISH,
            "限帧信息已补齐",
            "游戏最高FPS 与 V-Sync",
            str(cap_value),
            str(vsync_value),
            "限帧状态已经记录，继续依据其他证据选择下一步。",
            "结合 GPU 占用与诊断实验判断主要限制。",
            "由下一轮规划选择需要验证的方向。",
            "按所选候选计划的条件判断。",
            "本步骤不修改配置。",
            round_number,
            setting_id="fps_cap",
        )
    if "cap_active" in codes:
        config = current.configuration or {}
        setting_id = "frame_limits"
        sync_label = {"enabled": "已开启", "disabled": "已关闭", "unknown": "未确认"}.get(str(config.get("vsync", task.vsync)), "未确认")
        old_value = f"最高 FPS：{config.get('fps_cap', task.fps_cap)}；垂直同步：{sync_label}"
        new_value = "不限帧且关闭垂直同步"
        if available(setting_id):
            return ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT, "临时排除帧率限制", "帧率限制",
                old_value, new_value, "设置已知不代表限帧影响已排除。",
                "临时解除限帧并关闭垂直同步，观察同场景帧率变化。",
                "将游戏最高 FPS 设为 0，并关闭垂直同步；其他设置与测试条件不变。先测 1 次，结论不明确时补测；结束后恢复这两项原值。若驱动或外部工具仍有限帧，请注明测试条件变化，不用于判断瓶颈。",
                "实际配置已确认，测试有效且可比；记录帧率变化，不将诊断结果作为最佳配置。",
                f"恢复本项原值：{old_value}。", round_number, setting_id=setting_id, purpose="diagnostic",
                required_configuration={"fps_cap": "0", "vsync": "disabled"},
            )
        return ExperimentPlan(
            AgentAction.FINISH, "限帧条件下暂不判断硬件瓶颈", "帧率限制", old_value, old_value,
            "已完成临时排除测试并恢复原配置，当前测量仍处于可能限帧的条件。",
            "恢复原限制后的数据不能直接用于分辨率瓶颈对比。",
            "查看诊断结论；如需继续硬件诊断，请用关闭垂直同步、解除限帧的配置建立新的有效基线任务。",
            "不把低 GPU 占用认定为 CPU 瓶颈，不报告未经正式验证的收益。",
            "原配置已恢复，无需继续修改。", round_number, setting_id="frame_limit_result", purpose="diagnostic",
        )
    if "laptop_power_unknown" in codes and available("gpu_power_w"):
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "记录测试时的 GPU 功耗",
            "GPU 功耗",
            "未知",
            "记录监控均值",
            "同型号笔记本显卡可能采用不同功耗，仅看型号无法判断实际性能。",
            "记录 GPU 功耗，可以判断显卡是否受到功耗限制。",
            "接通电源并运行相同测试，记录稳定阶段的 GPU 平均功耗和峰值。",
            "成功记录测试时的 GPU 平均功耗和峰值。",
            "本步骤不修改配置。",
            round_number,
            setting_id="gpu_power_w",
        )
    if "laptop_power_mode" in codes and available("power_mode"):
        power_mode = str((current.configuration or {}).get("power_mode", task.power_mode))
        power_mode_label = {
            "ac_performance": "接通电源 · 性能模式",
            "ac_balanced": "接通电源 · 平衡模式",
            "battery": "电池供电",
            "unknown": "不确定",
        }.get(power_mode, power_mode)
        if power_mode not in {"unknown", ""}:
            return ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "对比笔记本供电 / 性能模式",
                "供电 / 性能模式",
                power_mode_label,
                "接通电源 · 性能模式",
                "当前记录显示设备未处于接电性能模式，但其对帧率的实际影响尚未测量。",
                "同场景对比后，可以判断供电 / 性能模式是否是当前性能限制之一。",
                "接通电源并切换到性能模式，其他设置不变；同一场景先测试 1 次，结果不明确时最多再测 1 次。",
                "对比平均 FPS、1% Low 和 GPU 占用；本次只用于诊断，结束后恢复原模式。",
                f"恢复测试前的供电 / 性能模式：{power_mode_label}。",
                round_number,
                setting_id="power_mode",
                purpose="diagnostic",
            )
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "确认测试时的供电模式",
            "供电 / 性能模式",
            power_mode_label,
            "记录当前实际值",
            "供电状态可能影响性能，但尚不清楚当前测试时的供电情况。",
            "先确认现有状态，避免把尚未验证的性能模式当成必做优化。",
            "查看测试时是否接通电源，并记录 Windows 与厂商控制中心显示的运行模式；本步骤不切换设置，也不需要测试帧率。",
            "记录测试时的供电状态与运行模式。",
            "本步骤不修改配置。",
            round_number,
            setting_id="power_mode",
        )
    if "laptop_cooling" in codes and available("cooling_mode"):
        if task.cooling_mode not in {"unknown", ""}:
            return ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "对比笔记本散热模式",
                "散热模式",
                task.cooling_mode,
                "性能 / 狂暴模式",
                "当前散热模式未显示为性能档，但是否限制持续表现仍需结合测试结果。",
                "只对比散热模式后，可以判断它是否影响较长时间测试的帧率稳定性。",
                "只切换厂商控制中心中的散热模式，其他设置不变；先测试 1 次，结果不明确时最多再测 1 次。",
                "对比 1% Low 和测试后半段的帧率表现；本次只用于诊断，结束后恢复原模式。",
                f"恢复测试前的散热模式：{task.cooling_mode}。",
                round_number,
                setting_id="cooling_mode",
                purpose="diagnostic",
            )
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "确认测试时的散热模式",
            "散热模式",
            task.cooling_mode,
            "记录当前实际值",
            "散热模式可能影响长时间运行表现，但当前状态尚未确认。",
            "先记录实际模式，再结合温度、频率和帧率变化判断是否值得调整。",
            "查看厂商控制中心中的风扇或散热模式并记录；本步骤不切换设置，也不需要测试帧率。",
            "记录测试时的散热模式。",
            "本步骤不修改配置。",
            round_number,
            setting_id="cooling_mode",
        )
    if "laptop_graphics_mode" in codes and task.graphics_mode == "hybrid" and available("graphics_mode"):
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "对比显卡输出方式",
            "显卡输出模式",
            "混合输出",
            "独显直连",
            "混合输出可能增加显示路径开销，但仅凭模式本身不能确定它是当前限制。",
            "在相同场景对比独显直连与混合输出的结果，可以判断当前设备是否受显示路径影响。",
            "只切换显卡输出模式，按设备提示重启后使用同一场景先测 1 次；变化不明确时最多再测 1 次。",
            "观察帧率和 GPU 占用变化；本次仅用于诊断，不自动保留该模式。",
            "恢复混合输出并按设备要求重启。",
            round_number,
            setting_id="graphics_mode",
            purpose="diagnostic",
        )
    resolution_diagnostic_done = any(code.startswith("diagnostic_resolution_") for code in codes) or "resolution_flat" in codes or "resolution_response" in codes
    if "diagnostic_resolution_flat" in codes and "cpu_metrics_note" not in codes and available("cpu_metrics"):
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "补充 CPU 运行监控",
            "CPU 占用 / 温度 / 频率",
            "未记录",
            "记录测试时数据",
            "分辨率对比没有明显改变帧率，GPU 渲染负担可能不是主要限制，但这还不能确认 CPU 瓶颈。",
            "补充 CPU 占用、温度和频率记录，可以帮助区分 CPU 负载、温度限制及其他因素。",
            "使用监控工具在同一测试场景记录 CPU 总占用、最忙核心占用、温度和持续频率；填写观察到的数据。无需为了这一步更改游戏设置。",
            "补充一条测试期间的 CPU 监控记录；如无法采集，注明未采集。",
            "本步骤不修改配置。",
            round_number,
            setting_id="cpu_metrics",
        )
    if ("diagnostic_resolution_flat" in codes or "cpu_metrics_note" in codes) and available("cpu_background_tasks"):
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "检查后台程序对游戏的影响",
            "后台 CPU 负载",
            "按当前使用状态",
            "关闭非必要后台程序",
            "分辨率测试没有明显改变帧率，下一步可用低风险方式检查后台任务是否在争用处理器资源。",
            "如果后台任务影响了游戏运行，关闭非必要程序后 1% Low 或平均 FPS 可能改善。",
            "关闭非必要后台程序，其他设置和场景保持不变；先测 1 次筛选，出现改善迹象时再累计确认。",
            "同条件下平均 FPS 或 1% Low 有可靠提升，且没有其他设置变化。",
            "重新打开本轮前关闭的程序。",
            round_number,
            setting_id="cpu_background_tasks",
            purpose="optimization",
        )
    if task.game != "Counter-Strike 2" and (
        "high_gpu" in codes
        or "resolution_response" in codes
        or "diagnostic_resolution_response" in codes
        or ("cpu_metrics_note" in codes and not available("cpu_background_tasks"))
    ) and available("user_adjustment"):
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "选择通用优化方向并指定一项设置",
            "方向与单项设置",
            "由你填写当前值",
            "由你填写调整值",
            "当前游戏还没有专用设置规则；可以依据诊断结果选择一个通用方向，再指定一项你熟悉、可恢复的游戏设置。",
            "把诊断方向和玩家确认的单项设置分开记录，后续只比较这一项的效果，不臆造游戏菜单或设置值。",
            "按“优化方向 | 设置名称 | 当前值 | 调整后值”填写；方向从以下范围选择："
            + "、".join(GENERIC_OPTIMIZATION_DIRECTIONS)
            + "。每次只填写一项你能确认并恢复的设置。",
            "得到一项设置名称明确、原值和调整值明确且可以恢复的单变量计划。",
            "本步骤只登记用户选择，不修改任何设置。",
            round_number,
            setting_id="user_adjustment",
            purpose="optimization",
        )
    if "low_gpu" in codes and not resolution_diagnostic_done and available("resolution"):
        try:
            target_resolution = lower_resolution_same_aspect(current.resolution)
        except ValueError:
            target_resolution = None
        ratio_name = aspect_ratio_name(current.resolution)
        if target_resolution:
            return ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "降低分辨率进行对比",
                "分辨率",
                current.resolution,
                target_resolution,
                f"GPU 占用偏低，但单凭这一点无法确定原因。本次保持 {ratio_name} 画面比例。",
                "如果降低分辨率后帧率变化很小，GPU 渲染负担可能不是主要限制，但不能据此单独确认 CPU 瓶颈。",
                "只修改分辨率；使用同一场景，预热后先测试 1 次，没有明显下降时再补测到累计 3 次，每次至少 60 秒。",
                "累计 3 次后，平均 FPS 变化不超过 5%，且 GPU 占用仍低于 85%。",
                f"将分辨率恢复为 {current.resolution}。",
                round_number,
                setting_id="resolution",
                purpose="diagnostic",
            )
    if task.game == "Counter-Strike 2" and ("high_gpu" in codes or "resolution_response" in codes):
        levels = ("低", "中", "高", "非常高")
        dynamic_shadows = current.configuration.get("dynamic_shadows", "")
        if dynamic_shadows not in {"all", "sun_only"} and available("dynamic_shadows"):
            return ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "记录当前动态阴影",
                "动态阴影",
                "未知",
                "记录当前值",
                "当前 CS2 将动态阴影与全局阴影效果分开保存，必须分别记录才能保证单变量测试。",
                "记录动态阴影后，可以只降低全局阴影效果而不改变阴影显示范围。",
                "打开高级视频设置，查看动态阴影是“全部”还是“仅限日光”；这一步不用测试帧率。",
                "成功记录全部或仅限日光。",
                "本步骤不修改配置。",
                round_number,
                setting_id="dynamic_shadows",
            )
        old_value = current.configuration.get("shadow_quality", "")
        if old_value not in levels and available("shadow_quality"):
            return ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "记录当前阴影质量",
                "全局阴影效果",
                "未知",
                "记录当前值",
                "需要先知道当前设置，才能只改一项并在需要时恢复。",
                "记录当前阴影质量后，再决定是否降低一级。",
                "打开游戏设置，查看全局阴影效果并填写当前档位；这一步不用测试帧率。",
                "成功记录低、中、高或非常高中的一个档位。",
                "本步骤不修改配置。",
                round_number,
                setting_id="shadow_quality",
            )
        if old_value == levels[0] or not available("shadow_quality"):
            return ExperimentPlan(
                AgentAction.FINISH,
                "已无可用的阴影降档动作",
                "全局阴影效果",
                old_value,
                old_value,
                "阴影质量已处于最低档，当前规则库没有进一步的已支持调整。",
                "保留已验证配置，避免重复无效动作。",
                "汇总当前已验证结果；如需继续优化，应先扩充经过验证的设置规则。",
                "输出已有最佳配置，不宣称达到目标。",
                "无需恢复配置。",
                round_number,
                setting_id="shadow_quality",
            )
        new_value = levels[levels.index(old_value) - 1]
        return ExperimentPlan(
            AgentAction.RECOMMEND_CHANGE,
            "降低一级全局阴影效果",
            "全局阴影效果",
            old_value,
            new_value,
            "当前更可能受显卡限制，先降低对显卡压力较大的阴影质量。",
            "降低一级后，平均 FPS 应提高，且 1% Low 不应下降。",
            f"只把全局阴影效果降低一级；动态阴影保持“{'全部' if dynamic_shadows == 'all' else '仅限日光'}”，分辨率和测试场景保持不变，先测试 1 次，没有明显下降时再补测到累计 3 次。",
            "累计 3 次后平均 FPS 提高，1% Low 不下降，并且画质仍可接受。",
            f"将全局阴影效果恢复为{old_value}。",
            round_number,
            setting_id="shadow_quality",
        )
    if not available("repeats"):
        return ExperimentPlan(
            AgentAction.FINISH,
            "没有新的可验证设置",
            "测试重复次数",
            str(current.repeats),
            str(current.repeats),
            "已尝试的设置都没有留下新的可靠收益，避免继续重复同一动作。",
            "保留最近一组已验证配置，并把未确认结果明确标记出来。",
            "汇总当前已验证结果。",
            "输出已有最佳配置，不宣称达到性能目标。",
            "无需恢复配置。",
            round_number,
            setting_id="repeats",
        )
    return ExperimentPlan(
        AgentAction.FINISH,
        "当前没有新的可验证调整",
        "可验证的设置",
        "当前已记录配置",
        "等待更多可用证据或设置规则",
        "当前证据没有指向新的、安全且可验证的具体调整。",
        "先保留已有测试结论，补充新监控数据或适用的游戏设置规则后再继续。",
        "查看本轮诊断结果和已尝试动作；不要为了继续流程重复一项没有变化的测试。",
        "只报告有测试记录支持的结果，不宣称已经找到最优配置。",
        "无需恢复配置。",
        round_number,
        setting_id="no_supported_action",
    )


def plan_candidates(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    round_number: int,
    *,
    blocked_setting_ids: set[str] | None = None,
    limit: int = 10,
) -> list[ExperimentPlan]:
    """Build the safe action menu that the model may choose from.

    The local planner remains the source of executable plans, but the model
    can compare several independently available next steps instead of merely
    rephrasing a single fixed-priority recommendation.
    """
    if limit < 1:
        return []
    blocked = set(blocked_setting_ids or ())
    candidates: list[ExperimentPlan] = []
    seen: set[str] = set()
    for _ in range(limit):
        plan = plan_next_experiment(
            task,
            current,
            evidence,
            round_number,
            blocked_setting_ids=blocked,
        )
        if plan.action == AgentAction.FINISH:
            break
        identity = plan_candidate_id(plan)
        if identity not in seen:
            candidates.append(plan)
            seen.add(identity)
        setting_id = plan.setting_id or plan.setting
        if not setting_id or setting_id in blocked:
            break
        blocked.add(setting_id)
    return candidates


def gpu_optimization_candidates(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    round_number: int,
    game_settings: list[dict],
    *,
    blocked_setting_ids: set[str] | None = None,
) -> list[ExperimentPlan]:
    """Create one-step game-supported GPU options after evidence supports them."""
    if task.mode == "visual":
        return []
    gpu_supported = any(
        item.supports == "gpu"
        or item.code in {"high_gpu", "resolution_response", "diagnostic_resolution_response"}
        for item in evidence
    )
    if not gpu_supported:
        return []
    blocked = set(blocked_setting_ids or ())
    configuration = current.configuration or {}
    candidates: list[ExperimentPlan] = []
    for setting in game_settings:
        setting_id = str(setting.get("id", ""))
        order = setting.get("optimization_order")
        if (
            not setting_id
            or setting_id in blocked
            or not isinstance(order, list)
            or len(order) < 2
            or setting.get("primarily_affects") not in {"gpu", "vram"}
        ):
            continue
        old_value = str(configuration.get(setting_id, "")).strip()
        if setting_id == "shadow_quality" and not old_value:
            old_value = str(configuration.get("global_shadow_quality", "")).strip()
        if old_value not in order:
            continue
        index = order.index(old_value)
        if index + 1 >= len(order):
            continue
        new_value = str(order[index + 1])
        label = str(setting.get("label", setting_id))
        tradeoff = str(setting.get("visual_tradeoff", "画面细节"))
        display_values = setting.get("display_values", {})
        old_label = str(display_values.get(old_value, old_value)) if isinstance(display_values, dict) else old_value
        new_label = str(display_values.get(new_value, new_value)) if isinstance(display_values, dict) else new_value
        candidates.append(ExperimentPlan(
            action=AgentAction.RECOMMEND_CHANGE,
            title=f"验证{label}的一档调整",
            setting=label,
            old_value=old_value,
            new_value=new_value,
            rationale=f"当前证据支持检查 GPU 渲染负担；调整{label}可能降低相关负载，也可能影响{tradeoff}。",
            hypothesis=f"只调整{label}后，帧率可能改善；实际效果需要相同场景测试确认。",
            method=f"只将{label}从“{old_label}”调整到“{new_label}”，其他设置和场景保持不变；先测 1 次，有改善迹象时再累计确认。",
            success_criteria="测试条件一致、设置已确认，并且累计结果达到可靠收益门槛；画面代价仍可接受。",
            rollback=f"将{label}恢复为“{old_label}”。",
            round_number=round_number,
            setting_id=setting_id,
            purpose="optimization",
        ))
    return candidates


def plan_candidate_id(plan: ExperimentPlan) -> str:
    """Return a stable identifier for a concrete, validated candidate."""
    identity = "\x1f".join((
        plan.action.value,
        plan.setting_id or plan.setting,
        plan.old_value,
        plan.new_value,
        plan.purpose,
        str(plan.round_number),
        plan.reference_record_id,
        plan.diagnostic_record_id,
        json.dumps(plan.required_configuration, sort_keys=True),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
