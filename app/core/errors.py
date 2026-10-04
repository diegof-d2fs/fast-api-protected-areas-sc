from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppError(Exception):
    status_code: int
    error_code: str
    title: str
    detail: str
    violations: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.detail


def bad_request(error_code: str, detail: str, *, violations: list[dict[str, Any]] | None = None) -> AppError:
    return AppError(400, error_code, "Requisição inválida", detail, violations or [])


def conflict(error_code: str, detail: str) -> AppError:
    return AppError(409, error_code, "Conflito", detail)


def not_found(resource: str, identifier: str) -> AppError:
    return AppError(
        404, "RESOURCE_NOT_FOUND", "Recurso não encontrado", f"{resource} '{identifier}' não foi encontrado."
    )


def payload_too_large(limit: int) -> AppError:
    return AppError(
        413, "UPLOAD_TOO_LARGE", "Arquivo muito grande", f"O upload excede o limite de {limit} bytes."
    )


def unsupported_media(detail: str) -> AppError:
    return AppError(415, "UNSUPPORTED_GEOSPATIAL_FORMAT", "Formato não suportado", detail)
