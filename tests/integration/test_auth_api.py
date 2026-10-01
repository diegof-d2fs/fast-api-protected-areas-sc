from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.application.auth import AuthService
from app.core.config import Settings
from app.core.security import SESSION_COOKIE_NAME, hash_password
from app.domain.auth import UserRole
from app.main import create_app
from tests.fakes import FakeAuthRepository

# O fixture `client` (tests/conftest.py) já autentica por padrão, para que os testes de
# /imports e /ucs em outros arquivos não precisem se preocupar com sessão. Este arquivo testa a
# própria autenticação, então cada teste começa de uma sessão em branco e monta seu próprio
# `auth_service`.
#
# Mesmo com `session_cookie_secure=False` nos testes, cada chamada autenticada abaixo ainda manda
# o cookie como um cabeçalho `Cookie` explícito por clareza: é o mesmo padrão que um cliente real
# sem TLS (Postman/curl) precisaria usar, e deixa visível qual sessão cada requisição carrega.


@pytest.fixture(autouse=True)
def _start_from_a_blank_session(client: TestClient) -> None:
    client.cookies.clear()


def _enable_auth_service(client: TestClient, *, lockout_threshold: int = 5) -> FakeAuthRepository:
    repository = FakeAuthRepository()
    client.app.state.auth_service = AuthService(repository, lockout_threshold=lockout_threshold)
    return repository


def _create_user(
    repository: FakeAuthRepository,
    *,
    username: str,
    password: str,
    role: UserRole = UserRole.OPERATOR,
) -> None:
    repository.create_user(
        username=username,
        nome=username.title(),
        password_hash=hash_password(password),
        role=role,
        criado_por=None,
    )


def _login(client: TestClient, *, username: str, password: str):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _session_headers(login_response) -> dict[str, str]:
    token = login_response.cookies[SESSION_COOKIE_NAME]
    return {"Cookie": f"{SESSION_COOKIE_NAME}={token}"}


def test_auth_routes_require_database_configuration(client: TestClient) -> None:
    # O fixture compartilhado liga um `auth_service` de teste por padrão; este teste verifica
    # justamente o caso em que ele não está configurado (sem PA_SC_DATABASE_DSN em produção).
    client.app.state.auth_service = None
    response = _login(client, username="admin", password="qualquer")
    assert response.status_code == 503
    assert response.json()["error_code"] == "AUTH_DATABASE_NOT_CONFIGURED"


def test_login_sets_an_httponly_cookie_and_returns_the_current_user(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="operador1", password="senha-forte-123")

    response = _login(client, username="operador1", password="senha-forte-123")

    assert response.status_code == 200
    assert response.json() == {"id": 1, "nome": "Operador1", "role": "operador"}
    assert SESSION_COOKIE_NAME in response.cookies
    raw_header = response.headers["set-cookie"]
    assert "HttpOnly" in raw_header
    assert "SameSite=lax" in raw_header
    # `session_cookie_secure=False` nos testes (ver conftest.py) é o que permite o TestClient
    # reenviar este cookie sobre `http://testserver`; `PA_SC_SESSION_COOKIE_SECURE` continua
    # `true` por padrão fora dos testes (ver test abaixo).
    assert "Secure" not in raw_header


def test_login_sets_a_secure_cookie_when_configured_for_it(settings: Settings) -> None:
    secure_settings = settings.model_copy(update={"session_cookie_secure": True})
    with TestClient(create_app(secure_settings)) as secure_client:
        repository = _enable_auth_service(secure_client)
        _create_user(repository, username="operador1", password="senha-forte-123")

        response = _login(secure_client, username="operador1", password="senha-forte-123")

        assert "Secure" in response.headers["set-cookie"]


def test_login_rejects_wrong_password_with_401(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="operador1", password="senha-forte-123")

    response = _login(client, username="operador1", password="senha-errada")

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


def test_login_locks_out_after_repeated_failures(client: TestClient) -> None:
    repository = _enable_auth_service(client, lockout_threshold=3)
    _create_user(repository, username="operador1", password="senha-forte-123")

    for _ in range(3):
        assert _login(client, username="operador1", password="senha-errada").status_code == 401

    locked = _login(client, username="operador1", password="senha-forte-123")
    assert locked.status_code == 429
    assert locked.json()["error_code"] == "LOGIN_LOCKED"


def test_me_requires_a_valid_session(client: TestClient) -> None:
    _enable_auth_service(client)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_me_returns_the_authenticated_user(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="operador1", password="senha-forte-123")
    login = _login(client, username="operador1", password="senha-forte-123")

    response = client.get("/api/v1/auth/me", headers=_session_headers(login))

    assert response.status_code == 200
    assert response.json()["nome"] == "Operador1"


def test_logout_revokes_the_session(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="operador1", password="senha-forte-123")
    login = _login(client, username="operador1", password="senha-forte-123")
    headers = _session_headers(login)

    logout = client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204
    # A resposta precisa instruir o navegador a apagar o cookie, não só revogar no servidor.
    deletion_header = logout.headers["set-cookie"]
    assert deletion_header.startswith(f'{SESSION_COOKIE_NAME}=""')
    assert "Max-Age=0" in deletion_header

    after_logout = client.get("/api/v1/auth/me", headers=headers)
    assert after_logout.status_code == 401


def test_create_user_requires_admin_role(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="operador1", password="senha-forte-123", role=UserRole.OPERATOR)
    login = _login(client, username="operador1", password="senha-forte-123")

    response = client.post(
        "/api/v1/auth/users",
        json={"username": "novo", "nome": "Novo", "password": "senha-forte-123", "role": "operador"},
        headers=_session_headers(login),
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN_ROLE"


def test_admin_can_create_list_and_deactivate_a_user(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="admin", password="senha-forte-123", role=UserRole.ADMIN)
    login = _login(client, username="admin", password="senha-forte-123")
    headers = _session_headers(login)

    created = client.post(
        "/api/v1/auth/users",
        json={"username": "novo", "nome": "Novo Operador", "password": "senha-forte-123", "role": "operador"},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["username"] == "novo"
    assert "password_hash" not in created.text

    listed = client.get("/api/v1/auth/users", headers=headers)
    assert listed.status_code == 200
    assert {user["username"] for user in listed.json()} == {"admin", "novo"}
    assert "password_hash" not in listed.text

    deactivated = client.patch(f"/api/v1/auth/users/{body['id']}", json={"ativo": False}, headers=headers)
    assert deactivated.status_code == 200
    assert deactivated.json()["ativo"] is False


def test_deactivating_a_user_revokes_their_session_immediately(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="admin", password="senha-forte-123", role=UserRole.ADMIN)
    _create_user(repository, username="operador1", password="senha-forte-123", role=UserRole.OPERATOR)

    operator_login = _login(client, username="operador1", password="senha-forte-123")
    operator_headers = _session_headers(operator_login)
    assert client.get("/api/v1/auth/me", headers=operator_headers).status_code == 200

    admin_login = _login(client, username="admin", password="senha-forte-123")
    admin_headers = _session_headers(admin_login)
    deactivated = client.patch("/api/v1/auth/users/2", json={"ativo": False}, headers=admin_headers)
    assert deactivated.status_code == 200

    assert client.get("/api/v1/auth/me", headers=operator_headers).status_code == 401


def test_create_user_rejects_duplicate_username_with_409(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="admin", password="senha-forte-123", role=UserRole.ADMIN)
    login = _login(client, username="admin", password="senha-forte-123")

    response = client.post(
        "/api/v1/auth/users",
        json={"username": "admin", "nome": "Outro", "password": "senha-forte-123", "role": "operador"},
        headers=_session_headers(login),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "USERNAME_ALREADY_EXISTS"


def test_patch_user_requires_admin_role(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="admin", password="senha-forte-123", role=UserRole.ADMIN)
    _create_user(repository, username="operador1", password="senha-forte-123", role=UserRole.OPERATOR)
    login = _login(client, username="operador1", password="senha-forte-123")

    response = client.patch(
        "/api/v1/auth/users/1", json={"ativo": False}, headers=_session_headers(login)
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN_ROLE"


def test_patch_user_returns_404_for_unknown_user(client: TestClient) -> None:
    repository = _enable_auth_service(client)
    _create_user(repository, username="admin", password="senha-forte-123", role=UserRole.ADMIN)
    login = _login(client, username="admin", password="senha-forte-123")

    response = client.patch(
        "/api/v1/auth/users/999", json={"ativo": False}, headers=_session_headers(login)
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "USER_NOT_FOUND"
