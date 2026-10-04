from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import app.infrastructure.geospatial.validator as validator_module
from app.application.data_dictionary import build_data_dictionary
from app.core.config import Settings
from app.domain.data_dictionary import UC_COLUMNS, UC_IDENTITY_FIELDS, UC_NAME_FIELDS, ColumnRequirement
from app.domain.models import SubmissionDomain
from app.infrastructure.geospatial.validator import GeospatialValidator


@pytest.fixture
def dictionary():
    return build_data_dictionary(Settings())


def test_every_validator_code_is_documented(dictionary) -> None:
    source = Path(validator_module.__file__).read_text(encoding="utf-8")
    raised = set(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', source))
    documented = {error.code for error in dictionary.errors}
    assert raised - documented == set()


def test_error_codes_are_unique(dictionary) -> None:
    codes = [error.code for error in dictionary.errors]
    assert len(codes) == len(set(codes))


def test_limits_come_from_settings() -> None:
    settings = Settings(max_upload_bytes=8 * 1024**2, max_features=42, max_vertices=999)
    limits = {limit.key: limit.value for limit in build_data_dictionary(settings).limits}
    assert limits["max_upload_bytes"] == 8
    assert limits["max_features"] == 42
    assert limits["max_vertices"] == 999


def test_name_and_identity_aliases_match_what_the_api_reads() -> None:
    columns = {column.name: column for column in UC_COLUMNS}
    assert ("nm_uc", *columns["nm_uc"].aliases) == UC_NAME_FIELDS
    assert ("cd_cnuc", *columns["cd_cnuc"].aliases) == UC_IDENTITY_FIELDS["cd_cnuc"]
    assert ("wdpa_pid", *columns["wdpa_pid"].aliases) == UC_IDENTITY_FIELDS["wdpa_pid"]
    assert set(UC_IDENTITY_FIELDS["official_identifier"]) <= {"uc_id", *columns["uc_id"].aliases}
    assert {
        name for name, column in columns.items() if column.requirement is ColumnRequirement.STRONG_IDENTITY
    } == {"uc_id", "cd_cnuc", "wdpa_pid"}


def test_every_example_file_passes_validation(dictionary, tmp_path: Path) -> None:
    validator = GeospatialValidator(Settings())
    examples = [base for base in dictionary.bases if base.example_geojson is not None]
    assert examples
    for base in examples:
        domain = SubmissionDomain.UC if base.domain == "uc" else SubmissionDomain.OFFICIAL_ZONE
        path = tmp_path / f"{base.id}.geojson"
        path.write_text(json.dumps(base.example_geojson), encoding="utf-8")
        outcome = validator.validate(
            path, original_filename=path.name, domain=domain, metadata={}, repair_geometry=False
        )
        assert outcome.report.valid, (base.id, outcome.report.errors)
        assert set(outcome.report.geometry_types) <= set(base.geometry_types) | {"MultiPolygon"}


def test_required_metadata_follows_the_operation_rules(dictionary) -> None:
    required = {
        base.id: {field.name for field in base.metadata_fields if field.required} for base in dictionary.bases
    }
    assert required["uc-create"] == {"source"}
    assert required["uc-update"] == {"source", "reason", "expected_version"}
    assert required["za-replace-buffer"] == {"source", "uc_identifier", "reason"}


def test_shapefile_format_lists_the_validator_sidecars(dictionary) -> None:
    shapefile = next(item for item in dictionary.formats if item.id == "shapefile_zip")
    assert shapefile.required_files == [".dbf", ".prj", ".shp", ".shx"]
    assert ".cpg" in shapefile.optional_files
