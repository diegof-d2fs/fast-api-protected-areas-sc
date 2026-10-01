from __future__ import annotations

import json
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import bad_request
from app.domain.models import SubmissionDomain, SubmissionOperation


class BaseSubmissionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=255)
    legal_act_date: date | None = None
    reason: str | None = Field(default=None, max_length=1000)
    actor: str | None = Field(default=None, min_length=1, max_length=255)


class UcSubmissionMetadata(BaseSubmissionMetadata):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    official_identifier: str | None = Field(default=None, min_length=1, max_length=100)
    cd_cnuc: str | None = Field(default=None, min_length=1, max_length=50)
    wdpa_pid: str | None = Field(default=None, min_length=1, max_length=50)
    expected_version: int | None = Field(default=None, ge=1)


class OfficialZoneSubmissionMetadata(BaseSubmissionMetadata):
    uc_identifier: str = Field(min_length=1, max_length=100)
    valid_from: date | None = None


def parse_metadata(
    raw: str,
    domain: SubmissionDomain,
    operation: SubmissionOperation = SubmissionOperation.CREATE,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise bad_request(
            "INVALID_METADATA_JSON",
            "O campo metadata deve conter um objeto JSON válido.",
            violations=[{"field": "metadata", "rule": "valid_json", "message": str(exc)}],
        ) from exc
    if not isinstance(payload, dict):
        raise bad_request("INVALID_METADATA_TYPE", "O campo metadata deve ser um objeto JSON.")

    model = UcSubmissionMetadata if domain is SubmissionDomain.UC else OfficialZoneSubmissionMetadata
    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        violations = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "rule": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        raise bad_request(
            "INVALID_DOMAIN_METADATA",
            "Os metadados obrigatórios do domínio não foram informados corretamente.",
            violations=violations,
        ) from exc
    payload = validated.model_dump(mode="json", exclude_none=True)
    if operation in {
        SubmissionOperation.UPDATE,
        SubmissionOperation.REPLACE_POINT,
        SubmissionOperation.EXTINGUISH,
    }:
        if domain is not SubmissionDomain.UC:
            raise bad_request(
                "UPDATE_DOMAIN_NOT_ENABLED",
                "A mutação versionada está habilitada somente para o domínio `uc`.",
            )
        operation_label = operation.value
        error_code = (
            "INVALID_REPLACE_POINT_METADATA"
            if operation is SubmissionOperation.REPLACE_POINT
            else "INVALID_EXTINGUISH_METADATA"
            if operation is SubmissionOperation.EXTINGUISH
            else "INVALID_UPDATE_METADATA"
        )
        violations = []
        if payload.get("expected_version") is None:
            violations.append(
                {
                    "field": "expected_version",
                    "rule": f"required_for_{operation_label}",
                    "message": "Informe a versão atual observada antes de solicitar a atualização.",
                }
            )
        if not payload.get("reason"):
            violations.append(
                {
                    "field": "reason",
                    "rule": f"required_for_{operation_label}",
                    "message": "Informe a justificativa auditável da atualização.",
                }
            )
        if not any(payload.get(field) for field in ("official_identifier", "cd_cnuc", "wdpa_pid")):
            violations.append(
                {
                    "field": "official_identifier",
                    "rule": "strong_identity_required",
                    "message": (
                        "Informe official_identifier, cd_cnuc ou wdpa_pid para identificar a UC-alvo."
                    ),
                }
            )
        if violations:
            raise bad_request(
                error_code,
                "Os metadados da mutação versionada estão incompletos.",
                violations=violations,
            )
    if operation is SubmissionOperation.REPLACE_BUFFER_ABRANGENCIA:
        if domain is not SubmissionDomain.OFFICIAL_ZONE:
            raise bad_request(
                "REPLACE_BUFFER_ABRANGENCIA_DOMAIN_NOT_ENABLED",
                "A operação `replace_buffer_abrangencia` aceita somente o domínio `za_oficial`.",
            )
        if not payload.get("reason"):
            raise bad_request(
                "INVALID_REPLACE_BUFFER_ABRANGENCIA_METADATA",
                "Informe `reason` para auditar a substituição do Buffer de Abrangência pela ZA oficial.",
                violations=[
                    {
                        "field": "reason",
                        "rule": "required_for_replace_buffer_abrangencia",
                        "message": "Informe a justificativa legal ou administrativa da substituição.",
                    }
                ],
            )
    return payload
