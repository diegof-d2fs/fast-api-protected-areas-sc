from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import AppError
from app.domain.models import Submission
from app.infrastructure.storage.bronze import BronzePublication, LocalBronzePublisher

MANIFEST_NAME = "manifest.json"


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...

    def get_object(self, **kwargs: Any) -> Any: ...

    def head_bucket(self, **kwargs: Any) -> Any: ...


class S3BronzePublisher:
    """Publish each batch locally (atomic, idempotent) and mirror it to the Bronze bucket.

    The pipeline only treats a batch as existing once its top-level manifest is present, so the
    manifest is always uploaded last: an interrupted mirror never exposes a partial batch, and a
    retry re-uploads the same immutable files.
    """

    def __init__(self, local: LocalBronzePublisher, client: S3Client, bucket: str) -> None:
        self.local = local
        self.client = client
        self.bucket = bucket

    @property
    def root(self) -> Path:
        return self.local.root

    def initialize(self) -> None:
        self.local.initialize()

    def publish(self, submission: Submission) -> BronzePublication:
        publication = self.local.publish(submission)
        batch_key = publication.manifest_key.removesuffix(f"/{MANIFEST_NAME}")
        remote_manifest = self._read_json(publication.manifest_key)
        if remote_manifest is not None:
            self._verify_remote(remote_manifest, submission)
            return publication

        batch_dir = self.local.root.joinpath(*batch_key.split("/"))
        files = sorted(path for path in batch_dir.rglob("*") if path.is_file())
        top_manifest = batch_dir / MANIFEST_NAME
        for path in [path for path in files if path != top_manifest] + [top_manifest]:
            key = f"{batch_key}/{path.relative_to(batch_dir).as_posix()}"
            with path.open("rb") as body:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        return publication

    def ready(self) -> bool:
        if not self.local.ready():
            return False
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            return False
        return True

    def _read_json(self, key: str) -> dict[str, Any] | None:
        return read_s3_json(self.client, self.bucket, key)

    @staticmethod
    def _verify_remote(payload: dict[str, Any], submission: Submission) -> None:
        checksum = payload.get("original", {}).get("checksum_sha256")
        if payload.get("import_id") != submission.submission_id or checksum != submission.checksum_sha256:
            raise AppError(
                409,
                "BRONZE_BATCH_CONFLICT",
                "Lote Bronze conflitante",
                "O lote existente no bucket não corresponde ao import_id e checksum solicitados.",
            )


class PipelineResults(Protocol):
    def read(self, import_id: str) -> dict[str, Any] | None: ...


class LocalPipelineResults:
    """Results written by the pipeline under `quality/api_results/import_id=<id>/result.json`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.logger = logging.getLogger(__name__)

    def read(self, import_id: str) -> dict[str, Any] | None:
        path = self.root / f"import_id={import_id}" / "result.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            self.logger.exception("could not read pipeline result", extra={"path": str(path)})
            return None


class S3PipelineResults:
    """Same contract as `LocalPipelineResults`, read from the lake bucket."""

    def __init__(self, client: S3Client, bucket: str, prefix: str = "quality/api_results") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def read(self, import_id: str) -> dict[str, Any] | None:
        return read_s3_json(self.client, self.bucket, f"{self.prefix}/import_id={import_id}/result.json")


def read_s3_json(client: S3Client, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _is_missing(exc):
            return None
        raise
    try:
        payload = json.loads(response["Body"].read())
    except (ValueError, KeyError):
        logging.getLogger(__name__).exception("invalid JSON object", extra={"key": key})
        return None
    return payload if isinstance(payload, dict) else None


def _is_missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code in {"NoSuchKey", "404", "NotFound"}
