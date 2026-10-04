"""Migração 006 e repositório do modo eventos contra um PostGIS real.

Roda só com `PA_SC_TEST_DATABASE_DSN` apontando para um banco descartável, por exemplo
`docker run -e POSTGRES_PASSWORD=ci -p 55432:5432 postgis/postgis:17-3.5`, com o `init_db.sql`
do pipeline já aplicado (as migrações da API partem do esquema cadastral dele).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.application.event_mode import EventAlreadyOpenError, EventVersionConflictError
from app.domain.event_mode import EventRecord, EventStatus
from app.infrastructure.persistence.event_mode_repository import PostgresEventModeRepository
from app.infrastructure.persistence.migrate import apply_migrations

DSN = os.environ.get("PA_SC_TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PA_SC_TEST_DATABASE_DSN não definido")
T0 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def repository() -> PostgresEventModeRepository:
    apply_migrations(DSN)
    with psycopg.connect(DSN) as connection:
        connection.execute("TRUNCATE public.event_mode_audit, public.event_mode_event RESTART IDENTITY CASCADE")
        connection.execute(
            "INSERT INTO public.app_user (username, nome, password_hash, role) "
            "VALUES ('evento-admin', 'Evento Admin', 'x', 'admin') ON CONFLICT DO NOTHING"
        )
    return PostgresEventModeRepository(DSN)


def _user_id() -> int:
    with psycopg.connect(DSN) as connection:
        return connection.execute("SELECT id FROM public.app_user WHERE username = 'evento-admin'").fetchone()[0]


def _record(key: str) -> EventRecord:
    return EventRecord(
        id=0,
        nome="Univali",
        status=EventStatus.SCHEDULED,
        db_login="ws_univali_20261020",
        starts_at=T0,
        auto_return_at=T0 + timedelta(hours=5),
        requested_by=_user_id(),
        requested_by_name="",
        created_at=T0,
        idempotency_key=key,
    )


def test_lifecycle_unique_open_event_and_optimistic_lock(repository: PostgresEventModeRepository) -> None:
    created = repository.create(_record("chave-1"))
    assert created.requested_by_name == "Evento Admin"
    assert repository.get_open().id == created.id
    assert repository.find_by_idempotency_key("chave-1").id == created.id

    with pytest.raises(EventAlreadyOpenError):
        repository.create(_record("chave-2"))

    created.status = EventStatus.ACTIVATING
    created.execution_id = "exec-1"
    updated = repository.update(created)
    assert updated.version == created.version + 1

    with pytest.raises(EventVersionConflictError):
        repository.update(created)

    updated.status = EventStatus.FINISHED
    updated.finished_at = T0 + timedelta(hours=5)
    repository.update(updated)
    repository.audit(updated.id, None, "encerrado")
    assert repository.get_open() is None
    assert repository.create(_record("chave-3")).id != created.id
    assert [item.id for item in repository.list_recent(5)][0] > created.id


def test_constraints_reject_invalid_login_and_return_before_start() -> None:
    with psycopg.connect(DSN) as connection:
        for login, starts, returns in (
            ("powerbi_svc", T0, T0 + timedelta(hours=1)),
            ("ws_ok", T0, T0 - timedelta(hours=1)),
        ):
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute(
                    "INSERT INTO public.event_mode_event (nome, status, db_login, starts_at, auto_return_at, "
                    "requested_by) VALUES ('x', 'cancelado', %s, %s, %s, %s)",
                    (login, starts, returns, _user_id()),
                )
