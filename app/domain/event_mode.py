"""Modo eventos: ampliação temporária do nó de serviço para oficinas com muitos acessos.

A API roda no próprio nó que é ampliado. Por isso ela só registra a intenção e entrega a troca de
tamanho a uma automação do AWS Systems Manager, que roda fora da máquina; o estado vive no banco e
sobrevive à parada do nó.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class EventStatus(StrEnum):
    SCHEDULED = "agendado"
    ACTIVATING = "ativando"
    ACTIVE = "ativo"
    DEACTIVATING = "desativando"
    FINISHED = "encerrado"
    CANCELLED = "cancelado"
    FAILED = "falhou"


OPEN_STATUSES = frozenset(
    {EventStatus.SCHEDULED, EventStatus.ACTIVATING, EventStatus.ACTIVE, EventStatus.DEACTIVATING}
)


class AutomationStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutomationExecution:
    status: AutomationStatus
    current_step: str | None = None
    failure_message: str | None = None


@dataclass(slots=True)
class EventRecord:
    id: int
    nome: str
    status: EventStatus
    db_login: str
    starts_at: datetime
    auto_return_at: datetime
    requested_by: int
    requested_by_name: str
    created_at: datetime
    version: int = 1
    activated_at: datetime | None = None
    deactivate_allowed_at: datetime | None = None
    finished_at: datetime | None = None
    execution_id: str | None = None
    current_step: str | None = None
    failure_message: str | None = None
    idempotency_key: str | None = None


# Horário de Brasília sem horário de verão desde 2019; evita depender da base tzdata na imagem.
BRASILIA = timezone(timedelta(hours=-3))


def event_login(nome: str, starts_at: datetime) -> str:
    """Login de banco do evento: prefixo fixo `ws_`, instituição normalizada e data em Brasília."""
    ascii_name = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")[:30] or "evento"
    return f"ws_{slug}_{starts_at.astimezone(BRASILIA):%Y%m%d}"


class ActivateEventRequest(BaseModel):
    nome: str = Field(min_length=2, max_length=80, description="Instituição ou nome do evento.")
    inicio: datetime | None = Field(
        default=None, description="Início agendado com fuso; vazio ativa agora."
    )
    retorno_automatico_em: datetime | None = Field(
        default=None, description="Horário do retorno automático, com fuso; vazio usa o padrão."
    )
    confirmacao: str = Field(description='Precisa ser exatamente "ATIVAR".')


class ExtendEventRequest(BaseModel):
    retorno_automatico_em: datetime = Field(description="Novo horário de retorno automático, com fuso.")


class DeactivateEventRequest(BaseModel):
    confirmacao: str = Field(description='Precisa ser exatamente "DESATIVAR".')


class EventView(BaseModel):
    id: int
    nome: str
    status: EventStatus
    db_login: str
    starts_at: datetime
    auto_return_at: datetime
    activated_at: datetime | None
    deactivate_allowed_at: datetime | None
    finished_at: datetime | None
    current_step: str | None = Field(description="Passo da automação em execução, quando houver.")
    failure_message: str | None
    execution_url: str | None = Field(description="Execução da automação no console da AWS.")
    requested_by_name: str
    created_at: datetime
    estimated_cost_usd: float = Field(description="Custo extra estimado até agora ou até o fim.")


class EventModeRules(BaseModel):
    normal_instance_type: str
    event_instance_type: str
    min_active_minutes: int
    default_duration_hours: float
    max_duration_hours: float
    extra_cost_usd_per_hour: float
    estimated_activation_minutes: str
    estimated_downtime_minutes: str
    estimated_full_performance_minutes: str
    recommended_lead_minutes: int


class EventModeState(BaseModel):
    configured: bool = Field(description="Indica se a automação está configurada neste ambiente.")
    server_time: datetime
    rules: EventModeRules
    current: EventView | None


class EventCredentials(BaseModel):
    db_login: str
    db_password: str
    db_host: str
    db_name: str
    db_port: int = 5432
    ogc_base_url: str
