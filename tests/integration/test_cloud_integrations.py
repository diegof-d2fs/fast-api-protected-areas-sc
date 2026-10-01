"""Bronze no S3, publicação com o processamento desligado e despacho posterior.

Na AWS a API e o Airflow rodam em máquinas separadas e o nó de processamento só fica ligado
quando há trabalho. Estes testes fixam o contrato: o lote vai ao bucket com o manifesto por
último, a importação fica `PUBLISHED` (sem erro) se o Airflow não responder, o nó é acordado e o
despachante dispara a DAG quando o Airflow volta.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.infrastructure.airflow.client import AirflowDagRun
from app.infrastructure.compute.ec2 import ProcessingNodeWaker
from app.infrastructure.storage.bronze import LocalBronzePublisher
from app.infrastructure.storage.s3 import S3BronzePublisher, S3PipelineResults
from tests.integration.test_submissions_api import FakeUCDuplicateChecker, _submit


class MissingKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_order: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body) -> None:
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else Body
        self.put_order.append(Key)

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise MissingKey(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_bucket(self, *, Bucket: str) -> None:
        return None


class SwitchableAirflow:
    def __init__(self) -> None:
        self.online = False
        self.calls: list[str] = []
        self.state = "running"

    def trigger(self, dag_id: str, dag_run_id: str, conf: dict) -> None:
        if not self.online:
            raise AppError(503, "AIRFLOW_UNAVAILABLE", "Airflow indisponível", "desligado")
        self.calls.append(dag_run_id)

    def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun:
        if not self.online:
            raise AppError(503, "AIRFLOW_UNAVAILABLE", "Airflow indisponível", "desligado")
        return AirflowDagRun(dag_id=dag_id, dag_run_id=dag_run_id, state=self.state)


class FakeEC2:
    def __init__(self, state: str = "stopped") -> None:
        self.state = state
        self.started: list[str] = []

    def describe_instances(self, *, InstanceIds: list[str]) -> dict:
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}

    def start_instances(self, *, InstanceIds: list[str]) -> None:
        self.started.extend(InstanceIds)
        self.state = "pending"


@pytest.fixture
def cloud(client: TestClient, tmp_path: Path):
    s3 = FakeS3()
    ec2 = FakeEC2()
    airflow = SwitchableAirflow()
    local = LocalBronzePublisher(tmp_path / "mirror" / "bronze", client.app.state.storage)
    publisher = S3BronzePublisher(local, s3, "bronze-bucket")
    publisher.initialize()
    service = client.app.state.submission_service
    service.bronze_publisher = publisher
    service.airflow_client = airflow
    service.uc_duplicate_checker = FakeUCDuplicateChecker()
    service.pipeline_results = S3PipelineResults(s3, "lake-bucket")
    service.processing_waker = ProcessingNodeWaker(ec2, "i-processamento")
    return service, s3, ec2, airflow


def test_publish_mirrors_batch_to_s3_with_manifest_last(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    _, s3, _, airflow = cloud
    airflow.online = True
    created = _submit(client, valid_uc_geojson, key="s3-mirror").json()

    published = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert published.status_code == 202
    manifest_key = published.json()["bronze_manifest_key"]
    assert s3.put_order[-1] == manifest_key
    assert {key for key in s3.put_order} >= {
        manifest_key.replace("manifest.json", "canonical/data.geojson"),
        manifest_key,
    }
    manifest = json.loads(s3.objects[("bronze-bucket", manifest_key)])
    assert manifest["import_id"] == created["import_id"]


def test_existing_remote_batch_is_not_uploaded_again(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    service, s3, _, airflow = cloud
    airflow.online = True
    created = _submit(client, valid_uc_geojson, key="s3-idempotent").json()
    client.post(f"/api/v1/imports/{created['import_id']}/publish")
    uploads = len(s3.put_order)

    service.bronze_publisher.publish(service.get(created["import_id"]))

    assert len(s3.put_order) == uploads


def test_conflicting_remote_manifest_is_rejected(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    service, s3, _, airflow = cloud
    airflow.online = True
    created = _submit(client, valid_uc_geojson, key="s3-conflict").json()
    batch_key = f"ucs/import_id={created['import_id']}/manifest.json"
    s3.objects[("bronze-bucket", batch_key)] = json.dumps(
        {"import_id": created["import_id"], "original": {"checksum_sha256": "outro"}}
    ).encode()

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 409
    assert response.json()["error_code"] == "BRONZE_BATCH_CONFLICT"


def test_publish_with_processing_down_queues_import_and_wakes_node(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    _, _, ec2, airflow = cloud
    created = _submit(client, valid_uc_geojson, key="queued").json()

    response = client.post(f"/api/v1/imports/{created['import_id']}/publish")

    assert response.status_code == 202
    assert response.json()["status"] == "PUBLISHED"
    assert ec2.started == ["i-processamento"]
    assert airflow.calls == []


def test_dispatcher_triggers_queued_import_and_refreshes_it_when_airflow_returns(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    service, _, _, airflow = cloud
    created = _submit(client, valid_uc_geojson, key="dispatch").json()
    client.post(f"/api/v1/imports/{created['import_id']}/publish")

    service.dispatch_pending()
    assert service.get(created["import_id"]).status.value == "PUBLISHED"

    airflow.online = True
    service.dispatch_pending()
    assert airflow.calls == [f"api__{created['import_id']}"]
    assert service.get(created["import_id"]).status.value == "PROCESSING"

    airflow.state = "success"
    service.dispatch_pending()
    assert service.get(created["import_id"]).status.value == "SUCCEEDED"


def test_dispatcher_does_not_wake_node_without_pending_work(client: TestClient, cloud) -> None:
    service, _, ec2, _ = cloud

    service.dispatch_pending()

    assert ec2.started == []


def test_waker_only_starts_a_stopped_node() -> None:
    running = FakeEC2(state="running")

    assert ProcessingNodeWaker(running, "i-1").wake() is False
    assert running.started == []


def test_pipeline_failure_is_read_from_lake_bucket(
    client: TestClient, cloud, valid_uc_geojson: bytes
) -> None:
    service, s3, _, airflow = cloud
    airflow.online = True
    created = _submit(client, valid_uc_geojson, key="s3-result").json()
    client.post(f"/api/v1/imports/{created['import_id']}/publish")
    s3.objects[("lake-bucket", f"quality/api_results/import_id={created['import_id']}/result.json")] = (
        json.dumps(
            {
                "import_id": created["import_id"],
                "status": "FAILED",
                "error_code": "GEOMETRY_REJECTED",
                "error_detail": "geometria inválida no pipeline",
            }
        ).encode()
    )

    current = client.get(f"/api/v1/imports/{created['import_id']}").json()

    assert current["status"] == "FAILED"
    assert current["error_code"] == "GEOMETRY_REJECTED"


@pytest.mark.parametrize(
    ("header_value", "expected"),
    [("198.51.100.10:46532", "198.51.100.10"), ("2001:db8::1:46532", "2001:db8::1"), ("lixo", None)],
)
def test_login_records_viewer_ip_from_trusted_header(
    client: TestClient, header_value: str, expected: str | None
) -> None:
    client.app.state.settings.client_ip_header = "CloudFront-Viewer-Address"
    repository = client.app.state.auth_service.repository
    before = len(repository.login_ips)

    client.post(
        "/api/v1/auth/login",
        json={"username": "inexistente", "password": "x" * 12},
        headers={"CloudFront-Viewer-Address": header_value},
    )

    assert repository.login_ips[before] == expected


def test_swagger_and_openapi_live_under_api_prefix(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 404
