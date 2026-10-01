from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UCStatus(StrEnum):
    ACTIVE = "ATIVA"
    EXTINCT = "EXTINTA"


class UCSearchView(BaseModel):
    id: int
    name: str
    status: UCStatus
    geometry_version: int
    official_identifier: str | None = None
    cnuc_code: str | None = None
    wdpa_pid: str | None = None
    links: dict[str, str]


class UCExtinguishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=1000)


class UCDetailView(BaseModel):
    id: int
    name: str
    status: UCStatus
    geometry: dict[str, Any]
    geometry_type: str
    geometry_version: int
    official_identifier: str | None = None
    cnuc_code: str | None = None
    wdpa_pid: str | None = None
    creation_date: date | None = None
    legal_act: str | None = None
    group: str | None = None
    category: str | None = None
    administrative_sphere: str | None = None
    managing_agency: str | None = None
    calculated_area_ha: float | None = None
    legal_area_ha: float | None = None
    valid_from: date
    valid_until: date | None = None
    updated_at: datetime
    links: dict[str, str]


class BufferAbrangenciaView(BaseModel):
    id: int
    uc_id: int
    source: str | None = None
    generated_at: datetime
    buffer_distance_m: float
    active: bool
    geometry: dict[str, Any]
    links: dict[str, str]
