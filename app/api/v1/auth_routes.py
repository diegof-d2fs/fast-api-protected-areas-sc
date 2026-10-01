from __future__ import annotations

import ipaddress
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status

from app.api.dependencies import get_auth_service, get_current_user, require_role
from app.application.auth import AuthService
from app.core.security import SESSION_COOKIE_NAME
from app.domain.auth import (
    CreateUserRequest,
    CurrentUser,
    LoginRequest,
    UpdateUserActiveRequest,
    UserRole,
    UserView,
)
from app.domain.models import ProblemDetail

router = APIRouter(prefix="/api/v1/auth", tags=["Autenticação"])


def _client_ip(request: Request) -> str | None:
    """Return a value the `INET` column accepts, discarding test-client/proxy placeholders.

    Behind a trusted proxy the socket peer is the proxy itself, so the viewer address comes from
    the configured header. CloudFront sends `<ip>:<porta>` (IPv6 without brackets).
    """
    header = getattr(request.app.state.settings, "client_ip_header", None)
    if header and (value := request.headers.get(header)):
        return _valid_ip(value.rsplit(":", 1)[0].strip("[]")) if ":" in value else _valid_ip(value)
    if request.client is None:
        return None
    return _valid_ip(request.client.host)


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


@router.post(
    "/login",
    response_model=CurrentUser,
    summary="Autenticar e abrir uma sessão",
    description=(
        "Verifica usuário e senha, aplica o bloqueio por força bruta (5 tentativas malsucedidas "
        "em 15 minutos) e, em caso de sucesso, define um cookie de sessão httpOnly."
    ),
    operation_id="login",
    responses={
        401: {"model": ProblemDetail, "description": "Credenciais inválidas ou conta inativa."},
        429: {"model": ProblemDetail, "description": "Bloqueado por excesso de tentativas."},
        503: {"model": ProblemDetail, "description": "Autenticação não configurada."},
    },
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> CurrentUser:
    result = service.login(
        username=payload.username, password=payload.password, ip_origem=_client_ip(request)
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.token,
        expires=result.expires_at,
        httponly=True,
        secure=request.app.state.settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return result.user


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Encerrar a sessão atual",
    operation_id="logout",
    responses={401: {"model": ProblemDetail, "description": "Sem sessão válida."}},
)
def logout(
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    _current: Annotated[CurrentUser, Depends(get_current_user)],
) -> Response:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        service.logout(token)
    # Reaproveitar o `Response` injetado (em vez de construir um novo) é o que faz o
    # `delete_cookie` abaixo realmente chegar ao cliente no cabeçalho da resposta.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="Consultar a sessão atual",
    operation_id="get_current_session_user",
    responses={401: {"model": ProblemDetail, "description": "Sem sessão válida."}},
)
def me(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    return current


@router.post(
    "/users",
    response_model=UserView,
    status_code=status.HTTP_201_CREATED,
    summary="Criar uma conta (somente administrador)",
    operation_id="create_user",
    responses={
        403: {"model": ProblemDetail, "description": "Sessão autenticada sem papel admin."},
        409: {"model": ProblemDetail, "description": "Username já cadastrado."},
    },
)
def create_user(
    payload: CreateUserRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    admin: Annotated[CurrentUser, Depends(require_role(UserRole.ADMIN))],
) -> UserView:
    return service.create_user(payload, criado_por=admin.id)


@router.get(
    "/users",
    response_model=list[UserView],
    summary="Listar contas (somente administrador)",
    operation_id="list_users",
    responses={403: {"model": ProblemDetail, "description": "Sessão autenticada sem papel admin."}},
)
def list_users(
    service: Annotated[AuthService, Depends(get_auth_service)],
    _admin: Annotated[CurrentUser, Depends(require_role(UserRole.ADMIN))],
) -> list[UserView]:
    return service.list_users()


@router.patch(
    "/users/{user_id}",
    response_model=UserView,
    summary="Ativar ou desativar uma conta (somente administrador)",
    description=(
        "Desativar uma conta revoga, na mesma transação, toda sessão viva daquele usuário."
    ),
    operation_id="update_user_active",
    responses={
        403: {"model": ProblemDetail, "description": "Sessão autenticada sem papel admin."},
        404: {"model": ProblemDetail, "description": "Usuário não encontrado."},
    },
)
def update_user_active(
    user_id: Annotated[int, Path(gt=0, description="Identificador do usuário.")],
    payload: UpdateUserActiveRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    _admin: Annotated[CurrentUser, Depends(require_role(UserRole.ADMIN))],
) -> UserView:
    return service.set_user_active(user_id, payload.ativo)
