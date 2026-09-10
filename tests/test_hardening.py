"""The hardening round: one file, so the guards and their proofs read together.

Every test here fails without the guard it pins.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app import audit, operations, projections
from app.conduit import ConduitClient, execute_operation
from app.conduit.client import Outcome, classify, parse_problem
from tests.web_harness import make_app, signed_in, stub
from app.models import OPERATION_TYPES, Operation
from tests.conftest import settings_override

ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}
SYSTEM = {"actor_id": audit.SYSTEM_ACTOR_ID, "actor_email": audit.SYSTEM_ACTOR_EMAIL}
PATH = "/v2/payouts"


async def reload(session, op_id) -> Operation:
    return (
        await session.execute(
            select(Operation).where(Operation.id == op_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def start_payout(session, body: dict | None = None):
    return await operations.start(
        session,
        type="payout_create",
        **ACTOR,
        path=PATH,
        body=body or {"amount": "100.00", "asset": "USD"},
    )


def stub(handler) -> ConduitClient:
    return ConduitClient(transport=httpx.MockTransport(handler))


# --- a webhook may resolve the row while the HTTP call is open ------------


async def test_a_webhook_confirming_mid_call_does_not_500_the_submit(session):
    """Conduit answers our POST, and the settlement webhook for it
    arrives before our own response finishes being recorded. The recorder must
    converge on the webhook's answer — a 500 here is what makes an operator
    resubmit, and a resubmit is how you pay twice."""
    op, _ = await start_payout(session)
    client = stub(lambda request: httpx.Response(202, json={"id": "txn_1", "status": "pending"}))

    async def deliver_webhook_then_answer(*args, **kwargs):
        response = await real_mutate(*args, **kwargs)
        await projections.apply_observation(
            session,
            resource_kind="transactions",
            resource_id="txn_1",
            observed={"status": "completed", "clientReferenceId": str(op.id)},
            observed_at=datetime.now(UTC),
        )
        return response

    real_mutate = client.mutate
    client.mutate = deliver_webhook_then_answer  # type: ignore[method-assign]

    resolved = await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    assert (resolved.state, resolved.conduit_resource_id) == ("confirmed", "txn_1")
    assert await confirmed_rows(session, op.id) == 1  # not two, not an exception


async def confirmed_rows(session, op_id) -> int:
    from sqlalchemy import func

    from app.models import AuditEvent

    return await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.operation_id == op_id, AuditEvent.action == "operation.confirmed")
    )


async def test_a_webhook_confirming_mid_call_beats_a_lost_response(session):
    """Same interleaving, but our own call then fails. The webhook's confirmation
    is proof from Conduit; it must not be overwritten with outcome_unknown."""
    op, _ = await start_payout(session)
    client = stub(lambda request: httpx.Response(200, json={}))

    async def confirm_then_drop(*args, **kwargs):
        await projections.apply_observation(
            session,
            resource_kind="transactions",
            resource_id="txn_1",
            observed={"status": "completed", "clientReferenceId": str(op.id)},
            observed_at=datetime.now(UTC),
        )
        raise httpx.ReadError("connection reset")

    client.mutate = confirm_then_drop  # type: ignore[method-assign]

    with pytest.raises(httpx.ReadError):
        await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    assert (await reload(session, op.id)).state == "confirmed"


# --- `stalled` is covered by the double-submit guard ----------------------


async def test_an_identical_resubmit_while_stalled_resolves_to_the_same_row(session):
    """A stalled operation may already have paid. A fresh row would
    mean a fresh idempotency key and a second payout."""
    body = {"amount": "250.00", "asset": "USD"}
    first, is_new = await start_payout(session, body)
    assert is_new
    await drive_to_stalled(session, first.id)

    second, is_new = await start_payout(session, body)
    assert not is_new
    assert (second.id, second.idempotency_key) == (first.id, first.idempotency_key)


async def test_an_admin_abandonment_releases_the_guard(session):
    """The release valve (§2): once an admin declares the stalled row dead, an
    intentional resubmit is a genuinely new operation."""
    body = {"amount": "250.00", "asset": "USD"}
    first, _ = await start_payout(session, body)
    await drive_to_stalled(session, first.id)
    await operations.transition(
        session, first.id, "abandoned", **ACTOR, detail={"reason": "admin_release"}
    )

    second, is_new = await start_payout(session, body)
    assert is_new and second.id != first.id
    assert second.idempotency_key != first.idempotency_key


async def drive_to_stalled(session, op_id) -> None:
    for state in ("in_flight", "outcome_unknown", "stalled"):
        await operations.transition(session, op_id, state, **SYSTEM)


# --- a manual retry gets a fresh reconciliation cycle ---------------------


async def test_the_stalled_retry_resets_the_reconciliation_cycle(session):
    """Carrying the exhausted count means a lost retry response waits
    out the last backoff step and re-stalls without ever doing a lookup."""
    op, _ = await start_payout(session)
    await drive_to_stalled(session, op.id)
    await session.execute(
        update(Operation).where(Operation.id == op.id).values(reconcile_count=5)
    )
    await session.commit()

    retried = await operations.transition(session, op.id, "in_flight", **ACTOR)
    assert (retried.reconcile_count, retried.unknown_since) == (0, None)


# --- retention never purges a retryable body -----------------------------


async def test_retention_keeps_the_body_of_a_stalled_operation(session):
    """`stalled` is retryable, and the retry promises the original
    bytes with the original key."""
    stalled, _ = await start_payout(session, {"amount": "1.00"})
    await drive_to_stalled(session, stalled.id)
    confirmed, _ = await start_payout(session, {"amount": "2.00"})
    await operations.transition(session, confirmed.id, "in_flight", **ACTOR)
    await operations.transition(
        session, confirmed.id, "confirmed", **ACTOR, conduit_resource_id="txn_1"
    )

    long_ago = datetime.now(UTC) - timedelta(days=400)
    await session.execute(update(Operation).values(resolved_at=long_ago))
    await session.commit()

    assert await operations.purge_request_bodies(session) == 1
    assert (await reload(session, stalled.id)).request_body == {"amount": "1.00"}
    assert (await reload(session, confirmed.id)).request_body is None


async def test_retention_purges_the_error_with_the_body(session):
    """Conduit's problem detail echoes submitted field values
    back — "recipient accountNumber 1234… is invalid" — so a purge that dropped
    `request_body` and kept `error` kept the coordinates it existed to drop, for
    ever. Same row, same cutoff, same statement."""
    rejected, _ = await start_payout(session, {"amount": "3.00"})
    await operations.transition(session, rejected.id, "in_flight", **ACTOR)
    await operations.transition(
        session,
        rejected.id,
        "rejected",
        **ACTOR,
        error={"type": "INVALID_FIELD", "detail": "accountNumber 1234567890 is invalid"},
    )
    # …and a `stalled` row keeps BOTH: it is retryable, and the retry promises
    # the original bytes (above).
    stalled, _ = await start_payout(session, {"amount": "4.00"})
    await drive_to_stalled(session, stalled.id)
    await session.execute(
        update(Operation).where(Operation.id == stalled.id).values(error={"detail": "kept"})
    )

    long_ago = datetime.now(UTC) - timedelta(days=400)
    await session.execute(update(Operation).values(resolved_at=long_ago))
    await session.commit()

    assert await operations.purge_request_bodies(session) == 1
    purged = await reload(session, rejected.id)
    assert purged.request_body is None and purged.error is None
    survivor = await reload(session, stalled.id)
    assert survivor.request_body == {"amount": "4.00"} and survivor.error == {"detail": "kept"}


async def test_an_error_alone_is_enough_to_be_purged(session):
    """A confirmed operation whose body was purged by an earlier pass, but which
    still carries an error, is still selected: the WHERE clause is an `or_`, not
    the old `request_body is not null` alone."""
    op, _ = await start_payout(session, {"amount": "5.00"})
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", **ACTOR, conduit_resource_id="txn_9")
    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(
            request_body=None,
            error={"detail": "leftover"},
            resolved_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    await session.commit()

    assert await operations.purge_request_bodies(session) == 1
    assert (await reload(session, op.id)).error is None


# --- only a parsed problem-detail is a definitive rejection ---------------


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ('{"type":"INVALID_FIELD","title":"Bad"}', "application/json", Outcome.REJECTED),
        ('{"title":"Bad request"}', "application/json", Outcome.REJECTED),
        ("<html><body>400 Bad Request</body></html>", "text/html", Outcome.AMBIGUOUS),
        ("", "text/plain", Outcome.AMBIGUOUS),
        ("{}", "application/json", Outcome.AMBIGUOUS),
        ("null", "application/json", Outcome.AMBIGUOUS),
    ],
)
def test_only_a_real_problem_detail_releases_deduplication(body, content_type, expected):
    """A gateway's HTML 400 says nothing about what Conduit did with
    the request, so it cannot be treated as "definitely did not happen"."""
    response = httpx.Response(400, content=body, headers={"content-type": content_type})
    assert classify(parse_problem(response)) is expected


async def test_an_unparseable_4xx_leaves_the_operation_unknown(session):
    """End to end: the guard stays on, and the reconciler gets to find out."""
    op, _ = await start_payout(session)
    client = stub(lambda request: httpx.Response(400, content=b"<html>nope</html>"))
    resolved = await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    assert (resolved.state, resolved.error) == ("outcome_unknown", None)


# --- a malformed 2xx is never proof of absence ---------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>502 Bad Gateway</body></html>",  # a gateway page, HTTP 200
        '{"message":"ok"}',  # 2xx JSON that is not a list envelope
        '{"data":{"id":"txn_1"}}',  # `data` present but not a list
        "[]",  # bare array, no envelope
    ],
)
async def test_a_malformed_200_list_never_triggers_a_replay(session, body):
    """`Page(items=[])` was indistinguishable from "Conduit says this
    payout does not exist" — and that is the one belief that authorizes a
    replay. Past the key-cache window, that replay pays twice."""
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        posts.append(request)
        return httpx.Response(202, json={"id": "txn_1"})

    fake_client = stub(handle)
    op, _ = await start_payout(session)
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await make_due(session, op.id)

    from app import reconciliation

    counts = await reconciliation.reconcile_pass(session, fake_client)
    await fake_client.aclose()

    assert counts == {"unresolved": 1}
    assert posts == []  # nothing re-sent on an unreadable answer
    assert (await reload(session, op.id)).state == "outcome_unknown"


async def make_due(session, op_id) -> None:
    await session.execute(
        update(Operation)
        .where(Operation.id == op_id)
        .values(unknown_since=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()


# --- a read cannot overspend the reconciler's budget --------------------


async def test_a_read_never_spends_more_than_the_remaining_budget(session):
    """One unit left must buy one wire request, not four retries."""
    gets: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        gets.append(request)
        return httpx.Response(503, json={"type": "UPSTREAM"})

    from app.reconciliation.service import BudgetExhausted, _Budgeted

    client = stub(handle)
    budgeted = _Budgeted(client, 1)
    await budgeted.get("/v2/transactions")
    assert (len(gets), budgeted.remaining) == (1, 0)

    with pytest.raises(BudgetExhausted):
        await budgeted.get("/v2/transactions")
    await client.aclose()
    assert budgeted.remaining >= 0


# --- absence must be proven, not assumed from page one -------------------


def paged(items: list[dict], next_cursor: str | None) -> httpx.Response:
    return httpx.Response(
        200, json={"data": items, "meta": {"nextCursor": next_cursor, "previousCursor": None}}
    )


async def test_a_match_beyond_the_first_page_is_still_found(session):
    """A busy minute can push our application off page one; stopping
    there and replaying would submit the onboarding twice."""
    op, _ = await start_payout(session)  # reuse the row; swap in the recipe below
    await session.execute(
        update(Operation).where(Operation.id == op.id).values(type="onboarding_submit")
    )
    await session.commit()
    op = await reload(session, op.id)
    recent = (datetime.now(UTC) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    filler = [{"id": f"app_{n}", "createdAt": recent} for n in range(100)]
    ours = {"id": "app_ours", "clientReferenceId": str(op.id), "createdAt": recent}
    pages = [paged(filler, "c1"), paged([ours], None)]

    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return pages[min(len(pages) - 1, int(bool(request.url.params.get("cursor"))))]
        posts.append(request)
        return httpx.Response(202, json={"id": "app_new"})

    client = stub(handle)
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await make_due(session, op.id)

    from app import reconciliation

    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert counts == {"confirmed": 1}
    assert posts == []  # found on page two, so nothing was resubmitted
    assert (await reload(session, op.id)).conduit_resource_id == "app_ours"


async def test_an_unbounded_collection_is_unresolved_not_absent(session):
    """Walking out of pages without reaching a boundary is "I could not tell"."""
    from app.reconciliation.service import MAX_PAGES, LookupUnavailable, _Budgeted, _walk

    recent = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    client = stub(lambda request: paged([{"id": "x", "createdAt": recent}], "more"))
    budgeted = _Budgeted(client, MAX_PAGES + 5)

    with pytest.raises(LookupUnavailable):
        await _walk(budgeted, "/v2/applications", stop_before=datetime.now(UTC))
    await client.aclose()


# --- an RFI response is not identified by its text alone -----------------


async def test_an_earlier_round_with_the_same_text_does_not_confirm(session):
    """The operator answered "see attached" in round 1 and again in
    round 2; round 1's response must not confirm round 2's operation."""
    op, _ = await operations.start(
        session,
        type="rfi_respond",
        **ACTOR,
        path="/v2/rfis/rfi_1/responses",
        body={
            "message": "see attached",
            "documentIds": ["doc_2"],
            "submittedBy": {"email": "operator@example.com"},
        },
    )
    old = (op.created_at - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    earlier_round = {
        "id": "rfr_1",
        "roundId": "rnd_1",
        "message": "see attached",
        "documentIds": ["doc_1"],
        "submittedByEmail": "operator@example.com",
        "createdAt": old,
    }

    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "rfi_1", "responses": [earlier_round]})
        posts.append(request)
        return httpx.Response(201, json={"id": "rfr_2"})

    client = stub(handle)
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await make_due(session, op.id)

    from app import reconciliation

    await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    # It did not match, so the recipe replayed with the original key…
    assert len(posts) == 1
    assert (await reload(session, op.id)).conduit_resource_id == "rfr_2"


# --- an unknown status is never evidence of success ----------------------


@pytest.mark.parametrize(
    ("op_type", "path", "status"),
    [
        ("order_execute", "/v2/orders/ord_1/execute", "settling"),
        ("order_cancel", "/v2/orders/ord_1/cancel", "settling"),
        ("payout_cancel", "/v2/payouts/pay_1/cancel", "clawed_back"),
        ("whitelist_revoke", "/v2/customers/cus_1/whitelist-recipients/wr_1", "frozen"),
    ],
)
async def test_an_unknown_status_leaves_the_operation_unresolved(session, op_type, path, status):
    """`order_execute` counted every status except `pending` as done,
    including ones that do not exist yet — the exact inference plan v2 §7 bans."""
    op, _ = await operations.start(session, type=op_type, **ACTOR, path=path, body={})
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "res_1", "status": status})
        posts.append(request)
        return httpx.Response(200, json={"id": "res_1"})

    client = stub(handle)
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await make_due(session, op.id)

    from app import reconciliation

    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert counts == {"unresolved": 1}
    assert posts == []  # not confirmed, and not blindly replayed either
    assert (await reload(session, op.id)).state == "outcome_unknown"


def test_the_reconciler_and_the_projections_share_one_status_vocabulary():
    from app.reconciliation.service import _known

    assert _known("orders") == ("pending", "succeeded", "failed", "cancelled")
    assert "completed" in _known("transactions")
    # A payout is a transaction, so there is no separate `payouts` vocabulary to
    # drift from this one — asking for it fails loudly rather than returning {}.
    with pytest.raises(KeyError):
        _known("payouts")


# --- document_upload is no longer refused — it is multipart -------------


async def test_no_operation_type_is_refused_by_the_executor(session):
    """The refusal existed because a JSON
    executor cannot carry a multipart upload; `app.documents` now does. What must
    stay true is that no type is quietly half-supported — every one of them has a
    send path and a reconciliation recipe.

    The multipart body itself is covered end-to-end in `test_documents.py`.
    """
    from app import reconciliation
    from app.conduit import execute

    assert not hasattr(execute, "UNSUPPORTED")
    assert set(reconciliation.RECIPES) == set(OPERATION_TYPES)


# --- a claim is a lease, not a life sentence ----------------------------


async def test_a_stranded_claim_is_reclaimed_and_processed(session):
    """A worker that dies between claiming and recording used to
    strand its whole batch in `processing` forever."""
    from app import worker
    from app.models import WebhookEvent
    from app.webhooks import store
    from tests.test_worker import event

    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))
    claimed = await worker.claim(session)
    assert [e.status for e in claimed] == ["processing"]
    assert await worker.process_pending(session) == {}  # still leased, nobody else takes it

    await session.execute(
        update(WebhookEvent).values(
            claimed_at=datetime.now(UTC) - timedelta(seconds=worker.CLAIM_LEASE_SECONDS + 1)
        )
    )
    await session.commit()

    assert await worker.process_pending(session) == {"processed": 1}
    stranded = (
        await session.execute(
            select(WebhookEvent).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (stranded.status, stranded.claimed_at) == ("processed", None)
    assert stranded.attempts == 2  # the dead worker's attempt still counts


# --- the inbox does not wait behind a slow repair pass ------------------


async def test_the_inbox_drains_while_a_repair_pass_is_stuck(session, monkeypatch):
    """One shared loop meant a reconciliation pass grinding through an
    outage blocked every incoming webhook — and SIGTERM with them."""
    import asyncio
    import time

    from app import worker
    from app.webhooks import store
    from tests.test_worker import event, statuses

    async def never_finishes(_session, _client):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "tick", never_finishes)
    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))

    stop = asyncio.Event()
    with settings_override(reconcile_interval_seconds=1):
        running = asyncio.create_task(worker.run(stop=stop))
        drained = asyncio.create_task(_await_processed(session, statuses))
        await asyncio.wait_for(drained, timeout=10)  # …despite the wedged repair pass

        stop.set()
        started = time.monotonic()
        await asyncio.wait_for(running, timeout=10)

    assert time.monotonic() - started < 5  # shutdown cancels the stuck pass
    assert await statuses(session) == [("evt_1", "processed")]


async def _await_processed(session, statuses) -> None:
    import asyncio

    while await statuses(session) != [("evt_1", "processed")]:
        await asyncio.sleep(0.01)


# --- the unauthenticated route reads under a cap ------------------------


async def test_an_oversized_body_is_refused_before_verification(session):
    """The signature cannot be checked until the body is read, so the
    read itself has to be bounded — otherwise anyone who can reach the port can
    make the web role buffer memory without knowing the secret."""
    from app.models import WebhookEvent
    from app.webhooks.inbox import MAX_BODY_BYTES
    from tests.test_webhooks import client, sign

    huge = b"x" * (MAX_BODY_BYTES + 1)
    async with client() as http:
        declared = await http.post(
            "/webhooks/conduit", content=huge, headers={"X-Conduit-Signature": sign(huge)}
        )
    assert declared.status_code == 413

    async def chunks():
        for _ in range((MAX_BODY_BYTES // 4096) + 2):
            yield b"y" * 4096

    async with client() as http:  # no content-length to lie about: chunked
        streamed = await http.post(
            "/webhooks/conduit", content=chunks(), headers={"X-Conduit-Signature": "t=1,v1=ff"}
        )
    assert streamed.status_code == 413
    assert (await session.execute(select(WebhookEvent))).scalars().all() == []


async def test_a_normal_event_is_unaffected_by_the_cap(session):
    from tests.test_webhooks import post

    assert (await post()).status_code == 200


# --- sandbox events never land in a production console ------------------


def test_an_event_from_the_wrong_mode_is_not_projected():
    """One endpoint can receive both live and sandbox deliveries."""
    from app import worker

    sandbox_event = {
        "type": "transaction.completed",
        "mode": "sandbox",
        "data": {"id": "txn_1", "status": "completed"},
    }
    with settings_override(conduit_env="production"):
        assert worker.observation(sandbox_event) is None
        assert worker.observation({**sandbox_event, "mode": "live"}) is not None
        # Absent `mode` is tolerated: the field's presence is unverified against
        # a real delivery, and dropping real events would be worse.
        assert worker.observation({k: v for k, v in sandbox_event.items() if k != "mode"})

    with settings_override(conduit_env="sandbox"):
        assert worker.observation(sandbox_event) is not None
        assert worker.observation({**sandbox_event, "mode": "live"}) is None


# --- findings 16 + 18: startup refuses to run a real environment unsafely -----------


def config(**overrides):
    from cryptography.fernet import Fernet

    from app.config import Settings

    strong = "9f2c" * 16
    return Settings(
        **{
            "database_url": "postgresql+psycopg://x@/y",
            # Explicit, not inherited: conftest puts AUTH_MODE=disabled in the
            # environment and Settings would otherwise read it from there.
            "auth_mode": "proxy",
            "session_secret": strong,
            "encryption_key": Fernet.generate_key().decode(),
            "proxy_shared_secret": strong,
            **overrides,
        }
    )


def test_a_non_utf8_secret_file_fails_boot_without_echoing_another_secret(monkeypatch, tmp_path):
    """`UnicodeDecodeError` is a `ValueError` subclass, so it used to
    escape the `except OSError` handler in `_secret_files` and pydantic wrapped
    it into a `ValidationError` whose message echoes the pre-parse settings
    dict — leaking whatever secret happened to sit in that dict, not just
    naming the setting whose file was unreadable."""
    from app.config import Settings

    bad = tmp_path / "encryption_key"
    bad.write_bytes(b"\xff\xfe\x00not-valid-utf8\xff")

    canary = "TOPSECRET-canary-fragment-0123456789ABCDEF"
    # Strip every other env-sourced field conftest seeds by default, so the
    # canary is the only secret-shaped value left in play.
    for var in ("ENCRYPTION_KEY", "AUTH_MODE", "CONDUIT_ENV", "CONDUIT_API_KEY", "CONDUIT_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SESSION_SECRET", canary)
    monkeypatch.setenv("ENCRYPTION_KEY_FILE", str(bad))

    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY_FILE") as raised:
        Settings()

    message = str(raised.value)
    assert canary not in message
    # No fragment either — a truncated echo would still leak a recognisable tail.
    for start in range(0, len(canary) - 8):
        assert canary[start : start + 8] not in message


@pytest.mark.parametrize("env", ["staging", "production"])
def test_plaintext_cookies_are_refused_outside_development(env):
    """It applies to the session cookie, so it is a bearer token
    travelling in the clear before the first redirect."""
    with pytest.raises(RuntimeError, match="COOKIE_SECURE=false"):
        config(conduit_env=env, cookie_secure=False)
    assert config(conduit_env="sandbox", cookie_secure=False).cookie_secure is False


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("session_secret", "replace-me"),
        ("session_secret", "tooshort123"),
        ("proxy_shared_secret", "replace-me-with-something"),
        ("proxy_shared_secret", "proxy-secret"),
    ],
)
@pytest.mark.parametrize("env", ["staging", "production"])
def test_placeholder_and_short_secrets_are_refused(env, setting, value):
    """`.env.example` is published; a copied config must not boot."""
    with pytest.raises(RuntimeError) as raised:
        config(conduit_env=env, **{setting: value})
    assert setting.upper() in str(raised.value)
    assert value not in str(raised.value)  # the message never echoes the secret


def test_development_still_boots_with_a_convenient_secret():
    assert config(conduit_env="sandbox", session_secret="dev").conduit_env == "sandbox"


def test_a_strong_passphrase_containing_the_word_secret_is_accepted():
    """Length + randomness is what makes a
    secret usable, not the absence of the English word "secret"."""
    from app.config import weak_secret

    passphrase = "correct-horse-battery-staple-secret-society-42"
    assert len(passphrase) >= 32
    assert weak_secret(passphrase) is None


def test_the_env_example_encryption_key_is_still_refused():
    """The literal placeholder .env.example
    ships for ENCRYPTION_KEY must still be caught at boot.

    This drives `Settings()` itself rather than calling `weak_secret`
    directly, because ENCRYPTION_KEY is not in the weak-secret loop at all —
    `weak_secret` never sees it. What actually refuses this value is the
    Fernet-format check in `_guards`; calling `weak_secret` in isolation would
    keep passing even if that check were removed, which is exactly the gap
    that let a genuinely valid Fernet key sit uncaught elsewhere in this repo.
    """
    from pathlib import Path

    placeholder = None
    for line in (Path(__file__).resolve().parent.parent / ".env.example").read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "ENCRYPTION_KEY":
            placeholder = value
    assert placeholder, "ENCRYPTION_KEY not found in .env.example"

    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        config(encryption_key=placeholder)


def test_a_repository_wide_scan_finds_no_committed_value_a_fernet_key_would_accept():
    """`scripts/generate_permission_matrix.py` used to hardcode a
    genuinely valid Fernet key as its bootstrap ENCRYPTION_KEY, which would
    also boot in a real deployment if copy-pasted. `Fernet()` only accepts 32
    url-safe-base64-encoded bytes (44 characters), so this scans every
    tracked, text-ish file for a token of that shape and asserts none of them
    survive the same construction Fernet() performs on ENCRYPTION_KEY."""
    import re
    import subprocess
    from pathlib import Path

    from cryptography.fernet import Fernet

    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    skip_names = {"uv.lock"}
    token_re = re.compile(r"[A-Za-z0-9_-]{43,44}=?")
    offenders = []
    for rel in tracked:
        if rel in skip_names:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not a place a copy-pasted key lives as text
        for match in token_re.finditer(text):
            try:
                Fernet(match.group())
            except Exception:
                continue
            offenders.append(f"{rel}: {match.group()[:6]}...{match.group()[-6:]}")

    assert not offenders, f"committed value(s) accepted by Fernet(): {offenders}"


def test_the_example_env_file_ships_no_usable_placeholder_secret():
    from pathlib import Path

    from app.config import weak_secret

    lines = (Path(__file__).resolve().parent.parent / ".env.example").read_text().splitlines()
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() in {"SESSION_SECRET", "PROXY_SHARED_SECRET"} and value:
            assert weak_secret(value) is not None, f"{name} ships a value that would pass the guard"


# --- no plaintext OIDC anywhere but loopback ---------------------------


@pytest.mark.parametrize(
    ("issuer", "accepted"),
    [
        ("https://idp.example.com", True),
        ("http://localhost:8080/realms/x", True),  # local development
        ("http://127.0.0.1:8080", True),
        ("http://idp.example.com", False),
        ("http://idp.example.com.localhost.evil.net", False),
    ],
)
def test_a_plaintext_oidc_issuer_is_refused(issuer, accepted):
    """An http issuer puts the client secret, the code and the PKCE
    verifier on the wire in cleartext."""
    strong = "9f2c" * 16
    make = lambda: config(  # noqa: E731
        auth_mode="oidc",
        oidc_issuer=issuer,
        oidc_client_id="console",
        oidc_client_secret=strong,
    )
    if accepted:
        assert make().oidc_issuer == issuer
    else:
        with pytest.raises(RuntimeError, match="OIDC_ISSUER must be https"):
            make()


async def test_a_downgraded_discovery_document_is_refused():
    """An https issuer that then points its token endpoint at http."""
    from app.auth.oidc import OIDCClient, OIDCError

    document = {
        "issuer": "https://idp.example.test",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "http://idp.example.test/token",  # downgrade
        "jwks_uri": "https://idp.example.test/jwks",
    }
    client = OIDCClient(
        config(
            auth_mode="oidc",
            oidc_issuer="https://idp.example.test",
            oidc_client_id="console",
            oidc_client_secret="9f2c" * 16,
        ),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document)),
    )
    with pytest.raises(OIDCError, match="token_endpoint is not https"):
        await client.discover()
    await client.aclose()


# --- a broken issuer is an audited sign-in failure, not a 500 -----------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>maintenance</html>"),  # 200, not JSON
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
async def test_a_broken_discovery_document_raises_oidcerror(response):
    """`response.json()` on an HTML body used to escape as a 500."""
    from app.auth.oidc import OIDCClient, OIDCError

    client = OIDCClient(
        config(
            auth_mode="oidc",
            oidc_issuer="https://idp.example.test",
            oidc_client_id="console",
            oidc_client_secret="9f2c" * 16,
        ),
        transport=httpx.MockTransport(lambda request: response),
    )
    with pytest.raises(OIDCError):
        await client.discover()
    await client.aclose()


# --- __Host- binds the cookie to this exact origin ---------------------


def test_auth_cookies_carry_the_host_prefix_when_secure():
    """A sibling subdomain can set a cookie on the parent domain. If
    the app then reads it, a *validly signed* session the attacker minted logs
    them in as themselves — and every money action is audited under their name.
    `__Host-` is the browser-enforced answer: same origin, `Path=/`, no `Domain`.
    """
    from app.auth.tokens import CSRF_COOKIE, LOGIN_COOKIE, SESSION_COOKIE, cookie_name

    for base in (SESSION_COOKIE, CSRF_COOKIE, LOGIN_COOKIE):
        assert cookie_name(base, secure=True) == f"__Host-{base}"
        # Browsers reject __Host- without Secure, so plain-http development
        # keeps the bare name — and config refuses COOKIE_SECURE=false outside
        # sandbox, so this branch cannot reach a real deployment.
        assert cookie_name(base, secure=False) == base


async def test_the_app_actually_sets_the_prefixed_cookie():
    """Asserted against the real middleware, not the helper."""
    from fastapi import FastAPI

    from app.auth import install_auth
    from app.auth.tokens import CSRF_COOKIE

    settings = config(auth_mode="disabled", conduit_env="sandbox")
    app = FastAPI()

    @app.get("/whoami")
    async def whoami() -> dict:
        return {"ok": True}

    install_auth(app, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://console.test"
    ) as http:
        response = await http.get("/whoami")

    assert response.status_code == 200
    issued = response.headers["set-cookie"]
    assert issued.startswith(f"__Host-{CSRF_COOKIE}=")
    assert "Secure" in issued and "Path=/" in issued and "Domain" not in issued


# --- §2 (2026-08-28): stalled stops looking, it does not stop listening -------------


async def observe_settlement(op_id, *, resource="txn_1", status="completed"):
    """One webhook-shaped observation, in its own session like the worker's."""
    from app.db import sessionmaker

    async with sessionmaker()() as own:
        return await projections.apply_observation(
            own,
            resource_kind="transactions",
            resource_id=resource,
            observed={"status": status, "clientReferenceId": str(op_id)},
            observed_at=datetime.now(UTC),
        )


async def test_an_observation_resolves_a_stalled_operation(session):
    """The reconciler gave up asking; the settlement webhook arrived anyway.
    Holding that proof while the row still reads "couldn't confirm" would be a
    lie the operator has to chase down by hand."""
    from app.models import AuditEvent

    op, _ = await start_payout(session)
    await drive_to_stalled(session, op.id)
    assert (await reload(session, op.id)).state == "stalled"

    await observe_settlement(op.id)

    resolved = await reload(session, op.id)
    assert (resolved.state, resolved.conduit_resource_id) == ("confirmed", "txn_1")
    assert resolved.resolved_at is not None

    confirmation = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.operation_id == op.id, AuditEvent.action == "operation.confirmed"
            )
        )
    ).scalar_one()
    assert confirmation.actor_id == audit.SYSTEM_ACTOR_ID
    assert confirmation.detail["from"] == "stalled"
    assert confirmation.detail["reason"] == "observed"


async def test_the_reconciler_still_leaves_stalled_rows_alone(session):
    """Only evidence resolves a stalled row — the reconciler does not go back to
    asking about one (§2, unchanged)."""
    from app import reconciliation

    op, _ = await start_payout(session)
    await drive_to_stalled(session, op.id)
    await make_due(session, op.id)

    touched: list[httpx.Request] = []
    client = stub(lambda request: touched.append(request) or httpx.Response(200, json={}))
    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert (counts, touched) == ({}, [])
    assert (await reload(session, op.id)).state == "stalled"


async def test_an_operator_retry_racing_an_observation_converges(session):
    """The reverse race: the operator hits Retry on the stalled row at the same
    moment the observation lands. Whoever takes the row lock second finds a
    state that makes its own move illegal, and drops it — no error either way."""
    import asyncio

    from app.models import AuditEvent

    op, _ = await start_payout(session)
    await drive_to_stalled(session, op.id)

    async def operator_retry():
        from app.db import sessionmaker

        async with sessionmaker()() as own:
            return await operations.try_transition(own, op.id, "in_flight", **ACTOR)

    retried, applied = await asyncio.gather(operator_retry(), observe_settlement(op.id))
    assert applied  # the projection is written whoever wins

    final = await reload(session, op.id)
    # Either order is legitimate; what must never happen is a raised
    # IllegalTransition or two competing resolutions of the same row.
    assert final.state in ("confirmed", "in_flight")
    if retried is None:  # the observation got there first
        assert final.state == "confirmed"

    resolutions = (
        await session.execute(
            select(AuditEvent.action).where(
                AuditEvent.operation_id == op.id,
                AuditEvent.action.in_(("operation.confirmed", "operation.rejected")),
            )
        )
    ).scalars().all()
    assert len(resolutions) <= 1


# --- pre-share hardening ------------------------------------------------


def test_every_live_script_refuses_anything_but_the_sandbox():
    """Item 2. `tests/e2e/` talks to a real Conduit host and writes. Scripts
    02–05 used to read `SANDBOX_API_KEY`/`SANDBOX_HOST` from `../.env` — which in
    this engagement is a **live key against a host that self-labels
    "Production"** — with no prefix or host check at all, and `04` posts payouts
    with it. They now carry `06_sandbox_sweep.py`'s guard verbatim.

    A source assertion, because the scripts exit on missing credentials by
    design and cannot be imported. The rule is the directory's, not four
    files' — a new script that forgets fails here.
    """
    from pathlib import Path

    scripts = sorted((Path(__file__).parent / "e2e").glob("*.py"))
    assert len(scripts) >= 11, "the e2e directory moved; this pin needs re-aiming"
    for script in scripts:
        source = script.read_text()
        assert "ck_sandbox_" in source, f"{script.name} has no key-prefix guard"
        assert "https://api.sandbox.conduit.financial" in source, f"{script.name}: no host pin"
        assert "CONDUIT_SANDBOX_API_KEY" in source, f"{script.name} reads the wrong key name"
        # The pre-Phase-19 names, which resolve to the live staging pair.
        assert 'values.get("SANDBOX_API_KEY")' not in source, f"{script.name} still reads the live key"


async def test_the_security_headers_are_on_a_rendered_page():
    """Item 7. Four headers, asserted through the real ASGI app rather than on
    the middleware in isolation — the point is that they survive the auth layer,
    which sits inside this one."""
    from app.main import SECURITY_HEADERS, create_app

    with settings_override(auth_mode="disabled"):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.test"
        ) as http:
            response = await http.get("/health/live")
    assert response.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    csp = response.headers["Content-Security-Policy"]
    # The two allowances that would make the policy decorative.
    assert "unsafe-inline" not in csp.split("style-src")[0]
    assert "unsafe-eval" not in csp
    assert "script-src 'self';" in csp


async def test_the_security_headers_are_on_an_unauthenticated_refusal():
    """The outermost-middleware claim, pinned. Starlette runs middleware in
    reverse registration order and this one is added LAST, so it wraps the auth
    layer — which means a 401 from a request that never reached a route still
    carries the headers. Registered any earlier, it would not, and the responses
    a prober actually sees would be the bare ones."""
    from app.main import SECURITY_HEADERS, create_app

    from pydantic import SecretStr

    with settings_override(
        auth_mode="proxy", proxy_shared_secret=SecretStr("9f2c" * 16)
    ):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.test"
        ) as http:
            # No proxy secret header, so the adapter authenticates nobody.
            response = await http.get("/customers")
    assert response.status_code == 401, response.text[:200]
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_the_app_publishes_no_openapi_surface():
    """Item 8. Three keyword arguments; nothing in-app or in the tests reads
    them (the drift test reads `contracts/`, which is *Conduit's* spec)."""
    from app.main import create_app

    with settings_override(auth_mode="disabled"):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://console.test"
        ) as http:
            for path in ("/openapi.json", "/docs", "/redoc"):
                assert (await http.get(path)).status_code == 404, path


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("/auth/callback", True),
        ("/oidc/cb", True),
        ("/a/b/c", True),
        ("/", False),          # every path startswith this — the whole app goes anonymous
        ("", False),           # …and `"".startswith` is true of everything too
        ("/callback", False),  # one segment: `/callbacks-are-fun` matches the prefix
        ("/auth/callback/", False),
        ("auth/callback", False),
    ],
)
def test_the_oidc_callback_path_cannot_swallow_the_app(path, accepted):
    """Item 13. `install_auth` adds this value to the anonymous prefixes and
    matches with `str.startswith`, so a one-character value turns authentication
    off for the whole console. Refused at boot."""
    from app.config import Settings

    def build():
        return config(
            auth_mode="oidc",
            oidc_issuer="https://idp.example.com",
            oidc_client_id="console",
            oidc_client_secret="9f2c" * 16,
            oidc_callback_path=path,
        )

    if accepted:
        assert build().oidc_callback_path == path
    else:
        with pytest.raises(RuntimeError, match="OIDC_CALLBACK_PATH"):
            build()
    # Only oidc mode cares: a proxy deployment never mounts a callback.
    assert Settings.model_fields["oidc_callback_path"].default == "/auth/callback"


@pytest.mark.parametrize(
    "value", ["", "0", "-1", "abc", "1e", "NaN", "Infinity", "0.00"]
)
def test_a_useless_money_ceiling_refuses_to_start(value):
    """Item 12. The setting whose whole job is to refuse money fails at boot, not
    at the first payout. Empty is the exception — it means "no ceiling", which is
    what every deployment ran before this existed."""
    if value == "":
        assert config(money_ceiling="").ceiling is None
        return
    with pytest.raises(RuntimeError, match="MONEY_CEILING"):
        config(money_ceiling=value)


def test_the_ceiling_refuses_above_and_permits_at():
    """At the ceiling is allowed; one minimum unit over is not; unset never
    refuses. `over_ceiling` is the single function every money mutation calls."""
    from app import payments

    with settings_override(money_ceiling="25.00"):
        assert payments.over_ceiling("25.00") is None
        assert payments.over_ceiling("24.99") is None
        assert payments.over_ceiling("25.01") is not None
        assert "25.00" in payments.over_ceiling("25.01")
        assert "25.01" in payments.over_ceiling("25.01")
        # Fail CLOSED: with a ceiling set, "could not read the number" must not
        # resolve to "therefore it is under the limit".
        assert payments.over_ceiling("not a number") is not None
        # An absent amount is the site's own AMOUNT_MESSAGE, not this one.
        assert payments.over_ceiling("") is None and payments.over_ceiling(None) is None
    # Unset: nothing is ever above it.
    assert payments.over_ceiling("999999999.99") is None


def test_the_environment_strip_states_the_ceiling():
    """A refusal the operator did not know was coming reads as a broken
    console."""
    from app.web import env_badge

    with settings_override(money_ceiling="25.00"):
        assert env_badge()["ceiling"] == "25.00"
    assert env_badge()["ceiling"] == ""


async def test_static_assets_must_be_revalidated_before_they_are_reused():
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag") or response.headers.get("last-modified")
