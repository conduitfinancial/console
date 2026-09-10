"""`apply_observation` — the single projection writer (OPERATIONS_SPEC §4, §7.8–9).

Every test here is a variant of one question: can two observations arriving in
the wrong order, twice, or at the same instant leave the projection or the
operation saying something untrue?
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app import operations, projections, reconciliation
from app.conduit import ConduitClient
from app.operations import service as operations_service
from app.db import sessionmaker
from app.models import AuditEvent, Operation, Projection

ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}
T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


async def observe(session, kind="transactions", resource="txn_1", *, at=T0, **observed):
    return await projections.apply_observation(
        session, resource_kind=kind, resource_id=resource, observed=observed, observed_at=at
    )


async def projection(session, kind="transactions", resource="txn_1") -> Projection:
    return (
        await session.execute(
            select(Projection)
            .where(Projection.resource_kind == kind, Projection.resource_id == resource)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- the ladders ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "lower", "higher"),
    [
        ("transactions", "pending", "processing"),
        ("transactions", "processing", "completed"),
        ("transactions", "pending", "failed"),
        ("applications", "pending", "processing"),
        ("applications", "processing", "approved"),
        ("orders", "pending", "succeeded"),
        ("virtual_accounts", "pending_activation", "active"),
        ("virtual_accounts", "active", "disabled"),
        ("whitelist_recipients", "pending_review", "registered"),
        ("whitelist_recipients", "registered", "revoked"),
        ("rfis", "draft", "open"),
        ("rfis", "open", "resolved"),
        ("rfis", "responded", "cancelled"),
    ],
)
async def test_a_state_advances_but_never_regresses(session, kind, lower, higher):
    resource = f"{kind}_1"
    assert await observe(session, kind, resource, status=lower)
    assert await observe(session, kind, resource, at=T0 + timedelta(minutes=1), status=higher)
    assert (await projection(session, kind, resource)).state == higher

    # …and the same pair replayed in the wrong order changes nothing.
    assert not await observe(
        session, kind, resource, at=T0 + timedelta(minutes=2), status=lower
    )
    assert (await projection(session, kind, resource)).state == higher


async def test_a_rejection_is_terminal_even_at_registered_rank(session):
    """`rejected` and `registered` share a rung; only one of them is the end."""
    await observe(session, "whitelist_recipients", "wr_1", status="rejected")
    assert not await observe(
        session,
        "whitelist_recipients",
        "wr_1",
        at=T0 + timedelta(hours=1),
        status="registered",
    )
    assert (await projection(session, "whitelist_recipients", "wr_1")).state == "rejected"


async def test_an_rfi_reopened_for_another_round_goes_back_to_open(session, caplog):
    """`rfi.more_info_requested` after `rfi.response_submitted` is a real Conduit
    transition, not a regression — which is why `open` and `responded` share a
    rank. Ranking `responded` higher would drop this delivery
    *and* refuse the stale sweep's re-read of the same truth, for ever."""
    await observe(session, "rfis", "rfi_1", status="open")
    assert await observe(session, "rfis", "rfi_1", at=T0 + timedelta(minutes=1), status="responded")
    with caplog.at_level(logging.WARNING, logger="app.projections"):
        assert await observe(session, "rfis", "rfi_1", at=T0 + timedelta(minutes=2), status="open")

    assert (await projection(session, "rfis", "rfi_1")).state == "open"
    # The cost of the equal rank: each round-trip is logged as drift, which is
    # a true description of an equal-rank value change.
    assert "kept the newer" in caplog.text


async def test_a_resolved_rfi_is_never_reopened_by_a_late_delivery(session):
    """Terminality is what stops the ping-pong: `resolved`/`cancelled` end it."""
    await observe(session, "rfis", "rfi_1", status="resolved")
    assert not await observe(session, "rfis", "rfi_1", at=T0 + timedelta(hours=1), status="open")
    assert (await projection(session, "rfis", "rfi_1")).state == "resolved"


async def test_an_rfi_observation_resolves_no_operation(session):
    """OPERATIONS_SPEC §5: nothing this console calls creates an RFI, and the one
    operation that touches one (`rfi_respond`) only earns its
    `conduit_resource_id` at confirmation — after the states the resolver
    matches. So an `rfi.*` observation has nothing to resolve, even when the id
    it names is the one the operation is about."""
    op = await operations.start(
        session,
        type="rfi_respond",
        path="/v2/rfis/rfi_1/responses",
        body={"message": "attached"},
        **ACTOR,
    )
    op = op[0]
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)

    await observe(session, "rfis", "rfi_1", status="responded")

    await session.refresh(op)
    assert op.state == "outcome_unknown"


# --- §7.8 out of order ----------------------------------------------------------------


async def test_completed_then_processing_stays_completed(session, caplog):
    await observe(session, status="completed", amount="10.00")
    with caplog.at_level(logging.WARNING, logger="app.projections"):
        applied = await observe(session, at=T0 + timedelta(minutes=5), status="processing")

    assert not applied
    row = await projection(session)
    assert (row.state, row.payload["amount"]) == ("completed", "10.00")
    assert "drift" in caplog.text and "ignored" in caplog.text


async def test_conflicting_terminal_values_keep_the_newer_and_log_drift(session, caplog):
    """Equal rank, both terminal, different answers — the newer wins, loudly."""
    await observe(session, status="completed")
    with caplog.at_level(logging.WARNING, logger="app.projections"):
        applied = await observe(session, at=T0 + timedelta(minutes=5), status="failed")

    assert applied
    assert (await projection(session)).state == "failed"
    assert "drift" in caplog.text and "kept the newer" in caplog.text


async def test_an_older_conflicting_value_is_dropped(session):
    await observe(session, at=T0 + timedelta(minutes=5), status="failed")
    assert not await observe(session, at=T0, status="completed")
    assert (await projection(session)).state == "failed"


async def test_a_duplicate_observation_is_a_no_op(session):
    assert await observe(session, status="processing")
    assert not await observe(session, status="processing")
    assert await session.scalar(select(func.count()).select_from(Projection)) == 1


# --- unknown vocabulary (plan v2 §7) ---------------------------------------------------


async def test_an_unknown_state_is_stored_verbatim_and_never_terminal(session):
    assert await observe(session, status="warp_speed", note="from the future")
    row = await projection(session)
    assert (row.state, row.payload["note"]) == ("warp_speed", "from the future")
    assert not projections.is_terminal("transactions", "warp_speed")
    assert projections.rank("transactions", "warp_speed") == 0

    # It ranks as the lowest non-terminal rung, so a real state still lands…
    assert await observe(session, at=T0 + timedelta(minutes=1), status="completed")
    assert (await projection(session)).state == "completed"
    # …and it cannot pull a terminal projection back.
    assert not await observe(session, at=T0 + timedelta(minutes=2), status="warp_speed")


async def test_an_unknown_kind_is_stored_and_ordered_by_time(session):
    assert await observe(session, "widgets", "wid_1", status="spinning")
    assert not await observe(session, "widgets", "wid_1", at=T0 - timedelta(days=1), status="idle")
    assert await observe(session, "widgets", "wid_1", at=T0 + timedelta(days=1), status="idle")
    assert (await projection(session, "widgets", "wid_1")).state == "idle"


async def test_a_stateless_observation_never_crashes(session):
    assert await observe(session, "customers", "cus_1", legalName="Acme")
    assert (await projection(session, "customers", "cus_1")).state is None


# --- operation resolution --------------------------------------------------------------


async def unknown_payout(session) -> Operation:
    op, _ = await operations.start(
        session, type="payout_create", **ACTOR, path="/v2/payouts", body={"amount": "1.00"}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    return op


async def reload(session, op_id) -> Operation:
    return (
        await session.execute(
            select(Operation).where(Operation.id == op_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_an_observation_confirms_the_operation_it_belongs_to(session):
    op = await unknown_payout(session)
    await observe(session, status="completed", clientReferenceId=str(op.id))

    resolved = await reload(session, op.id)
    assert (resolved.state, resolved.conduit_resource_id) == ("confirmed", "txn_1")


async def test_an_observation_matches_on_the_recorded_resource_id_too(session):
    op = await unknown_payout(session)
    op.conduit_resource_id = "txn_1"
    await session.commit()

    await observe(session, status="completed")  # no clientReferenceId in this payload
    assert (await reload(session, op.id)).state == "confirmed"


async def test_an_already_terminal_operation_is_untouched(session):
    op = await unknown_payout(session)
    await operations.transition(
        session, op.id, "rejected", **ACTOR, error={"type": "REFUSED"}
    )

    await observe(session, status="completed", clientReferenceId=str(op.id))
    resolved = await reload(session, op.id)
    assert (resolved.state, resolved.error) == ("rejected", {"type": "REFUSED"})


async def test_an_unrelated_reference_resolves_nothing(session):
    op = await unknown_payout(session)
    await observe(session, status="completed", clientReferenceId="not-a-uuid")
    assert (await reload(session, op.id)).state == "outcome_unknown"


# --- §7.9 webhook vs reconciler ---------------------------------------------------------


async def confirmations(session, op_id) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.operation_id == op_id, AuditEvent.action == "operation.confirmed")
    )


async def test_concurrent_observers_produce_one_confirmed_transition(session):
    """Two deliveries of the same settlement at the same instant. The row lock
    decides; the loser finds the transition already made and drops it."""
    op = await unknown_payout(session)

    async def observer():
        async with sessionmaker()() as own:
            return await projections.apply_observation(
                own,
                resource_kind="transactions",
                resource_id="txn_1",
                observed={"status": "completed", "clientReferenceId": str(op.id)},
                observed_at=T0,
            )

    applied = await asyncio.gather(observer(), observer())

    assert sum(applied) == 1  # exactly one write advanced the projection
    assert (await reload(session, op.id)).state == "confirmed"
    assert await session.scalar(select(func.count()).select_from(Projection)) == 1
    assert await confirmations(session, op.id) == 1


async def settled_payout(session) -> tuple[Operation, dict, ConduitClient]:
    """An `outcome_unknown` payout that Conduit has, in fact, already settled —
    and which both the reconciler and a webhook are about to notice."""
    op = await unknown_payout(session)
    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(unknown_since=datetime.now(UTC) - timedelta(hours=2))  # due now
    )
    await session.commit()
    settled = {"id": "txn_1", "status": "completed", "clientReferenceId": str(op.id)}
    client = ConduitClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [settled], "meta": {}})
        )
    )
    return op, settled, client


async def deliver(settled: dict, at: datetime = T0) -> bool:
    async with sessionmaker()() as own:
        return await projections.apply_observation(
            own,
            resource_kind="transactions",
            resource_id="txn_1",
            observed=settled,
            observed_at=at,
        )


async def test_a_webhook_and_the_reconciler_racing_confirm_once(session):
    """§7.9 with the real reconciler: both are looking at the same settled
    payout, and only one of them may move the operation."""
    op, settled, client = await settled_payout(session)

    async def reconciler():
        async with sessionmaker()() as own:
            return await reconciliation.reconcile_pass(own, client)

    counts, _ = await asyncio.gather(reconciler(), deliver(settled))
    await client.aclose()

    resolved = await reload(session, op.id)
    assert (resolved.state, resolved.conduit_resource_id) == ("confirmed", "txn_1")
    assert await confirmations(session, op.id) == 1  # one transition, whoever won
    # …and one settled projection, whoever wrote it. Asserting on the *webhook's*
    # return value stopped being meaningful once the reconciler also feeds
    # `apply_observation`: if the reconciler's read lands
    # first, the webhook's older-but-identical observation is correctly a no-op.
    assert await session.scalar(select(func.count()).select_from(Projection)) == 1
    assert (
        await session.scalar(select(Projection.state).where(Projection.resource_id == "txn_1"))
    ) == "completed"
    assert counts in ({"confirmed": 1}, {"already_resolved": 1})


async def test_the_reconciler_drops_a_confirmation_a_webhook_already_made(session, monkeypatch):
    """The same race, pinned to the interleaving the row lock exists for: the
    webhook lands between the reconciler's lookup and its own transition."""
    op, settled, client = await settled_payout(session)
    real, raced = operations_service.transition, []

    async def webhook_first(*args, **kwargs):
        if not raced:  # once, and not for the webhook's own transition
            raced.append(True)
            await deliver(settled)
        return await real(*args, **kwargs)

    # Patched where every caller funnels: `try_transition` resolves this name at
    # call time, so both the reconciler and the webhook go through it.
    monkeypatch.setattr(operations_service, "transition", webhook_first)
    counts = await reconciliation.reconcile_pass(session, client)
    monkeypatch.undo()
    await client.aclose()

    assert raced and counts == {"already_resolved": 1}
    assert (await reload(session, op.id)).state == "confirmed"
    assert await confirmations(session, op.id) == 1
