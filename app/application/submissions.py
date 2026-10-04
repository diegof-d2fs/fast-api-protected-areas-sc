from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from fastapi import UploadFile
from shapely.geometry import shape

from app.core.errors import AppError, bad_request
from app.domain.data_dictionary import UC_IDENTITY_FIELDS, UC_NAME_FIELDS
from app.domain.models import (
    DuplicatePolicy,
    Submission,
    SubmissionDomain,
    SubmissionOperation,
    SubmissionStatus,
    ValidationIssue,
    ValidationSeverity,
)
from app.domain.state import ensure_transition
from app.infrastructure.airflow.client import AirflowClient
from app.infrastructure.compute.ec2 import ProcessingNodeWaker
from app.infrastructure.geospatial.validator import GeospatialValidator
from app.infrastructure.persistence.sqlite import SqliteSubmissionRepository
from app.infrastructure.storage.bronze import LocalBronzePublisher
from app.infrastructure.storage.local import LocalObjectStorage
from app.infrastructure.storage.s3 import LocalPipelineResults, PipelineResults, S3BronzePublisher

_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class UCDuplicateChecker(Protocol):
    def find_create_duplicates(
        self,
        *,
        uc_ids: list[str],
        cnuc_codes: list[str],
        wdpa_pids: list[str],
        geometries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class SubmissionService:
    def __init__(
        self,
        repository: SqliteSubmissionRepository,
        storage: LocalObjectStorage,
        validator: GeospatialValidator,
        bronze_publisher: LocalBronzePublisher | S3BronzePublisher | None = None,
        airflow_client: AirflowClient | None = None,
        uc_duplicate_checker: UCDuplicateChecker | None = None,
        *,
        pipeline_results: PipelineResults | None = None,
        processing_waker: ProcessingNodeWaker | None = None,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.validator = validator
        self.bronze_publisher = bronze_publisher
        self.airflow_client = airflow_client
        self.uc_duplicate_checker = uc_duplicate_checker
        self.pipeline_results = pipeline_results
        self.processing_waker = processing_waker
        self.logger = logging.getLogger(__name__)

    async def create(
        self,
        *,
        upload: UploadFile,
        domain: SubmissionDomain,
        operation: SubmissionOperation,
        duplicate_policy: DuplicatePolicy,
        metadata: dict[str, Any],
        repair_geometry: bool,
        idempotency_key: str,
        correlation_id: str,
    ) -> Submission:
        self._validate_idempotency_key(idempotency_key)
        if duplicate_policy is DuplicatePolicy.SKIP_DUPLICATES and not (
            domain is SubmissionDomain.UC and operation is SubmissionOperation.CREATE
        ):
            raise bad_request(
                "DUPLICATE_POLICY_NOT_ALLOWED",
                "`skip_duplicates` é permitido somente em importações `uc/create`.",
            )
        submission_id = str(uuid4())
        original_filename = self._safe_original_filename(upload.filename)
        suffix = Path(original_filename).suffix.lower()
        server_filename = f"original{suffix or '.bin'}"
        quarantine_prefix = f"quarantine/{submission_id}"
        original_key = f"{quarantine_prefix}/original/{server_filename}"

        try:
            size_bytes, checksum = await self.storage.put_upload(original_key, upload)
            detected_format = self.validator.detect_format(
                self.storage.resolve(original_key), original_filename
            )
            fingerprint = self._fingerprint(
                checksum=checksum,
                domain=domain,
                operation=operation,
                duplicate_policy=duplicate_policy,
                metadata=metadata,
                repair_geometry=repair_geometry,
            )
            now = datetime.now(UTC)
            candidate = Submission(
                submission_id=submission_id,
                status=SubmissionStatus.RECEIVED,
                domain=domain,
                operation=operation,
                duplicate_policy=duplicate_policy,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                original_filename=original_filename,
                server_filename=server_filename,
                content_type=upload.content_type,
                size_bytes=size_bytes,
                checksum_sha256=checksum,
                metadata_payload=metadata,
                repair_geometry=repair_geometry,
                detected_format=detected_format,
                original_key=original_key,
                created_at=now,
                updated_at=now,
            )
            item, created = self.repository.create_or_get(candidate)
            if not created:
                self.storage.delete_prefix(quarantine_prefix)
                self.logger.info(
                    "idempotent submission replay",
                    extra={"import_id": item.submission_id, "correlation_id": item.correlation_id},
                )
                return item
        except AppError:
            self.storage.delete_prefix(quarantine_prefix)
            raise

        try:
            item = self._transition(item, SubmissionStatus.VALIDATING)
            outcome = self.validator.validate(
                self.storage.resolve(item.original_key),
                original_filename=item.original_filename,
                domain=item.domain,
                metadata=item.metadata_payload,
                repair_geometry=item.repair_geometry,
            )
            canonical_geojson = outcome.canonical_geojson
            batch_items: list[dict[str, Any]] = []
            if (
                item.domain is SubmissionDomain.UC
                and item.operation is SubmissionOperation.CREATE
                and outcome.report.valid
                and canonical_geojson is not None
                and outcome.report.feature_count > 1
            ):
                batch_issues, batch_items = self._validate_multi_uc_create(canonical_geojson)
                if batch_issues:
                    outcome.report = outcome.report.model_copy(
                        update={"valid": False, "errors": [*outcome.report.errors, *batch_issues]}
                    )
                    canonical_geojson = None
            single_feature_operation = (
                item.domain is SubmissionDomain.UC
                and item.operation in {
                    SubmissionOperation.UPDATE,
                    SubmissionOperation.REPLACE_POINT,
                }
            ) or (
                item.domain is SubmissionDomain.OFFICIAL_ZONE
                and item.operation is SubmissionOperation.REPLACE_BUFFER_ABRANGENCIA
            )
            if (
                single_feature_operation
                and outcome.report.valid
                and outcome.report.feature_count != 1
            ):
                outcome.report = outcome.report.model_copy(
                    update={
                        "valid": False,
                        "errors": [
                            *outcome.report.errors,
                            ValidationIssue(
                                code=(
                                    "UC_OPERATION_REQUIRES_SINGLE_FEATURE"
                                    if item.domain is SubmissionDomain.UC
                                    else "OPERATION_REQUIRES_SINGLE_FEATURE"
                                ),
                                message=(
                                    "Uma mutação cadastral dirigida deve conter exatamente uma feição."
                                ),
                                severity=ValidationSeverity.ERROR,
                                field="file",
                                suggestion=(
                                    "Envie uma importação separada para cada UC, com sua geometria "
                                    "completa."
                                ),
                            ),
                        ],
                    }
                )
                canonical_geojson = None
            if (
                item.domain is SubmissionDomain.UC
                and item.operation is SubmissionOperation.REPLACE_POINT
                and outcome.report.valid
                and not set(outcome.report.geometry_types).issubset({"Polygon", "MultiPolygon"})
            ):
                outcome.report = outcome.report.model_copy(
                    update={
                        "valid": False,
                        "errors": [
                            *outcome.report.errors,
                            ValidationIssue(
                                code="REPLACE_POINT_REQUIRES_POLYGON",
                                message=(
                                    "A substituição de uma UC pontual exige uma geometria "
                                    "Polygon ou MultiPolygon."
                                ),
                                severity=ValidationSeverity.ERROR,
                                field="file",
                                suggestion="Envie o limite poligonal oficial completo da UC.",
                            ),
                        ],
                    }
                )
                canonical_geojson = None
            if (
                item.domain is SubmissionDomain.UC
                and outcome.report.valid
                and canonical_geojson is not None
            ):
                inferred_name = self._resolve_uc_name(item.metadata_payload, canonical_geojson)
                if inferred_name:
                    if not item.metadata_payload.get("name"):
                        item = item.model_copy(
                            update={
                                "metadata_payload": {
                                    **item.metadata_payload,
                                    "name": inferred_name,
                                }
                            }
                        )
                else:
                    outcome.report = outcome.report.model_copy(
                        update={
                            "valid": False,
                            "errors": [
                                *outcome.report.errors,
                                ValidationIssue(
                                    code="MISSING_UC_NAME",
                                    message=(
                                        "O nome da UC não foi informado nos metadados nem "
                                        "encontrado nos atributos do arquivo."
                                    ),
                                    severity=ValidationSeverity.ERROR,
                                    field="name",
                                    suggestion=(
                                        "Informe metadata.name ou inclua um atributo nm_uc, "
                                        "nome_uc, nome ou name."
                                    ),
                                ),
                            ],
                        }
                    )
                    canonical_geojson = None
            item = item.model_copy(
                update={
                    "validation": outcome.report,
                    "detected_format": outcome.report.detected_format,
                    "batch_items": batch_items,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.repository.save(item)
            if outcome.report.valid and canonical_geojson is not None:
                item = self._accept(item, canonical_geojson)
            else:
                item = self._reject(item)
            self.logger.info(
                "submission validated",
                extra={
                    "import_id": item.submission_id,
                    "correlation_id": item.correlation_id,
                    "status": item.status.value,
                    "format": item.detected_format.value if item.detected_format else None,
                },
            )
            return item
        except Exception:
            try:
                item = self._transition(item, SubmissionStatus.FAILED)
            except Exception:
                self.logger.exception(
                    "failed to persist terminal submission state", extra={"import_id": item.submission_id}
                )
            raise

    def get(self, import_id: str) -> Submission:
        return self.repository.get(import_id)

    def get_with_pipeline_state(self, import_id: str) -> Submission:
        item = self.get(import_id)
        if (
            item.status is not SubmissionStatus.PROCESSING
            or self.airflow_client is None
            or not item.dag_id
            or not item.dag_run_id
        ):
            return item
        pipeline_result = self._read_pipeline_result(item)
        if pipeline_result and pipeline_result.get("error_code") == "UC_ALREADY_EXISTS":
            item = item.model_copy(
                update={
                    "error_code": "UC_ALREADY_EXISTS",
                    "error_detail": pipeline_result.get("error_detail"),
                    "duplicate_matches": pipeline_result.get("duplicate_matches") or [],
                    "updated_at": datetime.now(UTC),
                }
            )
            return self._transition(item, SubmissionStatus.DUPLICATE)
        if pipeline_result and pipeline_result.get("status") == "FAILED":
            item = item.model_copy(
                update={
                    "error_code": pipeline_result.get("error_code") or "PIPELINE_ERROR",
                    "error_detail": pipeline_result.get("error_detail"),
                    "updated_at": datetime.now(UTC),
                }
            )
            return self._transition(item, SubmissionStatus.FAILED)
        try:
            dag_run = self.airflow_client.get_dag_run(item.dag_id, item.dag_run_id)
        except AppError:
            self.logger.warning(
                "could not refresh Airflow state",
                extra={"import_id": item.submission_id, "dag_run_id": item.dag_run_id},
            )
            return item
        if dag_run.state == "success":
            return self._transition(item, SubmissionStatus.SUCCEEDED)
        if dag_run.state in {"failed", "upstream_failed"}:
            return self._transition(item, SubmissionStatus.FAILED)
        return item

    def _read_pipeline_result(self, item: Submission) -> dict[str, Any] | None:
        reader = self.pipeline_results
        if reader is None:
            if self.bronze_publisher is None:
                return None
            reader = LocalPipelineResults(self.bronze_publisher.root.parent / "quality" / "api_results")
        payload = reader.read(item.submission_id)
        if payload is None:
            return None
        if payload.get("import_id") != item.submission_id:
            self.logger.error("ignored invalid pipeline result", extra={"import_id": item.submission_id})
            return None
        return payload

    def dispatch_pending(self) -> None:
        """Trigger imports left `PUBLISHED` while Airflow was down and refresh running ones.

        Runs periodically. When there is pending work and Airflow does not answer, it asks for the
        processing node to start; the next round dispatches once Airflow is back.
        """
        if self.airflow_client is None:
            return
        published = self.repository.list_by_status([SubmissionStatus.PUBLISHED])
        for item in published:
            try:
                self._trigger(item)
            except AppError as exc:
                if exc.error_code != "AIRFLOW_UNAVAILABLE":
                    raise
                self._wake_processing_node()
                return
            self._transition(item, SubmissionStatus.PROCESSING)
        for item in self.repository.list_by_status([SubmissionStatus.PROCESSING]):
            self.get_with_pipeline_state(item.submission_id)

    def _trigger(self, item: Submission) -> None:
        if not item.dag_id or not item.dag_run_id or not item.bronze_manifest_key:
            raise AppError(
                500,
                "PUBLISHED_IMPORT_INCOMPLETE",
                "Publicação inconsistente",
                "A importação publicada não possui identificadores completos de orquestração.",
            )
        self.airflow_client.trigger(
            item.dag_id,
            item.dag_run_id,
            {
                "schema_version": "1.0",
                "import_id": item.submission_id,
                "correlation_id": item.correlation_id,
                "domain": item.domain.value,
                "operation": item.operation.value,
                "manifest_key": item.bronze_manifest_key,
                "checksum_sha256": item.checksum_sha256,
            },
        )

    def _wake_processing_node(self) -> None:
        if self.processing_waker is not None:
            self.processing_waker.wake()

    def publish(self, import_id: str) -> Submission:
        item = self.get(import_id)
        if self.repository.batch_for_member(import_id):
            raise AppError(409, "IMPORT_BELONGS_TO_BATCH", "Importação agrupada",
                           "Publique o lote atômico ao qual esta importação pertence.")
        if item.status in {SubmissionStatus.PROCESSING, SubmissionStatus.SUCCEEDED}:
            return item
        if item.status not in {SubmissionStatus.ACCEPTED, SubmissionStatus.PUBLISHED}:
            raise AppError(
                409,
                "IMPORT_NOT_ACCEPTED",
                "Importação não publicável",
                "A importação precisa estar no estado ACCEPTED antes da publicação na Bronze.",
            )
        if (
            item.domain is SubmissionDomain.OFFICIAL_ZONE
            and item.operation is SubmissionOperation.CREATE
        ):
            raise AppError(
                409,
                "ZA_CREATE_REQUIRES_BATCH",
                "Cadastro isolado de ZA não permitido",
                "Uma ZA de UC ainda inexistente deve ser publicada junto com a nova UC pelo "
                "lote atômico UC+ZA. Para uma UC existente com Buffer de Abrangência ativo, use `replace_buffer_abrangencia`.",
            )
        enabled_operation = (
            item.domain is SubmissionDomain.UC
            and item.operation in {
                SubmissionOperation.CREATE,
                SubmissionOperation.CREATE_WITH_ZONE,
                SubmissionOperation.UPDATE,
                SubmissionOperation.EXTINGUISH,
                SubmissionOperation.REPLACE_POINT,
            }
        ) or (
            item.domain is SubmissionDomain.OFFICIAL_ZONE
            and item.operation is SubmissionOperation.REPLACE_BUFFER_ABRANGENCIA
        )
        if not enabled_operation:
            raise AppError(
                409,
                "OPERATION_NOT_ENABLED",
                "Operação ainda não habilitada",
                "Esta versão publica `uc/create`, `uc/update`, `uc/extinguish`, `uc/replace_point` e "
                "`za_oficial/replace_buffer_abrangencia`. A atualização de uma ZA oficial já existente "
                "permanece fora deste incremento.",
            )
        if self.bronze_publisher is None or self.airflow_client is None:
            raise AppError(
                503,
                "PIPELINE_INTEGRATION_NOT_ENABLED",
                "Publicação indisponível",
                "Nenhum dado foi gravado no PostGIS. Configure a Bronze e o cliente Airflow.",
            )

        self.repository.reserve_publication(import_id)
        if item.status is SubmissionStatus.ACCEPTED:
            if (
                item.domain is SubmissionDomain.UC
                and item.operation in {SubmissionOperation.CREATE, SubmissionOperation.CREATE_WITH_ZONE}
            ):
                duplicate_features = self._find_uc_duplicate_features(item)
                duplicates = [
                    match
                    for result in duplicate_features
                    for match in result["matches"]
                ]
                if duplicate_features and item.duplicate_policy is DuplicatePolicy.SKIP_DUPLICATES:
                    item = self._prepare_partial_create_batch(item, duplicate_features)
                    if any(entry.get("status") == "ACCEPTED" for entry in item.batch_items):
                        duplicates = []
                if duplicates:
                    detail = (
                        "A UC não foi publicada porque já existe cadastro correspondente no PostGIS."
                    )
                    item = item.model_copy(
                        update={
                            "error_code": "UC_ALREADY_EXISTS",
                            "error_detail": detail,
                            "duplicate_matches": duplicates,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    self._transition(item, SubmissionStatus.DUPLICATE)
                    raise AppError(
                        409,
                        "UC_ALREADY_EXISTS",
                        "Unidade de Conservação já cadastrada",
                        detail,
                        violations=[
                            {
                                "field": "file",
                                "rule": "unique_uc",
                                "message": (
                                    f"Correspondência por {match['matched_by']} com a UC "
                                    f"{match['uc_id']} ({match['name']})."
                                ),
                            }
                            for match in duplicates
                        ],
                    )
            publication = self.bronze_publisher.publish(item)
            dag_id = "DAG_UCS" if item.domain is SubmissionDomain.UC else "DAG_ZA_BUFFER"
            if item.operation is SubmissionOperation.CREATE_WITH_ZONE:
                dag_id = "DAG_UC_ZA"
            dag_run_id = f"api__{item.submission_id}"
            item = item.model_copy(
                update={
                    "bronze_manifest_key": publication.manifest_key,
                    "dag_id": dag_id,
                    "dag_run_id": dag_run_id,
                    "updated_at": datetime.now(UTC),
                }
            )
            item = self._transition(item, SubmissionStatus.PUBLISHED)

        try:
            self._trigger(item)
        except AppError as exc:
            if exc.error_code != "AIRFLOW_UNAVAILABLE":
                raise
            # The batch is already durable in the Bronze; it stays PUBLISHED and the dispatcher
            # triggers it once the processing node is up.
            self.logger.info("airflow unavailable; import queued", extra={"import_id": item.submission_id})
            self._wake_processing_node()
            return item
        return self._transition(item, SubmissionStatus.PROCESSING)

    def _find_uc_duplicate_features(self, item: Submission) -> list[dict[str, Any]]:
        """Return existing-UC matches grouped by the candidate feature that produced them."""
        if self.uc_duplicate_checker is None:
            raise AppError(
                503,
                "DUPLICATE_CHECK_UNAVAILABLE",
                "Verificação de duplicidade indisponível",
                "A publicação não prosseguiu porque o PostGIS não está configurado para a "
                "verificação preventiva de UCs repetidas.",
            )
        if not item.canonical_key:
            raise AppError(
                409,
                "IMPORT_ARTIFACTS_INCOMPLETE",
                "Artefatos incompletos",
                "A importação não possui GeoJSON canônico para verificar duplicidade.",
            )
        canonical = json.loads(self.storage.resolve(item.canonical_key).read_text(encoding="utf-8"))
        features = canonical.get("features", [])
        results: list[dict[str, Any]] = []
        for index, feature in enumerate(features):
            uc_ids: set[str] = set()
            cnuc_codes: set[str] = set()
            wdpa_pids: set[str] = set()
            properties = {
                str(key).casefold(): value for key, value in (feature.get("properties") or {}).items()
            }
            for key in UC_IDENTITY_FIELDS["official_identifier"]:
                self._add_identity(uc_ids, properties.get(key))
            for key in UC_IDENTITY_FIELDS["cd_cnuc"]:
                self._add_identity(cnuc_codes, properties.get(key))
            for key in UC_IDENTITY_FIELDS["wdpa_pid"]:
                self._add_identity(wdpa_pids, properties.get(key))
            geometry = feature.get("geometry")
            if len(features) == 1:
                self._add_identity(uc_ids, item.metadata_payload.get("official_identifier"))
                self._add_identity(cnuc_codes, item.metadata_payload.get("cd_cnuc"))
                self._add_identity(wdpa_pids, item.metadata_payload.get("wdpa_pid"))
            try:
                matches = self.uc_duplicate_checker.find_create_duplicates(
                    uc_ids=sorted(uc_ids),
                    cnuc_codes=sorted(cnuc_codes),
                    wdpa_pids=sorted(wdpa_pids),
                    geometries=[geometry] if isinstance(geometry, dict) else [],
                )
                if matches:
                    results.append(
                        {
                            "feature_index": index,
                            "matches": [dict(match, feature_index=index) for match in matches],
                        }
                    )
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    503,
                    "DUPLICATE_CHECK_UNAVAILABLE",
                    "Verificação de duplicidade indisponível",
                    "Não foi possível consultar o PostGIS; nenhuma publicação foi realizada.",
                ) from exc
        return results

    def _prepare_partial_create_batch(
        self,
        item: Submission,
        duplicate_features: list[dict[str, Any]],
    ) -> Submission:
        """Publish a derived canonical batch while retaining per-feature audit results."""
        if not item.canonical_key or not item.manifest_key:
            raise AppError(
                409,
                "IMPORT_ARTIFACTS_INCOMPLETE",
                "Artefatos incompletos",
                "O lote aceito não possui artefatos canônicos completos.",
            )
        canonical = json.loads(self.storage.resolve(item.canonical_key).read_text(encoding="utf-8"))
        skipped_by_index = {entry["feature_index"]: entry["matches"] for entry in duplicate_features}
        canonical["features"] = [
            feature
            for index, feature in enumerate(canonical.get("features", []))
            if index not in skipped_by_index
        ]
        batch_items = []
        for batch_item in item.batch_items:
            index = batch_item["feature_index"]
            batch_items.append(
                {
                    **batch_item,
                    "status": "SKIPPED_DUPLICATE" if index in skipped_by_index else "ACCEPTED",
                    "duplicate_matches": skipped_by_index.get(index, []),
                }
            )
        updated = item.model_copy(
            update={
                "batch_items": batch_items,
                "duplicate_matches": [
                    match
                    for matches in skipped_by_index.values()
                    for match in matches
                ],
                "updated_at": datetime.now(UTC),
            }
        )
        self.storage.write_json(updated.canonical_key, canonical)
        self.storage.write_json(updated.manifest_key, self._manifest(updated))
        return self.repository.save(updated)

    @staticmethod
    def _add_identity(target: set[str], value: Any) -> None:
        if value is None:
            return
        normalized = str(value).strip()
        if normalized:
            target.add(normalized)

    @staticmethod
    def _resolve_uc_name(
        metadata: dict[str, Any], canonical_geojson: dict[str, Any]
    ) -> str | None:
        explicit = metadata.get("name")
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        for feature in canonical_geojson.get("features", []):
            properties = {
                str(key).casefold(): value
                for key, value in (feature.get("properties") or {}).items()
            }
            for key in UC_NAME_FIELDS:
                value = properties.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        return None

    @staticmethod
    def _validate_multi_uc_create(
        canonical_geojson: dict[str, Any],
    ) -> tuple[list[ValidationIssue], list[dict[str, Any]]]:
        """Require independent names and strong identities in an atomic UC batch."""
        identity_fields = UC_IDENTITY_FIELDS
        name_fields = UC_NAME_FIELDS
        seen = {field: set() for field in identity_fields}
        seen_geometries: set[str] = set()
        errors: list[ValidationIssue] = []
        items: list[dict[str, Any]] = []
        for index, feature in enumerate(canonical_geojson.get("features", [])):
            properties = {
                str(key).casefold(): value for key, value in (feature.get("properties") or {}).items()
            }
            identities: dict[str, str] = {}
            for field, candidates in identity_fields.items():
                value = next(
                    (properties[key] for key in candidates if properties.get(key) is not None), None
                )
                if value is not None and str(value).strip():
                    normalized = str(value).strip()
                    identities[field] = normalized
                    if normalized.casefold() in seen[field]:
                        errors.append(
                            ValidationIssue(
                                code="DUPLICATE_UC_IDENTITY_IN_BATCH",
                                message=f"A identidade {field} esta repetida no arquivo.",
                                severity=ValidationSeverity.ERROR,
                                feature=index,
                                field=field,
                            )
                        )
                    seen[field].add(normalized.casefold())
            if not identities:
                errors.append(
                    ValidationIssue(
                        code="MISSING_UC_IDENTITY",
                        message="Cada UC do lote deve possuir uc_id, cd_cnuc ou wdpa_pid.",
                        severity=ValidationSeverity.ERROR,
                        feature=index,
                        field="file",
                    )
                )
            name = next(
                (
                    str(properties[key]).strip()
                    for key in name_fields
                    if properties.get(key) is not None and str(properties[key]).strip()
                ),
                None,
            )
            if not name:
                errors.append(
                    ValidationIssue(
                        code="MISSING_UC_NAME",
                        message="Cada UC do lote deve possuir nome nos atributos do arquivo.",
                        severity=ValidationSeverity.ERROR,
                        feature=index,
                        field="name",
                    )
                )
            geometry = feature.get("geometry")
            if geometry is not None:
                geometry_key = shape(geometry).normalize().wkb_hex
                if geometry_key in seen_geometries:
                    errors.append(
                        ValidationIssue(
                            code="DUPLICATE_UC_GEOMETRY_IN_BATCH",
                            message="A geometria e equivalente a outra UC do mesmo arquivo.",
                            severity=ValidationSeverity.ERROR,
                            feature=index,
                            field="geometry",
                        )
                    )
                seen_geometries.add(geometry_key)
            items.append({"feature_index": index, "name": name, "identities": identities})
        return errors, items

    def _accept(self, item: Submission, canonical_geojson: dict[str, Any]) -> Submission:
        date_partition = item.created_at.date().isoformat()
        prefix = (
            f"accepted/{item.domain.value}/ingestion_date={date_partition}/import_id={item.submission_id}"
        )
        accepted_original_key = f"{prefix}/original/{item.server_filename}"
        canonical_key = f"{prefix}/canonical/data.geojson"
        manifest_key = f"{prefix}/manifest.json"
        self.storage.move(item.original_key, accepted_original_key)
        self.storage.write_json(canonical_key, canonical_geojson)
        item = item.model_copy(
            update={
                "original_key": accepted_original_key,
                "canonical_key": canonical_key,
                "manifest_key": manifest_key,
                "updated_at": datetime.now(UTC),
            }
        )
        item = self._transition(item, SubmissionStatus.ACCEPTED, save=False)
        self.storage.write_json(manifest_key, self._manifest(item))
        return self.repository.save(item)

    def _reject(self, item: Submission) -> Submission:
        manifest_key = f"rejected/{item.submission_id}/manifest.json"
        item = item.model_copy(update={"manifest_key": manifest_key, "updated_at": datetime.now(UTC)})
        item = self._transition(item, SubmissionStatus.REJECTED, save=False)
        self.storage.write_json(manifest_key, self._manifest(item))
        return self.repository.save(item)

    def _transition(self, item: Submission, target: SubmissionStatus, *, save: bool = True) -> Submission:
        ensure_transition(item.status, target)
        updated = item.model_copy(update={"status": target, "updated_at": datetime.now(UTC)})
        return self.repository.save(updated) if save else updated

    @staticmethod
    def _manifest(item: Submission) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "import_id": item.submission_id,
            "correlation_id": item.correlation_id,
            "idempotency_key": item.idempotency_key,
            "actor": item.metadata_payload.get("actor") or "api-client",
            "status": item.status.value,
            "domain": item.domain.value,
            "operation": item.operation.value,
            "duplicate_policy": item.duplicate_policy.value,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "original": {
                "client_filename": item.original_filename,
                "server_filename": item.server_filename,
                "storage_key": item.original_key,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
                "checksum_sha256": item.checksum_sha256,
            },
            "canonical": {"storage_key": item.canonical_key, "crs": "EPSG:4674"},
            "metadata": item.metadata_payload,
            "batch_items": item.batch_items,
            "repair_geometry": item.repair_geometry,
            "validation": item.validation.model_dump(mode="json") if item.validation else None,
        }

    @staticmethod
    def _safe_original_filename(filename: str | None) -> str:
        if not filename:
            raise bad_request("MISSING_FILENAME", "O arquivo enviado deve possuir um nome.")
        normalized = filename.replace("\\", "/").split("/")[-1].strip()
        if not normalized or len(normalized) > 255 or any(ord(character) < 32 for character in normalized):
            raise bad_request("INVALID_FILENAME", "O nome original do arquivo é inválido.")
        return normalized

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not _IDEMPOTENCY_PATTERN.fullmatch(value):
            raise bad_request(
                "INVALID_IDEMPOTENCY_KEY",
                "A Idempotency-Key deve ter de 1 a 200 caracteres alfanuméricos e pode conter . _ : -.",
            )

    @staticmethod
    def _fingerprint(
        *,
        checksum: str,
        domain: SubmissionDomain,
        operation: SubmissionOperation,
        metadata: dict[str, Any],
        repair_geometry: bool,
        duplicate_policy: DuplicatePolicy,
    ) -> str:
        payload = json.dumps(
            {
                "checksum": checksum,
                "domain": domain.value,
                "operation": operation.value,
                "metadata": metadata,
                "repair_geometry": repair_geometry,
                "duplicate_policy": duplicate_policy.value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
