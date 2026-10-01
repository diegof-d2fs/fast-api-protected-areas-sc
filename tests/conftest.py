from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS

from app.application.auth import AuthService
from app.core.config import Settings
from app.core.security import hash_password
from app.domain.auth import UserRole
from app.main import create_app
from tests.fakes import FakeAuthRepository

# Identidade padrão usada pelo fixture `client` para autenticar toda chamada a
# `/api/v1/imports`/`/api/v1/ucs`, protegidas desde que o módulo `auth` existe. Um teste que
# precise verificar o comportamento sem sessão chama `client.cookies.clear()` antes da requisição.
DEFAULT_TEST_USERNAME = "operador-teste"
DEFAULT_TEST_PASSWORD = "senha-forte-123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        max_upload_bytes=2 * 1024 * 1024,
        max_zip_uncompressed_bytes=4 * 1024 * 1024,
        max_features=100,
        max_vertices=10_000,
        # Sem TLS no TestClient (`http://testserver`): um cookie `Secure` seria armazenado mas
        # nunca reenviado pelo httpx, ao contrário de um navegador real em http://localhost.
        session_cookie_secure=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        auth_repository = FakeAuthRepository()
        auth_repository.create_user(
            username=DEFAULT_TEST_USERNAME,
            nome="Operador de Teste",
            password_hash=hash_password(DEFAULT_TEST_PASSWORD),
            role=UserRole.OPERATOR,
            criado_por=None,
        )
        test_client.app.state.auth_service = AuthService(auth_repository)
        login = test_client.post(
            "/api/v1/auth/login",
            json={"username": DEFAULT_TEST_USERNAME, "password": DEFAULT_TEST_PASSWORD},
        )
        assert login.status_code == 200, login.text
        yield test_client


@pytest.fixture
def valid_uc_geojson() -> bytes:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {"nm_uc": "UC de teste"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-49.2, -27.2],
                                [-49.0, -27.2],
                                [-49.0, -27.0],
                                [-49.2, -27.0],
                                [-49.2, -27.2],
                            ]
                        ],
                    },
                }
            ],
        }
    ).encode("utf-8")


@pytest.fixture
def valid_uc_point_geojson() -> bytes:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {"nm_uc": "UC pontual de teste"},
                    "geometry": {"type": "Point", "coordinates": [-49.1, -27.1]},
                }
            ],
        }
    ).encode("utf-8")


@pytest.fixture
def valid_kml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<name>UC KML</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
-49.2,-27.2,0 -49.0,-27.2,0 -49.0,-27.0,0 -49.2,-27.0,0 -49.2,-27.2,0
</coordinates></LinearRing></outerBoundaryIs></Polygon>
</Placemark></Document></kml>"""


@pytest.fixture
def valid_shapefile_zip(tmp_path: Path) -> bytes:
    base = tmp_path / "uc_fixture"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("nm_uc", "C", size=80)
    writer.poly(
        [
            [
                [-49.2, -27.2],
                [-49.2, -27.0],
                [-49.0, -27.0],
                [-49.0, -27.2],
                [-49.2, -27.2],
            ]
        ]
    )
    writer.record("UC Shapefile")
    writer.close()
    base.with_suffix(".prj").write_text(CRS.from_epsg(4674).to_wkt(), encoding="utf-8")
    base.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    base.with_suffix(".qmd").write_text("<qgis><identifier>fixture</identifier></qgis>", encoding="utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd"):
            archive.write(base.with_suffix(suffix), arcname=f"dataset/uc_fixture{suffix}")
    return output.getvalue()


def uc_metadata(**extra: object) -> str:
    return json.dumps({"source": "fixture automatizada", "name": "UC de teste", **extra})


def za_metadata(**extra: object) -> str:
    return json.dumps({"source": "fixture automatizada", "uc_identifier": "UC-001", **extra})
