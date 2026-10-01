from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operador"


class UsernameAlreadyExistsError(Exception):
    """Raised by a repository when a username violates the unique constraint."""

    def __init__(self, username: str):
        self.username = username
        super().__init__(f"username '{username}' already exists")


@dataclass(slots=True)
class UserRecord:
    """Internal representation of an `app_user` row, including the password hash.

    Never return this type from an API route; convert to `UserView` or `CurrentUser` first,
    which are the only allowlisted shapes a response may serialize.
    """

    id: int
    username: str
    nome: str
    password_hash: str
    role: UserRole
    ativo: bool
    criado_em: datetime


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=255)


class CurrentUser(BaseModel):
    id: int = Field(description="Identificador interno do usuário autenticado.")
    nome: str = Field(description="Nome de exibição do usuário.")
    role: UserRole = Field(description="Papel usado para autorização.")


class UserView(BaseModel):
    """Formato público de um usuário: allowlist explícita, nunca inclui `password_hash`."""

    id: int
    username: str
    nome: str
    role: UserRole
    ativo: bool
    criado_em: datetime

    @classmethod
    def from_record(cls, record: UserRecord) -> UserView:
        return cls(
            id=record.id,
            username=record.username,
            nome=record.nome,
            role=record.role,
            ativo=record.ativo,
            criado_em=record.criado_em,
        )


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    nome: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=255)
    role: UserRole


class UpdateUserActiveRequest(BaseModel):
    ativo: bool
