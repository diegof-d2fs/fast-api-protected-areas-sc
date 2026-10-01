from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.domain.auth import UsernameAlreadyExistsError, UserRecord, UserRole

_USER_COLUMNS = "id, username, nome, password_hash, role, ativo, criado_em"


def _row_to_user(row: dict) -> UserRecord:
    return UserRecord(
        id=row["id"],
        username=row["username"],
        nome=row["nome"],
        password_hash=row["password_hash"],
        role=UserRole(row["role"]),
        ativo=row["ativo"],
        criado_em=row["criado_em"],
    )


class PostgresAuthRepository:
    """Owns every read/write against `app_user`, `app_session` and `login_attempt`."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def get_user_by_username(self, username: str) -> UserRecord | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM public.app_user WHERE username = %s", (username,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM public.app_user WHERE id = %s", (user_id,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def count_users(self) -> int:
        with psycopg.connect(self._dsn) as connection:
            row = connection.execute("SELECT COUNT(*) FROM public.app_user").fetchone()
        return row[0]

    def create_user(
        self, *, username: str, nome: str, password_hash: str, role: UserRole, criado_por: int | None
    ) -> UserRecord:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            try:
                with connection.transaction():
                    row = connection.execute(
                        f"""
                        INSERT INTO public.app_user (username, nome, password_hash, role, criado_por)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING {_USER_COLUMNS}
                        """,
                        (username, nome, password_hash, role.value, criado_por),
                    ).fetchone()
            except errors.UniqueViolation as exc:
                raise UsernameAlreadyExistsError(username) from exc
        return _row_to_user(row)

    def list_users(self) -> list[UserRecord]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM public.app_user ORDER BY criado_em, id"
            ).fetchall()
        return [_row_to_user(row) for row in rows]

    def set_user_active(self, user_id: int, ativo: bool) -> UserRecord | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                row = connection.execute(
                    f"""
                    UPDATE public.app_user
                    SET ativo = %s, atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING {_USER_COLUMNS}
                    """,
                    (ativo, user_id),
                ).fetchone()
                if row is not None and not ativo:
                    # Desativar revoga toda sessão viva na mesma transação: ninguém autenticado
                    # com a conta antiga sobrevive a uma desativação administrativa.
                    connection.execute(
                        """
                        UPDATE public.app_session
                        SET revogado_em = CURRENT_TIMESTAMP
                        WHERE user_id = %s AND revogado_em IS NULL
                        """,
                        (user_id,),
                    )
        return _row_to_user(row) if row else None

    def create_session(self, *, user_id: int, token_hash: str, expires_at: datetime) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO public.app_session (token_hash, user_id, expira_em) VALUES (%s, %s, %s)",
                    (token_hash, user_id, expires_at),
                )

    def get_active_session_user(self, token_hash: str, now: datetime) -> UserRecord | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username, u.nome, u.password_hash, u.role, u.ativo, u.criado_em
                FROM public.app_session s
                JOIN public.app_user u ON u.id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revogado_em IS NULL
                  AND s.expira_em > %s
                  AND u.ativo = TRUE
                """,
                (token_hash, now),
            ).fetchone()
        return _row_to_user(row) if row else None

    def revoke_session(self, token_hash: str) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE public.app_session
                    SET revogado_em = CURRENT_TIMESTAMP
                    WHERE token_hash = %s AND revogado_em IS NULL
                    """,
                    (token_hash,),
                )

    def record_login_attempt(self, *, username: str, sucesso: bool, ip_origem: str | None) -> None:
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO public.login_attempt (username, sucesso, ip_origem) VALUES (%s, %s, %s)",
                    (username, sucesso, ip_origem),
                )

    def count_recent_failed_attempts(self, *, username: str, since: datetime) -> int:
        with psycopg.connect(self._dsn) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM public.login_attempt
                WHERE username = %s AND sucesso = FALSE AND criado_em >= %s
                """,
                (username, since),
            ).fetchone()
        return row[0]
