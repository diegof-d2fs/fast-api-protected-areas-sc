from __future__ import annotations

from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)


def test_hash_password_round_trips_with_verify_password() -> None:
    password_hash = hash_password("senha-correta-123")
    assert verify_password("senha-correta-123", password_hash) is True


def test_verify_password_rejects_wrong_password() -> None:
    password_hash = hash_password("senha-correta-123")
    assert verify_password("senha-errada", password_hash) is False


def test_verify_password_rejects_malformed_hash_instead_of_raising() -> None:
    assert verify_password("qualquer-senha", "isto-nao-e-um-hash-argon2") is False


def test_hash_password_never_stores_the_plaintext() -> None:
    password_hash = hash_password("senha-correta-123")
    assert "senha-correta-123" not in password_hash


def test_generate_session_token_is_unique_and_long_enough() -> None:
    first = generate_session_token()
    second = generate_session_token()
    assert first != second
    assert len(first) >= 64  # 32 bytes em hexadecimal


def test_hash_session_token_is_deterministic_and_never_the_token_itself() -> None:
    token = generate_session_token()
    assert hash_session_token(token) == hash_session_token(token)
    assert hash_session_token(token) != token
