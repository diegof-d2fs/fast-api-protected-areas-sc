from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.errors import AppError
from app.domain.ucs import BufferAbrangenciaView, UCDetailView, UCSearchView


class UCRepository(Protocol):
    def get(self, uc_id: int) -> UCDetailView | None: ...

    def find_by_identifier(
        self, *, cd_cnuc: str | None = None, wdpa_pid: str | None = None
    ) -> UCSearchView | None: ...

    def list_buffer_abrangencia(self, uc_id: int) -> list[BufferAbrangenciaView] | None: ...


@dataclass
class UCService:
    repository: UCRepository

    def search(
        self, *, cd_cnuc: str | None = None, wdpa_pid: str | None = None
    ) -> list[UCSearchView]:
        normalized_cnuc = cd_cnuc.strip() if cd_cnuc is not None else None
        normalized_wdpa = wdpa_pid.strip() if wdpa_pid is not None else None
        supplied = [value for value in (normalized_cnuc, normalized_wdpa) if value]
        if len(supplied) != 1:
            raise AppError(
                400,
                "INVALID_UC_SEARCH_FILTER",
                "Filtro de busca inválido",
                "Informe exatamente um filtro: cd_cnuc ou wdpa_pid.",
                violations=[
                    {
                        "field": "query",
                        "rule": "exactly_one_identifier",
                        "message": "Informe somente cd_cnuc ou wdpa_pid.",
                    }
                ],
            )
        item = self.repository.find_by_identifier(
            cd_cnuc=normalized_cnuc or None,
            wdpa_pid=normalized_wdpa or None,
        )
        return [] if item is None else [item]

    def get(self, uc_id: int) -> UCDetailView:
        item = self.repository.get(uc_id)
        if item is None:
            raise AppError(404, "UC_NOT_FOUND", "UC não encontrada", f"A UC '{uc_id}' não foi encontrada.")
        return item

    def list_buffer_abrangencia(self, uc_id: int) -> list[BufferAbrangenciaView]:
        items = self.repository.list_buffer_abrangencia(uc_id)
        if items is None:
            raise AppError(404, "UC_NOT_FOUND", "UC não encontrada", f"A UC '{uc_id}' não foi encontrada.")
        return items
