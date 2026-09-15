from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any
from uuid import uuid4


class AgentAction(str, Enum):
    REQUEST_DATA = "request_data"
    PROPOSE_EXPERIMENT = "propose_experiment"
    RECOMMEND_CHANGE = "recommend_change"
    KEEP_CHANGE = "keep_change"
    REVERT_CHANGE = "revert_change"
    RETEST = "retest"
    FINISH = "finish"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class OptimizationTask:
    name: str
    game: str
    cpu: str
    gpu: str
    resolution: str
    refresh_rate: str
    test_scene: str
    target_avg_fps: float
    target_low_fps: float
    mode: str = "competitive"
    max_rounds: int = 4
    is_laptop: bool = False
    gpu_power_w: int | None = None
    graphics_mode: str = "unknown"
    power_mode: str = "unknown"
    cooling_mode: str = "unknown"
    task_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (self.name, self.game, self.cpu, self.gpu, self.resolution, self.test_scene)
        if any(not str(value).strip() for value in required):
            raise ValueError("任务名称、游戏、硬件、分辨率和测试场景不能为空")
        if not _finite_positive(self.target_avg_fps) or not _finite_positive(self.target_low_fps):
            raise ValueError("目标 FPS 必须大于 0")
        if self.target_low_fps > self.target_avg_fps:
            raise ValueError("目标 1% Low 不能高于目标平均 FPS")
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int) or not 1 <= self.max_rounds <= 20:
            raise ValueError("最大实验轮数必须在 1 到 20 之间")
        if self.gpu_power_w is not None and (
            isinstance(self.gpu_power_w, bool)
            or not isinstance(self.gpu_power_w, int)
            or not 20 <= self.gpu_power_w <= 250
        ):
            raise ValueError("笔记本 GPU 功耗应在 20W 到 250W 之间，未知时请留空")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BenchmarkRecord:
    avg_fps: float
    low_fps: float
    gpu_usage: float | None
    resolution: str
    test_scene: str
    repeats: int = 1
    duration_seconds: int | None = None
    comparable: bool = True
    notes: str = ""
    source: str = "manual"
    configuration: dict[str, str] = field(default_factory=dict)
    configuration_confirmed: bool = True
    # When present, these are the individual runs behind the summary values.
    # An empty list intentionally means that the record is a manually entered
    # summary and has no dispersion evidence attached to it.
    runs: list[dict[str, float]] = field(default_factory=list)
    record_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not _finite_positive(self.avg_fps) or not _finite_positive(self.low_fps):
            raise ValueError("平均 FPS 和 1% Low 必须大于 0")
        if self.low_fps > self.avg_fps * 1.05:
            raise ValueError("1% Low 不应高于平均 FPS")
        if self.gpu_usage is not None and (
            not _finite_number(self.gpu_usage) or not 0 <= self.gpu_usage <= 100
        ):
            raise ValueError("GPU 占用率必须在 0 到 100 之间")
        if isinstance(self.repeats, bool) or not isinstance(self.repeats, int) or self.repeats < 1:
            raise ValueError("重复次数至少为 1")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or self.duration_seconds <= 0
        ):
            raise ValueError("测试时长必须大于 0")
        if not isinstance(self.runs, list):
            raise ValueError("逐次测试数据必须是列表")
        for run in self.runs:
            if not isinstance(run, dict):
                raise ValueError("逐次测试数据必须是对象")
            for key in ("avg_fps", "low_fps"):
                if key not in run or not _finite_positive(run[key]):
                    raise ValueError("逐次测试的 FPS 必须是有限的正数")
            if run["low_fps"] > run["avg_fps"] * 1.05:
                raise ValueError("逐次测试的 1% Low 不应高于平均 FPS")
            if "gpu_usage" in run and run["gpu_usage"] is not None and (
                not _finite_number(run["gpu_usage"]) or not 0 <= run["gpu_usage"] <= 100
            ):
                raise ValueError("逐次测试的 GPU 占用率必须在 0 到 100 之间")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Evidence:
    code: str
    title: str
    detail: str
    weight: float
    supports: str

    def __post_init__(self) -> None:
        if not _finite_number(self.weight):
            raise ValueError("证据权重必须是有限数值")
        self.weight = max(0.0, min(float(self.weight), 1.0))


@dataclass(slots=True)
class ExperimentPlan:
    action: AgentAction
    title: str
    setting: str
    old_value: str
    new_value: str
    rationale: str
    hypothesis: str
    method: str
    success_criteria: str
    rollback: str
    round_number: int
    # Stable rule identifier.  Keep it optional for existing callers and
    # persisted plans that only contain the display label.
    setting_id: str = ""


@dataclass(slots=True)
class Decision:
    action: AgentAction
    summary: str
    avg_delta_pct: float = 0.0
    low_delta_pct: float = 0.0
    score_delta: float = 0.0
    needs_retest: bool = False
    reasons: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    # ``FINISH`` remains a compatibility action for the historical API.  New
    # callers should use these explicit fields to decide state transitions.
    accepted: bool = False
    task_complete: bool = False
    stop_reason: str = ""
    retry_same_experiment: bool = False
    rollback_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


def _finite_number(value: Any) -> bool:
    """Return whether *value* is a real, finite number (not bool)."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(float(value))


def _finite_positive(value: Any) -> bool:
    return _finite_number(value) and float(value) > 0
