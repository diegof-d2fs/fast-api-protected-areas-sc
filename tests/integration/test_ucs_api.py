from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from app.application.ucs import UCService
from app.domain.ucs import BufferAbrangenciaView, UCDetailView, UCSearchView, UCStatus


class FakeReadOnlyUCRepository:
    def get(self, uc_id: int) -> UCDetailView | None:
        if uc_id != 101:
            return None
        return UCDetailView(
            id=101,
            name="Parque Estadual de Teste",
            status=UCStatus.ACTIVE,
            geometry={
                "type": "Polygon",
                "coordinates": [
                    [[-49.2, -27.2], [-49.0, -27.2], [-49.0, -27.0], [-49.2, -27.2]]
                ],
            },
            geometry_type="Polygon",
            geometry_version=1,
            official_identifier="UC-101",
            valid_from=date(2026, 8, 16),
            updated_at=datetime.now(UTC),
            links={"self": "/api/v1/ucs/101"},
        )

    def list_buffer_abrangencia(self, uc_id: int) -> list[BufferAbrangenciaView] | None:
        if uc_id != 101:
            return None
        return []

    def find_by_identifier(
        self, *, cd_cnuc: str | None = None, wdpa_pid: str | None = None
    ) -> UCSearchView | None:
        if cd_cnuc != "0000.00.0101" and wdpa_pid != "101":
            return None
        return UCSearchView(
            id=101,
            name="Parque Estadual de Teste",
            status=UCStatus.ACTIVE,
            geometry_version=7,
            official_identifier="UC-101",
            cnuc_code="0000.00.0101",
            wdpa_pid="101",
            links={"self": "/api/v1/ucs/101"},
        )


def _enable_read_only_uc_service(client: TestClient) -> None:
    client.app.state.uc_service = UCService(FakeReadOnlyUCRepository())


def test_uc_collection_does_not_expose_direct_postgis_write(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ucs",
        headers={"Idempotency-Key": "forbidden-direct-write"},
        json={"name": "UC que não pode contornar o pipeline"},
    )

    assert response.status_code == 405


def test_get_uc_is_a_read_only_query(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    response = client.get("/api/v1/ucs/101")
    assert response.status_code == 200
    assert response.json()["id"] == 101
    assert response.json()["geometry_type"] == "Polygon"


def test_search_uc_by_cnuc_returns_current_version(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    response = client.get("/api/v1/ucs", params={"cd_cnuc": "0000.00.0101"})
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 101,
            "name": "Parque Estadual de Teste",
            "status": "ATIVA",
            "geometry_version": 7,
            "official_identifier": "UC-101",
            "cnuc_code": "0000.00.0101",
            "wdpa_pid": "101",
            "links": {"self": "/api/v1/ucs/101"},
        }
    ]


def test_search_uc_by_wdpa_pid_returns_zero_or_one_result(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    found = client.get("/api/v1/ucs", params={"wdpa_pid": "101"})
    missing = client.get("/api/v1/ucs", params={"wdpa_pid": "999"})
    assert found.status_code == 200
    assert found.json()[0]["geometry_version"] == 7
    assert missing.status_code == 200
    assert missing.json() == []


def test_search_uc_requires_exactly_one_identifier(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    missing = client.get("/api/v1/ucs")
    combined = client.get(
        "/api/v1/ucs", params={"cd_cnuc": "0000.00.0101", "wdpa_pid": "101"}
    )
    blank = client.get("/api/v1/ucs", params={"cd_cnuc": "   "})
    assert missing.status_code == 400
    assert combined.status_code == 400
    assert blank.status_code == 400
    assert missing.json()["error_code"] == "INVALID_UC_SEARCH_FILTER"


def test_get_buffer_abrangencia_is_a_read_only_query(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    response = client.get("/api/v1/ucs/101/buffer-abrangencia")
    assert response.status_code == 200
    assert response.json() == []


def test_uc_query_requires_postgis_configuration(client: TestClient) -> None:
    response = client.get("/api/v1/ucs/101")
    assert response.status_code == 503
    assert response.json()["error_code"] == "CADASTRAL_DATABASE_NOT_CONFIGURED"


def test_extinguish_command_preserves_snapshot_and_replays(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    body = {"expected_version": 1, "source": "Ato legal", "reason": "Extinção publicada"}
    headers = {"Idempotency-Key": "extinction-101"}
    first = client.post("/api/v1/ucs/101/extinguish", json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "ACCEPTED"
    assert first.json()["operation"] == "extinguish"
    assert first.json()["metadata"]["actor"] == "user:1:Operador de Teste"
    replay = client.post("/api/v1/ucs/101/extinguish", json=body, headers=headers)
    assert replay.json()["import_id"] == first.json()["import_id"]
    changed = client.post("/api/v1/ucs/101/extinguish",
                          json={**body, "reason": "Outra intenção"}, headers=headers)
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_extinguish_command_rejects_stale_version(client: TestClient) -> None:
    _enable_read_only_uc_service(client)
    response = client.post("/api/v1/ucs/101/extinguish",
                           json={"expected_version": 2, "source": "Ato", "reason": "Extinção"},
                           headers={"Idempotency-Key": "extinction-stale"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "UC_VERSION_CONFLICT"
