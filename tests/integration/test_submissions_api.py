from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.infrastructure.airflow.client import AirflowDagRun
from app.infrastructure.storage.bronze import LocalBronzePublisher
from tests.conftest import uc_metadata, za_metadata


class FakeAirflowClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.state = "running"

    def trigger(self, dag_id: str, dag_run_id: str, conf: dict):
        self.calls.append((dag_id, dag_run_id, conf))

    def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun:
        return AirflowDagRun(dag_id=dag_id, dag_run_id=dag_run_id, state=self.state)


class FakeUCDuplicateChecker:
    def __init__(self, matches: list[dict] | None = None) -> None:
        self.matches = matches or []
        self.calls: list[dict] = []

    def find_create_duplicates(self, **identities):
        self.calls.append(identities)
        return self.matches


class SelectiveUCDuplicateChecker(FakeUCDuplicateChecker):
    def __init__(self, matches_by_identifier: dict[str, list[dict]]) -> None:
        super().__init__()
        self.matches_by_identifier = matches_by_identifier

    def find_create_duplicates(self, **identities):
        self.calls.append(identities)
        matches = []
        for identifier in identities["uc_ids"]:
            matches.extend(self.matches_by_identifier.get(identifier, []))
        return matches


def _submit(
    client: TestClient,
    content: bytes,
    *,
    filename: str = "uc.geojson",
    domain: str = "uc",
    metadata: str | None = None,
    key: str = "test-key-1",
    repair_geometry: bool = False,
    operation: str = "create",
    duplicate_policy: str = "reject_batch",
):
    return client.post(
        "/api/v1/imports",
        headers={"Idempotency-Key": key, "X-Correlation-ID": "test-correlation"},
        data={
            "domain": domain,
            "operation": operation,
            "metadata": metadata or (uc_metadata() if domain == "uc" else za_metadata()),
            "repair_geometry": str(repair_geometry).lower(),
            "duplicate_policy": duplicate_policy,
        },
        files={"file": (filename, content, "application/octet-stream")},
    )


def test_health_and_openapi(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"storage": True, "submission_repository": True}
    openapi = client.get("/api/openapi.json")
    assert openapi.status_code == 200
    schema = openapi.json()
    assert schema["info"]["version"] == "0.5.0"
    assert "/api/v1/imports" in schema["paths"]
    assert "/api/v1/submissions" not in schema["paths"]
    assert schema["paths"]["/api/v1/imports"]["post"]["summary"] == "Enviar UC ou ZA oficial para validação"
    assert [tag["name"] for tag in schema["tags"]] == [
        "Importações geoespaciais",
        "Unidades de Conservação",
        "Autenticação",
        "Saúde",
    ]
    assert set(schema["paths"]["/api/v1/ucs"]) == {"get"}
    assert schema["paths"]["/api/v1/ucs"]["get"]["summary"] == (
        "Buscar uma UC por identificador forte"
    )
    assert schema["paths"]["/api/v1/ucs/{uc_id}"]["get"]["tags"] == [
        "Unidades de Conservação"
    ]
    publish = schema["paths"]["/api/v1/imports/{import_id}/publish"]["post"]
    assert publish["summary"] == "Publicar uma importação aceita e iniciar o pipeline"
    create_description = schema["paths"]["/api/v1/imports"]["post"]["description"]
    assert "Uma UC pontual permanece como ponto" in create_description
    assert "pertence exclusivamente ao Airflow" in create_description


def test_accepts_geojson_and_persists_artifacts(
    client: TestClient,
    settings,
    valid_uc_geojson: bytes,
) -> None:
    response = _submit(client, valid_uc_geojson)
    assert response.status_code == 202, response.json()
    payload = response.json()
    assert payload["status"] == "ACCEPTED"
    assert payload["detected_format"] == "geojson"
    assert payload["correlation_id"] == "test-correlation"

    validation = client.get(payload["links"]["validation"])
    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    assert validation.json()["target_crs"] == "EPSG:4674"
    assert validation.json()["feature_count"] == 1

    manifest = settings.data_root.joinpath(*Path(payload["manifest_key"]).parts)
    canonical = settings.data_root.joinpath(*Path(payload["canonical_key"]).parts)
    original = settings.data_root.joinpath(*Path(payload["original_key"]).parts)
    assert manifest.is_file()
    assert canonical.is_file()
    assert original.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["import_id"] == payload["import_id"]


def test_accepts_point_uc_without_converting_it_in_the_api(
    client: TestClient,
    settings,
    valid_uc_point_geojson: bytes,
) -> None:
    response = _submit(
        client,
        valid_uc_point_geojson,
        metadata=uc_metadata(
            name="UC pontual de teste",
            official_identifier="UC-POINT-TEST-001",
        ),
        key="uc-point-create",
    )

    assert response.status_code == 202, response.json()
    payload = response.json()
    assert payload["status"] == "ACCEPTED"
    report = client.get(payload["links"]["validation"]).json()
    assert report["geometry_types"] == ["Point"]
    canonical_path = settings.data_root.joinpath(*Path(payload["canonical_key"]).parts)
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert canonical["features"][0]["geometry"]["type"] == "Point"


def test_accepts_atomic_create_batch_with_multiple_uc_features(client: TestClient) -> None:
    multiple = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {"nm_uc": "UC pontual A", "uc_id": "BATCH-UC-A"},
                    "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]},
                },
                {
                    "type": "Feature",
                    "properties": {"nm_uc": "UC pontual B", "uc_id": "BATCH-UC-B"},
                    "geometry": {"type": "Point", "coordinates": [-49.2, -27.2]},
                },
            ],
        }
    ).encode("utf-8")

    response = _submit(client, multiple, key="uc-multiple-create")

    assert response.status_code == 202
    assert response.json()["status"] == "ACCEPTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["valid"] is True
    assert response.json()["batch_items"] == [
        {"feature_index": 0, "name": "UC pontual A", "identities": {"official_identifier": "BATCH-UC-A"}},
        {"feature_index": 1, "name": "UC pontual B", "identities": {"official_identifier": "BATCH-UC-B"}},
    ]


def test_rejects_multi_uc_batch_without_identity_per_feature(client: TestClient) -> None:
    multiple = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {"type": "Feature", "properties": {"nm_uc": "UC A", "uc_id": "BATCH-A"}, "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]}},
                {"type": "Feature", "properties": {"nm_uc": "UC B"}, "geometry": {"type": "Point", "coordinates": [-49.2, -27.2]}},
            ],
        }
    ).encode("utf-8")

    response = _submit(client, multiple, key="uc-multiple-missing-identity")

    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "MISSING_UC_IDENTITY"
    assert report["errors"][0]["feature"] == 1


def test_idempotency_replays_same_request_and_rejects_changed_payload(
    client: TestClient,
    valid_uc_geojson: bytes,
) -> None:
    first = _submit(client, valid_uc_geojson, key="same-key")
    second = _submit(client, valid_uc_geojson, key="same-key")
    assert second.status_code == 202
    assert second.json()["import_id"] == first.json()["import_id"]

    changed = _submit(client, valid_uc_geojson, key="same-key", metadata=uc_metadata(name="Outra UC"))
    assert changed.status_code == 409
    assert changed.headers["content-type"].startswith("application/problem+json")
    assert changed.json()["error_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_infers_uc_name_from_file_when_metadata_omits_it(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    response = _submit(client, valid_uc_geojson, metadata=json.dumps({"source": "teste"}))
    assert response.status_code == 202
    assert response.json()["status"] == "ACCEPTED"
    assert response.json()["metadata"]["name"] == "UC de teste"


def test_rejects_uc_without_name_in_metadata_or_file(client: TestClient) -> None:
    unnamed = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-49.2, -27.2],
                                [-49.0, -27.2],
                                [-49.0, -27.0],
                                [-49.2, -27.0],
                                [-49.2, -27.2],
                            ]
                        ],
                    },
                }
            ],
        }
    ).encode()
    response = _submit(client, unnamed, metadata=json.dumps({"source": "teste"}), key="no-name")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "MISSING_UC_NAME"


def test_geospatial_content_error_creates_rejected_submission(client: TestClient) -> None:
    invalid = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]},
                }
            ],
        }
    ).encode()
    response = _submit(client, invalid, key="missing-crs")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "MISSING_CRS"


def test_za_rejects_point_geometry(client: TestClient) -> None:
    point = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]},
                }
            ],
        }
    ).encode()
    response = _submit(client, point, domain="za_oficial", key="za-point")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "GEOMETRY_TYPE_NOT_ALLOWED"


def test_replace_buffer_abrangencia_requires_official_zone_and_auditable_reason(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    wrong_domain = _submit(
        client,
        valid_uc_geojson,
        operation="replace_buffer_abrangencia",
        key="replace-buffer-abrangencia-wrong-domain",
    )
    assert wrong_domain.status_code == 400
    assert wrong_domain.json()["error_code"] == "REPLACE_BUFFER_ABRANGENCIA_DOMAIN_NOT_ENABLED"

    missing_reason = _submit(
        client,
        valid_uc_geojson,
        domain="za_oficial",
        operation="replace_buffer_abrangencia",
        key="replace-buffer-abrangencia-missing-reason",
    )
    assert missing_reason.status_code == 400
    assert missing_reason.json()["error_code"] == "INVALID_REPLACE_BUFFER_ABRANGENCIA_METADATA"


def test_replace_buffer_abrangencia_requires_exactly_one_official_zone(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    payload = json.loads(valid_uc_geojson)
    payload["features"].append(payload["features"][0])
    response = _submit(
        client,
        json.dumps(payload).encode(),
        domain="za_oficial",
        operation="replace_buffer_abrangencia",
        metadata=za_metadata(reason="Publicação da ZA oficial"),
        key="replace-buffer-abrangencia-multiple-features",
    )

    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "OPERATION_REQUIRES_SINGLE_FEATURE"


def test_accepts_kml(client: TestClient, valid_kml: bytes) -> None:
    response = _submit(client, valid_kml, filename="uc.kml", key="kml-key")
    assert response.status_code == 202
    assert response.json()["status"] == "ACCEPTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["source_crs"] == "EPSG:4326"
    assert report["warnings"][0]["code"] == "KML_CRS_DEFINED_BY_FORMAT"


def test_accepts_safe_shapefile_zip(client: TestClient, valid_shapefile_zip: bytes) -> None:
    response = _submit(client, valid_shapefile_zip, filename="uc.zip", key="shp-key")
    assert response.status_code == 202
    report = client.get(response.json()["links"]["validation"]).json()
    assert response.json()["status"] == "ACCEPTED", report
    assert response.json()["detected_format"] == "shapefile_zip"


def test_rejects_zip_slip(client: TestClient) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../uc.shp", b"invalid")
        archive.writestr("../uc.shx", b"invalid")
        archive.writestr("../uc.dbf", b"invalid")
        archive.writestr("../uc.prj", b"invalid")
    response = _submit(client, output.getvalue(), filename="attack.zip", key="zip-slip")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "ZIP_SLIP"


def test_rejects_shapefile_without_prj(client: TestClient) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("uc.shp", b"placeholder")
        archive.writestr("uc.shx", b"placeholder")
        archive.writestr("uc.dbf", b"placeholder")
    response = _submit(client, output.getvalue(), filename="missing-prj.zip", key="missing-prj")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "MISSING_SHAPEFILE_SIDECARS"


def test_rejects_invalid_geometry_by_default(client: TestClient) -> None:
    bow_tie = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-49.2, -27.2], [-49.0, -27.0], [-49.2, -27.0], [-49.0, -27.2], [-49.2, -27.2]]
                        ],
                    },
                }
            ],
        }
    ).encode()
    response = _submit(client, bow_tie, key="invalid-geometry")
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][0]["code"] == "INVALID_GEOMETRY"


def test_repairs_geometry_only_when_explicitly_requested(client: TestClient) -> None:
    polygon_with_external_hole = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-49.2, -27.2], [-49.0, -27.2], [-49.0, -27.0], [-49.2, -27.0], [-49.2, -27.2]],
                            [
                                [-49.25, -27.25],
                                [-49.249, -27.25],
                                [-49.249, -27.249],
                                [-49.25, -27.249],
                                [-49.25, -27.25],
                            ],
                        ],
                    },
                }
            ],
        }
    ).encode()
    response = _submit(
        client,
        polygon_with_external_hole,
        key="repair-explicit",
        repair_geometry=True,
    )
    assert response.status_code == 202
    assert response.json()["status"] == "ACCEPTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["repair_applied"] is True
    assert report["warnings"][0]["code"] == "GEOMETRY_REPAIRED"


def test_rejects_upload_above_limit(client: TestClient) -> None:
    response = _submit(client, b"{" + (b"x" * (2 * 1024 * 1024)), key="too-large")
    assert response.status_code == 413
    assert response.json()["error_code"] == "UPLOAD_TOO_LARGE"


def test_rejects_extension_content_mismatch(client: TestClient, valid_uc_geojson: bytes) -> None:
    response = _submit(client, valid_uc_geojson, filename="uc.kml", key="mismatch")
    assert response.status_code == 415
    assert response.json()["error_code"] == "UNSUPPORTED_GEOSPATIAL_FORMAT"


def test_publish_is_explicitly_deferred_to_pipeline_integration_phase(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    created = _submit(client, valid_uc_geojson, key="publish-disabled").json()
    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")
    assert response.status_code == 503
    assert response.json()["error_code"] == "PIPELINE_INTEGRATION_NOT_ENABLED"


def test_publish_copies_immutable_bronze_batch_and_triggers_airflow_idempotently(
    client: TestClient,
    settings,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created = _submit(client, valid_uc_geojson, key="publish-integrated").json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = FakeUCDuplicateChecker()

    first = client.post(f"/api/v1/imports/{created['import_id']}/publish")
    replay = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert first.status_code == replay.status_code == 202
    assert first.json()["status"] == replay.json()["status"] == "PROCESSING"
    assert first.json()["dag_id"] == "DAG_UCS"
    assert first.json()["dag_run_id"] == f"api__{created['import_id']}"
    manifest_key = first.json()["bronze_manifest_key"]
    manifest = (publisher.root / Path(manifest_key)).read_text(encoding="utf-8")
    assert created["import_id"] in manifest
    assert len(airflow.calls) == 1
    assert airflow.calls[0][2]["manifest_key"] == manifest_key

    airflow.state = "success"
    completed = client.get(f"/api/v1/imports/{created['import_id']}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "SUCCEEDED"


def test_publish_blocks_standalone_official_zone_create(
    client: TestClient,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created = _submit(
        client,
        valid_uc_geojson,
        domain="za_oficial",
        key="standalone-za-create",
    ).json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 409
    assert response.json()["error_code"] == "ZA_CREATE_REQUIRES_BATCH"
    assert airflow.calls == []
    assert not (publisher.root / "za" / f"import_id={created['import_id']}").exists()


def test_publish_replace_buffer_abrangencia_carries_atomic_transition_contract(
    client: TestClient,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created_response = _submit(
        client,
        valid_uc_geojson,
        domain="za_oficial",
        operation="replace_buffer_abrangencia",
        metadata=za_metadata(
            reason="ZA oficial publicada por ato legal",
            actor="operador-postman",
            valid_from="2026-08-16",
        ),
        key="replace-buffer-abrangencia-enabled",
    )
    assert created_response.status_code == 202
    created = created_response.json()
    assert created["status"] == "ACCEPTED"

    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "PROCESSING"
    assert response.json()["dag_id"] == "DAG_ZA_BUFFER"
    manifest_path = publisher.root.joinpath(*Path(response.json()["bronze_manifest_key"]).parts)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["domain"] == "za_oficial"
    assert manifest["operation"] == "replace_buffer_abrangencia"
    assert manifest["metadata"]["reason"] == "ZA oficial publicada por ato legal"
    assert airflow.calls[0][0] == "DAG_ZA_BUFFER"
    assert airflow.calls[0][2]["operation"] == "replace_buffer_abrangencia"


def test_publish_marks_existing_uc_as_duplicate_without_bronze_or_airflow(
    client: TestClient,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created = _submit(
        client,
        valid_uc_geojson,
        key="duplicate-create",
        metadata=uc_metadata(official_identifier="UC-001"),
    ).json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    checker = FakeUCDuplicateChecker(
        [
            {
                "uc_id": 42,
                "official_identifier": "UC-001",
                "cd_cnuc": "0000.00.0042",
                "wdpa_pid": "12345",
                "name": "UC já cadastrada",
                "matched_by": "uc_id",
            }
        ]
    )
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = checker

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 409
    assert response.json()["error_code"] == "UC_ALREADY_EXISTS"
    status_response = client.get(f"/api/v1/imports/{created['import_id']}")
    assert status_response.json()["status"] == "DUPLICATE"
    assert status_response.json()["error_code"] == "UC_ALREADY_EXISTS"
    assert status_response.json()["duplicate_matches"][0]["uc_id"] == 42
    assert checker.calls[0]["uc_ids"] == ["UC-001"]
    assert not (publisher.root / "ucs" / f"import_id={created['import_id']}").exists()
    assert airflow.calls == []


def test_publish_mixed_batch_skips_duplicates_with_per_feature_audit(
    client: TestClient,
    tmp_path: Path,
) -> None:
    content = json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {"type": "Feature", "properties": {"nm_uc": "UC A", "uc_id": "NEW-A"}, "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]}},
                {"type": "Feature", "properties": {"nm_uc": "UC B", "uc_id": "EXISTING-B"}, "geometry": {"type": "Point", "coordinates": [-49.2, -27.2]}},
                {"type": "Feature", "properties": {"nm_uc": "UC C", "uc_id": "NEW-C"}, "geometry": {"type": "Point", "coordinates": [-49.3, -27.3]}},
            ],
        }
    ).encode()
    created = _submit(
        client,
        content,
        key="mixed-skip",
        duplicate_policy="skip_duplicates",
    ).json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    checker = SelectiveUCDuplicateChecker(
        {
            "EXISTING-B": [
                {
                    "uc_id": 42,
                    "official_identifier": "EXISTING-B",
                    "cd_cnuc": None,
                    "wdpa_pid": None,
                    "name": "UC B existente",
                    "matched_by": "uc_id",
                }
            ]
        }
    )
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = checker

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 202, response.json()
    payload = response.json()
    assert payload["status"] == "PROCESSING"
    assert payload["duplicate_policy"] == "skip_duplicates"
    assert [item["status"] for item in payload["batch_items"]] == [
        "ACCEPTED",
        "SKIPPED_DUPLICATE",
        "ACCEPTED",
    ]
    manifest_path = publisher.root.joinpath(*Path(payload["bronze_manifest_key"]).parts)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    canonical_path = publisher.root.joinpath(*Path(manifest["bronze"]["canonical_key"]).parts)
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert [feature["properties"]["uc_id"] for feature in canonical["features"]] == [
        "NEW-A",
        "NEW-C",
    ]
    assert manifest["duplicate_policy"] == "skip_duplicates"
    assert manifest["batch_items"][1]["duplicate_matches"][0]["feature_index"] == 1
    assert len(airflow.calls) == 1


def test_skip_duplicates_rejects_batch_when_no_new_feature_remains(
    client: TestClient,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created = _submit(
        client,
        valid_uc_geojson,
        key="all-duplicates-skip",
        metadata=uc_metadata(official_identifier="EXISTING"),
        duplicate_policy="skip_duplicates",
    ).json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = FakeUCDuplicateChecker(
        [{"uc_id": 42, "official_identifier": "EXISTING", "cd_cnuc": None, "wdpa_pid": None, "name": "UC existente", "matched_by": "uc_id"}]
    )

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 409
    assert response.json()["error_code"] == "UC_ALREADY_EXISTS"
    assert client.get(f"/api/v1/imports/{created['import_id']}").json()["status"] == "DUPLICATE"
    assert airflow.calls == []
    assert not (publisher.root / "ucs" / f"import_id={created['import_id']}").exists()


def test_skip_duplicates_is_restricted_to_uc_create(
    client: TestClient,
    valid_uc_geojson: bytes,
) -> None:
    response = _submit(
        client,
        valid_uc_geojson,
        key="skip-update-not-allowed",
        operation="update",
        duplicate_policy="skip_duplicates",
        metadata=uc_metadata(
            official_identifier="UC-001",
            expected_version=1,
            reason="Ajuste do limite",
        ),
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "DUPLICATE_POLICY_NOT_ALLOWED"


def test_failed_airflow_run_exposes_authoritative_duplicate_result(
    client: TestClient,
    valid_uc_geojson: bytes,
    tmp_path: Path,
) -> None:
    created = _submit(client, valid_uc_geojson, key="duplicate-race").json()
    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = FakeUCDuplicateChecker()
    published = client.post(f"/api/v1/imports/{created['import_id']}/publish")
    assert published.status_code == 202

    result_dir = (
        publisher.root.parent
        / "quality"
        / "api_results"
        / f"import_id={created['import_id']}"
    )
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "import_id": created["import_id"],
                "status": "DUPLICATE",
                "error_code": "UC_ALREADY_EXISTS",
                "error_detail": "A UC já existe no PostGIS.",
                "duplicate_matches": [
                    {
                        "uc_id": 99,
                        "name": "UC concorrente",
                        "matched_by": "geometry",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    airflow.state = "failed"

    status_response = client.get(f"/api/v1/imports/{created['import_id']}")

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "DUPLICATE"
    assert status_response.json()["duplicate_matches"][0]["matched_by"] == "geometry"


def test_update_requires_version_reason_and_strong_identity(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    response = _submit(
        client,
        valid_uc_geojson,
        key="update-incomplete",
        operation="update",
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_UPDATE_METADATA"
    fields = {item["field"] for item in response.json()["violations"]}
    assert fields == {"expected_version", "reason", "official_identifier"}


def test_replace_point_requires_version_reason_and_strong_identity(
    client: TestClient, valid_uc_geojson: bytes
) -> None:
    response = _submit(
        client,
        valid_uc_geojson,
        key="replace-point-incomplete",
        operation="replace_point",
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REPLACE_POINT_METADATA"
    fields = {item["field"] for item in response.json()["violations"]}
    assert fields == {"expected_version", "reason", "official_identifier"}


def test_replace_point_rejects_non_polygon_geometry(
    client: TestClient, valid_uc_point_geojson: bytes
) -> None:
    response = _submit(
        client,
        valid_uc_point_geojson,
        key="replace-point-with-point",
        operation="replace_point",
        metadata=uc_metadata(
            name="UC pontual",
            official_identifier="UC-POINT-001",
            expected_version=1,
            reason="Limite oficial recebido",
        ),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    report = client.get(response.json()["links"]["validation"]).json()
    assert report["errors"][-1]["code"] == "REPLACE_POINT_REQUIRES_POLYGON"


def test_publish_replace_point_carries_versioned_contract_to_airflow(
    client: TestClient,
    tmp_path: Path,
    valid_uc_geojson: bytes,
) -> None:
    created = _submit(
        client,
        valid_uc_geojson,
        key="replace-point-enabled",
        operation="replace_point",
        metadata=uc_metadata(
            name="UC com limite oficial",
            official_identifier="UC-POINT-001",
            expected_version=1,
            reason="Substituição por polígono oficial",
        ),
    ).json()
    assert created["status"] == "ACCEPTED"

    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "PROCESSING"
    assert response.json()["dag_id"] == "DAG_UCS"
    manifest_path = publisher.root.joinpath(*Path(response.json()["bronze_manifest_key"]).parts)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["operation"] == "replace_point"
    assert manifest["metadata"]["expected_version"] == 1
    assert airflow.calls[0][2]["operation"] == "replace_point"


def test_publish_update_carries_versioned_audit_contract_to_bronze_and_airflow(
    client: TestClient,
    tmp_path: Path,
    valid_uc_geojson: bytes,
) -> None:
    metadata = json.dumps(
        {
            "source": "teste de atualização",
            "name": "UC de teste atualizada",
            "official_identifier": "123",
            "expected_version": 1,
            "reason": "Correção do limite oficial",
            # A API sobrescreve qualquer `actor` enviado pelo cliente com a identidade da sessão
            # autenticada (RNF16); este valor serve só para confirmar que a sobrescrita acontece.
            "actor": "operador-postman",
        }
    )
    created_response = _submit(
        client,
        valid_uc_geojson,
        key="update-enabled",
        operation="update",
        metadata=metadata,
    )
    assert created_response.status_code == 202
    created = created_response.json()

    publisher = LocalBronzePublisher(tmp_path / "pipeline" / "bronze", client.app.state.storage)
    publisher.initialize()
    airflow = FakeAirflowClient()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "PROCESSING"
    manifest_path = publisher.root.joinpath(*Path(response.json()["bronze_manifest_key"]).parts)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["operation"] == "update"
    assert manifest["idempotency_key"] == "update-enabled"
    assert manifest["actor"] == "user:1:Operador de Teste"
    assert manifest["metadata"]["expected_version"] == 1
    assert airflow.calls[0][2]["operation"] == "update"
