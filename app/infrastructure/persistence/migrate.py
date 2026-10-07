from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
from psycopg import sql

from app.core.config import get_settings

# Logins criados sem senha pelas migrações 005 e 007; o login fica bloqueado até esta etapa rodar.
REPORTING_LOGIN_ROLES = ("geoserver_svc", "powerbi_svc", "lab_svc")


def apply_migrations(dsn: str, migrations_dir: Path | None = None) -> list[str]:
    directory = migrations_dir or Path(__file__).resolve().parents[3] / "migrations"
    applied: list[str] = []
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.api_schema_migration (
                version VARCHAR(100) PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = connection.execute(
                "SELECT checksum FROM public.api_schema_migration WHERE version = %s",
                (path.name,),
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"Checksum divergente para migração já aplicada: {path.name}")
                continue
            with connection.transaction():
                connection.execute(sql, prepare=False)
                connection.execute(
                    "INSERT INTO public.api_schema_migration (version, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
            applied.append(path.name)
    return applied


def provision_reporting_credentials(dsn: str, passwords: dict[str, str | None]) -> list[str]:
    unknown = set(passwords) - set(REPORTING_LOGIN_ROLES)
    if unknown:
        raise ValueError(f"Papéis de leitura desconhecidos: {sorted(unknown)}")
    provisioned: list[str] = []
    with psycopg.connect(dsn) as connection:
        for role, password in passwords.items():
            if not password:
                continue
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            provisioned.append(role)
    return provisioned


def main() -> None:
    settings = get_settings()
    if not settings.database_dsn:
        raise SystemExit("PA_SC_DATABASE_DSN é obrigatória para executar migrações")
    for version in apply_migrations(settings.database_dsn):
        print(f"applied {version}")
    passwords = {
        "geoserver_svc": settings.reporting_geoserver_db_password,
        "powerbi_svc": settings.reporting_powerbi_db_password,
        "lab_svc": settings.reporting_lab_db_password,
    }
    provisioned = provision_reporting_credentials(settings.database_dsn, passwords)
    for role in REPORTING_LOGIN_ROLES:
        status = "senha aplicada" if role in provisioned else "sem senha no ambiente; login bloqueado"
        print(f"reporting {role}: {status}")


if __name__ == "__main__":
    main()
