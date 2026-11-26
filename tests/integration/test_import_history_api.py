from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password
from app.domain.auth import UserRole
from app.main import create_app
from tests.conftest import uc_metadata, za_metadata
from tests.integration.test_submissions_api import _submit

SECOND_USERNAME = "outra-operadora"
SECOND_PASSWORD = "outra-senha-forte-456"


def _login_as_second_user(client: TestClient) -> None:
    repository = client.app.state.auth_service.repository
    if repository.get_user_by_username(SECOND_USERNAME) is None:
        repository.create_user(
            username=SECOND_USERNAME,
            nome="Outra Operadora",
            password_hash=hash_password(SECOND_PASSWORD),
            role=UserRole.OPERATOR,
            criado_por=None,
        )
    response = client.post(
        "/api/v1/auth/login", json={"username": SECOND_USERNAME, "password": SECOND_PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_lists_only_the_session_accounts_imports_newest_first(
    client: TestClient, valid_uc_geojson: bytes, valid_uc_point_geojson: bytes
) -> None:
    polygon = _submit(client, valid_uc_geojson, key="history-polygon").json()
    point = _submit(client, valid_uc_point_geojson, key="history-point").json()

    page = client.get("/api/v1/imports")

    assert page.status_code == 200, page.text
    body = page.json()
    assert [item["import_id"] for item in body["items"]] == [point["import_id"], polygon["import_id"]]
    assert body["items"][0]["geometry_types"] == ["Point"]
    assert body["items"][1]["geometry_types"] == ["Polygon"]
    assert body["has_more"] is False

    _login_as_second_user(client)
    assert client.get("/api/v1/imports").json()["items"] == []


def test_history_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/imports")
    assert response.status_code == 401


def test_history_survives_a_new_browser_session_and_an_api_restart(
    client: TestClient, settings: Settings, valid_uc_geojson: bytes
) -> None:
    created = _submit(client, valid_uc_geojson, key="history-restart").json()
    auth_service = client.app.state.auth_service

    # Mesmo diretório de dados, processo novo da API e login novo: é o cenário em que o
    # histórico antigo, guardado só no sessionStorage da aba, aparecia vazio.
    with TestClient(create_app(settings)) as restarted:
        restarted.app.state.auth_service = auth_service
        login = restarted.post(
            "/api/v1/auth/login", json={"username": "operador-teste", "password": "senha-forte-123"}
        )
        assert login.status_code == 200, login.text
        items = restarted.get("/api/v1/imports").json()["items"]

    assert [item["import_id"] for item in items] == [created["import_id"]]
    assert items[0]["status"] == created["status"]


def test_batch_appears_once_and_belongs_to_whoever_grouped_it(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    uc = _submit(client, valid_uc_geojson, key="history-batch-uc",
                 metadata=uc_metadata(official_identifier="HIST-UC")).json()
    za = _submit(client, valid_uc_geojson, key="history-batch-za", domain="za_oficial",
                 metadata=za_metadata(uc_identifier="HIST-UC")).json()
    loose = _submit(client, valid_uc_geojson, key="history-loose").json()

    _login_as_second_user(client)
    batch = client.post(
        "/api/v1/import-batches",
        json={"uc_import_id": uc["import_id"], "official_zone_import_id": za["import_id"]},
        headers={"Idempotency-Key": "history-batch"},
    )
    assert batch.status_code == 202, batch.text

    second_items = client.get("/api/v1/imports").json()["items"]
    assert [item["import_id"] for item in second_items] == [batch.json()["batch_id"]]
    assert second_items[0]["operation"] == "create_with_zone"

    client.post("/api/v1/auth/login", json={"username": "operador-teste", "password": "senha-forte-123"})
    first_ids = [item["import_id"] for item in client.get("/api/v1/imports").json()["items"]]
    assert first_ids == [loose["import_id"]]


def test_history_pages_with_limit_and_offset(client: TestClient, valid_uc_geojson: bytes) -> None:
    ids = [_submit(client, valid_uc_geojson, key=f"history-page-{n}").json()["import_id"] for n in range(3)]

    first = client.get("/api/v1/imports", params={"limit": 2}).json()
    second = client.get("/api/v1/imports", params={"limit": 2, "offset": 2}).json()

    # Mesmo arquivo em três envios com chaves diferentes: são três importações distintas, e a
    # paginação precisa cobrir todas sem repetir nem pular nenhuma.
    assert first["has_more"] is True
    assert second["has_more"] is False
    listed = [item["import_id"] for item in first["items"] + second["items"]]
    assert sorted(listed) == sorted(ids)
    assert len(set(listed)) == 3


def test_admin_sees_every_account_with_who_sent_it(client: TestClient, valid_uc_geojson: bytes) -> None:
    mine = _submit(client, valid_uc_geojson, key="history-scope-first").json()
    _login_as_second_user(client)
    theirs = _submit(client, valid_uc_geojson, key="history-scope-second").json()

    repository = client.app.state.auth_service.repository
    repository.create_user(
        username="admin-historico",
        nome="Admin do Histórico",
        password_hash=hash_password("senha-admin-789"),
        role=UserRole.ADMIN,
        criado_por=None,
    )
    login = client.post("/api/v1/auth/login", json={"username": "admin-historico", "password": "senha-admin-789"})
    assert login.status_code == 200, login.text

    every = client.get("/api/v1/imports", params={"scope": "all"}).json()["items"]
    assert [(item["import_id"], item["submitted_by"]) for item in every] == [
        (theirs["import_id"], "Outra Operadora"),
        (mine["import_id"], "Operador de Teste"),
    ]
    # Sem `scope`, o administrador continua vendo só a própria conta.
    assert client.get("/api/v1/imports").json()["items"] == []


def test_operator_cannot_list_every_account(client: TestClient, valid_uc_geojson: bytes) -> None:
    created = _submit(client, valid_uc_geojson, key="history-scope-operator").json()

    response = client.get("/api/v1/imports", params={"scope": "all"})

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN_ROLE"
    own = client.get("/api/v1/imports").json()["items"]
    assert [(item["import_id"], item["submitted_by"]) for item in own] == [
        (created["import_id"], "Operador de Teste")
    ]
    assert client.get("/api/v1/imports", params={"scope": "outra"}).status_code == 422


def test_history_rejects_out_of_range_page_size(client: TestClient) -> None:
    assert client.get("/api/v1/imports", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/imports", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/imports", params={"offset": -1}).status_code == 422
