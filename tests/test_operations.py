"""Operation state machine (OPERATIONS_SPEC §1–§2, test plan §7 items 4 and 5)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update

from app import operations
from app.audit import SYSTEM_ACTOR_ID
from app.db import sessionmaker
from app.models import OPERATION_STATES, AuditEvent, Operation, OperationIntent
from app.operations import service

PATH = "/v2/payouts"
BODY = {"amount": "100.00", "asset": "USD", "recipient": "wr_1"}
OTHER_BODY = {"amount": "999.00", "asset": "USD", "recipient": "wr_1"}


async def reload(session, op_id) -> Operation:
    """Re-read the row, repopulating any ORM object expired by a rollback."""
    return (
        await session.execute(
            select(Operation).where(Operation.id == op_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def make_op(session, actor, *, state="created", body=None, type="payout_create"):
    op, is_new = await operations.start(
        session, type=type, **actor, path=PATH, body=body or {"n": uuid.uuid4().hex}
    )
    assert is_new
    if state != "created":
        await session.execute(
            update(Operation).where(Operation.id == op.id).values(state=state)
        )
        await session.commit()
        await session.refresh(op)
    return op


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


@pytest.mark.parametrize("from_state", OPERATION_STATES)
@pytest.mark.parametrize("to_state", OPERATION_STATES)
async def test_transition_matrix(session, actor, from_state, to_state):
    op_id = (await make_op(session, actor, state=from_state)).id
    legal = (from_state, to_state) in operations.LEGAL_TRANSITIONS
    if legal:
        result = await operations.transition(session, op_id, to_state, **actor)
        assert result.state == to_state
    else:
        with pytest.raises(operations.IllegalTransition):
            await operations.transition(session, op_id, to_state, **actor)
        # An illegal transition leaves the lock held; the caller rolls back.
        await session.rollback()
        assert (await reload(session, op_id)).state == from_state


async def test_transition_timestamps_and_fields(session, actor):
    op = await make_op(session, actor)
    op = await operations.transition(session, op.id, "in_flight", **actor)
    assert op.in_flight_at is not None and op.attempt_count == 1
    op = await operations.transition(
        session, op.id, "confirmed", **actor, conduit_resource_id="txn_1"
    )
    assert op.conduit_resource_id == "txn_1" and op.resolved_at is not None


async def test_unknown_records_unknown_since_and_error(session, actor):
    op = await make_op(session, actor, state="in_flight")
    op = await operations.transition(session, op.id, "outcome_unknown", **actor)
    assert op.unknown_since is not None and op.resolved_at is None
    problem = {"title": "Gateway timeout", "correlationId": "cor_1"}
    op = await operations.transition(session, op.id, "rejected", **actor, error=problem)
    assert op.error == problem


# --- double submit (spec §7.4) -------------------------------------------------


async def test_double_submit_resolves_to_existing_row(session, actor):
    first, first_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    second, second_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    assert (first_new, second_new) == (True, False)
    assert second.id == first.id
    assert second.idempotency_key == first.idempotency_key
    assert await session.scalar(select(func.count()).select_from(Operation)) == 1


async def test_two_concurrent_inserts_produce_one_row(actor):
    async def submit():
        async with sessionmaker()() as s:
            return await operations.start(
                session=s, type="payout_create", **actor, path=PATH, body=BODY
            )

    (a, a_new), (b, b_new) = await asyncio.gather(submit(), submit())
    assert a.id == b.id
    assert a.idempotency_key == b.idempotency_key
    assert {a_new, b_new} == {True, False}  # exactly one caller may call Conduit
    async with sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(Operation)) == 1


async def test_start_waits_on_an_uncommitted_duplicate(actor):
    """The insert→conflict→select path under a genuinely concurrent writer."""
    async with sessionmaker()() as a, sessionmaker()() as b:
        op_id = uuid.uuid4()
        await a.execute(
            insert(Operation).values(
                id=op_id,
                type="payout_create",
                **actor,
                request_path=PATH,
                request_hash=operations.request_hash(PATH, BODY),
                request_body=BODY,
                idempotency_key=uuid.uuid4(),
                state="created",
            )
        )  # deliberately not committed yet

        second = asyncio.create_task(
            operations.start(b, type="payout_create", **actor, path=PATH, body=BODY)
        )
        await asyncio.sleep(0.25)
        assert not second.done(), "duplicate insert should block on the partial unique index"

        await a.commit()
        op, is_new = await second
        assert (op.id, is_new) == (op_id, False)


async def test_in_flight_operation_still_blocks_a_duplicate(session, actor):
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    await operations.transition(session, first.id, "in_flight", **actor)
    again, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    assert (again.id, is_new) == (first.id, False)


async def test_changed_body_is_a_new_operation(session, actor):
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body={**BODY, "amount": "200.00"}
    )
    assert is_new and second.id != first.id
    assert second.idempotency_key != first.idempotency_key
    assert (await reload(session, first.id)).state == "created"


async def test_terminal_row_no_longer_blocks_resubmit(session, actor):
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    await operations.transition(session, first.id, "in_flight", **actor)
    await operations.transition(session, first.id, "rejected", **actor, error={"title": "no"})
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    assert is_new and second.id != first.id


# --- the intent nonce (OPERATIONS_SPEC §1) ------------------------------------------


@pytest.mark.parametrize("state", ["created", "in_flight", "confirmed", "rejected", "abandoned"])
async def test_the_same_intent_resolves_to_the_same_operation_in_every_state(
    session, actor, state
):
    """The hash guard covers *active* states only, so once an
    operation confirmed, an identical replay minted a second one and paid twice.
    The nonce has to match in every state, terminal ones included."""
    intent = uuid.uuid4()
    first, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=intent
    )
    assert is_new
    await session.execute(update(Operation).where(Operation.id == first.id).values(state=state))
    await session.commit()

    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=intent
    )
    assert is_new is False and second.id == first.id
    assert (await session.scalar(select(func.count(Operation.id)))) == 1


async def test_the_intent_wins_even_when_the_body_changed(session, actor):
    """One render, one operation — a replay that mangles a field in transit still
    resolves to what that render already produced rather than sending again."""
    intent = uuid.uuid4()
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=intent
    )
    second, is_new = await operations.start(
        session,
        type="payout_create",
        **actor,
        path=PATH,
        body={**BODY, "amount": "999.00"},
        intent=intent,
    )
    assert is_new is False and second.id == first.id


async def test_a_fresh_intent_is_a_deliberate_new_attempt(session, actor):
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
    )
    await operations.transition(session, first.id, "in_flight", **actor)
    await operations.transition(session, first.id, "confirmed", **actor)
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
    )
    assert is_new and second.id != first.id
    assert second.idempotency_key != first.idempotency_key


async def test_two_concurrent_posts_of_one_intent_produce_one_row(actor):
    """The unique index is the arbiter when both callers read "no row" at once."""
    intent = uuid.uuid4()

    async def submit():
        async with sessionmaker()() as session:
            return await operations.start(
                session, type="payout_create", **actor, path=PATH, body=BODY, intent=intent
            )

    results = await asyncio.gather(submit(), submit())
    assert {op.id for op, _ in results} == {results[0][0].id}
    assert [is_new for _, is_new in results].count(True) == 1
    async with sessionmaker()() as session:
        assert (await session.scalar(select(func.count(Operation.id)))) == 1


async def test_no_intent_still_gets_the_hash_guard(session, actor):
    """A caller with no nonce — the reconciler, an older tab — is exactly as safe
    as before: the active-state hash guard still resolves it."""
    first, _ = await operations.start(session, type="payout_create", **actor, path=PATH, body=BODY)
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    assert is_new is False and second.id == first.id
    assert first.intent is None


async def test_same_body_different_type_is_a_different_operation(session, actor):
    a, _ = await operations.start(session, type="payout_create", **actor, path=PATH, body=BODY)
    b, is_new = await operations.start(
        session, type="order_create", **actor, path=PATH, body=BODY
    )
    assert is_new and b.id != a.id


# --- the nonce that loses to the hash guard -----------------------


async def test_a_nonce_the_hash_guard_answered_is_bound_to_that_operation_forever(
    session, actor
):
    """The hole between the two guards.

    Render A creates the operation. Render B submits the identical body while A's
    operation is still active, so the *hash* guard answers it and B's own nonce
    creates nothing. The operation then confirms, which releases the hash guard.
    B's form is now re-POSTed mechanically — the lost redirect the nonce exists
    for — and before this fix it missed both guards and minted a second payment
    with a second idempotency key.
    """
    first, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
    )
    assert is_new
    losing = uuid.uuid4()
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=losing
    )
    assert is_new is False and second.id == first.id

    # Terminal: the hash guard no longer holds this body.
    await operations.transition(session, first.id, "in_flight", **actor)
    await operations.transition(session, first.id, "confirmed", **actor)

    replay, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=losing
    )
    assert is_new is False and replay.id == first.id
    assert replay.idempotency_key == first.idempotency_key
    assert (await session.scalar(select(func.count(Operation.id)))) == 1


async def test_a_fresh_nonce_after_a_refusal_is_still_a_new_operation(session, actor):
    """The other half, and the one the Execute panel's copy promises: fund an
    `INSUFFICIENT_FUNDS` order and press again. Binding *consumed* nonces must
    not bind the rendered-again ones."""
    losing = uuid.uuid4()
    first, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
    )
    second, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=losing
    )
    assert is_new is False and second.id == first.id
    await operations.transition(session, first.id, "in_flight", **actor)
    await operations.transition(
        session, first.id, "rejected", **actor, error={"title": "INSUFFICIENT_FUNDS"}
    )

    retry, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
    )
    assert is_new and retry.id != first.id
    assert retry.idempotency_key != first.idempotency_key


async def test_two_concurrent_posts_of_one_losing_nonce_map_it_once(actor):
    """The loser of a race records nothing of its own: whichever writer claimed
    the nonce is the answer everyone else already got back."""
    holder, _ = await operations.start(
        (session := sessionmaker()()),
        type="payout_create",
        **actor,
        path=PATH,
        body=BODY,
        intent=uuid.uuid4(),
    )
    await session.close()
    losing = uuid.uuid4()

    async def submit():
        async with sessionmaker()() as s:
            return await operations.start(
                s, type="payout_create", **actor, path=PATH, body=BODY, intent=losing
            )

    results = await asyncio.gather(submit(), submit())
    assert {op.id for op, _ in results} == {holder.id}
    assert [is_new for _, is_new in results] == [False, False]
    async with sessionmaker()() as s:
        assert (await s.scalar(select(func.count(Operation.id)))) == 1
        assert (
            await s.scalar(
                select(func.count()).select_from(OperationIntent).where(
                    OperationIntent.intent == losing
                )
            )
        ) == 1


async def test_a_second_claim_of_one_nonce_reports_the_conflict(actor):
    """`_remember` must answer honestly, because both "claim lost" branches of
    `start` hang off it. `ON CONFLICT DO NOTHING` reports `rowcount` -1 under
    psycopg async, so the obvious `bool(rowcount)` said True for a conflict and
    made every branch below unreachable — a race then paid twice for one nonce.
    """
    async with sessionmaker()() as s:
        holder, _ = await operations.start(
            s, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
        )
    nonce = uuid.uuid4()
    async with sessionmaker()() as a:
        assert await service._remember(a, nonce, holder.id) is True
        await a.commit()
    async with sessionmaker()() as b:
        assert await service._remember(b, nonce, holder.id) is False


async def test_claimer_commits_first_so_the_creator_discards_its_row(actor, monkeypatch):
    """A hash-guard racer maps N→holder while the creating racer holds an
    uncommitted row of its own. The creator must throw that row away and answer
    holder — one nonce, one operation, nothing sent twice."""
    async with sessionmaker()() as s:
        holder, _ = await operations.start(
            s, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
        )
    nonce = uuid.uuid4()
    original, fired = service._remember, []

    async def racing_remember(session, intent, op_id):
        if intent == nonce and op_id != holder.id and not fired:
            fired.append(op_id)
            async with sessionmaker()() as other:  # the claimer, committing first
                assert await original(other, nonce, holder.id)
                await other.commit()
        return await original(session, intent, op_id)

    monkeypatch.setattr(service, "_remember", racing_remember)
    async with sessionmaker()() as s:
        got, is_new = await operations.start(
            s, type="payout_create", **actor, path=PATH, body=OTHER_BODY, intent=nonce
        )
    assert fired, "the creating branch never reached _remember"
    assert (got.id, is_new) == (holder.id, False)
    async with sessionmaker()() as s:
        assert (await s.scalar(select(func.count(Operation.id)))) == 1  # the row did not survive
        assert (
            await s.scalar(
                select(OperationIntent.operation_id).where(OperationIntent.intent == nonce)
            )
        ) == holder.id


async def test_creator_commits_first_so_the_claimer_re_resolves(actor, monkeypatch):
    """The mirror order: holder goes terminal mid-race and a creating racer
    inserts Y under this nonce. The hash-guard branch's claim then loses, and it
    must answer Y — not the `existing` row it was about to return."""
    async with sessionmaker()() as s:
        holder, _ = await operations.start(
            s, type="payout_create", **actor, path=PATH, body=BODY, intent=uuid.uuid4()
        )
    nonce = uuid.uuid4()
    original, created = service._remember, []

    async def racing_remember(session, intent, op_id):
        if intent == nonce and op_id == holder.id and not created:
            async with sessionmaker()() as other:
                await operations.transition(
                    other, holder.id, "abandoned", **actor, detail={"reason": "probe"}
                )
                y, is_new = await operations.start(
                    other, type="payout_create", **actor, path=PATH, body=BODY, intent=nonce
                )
                assert is_new
                created.append(y.id)
        return await original(session, intent, op_id)

    monkeypatch.setattr(service, "_remember", racing_remember)
    async with sessionmaker()() as s:
        got, is_new = await operations.start(
            s, type="payout_create", **actor, path=PATH, body=BODY, intent=nonce
        )
    assert created, "the hash-guard branch never reached _remember"
    assert (got.id, is_new) == (created[0], False)
    async with sessionmaker()() as s:
        assert (
            await s.scalar(
                select(OperationIntent.operation_id).where(OperationIntent.intent == nonce)
            )
        ) == created[0]


async def test_by_intent_reads_the_table_not_the_column(session, actor):
    """The drift pin. `operations.intent` is kept as the *creating* nonce and as
    the concurrency arbiter, but it is no longer what resolves a submit — a
    reader that quietly went back to the column would pass every test above and
    silently reopen the hole, because for a creating nonce the two agree."""
    op, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=(minted := uuid.uuid4())
    )
    assert op.intent == minted
    # Point the table at nothing the column knows about; the column is untouched.
    await session.execute(
        update(OperationIntent)
        .where(OperationIntent.intent == minted)
        .values(intent=(moved := uuid.uuid4()))
    )
    await session.commit()
    assert await operations.by_intent(session, moved) is not None
    assert await operations.by_intent(session, minted) is None


def test_request_hash_is_key_order_independent():
    assert operations.request_hash(PATH, {"a": 1, "b": 2}) == operations.request_hash(
        PATH, {"b": 2, "a": 1}
    )
    assert operations.request_hash(PATH, BODY) != operations.request_hash("/v2/orders", BODY)


def test_an_unscoped_hash_is_byte_identical_to_the_one_every_form_has_always_made():
    """The `hash_scope` salt may not move the form
    paths' hashes by a byte: every mutation form in this console relies on the
    duplicate guard firing across tabs and renders, and a changed hash would
    silently reopen that window for one deploy."""
    assert operations.request_hash(PATH, BODY) == operations.request_hash(PATH, BODY, "")
    # A scope is a different question about the same body — the batch row's.
    scoped = operations.request_hash(PATH, BODY, "batch-1:2")
    assert scoped != operations.request_hash(PATH, BODY)
    assert scoped != operations.request_hash(PATH, BODY, "batch-1:3")


async def test_a_nonce_resolved_onto_another_resource_is_reported_as_elsewhere(session, actor):
    """`start` resolves an
    intent nonce on the nonce alone and does not care which resource the submit
    names — that is guard 1 doing its job — so the *caller* has to be able to
    ask whether what came back is about what it just sent. One recomputed digest
    answers it, because the path is inside the hash."""
    nonce = uuid.uuid4()
    cancel_a = f"{PATH}/txn_a/cancel"
    op, is_new = await operations.start(
        session, type="payout_cancel", **actor, path=cancel_a, body=None, intent=nonce
    )
    assert is_new and not operations.resolved_elsewhere(op, cancel_a)
    # The same nonce, the same operation type, a different payout.
    replayed, is_new = await operations.start(
        session,
        type="payout_cancel",
        **actor,
        path=f"{PATH}/txn_b/cancel",
        body=None,
        intent=nonce,
    )
    assert (replayed.id, is_new) == (op.id, False)  # `start` still resolves, by design
    assert operations.resolved_elsewhere(replayed, f"{PATH}/txn_b/cancel")


async def test_an_edited_body_under_a_spent_nonce_is_elsewhere_too(session, actor):
    """The second replay the one comparison has to catch: same resource, a body
    that changed between the render and the submit."""
    nonce = uuid.uuid4()
    op, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY, intent=nonce
    )
    replayed, is_new = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=OTHER_BODY, intent=nonce
    )
    assert (replayed.id, is_new) == (op.id, False)
    assert operations.resolved_elsewhere(replayed, PATH, OTHER_BODY)
    # The ordinary double-click — identical path, identical body — is not a
    # replay of anything and must stay silent, or every second click on every
    # guarded form would earn a refusal banner.
    assert not operations.resolved_elsewhere(replayed, PATH, BODY)


def test_the_elsewhere_check_honours_the_scope_its_caller_started_with():
    """Batch dispatch's rows may be byte-identical and are still different
    payments, so it salts the hash with `"{batch}:{row}"`. A guard that dropped
    the salt would call every row after the first a replay of the first."""
    row = Operation(
        type="payout_create",
        request_path=PATH,
        request_hash=operations.request_hash(PATH, BODY, "batch-1:2"),
    )
    assert not operations.resolved_elsewhere(row, PATH, BODY, "batch-1:2")
    assert operations.resolved_elsewhere(row, PATH, BODY, "batch-1:3")
    assert operations.resolved_elsewhere(row, PATH, BODY)


# --- the in_flight context manager ---------------------------------------------


async def test_in_flight_confirms(session, actor):
    op = await make_op(session, actor)
    async with operations.in_flight(session, op, **actor) as result:
        await result.confirmed("txn_42")
    op = await reload(session, op.id)
    assert (op.state, op.conduit_resource_id) == ("confirmed", "txn_42")
    assert await actions(session, op.id) == ["operation.created", "operation.in_flight", "operation.confirmed"]


async def test_in_flight_exception_lands_in_outcome_unknown(session, actor):
    op = await make_op(session, actor)
    with pytest.raises(TimeoutError):
        async with operations.in_flight(session, op, **actor):
            raise TimeoutError("conduit did not answer")
    op = await reload(session, op.id)
    assert op.state == "outcome_unknown" and op.unknown_since is not None


async def test_in_flight_without_recorded_result_is_outcome_unknown(session, actor):
    op = await make_op(session, actor)
    async with operations.in_flight(session, op, **actor):
        pass  # caller forgot to record — must never look like success
    assert (await reload(session, op.id)).state == "outcome_unknown"


async def test_stalled_row_can_be_retried_with_the_same_key(session, actor):
    op = await make_op(session, actor, state="stalled")
    key = op.idempotency_key
    async with operations.in_flight(session, op, **actor) as result:
        await result.confirmed("txn_after_retry")
    op = await reload(session, op.id)
    assert (op.state, op.idempotency_key, op.attempt_count) == ("confirmed", key, 1)


@pytest.mark.parametrize("from_state", OPERATION_STATES)
async def test_a_send_that_cannot_begin_is_an_advance_from_every_state_but_the_two(
    session, actor, from_state
):
    """The convergence rule, stated exhaustively so it cannot widen by
    accident: entering `in_flight` from `created` or `stalled` sends, and from
    every other state raises `OperationAdvanced` and sends nothing.

    That is the whole set, and it is the whole set for a structural reason
    rather than a chosen one. `created` and `stalled` are the only two states
    `LEGAL_TRANSITIONS` allows `-> in_flight` from, and no transition leads back
    to either, so a caller that checked one of them and then found this move
    illegal has been overtaken — by a webhook, the reconciler, the TTL job, a
    second click or a second dispatch run. None of the five is a bug in the
    caller, and there is no sixth reading available.
    """
    op = await make_op(session, actor, state=from_state)
    sendable = (from_state, "in_flight") in operations.LEGAL_TRANSITIONS
    assert sendable == (from_state in ("created", "stalled")), "the premise of the rule"

    entered = False
    if sendable:
        async with operations.in_flight(session, op, **actor) as result:
            entered = True
            await result.confirmed("txn_ok")
        assert entered
        assert (await reload(session, op.id)).state == "confirmed"
        return

    with pytest.raises(operations.OperationAdvanced) as raised:
        async with operations.in_flight(session, op, **actor):
            entered = True  # pragma: no cover - the point of the assertion below
    assert not entered, "the block that talks to Conduit must not run"
    assert (raised.value.from_state, raised.value.to_state) == (from_state, "in_flight")
    # Committed and not rolled back, for `try_transition`'s reason: `transition`
    # raises holding the row lock, that lock has to be released, and a rollback
    # would expire every ORM object the caller is still holding — starting with
    # the `op` it passed in, whose attributes are readable here without IO.
    assert op.idempotency_key is not None and op.request_path == PATH
    after = await reload(session, op.id)
    assert after.state == from_state
    assert after.attempt_count == 0 and after.in_flight_at is None


@pytest.mark.parametrize("from_state", OPERATION_STATES)
@pytest.mark.parametrize("to_state", [s for s in OPERATION_STATES if s != "in_flight"])
async def test_every_illegal_move_that_is_not_a_lost_send_is_still_a_plain_refusal(
    session, actor, from_state, to_state
):
    """The other half of the rule, and the half that keeps it honest: an illegal
    transition to anything *other* than `in_flight` is not a convergence and
    does not become one.

    `OperationAdvanced` is caught in three places — the global handler, and the
    batch dispatch loop — and each of them turns it into "carry on". Widening
    the type to any other target would put those readings behind, for instance,
    `confirmed -> rejected`: a row Conduit told us twice about, in two different
    ways, which is a real disagreement and must reach somebody. So the
    subclass is asserted absent here rather than merely not asserted present.
    """
    if (from_state, to_state) in operations.LEGAL_TRANSITIONS:
        pytest.skip("legal here; the matrix test covers it")
    op = await make_op(session, actor, state=from_state)
    with pytest.raises(operations.IllegalTransition) as raised:
        await operations.transition(session, op.id, to_state, **actor)
    await session.rollback()
    assert not isinstance(raised.value, operations.OperationAdvanced)


# --- worker helpers -------------------------------------------------------------


async def test_find_stale_in_flight(session, actor):
    fresh = await make_op(session, actor)
    await operations.transition(session, fresh.id, "in_flight", **actor)
    stale = await make_op(session, actor)
    await operations.transition(session, stale.id, "in_flight", **actor)
    await session.execute(
        update(Operation)
        .where(Operation.id == stale.id)
        .values(in_flight_at=datetime.now(UTC) - timedelta(seconds=61))  # > 30s × 2
    )
    await session.commit()

    found = await operations.find_stale_in_flight(session)
    assert [op.id for op in found] == [stale.id]


async def test_abandon_expired_created_operations(session, actor):
    old = await make_op(session, actor)
    young = await make_op(session, actor)
    await session.execute(
        update(Operation)
        .where(Operation.id == old.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=25))
    )
    await session.commit()

    assert await operations.abandon_expired(session) == 1
    assert (await reload(session, old.id)).state == "abandoned"
    assert (await reload(session, young.id)).state == "created"
    audit_row = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.operation_id == old.id, AuditEvent.action == "operation.abandoned"
            )
        )
    ).scalar_one()
    assert audit_row.actor_id == SYSTEM_ACTOR_ID


# --- audit + encryption ---------------------------------------------------------


async def test_every_transition_appends_audit(session, actor):
    op = await make_op(session, actor, state="in_flight")
    await operations.transition(session, op.id, "outcome_unknown", **actor)
    await operations.transition(session, op.id, "stalled", **actor)
    assert await actions(session, op.id) == [
        "operation.created",
        "operation.outcome_unknown",
        "operation.stalled",
    ]
    row = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "operation.stalled")
        )
    ).scalar_one()
    assert row.actor_email == actor["actor_email"]
    assert row.detail["from"] == "outcome_unknown"


async def test_request_body_is_encrypted_at_rest(session, actor):
    op, _ = await operations.start(
        session, type="payout_create", **actor, path=PATH, body=BODY
    )
    raw = await session.scalar(
        text("select request_body from operations where id = :i").bindparams(i=op.id)
    )
    assert b"wr_1" not in bytes(raw)
    assert (await reload(session, op.id)).request_body == BODY


# --- the applications list's local notes ----------------------------------------


async def submitted_onboarding(session, actor, *, legal_name, reference, resource_id):
    op, _ = await operations.start(
        session,
        type="onboarding_submit",
        **actor,
        path="/v2/onboarding",
        body={"businessInfo": {"legalName": legal_name}},
        reference=reference,
    )
    await operations.transition(session, op.id, "in_flight", **actor)
    return await operations.transition(
        session, op.id, "confirmed", conduit_resource_id=resource_id, **actor
    )


async def test_local_notes_ignore_an_operation_of_another_type_on_the_same_id(session, actor):
    await submitted_onboarding(
        session, actor, legal_name="ZZZTEST Acme", reference="the-submission", resource_id="app_1"
    )
    intruder, _ = await operations.start(
        session,
        type="document_upload",
        **actor,
        path="/v2/documents",
        body={"fileSha256": "d" * 64, "purpose": "organization_onboarding", "name": None,
              "scope": "drafty"},
        reference="not-the-submission",
    )
    await operations.transition(session, intruder.id, "in_flight", **actor)
    await operations.transition(
        session, intruder.id, "confirmed", conduit_resource_id="app_1", **actor
    )

    notes = await operations.local_notes(session, ["app_1"])
    assert notes["app_1"]["reference"] == "the-submission"
    assert notes["app_1"]["legal_name"] == "ZZZTEST Acme"


async def test_local_notes_name_the_newest_attempt_for_a_resubmitted_application(session, actor):
    """One application, two submissions. The note is the current attempt's — the
    same row the application's own page shows (`applications._operation_for`),
    which is the point: unordered, the two pages named different attempts."""
    first = await submitted_onboarding(
        session, actor, legal_name="ZZZTEST Acme", reference="first-try", resource_id="app_1"
    )
    await submitted_onboarding(
        session,
        actor,
        legal_name="ZZZTEST Acme Ltd",
        reference="second-try",
        resource_id="app_1",
    )
    # Backdating the earlier attempt also rewrites its tuple, so the physical row
    # order now disagrees with the time order — which is all it ever took.
    await session.execute(
        update(Operation)
        .where(Operation.id == first.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()

    notes = await operations.local_notes(session, ["app_1"])

    assert notes == {
        "app_1": {"reference": "second-try", "legal_name": "ZZZTEST Acme Ltd"}
    }
