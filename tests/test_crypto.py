"""Encryption at rest + key rotation (ENCRYPTION_KEY = newest-first key list)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app import crypto
from tests.test_config import settings


def use_keys(monkeypatch, keys: str) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: settings(encryption_key=keys))


def test_rotation_keeps_old_rows_readable(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    use_keys(monkeypatch, old)
    written_before_rotation = crypto.fernet().encrypt(b"payout body")

    use_keys(monkeypatch, f"{new},{old}")  # rotation: prepend, redeploy
    assert crypto.fernet().decrypt(written_before_rotation) == b"payout body"
    written_after_rotation = crypto.fernet().encrypt(b"payout body")

    # New writes use the new key: the retired key alone can no longer read them.
    use_keys(monkeypatch, old)
    with pytest.raises(InvalidToken):
        crypto.fernet().decrypt(written_after_rotation)


def test_invalid_key_is_refused_at_startup():
    with pytest.raises(RuntimeError, match="entry #2 is not a valid Fernet key"):
        settings(encryption_key=f"{Fernet.generate_key().decode()},not-a-fernet-key")


def test_key_material_never_appears_in_the_error():
    key = Fernet.generate_key().decode()
    with pytest.raises(RuntimeError) as exc:
        settings(encryption_key=f"{key},short")
    assert key not in str(exc.value)
