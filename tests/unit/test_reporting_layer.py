"""A camada `reporting` é a única superfície lida por GeoServer, QGIS e Power BI.

Estes testes impedem que uma view nova ou alterada passe a expor identidade de operador ou
identificadores de correlação interna do pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.infrastructure.persistence.migrate import provision_reporting_credentials

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "005_reporting_readonly_layer.sql"
_VIEW = re.compile(
    r"CREATE OR REPLACE VIEW reporting\.(\w+) AS(.*?);", re.DOTALL | re.IGNORECASE
)
AUDIT_COLUMNS = {"ator", "motivo", "correlation_id", "import_id", "dag_run_id", "criado_por", "run_id"}


def _views() -> dict[str, str]:
    return {name: body for name, body in _VIEW.findall(MIGRATION.read_text(encoding="utf-8"))}


def test_reporting_publishes_expected_views() -> None:
    assert set(_views()) == {
        "uc",
        "za_oficial",
        "buffer_abrangencia",
        "prodes_clip",
        "mapbiomas_alerta_clip",
        "firms_clip",
        "mapbiomas_legend_class",
        "mapbiomas_clip",
        "mapbiomas_raster_asset",
    }


@pytest.mark.parametrize("view", sorted(_views()))
def test_reporting_view_never_selects_audit_columns(view: str) -> None:
    selected = set(re.findall(r"\b\w+\b", _views()[view].split("FROM", 1)[0].lower()))
    assert selected & AUDIT_COLUMNS == set()


def test_reporting_views_never_use_select_star() -> None:
    assert all("*" not in body for body in _views().values())


def test_every_view_declares_primary_key_for_geoserver_paging() -> None:
    declared = dict(
        re.findall(r"\('reporting', '(\w+)', '(\w+)', 1, 'assigned'\)", MIGRATION.read_text(encoding="utf-8"))
    )
    views = _views()
    assert set(declared) == set(views)
    for view, key in declared.items():
        selected = re.findall(r"\b\w+\b", views[view].split("FROM", 1)[0])
        assert key in selected, f"{view} não seleciona a chave declarada {key}"


def test_provisioning_rejects_unknown_role_before_connecting() -> None:
    with pytest.raises(ValueError, match="desconhecidos"):
        provision_reporting_credentials("postgresql://unused", {"project": "x"})


def test_lab_login_is_read_only_member_of_reporting_with_its_own_limits() -> None:
    sql = (MIGRATION.parent / "007_reporting_lab_login.sql").read_text(encoding="utf-8")
    assert "CREATE ROLE lab_svc LOGIN PASSWORD NULL CONNECTION LIMIT 12 IN ROLE reporting_readonly" in sql
    assert "ALTER ROLE lab_svc SET default_transaction_read_only = on" in sql
    assert "ALTER ROLE lab_svc SET statement_timeout = '220s'" in sql
    assert "ALTER ROLE lab_svc SET search_path = reporting, public" in sql
