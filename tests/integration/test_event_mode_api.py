from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.application.auth import AuthService
from app.application.event_mode import EventModeService, EventModeSettings
from app.core.security import hash_password
from app.domain.auth import UserRole
from tests.fakes import (
    FakeAuthRepository,
    FakeAutomation,
    FakeEventModeRepository,
    FakeEventSecrets,
    FakeReturnGuard,
)

T0 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


@pytest.fixture
def admin(client: TestClient):
    """Liga um modo eventos em memória e entra como administrador; devolve automação e relógio."""
    repository = FakeAuthRepository()
    for username, role in (("admin", UserRole.ADMIN), ("operador", UserRole.OPERATOR)):
        repository.create_user(
            username=username,
            nome=username.title(),
            password_hash=hash_password("senha-forte-123"),
            role=role,
            criado_por=None,
        )
    client.app.state.auth_service = AuthService(repository)
    clock = {"now": T0}
    automation = FakeAutomation()
    secrets = FakeEventSecrets()
    client.app.state.event_mode_service = EventModeService(
        FakeEventModeRepository(),
        EventModeSettings(
            normal_instance_type="t3a.small",
            event_instance_type="t3a.large",
            min_active_minutes=30,
            default_duration_hours=5,
            max_duration_hours=24,
            extra_cost_usd_per_hour=0.0564,
            region="us-east-1",
            db_host="db.example",
            db_name="protected_areas_sc",
            ogc_base_url="https://areasprotegidas-sc.com/geoserver/protected_areas_sc/",
        ),
        automation=automation,
        return_guard=FakeReturnGuard(),
        secrets=secrets,
        imports_in_flight=lambda: 0,
        clock=lambda: clock["now"],
    )
    client.cookies.clear()
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "senha-forte-123"}).status_code == 200
    return {"automation": automation, "clock": clock, "secrets": secrets}


def _activate(client: TestClient, key: str = "evento-1"):
    return client.post(
        "/api/v1/admin/event-mode",
        json={"nome": "Univali", "confirmacao": "ATIVAR"},
        headers={"Idempotency-Key": key},
    )


def test_operator_cannot_see_or_use_event_mode(client: TestClient, admin) -> None:
    client.cookies.clear()
    client.post("/api/v1/auth/login", json={"username": "operador", "password": "senha-forte-123"})

    assert client.get("/api/v1/admin/event-mode").status_code == 403
    assert _activate(client).status_code == 403
    assert admin["automation"].started == []


def test_admin_activates_and_follows_the_state(client: TestClient, admin) -> None:
    response = _activate(client)

    assert response.status_code == 202
    assert response.json()["status"] == "ativando"
    state = client.get("/api/v1/admin/event-mode").json()
    assert state["configured"] is True
    assert state["rules"]["min_active_minutes"] == 30
    assert state["current"]["current_step"] == "pararServidor"


def test_early_deactivation_answers_409_with_retry_after(client: TestClient, admin) -> None:
    event_id = _activate(client).json()["id"]
    admin["automation"].finish("exec-1")
    client.get("/api/v1/admin/event-mode")
    admin["clock"]["now"] = T0 + timedelta(minutes=12)

    response = client.post(f"/api/v1/admin/event-mode/{event_id}/deactivate", json={"confirmacao": "DESATIVAR"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "EVENT_MODE_MIN_ACTIVE_TIME"
    assert response.headers["retry-after"] == str(18 * 60)


def test_credentials_are_not_cached(client: TestClient, admin) -> None:
    event_id = _activate(client).json()["id"]
    admin["automation"].finish("exec-1")
    client.get("/api/v1/admin/event-mode")
    admin["secrets"].passwords[event_id] = "senha-gerada"

    response = client.get(f"/api/v1/admin/event-mode/{event_id}/credentials")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["db_login"] == "ws_univali_20261020"


def test_without_database_the_routes_answer_503(client: TestClient, admin) -> None:
    client.app.state.event_mode_service = None
    response = client.get("/api/v1/admin/event-mode")
    assert response.status_code == 503
    assert response.json()["error_code"] == "EVENT_MODE_NOT_CONFIGURED"
