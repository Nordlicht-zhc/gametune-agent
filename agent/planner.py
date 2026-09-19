from __future__ import annotations

from models.schemas import AgentAction, BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask
from tools.resolution import aspect_ratio_name, lower_resolution_same_aspect


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
    if "cap_unknown" in codes and available("fps_cap"):
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "确认并排除帧率限制",
            "游戏最高FPS 与 V-Sync",
            "未确认",
            "游戏最高FPS设为 0，V-Sync 设为已禁用",
            "限帧会让帧率停止增长，同时使 GPU 占用偏低。",
            "关闭限帧后再测试，可以避免把限帧误判为硬件瓶颈。",
            "先记录“游戏最高FPS”和“V-Sync”的原值；将游戏最高FPS设为 0、V-Sync 设为已禁用，再用相同场景先测试 1 次。没有明显下降时，再补测到累计 3 次。",
            "确认两个设置的实际值，并得到累计 3 次结果接近的测试。",
            "按记录恢复游戏最高FPS和 V-Sync。",
            round_number,
            setting_id="fps_cap",
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
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "开启笔记本性能模式",
            "供电 / 性能模式",
            power_mode_label,
            "接通电源 · 性能模式",
            "电池供电或平衡模式可能限制 CPU 和 GPU 性能。",
            "切换到性能模式后，平均 FPS 或 1% Low 可能提高。",
            "接通电源，只把系统和厂商控制中心切换为性能模式，先测试 1 次；没有明显下降时再补测到累计 3 次。",
            "累计 3 次后，平均 FPS 或 1% Low 有可靠提升，或确认没有明显退步。",
            "恢复测试前的供电和性能模式。",
            round_number,
            setting_id="power_mode",
        )
    if "laptop_cooling" in codes and available("cooling_mode"):
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "切换到高性能散热模式",
            "散热模式",
            task.cooling_mode,
            "性能 / 狂暴模式",
            "安静模式可能让设备更早因温度而降频。",
            "提高风扇转速后，长时间游戏的帧率可能更稳定。",
            "只修改厂商控制中心的散热模式，其他设置不变，先测试 1 次；没有明显下降时再补测到累计 3 次。",
            "累计 3 次后，1% Low 有可靠提升，且测试后半段没有明显掉速。",
            "恢复原散热模式。",
            round_number,
            setting_id="cooling_mode",
        )
    if "laptop_graphics_mode" in codes and task.graphics_mode == "hybrid" and available("graphics_mode"):
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "测试独显直连",
            "显卡输出模式",
            "混合输出",
            "独显直连",
            "混合输出可能降低高帧率游戏的性能。",
            "如果独显直连后帧率提高，说明混合输出影响了当前游戏。",
            "在厂商控制中心切换为独显直连，按提示重启，再用相同场景先测试 1 次；没有明显下降时再补测到累计 3 次。",
            "累计 3 次后，平均 FPS 或 1% Low 有可靠提升。",
            "切回混合输出并按设备要求重启。",
            round_number,
            setting_id="graphics_mode",
        )
    if "low_gpu" in codes and "resolution_flat" not in codes and available("resolution"):
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
                f"GPU 占用偏低，但仅凭这一点不能确定 CPU 瓶颈。本次保持 {ratio_name} 画面比例。",
                "如果降低分辨率后帧率变化很小，瓶颈更可能不在显卡。",
                "只修改分辨率；使用同一场景，预热后先测试 1 次，没有明显下降时再补测到累计 3 次，每次至少 60 秒。",
                "累计 3 次后，平均 FPS 变化不超过 5%，且 GPU 占用仍低于 85%。",
                f"将分辨率恢复为 {current.resolution}。",
                round_number,
                setting_id="resolution",
            )
    if "high_gpu" in codes or "resolution_response" in codes:
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
        AgentAction.PROPOSE_EXPERIMENT,
        "先筛选，再确认稳定性",
        "测试重复次数",
        str(current.repeats),
        "先测 1 次，累计最多 3 次",
        "当前数据还不足以确定应该修改哪项设置。",
        "先用 1 次测试排除明显退步，再用累计 3 次结果确认是否可靠。",
        "使用相同场景先测试 1 次；没有明显下降时，再补测到累计 3 次并记录每次平均 FPS 和 1% Low。",
        "累计 3 次结果的差异不超过 5%。",
        "无需恢复配置。",
        round_number,
        setting_id="repeats",
    )
