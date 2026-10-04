from __future__ import annotations

from fastapi.testclient import TestClient


def test_dictionary_is_public_and_cacheable(client: TestClient) -> None:
    client.cookies.clear()

    response = client.get("/api/v1/data-dictionary")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    body = response.json()
    assert body["version"] == "1"
    assert {base["id"] for base in body["bases"]} >= {"uc-create", "uc-update", "za-replace-buffer"}
    limits = {limit["key"]: limit["value"] for limit in body["limits"]}
    assert limits["max_features"] == 100  # valor do fixture `settings`, não o padrão


def test_dictionary_answers_not_modified_for_the_same_etag(client: TestClient) -> None:
    first = client.get("/api/v1/data-dictionary")

    second = client.get("/api/v1/data-dictionary", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]
