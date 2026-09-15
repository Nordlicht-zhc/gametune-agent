from __future__ import annotations

from models.schemas import AgentAction, BenchmarkRecord, Evidence, ExperimentPlan, OptimizationTask
from tools.resolution import aspect_ratio_name, lower_resolution_same_aspect


def plan_next_experiment(
    task: OptimizationTask,
    current: BenchmarkRecord,
    evidence: list[Evidence],
    round_number: int,
) -> ExperimentPlan:
    codes = {item.code for item in evidence}
    if "conditions_changed" in codes:
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "恢复可比测试条件",
            "测试环境",
            "存在差异",
            "与基线一致",
            "不可比数据不能用于保留或撤销配置。",
            "在同一场景和采集口径下复测后，才能评估性能变化。",
            "恢复基线场景、路线、供电模式和时长，连续测试 3 次。",
            "测试条件检查全部通过。",
            "无需更改游戏配置。",
            round_number,
            setting_id="test_conditions",
        )
    if "cap_unknown" in codes:
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "排除人为限帧",
            "帧率上限与垂直同步",
            "未确认",
            "关闭并记录",
            "限帧会同时造成低 GPU 占用和帧率不再增长。",
            "关闭限帧后若帧率上升，可先排除部分硬件瓶颈误判。",
            "记录原值，关闭游戏内及驱动级限帧和垂直同步，用相同场景测试 3 次。",
            "确认限帧状态，并得到三次可重复结果。",
            "按记录恢复原限帧和垂直同步设置。",
            round_number,
            setting_id="fps_cap",
        )
    if "laptop_power_unknown" in codes:
        return ExperimentPlan(
            AgentAction.REQUEST_DATA,
            "补充笔记本持续 GPU 功耗",
            "GPU 功耗",
            "未知",
            "记录监控均值",
            "同一 Laptop GPU 可采用不同功耗配置，仅有型号不足以判断实际性能释放。",
            "记录稳定负载阶段的持续 GPU 功耗后，可区分负载不足与功耗受限。",
            "接通电源并运行相同测试场景，记录稳定阶段的 GPU 功耗均值和峰值。",
            "获得测试期间的持续 GPU 功耗数据。",
            "本步骤不修改配置。",
            round_number,
            setting_id="gpu_power_w",
        )
    if "laptop_power_mode" in codes:
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "验证笔记本性能模式",
            "供电 / 性能模式",
            task.power_mode,
            "接通电源 · 性能模式",
            "电池或平衡策略可能限制 CPU/GPU 持续功耗。",
            "切换到厂商性能模式后，持续帧率和 1% Low 应有所改善。",
            "接通原装电源，仅切换系统及厂商控制中心的性能模式，再测试 3 次。",
            "平均 FPS 或 1% Low 的改善超过自然波动。",
            "恢复测试前的供电和性能模式。",
            round_number,
            setting_id="power_mode",
        )
    if "laptop_cooling" in codes:
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "验证笔记本散热模式",
            "散热模式",
            task.cooling_mode,
            "性能 / 狂暴模式",
            "安静风扇策略可能使持续功耗更早受到温度限制。",
            "提高散热策略后，长时间测试的后半段帧率应更稳定。",
            "仅切换厂商控制中心的散热模式，保持场景和其余设置不变并测试 3 次。",
            "1% Low 改善且测试后半段不再明显掉速。",
            "恢复原散热模式。",
            round_number,
            setting_id="cooling_mode",
        )
    if "laptop_graphics_mode" in codes and task.graphics_mode == "hybrid":
        return ExperimentPlan(
            AgentAction.PROPOSE_EXPERIMENT,
            "执行独显直连对照测试",
            "显卡输出模式",
            "混合输出",
            "独显直连",
            "混合输出链路可能影响 CPU 敏感的高帧率场景。",
            "若独显直连后帧率提高，可确认输出链路对当前场景存在影响。",
            "在厂商控制中心切换为独显直连，按提示重启后用相同场景测试 3 次。",
            "平均 FPS 或 1% Low 的改善超过自然波动。",
            "切回混合输出并按设备要求重启。",
            round_number,
            setting_id="graphics_mode",
        )
    if "low_gpu" in codes and "resolution_flat" not in codes:
        try:
            target_resolution = lower_resolution_same_aspect(current.resolution)
        except ValueError:
            target_resolution = None
        ratio_name = aspect_ratio_name(current.resolution)
        if target_resolution:
            return ExperimentPlan(
                AgentAction.PROPOSE_EXPERIMENT,
                "执行分辨率对照测试",
                "分辨率",
                current.resolution,
                target_resolution,
                f"GPU 占用偏低，但单一指标不足以证明 CPU 侧限制；对照分辨率保持 {ratio_name} 宽高比。",
                "若降低分辨率后平均 FPS 变化不超过 5%，更支持非 GPU 侧限制。",
                "仅改变分辨率；同一场景预热后测试 3 次，每次至少 60 秒。",
                "平均 FPS 变化不超过 5%，且 GPU 占用仍低于 85%。",
                f"将分辨率恢复为 {current.resolution}。",
                round_number,
                setting_id="resolution",
            )
    if "high_gpu" in codes or "resolution_response" in codes:
        levels = ("低", "中", "高", "非常高")
        old_value = current.configuration.get("shadow_quality", "")
        if old_value not in levels:
            return ExperimentPlan(
                AgentAction.REQUEST_DATA,
                "确认当前全局阴影质量",
                "全局阴影质量",
                "未知",
                "记录当前值",
                "先确认实际设置，才能生成单变量调整并准确恢复。",
                "记录阴影质量后，可选择一个明确的较低档位进行验证。",
                "查看游戏中的全局阴影质量，将当前档位填入实际配置；本步骤无需测试帧率。",
                "当前档位为低、中、高或非常高之一。",
                "本步骤不修改配置。",
                round_number,
                setting_id="shadow_quality",
            )
        if old_value == levels[0]:
            return ExperimentPlan(
                AgentAction.FINISH,
                "已无可用的阴影降档动作",
                "全局阴影质量",
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
            "降低一级全局阴影质量",
            "全局阴影质量",
            old_value,
            new_value,
            "现有证据更支持 GPU 侧限制，优先调整 GPU 成本高且竞技画面收益较低的项目。",
            "降低一级后平均 FPS 应明显提升，同时 1% Low 不退步。",
            "一次只降低一级，保持分辨率和测试场景不变，测试 3 次。",
            "综合得分提升且画质仍满足用户底线。",
            f"将全局阴影质量恢复为{old_value}。",
            round_number,
            setting_id="shadow_quality",
        )
    return ExperimentPlan(
        AgentAction.PROPOSE_EXPERIMENT,
        "补充稳定性测试",
        "测试重复次数",
        str(current.repeats),
        str(max(3, current.repeats + 2)),
        "现有证据不足以支持明确配置修改。",
        "重复测试可区分真实收益与自然波动。",
        f"相同场景连续测试 {max(3, current.repeats + 2)} 次，记录每次平均 FPS 与 1% Low。",
        "多次结果波动在 5% 以内。",
        "无需恢复配置。",
        round_number,
        setting_id="repeats",
    )
