from __future__ import annotations

import json
import math
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree.ElementTree import ParseError

import shapefile
from defusedxml import ElementTree
from pyproj import CRS, Transformer
from shapely import force_2d, get_num_coordinates, make_valid
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Point,
    Polygon,
    box,
    mapping,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as transform_geometry

from app.core.config import Settings
from app.core.errors import unsupported_media
from app.domain.models import (
    GeospatialFormat,
    SubmissionDomain,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
)

# Regras de formato publicadas também pelo dicionário de dados (`app/application/data_dictionary.py`).
SHAPEFILE_REQUIRED = frozenset({".shp", ".shx", ".dbf", ".prj"})
SHAPEFILE_OPTIONAL = frozenset({".cpg", ".qix", ".qmd", ".sbn", ".sbx", ".xml"})
SHAPEFILE_ALLOWED = SHAPEFILE_REQUIRED | SHAPEFILE_OPTIONAL
SHAPEFILE_FALLBACK_ENCODING = "latin1"
KML_EPSG = 4326
FORMAT_EXTENSIONS = {
    GeospatialFormat.SHAPEFILE_ZIP: frozenset({".zip"}),
    GeospatialFormat.GEOJSON: frozenset({".geojson", ".json"}),
    GeospatialFormat.KML: frozenset({".kml"}),
}
ALLOWED_GEOMETRY_TYPES = {
    SubmissionDomain.UC: frozenset({"Point", "Polygon", "MultiPolygon"}),
    SubmissionDomain.OFFICIAL_ZONE: frozenset({"Polygon", "MultiPolygon"}),
}


class GeospatialContentError(Exception):
    def __init__(self, code: str, message: str, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


@dataclass(slots=True)
class GeospatialOutcome:
    report: ValidationReport
    canonical_geojson: dict[str, Any] | None


@dataclass(slots=True)
class _Dataset:
    geometries: list[BaseGeometry]
    properties: list[dict[str, Any]]
    source_crs: CRS
    warnings: list[ValidationIssue]


class GeospatialValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.target_crs = CRS.from_epsg(settings.canonical_epsg)
        self.metric_crs = CRS.from_epsg(settings.metric_epsg)
        self.metric_transformer = Transformer.from_crs(self.target_crs, self.metric_crs, always_xy=True)
        self.sc_bbox = box(*settings.sc_bbox)

    def detect_format(self, path: Path, original_filename: str) -> GeospatialFormat:
        extension = Path(original_filename).suffix.lower()
        with path.open("rb") as source:
            prefix = source.read(4096).lstrip()
        detected: GeospatialFormat | None = None
        if zipfile.is_zipfile(path):
            detected = GeospatialFormat.SHAPEFILE_ZIP
        elif prefix.startswith((b"{", b"[")):
            detected = GeospatialFormat.GEOJSON
        elif prefix.startswith(b"<") and (b"<kml" in prefix.lower() or b":kml" in prefix.lower()):
            detected = GeospatialFormat.KML

        if detected is None:
            raise unsupported_media("O conteúdo não foi reconhecido como Shapefile ZIP, GeoJSON ou KML.")
        if extension not in FORMAT_EXTENSIONS[detected]:
            expected = ", ".join(sorted(FORMAT_EXTENSIONS[detected]))
            raise unsupported_media(
                f"O conteúdo foi detectado como {detected.value}, mas a extensão '{extension or '(ausente)'}' "
                f"não corresponde. Extensões esperadas: {expected}."
            )
        return detected

    def validate(
        self,
        path: Path,
        *,
        original_filename: str,
        domain: SubmissionDomain,
        metadata: dict[str, Any],
        repair_geometry: bool,
    ) -> GeospatialOutcome:
        detected = self.detect_format(path, original_filename)
        try:
            if detected is GeospatialFormat.SHAPEFILE_ZIP:
                dataset = self._read_shapefile_zip(path)
            elif detected is GeospatialFormat.GEOJSON:
                dataset = self._read_geojson(path, metadata)
            else:
                dataset = self._read_kml(path)
        except GeospatialContentError as exc:
            return self._rejected(detected, exc)
        except (OSError, ValueError, zipfile.BadZipFile, shapefile.ShapefileException) as exc:
            return self._rejected(
                detected,
                GeospatialContentError("UNREADABLE_DATASET", f"Não foi possível ler o dataset: {exc}"),
            )

        return self._validate_dataset(dataset, detected, domain, repair_geometry)

    def _read_geojson(self, path: Path, metadata: dict[str, Any]) -> _Dataset:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GeospatialContentError("INVALID_GEOJSON", f"GeoJSON inválido: {exc}") from exc
        if not isinstance(payload, dict):
            raise GeospatialContentError("INVALID_GEOJSON_ROOT", "A raiz do GeoJSON deve ser um objeto.")

        payload_type = payload.get("type")
        if payload_type == "FeatureCollection":
            raw_features = payload.get("features")
            if not isinstance(raw_features, list):
                raise GeospatialContentError(
                    "INVALID_FEATURE_COLLECTION", "O campo features deve ser uma lista."
                )
        elif payload_type == "Feature":
            raw_features = [payload]
        else:
            raise GeospatialContentError(
                "UNSUPPORTED_GEOJSON_ROOT",
                "O GeoJSON deve ser uma Feature ou FeatureCollection.",
            )

        crs_value = metadata.get("source_crs") or self._geojson_crs(payload)
        if not crs_value:
            raise GeospatialContentError(
                "MISSING_CRS",
                "O CRS do GeoJSON deve ser informado em metadata.source_crs ou no membro crs do arquivo.",
                "Informe, por exemplo, metadata.source_crs como 'EPSG:4674'.",
            )
        source_crs = self._parse_crs(crs_value)
        geometries: list[BaseGeometry] = []
        properties: list[dict[str, Any]] = []
        for index, feature in enumerate(raw_features):
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise GeospatialContentError(
                    "INVALID_FEATURE", f"A feição {index} não é uma Feature GeoJSON válida."
                )
            raw_geometry = feature.get("geometry")
            if raw_geometry is None:
                geometries.append(GeometryCollection())
            else:
                try:
                    geometries.append(shape(raw_geometry))
                except (TypeError, ValueError) as exc:
                    raise GeospatialContentError(
                        "INVALID_GEOMETRY", f"Geometria inválida na feição {index}: {exc}"
                    ) from exc
            raw_properties = feature.get("properties")
            properties.append(raw_properties if isinstance(raw_properties, dict) else {})
        return _Dataset(geometries, properties, source_crs, [])

    def _read_shapefile_zip(self, path: Path) -> _Dataset:
        warnings: list[ValidationIssue] = []
        with zipfile.ZipFile(path) as archive:
            files = self._validate_zip(archive)
            shp_entry = next(info for info in files if Path(info.filename).suffix.lower() == ".shp")
            with tempfile.TemporaryDirectory(prefix="pa-sc-shapefile-") as temporary:
                temporary_root = Path(temporary).resolve()
                extracted: dict[str, Path] = {}
                for info in files:
                    relative = PurePosixPath(info.filename.replace("\\", "/"))
                    destination = temporary_root.joinpath(*relative.parts).resolve()
                    if temporary_root not in destination.parents:
                        raise GeospatialContentError("ZIP_SLIP", f"Entrada ZIP insegura: {info.filename}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    extracted[info.filename.lower()] = destination

                shp_path = extracted[shp_entry.filename.lower()]
                sidecars = {
                    candidate.suffix.lower(): candidate
                    for candidate in shp_path.parent.iterdir()
                    if candidate.is_file() and candidate.stem.lower() == shp_path.stem.lower()
                }
                prj_path = sidecars[".prj"]
                source_crs = self._parse_crs(prj_path.read_text(encoding="utf-8-sig", errors="strict"))
                cpg_path = sidecars.get(".cpg")
                encoding = "utf-8"
                if cpg_path is not None:
                    encoding = cpg_path.read_text(encoding="ascii", errors="ignore").strip() or "utf-8"
                else:
                    encoding = SHAPEFILE_FALLBACK_ENCODING
                    warnings.append(
                        self._issue(
                            "MISSING_CPG",
                            f"O Shapefile não possui .cpg; foi aplicada a codificação {encoding}.",
                            ValidationSeverity.WARNING,
                            suggestion="Inclua o arquivo .cpg no pacote.",
                        )
                    )
                try:
                    geometries: list[BaseGeometry] = []
                    properties: list[dict[str, Any]] = []
                    with shapefile.Reader(str(shp_path), encoding=encoding) as reader:
                        field_names = [field[0] for field in reader.fields[1:]]
                        for shape_record in reader.iterShapeRecords():
                            geometries.append(shape(shape_record.shape.__geo_interface__))
                            properties.append(
                                {
                                    key: self._json_value(value)
                                    for key, value in zip(field_names, list(shape_record.record), strict=True)
                                }
                            )
                except UnicodeDecodeError as exc:
                    raise GeospatialContentError(
                        "SHAPEFILE_ENCODING_ERROR",
                        f"Não foi possível decodificar o DBF usando '{encoding}'.",
                        "Inclua um .cpg correto no pacote.",
                    ) from exc
        return _Dataset(geometries, properties, source_crs, warnings)

    def _validate_zip(self, archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        entries = archive.infolist()
        if len(entries) > self.settings.max_zip_entries:
            raise GeospatialContentError(
                "ZIP_ENTRY_LIMIT", "O ZIP possui mais entradas que o limite configurado."
            )
        files: list[zipfile.ZipInfo] = []
        seen: set[str] = set()
        total_uncompressed = 0
        for info in entries:
            normalized_name = info.filename.replace("\\", "/")
            relative = PurePosixPath(normalized_name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise GeospatialContentError("ZIP_SLIP", f"Entrada ZIP insegura: {info.filename}")
            if info.is_dir():
                continue
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise GeospatialContentError(
                    "ZIP_LINK_NOT_ALLOWED", f"Links não são permitidos: {info.filename}"
                )
            if info.flag_bits & 0x1:
                raise GeospatialContentError(
                    "ENCRYPTED_ZIP_ENTRY",
                    f"Entradas ZIP criptografadas não são permitidas: {info.filename}",
                )
            lowered = normalized_name.lower()
            if lowered in seen:
                raise GeospatialContentError(
                    "DUPLICATE_ZIP_ENTRY", f"Entrada duplicada no ZIP: {info.filename}"
                )
            seen.add(lowered)
            extension = Path(normalized_name).suffix.lower()
            if extension not in SHAPEFILE_ALLOWED:
                raise GeospatialContentError(
                    "UNEXPECTED_ZIP_CONTENT", f"Extensão não permitida no ZIP: {info.filename}"
                )
            total_uncompressed += info.file_size
            if total_uncompressed > self.settings.max_zip_uncompressed_bytes:
                raise GeospatialContentError(
                    "ZIP_UNCOMPRESSED_LIMIT", "O conteúdo expandido do ZIP excede o limite configurado."
                )
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > self.settings.max_zip_compression_ratio:
                raise GeospatialContentError(
                    "ZIP_COMPRESSION_RATIO",
                    f"A entrada {info.filename} excede a razão de compressão permitida.",
                )
            files.append(info)

        shapefiles = [info for info in files if Path(info.filename).suffix.lower() == ".shp"]
        if len(shapefiles) != 1:
            raise GeospatialContentError(
                "SHAPEFILE_DATASET_COUNT", "O ZIP deve conter exatamente um arquivo .shp."
            )
        shp = PurePosixPath(shapefiles[0].filename.replace("\\", "/"))
        logical_files = {
            PurePosixPath(info.filename.replace("\\", "/"))
            for info in files
            if PurePosixPath(info.filename.replace("\\", "/")).parent == shp.parent
            and (
                PurePosixPath(info.filename.replace("\\", "/")).stem.lower() == shp.stem.lower()
                or PurePosixPath(info.filename.replace("\\", "/")).name.lower() == f"{shp.name.lower()}.xml"
            )
        }
        present = {item.suffix.lower() for item in logical_files}
        missing = sorted(SHAPEFILE_REQUIRED - present)
        if missing:
            raise GeospatialContentError(
                "MISSING_SHAPEFILE_SIDECARS",
                f"Faltam componentes obrigatórios do Shapefile: {', '.join(missing)}.",
            )
        unrelated = [
            info.filename
            for info in files
            if PurePosixPath(info.filename.replace("\\", "/")) not in logical_files
        ]
        if unrelated:
            raise GeospatialContentError(
                "MULTIPLE_OR_UNRELATED_DATASETS",
                f"O ZIP contém arquivos que não pertencem ao dataset único: {', '.join(unrelated)}.",
            )
        return files

    def _read_kml(self, path: Path) -> _Dataset:
        try:
            root = ElementTree.parse(path).getroot()
        except ParseError as exc:
            raise GeospatialContentError("INVALID_KML", f"KML inválido: {exc}") from exc
        if self._local_name(root.tag).lower() != "kml":
            raise GeospatialContentError("INVALID_KML_ROOT", "O elemento raiz do arquivo deve ser kml.")
        geometries: list[BaseGeometry] = []
        properties: list[dict[str, Any]] = []
        placemarks = [element for element in root.iter() if self._local_name(element.tag) == "Placemark"]
        for placemark in placemarks:
            geometry = self._kml_geometry(placemark)
            if geometry is None:
                continue
            item_properties: dict[str, Any] = {}
            for child in placemark:
                if self._local_name(child.tag) == "name" and child.text:
                    item_properties["name"] = child.text.strip()
                if self._local_name(child.tag) == "ExtendedData":
                    for data in child.iter():
                        if self._local_name(data.tag) == "Data" and data.attrib.get("name"):
                            value = next(
                                (nested.text for nested in data if self._local_name(nested.tag) == "value"),
                                None,
                            )
                            item_properties[data.attrib["name"]] = value
            geometries.append(geometry)
            properties.append(item_properties)
        if not geometries:
            raise GeospatialContentError(
                "EMPTY_KML", "O KML não contém Placemarks com geometrias suportadas."
            )
        warnings = [
            self._issue(
                "KML_CRS_DEFINED_BY_FORMAT",
                "As coordenadas KML foram interpretadas como WGS 84 (EPSG:4326), conforme o formato.",
                ValidationSeverity.WARNING,
            )
        ]
        return _Dataset(geometries, properties, CRS.from_epsg(KML_EPSG), warnings)

    def _kml_geometry(self, parent: Any) -> BaseGeometry | None:
        parent_kind = self._local_name(parent.tag)
        if parent_kind in {"Point", "Polygon", "MultiGeometry"}:
            element = parent
            kind = parent_kind
        else:
            geometry_children = [
                child
                for child in parent
                if self._local_name(child.tag) in {"Point", "Polygon", "MultiGeometry"}
            ]
            if geometry_children:
                element = geometry_children[0]
                kind = self._local_name(element.tag)
            else:
                element = None
                kind = ""
        if element is None:
            for child in parent:
                result = self._kml_geometry(child)
                if result is not None:
                    return result
            return None
        if kind == "Point":
            coordinates = self._coordinates_from_element(element)
            return Point(coordinates[0]) if coordinates else GeometryCollection()
        if kind == "Polygon":
            rings: list[list[tuple[float, float]]] = []
            for ring_element in element.iter():
                if self._local_name(ring_element.tag) == "LinearRing":
                    rings.append(self._coordinates_from_element(ring_element))
            return Polygon(rings[0], rings[1:]) if rings and rings[0] else GeometryCollection()
        parts = [self._kml_geometry(child) for child in element]
        parts = [part for part in parts if part is not None and not part.is_empty]
        if parts and all(isinstance(part, Polygon) for part in parts):
            return MultiPolygon(parts)
        return GeometryCollection(parts)

    def _coordinates_from_element(self, element: Any) -> list[tuple[float, float]]:
        coordinate_text = next(
            (child.text for child in element.iter() if self._local_name(child.tag) == "coordinates"),
            None,
        )
        if not coordinate_text:
            return []
        coordinates: list[tuple[float, float]] = []
        try:
            for token in coordinate_text.split():
                values = token.split(",")
                coordinates.append((float(values[0]), float(values[1])))
        except (IndexError, ValueError) as exc:
            raise GeospatialContentError(
                "INVALID_KML_COORDINATES", "O KML possui coordenadas inválidas."
            ) from exc
        return coordinates

    def _validate_dataset(
        self,
        dataset: _Dataset,
        detected: GeospatialFormat,
        domain: SubmissionDomain,
        repair_geometry: bool,
    ) -> GeospatialOutcome:
        errors: list[ValidationIssue] = []
        warnings = list(dataset.warnings)
        if not dataset.geometries:
            errors.append(
                self._issue("EMPTY_DATASET", "O dataset não possui feições.", ValidationSeverity.ERROR)
            )
        if len(dataset.geometries) > self.settings.max_features:
            errors.append(
                self._issue(
                    "FEATURE_LIMIT_EXCEEDED",
                    f"O dataset possui {len(dataset.geometries)} feições; o limite é {self.settings.max_features}.",
                    ValidationSeverity.ERROR,
                )
            )

        transformer = Transformer.from_crs(dataset.source_crs, self.target_crs, always_xy=True)
        canonical_geometries: list[BaseGeometry] = []
        repair_applied = False
        vertex_count = 0
        allowed_types = ALLOWED_GEOMETRY_TYPES[domain]

        for index, raw_geometry in enumerate(dataset.geometries):
            geometry = force_2d(raw_geometry)
            if dataset.source_crs != self.target_crs:
                geometry = transform_geometry(transformer.transform, geometry)
            if geometry.is_empty:
                errors.append(
                    self._issue(
                        "EMPTY_GEOMETRY", "A geometria está vazia.", ValidationSeverity.ERROR, feature=index
                    )
                )
                continue
            if geometry.geom_type not in allowed_types:
                errors.append(
                    self._issue(
                        "GEOMETRY_TYPE_NOT_ALLOWED",
                        f"O tipo {geometry.geom_type} não é permitido para o domínio {domain.value}.",
                        ValidationSeverity.ERROR,
                        feature=index,
                    )
                )
                continue
            if not geometry.is_valid:
                if not repair_geometry:
                    errors.append(
                        self._issue(
                            "INVALID_GEOMETRY",
                            "A geometria é topologicamente inválida.",
                            ValidationSeverity.ERROR,
                            feature=index,
                            suggestion="Corrija a origem ou reenvie com repair_geometry=true.",
                        )
                    )
                    continue
                repaired = make_valid(geometry)
                if repaired.is_empty or repaired.geom_type not in allowed_types:
                    errors.append(
                        self._issue(
                            "GEOMETRY_REPAIR_REJECTED",
                            f"A correção produziu uma geometria {repaired.geom_type} incompatível ou vazia.",
                            ValidationSeverity.ERROR,
                            feature=index,
                        )
                    )
                    continue
                delta = self._area_delta_ratio(geometry, repaired)
                if delta > self.settings.max_repair_area_delta_ratio:
                    errors.append(
                        self._issue(
                            "GEOMETRY_REPAIR_AREA_DELTA",
                            f"A correção alterou a área em {delta:.4%}, acima do limite configurado.",
                            ValidationSeverity.ERROR,
                            feature=index,
                        )
                    )
                    continue
                geometry = repaired
                repair_applied = True
                warnings.append(
                    self._issue(
                        "GEOMETRY_REPAIRED",
                        f"A geometria da feição {index} foi corrigida; variação relativa de área: {delta:.4%}.",
                        ValidationSeverity.WARNING,
                        feature=index,
                    )
                )
            if domain is SubmissionDomain.OFFICIAL_ZONE and isinstance(geometry, Polygon):
                geometry = MultiPolygon([geometry])
            if not geometry.intersects(self.sc_bbox):
                errors.append(
                    self._issue(
                        "OUTSIDE_SC_BBOX",
                        "A geometria não intersecta o envelope territorial configurado para Santa Catarina.",
                        ValidationSeverity.ERROR,
                        feature=index,
                    )
                )
                continue
            vertex_count += int(get_num_coordinates(geometry))
            canonical_geometries.append(geometry)

        if vertex_count > self.settings.max_vertices:
            errors.append(
                self._issue(
                    "VERTEX_LIMIT_EXCEEDED",
                    f"O dataset possui {vertex_count} vértices; o limite é {self.settings.max_vertices}.",
                    ValidationSeverity.ERROR,
                )
            )

        bounds: list[float] | None = None
        if canonical_geometries:
            merged_bounds = GeometryCollection(canonical_geometries).bounds
            if all(math.isfinite(value) for value in merged_bounds):
                bounds = [float(value) for value in merged_bounds]
        report = ValidationReport(
            valid=not errors and len(canonical_geometries) == len(dataset.geometries),
            detected_format=detected,
            source_crs=dataset.source_crs.to_string(),
            target_crs=self.target_crs.to_string(),
            geometry_types=sorted({geometry.geom_type for geometry in canonical_geometries}),
            feature_count=len(dataset.geometries),
            vertex_count=vertex_count,
            bbox=bounds,
            errors=errors,
            warnings=warnings,
            repair_applied=repair_applied,
        )
        if not report.valid:
            return GeospatialOutcome(report, None)
        features = [
            {
                "type": "Feature",
                "id": index,
                "properties": dataset.properties[index],
                "geometry": mapping(geometry),
            }
            for index, geometry in enumerate(canonical_geometries)
        ]
        canonical = {
            "type": "FeatureCollection",
            "name": "canonical",
            "crs": {"type": "name", "properties": {"name": self.target_crs.to_string()}},
            "features": features,
        }
        return GeospatialOutcome(report, canonical)

    def _rejected(self, detected: GeospatialFormat, error: GeospatialContentError) -> GeospatialOutcome:
        report = ValidationReport(
            valid=False,
            detected_format=detected,
            target_crs=self.target_crs.to_string(),
            errors=[
                self._issue(
                    error.code,
                    error.message,
                    ValidationSeverity.ERROR,
                    suggestion=error.suggestion,
                )
            ],
        )
        return GeospatialOutcome(report, None)

    @staticmethod
    def _geojson_crs(payload: dict[str, Any]) -> str | None:
        crs = payload.get("crs")
        if not isinstance(crs, dict):
            return None
        properties = crs.get("properties")
        if isinstance(properties, dict):
            name = properties.get("name")
            return str(name) if name else None
        return None

    @staticmethod
    def _parse_crs(value: Any) -> CRS:
        try:
            return CRS.from_user_input(value)
        except (TypeError, ValueError) as exc:
            raise GeospatialContentError(
                "INVALID_CRS", f"CRS inválido ou não transformável: {value!r}."
            ) from exc

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _area_delta_ratio(self, original: BaseGeometry, repaired: BaseGeometry) -> float:
        original_metric = transform_geometry(self.metric_transformer.transform, original)
        repaired_metric = transform_geometry(self.metric_transformer.transform, repaired)
        if original_metric.area == 0:
            return 0.0 if repaired_metric.area == 0 else 1.0
        return abs(repaired_metric.area - original_metric.area) / abs(original_metric.area)

    @staticmethod
    def _json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _issue(
        code: str,
        message: str,
        severity: ValidationSeverity,
        *,
        feature: int | None = None,
        suggestion: str | None = None,
    ) -> ValidationIssue:
        return ValidationIssue(
            code=code,
            message=message,
            severity=severity,
            feature=feature,
            suggestion=suggestion,
        )
