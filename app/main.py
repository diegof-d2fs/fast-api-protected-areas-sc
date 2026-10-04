from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.v1.auth_routes import router as auth_router
from app.api.v1.batch_routes import router as batch_router
from app.api.v1.dictionary_routes import router as dictionary_router
from app.api.v1.routes import router as v1_router
from app.api.v1.ucs_routes import router as ucs_router
from app.application.auth import AuthService
from app.application.submissions import SubmissionService
from app.application.ucs import UCService
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.airflow.client import AirflowClient
from app.infrastructure.compute.ec2 import ProcessingNodeWaker
from app.infrastructure.geospatial.validator import GeospatialValidator
from app.infrastructure.persistence.auth_repository import PostgresAuthRepository
from app.infrastructure.persistence.postgres_ucs import PostgresUCRepository
from app.infrastructure.persistence.sqlite import SqliteSubmissionRepository
from app.infrastructure.storage.bronze import LocalBronzePublisher
from app.infrastructure.storage.local import LocalObjectStorage

_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _cloud_integrations(configured: Settings, local_publisher: LocalBronzePublisher | None):
    """Build the S3 Bronze mirror, the S3 pipeline-result reader and the processing-node waker."""
    import boto3

    from app.infrastructure.storage.s3 import S3BronzePublisher, S3PipelineResults

    if local_publisher is None or not configured.s3_bronze_bucket or not configured.s3_lake_bucket:
        raise RuntimeError(
            "storage_backend=s3 exige PA_SC_BRONZE_ROOT, PA_SC_S3_BRONZE_BUCKET e PA_SC_S3_LAKE_BUCKET"
        )
    s3 = boto3.client("s3", region_name=configured.aws_region)
    waker = None
    if configured.processing_instance_id:
        waker = ProcessingNodeWaker(
            boto3.client("ec2", region_name=configured.aws_region), configured.processing_instance_id
        )
    return (
        S3BronzePublisher(local_publisher, s3, configured.s3_bronze_bucket),
        S3PipelineResults(s3, configured.s3_lake_bucket),
        waker,
    )


async def _dispatch_forever(service: SubmissionService, interval_seconds: float) -> None:
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(service.dispatch_pending)
        except Exception:
            logger.exception("pending import dispatch failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    configure_logging(configured.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = LocalObjectStorage(
            configured.data_root,
            chunk_size=configured.upload_chunk_bytes,
            max_upload_bytes=configured.max_upload_bytes,
        )
        storage.initialize()
        repository = SqliteSubmissionRepository(configured.data_root / "submissions.sqlite3")
        repository.initialize()
        bronze_publisher = None
        pipeline_results = None
        processing_waker = None
        if configured.bronze_root:
            bronze_publisher = LocalBronzePublisher(configured.bronze_root, storage)
        if configured.storage_backend == "s3":
            bronze_publisher, pipeline_results, processing_waker = _cloud_integrations(
                configured, bronze_publisher
            )
        if bronze_publisher is not None:
            bronze_publisher.initialize()
        airflow_client = None
        if (
            configured.airflow_base_url
            and configured.airflow_username
            and configured.airflow_password
        ):
            airflow_client = AirflowClient(
                configured.airflow_base_url,
                username=configured.airflow_username,
                password=configured.airflow_password,
                timeout_seconds=configured.airflow_timeout_seconds,
            )
        uc_repository = None
        uc_service = None
        if configured.database_dsn:
            uc_repository = PostgresUCRepository(configured.database_dsn)
            uc_service = UCService(uc_repository)
        auth_service = None
        if configured.database_dsn:
            auth_service = AuthService(
                PostgresAuthRepository(configured.database_dsn),
                session_ttl=timedelta(hours=configured.session_ttl_hours),
                lockout_threshold=configured.login_lockout_threshold,
                lockout_window=timedelta(minutes=configured.login_lockout_window_minutes),
            )
            _bootstrap_admin(auth_service, configured)
        app.state.settings = configured
        app.state.storage = storage
        app.state.repository = repository
        app.state.bronze_publisher = bronze_publisher
        app.state.airflow_client = airflow_client
        app.state.submission_service = SubmissionService(
            repository,
            storage,
            GeospatialValidator(configured),
            bronze_publisher,
            airflow_client,
            uc_repository,
            pipeline_results=pipeline_results,
            processing_waker=processing_waker,
        )
        app.state.uc_repository = uc_repository
        app.state.uc_service = uc_service
        app.state.auth_service = auth_service
        dispatcher = None
        if airflow_client is not None and configured.dispatch_interval_seconds > 0:
            dispatcher = asyncio.create_task(
                _dispatch_forever(app.state.submission_service, configured.dispatch_interval_seconds)
            )
        yield
        if dispatcher is not None:
            dispatcher.cancel()

    app = FastAPI(
        title=configured.app_name,
        version=configured.app_version,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        summary="Importação e validação de geometrias de áreas protegidas de Santa Catarina",
        description="""
## Finalidade

API REST do TCC 3 para receber e acompanhar **importações geoespaciais** de Unidades de
Conservação (UCs) e Zonas de Amortecimento oficiais (ZAs). Buffers de Abrangência não são enviados:
são derivados da geometria da UC pelo pipeline.

## Fluxo de escrita geoespacial

`upload → quarentena → validação → aceite → Bronze → Airflow → PostGIS → derivados temáticos`

A API recebe, valida e acompanha a solicitação. Toda mutação geoespacial de domínio deve passar
pela Bronze e pelo Airflow. As rotas GET de `/ucs` consultam o PostGIS em modo somente leitura;
a API não insere UCs nem gera Buffers de Abrangência diretamente nas tabelas de domínio.

Toda mutação exige `Idempotency-Key`. O cliente pode enviar `X-Correlation-ID`; quando ausente
ou inválido, a API gera um UUID e o devolve no cabeçalho da resposta. Erros HTTP seguem
`application/problem+json`.

## Integração com o pipeline

A publicação dirigida por manifesto na Bronze e o disparo idempotente do Airflow estão
integrados. Para UCs, a execução encadeia `DAG_UCS`, `DAG_ZA_BUFFER` e as DAGs temáticas afetadas.
Cadastros usam `operation=create`; atualizações usam `operation=update`, exigem versão esperada
e preservam o mesmo `id_uc`, o histórico geométrico e a auditoria transacional.
No cadastro pontual, a `DAG_UCS` persiste a UC como `Point` e a `DAG_ZA_BUFFER` gera separadamente
o Buffer de Abrangência de 3.000 m; a FastAPI não cria esse buffer.
Uma ZA oficial posterior usa `za_oficial/replace_buffer_abrangencia`: o Airflow encerra o Buffer de Abrangência, ativa/versiona a
ZA e registra auditoria na mesma transação. `za_oficial/create` isolado permanece bloqueado e
será usado apenas no lote atômico UC+ZA novas. No fluxo agendado legado, uma ZA autoritativa com
`ds_fonte` que já esteja na Bronze pode ser descoberta quando sua UC passar a existir; esse
vínculo espacial não é permitido nas importações dirigidas pela API.
Se o Airflow estiver indisponível, a importação publicada fica `PUBLISHED` e é disparada
automaticamente quando o processamento voltar; se a Bronze estiver indisponível, a operação falha de
modo seguro e pode ser retomada.
No incremento atual, `DAG_PRODES`, `DAG_MAPBIOMAS_ALERTA` e `DAG_MAPBIOMAS` participam do
encadeamento dirigido. `DAG_FIRMS` está implementada, mas roda fora dessa cadeia por desenho: sua
aquisição diária reaproveita o snapshot cadastral vigente e não deve repetir download por causa de
uma alteração cadastral pontual.
""",
        openapi_tags=[
            {
                "name": "Importações geoespaciais",
                "description": (
                    "Envio, validação, publicação e acompanhamento de arquivos de UC e ZA oficial."
                ),
            },
            {
                "name": "Unidades de Conservação",
                "description": (
                    "Solicitações assíncronas de cadastro via Bronze/Airflow e consultas somente "
                    "leitura das UCs e Buffers de Abrangência materializados no PostGIS."
                ),
            },
            {
                "name": "Autenticação",
                "description": (
                    "Sessão opaca em cookie httpOnly e gestão de contas por um administrador."
                ),
            },
            {
                "name": "Saúde",
                "description": "Endpoints técnicos para liveness e readiness do contêiner.",
            },
        ],
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlation_and_logging(request: Request, call_next):
        received = request.headers.get("X-Correlation-ID", "")
        correlation_id = received if _CORRELATION_PATTERN.fullmatch(received) else str(uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        logging.getLogger("api.request").info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "correlation_id": correlation_id,
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _problem_response(
            request,
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            error_code=exc.error_code,
            violations=exc.violations,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        violations = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "rule": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return _problem_response(
            request,
            status_code=422,
            title="Erro de validação",
            detail="A requisição não atende ao contrato da API.",
            error_code="REQUEST_VALIDATION_ERROR",
            violations=violations,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem_response(
            request,
            status_code=exc.status_code,
            title="Erro HTTP",
            detail=str(exc.detail),
            error_code="HTTP_ERROR",
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never expose internals to an internet client; the full error stays in the log,
        # findable by the correlation id returned in the response.
        logging.getLogger("api.errors").exception(
            "unexpected error",
            extra={"correlation_id": getattr(request.state, "correlation_id", None), "path": request.url.path},
        )
        return _problem_response(
            request,
            status_code=500,
            title="Erro interno",
            detail="Ocorreu um erro inesperado. Informe o correlation_id ao suporte.",
            error_code="INTERNAL_ERROR",
        )

    app.include_router(health_router)
    app.include_router(v1_router)
    app.include_router(ucs_router)
    app.include_router(batch_router)
    app.include_router(auth_router)
    app.include_router(dictionary_router)
    return app


def _bootstrap_admin(auth_service: AuthService, configured: Settings) -> None:
    """Create the first administrator from environment variables, if none exists yet.

    Runs once at startup. A missing/unreachable database or missing bootstrap variables must
    never crash the API — they only mean nobody can log in until an operator fixes it, which is
    logged loudly instead of failing startup.
    """
    logger = logging.getLogger("api.auth.bootstrap")
    try:
        if auth_service.repository.count_users() > 0:
            return
        if not configured.admin_bootstrap_username or not configured.admin_bootstrap_password:
            logger.warning(
                "app_user está vazia e PA_SC_ADMIN_BOOTSTRAP_USERNAME/PASSWORD não estão "
                "definidas: ninguém consegue autenticar até essas variáveis serem configuradas "
                "e a API reiniciada."
            )
            return
        auth_service.bootstrap_admin(
            username=configured.admin_bootstrap_username,
            password=configured.admin_bootstrap_password,
        )
        logger.info(
            "administrador inicial criado a partir de PA_SC_ADMIN_BOOTSTRAP_USERNAME",
            extra={"username": configured.admin_bootstrap_username},
        )
    except Exception:
        logger.exception("falha ao inicializar o administrador de bootstrap; API segue no ar")


def _problem_response(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    error_code: str,
    violations: list[dict] | None = None,
) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": f"urn:protected-areas-sc:error:{error_code.lower()}",
            "title": title,
            "status": status_code,
            "detail": detail,
            "instance": request.url.path,
            "error_code": error_code,
            "correlation_id": correlation_id,
            "violations": violations or [],
        },
    )


app = create_app()
