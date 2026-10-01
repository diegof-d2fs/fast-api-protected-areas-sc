from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Path, Request, UploadFile, status

from app.api.dependencies import get_current_user, get_submission_service
from app.application.submissions import SubmissionService
from app.core.errors import AppError
from app.domain.auth import CurrentUser
from app.domain.metadata import parse_metadata
from app.domain.models import (
    DuplicatePolicy,
    ProblemDetail,
    SubmissionDomain,
    SubmissionOperation,
    SubmissionView,
    ValidationReport,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Importações geoespaciais"],
    dependencies=[Depends(get_current_user)],
)


@router.post(
    "/imports",
    response_model=SubmissionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enviar UC ou ZA oficial para validação",
    description="""
Cria uma **importação geoespacial assíncrona** para uma Unidade de Conservação (`uc`)
ou Zona de Amortecimento oficial (`za_oficial`).

O arquivo é recebido por streaming, identificado por conteúdo, armazenado inicialmente em
quarentena, validado e então movido para a área de aceitos ou mantido em quarentena como rejeitado.
A resposta contém o estado atual e links para acompanhamento.

Formatos aceitos:

- Shapefile em ZIP com `.shp`, `.shx`, `.dbf` e `.prj`;
- GeoJSON direto, com CRS no arquivo ou em `metadata.source_crs`;
- KML direto, interpretado em WGS 84 conforme a especificação do formato.

Geometrias por domínio:

- `uc/create`: uma ou varias feicoes `Point`, `Polygon` ou `MultiPolygon`. Em lote, cada feature deve ter nome e identificador forte proprios. Uma UC pontual permanece como ponto na
  `DAG_UCS`; o Buffer de Abrangência de 3.000 m é derivado posteriormente pela `DAG_ZA_BUFFER`;
- `uc/update`: `Point`, `Polygon` ou `MultiPolygon`, sempre com uma única feição completa;
- `za_oficial/replace_buffer_abrangencia`: exatamente uma feição `Polygon` ou `MultiPolygon`, vinculada a
  uma UC existente com Buffer de Abrangência ativo. A publicação encerra o buffer e ativa a ZA na
  mesma transação;
- `za_oficial/create`: pode ser validada, mas não é publicada isoladamente. O cadastro de UC e
  ZA novas pertence ao lote atômico `/import-batches`.

A API não converte uma UC pontual em polígono e não grava seu buffer no PostGIS. Essa mutação
derivada pertence exclusivamente ao Airflow.

Metadados mínimos:

- `uc/create`: `{"source": "..."}`; `name` é opcional quando o arquivo possui `nm_uc`, `nome_uc`,
  `nome`, `nm_unid_con` ou `name`;
- `uc/update`: além de `source`, exige `reason`, `expected_version` e ao menos um identificador
  forte (`official_identifier`, `cd_cnuc` ou `wdpa_pid`) e contém exatamente uma feição;
- `za_oficial/replace_buffer_abrangencia`: `{"source": "...", "uc_identifier": "...", "reason": "..."}`;
  `valid_from` pode registrar o início da vigência oficial.

Repetir a mesma `Idempotency-Key` com o mesmo payload devolve a importação original. Reutilizar
a chave com payload diferente produz `409 Conflict`.

Em `uc/create`, `duplicate_policy=reject_batch` é o padrão e preserva atomicidade estrita.
`duplicate_policy=skip_duplicates` publica somente as UCs inéditas de um lote misto e registra
as feições ignoradas em `batch_items`, `duplicate_matches` e no manifesto.
""",
    response_description="Importação criada ou repetição idempotente de uma importação existente.",
    operation_id="create_geospatial_import",
    responses={
        400: {"model": ProblemDetail, "description": "Metadados ou cabeçalhos inválidos."},
        409: {"model": ProblemDetail, "description": "Idempotency-Key reutilizada com outro payload."},
        413: {"model": ProblemDetail, "description": "Arquivo acima do limite configurado."},
        415: {"model": ProblemDetail, "description": "Formato não reconhecido ou extensão incompatível."},
        422: {"model": ProblemDetail, "description": "Requisição multipart fora do contrato."},
    },
)
async def create_geospatial_import(
    request: Request,
    file: Annotated[
        UploadFile,
        File(description="Arquivo Shapefile ZIP, GeoJSON ou KML contendo um único dataset."),
    ],
    domain: Annotated[
        SubmissionDomain,
        Form(description="Domínio do arquivo: `uc` ou `za_oficial`. O Buffer de Abrangência não é aceito por upload."),
    ],
    metadata: Annotated[
        str,
        Form(
            description=(
                "Objeto JSON. Para UC, informe `source`; `name` pode sobrescrever o nome lido "
                "dos atributos do arquivo. Em `uc/update` e `uc/replace_point`, informe também `reason`, "
                "`expected_version` e um identificador forte. Para ZA oficial, informe `source` "
                "e `uc_identifier`; em `replace_buffer_abrangencia`, `reason` também é obrigatório. "
                "`source_crs` pode informar o CRS de um GeoJSON."
            ),
            examples=['{"source":"órgão ambiental","source_crs":"EPSG:4674"}'],
        ),
    ],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            description=(
                "Chave gerada pelo cliente antes do primeiro envio (normalmente um UUID). "
                "O frontend fará isso sem intervenção do usuário. Reutilize a mesma chave ao "
                "repetir exatamente a mesma operação; gere outra para uma nova operação."
            ),
            examples=["7cb0d867-7bd4-4dd8-b03c-c91679949d1a"],
        ),
    ],
    operation: Annotated[
        SubmissionOperation,
        Form(
            description=(
                "Operação de negócio. `create` cadastra uma nova entidade; `update` atualiza "
                "uma UC de forma versionada via Bronze/Airflow; `replace_buffer_abrangencia` substitui o "
                "Buffer de Abrangência ativo de uma UC existente por uma ZA oficial; `replace_point` troca uma UC "
                "pontual por seu limite poligonal oficial, preservando versões."
            )
        ),
    ] = SubmissionOperation.CREATE,
    repair_geometry: Annotated[
        bool,
        Form(
            description=(
                "Quando `true`, permite tentativa explícita e auditável de correção geométrica. "
                "O padrão é rejeitar geometrias inválidas."
            )
        ),
    ] = False,
    duplicate_policy: Annotated[
        DuplicatePolicy,
        Form(
            description=(
                "Política exclusiva de `uc/create`: `reject_batch` rejeita atomicamente o lote "
                "quando houver repetidas; `skip_duplicates` publica somente as UCs inéditas e "
                "registra as ignoradas no resultado e no manifesto."
            )
        ),
    ] = DuplicatePolicy.REJECT_BATCH,
    service: SubmissionService = Depends(get_submission_service),
    current_user: CurrentUser = Depends(get_current_user),
) -> SubmissionView:
    if operation in {SubmissionOperation.EXTINGUISH, SubmissionOperation.CREATE_WITH_ZONE}:
        raise AppError(
            400, "EXTINGUISH_REQUIRES_COMMAND", "Use o comando de extinção",
            "Use /ucs/{id_uc}/extinguish ou /import-batches para comandos compostos.",
        )
    metadata_payload = parse_metadata(metadata, domain, operation)
    # RNF16 exige identificação do operador no log de auditoria: a sessão validada no servidor
    # sempre prevalece sobre qualquer `actor` que o cliente tenha enviado no JSON de metadados.
    metadata_payload["actor"] = f"user:{current_user.id}:{current_user.nome}"
    metadata_payload["actor_id"] = current_user.id
    submission = await service.create(
        upload=file,
        domain=domain,
        operation=operation,
        duplicate_policy=duplicate_policy,
        metadata=metadata_payload,
        repair_geometry=repair_geometry,
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    return SubmissionView.from_submission(submission)


@router.get(
    "/imports/{import_id}",
    response_model=SubmissionView,
    summary="Consultar uma importação geoespacial",
    description="Retorna o estado, os identificadores de correlação e as chaves lógicas dos artefatos.",
    response_description="Estado atual da importação.",
    operation_id="get_geospatial_import",
    responses={404: {"model": ProblemDetail, "description": "Importação não encontrada."}},
)
def get_geospatial_import(
    import_id: Annotated[str, Path(description="Identificador retornado na criação da importação.")],
    service: SubmissionService = Depends(get_submission_service),
) -> SubmissionView:
    return SubmissionView.from_submission(service.get_with_pipeline_state(import_id))


@router.get(
    "/imports/{import_id}/validation",
    response_model=ValidationReport,
    summary="Consultar o relatório de validação",
    description=(
        "Apresenta formato detectado, CRS, tipos geométricos, contagens, envelope, reparos, "
        "advertências e erros reproduzíveis."
    ),
    response_description="Relatório técnico da validação geoespacial.",
    operation_id="get_geospatial_import_validation",
    responses={
        404: {"model": ProblemDetail, "description": "Importação não encontrada."},
        409: {"model": ProblemDetail, "description": "Validação ainda indisponível."},
    },
)
def get_geospatial_import_validation(
    import_id: Annotated[str, Path(description="Identificador da importação geoespacial.")],
    service: SubmissionService = Depends(get_submission_service),
) -> ValidationReport:
    submission = service.get(import_id)
    if submission.validation is None:
        raise AppError(
            409,
            "VALIDATION_NOT_AVAILABLE",
            "Validação indisponível",
            "A validação da importação ainda não está disponível.",
        )
    return submission.validation


@router.post(
    "/imports/{import_id}/publish",
    response_model=SubmissionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Publicar uma importação aceita e iniciar o pipeline",
    description="""
Publica de forma idempotente o lote dirigido por manifesto na camada Bronze e solicita a DAG
adequada pela API REST do Airflow. Para uma UC, o processamento completo deve encadear:
`DAG_UCS → DAG_ZA_BUFFER → DAGs temáticas afetadas`.

O Airflow é o único responsável por gravar a UC, gerar o Buffer de Abrangência quando aplicável e refazer os
cruzamentos espaciais. Esta API não escreve diretamente nas tabelas geoespaciais de domínio.
Para `create`, a API consulta o PostGIS em modo somente leitura antes da publicação. No modo
atômico, uma UC existente por identificador forte ou geometria produz `409 UC_ALREADY_EXISTS` e
estado `DUPLICATE`. No modo `skip_duplicates`, as repetidas são auditadas e apenas as inéditas
seguem; o pipeline repete a verificação para cobrir publicações concorrentes.
Para `update`, o Airflow localiza a UC por identificador forte, bloqueia o registro e compara
`metadata.expected_version`. Uma versão desatualizada termina como `FAILED` com
`UC_VERSION_CONFLICT`; uma atualização válida preserva `id_uc`, encerra a versão geométrica
anterior, cria a seguinte e registra `cadastral_event` na mesma transação.
Para `uc/replace_point`, aplicam-se as mesmas garantias de versão, mas o Airflow também confirma
que a geometria vigente é Point e a nova é Polygon/MultiPolygon, registra evento específico e
encadeia a reconstrução do Buffer de Abrangência a partir do novo limite.
Para `za_oficial/replace_buffer_abrangencia`, o Airflow bloqueia a UC, encerra o Buffer de Abrangência vigente, insere e ativa a
ZA oficial e registra um único evento auditável. Se qualquer passo falhar, toda a transação é
revertida e o Buffer de Abrangência anterior permanece ativo. `za_oficial/create` nunca é publicado isoladamente.
Se a publicação ou o Airflow estiverem indisponíveis, a rota responde `503` sem confirmar a
mutação de domínio e pode ser repetida de forma idempotente.
""",
    response_description="Importação publicada ou publicação idempotente já existente.",
    operation_id="publish_geospatial_import",
    responses={
        404: {"model": ProblemDetail, "description": "Importação não encontrada."},
        409: {
            "model": ProblemDetail,
            "description": "Importação não publicável ou UC já cadastrada.",
        },
        503: {"model": ProblemDetail, "description": "Bronze ou Airflow indisponível."},
    },
)
def publish_geospatial_import(
    import_id: Annotated[str, Path(description="Identificador da importação aceita.")],
    service: SubmissionService = Depends(get_submission_service),
) -> SubmissionView:
    return SubmissionView.from_submission(service.publish(import_id))
