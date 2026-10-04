from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request, Response

from app.application.data_dictionary import build_data_dictionary
from app.core.config import get_settings
from app.domain.data_dictionary import DataDictionary

router = APIRouter(prefix="/api/v1/data-dictionary", tags=["Dicionário de dados"])

_CACHE_CONTROL = "public, max-age=3600"


@router.get(
    "",
    response_model=DataDictionary,
    summary="Consultar o dicionário de dados de envio",
    description=(
        "Contrato mínimo de cada base que pode ser enviada: formatos, colunas com tipo, "
        "obrigatoriedade e nomes alternativos aceitos, metadados, limites, regras e erros comuns. "
        "É montado a partir das mesmas regras que a validação aplica. Rota pública: não expõe dados, "
        "só o contrato. Responde `304` quando `If-None-Match` coincide com o `ETag`."
    ),
    operation_id="get_data_dictionary",
    responses={304: {"description": "O dicionário não mudou desde o `ETag` informado."}},
)
def get_data_dictionary(request: Request) -> Response:
    cached = getattr(request.app.state, "data_dictionary", None)
    if cached is None:
        settings = getattr(request.app.state, "settings", None) or get_settings()
        body = build_data_dictionary(settings).model_dump_json().encode("utf-8")
        cached = (body, f'"{hashlib.sha256(body).hexdigest()[:32]}"')
        request.app.state.data_dictionary = cached
    body, etag = cached
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    if etag in {value.strip() for value in request.headers.get("if-none-match", "").split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
