"""Adaptadores AWS do modo eventos: automação SSM, rede de segurança no Scheduler e senha no SSM."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

from app.domain.event_mode import AutomationExecution, AutomationStatus

_RUNNING = {"Pending", "InProgress", "Waiting", "Scheduled", "RunbookInProgress", "PendingApproval", "Approved"}


class SSMClient(Protocol):
    def start_automation_execution(self, **kwargs: Any) -> Any: ...

    def get_automation_execution(self, **kwargs: Any) -> Any: ...

    def get_parameter(self, **kwargs: Any) -> Any: ...


class SchedulerClient(Protocol):
    def create_schedule(self, **kwargs: Any) -> Any: ...

    def update_schedule(self, **kwargs: Any) -> Any: ...

    def delete_schedule(self, **kwargs: Any) -> Any: ...


def automation_parameters(
    *, event_id: int, instance_id: str, instance_type: str, db_login: str, login_valid_until: datetime
) -> dict[str, list[str]]:
    return {
        "EventId": [str(event_id)],
        "InstanceId": [instance_id],
        "TargetInstanceType": [instance_type],
        "DbLogin": [db_login],
        "LoginValidUntil": [login_valid_until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")],
    }


class SsmEventAutomation:
    def __init__(
        self,
        client: SSMClient,
        *,
        activate_document: str,
        deactivate_document: str,
        instance_id: str,
        normal_instance_type: str,
        event_instance_type: str,
    ) -> None:
        self.client = client
        self.documents = {"activate": activate_document, "deactivate": deactivate_document}
        self.types = {"activate": event_instance_type, "deactivate": normal_instance_type}
        self.instance_id = instance_id

    def start(self, action: str, *, event_id: int, db_login: str, login_valid_until: datetime) -> str:
        response = self.client.start_automation_execution(
            DocumentName=self.documents[action],
            Parameters=automation_parameters(
                event_id=event_id,
                instance_id=self.instance_id,
                instance_type=self.types[action],
                db_login=db_login,
                login_valid_until=login_valid_until,
            ),
        )
        return response["AutomationExecutionId"]

    def get(self, execution_id: str) -> AutomationExecution:
        execution = self.client.get_automation_execution(AutomationExecutionId=execution_id)["AutomationExecution"]
        status = execution["AutomationExecutionStatus"]
        if status in _RUNNING:
            step = next(
                (
                    item["StepName"]
                    for item in execution.get("StepExecutions", [])
                    if item.get("StepStatus") in {"InProgress", "Pending", "Waiting"}
                ),
                None,
            )
            return AutomationExecution(AutomationStatus.RUNNING, current_step=step)
        if status == "Success":
            return AutomationExecution(AutomationStatus.SUCCESS)
        return AutomationExecution(
            AutomationStatus.FAILED,
            failure_message=(execution.get("FailureMessage") or f"Automação terminou como {status}.")[:1000],
        )


class SchedulerReturnGuard:
    """Agendamento único que executa o retorno caso a API não o faça no horário."""

    def __init__(
        self,
        client: SchedulerClient,
        *,
        group: str,
        role_arn: str,
        deactivate_document: str,
        instance_id: str,
        normal_instance_type: str,
    ) -> None:
        self.client = client
        self.group = group
        self.role_arn = role_arn
        self.deactivate_document = deactivate_document
        self.instance_id = instance_id
        self.normal_instance_type = normal_instance_type

    @staticmethod
    def name(event_id: int) -> str:
        return f"pa-sc-event-{event_id}-return-guard"

    def schedule(self, *, event_id: int, at: datetime, db_login: str) -> None:
        parameters = automation_parameters(
            event_id=event_id,
            instance_id=self.instance_id,
            instance_type=self.normal_instance_type,
            db_login=db_login,
            login_valid_until=at,
        )
        request = {
            "Name": self.name(event_id),
            "GroupName": self.group,
            "ScheduleExpression": f"at({at.astimezone(UTC):%Y-%m-%dT%H:%M:%S})",
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "ActionAfterCompletion": "DELETE",
            "Target": {
                "Arn": "arn:aws:scheduler:::aws-sdk:ssm:startAutomationExecution",
                "RoleArn": self.role_arn,
                "Input": json.dumps({"DocumentName": self.deactivate_document, "Parameters": parameters}),
            },
        }
        try:
            self.client.update_schedule(**request)
        except Exception as exc:
            if type(exc).__name__ != "ResourceNotFoundException" and "ResourceNotFound" not in str(exc):
                raise
            self.client.create_schedule(**request)

    def delete(self, event_id: int) -> None:
        try:
            self.client.delete_schedule(Name=self.name(event_id), GroupName=self.group)
        except Exception as exc:
            if "ResourceNotFound" not in type(exc).__name__ + str(exc):
                raise


class SsmEventSecrets:
    def __init__(self, client: SSMClient, prefix: str) -> None:
        self.client = client
        self.prefix = prefix.rstrip("/")

    def db_password(self, event_id: int) -> str | None:
        try:
            response = self.client.get_parameter(Name=f"{self.prefix}/{event_id}/db_password", WithDecryption=True)
        except Exception as exc:
            if "ParameterNotFound" in type(exc).__name__ + str(exc):
                return None
            raise
        return response["Parameter"]["Value"]
