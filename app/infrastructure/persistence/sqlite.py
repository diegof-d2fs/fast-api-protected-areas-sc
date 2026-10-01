from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.errors import conflict, not_found
from app.domain.models import Submission


class SqliteSubmissionRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS import_publication_claim "
                "(import_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS import_batch_member "
                "(import_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS submission (
                    submission_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_or_get(self, item: Submission) -> tuple[Submission, bool]:
        payload = item.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_fingerprint, payload_json FROM submission WHERE idempotency_key = ?",
                (item.idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_fingerprint"] != item.request_fingerprint:
                    raise conflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "A Idempotency-Key já foi utilizada com um payload diferente.",
                    )
                return Submission.model_validate_json(row["payload_json"]), False
            connection.execute(
                """
                INSERT INTO submission (
                    submission_id, idempotency_key, request_fingerprint,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.submission_id,
                    item.idempotency_key,
                    item.request_fingerprint,
                    payload,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )
        return item, True

    def get(self, import_id: str) -> Submission:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM submission WHERE submission_id = ?",
                (import_id,),
            ).fetchone()
        if row is None:
            raise not_found("Importação", import_id)
        return Submission.model_validate_json(row["payload_json"])

    def find_by_idempotency_key(self, key: str) -> Submission | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM submission WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Submission.model_validate_json(row["payload_json"]) if row else None

    def batch_for_member(self, import_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT batch_id FROM import_batch_member WHERE import_id = ?", (import_id,)
            ).fetchone()
        return row["batch_id"] if row else None

    def create_batch(self, item: Submission, members: list[str]) -> Submission:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_fingerprint, payload_json FROM submission WHERE idempotency_key = ?",
                (item.idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != item.request_fingerprint:
                    raise conflict("IDEMPOTENCY_KEY_REUSED", "A chave identifica outro lote.")
                return Submission.model_validate_json(existing["payload_json"])
            for member in members:
                if connection.execute(
                    "SELECT 1 FROM import_publication_claim WHERE import_id = ?", (member,)
                ).fetchone():
                    raise conflict("IMPORT_PUBLICATION_STARTED", "A publicação individual já foi iniciada.")
                if connection.execute(
                    "SELECT 1 FROM import_batch_member WHERE import_id = ?", (member,)
                ).fetchone():
                    raise conflict("IMPORT_ALREADY_GROUPED", "A importação já pertence a outro lote.")
                row = connection.execute(
                    "SELECT payload_json FROM submission WHERE submission_id = ?", (member,)
                ).fetchone()
                if row is None or Submission.model_validate_json(row["payload_json"]).status.value != "ACCEPTED":
                    raise conflict("IMPORT_NOT_ACCEPTED", "Os dois membros devem estar aceitos e não publicados.")
            connection.execute(
                "INSERT INTO submission VALUES (?, ?, ?, ?, ?, ?)",
                (item.submission_id, item.idempotency_key, item.request_fingerprint,
                 item.model_dump_json(), item.created_at.isoformat(), item.updated_at.isoformat()),
            )
            connection.executemany(
                "INSERT INTO import_batch_member VALUES (?, ?)",
                [(member, item.submission_id) for member in members],
            )
        return item

    def reserve_publication(self, import_id: str) -> None:
        # A persistent reservation also covers retries after a Bronze or Airflow failure.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM import_batch_member WHERE import_id = ?", (import_id,)
            ).fetchone():
                raise conflict("IMPORT_BELONGS_TO_BATCH", "Publique o lote ao qual esta importação pertence.")
            connection.execute(
                "INSERT OR IGNORE INTO import_publication_claim VALUES (?)", (import_id,)
            )

    def save(self, item: Submission) -> Submission:
        payload = item.model_dump_json()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE submission
                SET payload_json = ?, updated_at = ?
                WHERE submission_id = ?
                """,
                (payload, item.updated_at.isoformat(), item.submission_id),
            )
        if cursor.rowcount != 1:
            raise not_found("Importação", item.submission_id)
        return item

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection
