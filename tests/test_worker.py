"""Worker loop: inbox draining, poison handling, housekeeping (plan v2 §5).

End-to-end where it matters — a delivery goes in through the signed route and
comes out as a projection and a resolved operation, exactly once
(OPERATIONS_SPEC §7.7).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app import operations, projections, worker
from app.config import get_settings
from app.db import sessionmaker
from app.models import Operation, PayoutBatchRow, Projection, WebhookEvent
from app.webhooks import store
from tests.conftest import settings_override
from tests.test_batches import seeded_batch
from tests.test_webhooks import post

ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}


def event(event_id: str, event_type: str, **data) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "apiVersion": "2",
            "mode": "sandbox",
            "createdAt": "2026-08-20T12:00:00.000Z",
            "data": data,
        }
    ).encode()


async def statuses(session) -> list[tuple[str, str]]:
    return [
        (e.event_id, e.status)
        for e in (
            await session.execute(
                select(WebhookEvent)
                .order_by(WebhookEvent.received_at)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    ]


async def projections_of(session, kind) -> list[Projection]:
    return list(
        (
            await session.execute(
                select(Projection).where(Projection.resource_kind == kind)
            )
        ).scalars()
    )


# --- event mapping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "kind"),
    [
        ("transaction.completed", "transactions"),
        ("application.approved", "applications"),
        ("order.succeeded", "orders"),
        ("virtual_account.activated", "virtual_accounts"),
        ("whitelist_recipient.registered", "whitelist_recipients"),
        ("customer.created", "customers"),
    ],
)
def test_the_kind_comes_from_the_event_type_prefix(event_type, kind):
    mapped = worker.observation(json.loads(event("evt_1", event_type, id="res_1")))
    assert mapped["resource_kind"] == kind
    assert mapped["resource_id"] == "res_1"


def test_the_status_falls_back_to_the_event_verb():
    """`data` is the resource; when it carries no `status`, the verb is all we
    have — mapped where Conduit's verb and its status vocabulary differ."""
    assert worker.observation(
        json.loads(event("e", "virtual_account.activated", id="va_1"))
    )["observed"]["status"] == "active"
    assert worker.observation(json.loads(event("e", "order.created", id="ord_1")))["observed"][
        "status"
    ] == "pending"
    # …and the resource's own status always wins over the verb.
    assert worker.observation(
        json.loads(event("e", "transaction.created", id="txn_1", status="processing"))
    )["observed"]["status"] == "processing"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "wallet.created", "data": {"id": "wal_1"}},  # kind we project nothing for
        {"type": "transaction.completed", "data": {}},  # no resource id
        {"type": "transaction.completed"},  # no data block
        {"data": {"id": "txn_1"}},  # no type
    ],
)
def test_unmappable_events_map_to_nothing(payload):
    assert worker.observation(payload) is None


@pytest.mark.parametrize(
    ("payload", "kind", "resource_id", "state"),
    [
        # Verbatim deliveries from api.sandbox.conduit.financial, 2026-08-28.
        # No event type names its resource `id`: `GET /v2/webhooks/event-types`
        # says so for all 51. Reading only `data.id` made every real delivery
        # `processed_ignored` and no projection ever advanced by webhook.
        (
            {"type": "application.approved", "mode": "sandbox",
             "data": {"applicationType": "customer_onboarding",
                      "applicationId": "app_034FCeBVXZdRabCH7fFEvi",
                      "customerId": "cus_034FCeDANniGVBQX8P3qoV"}},
            "applications", "app_034FCeBVXZdRabCH7fFEvi", "approved",
        ),
        (
            {"type": "virtual_account.activated", "mode": "sandbox",
             "data": {"virtualAccountId": "vac_034FCiCByMrhNurskZcYDB",
                      "customerId": "cus_034FCeDANniGVBQX8P3qoV"}},
            "virtual_accounts", "vac_034FCiCByMrhNurskZcYDB", "active",
        ),
        (
            {"type": "whitelist_recipient.registered", "mode": "sandbox",
             "data": {"whitelistRecipientId": "wlr_034FCr8HiZFVSsFUjdFq0J",
                      "status": "registered"}},
            "whitelist_recipients", "wlr_034FCr8HiZFVSsFUjdFq0J", "registered",
        ),
    ],
)
def test_live_events_name_the_resource_after_itself(payload, kind, resource_id, state):
    observed = worker.observation(payload)
    assert observed is not None
    assert observed["resource_kind"] == kind
    assert observed["resource_id"] == resource_id
    assert observed["observed"]["status"] == state


# --- RFI events --------------------------------------------------------
#
# **SYNTHETIC, and deliberately so.** No RFI has ever been observed in any
# environment this console can reach — none can be created there (Conduit's
# compliance side publishes them), so no live delivery exists to capture. What
# IS live is the shape: `GET /v2/webhooks/event-types` on
# api.sandbox.conduit.financial (read-only, 2026-08-30) lists exactly these six
# `rfi.*` types, all `lifecycle: beta`, `category: compliance`, each with
# `exampleData` of exactly `{"rfiId": "rfi_2xKjF9mQb7vN4hL1pR3w8t"}` and a
# description ending "Fetch details via GET /v2/rfis/{id}". The payloads below
# are that `exampleData` in the live delivery envelope
# (`fixtures/webhook_event_live_application_approved.json`). The pinned spec's
# subscription enum lists the same six names.
#
# The consequence these assertions exist to pin: an RFI delivery carries **no
# status**, so the event-type suffix is the entire state — and the sweep
# (`test_reconciliation.py`) is what fills in everything else.
RFI = "rfi_2xKjF9mQb7vN4hL1pR3w8t"


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        ("rfi.published", "open"),
        ("rfi.more_info_requested", "open"),  # a new round re-opens an answered one
        ("rfi.response_submitted", "responded"),
        ("rfi.resolved", "resolved"),
        ("rfi.cancelled", "cancelled"),
    ],
)
def test_an_rfi_event_maps_to_the_rfis_kind_and_a_real_status(event_type, state):
    mapped = worker.observation(json.loads(event("evt_1", event_type, rfiId=RFI)))
    assert mapped["resource_kind"] == "rfis"
    assert mapped["resource_id"] == RFI
    assert mapped["observed"]["status"] == state
    # Every status this table produces is in Conduit's own 5-status vocabulary,
    # so none of them renders as `Unknown: …`.
    assert state in projections.STATE_RANKS["rfis"]


def test_a_deadline_extension_asserts_no_status():
    """It moves `dueAt`. Inventing a status from it would be inventing one — and
    an unmapped suffix would have stored `deadline_extended` as the state."""
    mapped = worker.observation(json.loads(event("evt_1", "rfi.deadline_extended", rfiId=RFI)))
    assert mapped["resource_kind"] == "rfis"
    assert mapped["observed"]["status"] is None


# --- draining the inbox ------------------------------------------------------------------


async def test_an_event_becomes_a_projection(session):
    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))

    assert await worker.process_pending(session) == {"processed": 1}
    assert await statuses(session) == [("evt_1", "processed")]

    (row,) = await projections_of(session, "transactions")
    assert (row.state, row.resource_id) == ("completed", "txn_1")
    assert row.observed_at == datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


async def test_an_rfi_event_becomes_an_rfis_projection(session):
    """The whole pipe for the kind that has never been seen live: the delivery
    names its resource `rfiId` and carries no status at all."""
    await store(session, event("evt_1", "rfi.published", rfiId=RFI))

    assert await worker.process_pending(session) == {"processed": 1}
    (row,) = await projections_of(session, "rfis")
    assert (row.state, row.resource_id) == ("open", RFI)


async def test_an_unsubscribed_event_type_is_ignored_not_failed(session):
    await store(session, event("evt_1", "wallet.rotated", id="wal_1"))

    assert await worker.process_pending(session) == {"processed_ignored": 1}
    assert await statuses(session) == [("evt_1", "processed_ignored")]
    assert await session.scalar(select(func.count()).select_from(Projection)) == 0


async def test_out_of_order_deliveries_leave_the_terminal_state_standing(session):
    """§7.8 through the whole pipe, not just the writer."""
    await store(
        session,
        event("evt_1", "transaction.completed", id="txn_1", status="completed"),
    )
    await worker.process_pending(session)
    late = json.loads(event("evt_2", "transaction.processing", id="txn_1", status="processing"))
    late["createdAt"] = "2026-08-20T12:05:00.000Z"
    await store(session, json.dumps(late).encode())

    assert await worker.process_pending(session) == {"processed": 1}
    (row,) = await projections_of(session, "transactions")
    assert row.state == "completed"


async def test_a_poison_event_is_retried_then_failed_without_blocking_the_queue(session):
    await store(session, b"{this is not json")
    await store(session, event("evt_2", "transaction.completed", id="txn_1", status="completed"))

    # The good event goes through on the very first pass, behind the poison one.
    assert await worker.process_pending(session) == {"pending": 1, "processed": 1}
    assert len(await projections_of(session, "transactions")) == 1

    # The first pass already spent attempt 1; the last one gives up.
    for _ in range(worker.MAX_ATTEMPTS - 2):
        assert await worker.process_pending(session) == {"pending": 1}
    assert await worker.process_pending(session) == {"failed": 1}

    poison = (
        await session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.status == "failed")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert poison.attempts == worker.MAX_ATTEMPTS
    assert "JSONDecodeError" in poison.error
    assert await worker.process_pending(session) == {}  # and it stops coming back


async def test_claim_skips_rows_another_worker_holds(session):
    await store(session, event("evt_1", "customer.created", id="cus_1"))
    await store(session, event("evt_2", "customer.created", id="cus_2"))

    async with sessionmaker()() as other:
        held = (
            await other.execute(
                select(WebhookEvent)
                .where(WebhookEvent.status == "pending")
                .order_by(WebhookEvent.received_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one()
        # `other` still holds the row lock here: a second worker must step over
        # it rather than wait behind it.
        claimed = await asyncio.wait_for(worker.claim(session), timeout=10)
        assert [e.event_id for e in claimed] == ["evt_2"]
        assert held.event_id == "evt_1"
        await other.rollback()

    assert [e.event_id for e in await worker.claim(session)] == ["evt_1"]


# --- §7.7 duplicate delivery, end to end ---------------------------------------------------


async def test_a_redelivered_event_produces_one_row_and_one_application(session):
    raw = event("evt_1", "application.approved", id="app_1", status="approved")
    for _ in range(3):
        assert (await post(raw)).status_code == 200

    assert await worker.process_pending(session) == {"processed": 1}
    assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    (row,) = await projections_of(session, "applications")
    assert (row.resource_id, row.state) == ("app_1", "approved")


async def test_a_webhook_confirms_an_unknown_outcome_operation(session):
    """The payout whose response was lost, settled by the event Conduit sends
    for it — payouts emit `transaction.*`, not `payout.*`."""
    op, _ = await operations.start(
        session, type="payout_create", **ACTOR, path="/v2/payouts", body={"amount": "1.00"}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)

    raw = event(
        "evt_1",
        "transaction.completed",
        id="txn_1",
        status="completed",
        clientReferenceId=str(op.id),
    )
    assert (await post(raw)).status_code == 200
    assert await worker.process_pending(session) == {"processed": 1}

    resolved = (
        await session.execute(
            select(Operation).where(Operation.id == op.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (resolved.state, resolved.conduit_resource_id) == ("confirmed", "txn_1")


# --- housekeeping --------------------------------------------------------------------------


async def test_request_bodies_are_purged_after_the_retention_window(session):
    op, _ = await operations.start(
        session, type="payout_create", **ACTOR, path="/v2/payouts", body={"amount": "1.00"}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", **ACTOR, conduit_resource_id="txn_1")

    active, _ = await operations.start(
        session, type="payout_create", **ACTOR, path="/v2/payouts", body={"amount": "2.00"}
    )

    assert await operations.purge_request_bodies(session) == 0  # not old enough yet
    retention = get_retention()
    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(resolved_at=datetime.now(UTC) - timedelta(days=retention + 1))
    )
    await session.commit()

    assert await operations.purge_request_bodies(session) == 1
    rows = {
        r.id: r.request_body
        for r in (
            await session.execute(
                select(Operation).execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert rows[op.id] is None  # the terminal one lost its body…
    assert rows[active.id] == {"amount": "2.00"}  # …the live one kept it


def get_retention() -> int:
    return get_settings().op_body_retention_days


# --- the inbox's own retention ------------------------------------------------


async def age_events(session, event_ids: list[str], days: int) -> None:
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.event_id.in_(event_ids))
        .values(processed_at=datetime.now(UTC) - timedelta(days=days))
    )
    await session.commit()


async def raw_bodies(session) -> dict[str, bytes | None]:
    return {
        e.event_id: e.raw_body
        for e in (
            await session.execute(
                select(WebhookEvent).execution_options(populate_existing=True)
            )
        ).scalars()
    }


async def test_settled_deliveries_lose_their_raw_body_after_the_window(session):
    """`raw_body` carries the payee's account number, legal name and address, and
    it was the last encrypted column with no retention job."""
    for name in ("evt_old", "evt_fresh"):
        await store(
            session,
            event(name, "transaction.completed", id=name.replace("evt", "txn"), status="completed"),
        )
    await worker.process_pending(session)
    assert dict(await statuses(session)) == {"evt_old": "processed", "evt_fresh": "processed"}

    assert await worker.purge_raw_bodies(session) == 0  # both processed just now

    await age_events(session, ["evt_old"], get_settings().webhook_raw_retention_days + 1)
    assert await worker.purge_raw_bodies(session) == 1
    bodies = await raw_bodies(session)
    assert bodies["evt_old"] is None
    assert bodies["evt_fresh"] is not None
    # The row itself survives the purge — the inbox still knows what arrived and
    # what became of it.
    assert dict(await statuses(session)) == {"evt_old": "processed", "evt_fresh": "processed"}
    # And it does not purge the same row twice.
    assert await worker.purge_raw_bodies(session) == 0


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
async def test_an_unsettled_delivery_keeps_its_body_at_any_age(session, status):
    """`pending`/`processing` still have to be parsed; a `failed` event's body is
    the only evidence of why it is poison, and a human is expected to read it."""
    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))
    await session.execute(
        update(WebhookEvent).values(
            status=status, processed_at=datetime.now(UTC) - timedelta(days=3650)
        )
    )
    await session.commit()

    assert await worker.purge_raw_bodies(session) == 0
    assert (await raw_bodies(session))["evt_1"] is not None


async def test_the_housekeeping_tick_runs_the_two_new_purges(session, monkeypatch):
    """A purge function nobody calls purges nothing, and nothing else in this
    suite drives `tick` — so the wiring is pinned here rather than
    assumed. Each purge's own rules are pinned in its own test above; this is
    only that `tick` reaches them. The reconciler is stubbed out: this is the
    housekeeping half, and it makes no Conduit call.
    """
    async def no_repair(session, client):
        return None

    monkeypatch.setattr(worker.reconciliation, "reconcile_pass", no_repair)

    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))
    await worker.process_pending(session)
    await age_events(session, ["evt_1"], get_settings().webhook_raw_retention_days + 1)

    batch = await seeded_batch(session, "dispatched", age_days=get_retention() + 1)

    await worker.tick(session, client=None)

    assert (await raw_bodies(session))["evt_1"] is None
    row = (
        await session.execute(
            select(PayoutBatchRow)
            .where(PayoutBatchRow.batch_id == batch)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.payload is None


async def test_an_ignored_delivery_is_settled_too(session):
    """`processed_ignored` is an outcome, not a pending state: the worker will
    never look at those bytes again either."""
    await store(session, event("evt_1", "nothing.we.project", id="x_1"))
    await worker.process_pending(session)
    assert dict(await statuses(session)) == {"evt_1": "processed_ignored"}

    await age_events(session, ["evt_1"], get_settings().webhook_raw_retention_days + 1)
    assert await worker.purge_raw_bodies(session) == 1
    assert (await raw_bodies(session))["evt_1"] is None


# --- the loop ------------------------------------------------------------------------------


async def until(predicate, *, what: str, tries: int = 500) -> None:
    """Poll instead of sleeping a fixed amount: the loop's own tick is 1s."""
    for _ in range(tries):
        await asyncio.sleep(0.01)
        if predicate():
            return
    pytest.fail(f"timed out waiting for {what}")


async def test_the_loop_drains_the_inbox_and_stops_on_request(session):
    await store(session, event("evt_1", "transaction.completed", id="txn_1", status="completed"))
    stop = asyncio.Event()
    drained = asyncio.Event()

    async def watch():
        while await statuses(session) != [("evt_1", "processed")]:
            await asyncio.sleep(0.01)
        drained.set()

    with settings_override(reconcile_interval_seconds=3600):
        running = asyncio.create_task(worker.run(stop=stop))
        watching = asyncio.create_task(watch())
        await asyncio.wait_for(drained.wait(), timeout=10)
        stop.set()
        await asyncio.wait_for(running, timeout=10)
    await watching

    assert await statuses(session) == [("evt_1", "processed")]
    assert running.exception() is None


async def test_sigterm_shuts_the_worker_down_cleanly(session):
    """The deployment contract: a container stop is a clean stop, and the
    handlers are removed again so nothing outlives the worker."""
    with settings_override(reconcile_interval_seconds=3600):
        running = asyncio.create_task(worker.run())
        await until(
            lambda: signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL,
            what="the worker to install its signal handlers",
        )
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(running, timeout=10)

    assert running.exception() is None
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


# --- liveness: heartbeat + giving up (deploy/README §4) -------------------------------


async def test_the_heartbeat_advances_after_every_successful_pass(tmp_path):
    """The worker serves nothing, so its mtime is the only liveness signal a
    container healthcheck can read."""
    beat = tmp_path / "worker.heartbeat"
    stop = asyncio.Event()
    seen: list[int] = []

    async def work(session):
        if beat.exists():
            seen.append(beat.stat().st_mtime_ns)
        if len(seen) >= 2:
            stop.set()

    with settings_override(worker_heartbeat_path=str(beat)):
        await asyncio.wait_for(worker._every(0.01, stop, "beat", work), timeout=10)

    assert beat.exists()
    # `seen` holds the mtimes *before* each later pass wrote a new one, so the
    # final file must be newer than every one of them.
    assert seen and beat.stat().st_mtime_ns > min(seen)


async def test_no_heartbeat_file_is_written_when_none_is_configured(tmp_path):
    beat = tmp_path / "unwanted.heartbeat"
    stop = asyncio.Event()

    async def work(session):
        stop.set()

    with settings_override(worker_heartbeat_path=""):
        await asyncio.wait_for(worker._every(0.01, stop, "beat", work), timeout=10)

    assert not beat.exists()


async def test_a_loop_gives_up_after_consecutive_failed_passes(tmp_path):
    """A blip is survivable; a dropped database is not. After the limit the loop
    stops the worker, which makes the container exit non-zero and the restart
    policy — not a human — deal with it."""
    beat = tmp_path / "worker.heartbeat"
    stop = asyncio.Event()
    attempts = 0
    gave_up: list[str] = []

    async def always_fails(session):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("the world is gone")

    with settings_override(worker_max_failed_passes=3, worker_heartbeat_path=str(beat)):
        await asyncio.wait_for(
            worker._every(0.01, stop, "repair", always_fails, gave_up), timeout=10
        )

    assert attempts == 3
    assert gave_up == ["repair"]
    assert stop.is_set()
    # Nothing succeeded, so nothing was ever reported as alive.
    assert not beat.exists()


async def test_one_good_pass_resets_the_failure_count(tmp_path):
    stop = asyncio.Event()
    outcomes = [False, False, True, False, False]
    attempts = 0

    async def flaky(session):
        nonlocal attempts
        if attempts >= len(outcomes):
            stop.set()
            return
        ok = outcomes[attempts]
        attempts += 1
        if not ok:
            raise RuntimeError("transient")

    gave_up: list[str] = []
    with settings_override(worker_max_failed_passes=3):
        await asyncio.wait_for(worker._every(0.01, stop, "inbox", flaky, gave_up), timeout=10)

    # Two failures, a success, two more failures — never three in a row.
    assert gave_up == []


async def test_run_reports_failure_when_a_loop_gives_up(session):
    """`run()`'s return value is what `python -m app.worker` turns into an exit
    code, so the restart policy sees a crash rather than a clean stop."""
    with settings_override(worker_max_failed_passes=1, reconcile_interval_seconds=3600):
        # Every inbox pass fails: the session factory is fine, the work is not.
        original = worker.process_pending

        async def broken(session, **kwargs):
            raise RuntimeError("inbox is unusable")

        worker.process_pending = broken
        try:
            healthy = await asyncio.wait_for(worker.run(stop=asyncio.Event()), timeout=15)
        finally:
            worker.process_pending = original

    assert healthy is False


async def test_run_reports_success_on_a_clean_stop(session):
    stop = asyncio.Event()
    with settings_override(reconcile_interval_seconds=3600):
        running = asyncio.create_task(worker.run(stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        assert await asyncio.wait_for(running, timeout=10) is True


def test_an_event_whose_name_is_a_stage_asserts_no_status():
    """`transaction.quorum_met` is a step in signing, not a state of a payment.

    Deriving a status from the event's own name put this console's coinage in
    the slot reserved for Conduit's word — the dashboard rendered
    "Unknown: quorum_met", which reads to an operator as a status Conduit sent.
    Six of the 51 live transaction event types have this shape. The vocabulary
    is the kind's own (`projections.STATE_RANKS`, pinned to the spec by
    `tests/test_vocabulary_drift.py`), so this cannot drift apart from it.
    """
    for suffix in (
        "quorum_met",
        "awaiting_signature",
        "signature_collected",
        "awaiting_sender_information",
        "awaiting_user_signature",
        "rejected",  # not a *transaction* status either — the DTO has no such value
    ):
        observed = worker.observation(
            {"type": f"transaction.{suffix}", "data": {"transactionId": "txn_1"}}
        )
        assert observed is not None, suffix
        assert observed["observed"]["status"] is None, (
            f"transaction.{suffix} asserted {observed['observed']['status']!r} — a status "
            "Conduit never sent"
        )


def test_an_event_whose_name_IS_a_status_still_asserts_it():
    """The other half: the suffix table exists because it works for the events
    whose verb really does name a state. Killing the invention must not kill
    this, or the RFI arm goes dark (its deliveries carry no status at all)."""
    for event_type, expected in (
        ("rfi.published", "open"),
        ("rfi.response_submitted", "responded"),
        ("rfi.resolved", "resolved"),
        ("virtual_account.activated", "active"),
    ):
        prefix = event_type.split(".")[0]
        key = worker._id_key(prefix)
        observed = worker.observation({"type": event_type, "data": {key: f"{prefix}_1"}})
        assert observed is not None, event_type
        assert observed["observed"]["status"] == expected, event_type


async def test_a_stage_event_cannot_erase_a_status_already_known(session):
    """The safety property behind asserting nothing: `None` ranks below every
    real state and is non-terminal, so a late-arriving stage event must not
    reopen or blank a payment the console already saw settle."""
    for state in ("completed", "processing"):
        rid = f"txn_{state}"
        await projections.apply_observation(
            session, resource_kind="transactions", resource_id=rid,
            observed={"status": state},
        )
        stage = worker.observation(
            {"type": "transaction.quorum_met", "data": {"transactionId": rid}}
        )
        await projections.apply_observation(session, **stage)
        row = (
            await session.execute(
                select(Projection).where(Projection.resource_id == rid)
            )
        ).scalar_one()
        assert row.state == state, f"a stage event overwrote {state!r}"
