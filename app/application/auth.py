from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.errors import AppError
from app.core.security import generate_session_token, hash_password, hash_session_token, verify_password
from app.domain.auth import (
    CreateUserRequest,
    CurrentUser,
    UsernameAlreadyExistsError,
    UserRecord,
    UserRole,
    UserView,
)


class AuthRepository(Protocol):
    def get_user_by_username(self, username: str) -> UserRecord | None: ...

    def get_user_by_id(self, user_id: int) -> UserRecord | None: ...

    def count_users(self) -> int: ...

    def create_user(
        self, *, username: str, nome: str, password_hash: str, role: UserRole, criado_por: int | None
    ) -> UserRecord: ...

    def list_users(self) -> list[UserRecord]: ...

    def set_user_active(self, user_id: int, ativo: bool) -> UserRecord | None: ...

    def create_session(self, *, user_id: int, token_hash: str, expires_at: datetime) -> None: ...

    def get_active_session_user(self, token_hash: str, now: datetime) -> UserRecord | None: ...

    def revoke_session(self, token_hash: str) -> None: ...

    def record_login_attempt(self, *, username: str, sucesso: bool, ip_origem: str | None) -> None: ...

    def count_recent_failed_attempts(self, *, username: str, since: datetime) -> int: ...


@dataclass
class LoginResult:
    user: CurrentUser
    token: str
    expires_at: datetime


@dataclass
class AuthService:
    repository: AuthRepository
    session_ttl: timedelta = timedelta(hours=12)
    lockout_threshold: int = 5
    lockout_window: timedelta = timedelta(minutes=15)

    def login(self, *, username: str, password: str, ip_origem: str | None) -> LoginResult:
        now = datetime.now(UTC)
        since = now - self.lockout_window
        failed_attempts = self.repository.count_recent_failed_attempts(username=username, since=since)
        if failed_attempts >= self.lockout_threshold:
            raise AppError(
                429,
                "LOGIN_LOCKED",
                "Muitas tentativas de login",
                "Esta conta está temporariamente bloqueada por excesso de tentativas de login "
                "malsucedidas. Tente novamente mais tarde.",
            )

        user = self.repository.get_user_by_username(username)
        credentials_valid = (
            user is not None and user.ativo and verify_password(password, user.password_hash)
        )
        self.repository.record_login_attempt(
            username=username, sucesso=credentials_valid, ip_origem=ip_origem
        )
        if not credentials_valid or user is None:
            raise AppError(
                401,
                "INVALID_CREDENTIALS",
                "Credenciais inválidas",
                "Usuário ou senha incorretos.",
            )

        token = generate_session_token()
        expires_at = now + self.session_ttl
        self.repository.create_session(
            user_id=user.id, token_hash=hash_session_token(token), expires_at=expires_at
        )
        return LoginResult(
            user=CurrentUser(id=user.id, nome=user.nome, role=user.role),
            token=token,
            expires_at=expires_at,
        )

    def logout(self, token: str) -> None:
        self.repository.revoke_session(hash_session_token(token))

    def get_current_user(self, token: str | None) -> CurrentUser:
        if not token:
            raise AppError(
                401, "NOT_AUTHENTICATED", "Não autenticado", "Esta rota exige uma sessão válida."
            )
        user = self.repository.get_active_session_user(hash_session_token(token), datetime.now(UTC))
        if user is None:
            raise AppError(
                401,
                "SESSION_INVALID",
                "Sessão inválida",
                "A sessão está ausente, expirada, revogada ou pertence a uma conta desativada.",
            )
        return CurrentUser(id=user.id, nome=user.nome, role=user.role)

    def create_user(self, request: CreateUserRequest, *, criado_por: int) -> UserView:
        try:
            record = self.repository.create_user(
                username=request.username,
                nome=request.nome,
                password_hash=hash_password(request.password),
                role=request.role,
                criado_por=criado_por,
            )
        except UsernameAlreadyExistsError as exc:
            raise AppError(
                409,
                "USERNAME_ALREADY_EXISTS",
                "Usuário já existe",
                f"O nome de usuário '{request.username}' já está em uso.",
            ) from exc
        return UserView.from_record(record)

    def list_users(self) -> list[UserView]:
        return [UserView.from_record(record) for record in self.repository.list_users()]

    def set_user_active(self, user_id: int, ativo: bool) -> UserView:
        record = self.repository.set_user_active(user_id, ativo)
        if record is None:
            raise AppError(
                404, "USER_NOT_FOUND", "Usuário não encontrado", f"O usuário '{user_id}' não foi encontrado."
            )
        return UserView.from_record(record)

    def bootstrap_admin(self, *, username: str, password: str, nome: str = "Administrador") -> bool:
        """Create the first admin from environment variables when `app_user` is empty.

        Returns whether an account was created, so the caller can log the outcome without
        the service reaching into logging concerns itself.
        """
        if self.repository.count_users() > 0:
            return False
        self.repository.create_user(
            username=username,
            nome=nome,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            criado_por=None,
        )
        return True
