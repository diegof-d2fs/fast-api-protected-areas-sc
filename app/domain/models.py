from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubmissionDomain(StrEnum):
    UC = "uc"
    OFFICIAL_ZONE = "za_oficial"


class SubmissionOperation(StrEnum):
    CREATE = "create"
    CREATE_WITH_ZONE = "create_with_zone"
    UPDATE = "update"
    EXTINGUISH = "extinguish"
    REPLACE_POINT = "replace_point"
    REPLACE_BUFFER_ABRANGENCIA = "replace_buffer_abrangencia"


class DuplicatePolicy(StrEnum):
    REJECT_BATCH = "reject_batch"
    SKIP_DUPLICATES = "skip_duplicates"


class SubmissionStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    PUBLISHED = "PUBLISHED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class GeospatialFormat(StrEnum):
    SHAPEFILE_ZIP = "shapefile_zip"
    GEOJSON = "geojson"
    KML = "kml"


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    code: str = Field(description="Código estável da regra de validação.", examples=["MISSING_CRS"])
    message: str = Field(description="Descrição legível do resultado.")
    severity: ValidationSeverity = Field(description="Severidade do item.")
    feature: int | None = Field(default=None, description="Índice zero-based da feição relacionada.")
    field: str | None = Field(default=None, description="Campo relacionado, quando aplicável.")
    suggestion: str | None = Field(default=None, description="Orientação para correção.")


class ValidationReport(BaseModel):
    valid: bool = Field(description="Indica se a importação pode seguir para publicação.")
    detected_format: GeospatialFormat | None = Field(
        default=None, description="Formato detectado pelo conteúdo."
    )
    source_crs: str | None = Field(
        default=None, description="CRS declarado na origem.", examples=["EPSG:4674"]
    )
    target_crs: str = Field(default="EPSG:4674", description="CRS do artefato canônico.")
    geometry_types: list[str] = Field(default_factory=list, description="Tipos após normalização.")
    feature_count: int = Field(default=0, description="Quantidade de feições lidas.")
    vertex_count: int = Field(default=0, description="Quantidade de coordenadas após normalização.")
    bbox: list[float] | None = Field(
        default=None,
        description="Envelope `[min_x, min_y, max_x, max_y]` no CRS de destino.",
    )
    errors: list[ValidationIssue] = Field(default_factory=list, description="Regras que impediram o aceite.")
    warnings: list[ValidationIssue] = Field(
        default_factory=list, description="Alertas que não impediram o aceite."
    )
    repair_applied: bool = Field(default=False, description="Indica se uma correção explícita foi aplicada.")


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: SubmissionStatus
    domain: SubmissionDomain
    operation: SubmissionOperation
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.REJECT_BATCH
    correlation_id: str
    idempotency_key: str
    request_fingerprint: str
    original_filename: str
    server_filename: str
    content_type: str | None = None
    size_bytes: int
    checksum_sha256: str
    metadata_payload: dict[str, Any]
    repair_geometry: bool = False
    detected_format: GeospatialFormat | None = None
    original_key: str
    canonical_key: str | None = None
    manifest_key: str | None = None
    bronze_manifest_key: str | None = None
    dag_id: str | None = None
    dag_run_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    duplicate_matches: list[dict[str, Any]] = Field(default_factory=list)
    batch_items: list[dict[str, Any]] = Field(default_factory=list)
    validation: ValidationReport | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SubmissionView(BaseModel):
    import_id: str = Field(description="Identificador estável da importação.")
    status: SubmissionStatus = Field(description="Estado atual do processamento.")
    domain: SubmissionDomain = Field(description="Domínio geoespacial recebido.")
    operation: SubmissionOperation = Field(description="Operação de negócio solicitada.")
    duplicate_policy: DuplicatePolicy = Field(
        description="Tratamento de UCs já cadastradas em lotes de criação."
    )
    correlation_id: str = Field(description="Identificador usado para correlacionar logs e etapas futuras.")
    original_filename: str = Field(
        description="Nome do arquivo informado pelo cliente, usado apenas como metadado."
    )
    size_bytes: int = Field(description="Tamanho do arquivo original.")
    checksum_sha256: str = Field(description="SHA-256 calculado durante o streaming.")
    metadata: dict[str, Any] = Field(description="Metadados cadastrais validados do domínio.")
    repair_geometry: bool = Field(description="Indica se a tentativa de reparo foi autorizada.")
    detected_format: GeospatialFormat | None = Field(description="Formato reconhecido pelo conteúdo.")
    original_key: str = Field(description="Chave lógica do original preservado.")
    canonical_key: str | None = Field(description="Chave do GeoJSON canônico, quando aceito.")
    manifest_key: str | None = Field(description="Chave do manifesto versionado.")
    bronze_manifest_key: str | None = Field(
        default=None, description="Chave do manifesto publicado na Bronze."
    )
    dag_id: str | None = Field(default=None, description="DAG responsável pela mutação de domínio.")
    dag_run_id: str | None = Field(default=None, description="Execução idempotente solicitada ao Airflow.")
    error_code: str | None = Field(default=None, description="Código do erro terminal de negócio.")
    error_detail: str | None = Field(default=None, description="Explicação segura para o operador.")
    duplicate_matches: list[dict[str, Any]] = Field(
        default_factory=list, description="Correspondências existentes encontradas por feição."
    )
    batch_items: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(description="Criação em UTC.")
    updated_at: datetime = Field(description="Última atualização em UTC.")
    links: dict[str, str] = Field(description="Links relativos para acompanhamento.")

    @classmethod
    def from_submission(cls, item: Submission) -> SubmissionView:
        base = f"/api/v1/imports/{item.submission_id}"
        return cls(
            import_id=item.submission_id,
            status=item.status,
            domain=item.domain,
            operation=item.operation,
            duplicate_policy=item.duplicate_policy,
            correlation_id=item.correlation_id,
            original_filename=item.original_filename,
            size_bytes=item.size_bytes,
            checksum_sha256=item.checksum_sha256,
            metadata=item.metadata_payload,
            repair_geometry=item.repair_geometry,
            detected_format=item.detected_format,
            original_key=item.original_key,
            canonical_key=item.canonical_key,
            manifest_key=item.manifest_key,
            bronze_manifest_key=item.bronze_manifest_key,
            dag_id=item.dag_id,
            dag_run_id=item.dag_run_id,
            error_code=item.error_code,
            error_detail=item.error_detail,
            duplicate_matches=item.duplicate_matches,
            batch_items=item.batch_items,
            created_at=item.created_at,
            updated_at=item.updated_at,
            links={"self": base, "validation": f"{base}/validation"},
        )


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    error_code: str
    correlation_id: str
    violations: list[dict[str, Any]] = Field(default_factory=list)
