from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkflowState(str, Enum):
    WAITING_INPUT = "waiting_input"
    VALIDATING = "validating"
    ANALYZING = "analyzing"
    AWAITING_TEST = "awaiting_test"
    AWAITING_ROLLBACK = "awaiting_rollback"
    EVALUATING = "evaluating"
    COMPLETE = "complete"


class WorkflowPhase(str, Enum):
    DIAGNOSIS = "diagnosis"
    OPTIMIZATION = "optimization"


ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.WAITING_INPUT: {WorkflowState.VALIDATING},
    WorkflowState.VALIDATING: {WorkflowState.ANALYZING, WorkflowState.WAITING_INPUT},
    WorkflowState.ANALYZING: {WorkflowState.AWAITING_TEST, WorkflowState.COMPLETE},
    WorkflowState.AWAITING_TEST: {WorkflowState.EVALUATING, WorkflowState.COMPLETE},
    WorkflowState.EVALUATING: {
        WorkflowState.AWAITING_TEST,
        WorkflowState.AWAITING_ROLLBACK,
        WorkflowState.COMPLETE,
    },
    WorkflowState.AWAITING_ROLLBACK: {WorkflowState.AWAITING_TEST, WorkflowState.COMPLETE},
    WorkflowState.COMPLETE: set(),
}


@dataclass(slots=True)
class Workflow:
    state: WorkflowState = WorkflowState.WAITING_INPUT
    round_number: int = 0
    phase: WorkflowPhase = WorkflowPhase.DIAGNOSIS

    def transition(self, target: WorkflowState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"非法状态转换：{self.state.value} -> {target.value}")
        self.state = target

    def start(self) -> None:
        self.transition(WorkflowState.VALIDATING)

    def validated(self) -> None:
        self.transition(WorkflowState.ANALYZING)

    def wait_for_test(self) -> None:
        self.transition(WorkflowState.AWAITING_TEST)
        self.round_number += 1

    def set_phase(self, purpose: str, *, action: str = "", setting_id: str = "") -> None:
        diagnostic_inputs = {
            "fps_cap", "vsync", "gpu_power_w", "graphics_mode", "power_mode",
            "cooling_mode", "cpu_metrics", "test_conditions",
        }
        is_diagnostic = (
            setting_id in diagnostic_inputs
            if action == "request_data"
            else purpose == "diagnostic"
        )
        self.phase = WorkflowPhase.DIAGNOSIS if is_diagnostic else WorkflowPhase.OPTIMIZATION

    def begin_evaluation(self) -> None:
        self.transition(WorkflowState.EVALUATING)

    def continue_testing(self, purpose: str | None = None, *, action: str = "", setting_id: str = "") -> None:
        self.transition(WorkflowState.AWAITING_TEST)
        self.round_number += 1
        if purpose is not None:
            self.set_phase(purpose, action=action, setting_id=setting_id)

    def retest(self) -> None:
        """Retry the same experiment without advancing its optimization round."""
        self.transition(WorkflowState.AWAITING_TEST)

    def request_rollback(self) -> None:
        """Pause until the user confirms restoration of the prior configuration."""
        self.transition(WorkflowState.AWAITING_ROLLBACK)

    def confirm_rollback(self, *, continue_testing: bool = True) -> None:
        """Finish a rollback or move to the next optimization round."""
        if self.state != WorkflowState.AWAITING_ROLLBACK:
            raise ValueError(f"当前状态不能确认回滚：{self.state.value}")
        if continue_testing:
            self.transition(WorkflowState.AWAITING_TEST)
            self.round_number += 1
        else:
            self.transition(WorkflowState.COMPLETE)

    def finish(self) -> None:
        self.transition(WorkflowState.COMPLETE)

    def complete_without_test(self) -> None:
        """Complete when planning has no remaining supported experiment."""
        self.transition(WorkflowState.COMPLETE)
