from __future__ import annotations

from fastapi import Depends, Request

from app.application.auth import AuthService
from app.application.submissions import SubmissionService
from app.application.ucs import UCService
from app.core.errors import AppError
from app.core.security import SESSION_COOKIE_NAME
from app.domain.auth import CurrentUser, UserRole


def get_submission_service(request: Request) -> SubmissionService:
    return request.app.state.submission_service


def get_uc_service(request: Request) -> UCService:
    service = getattr(request.app.state, "uc_service", None)
    if service is None:
        raise AppError(
            503,
            "CADASTRAL_DATABASE_NOT_CONFIGURED",
            "Banco cadastral indisponível",
            "Configure PA_SC_DATABASE_DSN para utilizar as rotas cadastrais.",
        )
    return service


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise AppError(
            503,
            "AUTH_DATABASE_NOT_CONFIGURED",
            "Autenticação indisponível",
            "Configure PA_SC_DATABASE_DSN para utilizar as rotas de autenticação.",
        )
    return service


def get_current_user(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return service.get_current_user(token)


def require_role(role: UserRole):
    """Build a dependency that only accepts a session whose user has the given role."""

    def _dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role != role:
            raise AppError(
                403,
                "FORBIDDEN_ROLE",
                "Acesso negado",
                f"Esta operação exige o papel '{role.value}'.",
            )
        return user

    return _dependency
