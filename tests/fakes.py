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

    def count_recent_failed_attempts(self, *, username: str, since: datetime) -> int:
        return sum(
            1
            for logged_username, sucesso, criado_em in self.login_attempts
            if logged_username == username and not sucesso and criado_em >= since
        )
