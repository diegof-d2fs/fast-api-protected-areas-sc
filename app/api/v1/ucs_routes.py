from __future__ import annotations

import json
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, UploadFile, status
from starlette.datastructures import Headers

from app.api.dependencies import get_current_user, get_submission_service, get_uc_service
from app.application.submissions import SubmissionService
from app.application.ucs import UCService
from app.core.errors import AppError
from app.domain.auth import CurrentUser
from app.domain.models import (
    DuplicatePolicy,
    ProblemDetail,
    SubmissionDomain,
    SubmissionOperation,
    SubmissionView,
)
from app.domain.ucs import BufferAbrangenciaView, UCDetailView, UCExtinguishRequest, UCSearchView, UCStatus

router = APIRouter(
    prefix="/api/v1/ucs",
    tags=["Unidades de Conservação"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "",
    response_model=list[UCSearchView],
    summary="Buscar uma UC por identificador forte",
    description=(
        "Busca somente leitura por código CNUC ou WDPA PID. Informe exatamente um filtro. "
        "A resposta contém zero ou uma UC e expõe a versão vigente necessária às atualizações "
        "com controle de concorrência."
    ),
    operation_id="search_ucs",
    responses={
        400: {"model": ProblemDetail, "description": "Quantidade de filtros inválida."},
        503: {"model": ProblemDetail, "description": "PostGIS não configurado ou indisponível."},
    },
)
def search_ucs(
    cd_cnuc: Annotated[
        str | None,
        Query(max_length=100, description="Código CNUC exato da UC."),
    ] = None,
    wdpa_pid: Annotated[
        str | None,
        Query(max_length=100, description="WDPA PID exato da UC."),
    ] = None,
    service: UCService = Depends(get_uc_service),
) -> list[UCSearchView]:
    return service.search(cd_cnuc=cd_cnuc, wdpa_pid=wdpa_pid)


@router.get(
    "/{uc_id}",
    response_model=UCDetailView,
    summary="Consultar uma UC processada pelo pipeline",
    description=(
        "Consulta, em modo somente leitura, uma UC que já foi materializada no PostGIS pela DAG_UCS. "
        "Esta rota não altera dados cadastrais."
    ),
    operation_id="get_uc",
    responses={
        404: {"model": ProblemDetail, "description": "UC não encontrada."},
        503: {"model": ProblemDetail, "description": "PostGIS não configurado ou indisponível."},
    },
)
def get_uc(
    uc_id: Annotated[int, Path(gt=0, description="Identificador numérico da UC no PostGIS.")],
    service: UCService = Depends(get_uc_service),
) -> UCDetailView:
    return service.get(uc_id)


@router.post(
    "/{uc_id}/extinguish",
    response_model=SubmissionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Solicitar a extinção lógica de uma UC",
    description=(
        "Cria uma importação dirigida `uc/extinguish` a partir da fotografia vigente da UC. "
        "A API apenas lê o cadastro e publica o comando; a mudança para EXTINTA, o encerramento "
        "da zona ativa e a auditoria pertencem à transação do Airflow."
    ),
    operation_id="extinguish_uc",
    responses={
        404: {"model": ProblemDetail, "description": "UC não encontrada."},
        409: {"model": ProblemDetail, "description": "UC já extinta ou versão divergente."},
    },
)
async def extinguish_uc(
    request: Request,
    uc_id: Annotated[int, Path(gt=0, description="Identificador numérico da UC no PostGIS.")],
    command: UCExtinguishRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
    uc_service: UCService = Depends(get_uc_service),
    submission_service: SubmissionService = Depends(get_submission_service),
    current_user: CurrentUser = Depends(get_current_user),
) -> SubmissionView:
    previous = submission_service.repository.find_by_idempotency_key(idempotency_key)
    command_identity = {"uc_id": uc_id, **command.model_dump()}
    if previous is not None:
        if (
            previous.operation is not SubmissionOperation.EXTINGUISH
            or previous.metadata_payload.get("extinguish_command") != command_identity
            or previous.metadata_payload.get("actor_id") != current_user.id
        ):
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Chave reutilizada",
                           "A chave já identifica outra intenção.")
        return SubmissionView.from_submission(
            submission_service.get_with_pipeline_state(previous.submission_id)
        )
    target = uc_service.get(uc_id)
    if target.status is UCStatus.EXTINCT:
        raise AppError(
            409,
            "UC_ALREADY_EXTINCT",
            "UC já extinta",
            "A unidade de conservação já está extinta; nenhuma nova intenção foi criada.",
        )
    if target.geometry_version != command.expected_version:
        raise AppError(
            409,
            "UC_VERSION_CONFLICT",
            "Versão da UC divergente",
            f"A UC está na versão {target.geometry_version}, mas a solicitação esperava "
            f"a versão {command.expected_version}.",
        )
    metadata: dict[str, object] = {
        "source": command.source,
        "reason": command.reason,
        "name": target.name,
        "expected_version": command.expected_version,
        "actor": f"user:{current_user.id}:{current_user.nome}",
        "actor_id": current_user.id,
        "extinguish_command": command_identity,
    }
    for field, value in (
        ("official_identifier", target.official_identifier),
        ("cd_cnuc", target.cnuc_code),
        ("wdpa_pid", target.wdpa_pid),
    ):
        if value:
            metadata[field] = value
    if not any(metadata.get(field) for field in ("official_identifier", "cd_cnuc", "wdpa_pid")):
        raise AppError(
            409,
            "UC_STRONG_IDENTITY_MISSING",
            "UC sem identificador forte",
            "A UC não pode ser extinta até possuir uc_id, cd_cnuc ou wdpa_pid.",
        )
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {"nm_uc": target.name},
                    "geometry": target.geometry,
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    upload = UploadFile(
        file=BytesIO(payload),
        filename=f"uc-{uc_id}-extinguish.geojson",
        headers=Headers({"content-type": "application/geo+json"}),
    )
    submission = await submission_service.create(
        upload=upload,
        domain=SubmissionDomain.UC,
        operation=SubmissionOperation.EXTINGUISH,
        duplicate_policy=DuplicatePolicy.REJECT_BATCH,
        metadata=metadata,
        repair_geometry=False,
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    return SubmissionView.from_submission(submission)


@router.get(
    "/{uc_id}/buffer-abrangencia",
    response_model=list[BufferAbrangenciaView],
    summary="Consultar os Buffers de Abrangência de uma UC",
    description=(
        "Consulta os Buffers de Abrangência gerados pela DAG_ZA_BUFFER. "
        "A API não calcula nem grava buffers diretamente."
    ),
    operation_id="list_uc_buffer_abrangencia",
    responses={
        404: {"model": ProblemDetail, "description": "UC não encontrada."},
        503: {"model": ProblemDetail, "description": "PostGIS não configurado ou indisponível."},
    },
)
def list_uc_buffer_abrangencia(
    uc_id: Annotated[int, Path(gt=0, description="Identificador numérico da UC no PostGIS.")],
    service: UCService = Depends(get_uc_service),
) -> list[BufferAbrangenciaView]:
    return service.list_buffer_abrangencia(uc_id)
