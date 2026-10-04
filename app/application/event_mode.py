"""Máquina de estados do modo eventos.

Transições: agendado → ativando → ativo → desativando → encerrado; agendado → cancelado; ativando
ou desativando → falhou. A troca de tamanho do nó é sempre feita por uma automação externa; este
serviço só a inicia e acompanha o resultado. Não há callback: cada consulta e o laço de fundo da
API reconciliam o estado lendo a execução da automação.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.errors import AppError, bad_request, conflict, not_found
from app.domain.auth import CurrentUser
from app.domain.event_mode import (
    ActivateEventRequest,
    AutomationExecution,
    AutomationStatus,
    EventCredentials,
    EventModeRules,
    EventModeState,
    EventRecord,
    EventStatus,
    EventView,
    event_login,
)

ACTIVATE_CONFIRMATION = "ATIVAR"
DEACTIVATE_CONFIRMATION = "DESATIVAR"
# Margem da rede de segurança: se a API não iniciar o retorno no horário, a automação agendada na
# AWS o faz por conta própria depois deste intervalo. A automação de retorno é idempotente.
RETURN_GUARD_DELAY = timedelta(minutes=30)
# O login do evento vale até o teto da ativação mais esta margem; estender não exige mexer no banco.
LOGIN_VALIDITY_MARGIN = timedelta(hours=1)
IMMEDIATE_TOLERANCE = timedelta(minutes=1)


class EventAlreadyOpenError(Exception):
    """O repositório recusou um segundo evento aberto (índice único parcial)."""


class EventVersionConflictError(Exception):
    """A linha mudou desde a leitura; a operação concorrente venceu."""


class EventRepository(Protocol):
    def get_open(self) -> EventRecord | None: ...

    def get(self, event_id: int) -> EventRecord | None: ...

    def find_by_idempotency_key(self, key: str) -> EventRecord | None: ...

    def create(self, record: EventRecord) -> EventRecord: ...

    def update(self, record: EventRecord) -> EventRecord: ...

    def list_recent(self, limit: int) -> list[EventRecord]: ...

    def audit(self, event_id: int, user_id: int | None, action: str, detail: str | None = None) -> None: ...


class Automation(Protocol):
    def start(self, action: str, *, event_id: int, db_login: str, login_valid_until: datetime) -> str: ...

    def get(self, execution_id: str) -> AutomationExecution: ...


class ReturnGuard(Protocol):
    def schedule(self, *, event_id: int, at: datetime, db_login: str) -> None: ...

    def delete(self, event_id: int) -> None: ...


class EventSecrets(Protocol):
    def db_password(self, event_id: int) -> str | None: ...


@dataclass(frozen=True, slots=True)
class EventModeSettings:
    normal_instance_type: str
    event_instance_type: str
    min_active_minutes: int
    default_duration_hours: float
    max_duration_hours: float
    extra_cost_usd_per_hour: float
    region: str
    db_host: str
    db_name: str
    ogc_base_url: str


class EventModeService:
    def __init__(
        self,
        repository: EventRepository,
        settings: EventModeSettings,
        *,
        automation: Automation | None,
        return_guard: ReturnGuard | None,
        secrets: EventSecrets | None,
        imports_in_flight: Callable[[], int],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.automation = automation
        self.return_guard = return_guard
        self.secrets = secrets
        self.imports_in_flight = imports_in_flight
        self.clock = clock
        self.logger = logging.getLogger(__name__)

    # Consultas -------------------------------------------------------------------------------

    def state(self) -> EventModeState:
        current = self._current()
        if current is not None:
            current = self._reconcile(current)
        return EventModeState(
            configured=self.automation is not None,
            server_time=self.clock(),
            rules=self.rules(),
            current=self.view(current) if current else None,
        )

    def _current(self) -> EventRecord | None:
        """Evento aberto ou, se o último falhou depois de alguma automação rodar, esse evento.

        Essa falha fica visível até o retorno forçado, porque a máquina pode ter ficado ampliada. Uma
        falha sem execução (a AWS recusou o início) não alterou nada e não bloqueia nova ativação.
        """
        record = self.repository.get_open()
        if record is not None:
            return record
        latest = self.repository.list_recent(1)
        if latest and latest[0].status is EventStatus.FAILED and latest[0].execution_id:
            return latest[0]
        return None

    def history(self, limit: int = 20) -> list[EventView]:
        return [self.view(record) for record in self.repository.list_recent(limit)]

    def rules(self) -> EventModeRules:
        return EventModeRules(
            normal_instance_type=self.settings.normal_instance_type,
            event_instance_type=self.settings.event_instance_type,
            min_active_minutes=self.settings.min_active_minutes,
            default_duration_hours=self.settings.default_duration_hours,
            max_duration_hours=self.settings.max_duration_hours,
            extra_cost_usd_per_hour=self.settings.extra_cost_usd_per_hour,
            estimated_activation_minutes="5",
            estimated_downtime_minutes="3 a 5",
            estimated_full_performance_minutes="15 a 20",
            recommended_lead_minutes=30,
        )

    def view(self, record: EventRecord) -> EventView:
        execution_url = None
        if record.execution_id:
            region = self.settings.region
            execution_url = (
                f"https://{region}.console.aws.amazon.com/systems-manager/automation/execution/"
                f"{record.execution_id}?region={region}"
            )
        return EventView(
            id=record.id,
            nome=record.nome,
            status=record.status,
            db_login=record.db_login,
            starts_at=record.starts_at,
            auto_return_at=record.auto_return_at,
            activated_at=record.activated_at,
            deactivate_allowed_at=record.deactivate_allowed_at,
            finished_at=record.finished_at,
            current_step=record.current_step,
            failure_message=record.failure_message,
            execution_url=execution_url,
            requested_by_name=record.requested_by_name,
            created_at=record.created_at,
            estimated_cost_usd=self._estimated_cost(record),
        )

    def credentials(self, user: CurrentUser, event_id: int) -> EventCredentials:
        record = self._get(event_id)
        if record.status is not EventStatus.ACTIVE:
            raise conflict("EVENT_MODE_NOT_ACTIVE", "As credenciais só existem enquanto o evento está ativo.")
        password = self.secrets.db_password(event_id) if self.secrets else None
        if not password:
            raise AppError(
                503,
                "EVENT_MODE_CREDENTIALS_UNAVAILABLE",
                "Credenciais indisponíveis",
                "A senha do login do evento ainda não foi encontrada no cofre de parâmetros.",
            )
        self.repository.audit(event_id, user.id, "credenciais_lidas")
        return EventCredentials(
            db_login=record.db_login,
            db_password=password,
            db_host=self.settings.db_host,
            db_name=self.settings.db_name,
            ogc_base_url=self.settings.ogc_base_url,
        )

    # Comandos --------------------------------------------------------------------------------

    def activate(self, user: CurrentUser, request: ActivateEventRequest, idempotency_key: str) -> EventView:
        if request.confirmacao != ACTIVATE_CONFIRMATION:
            raise bad_request(
                "EVENT_MODE_CONFIRMATION_REQUIRED",
                f'Para ativar, envie confirmacao igual a "{ACTIVATE_CONFIRMATION}".',
            )
        self._require_configured()
        previous = self.repository.find_by_idempotency_key(idempotency_key)
        if previous is not None:
            if previous.requested_by != user.id or previous.nome != request.nome.strip():
                raise conflict("IDEMPOTENCY_KEY_REUSED", "A chave já identifica outro pedido de ativação.")
            return self.view(previous)
        current = self._current()
        if current is not None and current.status is EventStatus.FAILED:
            raise conflict(
                "EVENT_MODE_FAILED_PENDING",
                "O último evento falhou. Use o retorno forçado antes de ativar outro.",
            )
        if current is not None:
            raise conflict("EVENT_MODE_ALREADY_OPEN", "Já existe um evento agendado ou ativo.")

        now = self.clock()
        starts_at = self._aware(request.inicio, "inicio") if request.inicio else now
        if starts_at < now - IMMEDIATE_TOLERANCE:
            raise bad_request("EVENT_MODE_START_IN_PAST", "O início agendado já passou.")
        immediate = starts_at <= now + IMMEDIATE_TOLERANCE
        if immediate:
            starts_at = now
        auto_return_at = (
            self._aware(request.retorno_automatico_em, "retorno_automatico_em")
            if request.retorno_automatico_em
            else starts_at + timedelta(hours=self.settings.default_duration_hours)
        )
        self._check_return(starts_at, auto_return_at)
        if immediate:
            self._require_no_imports_in_flight()

        try:
            record = self.repository.create(
                EventRecord(
                    id=0,
                    nome=request.nome.strip(),
                    status=EventStatus.SCHEDULED,
                    db_login=event_login(request.nome, starts_at),
                    starts_at=starts_at,
                    auto_return_at=auto_return_at,
                    requested_by=user.id,
                    requested_by_name=user.nome,
                    created_at=now,
                    idempotency_key=idempotency_key,
                )
            )
        except EventAlreadyOpenError as exc:
            raise conflict("EVENT_MODE_ALREADY_OPEN", "Já existe um evento agendado ou ativo.") from exc
        self.repository.audit(record.id, user.id, "agendado" if not immediate else "solicitado")
        self._schedule_guard(record)
        if immediate:
            record = self._start(record, "activate", EventStatus.ACTIVATING, user.id)
        return self.view(record)

    def extend(self, user: CurrentUser, event_id: int, auto_return_at: datetime) -> EventView:
        record = self._get(event_id)
        if record.status not in {EventStatus.SCHEDULED, EventStatus.ACTIVATING, EventStatus.ACTIVE}:
            raise conflict("EVENT_MODE_NOT_EXTENDABLE", "Só é possível estender um evento agendado ou ativo.")
        auto_return_at = self._aware(auto_return_at, "retorno_automatico_em")
        if auto_return_at < self.clock() + timedelta(minutes=5):
            raise bad_request("EVENT_MODE_RETURN_TOO_SOON", "O novo retorno precisa estar ao menos 5 minutos à frente.")
        self._check_return(record.activated_at or record.starts_at, auto_return_at)
        record.auto_return_at = auto_return_at
        record = self._save(record)
        self.repository.audit(record.id, user.id, "estendido", auto_return_at.isoformat())
        self._schedule_guard(record)
        return self.view(record)

    def deactivate(self, user: CurrentUser, event_id: int, confirmation: str) -> EventView:
        if confirmation != DEACTIVATE_CONFIRMATION:
            raise bad_request(
                "EVENT_MODE_CONFIRMATION_REQUIRED",
                f'Para desativar, envie confirmacao igual a "{DEACTIVATE_CONFIRMATION}".',
            )
        self._require_configured()
        record = self._reconcile(self._get(event_id))
        if record.status is EventStatus.ACTIVE:
            now = self.clock()
            if record.deactivate_allowed_at and now < record.deactivate_allowed_at:
                wait = math.ceil((record.deactivate_allowed_at - now).total_seconds())
                raise AppError(
                    409,
                    "EVENT_MODE_MIN_ACTIVE_TIME",
                    "Desativação ainda não liberada",
                    f"O modo eventos precisa ficar ativo por {self.settings.min_active_minutes} minutos "
                    f"antes de ser desativado. Faltam {math.ceil(wait / 60)} minuto(s).",
                    headers={"Retry-After": str(wait)},
                )
        elif record.status is not EventStatus.FAILED:
            raise conflict("EVENT_MODE_NOT_ACTIVE", "Só é possível desativar um evento ativo ou que falhou.")
        self._require_no_imports_in_flight()
        return self.view(self._start(record, "deactivate", EventStatus.DEACTIVATING, user.id))

    def cancel(self, user: CurrentUser, event_id: int) -> EventView:
        record = self._get(event_id)
        if record.status is not EventStatus.SCHEDULED:
            raise conflict("EVENT_MODE_NOT_CANCELLABLE", "Só um evento ainda agendado pode ser cancelado.")
        record.status = EventStatus.CANCELLED
        record.finished_at = self.clock()
        record = self._save(record)
        self.repository.audit(record.id, user.id, "cancelado")
        self._delete_guard(record.id)
        return self.view(record)

    def tick(self) -> None:
        """Executado pelo laço de fundo da API: reconcilia e cumpre os horários do evento aberto."""
        record = self.repository.get_open()
        if record is None or self.automation is None:
            return
        record = self._reconcile(record)
        now = self.clock()
        if record.status is EventStatus.SCHEDULED and record.starts_at <= now:
            if self.imports_in_flight():
                self.logger.info("event mode start waiting for imports in flight", extra={"event_id": record.id})
                return
            self._start(record, "activate", EventStatus.ACTIVATING, None)
        elif record.status is EventStatus.ACTIVE and record.auto_return_at <= now:
            if self.imports_in_flight():
                self.logger.info("event mode return waiting for imports in flight", extra={"event_id": record.id})
                return
            self._start(record, "deactivate", EventStatus.DEACTIVATING, None)

    # Internos --------------------------------------------------------------------------------

    def _reconcile(self, record: EventRecord) -> EventRecord:
        if record.status not in {EventStatus.ACTIVATING, EventStatus.DEACTIVATING} or not record.execution_id:
            return record
        if self.automation is None:
            return record
        try:
            execution = self.automation.get(record.execution_id)
        except Exception:
            self.logger.exception("could not read automation execution", extra={"event_id": record.id})
            return record
        now = self.clock()
        if execution.status is AutomationStatus.RUNNING:
            if execution.current_step == record.current_step:
                return record
            record.current_step = execution.current_step
        elif execution.status is AutomationStatus.SUCCESS:
            record.current_step = None
            if record.status is EventStatus.ACTIVATING:
                record.status = EventStatus.ACTIVE
                record.activated_at = now
                record.deactivate_allowed_at = now + timedelta(minutes=self.settings.min_active_minutes)
                action = "ativo"
            else:
                record.status = EventStatus.FINISHED
                record.finished_at = now
                action = "encerrado"
                self._delete_guard(record.id)
            self.repository.audit(record.id, None, action)
        else:
            record.status = EventStatus.FAILED
            record.failure_message = execution.failure_message or "A automação terminou com falha."
            self.repository.audit(record.id, None, "falhou", record.failure_message)
        try:
            return self._save(record)
        except EventVersionConflictError:
            return self.repository.get(record.id) or record

    def _start(self, record: EventRecord, action: str, status: EventStatus, user_id: int | None) -> EventRecord:
        assert self.automation is not None
        valid_until = (record.activated_at or record.starts_at) + timedelta(
            hours=self.settings.max_duration_hours
        ) + LOGIN_VALIDITY_MARGIN
        try:
            execution_id = self.automation.start(
                action, event_id=record.id, db_login=record.db_login, login_valid_until=valid_until
            )
        except Exception as exc:
            self.logger.exception("could not start event automation", extra={"event_id": record.id})
            record.status = EventStatus.FAILED
            record.failure_message = "Não foi possível iniciar a automação na AWS."
            self._save(record)
            self.repository.audit(record.id, user_id, "falhou", record.failure_message)
            raise AppError(
                502,
                "EVENT_MODE_AUTOMATION_UNAVAILABLE",
                "Automação indisponível",
                "A AWS não aceitou iniciar a automação. Nada foi alterado no servidor.",
            ) from exc
        record.status = status
        record.execution_id = execution_id
        record.current_step = None
        record.failure_message = None
        record = self._save(record)
        self.repository.audit(record.id, user_id, "ativacao_iniciada" if action == "activate" else "retorno_iniciado")
        return record

    def _save(self, record: EventRecord) -> EventRecord:
        try:
            return self.repository.update(record)
        except EventVersionConflictError as exc:
            raise conflict("EVENT_MODE_CONCURRENT_CHANGE", "O evento mudou durante a operação; recarregue.") from exc

    def _schedule_guard(self, record: EventRecord) -> None:
        if self.return_guard is None:
            return
        try:
            self.return_guard.schedule(
                event_id=record.id, at=record.auto_return_at + RETURN_GUARD_DELAY, db_login=record.db_login
            )
        except Exception:
            self.logger.exception("could not schedule event return guard", extra={"event_id": record.id})

    def _delete_guard(self, event_id: int) -> None:
        if self.return_guard is None:
            return
        try:
            self.return_guard.delete(event_id)
        except Exception:
            self.logger.exception("could not delete event return guard", extra={"event_id": event_id})

    def _check_return(self, base: datetime, auto_return_at: datetime) -> None:
        minimum = base + timedelta(minutes=self.settings.min_active_minutes)
        maximum = base + timedelta(hours=self.settings.max_duration_hours)
        if auto_return_at < minimum:
            raise bad_request(
                "EVENT_MODE_RETURN_TOO_SOON",
                f"O retorno precisa ser ao menos {self.settings.min_active_minutes} minutos depois do início.",
            )
        if auto_return_at > maximum:
            raise bad_request(
                "EVENT_MODE_RETURN_TOO_LATE",
                f"O retorno pode ser no máximo {self.settings.max_duration_hours:g} horas depois do início.",
            )

    def _require_configured(self) -> None:
        if self.automation is None:
            raise AppError(
                503,
                "EVENT_MODE_NOT_CONFIGURED",
                "Modo eventos indisponível",
                "A automação do modo eventos não está configurada neste ambiente.",
            )

    def _require_no_imports_in_flight(self) -> None:
        in_flight = self.imports_in_flight()
        if in_flight:
            raise conflict(
                "EVENT_MODE_IMPORTS_IN_FLIGHT",
                f"Há {in_flight} envio(s) de arquivo em validação; aguarde terminar para não interrompê-los.",
            )

    def _get(self, event_id: int) -> EventRecord:
        record = self.repository.get(event_id)
        if record is None:
            raise not_found("Evento", str(event_id))
        return record

    def _estimated_cost(self, record: EventRecord) -> float:
        start = record.activated_at
        if start is None:
            if record.status is not EventStatus.SCHEDULED:
                return 0.0
            start, end = record.starts_at, record.auto_return_at
        else:
            end = record.finished_at or min(self.clock(), record.auto_return_at)
        hours = max((end - start).total_seconds(), 0) / 3600
        return round(hours * self.settings.extra_cost_usd_per_hour, 2)

    @staticmethod
    def _aware(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None:
            raise bad_request(
                "EVENT_MODE_TIMEZONE_REQUIRED",
                f"Informe {field_name} com fuso horário, por exemplo 2026-10-20T14:00:00-03:00.",
            )
        return value.astimezone(UTC)
