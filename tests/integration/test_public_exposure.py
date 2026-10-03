"""Garantias da API exposta na internet sem VPN nem lista de IPs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.application import auth as auth_module


def test_unknown_user_spends_the_same_password_verification(client: TestClient, monkeypatch) -> None:
    verified_hashes: list[str] = []
    original = auth_module.verify_password

    def counting_verify(password: str, password_hash: str) -> bool:
        verified_hashes.append(password_hash)
        return original(password, password_hash)

    monkeypatch.setattr(auth_module, "verify_password", counting_verify)
    client.cookies.clear()

    response = client.post("/api/v1/auth/login", json={"username": "nao-existe", "password": "x" * 12})

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"
    assert len(verified_hashes) == 1
    assert verified_hashes[0].startswith("$argon2")


def test_unexpected_error_returns_problem_without_internals(client: TestClient) -> None:
    def boom() -> None:
        raise RuntimeError("segredo interno: senha=123")

    client.app.add_api_route("/api/v1/_teste_erro", boom)
    with TestClient(client.app, raise_server_exceptions=False) as raw:
        response = raw.get("/api/v1/_teste_erro", headers={"X-Correlation-ID": "rastreio-1"})

    body = response.json()
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "segredo" not in response.text
    assert body["correlation_id"]
