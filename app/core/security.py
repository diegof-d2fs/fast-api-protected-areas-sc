"""Password hashing and opaque session tokens for the authentication module.

Sessions are opaque, not JWT: a random token is handed to the client and only its SHA-256
hash is ever persisted. Revoking access is a single row update, with no JWT statelessness
trade-off to manage while the user base stays small (see SDD, secao 2.1).
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

SESSION_COOKIE_NAME = "pa_sc_session"
SESSION_TOKEN_BYTES = 32

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def generate_session_token() -> str:
    return secrets.token_hex(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
