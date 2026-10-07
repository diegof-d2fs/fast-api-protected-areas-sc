"""Login fixo do laboratório (`lab_svc`, migração 007) contra um PostGIS real.

Roda só com `PA_SC_TEST_DATABASE_DSN` apontando para um banco descartável com o `init_db.sql` do
pipeline já aplicado, como `test_event_mode_repository.py`.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from app.infrastructure.persistence.migrate import apply_migrations, provision_reporting_credentials

DSN = os.environ.get("PA_SC_TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PA_SC_TEST_DATABASE_DSN não definido")


@pytest.fixture(scope="module")
def lab() -> psycopg.Connection:
    apply_migrations(DSN)
    password = secrets.token_urlsafe(24)
    assert provision_reporting_credentials(DSN, {"lab_svc": password}) == ["lab_svc"]
    info = conninfo_to_dict(DSN) | {"user": "lab_svc", "password": password}
    with psycopg.connect(**info, autocommit=True) as connection:
        yield connection


def test_lab_login_session_limits(lab: psycopg.Connection) -> None:
    assert lab.execute("SHOW statement_timeout").fetchone()[0] == "220s"
    assert lab.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
    limit = lab.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = 'lab_svc'").fetchone()[0]
    assert limit == 12


def test_lab_login_reads_reporting_only(lab: psycopg.Connection) -> None:
    lab.execute("SELECT count(*) FROM reporting.uc").fetchone()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        lab.execute("SELECT 1 FROM public.uc LIMIT 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        lab.execute("SELECT 1 FROM public.app_user LIMIT 1")


def test_lab_login_cannot_write_even_outside_read_only_transaction(lab: psycopg.Connection) -> None:
    lab.execute("SET default_transaction_read_only = off")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            lab.execute("DELETE FROM reporting.uc")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            lab.execute("CREATE TABLE reporting.lab_teste (id int)")
    finally:
        lab.execute("RESET default_transaction_read_only")
