"""Catálogo das colunas reconhecidas nos arquivos enviados e formato público do dicionário de dados.

Este módulo é o dono dos nomes de coluna aceitos. A API os usa para inferir nome e identidade das
UCs, e o pipeline mantém um teste de contrato contra o JSON exportado de
`GET /api/v1/data-dictionary`: uma divergência entre os dois repositórios quebra o CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

DICTIONARY_VERSION = "1"


class ColumnRequirement(StrEnum):
    REQUIRED = "obrigatorio"
    STRONG_IDENTITY = "identificador_forte"
    OPTIONAL = "opcional"


class ColumnType(StrEnum):
    TEXT = "texto"
    DATE = "data"
    DECIMAL = "numero_decimal"
    INTEGER = "inteiro"
    BOOLEAN = "booleano"


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type: ColumnType
    requirement: ColumnRequirement
    aliases: tuple[str, ...]
    example: str
    description: str
    destination: str


# Nomes procurados nos atributos do arquivo, na ordem de prioridade. A comparação ignora
# maiúsculas; no pipeline também ignora caracteres que não sejam letras e dígitos.
UC_NAME_FIELDS = ("nm_uc", "nome_uc", "nm_unid_con", "nome", "name")
UC_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "official_identifier": ("uc_id", "id_uc"),
    "cd_cnuc": ("cd_cnuc", "cod_cnuc", "cnuc"),
    "wdpa_pid": ("wdpa_pid", "wdpaid", "wdpa"),
}

UC_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        "nm_uc", ColumnType.TEXT, ColumnRequirement.REQUIRED, UC_NAME_FIELDS[1:],
        "Parque Estadual da Serra do Tabuleiro",
        "Nome oficial da UC. Pode vir do arquivo ou de `metadata.name`, que tem prioridade.",
        "uc.nm_uc",
    ),
    ColumnSpec(
        "cd_cnuc", ColumnType.TEXT, ColumnRequirement.STRONG_IDENTITY,
        UC_IDENTITY_FIELDS["cd_cnuc"][1:], "0000.42.0001",
        "Código da UC no Cadastro Nacional de Unidades de Conservação (CNUC/MMA), como publicado.",
        "uc.cd_cnuc",
    ),
    ColumnSpec(
        "wdpa_pid", ColumnType.TEXT, ColumnRequirement.STRONG_IDENTITY,
        UC_IDENTITY_FIELDS["wdpa_pid"][1:], "555555",
        "Identificador da área na World Database on Protected Areas (Protected Planet).",
        "uc.wdpa_pid",
    ),
    ColumnSpec(
        "uc_id", ColumnType.TEXT, ColumnRequirement.STRONG_IDENTITY, ("id_uc", "id", "gid"), "786",
        "Identificador oficial da UC na fonte. Só `uc_id` e `id_uc` contam como identificador forte; "
        "`id` e `gid` são lidos pelo pipeline, mas não identificam a UC na checagem de duplicidade.",
        "uc.uc_id",
    ),
    ColumnSpec(
        "dt_criacao", ColumnType.DATE, ColumnRequirement.OPTIONAL,
        ("cria_ano", "data_criaca", "data_criac", "dt_criacao_ano"), "1975-11-01",
        "Data de criação em AAAA-MM-DD, DD/MM/AAAA ou só o ano (AAAA). Sem ela, o pipeline tenta "
        "extrair a data do texto do ato legal.",
        "uc.dt_criacao",
    ),
    ColumnSpec(
        "ds_ato_legal", ColumnType.TEXT, ColumnRequirement.OPTIONAL,
        ("cria_ato", "ato_legal", "instrumento"), "Decreto Estadual nº 1.260, de 01/11/1975",
        "Ato legal de criação. Uma coluna `outro_ato` é anexada ao texto quando existir.",
        "uc.ds_ato_legal",
    ),
    ColumnSpec(
        "ds_grupo", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("grupo",), "Proteção Integral",
        "Grupo do SNUC: Proteção Integral ou Uso Sustentável.", "uc.ds_grupo",
    ),
    ColumnSpec(
        "ds_categoria", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("categoria", "cat_manejo"),
        "Parque", "Categoria de manejo do SNUC (Parque, Reserva Biológica, APA…).", "uc.ds_categoria",
    ),
    ColumnSpec(
        "ds_esfera", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("esfera",), "Estadual",
        "Esfera administrativa: Federal, Estadual ou Municipal.", "uc.ds_esfera",
    ),
    ColumnSpec(
        "nm_orgao_gestor", ColumnType.TEXT, ColumnRequirement.OPTIONAL,
        ("org_gestor", "orgao_gest", "gestor", "orgao"), "IMA/SC", "Órgão responsável pela gestão.",
        "uc.nm_orgao_gestor",
    ),
    ColumnSpec(
        "sg_uf", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("uf",), "SC",
        "Sigla da UF com duas letras; o nome por extenso também é aceito. Padrão: SC.", "uc.sg_uf",
    ),
    ColumnSpec(
        "area_total_ha", ColumnType.DECIMAL, ColumnRequirement.OPTIONAL,
        ("ha_total", "area_ha", "area_total"), "87405.00",
        "Área total declarada pela fonte, em hectares, com ponto decimal.", "uc.area_total_ha",
    ),
    ColumnSpec(
        "area_ato_ha", ColumnType.DECIMAL, ColumnRequirement.OPTIONAL, ("ha_ato", "area_ato"), "87405.00",
        "Área informada no ato legal, em hectares.", "uc.area_ato_ha",
    ),
    ColumnSpec(
        "update_geom", ColumnType.BOOLEAN, ColumnRequirement.OPTIONAL, ("update_geo", "updategeometry"),
        "sim", "Indica se a fonte marcou o limite como atualizado (sim/não, true/false, 1/0).",
        "uc.update_geom",
    ),
)

OFFICIAL_ZONE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        "nm_uc", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("nome_uc", "nome", "label"),
        "Parque Estadual da Serra do Tabuleiro",
        "Nome da UC dona da zona, só para conferência. O vínculo vem de `metadata.uc_identifier`.",
        "za_oficial.nm_uc_source",
    ),
    ColumnSpec(
        "cd_cnuc", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("cod_cnuc",), "0000.42.0001",
        "Código CNUC da UC dona da zona, para conferência.", "za_oficial.cd_cnuc_source",
    ),
    ColumnSpec(
        "uc_id", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("id_uc",), "786",
        "Identificador da UC na fonte da zona.", "za_oficial.uc_id_source",
    ),
    ColumnSpec(
        "zam_uco_cd", ColumnType.TEXT, ColumnRequirement.OPTIONAL, (), "1234",
        "Código da UC usado pela fonte das zonas de amortecimento.", "za_oficial.source_uc_code",
    ),
    ColumnSpec(
        "id_za_ofic", ColumnType.INTEGER, ColumnRequirement.OPTIONAL, ("id",), "17",
        "Identificador numérico da zona na fonte.", "za_oficial.id_za_oficial_source",
    ),
    ColumnSpec(
        "ds_fonte", ColumnType.TEXT, ColumnRequirement.OPTIONAL, ("obs",), "Plano de Manejo, 2018",
        "Fonte ou observação sobre a delimitação.", "za_oficial.ds_fonte",
    ),
    ColumnSpec(
        "update_geom", ColumnType.BOOLEAN, ColumnRequirement.OPTIONAL, ("update_geo",), "sim",
        "Indica se a fonte marcou o limite como atualizado.", "za_oficial.update_geom",
    ),
)


class ColumnView(BaseModel):
    name: str = Field(description="Nome canônico da coluna.")
    type: ColumnType
    requirement: ColumnRequirement
    aliases: list[str] = Field(description="Outros nomes aceitos para a mesma coluna.")
    example: str
    description: str
    destination: str = Field(description="Tabela e coluna onde o valor é gravado.")


class MetadataFieldView(BaseModel):
    name: str
    type: str
    required: bool
    max_length: int | None = None
    minimum: int | None = None
    description: str


class FormatView(BaseModel):
    id: str
    label: str
    extensions: list[str]
    required_files: list[str] = Field(default_factory=list)
    optional_files: list[str] = Field(default_factory=list)
    crs_rule: str
    encoding: str
    rules: list[str]


class LimitView(BaseModel):
    key: str
    label: str
    value: float
    unit: str


class ErrorView(BaseModel):
    code: str
    stage: str = Field(description="Onde a regra é aplicada: arquivo, conteúdo, geometria, metadados ou lote.")
    meaning: str
    fix: str


class UploadBaseView(BaseModel):
    id: str
    title: str
    summary: str
    endpoint: str
    domain: str | None
    operation: str | None
    geometry_types: list[str]
    feature_rule: str
    columns: list[ColumnView]
    metadata_fields: list[MetadataFieldView]
    metadata_example: dict[str, Any]
    rules: list[str]
    example_geojson: dict[str, Any] | None = Field(
        description="Arquivo mínimo válido para esta base; também serve de modelo para download."
    )


class MaintainedBaseView(BaseModel):
    id: str
    title: str
    source: str
    refresh: str
    description: str


class CrsView(BaseModel):
    canonical: str
    metric: str
    accepted: str
    dimension: str


class TerritoryView(BaseModel):
    rule: str
    bbox: list[float]
    description: str


class DataDictionary(BaseModel):
    version: str
    formats: list[FormatView]
    limits: list[LimitView]
    crs: CrsView
    territory: TerritoryView
    geometry_repair: str
    bases: list[UploadBaseView]
    maintained_bases: list[MaintainedBaseView]
    errors: list[ErrorView]


def column_views(columns: tuple[ColumnSpec, ...]) -> list[ColumnView]:
    return [
        ColumnView(
            name=column.name,
            type=column.type,
            requirement=column.requirement,
            aliases=list(column.aliases),
            example=column.example,
            description=column.description,
            destination=column.destination,
        )
        for column in columns
    ]
