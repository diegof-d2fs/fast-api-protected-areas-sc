from __future__ import annotations

import json
from datetime import UTC, datetime

from app.domain.event_mode import AutomationStatus
from app.infrastructure.compute.event_mode_aws import (
    SchedulerReturnGuard,
    SsmEventAutomation,
    SsmEventSecrets,
)


class ResourceNotFoundException(Exception):
    pass


class ParameterNotFound(Exception):
    pass


class FakeSsm:
    def __init__(self, execution: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.execution = execution or {}

    def start_automation_execution(self, **kwargs):
        self.calls.append(kwargs)
        return {"AutomationExecutionId": "exec-123"}

    def get_automation_execution(self, **kwargs):
        return {"AutomationExecution": self.execution}

    def get_parameter(self, **kwargs):
        raise ParameterNotFound("not found")


class FakeScheduler:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def update_schedule(self, **kwargs):
        raise ResourceNotFoundException("Schedule not found")

    def create_schedule(self, **kwargs):
        self.created.append(kwargs)

    def delete_schedule(self, **kwargs):
        raise ResourceNotFoundException("Schedule not found")


def _automation(ssm: FakeSsm) -> SsmEventAutomation:
    return SsmEventAutomation(
        ssm,
        activate_document="pa-sc-event-mode-activate",
        deactivate_document="pa-sc-event-mode-deactivate",
        instance_id="i-serving",
        normal_instance_type="t3a.small",
        event_instance_type="t3a.large",
    )


def test_start_passes_the_target_type_and_login_to_the_document() -> None:
    ssm = FakeSsm()
    until = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)

    execution_id = _automation(ssm).start("activate", event_id=7, db_login="ws_univali_20261020", login_valid_until=until)

    call = ssm.calls[0]
    assert execution_id == "exec-123"
    assert call["DocumentName"] == "pa-sc-event-mode-activate"
    assert call["Parameters"]["TargetInstanceType"] == ["t3a.large"]
    assert call["Parameters"]["LoginValidUntil"] == ["2026-10-21T16:00:00Z"]


def test_execution_status_maps_running_step_success_and_failure() -> None:
    running = FakeSsm(
        {
            "AutomationExecutionStatus": "InProgress",
            "StepExecutions": [
                {"StepName": "pararServidor", "StepStatus": "Success"},
                {"StepName": "trocarTamanho", "StepStatus": "InProgress"},
            ],
        }
    )
    assert _automation(running).get("x").current_step == "trocarTamanho"
    assert _automation(FakeSsm({"AutomationExecutionStatus": "Success"})).get("x").status is AutomationStatus.SUCCESS
    failed = _automation(FakeSsm({"AutomationExecutionStatus": "TimedOut"})).get("x")
    assert failed.status is AutomationStatus.FAILED
    assert "TimedOut" in failed.failure_message


def test_return_guard_creates_a_one_time_schedule_when_absent() -> None:
    scheduler = FakeScheduler()
    guard = SchedulerReturnGuard(
        scheduler,
        group="pa-sc-event-mode",
        role_arn="arn:aws:iam::1:role/scheduler",
        deactivate_document="pa-sc-event-mode-deactivate",
        instance_id="i-serving",
        normal_instance_type="t3a.small",
    )

    guard.schedule(event_id=7, at=datetime(2026, 10, 20, 20, 30, tzinfo=UTC), db_login="ws_univali_20261020")
    guard.delete(7)

    request = scheduler.created[0]
    assert request["ScheduleExpression"] == "at(2026-10-20T20:30:00)"
    assert request["ActionAfterCompletion"] == "DELETE"
    payload = json.loads(request["Target"]["Input"])
    assert payload["DocumentName"] == "pa-sc-event-mode-deactivate"
    assert payload["Parameters"]["TargetInstanceType"] == ["t3a.small"]


def test_missing_password_returns_none() -> None:
    assert SsmEventSecrets(FakeSsm(), "/pa-sc/prod/event-mode/").db_password(7) is None
