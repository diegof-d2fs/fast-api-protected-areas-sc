from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.auth import AuthService
from app.core.errors import AppError
from app.core.security import hash_password
from app.domain.auth import CreateUserRequest, UserRole
from tests.fakes import FakeAuthRepository


def _service_with_user(
    *, username: str = "operador1", password: str = "senha-forte-123", ativo: bool = True
) -> tuple[AuthService, FakeAuthRepository]:
    repository = FakeAuthRepository()
    repository.create_user(
        username=username,
        nome="Operador de Teste",
        password_hash=hash_password(password),
        role=UserRole.OPERATOR,
        criado_por=None,
    )
    if not ativo:
        repository.set_user_active(1, False)
    return AuthService(repository), repository


def test_login_succeeds_and_creates_a_session() -> None:
    service, repository = _service_with_user()
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    assert result.user.nome == "Operador de Teste"
    assert result.user.role is UserRole.OPERATOR
    assert len(repository.sessions) == 1


def test_login_rejects_wrong_password() -> None:
    service, _ = _service_with_user()
    with pytest.raises(AppError) as captured:
        service.login(username="operador1", password="senha-errada", ip_origem=None)
    assert captured.value.status_code == 401
    assert captured.value.error_code == "INVALID_CREDENTIALS"


def test_login_rejects_unknown_username_with_the_same_generic_message() -> None:
    service, _ = _service_with_user()
    with pytest.raises(AppError) as captured:
        service.login(username="nunca-existiu", password="qualquer", ip_origem=None)
    assert captured.value.status_code == 401
    assert captured.value.error_code == "INVALID_CREDENTIALS"


def test_login_rejects_inactive_account_with_the_same_generic_message() -> None:
    service, _ = _service_with_user(ativo=False)
    with pytest.raises(AppError) as captured:
        service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    assert captured.value.status_code == 401
    assert captured.value.error_code == "INVALID_CREDENTIALS"


def test_login_locks_out_after_threshold_failed_attempts_even_with_correct_password() -> None:
    service, _ = _service_with_user()
    service.lockout_threshold = 3
    for _ in range(3):
        with pytest.raises(AppError):
            service.login(username="operador1", password="senha-errada", ip_origem=None)
    with pytest.raises(AppError) as captured:
        service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    assert captured.value.status_code == 429
    assert captured.value.error_code == "LOGIN_LOCKED"


def test_login_lockout_is_scoped_to_the_lockout_window() -> None:
    service, repository = _service_with_user()
    service.lockout_threshold = 3
    old = datetime.now(UTC) - timedelta(hours=1)
    repository.login_attempts.extend([("operador1", False, old)] * 5)
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    assert result.user.nome == "Operador de Teste"


def test_get_current_user_rejects_missing_token() -> None:
    service, _ = _service_with_user()
    with pytest.raises(AppError) as captured:
        service.get_current_user(None)
    assert captured.value.status_code == 401
    assert captured.value.error_code == "NOT_AUTHENTICATED"


def test_get_current_user_returns_the_session_owner_for_a_valid_token() -> None:
    service, _ = _service_with_user()
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    current = service.get_current_user(result.token)
    assert current.id == result.user.id
    assert current.role is UserRole.OPERATOR


def test_get_current_user_rejects_an_expired_session() -> None:
    service, repository = _service_with_user()
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    for token_hash, (user_id, _, revogado_em) in repository.sessions.items():
        repository.sessions[token_hash] = (user_id, datetime.now(UTC) - timedelta(seconds=1), revogado_em)
    with pytest.raises(AppError) as captured:
        service.get_current_user(result.token)
    assert captured.value.status_code == 401
    assert captured.value.error_code == "SESSION_INVALID"


def test_logout_revokes_the_session_so_it_stops_authenticating() -> None:
    service, _ = _service_with_user()
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    service.logout(result.token)
    with pytest.raises(AppError) as captured:
        service.get_current_user(result.token)
    assert captured.value.error_code == "SESSION_INVALID"


def test_create_user_returns_a_view_that_never_serializes_the_password_hash() -> None:
    repository = FakeAuthRepository()
    service = AuthService(repository)
    view = service.create_user(
        CreateUserRequest(username="novo", nome="Novo Usuário", password="senha-forte-123", role=UserRole.OPERATOR),
        criado_por=1,
    )
    payload = view.model_dump_json()
    assert "password_hash" not in payload
    assert "senha-forte-123" not in payload


def test_list_users_never_serializes_the_password_hash() -> None:
    service, _ = _service_with_user()
    payload = "".join(view.model_dump_json() for view in service.list_users())
    assert "password_hash" not in payload


def test_create_user_raises_conflict_for_a_duplicate_username() -> None:
    service, _ = _service_with_user(username="duplicado")
    with pytest.raises(AppError) as captured:
        service.create_user(
            CreateUserRequest(username="duplicado", nome="Outro", password="senha-forte-123", role=UserRole.OPERATOR),
            criado_por=1,
        )
    assert captured.value.status_code == 409
    assert captured.value.error_code == "USERNAME_ALREADY_EXISTS"


def test_deactivating_a_user_revokes_their_live_session() -> None:
    service, _ = _service_with_user()
    result = service.login(username="operador1", password="senha-forte-123", ip_origem=None)
    service.set_user_active(result.user.id, False)
    with pytest.raises(AppError) as captured:
        service.get_current_user(result.token)
    assert captured.value.error_code == "SESSION_INVALID"


def test_set_user_active_raises_not_found_for_an_unknown_user() -> None:
    service, _ = _service_with_user()
    with pytest.raises(AppError) as captured:
        service.set_user_active(999, True)
    assert captured.value.status_code == 404
    assert captured.value.error_code == "USER_NOT_FOUND"


def test_bootstrap_admin_creates_an_account_only_when_the_repository_is_empty() -> None:
    repository = FakeAuthRepository()
    service = AuthService(repository)
    assert service.bootstrap_admin(username="admin", password="senha-forte-123") is True
    assert repository.count_users() == 1
    assert service.bootstrap_admin(username="outro-admin", password="senha-forte-123") is False
    assert repository.count_users() == 1
