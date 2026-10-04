"""Shared in-memory test doubles, reused across unit and integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.domain.auth import UsernameAlreadyExistsError, UserRecord, UserRole


@dataclass
class FakeAuthRepository:
    """In-memory stand-in for `PostgresAuthRepository`, mirroring its exact contract."""

    users: dict[int, UserRecord] = field(default_factory=dict)
    usernames: dict[str, int] = field(default_factory=dict)
    # token_hash -> (user_id, expira_em, revogado_em)
    sessions: dict[str, tuple[int, datetime, datetime | None]] = field(default_factory=dict)
    login_attempts: list[tuple[str, bool, datetime]] = field(default_factory=list)
    login_ips: list[str | None] = field(default_factory=list)
    _next_id: int = 1

    def get_user_by_username(self, username: str) -> UserRecord | None:
        user_id = self.usernames.get(username)
        return self.users.get(user_id) if user_id is not None else None

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        return self.users.get(user_id)

    def count_users(self) -> int:
        return len(self.users)

    def create_user(
        self, *, username: str, nome: str, password_hash: str, role: UserRole, criado_por: int | None
    ) -> UserRecord:
        if username in self.usernames:
            raise UsernameAlreadyExistsError(username)
        user_id = self._next_id
        self._next_id += 1
        record = UserRecord(
            id=user_id,
            username=username,
            nome=nome,
            password_hash=password_hash,
            role=role,
            ativo=True,
            criado_em=datetime.now(UTC),
        )
        self.users[user_id] = record
        self.usernames[username] = user_id
        return record

    def list_users(self) -> list[UserRecord]:
        return sorted(self.users.values(), key=lambda record: record.id)

    def set_user_active(self, user_id: int, ativo: bool) -> UserRecord | None:
        record = self.users.get(user_id)
        if record is None:
            return None
        updated = replace(record, ativo=ativo)
        self.users[user_id] = updated
        if not ativo:
            for token_hash, (uid, expira_em, revogado_em) in list(self.sessions.items()):
                if uid == user_id and revogado_em is None:
                    self.sessions[token_hash] = (uid, expira_em, datetime.now(UTC))
        return updated

    def create_session(self, *, user_id: int, token_hash: str, expires_at: datetime) -> None:
        self.sessions[token_hash] = (user_id, expires_at, None)

    def get_active_session_user(self, token_hash: str, now: datetime) -> UserRecord | None:
        entry = self.sessions.get(token_hash)
        if entry is None:
            return None
        user_id, expira_em, revogado_em = entry
        if revogado_em is not None or expira_em <= now:
            return None
        user = self.users.get(user_id)
        return user if user is not None and user.ativo else None

    def revoke_session(self, token_hash: str) -> None:
        entry = self.sessions.get(token_hash)
        if entry is None:
            return
        user_id, expira_em, _ = entry
        self.sessions[token_hash] = (user_id, expira_em, datetime.now(UTC))

    def record_login_attempt(self, *, username: str, sucesso: bool, ip_origem: str | None) -> None:
        self.login_attempts.append((username, sucesso, datetime.now(UTC)))
        self.login_ips.append(ip_origem)

    def count_recent_failed_attempts(self, *, username: str, since: datetime) -> int:
        return sum(
            1
            for logged_username, sucesso, criado_em in self.login_attempts
            if logged_username == username and not sucesso and criado_em >= since
        )


class FakeEventModeRepository:
    """Repositório em memória com as mesmas garantias do Postgres: um evento aberto e versão."""

    def __init__(self) -> None:
        self.records: dict = {}
        self.audits: list[tuple[int, int | None, str, str | None]] = []

    def get_open(self):
        from app.domain.event_mode import OPEN_STATUSES

        return next((replace(r) for r in self.records.values() if r.status in OPEN_STATUSES), None)

    def get(self, event_id: int):
        record = self.records.get(event_id)
        return replace(record) if record else None

    def find_by_idempotency_key(self, key: str):
        return next((replace(r) for r in self.records.values() if r.idempotency_key == key), None)

    def create(self, record):
        from app.application.event_mode import EventAlreadyOpenError

        if self.get_open() is not None:
            raise EventAlreadyOpenError()
        created = replace(record, id=len(self.records) + 1, version=1)
        self.records[created.id] = created
        return replace(created)

    def update(self, record):
        from app.application.event_mode import EventVersionConflictError

        stored = self.records[record.id]
        if stored.version != record.version:
            raise EventVersionConflictError()
        updated = replace(record, version=record.version + 1)
        self.records[record.id] = updated
        return replace(updated)

    def list_recent(self, limit: int):
        return [replace(r) for r in sorted(self.records.values(), key=lambda r: -r.id)[:limit]]

    def audit(self, event_id: int, user_id: int | None, action: str, detail: str | None = None) -> None:
        self.audits.append((event_id, user_id, action, detail))


@dataclass
class FakeAutomation:
    started: list[tuple[str, int, str]] = field(default_factory=list)
    executions: dict = field(default_factory=dict)
    fail_start: bool = False

    def start(self, action: str, *, event_id: int, db_login: str, login_valid_until: datetime) -> str:
        from app.domain.event_mode import AutomationExecution, AutomationStatus

        if self.fail_start:
            raise RuntimeError("AccessDenied")
        execution_id = f"exec-{len(self.started) + 1}"
        self.started.append((action, event_id, db_login))
        self.executions[execution_id] = AutomationExecution(AutomationStatus.RUNNING, current_step="pararServidor")
        return execution_id

    def get(self, execution_id: str):
        return self.executions[execution_id]

    def finish(self, execution_id: str, *, success: bool = True, message: str | None = None) -> None:
        from app.domain.event_mode import AutomationExecution, AutomationStatus

        status = AutomationStatus.SUCCESS if success else AutomationStatus.FAILED
        self.executions[execution_id] = AutomationExecution(status, failure_message=message)


@dataclass
class FakeReturnGuard:
    scheduled: dict[int, datetime] = field(default_factory=dict)

    def schedule(self, *, event_id: int, at: datetime, db_login: str) -> None:
        self.scheduled[event_id] = at

    def delete(self, event_id: int) -> None:
        self.scheduled.pop(event_id, None)


@dataclass
class FakeEventSecrets:
    passwords: dict[int, str] = field(default_factory=dict)

    def db_password(self, event_id: int) -> str | None:
        return self.passwords.get(event_id)
