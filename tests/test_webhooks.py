"""Signature vectors (OPERATIONS_SPEC §7.12) + the inbox route (§4).

The vectors are fixture-based on purpose: live delivery cannot be exercised
until there is a public URL, and reconciliation covers correctness until then.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.main import create_app
from app.models import WebhookEvent
from app.webhooks import HEADER, verify
from tests.conftest import WEBHOOK_SECRET, settings_override

OTHER_SECRET = "whsec_" + "f9e8d7c6" * 8

# Compact, the way a real delivery arrives — and the way Python's own
# `json.dumps` does not write it, which is the point of the reserialize vector.
RAW = b'{"id":"evt_1","type":"transaction.completed","data":{"id":"txn_1"}}'


def digest(raw: bytes, timestamp: str, secret: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()


def sign(raw: bytes, *, secret: str = WEBHOOK_SECRET, at: int | None = None) -> str:
    timestamp = str(int(time.time()) if at is None else at)
    return f"t={timestamp},v1={digest(raw, timestamp, secret)}"


# --- §7.12 signature vectors ---------------------------------------------------------


def test_a_valid_signature_verifies():
    assert verify(RAW, sign(RAW), WEBHOOK_SECRET)


def test_the_whsec_prefix_is_part_of_the_key():
    """The spec's loudest warning: stripping it makes every delivery look forged."""
    stripped = WEBHOOK_SECRET.removeprefix("whsec_")
    assert not verify(RAW, sign(RAW, secret=stripped), WEBHOOK_SECRET)


def test_a_signature_from_another_secret_is_refused():
    assert not verify(RAW, sign(RAW, secret=OTHER_SECRET), WEBHOOK_SECRET)


def test_a_tampered_body_is_refused():
    header = sign(RAW)
    assert not verify(RAW.replace(b"txn_1", b"txn_9"), header, WEBHOOK_SECRET)


def test_a_reserialized_body_is_refused():
    """Why the route reads raw bytes: `json.dumps(json.loads(body))` is a
    different byte string, and would fail every genuine delivery."""
    reserialized = json.dumps(json.loads(RAW)).encode()
    assert reserialized != RAW
    assert not verify(reserialized, sign(RAW), WEBHOOK_SECRET)


@pytest.mark.parametrize("skew", [-3600, -301, 301, 3600])
def test_a_stale_or_future_timestamp_is_refused(skew):
    now = int(time.time())
    assert not verify(RAW, sign(RAW, at=now + skew), WEBHOOK_SECRET, now=now)


@pytest.mark.parametrize("skew", [-300, 0, 300])
def test_the_tolerance_window_is_inclusive(skew):
    now = int(time.time())
    assert verify(RAW, sign(RAW, at=now + skew), WEBHOOK_SECRET, now=now)


def test_either_v1_verifies_during_a_rotation():
    """Rotation grace window: one `v1` per currently-valid secret, any match wins."""
    timestamp = str(int(time.time()))
    rotated = (
        f"t={timestamp}"
        f",v1={digest(RAW, timestamp, OTHER_SECRET)}"
        f",v1={digest(RAW, timestamp, WEBHOOK_SECRET)}"
    )
    assert verify(RAW, rotated, WEBHOOK_SECRET)  # ours is second
    assert verify(RAW, rotated, OTHER_SECRET)  # …and first
    assert not verify(RAW, rotated, "whsec_" + "0" * 64)  # neither


@pytest.mark.parametrize(
    "header",
    ["", None, "garbage", "v1=deadbeef", "t=abc,v1=deadbeef", f"t={int(time.time())}"],
)
def test_a_malformed_header_is_refused(header):
    assert not verify(RAW, header, WEBHOOK_SECRET)


def test_an_unconfigured_secret_verifies_nothing():
    assert not verify(RAW, sign(RAW), "")


def test_a_non_ascii_signature_candidate_is_refused_rather_than_crashing():
    """The `v1` segment is caller-supplied and unconstrained; `compare_digest`
    raises TypeError on a non-ASCII str instead of just failing the check."""
    timestamp = str(int(time.time()))
    assert not verify(RAW, f"t={timestamp},v1=\xe9", WEBHOOK_SECRET)


# --- the inbox route -----------------------------------------------------------------


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    )


async def post(raw: bytes = RAW, header: str | None = None) -> httpx.Response:
    async with client() as http:
        return await http.post(
            "/webhooks/conduit",
            content=raw,
            headers={HEADER: header if header is not None else sign(raw)},
        )


async def rows(session) -> list[WebhookEvent]:
    return list((await session.execute(select(WebhookEvent))).scalars())


async def test_a_signed_delivery_is_stored_and_acked(session):
    assert (await post()).status_code == 200

    (event,) = await rows(session)
    assert (event.event_id, event.event_type, event.status) == (
        "evt_1",
        "transaction.completed",
        "pending",
    )
    assert event.raw_body == RAW  # kept byte-for-byte, through the encrypted column


async def test_an_invalid_signature_is_400_and_stores_nothing(session):
    assert (await post(header="t=1,v1=deadbeef")).status_code == 400
    assert await rows(session) == []


async def test_a_missing_signature_header_is_400(session):
    async with client() as http:
        response = await http.post("/webhooks/conduit", content=RAW)
    assert response.status_code == 400
    assert await rows(session) == []


async def test_a_redelivery_is_deduped_by_event_id(session):
    for _ in range(3):
        assert (await post()).status_code == 200
    assert len(await rows(session)) == 1


async def test_an_event_without_an_id_dedupes_on_the_body_hash(session):
    raw = b'{"type": "customer.created", "data": {"id": "cus_1"}}'
    assert (await post(raw)).status_code == 200
    assert (await post(raw)).status_code == 200

    (event,) = await rows(session)
    assert event.event_id == hashlib.sha256(raw).hexdigest()


async def test_an_unparseable_body_is_still_stored(session):
    """It was signed, so it is Conduit's — the worker decides what it means."""
    assert (await post(b"not json at all")).status_code == 200
    (event,) = await rows(session)
    assert (event.event_type, event.status) == (None, "pending")


async def test_the_route_is_not_mounted_without_a_secret(session):
    with settings_override(conduit_webhook_secret=SecretStr("")):
        response = await post()
    assert response.status_code == 404
    assert await rows(session) == []


async def test_the_receiver_needs_no_session_and_no_csrf_token(session):
    """It is a POST from a machine that has no cookies: signature is the auth."""
    from app.auth.web import ANONYMOUS_PREFIXES, CSRF_EXEMPT_PREFIXES

    assert "/webhooks/" in ANONYMOUS_PREFIXES
    assert "/webhooks/" in CSRF_EXEMPT_PREFIXES
    assert (await post()).status_code == 200
    assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
