"""Confirms /imports and /ucs actually enforce the session added in 27/09/2026.

`tests/conftest.py`'s `client` fixture authenticates by default so the rest of the suite can
focus on geospatial/cadastral behavior; these tests are the ones that deliberately drop the
session to prove the router-level `Depends(get_current_user)` guard is really there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_create_import_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/imports",
        headers={"Idempotency-Key": "no-session-create"},
        data={"domain": "uc", "operation": "create", "metadata": "{}"},
        files={"file": ("empty.geojson", b"{}", "application/geo+json")},
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_get_import_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/imports/any-id")
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_publish_import_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post("/api/v1/imports/any-id/publish")
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_get_uc_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/ucs/101")
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_list_buffer_abrangencia_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/ucs/101/buffer-abrangencia")
    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


def test_an_expired_session_is_rejected_the_same_way(client: TestClient) -> None:
    # Força a expiração direto no relógio da sessão (fake repository) em vez de esperar o TTL.
    expired = datetime.now(UTC) - timedelta(seconds=1)
    sessions = client.app.state.auth_service.repository.sessions
    for token_hash, (user_id, _expira_em, revogado_em) in list(sessions.items()):
        sessions[token_hash] = (user_id, expired, revogado_em)

    response = client.get("/api/v1/ucs/101")
    assert response.status_code == 401
    assert response.json()["error_code"] == "SESSION_INVALID"
