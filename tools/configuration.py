from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from models.schemas import BenchmarkRecord
from tools.resolution import parse_resolution


CONFIGURATION_LABELS = {
    "cpu": "处理器", "gpu": "显卡", "resolution": "分辨率", "refresh_rate": "刷新率", "test_scene": "测试场景",
    "fps_cap": "测试时最高 FPS", "vsync": "测试时垂直同步", "display_mode": "显示模式",
    "shadow_quality": "全局阴影效果", "dynamic_shadows": "动态阴影", "msaa_mode": "抗锯齿模式",
    "texture_detail": "模型 / 贴图细节", "texture_filtering": "贴图过滤模式", "shader_detail": "光影细节",
    "particle_detail": "粒子细节", "ambient_occlusion": "环境光遮蔽", "hdr": "高动态范围", "fsr": "超级分辨率",
    "power_mode": "供电 / 性能模式", "cooling_mode": "散热模式", "graphics_mode": "显卡输出方式", "gpu_power_w": "GPU 功耗",
    "player_contrast": "增强角色对比度", "nvidia_gsync": "G-Sync", "nvidia_reflex": "Reflex 低延迟", "amd_anti_lag": "Anti-Lag",
    "menu_fps_cap": "菜单最高 FPS", "cpu_background_tasks": "后台 CPU 负载", "user_defined": "用户指定设置",
}


_UNKNOWN = {"", "unknown", "未确认", "不确定"}
_METADATA = {"planned_setting", "planned_setting_id", "planned_value", "actual_setting_value", "frame_limits"}
_ALIASES = {
    "vsync": {"已禁用": "disabled", "已关闭": "disabled", "已启用": "enabled", "已开启": "enabled"},
    "display_mode": {"全屏": "fullscreen", "全屏窗口模式": "borderless", "无边框窗口": "borderless", "窗口模式": "windowed"},
    "power_mode": {"接通电源 · 性能模式": "ac_performance", "接通电源 · 平衡模式": "ac_balanced", "电池供电": "battery"},
    "cooling_mode": {"性能 / 狂暴模式": "performance", "平衡模式": "balanced", "安静模式": "quiet"},
    "graphics_mode": {"独显直连": "dgpu", "混合输出": "hybrid", "自动切换": "advanced_optimus"},
    "dynamic_shadows": {"全部": "all", "仅限日光": "sun_only", "仅太阳": "sun_only"},
}


@lru_cache(maxsize=1)
def _game_display_aliases() -> dict[str, dict[str, str]]:
    path = Path(__file__).resolve().parent.parent / "knowledge" / "cs2.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")).get("settings", [])
    except (OSError, ValueError):
        return {}
    return {str(item["id"]): {str(label): str(value) for value, label in item.get("display_values", {}).items()}
            for item in settings if isinstance(item, dict) and "id" in item}


def canonical_value(key: str, value: object) -> str:
    text = str(value).strip()
    if key == "resolution":
        try:
            width, height = parse_resolution(text)
            return f"{width}x{height}"
        except ValueError:
            return text
    if text in {"未确认", "不确定"}:
        return "unknown"
    if key == "refresh_rate":
        return "".join(text.lower().split())
    return _ALIASES.get(key, {}).get(text, _game_display_aliases().get(key, {}).get(text, text))


def unexpected_configuration_changes(
    reference: BenchmarkRecord,
    candidate: BenchmarkRecord,
    setting_id: str = "",
) -> list[str]:
    """Check every known reference setting, including missing observations.

    New fields have no known previous value and cannot prove a change. The
    frame-limit diagnostic explicitly controls both FPS cap and V-Sync.
    """
    allowed = {setting_id}
    if setting_id == "frame_limits":
        allowed.update({"fps_cap", "vsync"})
    before = {**reference.configuration, "resolution": reference.resolution, "test_scene": reference.test_scene}
    after = {**candidate.configuration, "resolution": candidate.resolution, "test_scene": candidate.test_scene}
    return sorted(
        key for key, value in before.items()
        if key not in allowed | _METADATA
        and value is not None and str(value).strip().lower() not in _UNKNOWN
        and canonical_value(key, value) != canonical_value(key, after.get(key, "unknown"))
    )
