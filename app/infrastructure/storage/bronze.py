from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from app.core.errors import AppError
from app.domain.models import Submission, SubmissionDomain, SubmissionOperation
from app.infrastructure.storage.local import LocalObjectStorage


@dataclass(frozen=True)
class BronzePublication:
    manifest_key: str
    canonical_key: str
    original_key: str


class LocalBronzePublisher:
    """Publish immutable, manifest-directed batches into the pipeline Bronze root."""

    _DOMAIN_FOLDERS = {
        SubmissionDomain.UC: "ucs",
        SubmissionDomain.OFFICIAL_ZONE: "za",
    }

    def __init__(self, root: Path, source_storage: LocalObjectStorage) -> None:
        self.root = root.resolve()
        self.source_storage = source_storage

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, submission: Submission) -> BronzePublication:
        if not submission.manifest_key or not submission.canonical_key:
            raise AppError(
                409,
                "IMPORT_ARTIFACTS_INCOMPLETE",
                "Artefatos incompletos",
                "A importação aceita não possui manifesto e canônico para publicação.",
            )

        domain_folder = self._DOMAIN_FOLDERS[submission.domain]
        batch_key = f"{domain_folder}/import_id={submission.submission_id}"
        if submission.operation is SubmissionOperation.CREATE_WITH_ZONE:
            batch_key = f"uc_za_batches/batch_id={submission.submission_id}"
        target = self._resolve(batch_key)
        publication = BronzePublication(
            manifest_key=f"{batch_key}/manifest.json",
            canonical_key=f"{batch_key}/canonical/data.geojson",
            original_key=f"{batch_key}/original/{submission.server_filename}",
        )

        if target.exists():
            self._verify_existing(publication, submission)
            return publication

        staging = target.with_name(f".{target.name}.publishing-{uuid4()}")
        try:
            (staging / "canonical").mkdir(parents=True)
            (staging / "original").mkdir(parents=True)
            shutil.copy2(
                self.source_storage.resolve(submission.canonical_key),
                staging / "canonical" / "data.geojson",
            )
            shutil.copy2(
                self.source_storage.resolve(submission.original_key),
                staging / "original" / submission.server_filename,
            )
            source_manifest = json.loads(
                self.source_storage.resolve(submission.manifest_key).read_text(encoding="utf-8")
            )
            if submission.operation is SubmissionOperation.CREATE_WITH_ZONE:
                zone = dict(source_manifest["metadata"]["official_zone"])
                (staging / "zone").mkdir()
                shutil.copy2(self.source_storage.resolve(zone["canonical_key"]), staging / "zone/data.geojson")
                shutil.copy2(self.source_storage.resolve(zone["original_key"]), staging / "zone/original")
                zone["canonical_key"] = f"{batch_key}/zone/data.geojson"
                zone["original_key"] = f"{batch_key}/zone/original"
                source_manifest["metadata"]["official_zone"] = zone
                for branch, canonical_key, metadata in (
                    ("ucs", publication.canonical_key, source_manifest["metadata"]),
                    ("za", zone["canonical_key"], zone["metadata"]),
                ):
                    member_dir = staging / branch
                    member_dir.mkdir()
                    (member_dir / "manifest.json").write_text(
                        json.dumps({
                            "created_at": source_manifest["created_at"],
                            "domain": "uc" if branch == "ucs" else "za_oficial",
                            "import_id": submission.submission_id,
                            "metadata": metadata,
                            "bronze": {"canonical_key": canonical_key},
                        }, ensure_ascii=False), encoding="utf-8",
                    )
            source_manifest["bronze"] = {
                "batch_key": batch_key,
                "manifest_key": publication.manifest_key,
                "canonical_key": publication.canonical_key,
                "original_key": publication.original_key,
            }
            source_manifest["canonical"]["storage_key"] = publication.canonical_key
            source_manifest["original"]["storage_key"] = publication.original_key
            (staging / "manifest.json").write_text(
                json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
        except FileExistsError:
            self._verify_existing(publication, submission)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return publication

    def ready(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)

    def _verify_existing(self, publication: BronzePublication, submission: Submission) -> None:
        manifest_path = self._resolve(publication.manifest_key)
        if not manifest_path.is_file():
            raise AppError(
                409,
                "BRONZE_BATCH_CONFLICT",
                "Lote Bronze inconsistente",
                "Já existe um lote para o import_id, mas seu manifesto está ausente.",
            )
        payload: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        checksum = payload.get("original", {}).get("checksum_sha256")
        if payload.get("import_id") != submission.submission_id or checksum != submission.checksum_sha256:
            raise AppError(
                409,
                "BRONZE_BATCH_CONFLICT",
                "Lote Bronze conflitante",
                "O lote existente não corresponde ao import_id e checksum solicitados.",
            )

    def _resolve(self, key: str) -> Path:
        normalized = PurePosixPath(key)
        if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
            raise AppError(500, "INVALID_BRONZE_KEY", "Chave Bronze inválida", key)
        candidate = self.root.joinpath(*normalized.parts).resolve()
        if self.root not in candidate.parents:
            raise AppError(500, "INVALID_BRONZE_KEY", "Chave Bronze inválida", key)
        return candidate
