"""Guards against a real bug found in 27/09/2026: a migration with its own BEGIN/COMMIT breaks
`app.infrastructure.persistence.migrate.apply_migrations`, because it already runs each file
inside `connection.transaction()`. A textual BEGIN/COMMIT inside the SQL desyncs psycopg3's
savepoint bookkeeping (`InvalidSavepointSpecification`) the moment the wrapping transaction tries
to exit. `CREATE ... ON COMMIT DROP` temp tables are unaffected: they still drop at the commit
the runner itself performs.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_TOP_LEVEL_TRANSACTION_CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", re.MULTILINE)


def test_no_migration_declares_its_own_transaction_boundary() -> None:
    offenders = {
        path.name: match.group(1)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if (match := _TOP_LEVEL_TRANSACTION_CONTROL.search(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"Migração(ões) com BEGIN/COMMIT/ROLLBACK próprio, o que quebra o runner: {offenders}"
    )
