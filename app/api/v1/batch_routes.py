from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from app.api.dependencies import get_current_user, get_submission_service
from app.application.submissions import SubmissionService
from app.core.errors import AppError
from app.domain.auth import CurrentUser
from app.domain.models import SubmissionDomain, SubmissionOperation, SubmissionStatus, SubmissionView

router = APIRouter(prefix="/api/v1/import-batches", tags=["Importações geoespaciais"],
                   dependencies=[Depends(get_current_user)])


class BatchRequest(BaseModel):
    uc_import_id: str
    official_zone_import_id: str


def view(item) -> dict:
    result = SubmissionView.from_submission(item).model_dump(mode="json")
    result["batch_id"] = item.submission_id
    result["links"] = {"self": f"/api/v1/import-batches/{item.submission_id}",
                       "publish": f"/api/v1/import-batches/{item.submission_id}/publish"}
    return result


@router.post("", status_code=202, summary="Agrupar uma UC e sua ZA oficial em lote atômico")
def create_batch(
    request: Request,
    payload: BatchRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    service: SubmissionService = Depends(get_submission_service),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    service._validate_idempotency_key(idempotency_key)
    fingerprint = hashlib.sha256(
        json.dumps({**payload.model_dump(), "actor_id": user.id}, sort_keys=True).encode()
    ).hexdigest()
    previous = service.repository.find_by_idempotency_key(idempotency_key)
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Chave reutilizada", "A chave identifica outro pedido.")
        return view(previous)
    uc = service.get(payload.uc_import_id)
    za = service.get(payload.official_zone_import_id)
    if not (
        uc.domain is SubmissionDomain.UC and za.domain is SubmissionDomain.OFFICIAL_ZONE
        and uc.operation is SubmissionOperation.CREATE and za.operation is SubmissionOperation.CREATE
        and uc.status is SubmissionStatus.ACCEPTED and za.status is SubmissionStatus.ACCEPTED
        and uc.validation and za.validation
        and uc.validation.feature_count == za.validation.feature_count == 1
    ):
        raise AppError(409, "INVALID_BATCH_MEMBERS", "Membros inválidos",
                       "O lote exige uma UC e uma ZA, cada uma com uma feição aceita em create.")
    canonical = json.loads(service.storage.resolve(uc.canonical_key).read_text(encoding="utf-8"))
    properties = {str(k).lower(): v for k, v in canonical["features"][0].get("properties", {}).items()}
    identifiers = {
        str(value) for value in [
            uc.metadata_payload.get("official_identifier"), uc.metadata_payload.get("cd_cnuc"),
            uc.metadata_payload.get("wdpa_pid"), properties.get("uc_id"), properties.get("cd_cnuc"),
            properties.get("wdpa_pid"), uc.submission_id,
        ] if value
    }
    if za.metadata_payload["uc_identifier"] not in identifiers:
        raise AppError(409, "BATCH_UC_IDENTITY_MISMATCH", "Vínculo divergente",
                       "O identificador da ZA deve corresponder à UC deste lote.")
    batch_id = str(uuid4())
    now = datetime.now(UTC)
    item = uc.model_copy(update={
        "submission_id": batch_id, "operation": SubmissionOperation.CREATE_WITH_ZONE,
        "idempotency_key": idempotency_key, "request_fingerprint": fingerprint,
        "correlation_id": request.state.correlation_id, "created_at": now, "updated_at": now,
        "manifest_key": f"batches/{batch_id}/manifest.json",
        "metadata_payload": {
            **uc.metadata_payload,
            "official_identifier": uc.metadata_payload.get("official_identifier") or properties.get("uc_id") or uc.submission_id,
            "actor": f"user:{user.id}:{user.nome}",
            "batch_members": payload.model_dump(),
            "official_zone": {
                "canonical_key": za.canonical_key, "original_key": za.original_key,
                "checksum_sha256": za.checksum_sha256, "metadata": za.metadata_payload,
            },
        },
    })
    service.storage.write_json(item.manifest_key, service._manifest(item))
    item = service.repository.create_batch(item, [uc.submission_id, za.submission_id])
    return view(item)


def get_batch_item(service: SubmissionService, batch_id: str):
    item = service.get(batch_id)
    if item.operation is not SubmissionOperation.CREATE_WITH_ZONE:
        raise AppError(404, "BATCH_NOT_FOUND", "Lote não encontrado", "O identificador não pertence a um lote.")
    return item


@router.get("/{batch_id}", summary="Consultar lote UC+ZA")
def get_batch(batch_id: str, service: SubmissionService = Depends(get_submission_service)) -> dict:
    get_batch_item(service, batch_id)
    return view(service.get_with_pipeline_state(batch_id))


@router.post("/{batch_id}/publish", status_code=202, summary="Publicar lote UC+ZA")
def publish_batch(batch_id: str, service: SubmissionService = Depends(get_submission_service)) -> dict:
    get_batch_item(service, batch_id)
    return view(service.publish(batch_id))
