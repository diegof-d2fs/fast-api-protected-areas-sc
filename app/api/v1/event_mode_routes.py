from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response

from app.api.dependencies import require_role
from app.application.event_mode import EventModeService
from app.core.errors import AppError
from app.domain.auth import CurrentUser, UserRole
from app.domain.event_mode import (
    ActivateEventRequest,
    DeactivateEventRequest,
    EventCredentials,
    EventModeState,
    EventView,
    ExtendEventRequest,
)
from app.domain.models import ProblemDetail

router = APIRouter(prefix="/api/v1/admin/event-mode", tags=["Modo eventos"])

AdminUser = Annotated[CurrentUser, Depends(require_role(UserRole.ADMIN))]
EventId = Annotated[int, Path(gt=0, description="Identificador do evento.")]


def get_event_mode_service(request: Request) -> EventModeService:
    service = getattr(request.app.state, "event_mode_service", None)
    if service is None:
        raise AppError(
            503,
            "EVENT_MODE_NOT_CONFIGURED",
            "Modo eventos indisponível",
            "Configure PA_SC_DATABASE_DSN para utilizar o modo eventos.",
        )
    return service


Service = Annotated[EventModeService, Depends(get_event_mode_service)]
_ERRORS = {
    400: {"model": ProblemDetail, "description": "Confirmação ausente ou horários inválidos."},
    403: {"model": ProblemDetail, "description": "Exige papel de administrador."},
    409: {"model": ProblemDetail, "description": "Estado do evento não permite a operação."},
    503: {"model": ProblemDetail, "description": "Automação não configurada neste ambiente."},
}


@router.get(
    "",
    response_model=EventModeState,
    summary="Consultar o modo eventos",
    description="Estado do evento aberto (reconciliado com a automação), regras, tempos estimados e custo.",
    operation_id="get_event_mode",
    responses=_ERRORS,
)
def get_state(_admin: AdminUser, service: Service) -> EventModeState:
    return service.state()


@router.get(
    "/history",
    response_model=list[EventView],
    summary="Listar eventos anteriores",
    operation_id="list_event_mode_history",
    responses=_ERRORS,
)
def history(_admin: AdminUser, service: Service) -> list[EventView]:
    return service.history()


@router.post(
    "",
    response_model=EventView,
    status_code=202,
    summary="Ativar ou agendar o modo eventos",
    description=(
        "Sem `inicio`, amplia o servidor agora: o sistema fica cerca de 2 minutos fora do ar. Com `inicio`, "
        "agenda a ampliação. O retorno ao tamanho normal é sempre automático no horário definido. "
        'Exige `confirmacao` igual a "ATIVAR" e o cabeçalho `Idempotency-Key`.'
    ),
    operation_id="activate_event_mode",
    responses=_ERRORS,
)
def activate(
    admin: AdminUser,
    service: Service,
    payload: ActivateEventRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> EventView:
    return service.activate(admin, payload, idempotency_key)


@router.post(
    "/{event_id}/extend",
    response_model=EventView,
    summary="Mudar o horário do retorno automático",
    operation_id="extend_event_mode",
    responses=_ERRORS,
)
def extend(admin: AdminUser, service: Service, event_id: EventId, payload: ExtendEventRequest) -> EventView:
    return service.extend(admin, event_id, payload.retorno_automatico_em)


@router.post(
    "/{event_id}/deactivate",
    response_model=EventView,
    status_code=202,
    summary="Desativar o modo eventos",
    description=(
        "Volta o servidor ao tamanho normal (cerca de 2 minutos fora do ar). Só é liberado depois do tempo "
        'mínimo de permanência; antes disso responde 409 com `Retry-After`. Exige `confirmacao` igual a "DESATIVAR".'
    ),
    operation_id="deactivate_event_mode",
    responses=_ERRORS,
)
def deactivate(
    admin: AdminUser, service: Service, event_id: EventId, payload: DeactivateEventRequest
) -> EventView:
    return service.deactivate(admin, event_id, payload.confirmacao)


@router.post(
    "/{event_id}/cancel",
    response_model=EventView,
    summary="Cancelar um evento agendado",
    operation_id="cancel_event_mode",
    responses=_ERRORS,
)
def cancel(admin: AdminUser, service: Service, event_id: EventId) -> EventView:
    return service.cancel(admin, event_id)


@router.get(
    "/{event_id}/credentials",
    response_model=EventCredentials,
    summary="Ler o login de banco do evento",
    description="Login somente leitura criado para os participantes. Cada leitura fica registrada na auditoria.",
    operation_id="get_event_mode_credentials",
    responses=_ERRORS,
)
def credentials(admin: AdminUser, service: Service, event_id: EventId, response: Response) -> EventCredentials:
    response.headers["Cache-Control"] = "no-store"
    return service.credentials(admin, event_id)
