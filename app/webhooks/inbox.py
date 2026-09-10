"""The receiver (OPERATIONS_SPEC §4): verify, store, `200`. Nothing else.

Processing happens in the worker, so a slow projection write, a Conduit outage
or a poison payload can never cost us a delivery — Conduit only needs the ack.

The route authenticates by signature, not by session, so `app/auth/web.py` lists
`/webhooks/` as anonymous *and* CSRF-exempt.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.config import get_settings
from app.db import sessionmaker
from app.models import WebhookEvent
from app.webhooks.signature import HEADER, verify

log = logging.getLogger(__name__)

# This route is unauthenticated by design — the signature is the auth, and the
# signature cannot be checked until the body has been read. So the body is read
# under a cap: without one, anyone who can reach the port can make the web role
# buffer arbitrary memory without knowing the secret. Real
# events are a few KB; a megabyte is already an absurd one.
MAX_BODY_BYTES = 1 << 20

router = APIRouter()


async def store(session: AsyncSession, raw_body: bytes) -> str:
    """Insert the delivery, or no-op if we already have it. Returns the event id.

    Dedupe key: the event's own id, falling back to a hash of the bytes so a
    payload without one still cannot be counted twice.
    """
    payload = _parse(raw_body)
    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id:
        event_id = hashlib.sha256(raw_body).hexdigest()
    event_type = payload.get("type")
    await session.execute(
        pg_insert(WebhookEvent)
        .values(
            event_id=event_id,
            event_type=event_type if isinstance(event_type, str) else None,
            # The bytes as they arrived: the column is `EncryptedBytes`, and the
            # signature was computed over exactly these. The `event_id` above is
            # still the hash of *these* bytes, not of the stored ciphertext —
            # Fernet is not deterministic, so hashing the stored form would make
            # every redelivery a new row.
            raw_body=raw_body,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await session.commit()
    return event_id


def _parse(raw_body: bytes) -> dict:
    """Tolerant: an unparseable body is still stored, and the worker decides."""
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def read_capped(request: Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """The raw bytes, or None if the request is over the cap.

    Streamed, not buffered by Starlette: a chunked body with no Content-Length
    would otherwise be read in full before anyone objected.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None  # stop reading; the connection is closed by the response
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/conduit")
async def receive(request: Request) -> Response:
    raw_body = await read_capped(request)  # raw bytes, before anything parses them
    if raw_body is None:
        log.warning("webhook rejected: body over %s bytes", MAX_BODY_BYTES)
        raise HTTPException(status_code=413, detail="payload too large")
    secret = get_settings().conduit_webhook_secret.get_secret_value()
    if not verify(raw_body, request.headers.get(HEADER), secret):
        # Nothing from the body is logged: it did not prove where it came from.
        log.warning("webhook rejected: signature did not verify")
        raise HTTPException(status_code=400, detail="invalid signature")
    async with sessionmaker()() as session:
        await store(session, raw_body)
    return Response(status_code=200)
