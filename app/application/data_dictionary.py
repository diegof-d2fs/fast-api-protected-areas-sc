"""Monta o dicionário de dados de envio a partir das mesmas regras que a validação aplica."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.domain.data_dictionary import (
    DICTIONARY_VERSION,
    OFFICIAL_ZONE_COLUMNS,
    UC_COLUMNS,
    CrsView,
    DataDictionary,
    ErrorView,
    FormatView,
    LimitView,
    MaintainedBaseView,
    MetadataFieldView,
    TerritoryView,
    UploadBaseView,
    column_views,
)
from app.domain.metadata import OfficialZoneSubmissionMetadata, UcSubmissionMetadata
from app.domain.models import GeospatialFormat, SubmissionDomain, SubmissionOperation
from app.domain.ucs import UCExtinguishRequest
from app.infrastructure.geospatial.validator import (
    ALLOWED_GEOMETRY_TYPES,
    FORMAT_EXTENSIONS,
    KML_EPSG,
    SHAPEFILE_FALLBACK_ENCODING,
    SHAPEFILE_OPTIONAL,
    SHAPEFILE_REQUIRED,
)

_METADATA_DESCRIPTIONS = {
    "source": "Órgão, publicação ou sistema de onde o arquivo foi obtido.",
    "legal_act_date": "Data do ato legal que fundamenta o envio (AAAA-MM-DD).",
    "reason": "Justificativa auditável da mudança.",
    "actor": "Responsável pelo envio, quando diferente do usuário logado.",
    "name": "Nome da UC; tem prioridade sobre o nome lido do arquivo.",
    "official_identifier": "Identificador oficial da UC na fonte (equivale a `uc_id`).",
    "cd_cnuc": "Código CNUC da UC.",
    "wdpa_pid": "Identificador WDPA da UC.",
    "expected_version": "Versão da geometria vigente observada antes do envio (trava otimista).",
    "uc_identifier": "Identificador da UC dona da zona (CNUC, WDPA ou identificador oficial).",
    "valid_from": "Início da vigência oficial da zona (AAAA-MM-DD).",
    "source_crs": "CRS de um GeoJSON sem membro `crs`, por exemplo `EPSG:4674`.",
}

# Polígono pequeno dentro de Santa Catarina (região de Florianópolis), em SIRGAS 2000.
_SAMPLE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-48.60, -27.70], [-48.55, -27.70], [-48.55, -27.65], [-48.60, -27.65], [-48.60, -27.70]]],
}
_SAMPLE_ZONE = {
    "type": "Polygon",
    "coordinates": [[[-48.63, -27.73], [-48.52, -27.73], [-48.52, -27.62], [-48.63, -27.62], [-48.63, -27.73]]],
}
_SAMPLE_UC_PROPERTIES = {
    "nm_uc": "UC de Exemplo",
    "cd_cnuc": "0000.42.9999",
    "ds_categoria": "Parque",
    "ds_grupo": "Proteção Integral",
    "ds_esfera": "Estadual",
    "nm_orgao_gestor": "IMA/SC",
    "dt_criacao": "2001-06-05",
    "ds_ato_legal": "Decreto Estadual de exemplo",
    "area_total_ha": 1234.5,
}


def _feature_collection(geometry: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
        "features": [{"type": "Feature", "properties": properties, "geometry": geometry}],
    }


def _metadata_fields(model: type[BaseModel], required: set[str], fields: tuple[str, ...]) -> list[MetadataFieldView]:
    properties = model.model_json_schema()["properties"]
    views = []
    for name in fields:
        schema = properties.get(name, {"type": "string"})
        variants = schema.get("anyOf", [schema])
        concrete = next((item for item in variants if item.get("type") != "null"), {"type": "string"})
        kind = concrete.get("format") or concrete.get("type", "string")
        views.append(
            MetadataFieldView(
                name=name,
                type={"string": "texto", "integer": "inteiro", "date": "data"}.get(kind, kind),
                required=name in required,
                max_length=concrete.get("maxLength"),
                minimum=concrete.get("minimum"),
                description=_METADATA_DESCRIPTIONS[name],
            )
        )
    return views


def _geometry_types(domain: SubmissionDomain, *, polygon_only: bool = False) -> list[str]:
    allowed = ALLOWED_GEOMETRY_TYPES[domain]
    if polygon_only:
        allowed = allowed - {"Point"}
    order = ["Point", "Polygon", "MultiPolygon"]
    return [kind for kind in order if kind in allowed]


def _bases() -> list[UploadBaseView]:
    uc_columns = column_views(UC_COLUMNS)
    zone_columns = column_views(OFFICIAL_ZONE_COLUMNS)
    uc_common = ("source", "name", "official_identifier", "cd_cnuc", "wdpa_pid", "legal_act_date", "actor",
                 "source_crs")
    uc_versioned = ("source", "reason", "expected_version", "official_identifier", "cd_cnuc", "wdpa_pid",
                    "legal_act_date", "actor", "source_crs")
    strong_identity_rule = (
        "Informe ao menos um identificador forte em metadata: `official_identifier`, `cd_cnuc` ou `wdpa_pid`."
    )
    return [
        UploadBaseView(
            id="uc-create",
            title="Cadastro de Unidade de Conservação",
            summary="Cadastra uma ou várias UCs novas, com limite poligonal ou como ponto.",
            endpoint="POST /api/v1/imports",
            domain=SubmissionDomain.UC.value,
            operation=SubmissionOperation.CREATE.value,
            geometry_types=_geometry_types(SubmissionDomain.UC),
            feature_rule="Uma ou mais feições; cada feição é uma UC.",
            columns=uc_columns,
            metadata_fields=_metadata_fields(UcSubmissionMetadata, {"source"}, uc_common),
            metadata_example={"source": "Portal de dados abertos do IMA/SC", "source_crs": "EPSG:4674"},
            rules=[
                "Com várias feições, cada uma precisa de nome e de identificador forte próprios "
                "(`uc_id`, `cd_cnuc` ou `wdpa_pid`), sem repetição de identidade ou geometria no lote.",
                "Uma UC enviada como ponto recebe o Buffer de Abrangência de 3.000 m calculado pelo pipeline.",
                "Por padrão, uma UC já cadastrada rejeita o lote inteiro (`duplicate_policy=reject_batch`); "
                "`skip_duplicates` publica só as inéditas.",
                "UC com ZA oficial própria é cadastrada no lote UC + ZA.",
            ],
            example_geojson=_feature_collection(_SAMPLE_POLYGON, _SAMPLE_UC_PROPERTIES),
        ),
        UploadBaseView(
            id="uc-update",
            title="Atualização da geometria de Unidade de Conservação",
            summary="Substitui o limite vigente de uma UC existente, preservando a versão anterior.",
            endpoint="POST /api/v1/imports",
            domain=SubmissionDomain.UC.value,
            operation=SubmissionOperation.UPDATE.value,
            geometry_types=_geometry_types(SubmissionDomain.UC),
            feature_rule="Exatamente uma feição, com a geometria completa da UC.",
            columns=uc_columns,
            metadata_fields=_metadata_fields(
                UcSubmissionMetadata, {"source", "reason", "expected_version"}, uc_versioned
            ),
            metadata_example={
                "source": "Decreto de alteração de limites",
                "reason": "Ajuste do limite conforme novo memorial descritivo",
                "expected_version": 1,
                "cd_cnuc": "0000.42.9999",
            },
            rules=[
                strong_identity_rule,
                "`expected_version` deve ser a versão vigente; se outra pessoa alterou a UC antes, o envio "
                "é recusado com conflito de versão.",
                "Todo o histórico temático é recalculado para a geometria nova.",
            ],
            example_geojson=_feature_collection(_SAMPLE_POLYGON, {"nm_uc": "UC de Exemplo", "cd_cnuc": "0000.42.9999"}),
        ),
        UploadBaseView(
            id="uc-replace-point",
            title="Substituição de ponto por polígono em Unidade de Conservação",
            summary="Troca uma UC cadastrada como ponto pelo seu limite poligonal oficial.",
            endpoint="POST /api/v1/imports",
            domain=SubmissionDomain.UC.value,
            operation=SubmissionOperation.REPLACE_POINT.value,
            geometry_types=_geometry_types(SubmissionDomain.UC, polygon_only=True),
            feature_rule="Exatamente uma feição poligonal.",
            columns=uc_columns,
            metadata_fields=_metadata_fields(
                UcSubmissionMetadata, {"source", "reason", "expected_version"}, uc_versioned
            ),
            metadata_example={
                "source": "Memorial descritivo oficial",
                "reason": "Limite poligonal publicado",
                "expected_version": 1,
                "cd_cnuc": "0000.42.9999",
            },
            rules=[
                strong_identity_rule,
                "A UC-alvo precisa estar cadastrada como ponto.",
                "O Buffer de Abrangência derivado do ponto é recalculado sobre o polígono.",
            ],
            example_geojson=_feature_collection(_SAMPLE_POLYGON, {"nm_uc": "UC de Exemplo", "cd_cnuc": "0000.42.9999"}),
        ),
        UploadBaseView(
            id="uc-extinguish",
            title="Extinção de Unidade de Conservação",
            summary="Marca uma UC como extinta e encerra sua zona ativa. Não há arquivo: a API usa o cadastro vigente.",
            endpoint="POST /api/v1/ucs/{uc_id}/extinguish",
            domain=SubmissionDomain.UC.value,
            operation=SubmissionOperation.EXTINGUISH.value,
            geometry_types=[],
            feature_rule="Sem arquivo.",
            columns=[],
            metadata_fields=_metadata_fields(
                UCExtinguishRequest, {"source", "reason", "expected_version"},
                ("source", "reason", "expected_version"),
            ),
            metadata_example={"source": "Lei de desafetação", "reason": "UC extinta por lei", "expected_version": 1},
            rules=["A UC continua no histórico; só deixa de ser vigente.", "Uma UC já extinta recusa o pedido."],
            example_geojson=None,
        ),
        UploadBaseView(
            id="uc-za-batch",
            title="Cadastro de UC com ZA oficial em lote atômico",
            summary="Cadastra uma UC nova junto com sua Zona de Amortecimento oficial, numa única publicação.",
            endpoint="POST /api/v1/import-batches",
            domain=None,
            operation=None,
            geometry_types=_geometry_types(SubmissionDomain.OFFICIAL_ZONE),
            feature_rule="Dois envios validados antes: a UC (`uc/create`) e a ZA (`za_oficial/create`), uma feição cada.",
            columns=zone_columns,
            metadata_fields=_metadata_fields(
                OfficialZoneSubmissionMetadata, {"source", "uc_identifier"},
                ("source", "uc_identifier", "valid_from", "legal_act_date", "source_crs"),
            ),
            metadata_example={"source": "Plano de Manejo", "uc_identifier": "0000.42.9999", "valid_from": "2018-03-01"},
            rules=[
                "A ZA oficial não é publicada sozinha: envie a UC e a ZA em `/api/v1/imports` e agrupe-as no lote.",
                "A ZA precisa ser polígono; colunas do arquivo da ZA são opcionais e servem para conferência.",
                "As colunas da UC seguem a base \"Cadastro de Unidade de Conservação\".",
            ],
            example_geojson=_feature_collection(_SAMPLE_ZONE, {"nm_uc": "UC de Exemplo", "ds_fonte": "Plano de Manejo"}),
        ),
        UploadBaseView(
            id="za-replace-buffer",
            title="Substituição do Buffer de Abrangência pela ZA oficial",
            summary="Substitui o Buffer de Abrangência de uma UC existente pela ZA oficial publicada.",
            endpoint="POST /api/v1/imports",
            domain=SubmissionDomain.OFFICIAL_ZONE.value,
            operation=SubmissionOperation.REPLACE_BUFFER_ABRANGENCIA.value,
            geometry_types=_geometry_types(SubmissionDomain.OFFICIAL_ZONE),
            feature_rule="Exatamente uma feição poligonal.",
            columns=zone_columns,
            metadata_fields=_metadata_fields(
                OfficialZoneSubmissionMetadata, {"source", "uc_identifier", "reason"},
                ("source", "uc_identifier", "reason", "valid_from", "legal_act_date", "source_crs"),
            ),
            metadata_example={
                "source": "Portaria de aprovação do Plano de Manejo",
                "uc_identifier": "0000.42.9999",
                "reason": "ZA oficial publicada",
                "valid_from": "2026-02-01",
            },
            rules=[
                "A UC precisa ter Buffer de Abrangência ativo.",
                "A publicação encerra o Buffer de Abrangência e ativa a ZA oficial na mesma transação.",
            ],
            example_geojson=_feature_collection(_SAMPLE_ZONE, {"nm_uc": "UC de Exemplo", "ds_fonte": "Portaria"}),
        ),
    ]


_MAINTAINED_BASES = [
    MaintainedBaseView(
        id="buffer-abrangencia", title="Buffer de Abrangência", source="calculado pelo pipeline",
        refresh="a cada UC nova ou alterada",
        description="Área de 3.000 m em torno da UC sem ZA oficial. Não é aceito por upload.",
    ),
    MaintainedBaseView(
        id="prodes", title="PRODES", source="INPE/TerraBrasilis", refresh="mensal (fonte anual)",
        description="Desmatamento anual cruzado com UCs e zonas.",
    ),
    MaintainedBaseView(
        id="mapbiomas-alerta", title="MapBiomas Alerta", source="API MapBiomas Alerta", refresh="semanal",
        description="Alertas de desmatamento validados de SC, PR e RS cruzados com UCs e zonas.",
    ),
    MaintainedBaseView(
        id="mapbiomas", title="MapBiomas Cobertura", source="MapBiomas", refresh="anual",
        description="Uso e cobertura da terra, todos os anos da coleção.",
    ),
    MaintainedBaseView(
        id="firms", title="FIRMS (focos de calor)", source="NASA FIRMS", refresh="diária",
        description="Focos de calor MODIS e VIIRS desde 2000 cruzados com UCs e zonas.",
    ),
]

_ERRORS = [
    ("MISSING_FILENAME", "arquivo", "O envio não trouxe nome de arquivo.", "Envie o arquivo pelo campo `file`."),
    ("INVALID_FILENAME", "arquivo", "O nome do arquivo tem caracteres ou caminho não permitidos.",
     "Renomeie o arquivo usando letras, números, hífen e ponto."),
    ("ZIP_ENTRY_LIMIT", "arquivo", "O ZIP tem entradas demais.", "Deixe no ZIP só os arquivos do Shapefile."),
    ("ZIP_SLIP", "arquivo", "Uma entrada do ZIP tem caminho inseguro.", "Recrie o ZIP sem caminhos absolutos ou `..`."),
    ("ZIP_LINK_NOT_ALLOWED", "arquivo", "O ZIP contém um link simbólico.", "Compacte os arquivos reais."),
    ("ENCRYPTED_ZIP_ENTRY", "arquivo", "O ZIP está protegido por senha.", "Compacte sem senha."),
    ("DUPLICATE_ZIP_ENTRY", "arquivo", "O ZIP repete um arquivo.", "Remova a cópia duplicada."),
    ("UNEXPECTED_ZIP_CONTENT", "arquivo", "O ZIP contém um tipo de arquivo não aceito.",
     "Mantenha só .shp, .shx, .dbf, .prj e os opcionais aceitos."),
    ("ZIP_UNCOMPRESSED_LIMIT", "arquivo", "O conteúdo descompactado passa do limite.", "Divida o envio ou simplifique a geometria."),
    ("ZIP_COMPRESSION_RATIO", "arquivo", "Uma entrada tem compressão anormalmente alta.", "Recrie o ZIP com compressão padrão."),
    ("SHAPEFILE_DATASET_COUNT", "arquivo", "O ZIP não tem exatamente um .shp.", "Envie um Shapefile por ZIP."),
    ("MISSING_SHAPEFILE_SIDECARS", "arquivo", "Falta .shx, .dbf ou .prj.", "Inclua os quatro arquivos obrigatórios com o mesmo nome."),
    ("MULTIPLE_OR_UNRELATED_DATASETS", "arquivo", "O ZIP tem arquivos de outro dataset.", "Deixe só os arquivos do Shapefile enviado."),
    ("SHAPEFILE_ENCODING_ERROR", "conteúdo", "O DBF não pôde ser lido com a codificação informada.",
     "Inclua um .cpg com a codificação correta (por exemplo, UTF-8)."),
    ("MISSING_CPG", "conteúdo", "Aviso: sem .cpg, o DBF foi lido em latin1.", "Inclua o .cpg para evitar acentos trocados."),
    ("INVALID_GEOJSON", "conteúdo", "O arquivo não é um JSON válido.", "Valide o arquivo num editor ou exporte de novo pelo QGIS."),
    ("INVALID_GEOJSON_ROOT", "conteúdo", "A raiz do GeoJSON não é um objeto.", "Use Feature ou FeatureCollection."),
    ("UNSUPPORTED_GEOJSON_ROOT", "conteúdo", "A raiz do GeoJSON não é Feature nem FeatureCollection.",
     "Envolva as geometrias em uma FeatureCollection."),
    ("INVALID_FEATURE_COLLECTION", "conteúdo", "`features` não é uma lista.", "Corrija a estrutura do GeoJSON."),
    ("INVALID_FEATURE", "conteúdo", "Um item de `features` não é uma Feature.", "Cada item precisa de `type: Feature`."),
    ("MISSING_CRS", "conteúdo", "O GeoJSON não informa o CRS.", "Inclua o membro `crs` ou `metadata.source_crs`."),
    ("INVALID_CRS", "conteúdo", "O CRS informado não é reconhecido.", "Use um código EPSG, por exemplo `EPSG:4674`."),
    ("INVALID_KML", "conteúdo", "O KML não é um XML válido.", "Exporte o KML de novo."),
    ("INVALID_KML_ROOT", "conteúdo", "A raiz do arquivo não é `kml`.", "Envie um KML padrão."),
    ("INVALID_KML_COORDINATES", "conteúdo", "Há coordenadas KML mal formadas.", "Use `longitude,latitude` separados por vírgula."),
    ("EMPTY_KML", "conteúdo", "O KML não tem Placemark com geometria aceita.", "Inclua Point, Polygon ou MultiGeometry."),
    ("KML_CRS_DEFINED_BY_FORMAT", "conteúdo", "Aviso: KML é sempre lido em WGS 84.", "Nenhuma ação necessária."),
    ("UNREADABLE_DATASET", "conteúdo", "O arquivo não pôde ser lido.", "Abra o arquivo no QGIS e exporte de novo."),
    ("EMPTY_DATASET", "geometria", "O arquivo não tem feições.", "Confira se a camada exportada não está vazia."),
    ("FEATURE_LIMIT_EXCEEDED", "geometria", "Feições acima do limite.", "Divida o envio."),
    ("EMPTY_GEOMETRY", "geometria", "Uma feição não tem geometria.", "Remova a feição ou desenhe a geometria."),
    ("GEOMETRY_TYPE_NOT_ALLOWED", "geometria", "Tipo de geometria não aceito nesta base.", "Confira os tipos aceitos da base."),
    ("INVALID_GEOMETRY", "geometria", "A geometria é topologicamente inválida.",
     "Corrija no QGIS (Corrigir geometrias) ou reenvie com `repair_geometry=true`."),
    ("GEOMETRY_REPAIR_REJECTED", "geometria", "A correção automática gerou um tipo incompatível.", "Corrija a geometria na origem."),
    ("GEOMETRY_REPAIR_AREA_DELTA", "geometria", "A correção mudaria a área além do limite.", "Corrija a geometria na origem."),
    ("GEOMETRY_REPAIRED", "geometria", "Aviso: a geometria foi corrigida automaticamente.", "Confira a variação de área informada."),
    ("OUTSIDE_SC_BBOX", "geometria", "A geometria não toca Santa Catarina.", "Confira o CRS e a posição dos dados."),
    ("VERTEX_LIMIT_EXCEEDED", "geometria", "Vértices acima do limite.", "Simplifique a geometria ou divida o envio."),
    ("UC_OPERATION_REQUIRES_SINGLE_FEATURE", "geometria", "A operação exige exatamente uma feição.", "Envie uma UC por importação."),
    ("OPERATION_REQUIRES_SINGLE_FEATURE", "geometria", "A operação exige exatamente uma feição.", "Envie uma zona por importação."),
    ("REPLACE_POINT_REQUIRES_POLYGON", "geometria", "A substituição de ponto exige polígono.", "Envie o limite poligonal oficial."),
    ("MISSING_UC_NAME", "conteúdo", "O nome da UC não foi encontrado.", "Inclua `nm_uc` no arquivo ou `name` em metadata."),
    ("MISSING_UC_IDENTITY", "lote", "Uma UC do lote não tem identificador forte.", "Preencha `uc_id`, `cd_cnuc` ou `wdpa_pid` em cada feição."),
    ("DUPLICATE_UC_IDENTITY_IN_BATCH", "lote", "Duas feições do lote têm o mesmo identificador.", "Corrija os identificadores repetidos."),
    ("DUPLICATE_UC_GEOMETRY_IN_BATCH", "lote", "Duas feições do lote têm a mesma geometria.", "Remova a feição repetida."),
    ("UC_ALREADY_EXISTS", "lote", "A UC já está cadastrada.", "Use a atualização de geometria ou `skip_duplicates`."),
    ("ZA_CREATE_REQUIRES_BATCH", "lote", "A ZA oficial nova não é publicada sozinha.", "Use o lote UC + ZA ou a substituição do Buffer de Abrangência."),
    ("INVALID_METADATA_JSON", "metadados", "`metadata` não é JSON válido.", "Envie um objeto JSON, como no exemplo da base."),
    ("INVALID_METADATA_TYPE", "metadados", "`metadata` não é um objeto JSON.", "Envie `{...}`, não lista nem texto."),
    ("INVALID_DOMAIN_METADATA", "metadados", "Faltam campos obrigatórios ou há valores inválidos.", "Confira a tabela de metadados da base."),
    ("INVALID_UPDATE_METADATA", "metadados", "Faltam `reason`, `expected_version` ou identificador forte.", "Preencha os três."),
    ("INVALID_REPLACE_POINT_METADATA", "metadados", "Faltam `reason`, `expected_version` ou identificador forte.", "Preencha os três."),
    ("INVALID_REPLACE_BUFFER_ABRANGENCIA_METADATA", "metadados", "Falta `reason`.", "Informe a justificativa da substituição."),
]


def build_data_dictionary(settings: Settings) -> DataDictionary:
    shapefile_extensions = sorted(FORMAT_EXTENSIONS[GeospatialFormat.SHAPEFILE_ZIP])
    formats = [
        FormatView(
            id=GeospatialFormat.SHAPEFILE_ZIP.value,
            label="Shapefile compactado (ZIP)",
            extensions=shapefile_extensions,
            required_files=sorted(SHAPEFILE_REQUIRED),
            optional_files=sorted(SHAPEFILE_OPTIONAL),
            crs_rule="Lido do arquivo .prj, obrigatório.",
            encoding=f"Lida do .cpg; sem ele, {SHAPEFILE_FALLBACK_ENCODING} (com aviso).",
            rules=[
                "Exatamente um .shp por ZIP, com os demais arquivos de mesmo nome.",
                "Sem outros datasets, links simbólicos ou arquivos protegidos por senha.",
                "Nomes de coluna do DBF têm no máximo 10 caracteres.",
            ],
        ),
        FormatView(
            id=GeospatialFormat.GEOJSON.value,
            label="GeoJSON",
            extensions=sorted(FORMAT_EXTENSIONS[GeospatialFormat.GEOJSON]),
            crs_rule="Membro `crs` do arquivo ou `metadata.source_crs`; um dos dois é obrigatório.",
            encoding="UTF-8.",
            rules=["Raiz Feature ou FeatureCollection.", "Atributos em `properties`."],
        ),
        FormatView(
            id=GeospatialFormat.KML.value,
            label="KML",
            extensions=sorted(FORMAT_EXTENSIONS[GeospatialFormat.KML]),
            crs_rule=f"Sempre WGS 84 (EPSG:{KML_EPSG}), conforme o padrão do formato.",
            encoding="UTF-8.",
            rules=[
                "Placemarks com Point, Polygon ou MultiGeometry.",
                "`name` e os campos de `ExtendedData` viram atributos.",
            ],
        ),
    ]
    limits = [
        LimitView(key="max_upload_bytes", label="Tamanho do arquivo", value=settings.max_upload_bytes / 1024**2, unit="MB"),
        LimitView(key="max_features", label="Feições por arquivo", value=settings.max_features, unit="feições"),
        LimitView(key="max_vertices", label="Vértices por arquivo", value=settings.max_vertices, unit="vértices"),
        LimitView(key="max_zip_entries", label="Arquivos dentro do ZIP", value=settings.max_zip_entries, unit="arquivos"),
        LimitView(
            key="max_zip_uncompressed_bytes", label="ZIP descompactado",
            value=settings.max_zip_uncompressed_bytes / 1024**2, unit="MB",
        ),
        LimitView(
            key="max_zip_compression_ratio", label="Razão de compressão por arquivo",
            value=settings.max_zip_compression_ratio, unit=":1",
        ),
    ]
    return DataDictionary(
        version=DICTIONARY_VERSION,
        formats=formats,
        limits=limits,
        crs=CrsView(
            canonical=f"EPSG:{settings.canonical_epsg}",
            metric=f"EPSG:{settings.metric_epsg}",
            accepted="Qualquer CRS que o PROJ consiga transformar; os dados são convertidos para o CRS canônico.",
            dimension="2D: coordenadas Z e M são descartadas.",
        ),
        territory=TerritoryView(
            rule=settings.territorial_rule,
            bbox=list(settings.sc_bbox),
            description="Cada geometria precisa intersectar o envelope de Santa Catarina (longitude/latitude).",
        ),
        geometry_repair=(
            "Geometrias inválidas são recusadas. Com `repair_geometry=true`, a API tenta corrigir e aceita se a "
            f"área variar no máximo {settings.max_repair_area_delta_ratio:.0%}."
        ),
        bases=_bases(),
        maintained_bases=_MAINTAINED_BASES,
        errors=[ErrorView(code=code, stage=stage, meaning=meaning, fix=fix) for code, stage, meaning, fix in _ERRORS],
    )
