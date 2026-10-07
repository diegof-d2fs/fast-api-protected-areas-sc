from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PA_SC_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Protected Areas SC API"
    app_version: str = "0.5.0"
    environment: str = "development"
    log_level: str = "INFO"
    data_root: Path = Path("data")

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    upload_chunk_bytes: int = Field(default=1024 * 1024, ge=64 * 1024)
    max_zip_entries: int = Field(default=64, ge=1)
    max_zip_uncompressed_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    max_zip_compression_ratio: float = Field(default=100.0, gt=1)
    max_features: int = Field(default=10_000, ge=1)
    max_vertices: int = Field(default=1_000_000, ge=1)
    max_repair_area_delta_ratio: float = Field(default=0.01, ge=0, le=1)

    canonical_epsg: int = 4674
    metric_epsg: int = 31982
    sc_bbox_min_x: float = -54.5
    sc_bbox_min_y: float = -29.8
    sc_bbox_max_x: float = -47.8
    sc_bbox_max_y: float = -25.5
    territorial_rule: str = "intersects_bbox"

    readiness_requires_postgis: bool = False
    readiness_requires_airflow: bool = False
    database_dsn: str | None = None

    admin_bootstrap_username: str | None = None
    admin_bootstrap_password: str | None = None
    session_ttl_hours: float = Field(default=12.0, gt=0)
    login_lockout_threshold: int = Field(default=5, ge=1)
    login_lockout_window_minutes: float = Field(default=15.0, gt=0)
    # Um navegador real aceita `Secure` em https:// e em http://localhost (exceção de contexto
    # seguro), mas Postman/curl/httpx não replicam essa exceção. Manter True em produção; só
    # desligar explicitamente para testar o login manualmente sem TLS (Postman/Swagger locais).
    session_cookie_secure: bool = True
    reporting_geoserver_db_password: str | None = None
    reporting_powerbi_db_password: str | None = None
    reporting_lab_db_password: str | None = None
    bronze_root: Path | None = None
    # "local": Bronze e resultados do pipeline em disco compartilhado (desenvolvimento).
    # "s3": Bronze espelhada em `s3_bronze_bucket` e resultados lidos de `s3_lake_bucket`;
    # `bronze_root` passa a ser só o espelho local do nó de serviço.
    storage_backend: Literal["local", "s3"] = "local"
    s3_bronze_bucket: str | None = None
    s3_lake_bucket: str | None = None
    aws_region: str = "us-east-1"
    # Nó de processamento sob demanda: a API o liga quando há importação pendente.
    processing_instance_id: str | None = None
    dispatch_interval_seconds: float = Field(default=60.0, ge=0)
    # Cabeçalho com o IP real do cliente atrás de um proxy confiável (ex.: CloudFront-Viewer-Address).
    # Só deve ser configurado quando a API não é alcançável diretamente pela internet.
    client_ip_header: str | None = None
    # Modo eventos: o nó de serviço é ampliado por automação SSM disparada pelo painel. Sem
    # `serving_instance_id` e os dois documentos, as rotas existem mas respondem 503.
    serving_instance_id: str | None = None
    event_mode_activate_document: str | None = None
    event_mode_deactivate_document: str | None = None
    event_mode_scheduler_group: str | None = None
    event_mode_scheduler_role_arn: str | None = None
    event_mode_secret_prefix: str = "/pa-sc/prod/event-mode"
    event_mode_normal_instance_type: str = "t3a.small"
    event_mode_event_instance_type: str = "t3a.large"
    event_mode_min_active_minutes: int = Field(default=30, ge=1)
    event_mode_default_duration_hours: float = Field(default=5.0, gt=0)
    event_mode_max_duration_hours: float = Field(default=24.0, gt=0)
    # Diferença on-demand t3a.large − t3a.small em us-east-1 (US$ 0,0752 − 0,0188 por hora).
    event_mode_extra_cost_usd_per_hour: float = Field(default=0.0564, ge=0)
    public_db_host: str = "pa-sc.c8rw4kcekjcv.us-east-1.rds.amazonaws.com"
    public_db_name: str = "protected_areas_sc"
    public_ogc_base_url: str = "https://areasprotegidas-sc.com/geoserver/protected_areas_sc/"
    airflow_base_url: str | None = None
    airflow_username: str | None = None
    airflow_password: str | None = None
    airflow_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    @property
    def sc_bbox(self) -> tuple[float, float, float, float]:
        return (
            self.sc_bbox_min_x,
            self.sc_bbox_min_y,
            self.sc_bbox_max_x,
            self.sc_bbox_max_y,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
