from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.domain.ucs import BufferAbrangenciaView, UCDetailView, UCSearchView, UCStatus


class PostgresUCRepository:
    """Consulta o cadastro produzido pelo pipeline; não executa mutações de UC, ZA ou Buffer de Abrangência."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def check_connection(self) -> None:
        with psycopg.connect(self._dsn) as connection:
            connection.execute("SELECT 1")

    def find_create_duplicates(
        self,
        *,
        uc_ids: list[str],
        cnuc_codes: list[str],
        wdpa_pids: list[str],
        geometries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        matches: dict[int, dict[str, Any]] = {}
        geometry_payloads: list[str | None] = [
            json.dumps(geometry, ensure_ascii=False) for geometry in geometries
        ] or [None]
        query = """
            SELECT id_uc, uc_id, cd_cnuc, wdpa_pid, nm_uc,
                   CASE
                       WHEN uc_id::text = ANY(%s::text[]) THEN 'uc_id'
                       WHEN cd_cnuc::text = ANY(%s::text[]) THEN 'cd_cnuc'
                       WHEN wdpa_pid::text = ANY(%s::text[]) THEN 'wdpa_pid'
                       ELSE 'geometry'
                   END AS matched_by
            FROM public.uc
            WHERE uc_id::text = ANY(%s::text[])
               OR cd_cnuc::text = ANY(%s::text[])
               OR wdpa_pid::text = ANY(%s::text[])
               OR (%s::text IS NOT NULL AND ST_Equals(
                   geom,
                   ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4674)
               ))
        """
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            for geometry_payload in geometry_payloads:
                rows = connection.execute(
                    query,
                    (
                        uc_ids,
                        cnuc_codes,
                        wdpa_pids,
                        uc_ids,
                        cnuc_codes,
                        wdpa_pids,
                        geometry_payload,
                        geometry_payload,
                    ),
                ).fetchall()
                for row in rows:
                    matches[int(row["id_uc"])] = {
                        "uc_id": row["id_uc"],
                        "official_identifier": row["uc_id"],
                        "cd_cnuc": row["cd_cnuc"],
                        "wdpa_pid": row["wdpa_pid"],
                        "name": row["nm_uc"],
                        "matched_by": row["matched_by"],
                    }
        return list(matches.values())

    def get(self, uc_id: int) -> UCDetailView | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT
                    id_uc, nm_uc, situacao, ST_AsGeoJSON(geom)::jsonb AS geometry,
                    versao_registro, uc_id, cd_cnuc,
                    wdpa_pid, dt_criacao, ds_ato_legal, ds_grupo, ds_categoria, ds_esfera,
                    nm_orgao_gestor, area_total_ha, area_ato_ha, dt_inicio_vigencia,
                    dt_fim_vigencia, atualizado_em
                FROM public.uc
                WHERE id_uc = %s
                """,
                (uc_id,),
            ).fetchone()
        if row is None:
            return None
        base = f"/api/v1/ucs/{row['id_uc']}"
        return UCDetailView(
            id=row["id_uc"],
            name=row["nm_uc"],
            status=UCStatus(row["situacao"]),
            geometry=row["geometry"],
            geometry_type=row["geometry"]["type"],
            geometry_version=row["versao_registro"],
            official_identifier=row["uc_id"],
            cnuc_code=row["cd_cnuc"],
            wdpa_pid=row["wdpa_pid"],
            creation_date=row["dt_criacao"],
            legal_act=row["ds_ato_legal"],
            group=row["ds_grupo"],
            category=row["ds_categoria"],
            administrative_sphere=row["ds_esfera"],
            managing_agency=row["nm_orgao_gestor"],
            calculated_area_ha=row["area_total_ha"],
            legal_area_ha=row["area_ato_ha"],
            valid_from=row["dt_inicio_vigencia"],
            valid_until=row["dt_fim_vigencia"],
            updated_at=row["atualizado_em"],
            links={"self": base, "buffer_abrangencia": f"{base}/buffer-abrangencia"},
        )

    def find_by_identifier(
        self, *, cd_cnuc: str | None = None, wdpa_pid: str | None = None
    ) -> UCSearchView | None:
        column = "cd_cnuc" if cd_cnuc is not None else "wdpa_pid"
        value = cd_cnuc if cd_cnuc is not None else wdpa_pid
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                f"""
                SELECT id_uc, nm_uc, situacao, versao_registro, uc_id, cd_cnuc, wdpa_pid
                FROM public.uc
                WHERE {column} = %s
                """,
                (value,),
            ).fetchone()
        if row is None:
            return None
        base = f"/api/v1/ucs/{row['id_uc']}"
        return UCSearchView(
            id=row["id_uc"],
            name=row["nm_uc"],
            status=UCStatus(row["situacao"]),
            geometry_version=row["versao_registro"],
            official_identifier=row["uc_id"],
            cnuc_code=row["cd_cnuc"],
            wdpa_pid=row["wdpa_pid"],
            links={"self": base},
        )

    def list_buffer_abrangencia(self, uc_id: int) -> list[BufferAbrangenciaView] | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            exists = connection.execute("SELECT 1 FROM public.uc WHERE id_uc = %s", (uc_id,)).fetchone()
            if exists is None:
                return None
            rows = connection.execute(
                """
                SELECT id_buffer_abrangencia, id_uc, ds_fonte, dt_geracao, dist_buffer_m, fl_ativa,
                       ST_AsGeoJSON(geom)::jsonb AS geometry
                FROM public.buffer_abrangencia
                WHERE id_uc = %s
                ORDER BY fl_ativa DESC, dt_geracao DESC, id_buffer_abrangencia DESC
                """,
                (uc_id,),
            ).fetchall()
        return [
            BufferAbrangenciaView(
                id=row["id_buffer_abrangencia"],
                uc_id=row["id_uc"],
                source=row["ds_fonte"],
                generated_at=row["dt_geracao"],
                buffer_distance_m=row["dist_buffer_m"],
                active=row["fl_ativa"],
                geometry=row["geometry"],
                links={"uc": f"/api/v1/ucs/{uc_id}"},
            )
            for row in rows
        ]
