from __future__ import annotations

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.application.event_mode import EventAlreadyOpenError, EventVersionConflictError
from app.domain.event_mode import EventRecord, EventStatus

_SELECT = """
    SELECT e.id, e.nome, e.status, e.db_login, e.starts_at, e.auto_return_at, e.activated_at,
           e.deactivate_allowed_at, e.finished_at, e.execution_id, e.current_step, e.failure_message,
           e.requested_by, u.nome AS requested_by_name, e.idempotency_key, e.version, e.criado_em
    FROM public.event_mode_event e
    JOIN public.app_user u ON u.id = e.requested_by
"""
_OPEN = ("agendado", "ativando", "ativo", "desativando")


def _row_to_record(row: dict) -> EventRecord:
    return EventRecord(
        id=row["id"],
        nome=row["nome"],
        status=EventStatus(row["status"]),
        db_login=row["db_login"],
        starts_at=row["starts_at"],
        auto_return_at=row["auto_return_at"],
        requested_by=row["requested_by"],
        requested_by_name=row["requested_by_name"],
        created_at=row["criado_em"],
        version=row["version"],
        activated_at=row["activated_at"],
        deactivate_allowed_at=row["deactivate_allowed_at"],
        finished_at=row["finished_at"],
        execution_id=row["execution_id"],
        current_step=row["current_step"],
        failure_message=row["failure_message"],
        idempotency_key=row["idempotency_key"],
    )


class PostgresEventModeRepository:
    """Owns every read/write against `event_mode_event` and `event_mode_audit`."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def _one(self, where: str, params: tuple) -> EventRecord | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(f"{_SELECT} WHERE {where}", params).fetchone()
        return _row_to_record(row) if row else None

    def get_open(self) -> EventRecord | None:
        return self._one("e.status = ANY(%s)", (list(_OPEN),))

    def get(self, event_id: int) -> EventRecord | None:
        return self._one("e.id = %s", (event_id,))

    def find_by_idempotency_key(self, key: str) -> EventRecord | None:
        return self._one("e.idempotency_key = %s", (key,))

    def create(self, record: EventRecord) -> EventRecord:
        with psycopg.connect(self._dsn) as connection:
            try:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO public.event_mode_event
                            (nome, status, db_login, starts_at, auto_return_at, requested_by, idempotency_key)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            record.nome,
                            record.status.value,
                            record.db_login,
                            record.starts_at,
                            record.auto_return_at,
                            record.requested_by,
                            record.idempotency_key,
                        ),
                    ).fetchone()
            except errors.UniqueViolation as exc:
                raise EventAlreadyOpenError() from exc
        created = self.get(row[0])
        assert created is not None
        return created

    def update(self, record: EventRecord) -> EventRecord:
        with psycopg.connect(self._dsn) as connection:
            result = connection.execute(
                """
                UPDATE public.event_mode_event
                SET status = %s, auto_return_at = %s, activated_at = %s, deactivate_allowed_at = %s,
                    finished_at = %s, execution_id = %s, current_step = %s, failure_message = %s,
                    version = version + 1
                WHERE id = %s AND version = %s
                """,
                (
                    record.status.value,
                    record.auto_return_at,
                    record.activated_at,
                    record.deactivate_allowed_at,
                    record.finished_at,
                    record.execution_id,
                    record.current_step,
                    record.failure_message,
                    record.id,
                    record.version,
                ),
            )
            if result.rowcount != 1:
                raise EventVersionConflictError()
        updated = self.get(record.id)
        assert updated is not None
        return updated

    def list_recent(self, limit: int) -> list[EventRecord]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            rows = connection.execute(f"{_SELECT} ORDER BY e.id DESC LIMIT %s", (limit,)).fetchall()
        return [_row_to_record(row) for row in rows]

    def audit(self, event_id: int, user_id: int | None, action: str, detail: str | None = None) -> None:
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "INSERT INTO public.event_mode_audit (event_id, user_id, acao, detalhe) VALUES (%s, %s, %s, %s)",
                (event_id, user_id, action, detail),
            )
