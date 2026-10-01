from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1/health", tags=["Saúde"])


@router.get(
    "/live",
    summary="Verificar se o processo está ativo",
    description="Liveness probe: confirma que o processo HTTP está respondendo.",
    operation_id="health_live",
)
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    summary="Verificar se a API está pronta",
    description="Readiness probe: verifica storage local e repositório de importações exigidos na Fase 1.",
    operation_id="health_ready",
)
def ready(request: Request) -> JSONResponse:
    checks = {
        "storage": request.app.state.storage.ready(),
        "submission_repository": request.app.state.repository.ready(),
    }
    uc_repository = getattr(request.app.state, "uc_repository", None)
    if uc_repository is not None:
        try:
            uc_repository.check_connection()
            checks["cadastral_postgis"] = True
        except Exception:
            checks["cadastral_postgis"] = False
    settings = request.app.state.settings
    if settings.readiness_requires_airflow:
        airflow_client = getattr(request.app.state, "airflow_client", None)
        checks["airflow"] = airflow_client is not None and airflow_client.ready()
    if settings.bronze_root:
        bronze_publisher = getattr(request.app.state, "bronze_publisher", None)
        checks["bronze"] = bronze_publisher is not None and bronze_publisher.ready()
    is_ready = all(checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready", "checks": checks},
    )
