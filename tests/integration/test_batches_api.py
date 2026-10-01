from pathlib import Path

from app.infrastructure.storage.bronze import LocalBronzePublisher
from tests.conftest import uc_metadata, za_metadata
from tests.integration.test_submissions_api import FakeAirflowClient, FakeUCDuplicateChecker, _submit


def test_atomic_batch_groups_reserves_publishes_and_replays(client, valid_uc_geojson, tmp_path: Path):
    uc = _submit(client, valid_uc_geojson, key="batch-uc",
                 metadata=uc_metadata(official_identifier="BATCH-UC")).json()
    za = _submit(client, valid_uc_geojson, key="batch-za", domain="za_oficial",
                 metadata=za_metadata(uc_identifier="BATCH-UC")).json()
    body = {"uc_import_id": uc["import_id"], "official_zone_import_id": za["import_id"]}
    first = client.post("/api/v1/import-batches", json=body, headers={"Idempotency-Key": "batch"})
    assert first.status_code == 202, first.text
    replay = client.post("/api/v1/import-batches", json=body, headers={"Idempotency-Key": "batch"})
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    assert client.post(f"/api/v1/imports/{uc['import_id']}/publish").json()["error_code"] == "IMPORT_BELONGS_TO_BATCH"
    second = client.post("/api/v1/import-batches", json=body, headers={"Idempotency-Key": "another"})
    assert second.status_code == 409
    service = client.app.state.submission_service
    publisher = LocalBronzePublisher(tmp_path / "bronze", client.app.state.storage)
    publisher.initialize()
    service.bronze_publisher = publisher
    service.airflow_client = FakeAirflowClient()
    service.uc_duplicate_checker = FakeUCDuplicateChecker()
    published = client.post(first.json()["links"]["publish"])
    assert published.status_code == 202, published.text
    assert published.json()["dag_id"] == "DAG_UC_ZA"
    assert published.json()["bronze_manifest_key"].startswith("uc_za_batches/batch_id=")
    assert (publisher.root / published.json()["bronze_manifest_key"]).is_file()


def test_batch_rejects_wrong_uc_link(client, valid_uc_geojson):
    uc = _submit(client, valid_uc_geojson, key="mismatch-uc",
                 metadata=uc_metadata(official_identifier="RIGHT")).json()
    za = _submit(client, valid_uc_geojson, key="mismatch-za", domain="za_oficial",
                 metadata=za_metadata(uc_identifier="WRONG")).json()
    response = client.post("/api/v1/import-batches",
                           json={"uc_import_id": uc["import_id"], "official_zone_import_id": za["import_id"]},
                           headers={"Idempotency-Key": "mismatch"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "BATCH_UC_IDENTITY_MISMATCH"


def test_batch_cannot_take_member_reserved_for_individual_publication(client, valid_uc_geojson):
    uc = _submit(client, valid_uc_geojson, key="reserved-uc",
                 metadata=uc_metadata(official_identifier="RESERVED")).json()
    za = _submit(client, valid_uc_geojson, key="reserved-za", domain="za_oficial",
                 metadata=za_metadata(uc_identifier="RESERVED")).json()
    client.app.state.submission_service.repository.reserve_publication(uc["import_id"])
    result = client.post("/api/v1/import-batches",
                         json={"uc_import_id": uc["import_id"], "official_zone_import_id": za["import_id"]},
                         headers={"Idempotency-Key": "reserved-batch"})
    assert result.status_code == 409
    assert result.json()["error_code"] == "IMPORT_PUBLICATION_STARTED"
