"""Reconciler drills — OPERATIONS_SPEC §7 scenarios 1, 2, 3, 6, 10, 11.

Real Postgres, Conduit stubbed at the HTTP layer. The invariant every drill
checks is the same one: however the response was lost, exactly one resource
exists at Conduit and the operation ends in a truthful terminal state.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app import operations, reconciliation
from app.reconciliation.service import PROJECTION_SWEEP_LIMIT
from app.audit import SYSTEM_ACTOR_ID
from app.conduit import ConduitClient, execute_operation
from app.config import get_settings
from app.models import OPERATION_TYPES, AuditEvent, Operation, Projection

PATH = "/v2/payouts"
BODY = {"assetAmount": {"code": "USD", "amount": "100.00"}, "destination": {"id": "wr_1"}}
ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}


class FakeConduit:
    """Just enough Conduit: creates are keyed by Idempotency-Key (so a replay
    cannot produce a second resource), and the list endpoint answers the
    `clientReferenceId` lookup every §3 recipe starts with."""

    def __init__(self, *, on_post=None) -> None:
        self.resources: dict[str, dict] = {}  # idempotency key -> resource
        self.posts: list[tuple[str, str | None]] = []
        self.gets: list[httpx.URL] = []
        self.on_post = on_post
        self.sleeps: list[float] = []
        self.get_response = None  # override to test 429/5xx read behaviour

    def client(self) -> ConduitClient:
        async def sleep(delay):
            self.sleeps.append(delay)

        return ConduitClient(transport=httpx.MockTransport(self._handle), sleep=sleep)

    def create(self, request: httpx.Request) -> dict:
        key = request.headers["Idempotency-Key"]
        body = json.loads(request.content or b"{}")
        self.resources.setdefault(
            key,
            {
                "id": f"txn_{len(self.resources) + 1}",
                "status": "pending",
                "clientReferenceId": body.get("clientReferenceId"),
            },
        )
        return self.resources[key]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets.append(request.url)
            if self.get_response is not None:
                response = self.get_response(len(self.gets))
                if response is not None:
                    return response
            reference = request.url.params.get("clientReferenceId")
            items = [
                r
                for r in self.resources.values()
                if reference is None or r["clientReferenceId"] == reference
            ]
            return httpx.Response(
                200,
                json={
                    "data": items,
                    "meta": {
                        "mode": "cursor",
                        "nextCursor": None,
                        "previousCursor": None,
                        "total": len(items),
                    },
                },
            )
        self.posts.append((request.url.path, request.headers.get("Idempotency-Key")))
        if self.on_post is not None:
            outcome = self.on_post(self, request, len(self.posts))
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        return httpx.Response(202, json=self.create(request))


@contextmanager
def settings_override(**values):
    settings = get_settings()
    previous = {k: getattr(settings, k) for k in values}
    for key, value in values.items():
        setattr(settings, key, value)
    try:
        yield settings
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


async def reload(session, op_id) -> Operation:
    return (
        await session.execute(
            select(Operation).where(Operation.id == op_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def make_op(session, *, type="payout_create", path=PATH, body=None, customer_id=None):
    op, is_new = await operations.start(
        session, type=type, **ACTOR, path=path, body=body or BODY, customer_id=customer_id
    )
    assert is_new
    return op


async def age(session, op_id, *, unknown_since=None, in_flight_at=None, reconcile_count=None):
    """Push timestamps into the past instead of waiting for the backoff schedule."""
    values = {
        k: v
        for k, v in {
            "unknown_since": unknown_since,
            "in_flight_at": in_flight_at,
            "reconcile_count": reconcile_count,
        }.items()
        if v is not None
    }
    await session.execute(update(Operation).where(Operation.id == op_id).values(**values))
    await session.commit()


async def actions(session, op_id) -> list[str]:
    return list(
        (
            await session.execute(
                select(AuditEvent.action)
                .where(AuditEvent.operation_id == op_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.action)
            )
        ).scalars()
    )


LONG_AGO = datetime.now(UTC) - timedelta(hours=2)


# --- §7.1 lost response ------------------------------------------------------------


async def test_lost_response_is_resolved_by_reference_lookup(session):
    def create_then_drop(fake, request, n):
        fake.create(request)  # Conduit did the work…
        return httpx.ReadError("connection reset")  # …then the answer was lost

    fake = FakeConduit(on_post=create_then_drop)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown"

    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    op = await reload(session, op.id)
    assert (op.state, op.conduit_resource_id) == ("confirmed", "txn_1")
    assert len(fake.resources) == 1 and len(fake.posts) == 1  # exactly one payout
    assert await actions(session, op.id) == [
        "operation.created",
        "operation.in_flight",
        "operation.outcome_unknown",
        "operation.confirmed",
    ]
    confirmation = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "operation.confirmed")
        )
    ).scalar_one()
    assert confirmation.actor_id == SYSTEM_ACTOR_ID


async def test_the_reference_sent_to_conduit_is_the_operation_id(session):
    fake = FakeConduit()
    op = await make_op(session)
    await execute_operation(session, op, client=fake.client(), **ACTOR)
    assert next(iter(fake.resources.values()))["clientReferenceId"] == str(op.id)
    # …and it is injected, not stored: the hash stays the double-submit guard.
    assert "clientReferenceId" not in (await reload(session, op.id)).request_body


async def test_the_payout_lookup_sends_the_required_transaction_type(session):
    """`type` is a **required** query parameter on `/v2/transactions`. Omitting it
    made the lookup a 400 — which reads as `LookupUnavailable`, so the recipe
    never confirmed and every unresolved payout walked to `stalled`. Fail-safe,
    but never resolving."""
    fake = FakeConduit(on_post=lambda f, r, n: (f.create(r), httpx.ReadError("reset"))[1])
    client = fake.client()
    op = await make_op(session)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)

    lookup = next(url for url in fake.gets if url.path == "/v2/transactions")
    assert lookup.params.get("type") == "withdrawal"
    assert lookup.params.get("clientReferenceId") == str(op.id)
    assert (await reload(session, op.id)).state == "confirmed"


async def test_a_lost_order_create_is_found_by_its_reference_on_the_orders_list(session):
    """The conversion half of §7.1. `/v2/orders` *does* filter by
    `clientReferenceId`, so one page is the whole answer and no `type` is
    needed — unlike the payout lookup above."""
    fake = FakeConduit(on_post=lambda f, r, n: (f.create(r), httpx.ReadError("reset"))[1])
    client = fake.client()
    op = await make_op(
        session,
        type="order_create",
        path="/v2/orders",
        body={
            "quoteOptionId": "qop_1",
            "source": {"type": "virtual_account", "id": "vac_1"},
            "destination": {"type": "virtual_account", "id": "vac_2"},
        },
    )
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    lookup = next(url for url in fake.gets if url.path == "/v2/orders")
    assert lookup.params.get("clientReferenceId") == str(op.id)
    op = await reload(session, op.id)
    assert op.state == "confirmed"
    assert len(fake.resources) == 1  # exactly one order, never two


WHITELIST_PATH = "/v2/customers/cus_1/whitelist-recipients"
WHITELIST_BODY = {
    "rail": "us",
    "accountNumber": "000123456789",
    "routingNumber": "021000021",
    "relationship": "group_entity",
    "legalName": "ZZZTEST Globex Supplies LLC",
    "evidenceDocumentIds": ["doc_1"],
}


def _entry(**overrides) -> dict:
    return {
        "id": "wlr_old",
        "rail": "us",
        "accountNumber": "000123456789",
        "routingNumber": "021000021",
        "relationship": "group_entity",
        "legalName": "ZZZTEST Globex Supplies LLC",
        "status": "registered",
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        **overrides,
    }


async def _whitelist_lookup(session, entries: list[dict]) -> tuple[Operation, FakeConduit]:
    # The first send is lost; a replay (if the recipe orders one) succeeds.
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadError("reset") if n == 1 else None)
    fake.get_response = lambda n: httpx.Response(
        200,
        json={
            "data": entries,
            "meta": {"mode": "cursor", "nextCursor": None, "previousCursor": None,
                     "total": len(entries)},
        },
    )
    client = fake.client()
    op = await make_op(session, type="whitelist_create", path=WHITELIST_PATH, body=WHITELIST_BODY)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)
    return await reload(session, op.id), fake


async def test_a_whitelist_registration_is_matched_on_everything_it_stated(session):
    op, _ = await _whitelist_lookup(session, [_entry(id="wlr_new")])
    assert op.state == "confirmed" and op.conduit_resource_id == "wlr_new"


@pytest.mark.parametrize(
    "entry",
    [
        # The residue of an earlier registration of the same account — terminal,
        # so it cannot be the one this operation just made.
        _entry(status="revoked"),
        _entry(status="rejected"),
        # The same bank account, registered on another rail.
        _entry(rail="swift", accountNumber="000123456789"),
        # The same coordinates, registered as the customer's own.
        _entry(relationship="self"),
        # Registered before this operation existed.
        _entry(createdAt="2020-01-01T00:00:00.000Z"),
    ],
)
async def test_an_older_registration_of_the_same_account_never_confirms_ours(session, entry):
    """`legalName` + coordinates alone matched all five of these, so a lost
    response confirmed against a row this operation did not create — and the
    registration the operator asked for was never made."""
    op, fake = await _whitelist_lookup(session, [entry])
    # The lookup missed, so the recipe replayed with the same key: exactly one
    # registration exists, and it is ours.
    assert op.state == "confirmed"
    assert op.conduit_resource_id != entry["id"]
    assert len(fake.resources) == 1
    assert {key for _, key in fake.posts} == {str(op.idempotency_key)}


async def test_an_order_execute_confirms_once_the_order_is_past_pending(session):
    """`order_execute`'s recipe is `confirmed_unless=('pending',)`: anything past
    pending proves the execution was claimed."""
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadError("reset"))
    fake.get_response = lambda n: httpx.Response(
        200, json={"id": "ord_1", "status": "succeeded"}
    )
    client = fake.client()
    op = await make_op(session, type="order_execute", path="/v2/orders/ord_1/execute", body=None)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}
    assert (await reload(session, op.id)).state == "confirmed"
    assert fake.gets[0].path == "/v2/orders/ord_1"


async def test_an_order_cancel_that_arrived_too_late_is_a_local_rejection(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadError("reset"))
    fake.get_response = lambda n: httpx.Response(200, json={"id": "ord_1", "status": "succeeded"})
    client = fake.client()
    op = await make_op(session, type="order_cancel", path="/v2/orders/ord_1/cancel", body=None)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"rejected": 1}
    op = await reload(session, op.id)
    assert op.state == "rejected" and op.error["type"] == "RESOURCE_NOT_ACTIONABLE"
    # Minted locally, so there is no correlation id to show (OPERATIONS_SPEC §5).
    assert "correlationId" not in op.error


# --- §7.2 never arrived ------------------------------------------------------------


async def test_never_arrived_is_replayed_with_the_same_key_and_creates_once(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ConnectError("dropped") if n == 1 else None)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown" and fake.resources == {}

    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    op = await reload(session, op.id)
    assert (op.state, op.conduit_resource_id) == ("confirmed", "txn_1")
    assert len(fake.posts) == 2  # first attempt + replay
    assert {key for _, key in fake.posts} == {str(op.idempotency_key)}  # never a fresh key
    assert len(fake.resources) == 1


async def test_a_failed_lookup_never_triggers_a_replay(session):
    """The replay is safe only because the lookup proved absence. If the lookup
    itself could not run, the operation stays unknown."""
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ConnectError("dropped") if n == 1 else None)
    fake.get_response = lambda n: httpx.Response(503, json={"type": "UPSTREAM"})
    client = fake.client()

    op = await make_op(session)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"unresolved": 1}

    op = await reload(session, op.id)
    assert (op.state, op.reconcile_count) == ("outcome_unknown", 1)
    assert len(fake.posts) == 1  # no replay on a blind lookup


# --- §7.3 restart mid-call ---------------------------------------------------------


async def test_restart_mid_call_is_swept_then_resolved(session):
    fake = FakeConduit()
    client = fake.client()

    op = await make_op(session)
    # The process died between `in_flight` and recording the result…
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    fake.create(
        httpx.Request(
            "POST",
            PATH,
            headers={"Idempotency-Key": str(op.idempotency_key)},
            json={"clientReferenceId": str(op.id)},
        )
    )
    await age(session, op.id, in_flight_at=datetime.now(UTC) - timedelta(seconds=61))

    assert await reconciliation.reconcile_pass(session, client) == {"swept": 1}
    assert (await reload(session, op.id)).state == "outcome_unknown"

    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}
    assert (await reload(session, op.id)).conduit_resource_id == "txn_1"
    assert fake.posts == []  # nothing re-sent: the lookup found it


# --- §7.6 idempotency conflict -----------------------------------------------------


async def test_409_key_conflict_is_ambiguous_then_resolved_by_lookup(session):
    def conflict(fake, request, n):
        fake.create(request)  # the earlier request did land
        return httpx.Response(
            409,
            json={
                "type": "IDEMPOTENCY_KEY_CONFLICT",
                "title": "Idempotency key conflict",
                "status": 409,
                "detail": "This key was used by a request that is still in progress.",
                "resolution": "Retry the original request unchanged.",
                "correlationId": "cor_409",
            },
        )

    fake = FakeConduit(on_post=conflict)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown"  # never surfaced as success or rejection
    assert op.error is None

    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)
    op = await reload(session, op.id)
    assert (op.state, op.conduit_resource_id) == ("confirmed", "txn_1")
    assert len(fake.resources) == 1


# --- §7.10 stalled + audited manual retry ------------------------------------------


async def test_attempts_exhausted_stalls_then_a_manual_retry_reuses_the_key(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadTimeout("no answer") if n == 1 else None)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    key = op.idempotency_key
    await age(
        session,
        op.id,
        unknown_since=LONG_AGO,
        reconcile_count=get_settings().reconcile_max_attempts,
    )

    assert await reconciliation.reconcile_pass(session, client) == {"stalled": 1}
    op = await reload(session, op.id)
    assert op.state == "stalled"

    # The operator's explicit, audited retry: same row, same key.
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert (op.state, op.idempotency_key) == ("confirmed", key)
    assert {k for _, k in fake.posts} == {str(key)}
    assert len(fake.resources) == 1
    assert await actions(session, op.id) == [
        "operation.created",
        "operation.in_flight",
        "operation.outcome_unknown",
        "operation.stalled",
        "operation.in_flight",
        "operation.confirmed",
    ]


async def test_the_backoff_schedule_spaces_attempts(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadTimeout("no answer"))
    client = fake.client()
    op = await make_op(session)
    await execute_operation(session, op, client=client, **ACTOR)

    now = datetime.now(UTC)
    await age(session, op.id, unknown_since=now - timedelta(seconds=30))
    assert await reconciliation.reconcile_pass(session, client) == {}  # not due yet

    await age(session, op.id, unknown_since=now - timedelta(seconds=61))
    assert await reconciliation.reconcile_pass(session, client) == {"unresolved": 1}
    assert (await reload(session, op.id)).reconcile_count == 1

    # attempt 2 is due at ~2 minutes, not at ~1
    await age(session, op.id, unknown_since=now - timedelta(seconds=90))
    assert await reconciliation.reconcile_pass(session, client) == {}
    assert reconciliation.DELAYS_SECONDS == (60, 120, 300, 900, 3600)


# --- §4 definitive-conflict short-circuit -------------------------------------------

ONBOARDING_CONFLICT = {
    "type": "CUSTOMER_ALREADY_ONBOARDED",
    "title": "Customer already onboarded",
    "status": 409,
    "detail": "A customer with this tax ID has already completed onboarding.",
    "resolution": "Use the existing customer instead of submitting again.",
    "correlationId": "cor_dup",
    "details": {"customerId": "cus_existing"},
}


async def _onboarding(session, *, on_post) -> tuple[Operation, FakeConduit, ConduitClient]:
    fake = FakeConduit(on_post=on_post)
    client = fake.client()
    op = await make_op(
        session, type="onboarding_submit", path="/v2/onboarding", body={"taxId": "X-1"}
    )
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown"
    await age(session, op.id, unknown_since=LONG_AGO)
    return op, fake, client


async def test_a_definitive_conflict_rejects_on_the_first_reconcile_attempt(session):
    """OPERATIONS_SPEC §4. The reference lookup missed — this operation created
    nothing — and the replay's 409 names a *pre-existing other* customer. That is
    a settled refusal, so it resolves now instead of riding the backoff to
    `stalled` an hour later."""
    op, fake, client = await _onboarding(
        session,
        on_post=lambda f, r, n: httpx.ReadTimeout("no answer")
        if n == 1
        else httpx.Response(409, json=ONBOARDING_CONFLICT),
    )
    assert await reconciliation.reconcile_pass(session, client) == {"rejected": 1}

    op = await reload(session, op.id)
    assert (op.state, op.reconcile_count) == ("rejected", 1)  # the FIRST attempt
    assert op.error == ONBOARDING_CONFLICT  # the full problem-detail, verbatim
    assert op.error["details"]["customerId"] == "cus_existing"
    assert await actions(session, op.id) == [
        "operation.created",
        "operation.in_flight",
        "operation.outcome_unknown",
        "operation.rejected",
    ]
    # The lookup ran first: the short-circuit is never reached on the conflict alone.
    assert [url.path for url in fake.gets] == ["/v2/applications"]


async def test_a_lookup_hit_beats_the_conflict(session):
    """Precondition, not a preference: a replay that *would* have conflicted is
    never sent, because step 1 found the application by reference."""
    def create_then_drop(fake, request, n):
        if n == 1:
            fake.create(request)
            return httpx.ReadError("connection reset")
        return httpx.Response(409, json=ONBOARDING_CONFLICT)  # must never happen

    op, fake, client = await _onboarding(session, on_post=create_then_drop)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    op = await reload(session, op.id)
    assert op.state == "confirmed" and op.error is None
    assert len(fake.posts) == 1  # no replay: the conflict was never consulted


UNLISTED_CONFLICT = {
    "type": "PAYOUT_NOT_CANCELLABLE",
    "title": "Payout not cancellable",
    "status": 409,
    "detail": "Synthetic: a conflict type the allowlist does not name.",
    "correlationId": "cor_other",
}


async def test_an_unlisted_conflict_keeps_backing_off_and_still_reaches_stalled(session):
    """The allowlist is the rule (§4): an unlisted 409 stays ambiguous, so the
    operation keeps its backoff and the §7.10 stalled path is still reachable for
    something genuinely unresolvable."""
    assert "PAYOUT_NOT_CANCELLABLE" not in reconciliation.DEFINITIVE_CONFLICTS
    fake = FakeConduit(
        on_post=lambda f, r, n: httpx.ReadTimeout("no answer")
        if n == 1
        else httpx.Response(409, json=UNLISTED_CONFLICT)
    )
    client = fake.client()
    op = await make_op(session)  # payout_create
    op = await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)

    assert await reconciliation.reconcile_pass(session, client) == {"unresolved": 1}
    op = await reload(session, op.id)
    assert (op.state, op.error, op.reconcile_count) == ("outcome_unknown", None, 1)

    # …and once the attempts are spent it stalls, exactly as before.
    await age(
        session,
        op.id,
        unknown_since=LONG_AGO,
        reconcile_count=get_settings().reconcile_max_attempts,
    )
    assert await reconciliation.reconcile_pass(session, client) == {"stalled": 1}
    assert (await reload(session, op.id)).state == "stalled"


# --- §7.11 429 discipline + request budget -----------------------------------------


async def test_reconciler_reads_honor_retry_after(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadTimeout("no answer"))
    fake.get_response = lambda n: (
        httpx.Response(429, headers={"Retry-After": "5"}, json={"type": "RATE_LIMITED"})
        if n == 1
        else None
    )
    client = fake.client()

    op = await make_op(session)
    await execute_operation(session, op, client=client, **ACTOR)
    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)

    assert fake.sleeps == [5.0]  # waited exactly what Conduit asked for
    # the 429, its retry (which misses), then the re-lookup after the replay
    assert len(fake.gets) == 3


async def test_a_pass_stops_at_its_request_budget(session):
    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadTimeout("no answer"))
    client = fake.client()

    ops = []
    for amount in ("1.00", "2.00", "3.00"):
        op = await make_op(session, body={**BODY, "amount": amount})
        await execute_operation(session, op, client=client, **ACTOR)
        await age(session, op.id, unknown_since=LONG_AGO)
        ops.append(op)

    sent_before_the_pass = len(fake.posts)
    with settings_override(reconcile_request_budget=3):
        counts = await reconciliation.reconcile_pass(session, client)

    # The first operation spent the pass's three requests (lookup, replay,
    # re-lookup); the other two wait for the next pass rather than pile on.
    assert counts == {"unresolved": 1, "budget_exhausted": 1}
    assert (len(fake.gets), len(fake.posts) - sent_before_the_pass) == (2, 1)
    assert [(await reload(session, o.id)).state for o in ops] == ["outcome_unknown"] * 3
    # …and the ones that never ran keep their attempt allowance intact.
    assert [(await reload(session, o.id)).reconcile_count for o in ops] == [1, 0, 0]


# --- recipe coverage ----------------------------------------------------------------


def test_every_operation_type_has_a_recipe():
    assert set(reconciliation.RECIPES) == set(OPERATION_TYPES)


# --- recipe → projection (OPERATIONS_SPEC §6, Phase-2 deferral closed) ---------------


async def projection(session, kind: str, resource: str):
    from app.models import Projection

    return (
        await session.execute(
            select(Projection).where(
                Projection.resource_kind == kind, Projection.resource_id == resource
            )
        )
    ).scalar_one_or_none()


async def test_a_lookup_that_finds_a_completed_transaction_also_writes_the_projection(session):
    """A reconciler read *is* an observation — the same one a webhook carries.
    Before this, the reconciler could resolve an operation from a settled payout
    and leave the list views still showing it as pending until an unrelated
    webhook happened to arrive."""

    def create_then_drop(fake, request, n):
        fake.create(request)
        return httpx.ReadError("connection reset")

    fake = FakeConduit(on_post=create_then_drop)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    # Conduit has since settled it; the reconciler is about to read that.
    fake.resources[str(op.idempotency_key)]["status"] = "completed"

    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    assert (await reload(session, op.id)).state == "confirmed"
    projected = await projection(session, "transactions", "txn_1")
    assert projected is not None and projected.state == "completed"
    assert projected.payload["clientReferenceId"] == str(op.id)
    assert projected.observed_at is not None


async def test_the_projection_advances_rather_than_regressing_on_a_later_read(session):
    """The reconciler goes through `apply_observation` like everything else, so
    a stale read cannot walk a projection backwards."""
    from app import projections

    fake = FakeConduit(on_post=lambda f, r, n: httpx.ReadTimeout("no answer") if n == 1 else None)
    client = fake.client()

    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    # A settlement webhook lands first; the reconciler's replay still reads
    # `pending` from the stub afterwards.
    await projections.apply_observation(
        session,
        resource_kind="transactions",
        resource_id="txn_1",
        observed={"status": "completed"},
        observed_at=datetime.now(UTC),
    )

    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)

    assert (await projection(session, "transactions", "txn_1")).state == "completed"


async def test_a_by_status_read_projects_the_resource_it_read(session):
    """The other positive-observation shape: cancels and executes read the
    resource itself, so that resource lands in the projection too."""

    fake = FakeConduit(on_post=lambda f, r, n: httpx.Response(200, json={"id": "txn_9"}))
    fake.get_response = lambda n: httpx.Response(200, json={"id": "txn_9", "status": "cancelled"})
    client = fake.client()

    op = await make_op(session, type="payout_cancel", path="/v2/payouts/txn_9/cancel", body={})
    await execute_operation(
        session,
        op,
        client=ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))),
        **ACTOR,
    )
    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)

    projected = await projection(session, "transactions", "txn_9")
    assert projected is not None and projected.state == "cancelled"


async def test_a_payout_read_and_a_transaction_webhook_share_one_projection_row(session):
    """A payout IS a transaction: `GET /v2/payouts/{id}` returns the withdrawal
    branch of the unified transaction view under the same `txn_` id, and
    `transaction.*` is the only event family Conduit emits for one. Projecting
    the reconciler's payout read under its own kind would leave the console with
    two rows for one resource — and a list view free to render the stale one."""
    from app.models import Projection
    from app.worker import observation

    # The reconciler resolving a cancel: reads /v2/payouts/txn_9 → `cancelled`.
    fake = FakeConduit(on_post=lambda f, r, n: httpx.Response(200, json={"id": "txn_9"}))
    fake.get_response = lambda n: httpx.Response(200, json={"id": "txn_9", "status": "processing"})
    client = fake.client()

    op = await make_op(session, type="payout_cancel", path="/v2/payouts/txn_9/cancel", body={})
    await execute_operation(
        session,
        op,
        client=ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))),
        **ACTOR,
    )
    await age(session, op.id, unknown_since=LONG_AGO)
    fake.get_response = lambda n: httpx.Response(200, json={"id": "txn_9", "status": "cancelled"})
    await reconciliation.reconcile_pass(session, client)

    # …and the webhook for the very same payout, routed exactly as the worker
    # routes it (`transaction.*` → kind `transactions`).
    from app import projections

    arguments = observation(
        {"type": "transaction.updated", "data": {"id": "txn_9", "status": "processing"}}
    )
    assert arguments["resource_kind"] == "transactions"
    await projections.apply_observation(session, **arguments)

    assert await session.scalar(select(func.count()).select_from(Projection)) == 1
    row = await projection(session, "transactions", "txn_9")
    assert row.state == "cancelled"  # the webhook could not regress it, either


async def test_a_lookup_with_nothing_to_observe_writes_no_projection(session):
    """RFI responses and webhook endpoints have no projection kind; inventing one
    would put rows in a table the read views do not understand."""
    from app.models import Projection

    fake = FakeConduit()
    fake.get_response = lambda n: httpx.Response(
        200,
        json={
            "id": "rfi_1",
            "responses": [{"id": "rsp_1", "message": "see attached", "documentIds": []}],
        },
    )
    client = fake.client()

    op = await make_op(
        session,
        type="rfi_respond",
        path="/v2/rfis/rfi_1/responses",
        body={"message": "see attached"},
    )
    await execute_operation(
        session,
        op,
        client=ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))),
        **ACTOR,
    )
    await age(session, op.id, unknown_since=LONG_AGO)
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}

    assert (await reload(session, op.id)).conduit_resource_id == "rsp_1"
    assert await session.scalar(select(func.count()).select_from(Projection)) == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [("cancelled", "confirmed"), ("completed", "rejected"), ("pending", "replayed")],
)
async def test_payout_cancel_recipe_reads_the_payout_status(session, status, expected):
    def handle_get(n):
        return httpx.Response(200, json={"id": "pay_1", "status": status})

    fake = FakeConduit(on_post=lambda f, r, n: httpx.Response(200, json={"id": "pay_1"}))
    fake.get_response = handle_get
    client = fake.client()

    op = await make_op(session, type="payout_cancel", path="/v2/payouts/pay_1/cancel", body={})
    await execute_operation(
        session, op, client=ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))), **ACTOR
    )
    await age(session, op.id, unknown_since=LONG_AGO)
    await reconciliation.reconcile_pass(session, client)

    op = await reload(session, op.id)
    if expected == "confirmed":
        assert (op.state, op.conduit_resource_id) == ("confirmed", "pay_1")
    elif expected == "rejected":
        assert op.state == "rejected" and op.error["observedStatus"] == "completed"
    else:
        assert op.state == "confirmed" and fake.posts  # still cancellable → replayed


# --- stale-projection sweep (plan v2 §5) ----------------------------------------------


async def project(session, kind: str, resource_id: str, state: str, *, age_seconds: int = 0):
    """A projection as a webhook would have left it, optionally aged."""
    from app import projections
    from app.models import Projection

    await projections.apply_observation(
        session, resource_kind=kind, resource_id=resource_id,
        observed={"id": resource_id, "status": state},
    )
    if age_seconds:
        when = datetime.now(UTC) - timedelta(seconds=age_seconds)
        await session.execute(
            update(Projection)
            .where(Projection.resource_kind == kind, Projection.resource_id == resource_id)
            .values(updated_at=when, observed_at=when)
        )
        await session.commit()


async def age_projection(session, kind: str, resource_id: str, *, seconds: int) -> None:
    """Push one projection back into the stale window again."""
    when = datetime.now(UTC) - timedelta(seconds=seconds)
    await session.execute(
        update(Projection)
        .where(Projection.resource_kind == kind, Projection.resource_id == resource_id)
        .values(updated_at=when, observed_at=when)
    )
    await session.commit()


async def projection_state(session, kind: str, resource_id: str) -> str | None:
    from app.models import Projection

    return await session.scalar(
        select(Projection.state).where(
            Projection.resource_kind == kind, Projection.resource_id == resource_id
        )
    )


def reader(resources: dict[str, dict], calls: list | None = None) -> ConduitClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        body = resources.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"type": "NOT_FOUND", "title": "gone"})
        return httpx.Response(200, json=body)

    return ConduitClient(transport=httpx.MockTransport(handle))


async def test_a_stale_non_terminal_projection_is_repaired(session):
    """A webhook that never arrives leaves a transaction at `processing`
    forever, and nothing else looks: the operation reconciler only visits rows
    with an unresolved *operation*, which a settled resource does not have."""
    await project(session, "transactions", "txn_1", "processing", age_seconds=3600)
    calls: list = []
    client = reader({"/v2/transactions/txn_1": {"id": "txn_1", "status": "completed"}}, calls)

    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert counts["projection_repaired"] == 1
    assert await projection_state(session, "transactions", "txn_1") == "completed"
    assert calls == ["/v2/transactions/txn_1"]


async def test_a_stale_rfi_is_swept_by_id(session):
    """For RFIs the sweep is not a last-resort repair, it is the only place the
    worker ever learns anything beyond the id: an `rfi.*` delivery carries
    `{"rfiId": …}` and nothing else (live event-types probe, 2026-08-30). Here
    the round was answered and then re-opened, and only this read says so."""
    await project(session, "rfis", "rfi_1", "responded", age_seconds=3600)
    calls: list = []
    client = reader(
        {
            "/v2/rfis/rfi_1": {
                "id": "rfi_1",
                "status": "open",
                "dueAt": "2026-09-02T00:00:00.000Z",
                "subjects": [{"subjectType": "transaction", "subjectId": "txn_1"}],
            }
        },
        calls,
    )

    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert counts["projection_repaired"] == 1
    assert calls == ["/v2/rfis/rfi_1"]
    assert await projection_state(session, "rfis", "rfi_1") == "open"


async def test_an_rfi_that_arrived_already_settled_is_hydrated_exactly_once(session):
    """An `rfi.resolved` delivery carries `{"rfiId": …}` and
    nothing else, so a projection whose *first* event is terminal holds an id, a
    status and no subjects — and terminality would keep it that way for ever: a
    row that cannot say what it was about. One read fixes it permanently.
    """
    await project(session, "rfis", "rfi_done", "resolved", age_seconds=3600)
    hydrated = {
        "id": "rfi_done",
        "status": "resolved",
        "subjects": [{"subjectType": "transaction", "subjectId": "txn_1"}],
        "title": "Invoice for this payment",
    }
    calls: list = []
    client = reader({"/v2/rfis/rfi_done": hydrated}, calls)

    await reconciliation.reconcile_pass(session, client)
    assert calls == ["/v2/rfis/rfi_done"]

    row = (
        await session.execute(
            select(Projection)
            .where(Projection.resource_id == "rfi_done")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.state == "resolved"  # terminal, and it stays terminal
    assert row.payload["subjects"] == hydrated["subjects"]

    # …and now that it can say what it was about, it is settled: no second read,
    # on this tick or any later one.
    await age_projection(session, "rfis", "rfi_done", seconds=3600)
    await reconciliation.reconcile_pass(session, client)
    await client.aclose()
    assert calls == ["/v2/rfis/rfi_done"]


async def test_a_settled_projection_never_starves_a_stale_one(session):
    """The 50-row LIMIT ran before the terminal rows were
    skipped in Python, so a ledger holding 50 older settled projections filled
    every batch and the stale row behind them was never reached — not slowly,
    but never, on every tick, for as long as those 50 stayed oldest.
    """
    for index in range(PROJECTION_SWEEP_LIMIT):
        await project(session, "transactions", f"txn_old_{index}", "completed", age_seconds=7200)
    await project(session, "transactions", "txn_stuck", "processing", age_seconds=3600)
    calls: list = []
    client = reader({"/v2/transactions/txn_stuck": {"id": "txn_stuck", "status": "completed"}}, calls)

    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert calls == ["/v2/transactions/txn_stuck"], "the settled rows starved the stale one"
    assert counts["projection_repaired"] == 1
    assert await projection_state(session, "transactions", "txn_stuck") == "completed"


async def test_fresh_and_terminal_projections_are_left_alone(session):
    await project(session, "transactions", "txn_fresh", "processing")  # just observed
    await project(session, "applications", "app_done", "approved", age_seconds=3600)
    await project(session, "virtual_accounts", "va_1", "pending_activation", age_seconds=3600)
    calls: list = []
    client = reader({}, calls)

    await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    # Fresh: not due. Terminal: nothing left to observe. virtual_accounts: its
    # read path is nested under a customer the projection does not carry.
    assert calls == []


async def test_the_sweep_is_capped_by_the_shared_request_budget(session):
    for index in range(6):
        await project(session, "orders", f"ord_{index}", "pending", age_seconds=3600)
    calls: list = []
    client = reader({}, calls)  # every read 404s, so each costs exactly one request

    with settings_override(reconcile_request_budget=3):
        counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert len(calls) <= 3
    assert counts.get("projection_budget_exhausted")


async def test_an_unreadable_resource_leaves_the_projection_stale(session):
    await project(session, "orders", "ord_1", "pending", age_seconds=3600)
    async def no_wait(delay):
        return None

    client = ConduitClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})), sleep=no_wait
    )
    counts = await reconciliation.reconcile_pass(session, client)
    await client.aclose()

    assert counts["projection_unreadable"] == 1
    assert await projection_state(session, "orders", "ord_1") == "pending"  # never guessed


async def test_a_failed_projection_write_never_unconfirms_the_operation(session, monkeypatch):
    """The transition commits first; if the projection write then fails the
    operation is already correctly confirmed, and the sweep is the repair path.
    Raising here would turn a resolved operation into an unresolved one."""
    from app import projections

    def create_then_drop(fake, request, n):
        fake.create(request)
        return httpx.ReadError("connection reset")

    fake = FakeConduit(on_post=create_then_drop)
    client = fake.client()
    op = await make_op(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown"
    await age(session, op.id, unknown_since=LONG_AGO)

    async def boom(*args, **kwargs):
        raise RuntimeError("projection write failed")

    monkeypatch.setattr(projections, "apply_observation", boom)
    counts = await reconciliation.reconcile_pass(session, client)
    monkeypatch.undo()

    assert counts["confirmed"] == 1
    assert (await reload(session, op.id)).state == "confirmed"
    assert await projection_state(session, "transactions", "txn_1") is None

    # …and the next pass repairs what the failed write left behind.
    await project(session, "transactions", "txn_stale", "processing", age_seconds=3600)
    repair = reader({"/v2/transactions/txn_stale": {"id": "txn_stale", "status": "completed"}})
    counts = await reconciliation.reconcile_pass(session, repair)
    await repair.aclose()
    await client.aclose()
    assert counts["projection_repaired"] == 1
