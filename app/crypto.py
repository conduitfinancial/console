"""Application-level encryption at rest (plan v2 §3): Fernet, key from env.

Used for `operations.request_body` and `drafts.payload`. A SQLAlchemy type so
call sites just assign dicts. ENCRYPTION_KEY holds one or more Fernet keys.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


def fernet() -> MultiFernet:
    """First key encrypts, all keys decrypt (MultiFernet) — key rotation is
    "prepend the new key and redeploy"; old rows stay readable until retention
    ages them out."""
    return MultiFernet([Fernet(k.strip()) for k in get_settings().encryption_keys])


def read_json(blob: object) -> dict | None:
    """One encrypted JSON column, decrypted **tolerantly** — or None when it
    cannot be read at all.

    The type below decrypts inside the query, so one corrupt or key-rotated row
    raises out of `session.execute` and takes a whole page down with it. Every
    list that must decrypt to render reads the raw `LargeBinary` and comes
    through here instead (`counterparties`, `batches`), so a bad row renders as
    *unreadable* beside its neighbours. The columns are NOT NULL wherever this is
    used, which is what makes None unambiguous: unreadable, never empty.
    """
    try:
        return json.loads(fernet().decrypt(bytes(blob)))  # type: ignore[arg-type]
    except (InvalidToken, ValueError, TypeError):
        return None


class EncryptedJSON(TypeDecorator):
    """JSON value, stored encrypted. NULL stays NULL (purged bodies)."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return fernet().encrypt(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(fernet().decrypt(bytes(value)))


class EncryptedBytes(TypeDecorator):
    """Raw bytes, stored encrypted — uploaded document files (`document_blobs`).

    Same key, same rotation story as `EncryptedJSON`; NULL stays NULL so a purged
    blob is indistinguishable from one that was never stored.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else fernet().encrypt(bytes(value))

    def process_result_value(self, value, dialect):
        return None if value is None else fernet().decrypt(bytes(value))
