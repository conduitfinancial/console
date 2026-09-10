"""Durable idempotency + unknown-outcome core (OPERATIONS_SPEC §1–§2).

Usage from a route:

    op, is_new = await operations.start(
        session, type="payout_create", actor_id=..., actor_email=...,
        path="/v2/payouts", body=body,
    )
    if not is_new:
        return redirect_to_status(op)          # double-click / second tab / restart
    async with operations.in_flight(session, op, actor_id=..., actor_email=...) as result:
        response = await client.post(op.request_path, json=op.request_body,
                                     idempotency_key=op.idempotency_key)
        await result.confirmed(response["id"])

Anything that leaves the block without a recorded result — exception, timeout,
process death — is `outcome_unknown`, never a silent success.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import get_settings
from app.models import (
    ACTIVE_STATE_SQL,
    ACTIVE_STATES,
    TERMINAL_STATES,
    Operation,
    OperationIntent,
)
from app.onboarding import drafts

log = logging.getLogger(__name__)

# OPERATIONS_SPEC §2 diagram, exhaustively.
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("created", "in_flight"),
        ("created", "abandoned"),
        ("in_flight", "confirmed"),
        ("in_flight", "rejected"),
        ("in_flight", "outcome_unknown"),
        ("outcome_unknown", "confirmed"),
        ("outcome_unknown", "rejected"),
        ("outcome_unknown", "stalled"),
        ("stalled", "in_flight"),
        # Stalled means "we stopped looking", not "we stop listening" (§2). We
        # gave up *asking* after N attempts; evidence that arrives anyway — a
        # webhook, or a reconciler read — still tells the truth about the
        # resource, and refusing to act on it would mean holding proof we
        # decline to use.
        ("stalled", "confirmed"),
        ("stalled", "rejected"),
        # The release valve for the §1 guard: a stalled row blocks identical
        # resubmits, so an admin must be able to declare it dead.
        ("stalled", "abandoned"),
    }
)


# Operation types whose confirmation means "the draft has been submitted".
# Everything else that carries a `draft_id` — a document upload from inside the
# wizard, most obviously — is work *towards* a submission: stamping the draft on
# the first uploaded passport marked it submitted, which blocked discarding it
# and started its retention clock while the operator was still filling it in.
DRAFT_SUBMITTING_TYPES = frozenset({"onboarding_submit", "customer_update"})


class IntentTypeMismatch(Exception):
    """A nonce that reached a `{found}` operation was submitted to a `{expected}` form.

    Refused rather than filtered. Treating the mismatch as "no such intent" would
    mint a *second* operation under a nonce that is already spent, which is
    exactly the double-send `operation_intents` exists to prevent; and the two
    operations would then answer to one nonce, so which one a later replay
    resolves to would depend on the order they were written. A nonce belongs to
    the render that minted it, and a render belongs to one form.
    """

    def __init__(self, intent: uuid.UUID, expected: str, found: str) -> None:
        super().__init__(f"intent {intent} reached a {found} operation, not a {expected} one")
        self.intent, self.expected, self.found = intent, expected, found


class IllegalTransition(Exception):
    def __init__(self, op_id: uuid.UUID, from_state: str, to_state: str) -> None:
        super().__init__(f"operation {op_id}: {from_state} -> {to_state} is not a legal transition")
        self.op_id, self.from_state, self.to_state = op_id, from_state, to_state


class OperationAdvanced(IllegalTransition):
    """The one lost race that is a convergence: a send that arrived too late to
    begin.

    `in_flight` is reachable from `created` and from `stalled` and from nowhere
    else, so a caller that checked one of those two and then found `X ->
    in_flight` illegal has learned exactly one thing — **the row moved under
    it**. Someone else is already doing, or has already done, what this caller
    was about to start: another confirm click, another dispatch run, a webhook,
    the reconciler, the TTL job. There is no state this can be raised from that
    means "the caller's logic is wrong about which operations are sendable",
    because `created` and `stalled` are the only states from which the attempt
    would have been right and the row cannot go back to either.

    A subclass, and the narrowest possible one — `to_state` is `in_flight` and
    nothing else — because the distinction is the whole point. Every *other*
    illegal transition still arrives as a plain `IllegalTransition`, is handled
    nowhere, and is still a 500: an operation asked to move somewhere
    `LEGAL_TRANSITIONS` refuses is a bug, and `batches.set_status`'s docstring
    makes the same ruling for the batch statuses in the same words —
    "collapsing the two would hide the second inside the first".

    Nothing is swallowed here either. It is re-raised, precisely typed, and each
    consumer decides what a convergence is worth to it: an HTTP route hands it
    to `main.operation_already_moved`, which shows the operator the operation
    rather than a 500, and the batch dispatch loop counts the row as skipped and
    goes on to the next one.
    """


def request_hash(path: str, body: dict | None, scope: str = "") -> str:
    """sha256 over path + canonical JSON body, optionally salted by `scope`.

    The idempotency key and any timestamps are *not* part of the body the caller
    passes here (OPERATIONS_SPEC §1): the key is derived from the hash, not the
    other way round.

    **`scope` is what makes two identical rows of one batch two payments**
    (added 2026-08-31). The duplicate guard asks
    "is an identical body already in flight", and for a *form* that question is
    exactly right — the same body twice is the same payment submitted twice. For
    a batch it is wrong: a file may legitimately carry the same recipient and
    amount on two lines (two invoices to one supplier), and nothing in the body
    distinguishes them, because the one per-operation value —
    `clientReferenceId` — is injected at send time and deliberately absent from
    this hash. So the caller that knows the rows are different passes what makes
    them different (`"{batch}:{row}"`), and the guard then compares like with
    like.

    This hash is **console-local**: it exists only to feed the partial unique
    index. Salting it changes nothing Conduit ever sees, and callers that pass
    no scope hash byte-identically to before.
    """
    canonical = json.dumps(body or {}, sort_keys=True, separators=(",", ":"))
    salt = f"\n{scope}" if scope else ""
    return hashlib.sha256(f"{path}\n{canonical}{salt}".encode()).hexdigest()


def resolved_elsewhere(
    op: Operation, path: str, body: dict | None = None, scope: str = ""
) -> bool:
    """Is the operation this submit resolved to about something **else**?

    `start` resolves an intent nonce on the nonce alone, before the request hash
    is even computed, and `by_intent` narrows only by operation *type* — see
    `start`'s guard 1, which is load-bearing exactly as written and is not this
    function's to change. The consequence is that a nonce spent cancelling
    payout A also answers a cancel pressed on payout B, and a nonce spent on a
    body that has since been edited answers the edited submit: in both cases
    with a real, already-confirmed operation that is about a different thing.

    Resolving that way is right — one render is one mutation, forever. Reporting
    it as "done" is not: five routes fell from `is_new=False` straight to their
    success flash (the upload, to printing the resolved `doc_` id on a chip), so
    a replayed nonce said "Payout cancelled." about a payout that is still
    pending and will settle. `start`'s job is to hand back the one operation a
    render owns; deciding what to *tell the operator* about that resolution
    belongs to the caller, and this is that decision in one place.

    **One comparison catches both replays**, because `request_hash` hashes the
    request path together with the canonical body (newline-separated, above).
    Replaying a nonce at another resource moves the path; replaying it with an
    edited body moves the body; either moves the digest. `Operation` also stores
    `request_path` on its own, but comparing that as well would be a second
    implementation of half of this check, free to drift from the first.

    Callers pass what they just submitted — never what the row stored, which
    would be comparing the row with itself. `scope` is the same `hash_scope`
    passed to `start`; the one caller that has one (batch dispatch, whose
    byte-identical rows are different payments) must pass it here too, or every
    row after the first would look like a replay of the first.
    """
    return op.request_hash != request_hash(path, body, scope)


async def by_intent(
    session: AsyncSession, intent: uuid.UUID | None, type: str | None = None
) -> Operation | None:
    """The operation a given form render **reached**, whatever state it is in.

    Reads `operation_intents`, never `operations.intent`: a nonce reaches an
    operation either by creating it or by being resolved onto it by the
    request-hash guard, and only the table records both (see `OperationIntent`).

    `type` is the submitting form's own operation type, and a mismatch is
    `IntentTypeMismatch` — a refusal, deliberately not a filter. Filtering would
    read as "this nonce reached nothing", and the caller would then mint a second
    operation under a spent nonce.
    """
    if intent is None:
        return None
    op = (
        await session.execute(
            select(Operation)
            .join(OperationIntent, OperationIntent.operation_id == Operation.id)
            .where(OperationIntent.intent == intent)
        )
    ).scalar_one_or_none()
    if op is not None and type is not None and op.type != type:
        raise IntentTypeMismatch(intent, type, op.type)
    return op


async def _remember(session: AsyncSession, intent: uuid.UUID | None, op_id: uuid.UUID) -> bool:
    """Record that this nonce reached this operation. Does not commit.

    Returns whether this call is the one that claimed the nonce. The first
    writer wins and its answer is the one `by_intent` hands every later racer: a
    nonce that is already mapped is already answered, and re-pointing it would
    be exactly the drift this table exists to make impossible.

    `True` for `intent is None` — the caller mapped nothing and nothing can be
    wrong about it.
    """
    if intent is None:
        return True
    claimed = await session.execute(
        pg_insert(OperationIntent)
        .values(intent=intent, operation_id=op_id)
        .on_conflict_do_nothing(index_elements=["intent"])
        # RETURNING, not `rowcount`: on SQLAlchemy 2 + psycopg async an
        # ON CONFLICT DO NOTHING reports rowcount -1, so `bool(rowcount)` was
        # True even for a conflict and every "claim lost" branch below was dead.
        .returning(OperationIntent.intent)
    )
    return claimed.scalar_one_or_none() is not None


async def start(
    session: AsyncSession,
    *,
    type: str,
    actor_id: str,
    actor_email: str,
    path: str,
    body: dict | None = None,
    customer_id: str | None = None,
    draft_id: uuid.UUID | None = None,
    intent: uuid.UUID | None = None,
    reference: str | None = None,
    hash_scope: str = "",
) -> tuple[Operation, bool]:
    """Insert a `created` operation, or resolve to the one this form already made.

    Returns (operation, is_new). `is_new=False` means the caller must NOT call
    Conduit; it shows that operation's status page. Two guards produce it:

    1. **The intent nonce (§1), checked first and in every state.** The form
       render that produced this POST minted a one-use uuid, so a browser that
       mechanically re-sends the same body — a connection retry, a back-button
       resubmit, a lost redirect — lands back on the *same* operation even after
       it has confirmed. Without this the hash guard released on confirmation
       and the replay paid twice. A fresh render mints a fresh intent, which is
       what makes an *intentional* resubmit a new operation.
    2. **The active-state request hash**, the cross-tab/second-render belt.

    **Both outcomes are recorded against the nonce**. Guard 2
    resolving a submit to somebody else's operation used to leave that submit's
    own nonce written down nowhere, so guard 1 could never answer it again: once
    the operation went terminal and guard 2 released, the same form's next
    mechanical re-POST minted a second payment with a second idempotency key.
    `operation_intents` now holds every nonce that reached an operation, by
    either route, and guard 1 reads it — so one render maps to one operation
    forever, whichever guard got it there.

    `hash_scope` narrows guard 2 to a caller-declared identity, and exists for
    exactly one caller: batch dispatch, whose rows may be byte-identical and are
    still different payments (see `request_hash`). Without it, row 2 of a
    two-identical-row batch resolved to row 1's still-active operation and was
    silently never sent. A form passes nothing and its guard is unchanged: for a
    form, an identical body IS the same payment.

    `reference` is the operator's console-local note. It is stored on the row
    and on nothing else: not in `body`, therefore not in `request_hash`, and
    never on the wire. So it cannot move either guard — an identical body under
    a different note is still the same payment and still resolves to the
    operation already handling it, which then keeps the note it was opened with.

    Commits.
    """
    if (existing := await by_intent(session, intent, type)) is not None:
        return existing, False

    digest = request_hash(path, body, hash_scope)
    for _ in range(3):
        op_id = uuid.uuid4()
        try:
            inserted = await session.execute(
                pg_insert(Operation)
                .values(
                    id=op_id,
                    type=type,
                    actor_id=actor_id,
                    actor_email=actor_email,
                    customer_id=customer_id,
                    draft_id=draft_id,
                    request_path=path,
                    request_hash=digest,
                    request_body=body,
                    reference=reference,
                    idempotency_key=uuid.uuid4(),
                    intent=intent,
                    state="created",
                )
                .on_conflict_do_nothing(
                    index_elements=["type", "request_hash"],
                    index_where=text(ACTIVE_STATE_SQL),
                )
                .returning(Operation.id)
            )
        except IntegrityError:
            # Two requests carrying the same intent raced past the read above.
            # The unique index picked a winner; this is the loser, and the
            # winner's row is the answer.
            await session.rollback()
            if (existing := await by_intent(session, intent, type)) is not None:
                return existing, False
            raise
        if inserted.scalar_one_or_none() is not None:
            op = await session.get_one(Operation, op_id)
            # Same transaction as the row it names, so `operations.intent` and
            # `operation_intents` cannot disagree about the creating nonce.
            #
            # A failed claim is not silently tolerated here. `uq_operations_intent`
            # arbitrates two *creating* racers, but it cannot see a racer that
            # took the hash-guard branch below and mapped this same nonce onto
            # somebody else's operation — reachable when an unrelated earlier
            # operation with this body goes terminal mid-race. Letting the
            # conflict pass would leave `by_intent` pointing at an operation this
            # render did not produce. Discard and re-resolve instead: the row we
            # just inserted has not committed, so nothing survives the rollback.
            if not await _remember(session, intent, op.id):
                await session.rollback()
                if (existing := await by_intent(session, intent, type)) is not None:
                    return existing, False
                continue
            audit.record(
                session,
                action="operation.created",
                actor_id=actor_id,
                actor_email=actor_email,
                operation_id=op.id,
                detail={"type": type, "path": path},
            )
            await session.commit()
            return op, True

        existing = (
            await session.execute(
                select(Operation).where(
                    Operation.type == type,
                    Operation.request_hash == digest,
                    Operation.state.in_(ACTIVE_STATES),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # **The losing nonce is recorded too**. This submit
            # carried a nonce of its own and the hash guard answered it with an
            # operation some *other* render created. Written nowhere, that nonce
            # was forgotten the moment this request returned — and once the
            # operation went terminal the hash guard released, so the next
            # mechanical re-POST of this same form missed both guards and paid
            # again. Recorded here, this render maps to this operation forever,
            # terminal or not, exactly as a creating render does.
            #
            # A racer that claimed the nonce first has the answer, and `start`
            # returns what `by_intent` would — always, not usually. Two racers
            # that hash-resolve at once resolve to the same row anyway (the
            # partial unique index allows only one active row per type+hash), so
            # this differs from `existing` only for a replay whose body was
            # altered in transit, which is the case the nonce is supposed to win.
            if not await _remember(session, intent, existing.id):
                existing = await by_intent(session, intent, type) or existing
        await session.commit()
        if existing is not None:
            return existing, False
        # The row we collided with went terminal before we could read it; retry.
    raise RuntimeError(f"could not create or resolve operation for {type} {digest}")


async def for_resource(
    session: AsyncSession, resource_id: str, *paths: str
) -> Operation | None:
    """The newest operation this console recorded against one Conduit resource.

    `conduit_resource_id` alone is not enough: an *action* operation (a cancel,
    an execute) only gets one when it confirms, so a cancel sitting in
    `outcome_unknown` — precisely the state whose panel the operator must see
    before pressing the button again — was invisible to the resource's own page.
    The action's request path names the resource, so it is the second matcher.
    """
    match = [Operation.conduit_resource_id == resource_id]
    if paths:
        match.append(Operation.request_path.in_(paths))
    return (
        await session.execute(
            select(Operation)
            .where(or_(*match))
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def transition(
    session: AsyncSession,
    op_id: uuid.UUID,
    to_state: str,
    *,
    actor_id: str,
    actor_email: str,
    conduit_resource_id: str | None = None,
    error: dict | None = None,
    detail: dict | None = None,
    bump_reconcile: bool = False,
) -> Operation:
    """Row-locked state transition + audit row, committed together."""
    op = (
        await session.execute(
            select(Operation)
            .where(Operation.id == op_id)
            .with_for_update()
            # populate_existing or the lock is decoration: without it a row this
            # session already loaded comes back from the identity map with the
            # state it had *before* another session (a webhook) moved it, and
            # the legality check passes on a value that is no longer true.
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if (op.state, to_state) not in LEGAL_TRANSITIONS:
        raise IllegalTransition(op_id, op.state, to_state)

    now = datetime.now(UTC)
    from_state, op.state = op.state, to_state
    if to_state == "in_flight":
        op.in_flight_at = now
        op.attempt_count += 1
        # A fresh send earns a fresh reconciliation cycle (§2). Without this a
        # `stalled → in_flight` operator retry carries the exhausted count, so a
        # lost response would wait out the last backoff step and re-stall
        # without ever performing a lookup.
        op.reconcile_count = 0
        op.unknown_since = None
    elif to_state == "outcome_unknown":
        op.unknown_since = now
    else:
        op.resolved_at = now
    if conduit_resource_id is not None:
        op.conduit_resource_id = conduit_resource_id
    if error is not None:
        op.error = error
    if bump_reconcile:
        op.reconcile_count += 1
    if to_state == "confirmed" and op.draft_id is not None and op.type in DRAFT_SUBMITTING_TYPES:
        # The form-born half of "sensitive fields purged after successful
        # submission" (plan v2 §3). Here rather than at the web call site
        # because a submission can be confirmed by three different callers —
        # the recorder, the reconciler, or a webhook observation — and all three
        # come through this function. Same transaction as the transition.
        await drafts.submitted(session, op.draft_id)

    audit.record(
        session,
        action=f"operation.{to_state}",
        actor_id=actor_id,
        actor_email=actor_email,
        operation_id=op.id,
        detail={"from": from_state, "to": to_state, **(detail or {})},
    )
    await session.commit()
    return op


async def try_transition(
    session: AsyncSession, op_id: uuid.UUID, to_state: str, **kwargs
) -> Operation | None:
    """`transition`, but a concurrent resolver winning is not an error.

    Three callers race for the same row: the web recorder finishing its HTTP
    call, the reconciler, and a webhook applying an observation. The row lock
    orders them; the loser then finds a state that makes its own transition
    illegal — which means the row already says something *truer* than what the
    loser was about to write. That is convergence, not failure. Returns None
    when it converged, so callers can count it.

    Commit rather than roll back: `transition` raises before any DML, the row
    lock still has to be released, and a rollback would expire every ORM object
    the caller is holding.
    """
    try:
        return await transition(session, op_id, to_state, **kwargs)
    except IllegalTransition as converged:
        await session.commit()
        log.info(
            "operation %s: already %s, dropping the %s we were about to record",
            op_id,
            converged.from_state,
            to_state,
        )
        return None


class _Result:
    """Recorder handed to the `in_flight` block. Exactly one call, at most."""

    def __init__(
        self, session: AsyncSession, op_id: uuid.UUID, actor_id: str, actor_email: str
    ) -> None:
        # Holds the id, not the ORM object: a rollback in the caller's block
        # would expire the object and make attribute access do IO.
        self._session, self._op_id = session, op_id
        self._actor = {"actor_id": actor_id, "actor_email": actor_email}
        self.recorded = False

    async def _record(self, state: str, **kwargs) -> Operation:
        self.recorded = True
        op = await try_transition(self._session, self._op_id, state, **self._actor, **kwargs)
        if op is not None:
            return op
        # Converged: a webhook (or the reconciler) resolved this row from
        # Conduit's own account of it while our HTTP call was still open. Their
        # answer wins — and the operator must not get a 500 for it, because a
        # 500 on a submit is exactly what provokes the resubmit that pays twice.
        return (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == self._op_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def confirmed(self, conduit_resource_id: str | None = None, **detail) -> Operation:
        return await self._record(
            "confirmed", conduit_resource_id=conduit_resource_id, detail=detail or None
        )

    async def rejected(self, error: dict, **detail) -> Operation:
        return await self._record("rejected", error=error, detail=detail or None)

    async def unknown(self, **detail) -> Operation:
        return await self._record("outcome_unknown", detail=detail or None)


@asynccontextmanager
async def in_flight(session: AsyncSession, op: Operation, *, actor_id: str, actor_email: str):
    """`created|stalled → in_flight → (caller's Conduit call) → recorded result`.

    Each step commits before the next, so a crash anywhere leaves a row the
    reconciler can find. An unrecorded or raising block lands in
    `outcome_unknown`.
    """
    op_id = op.id
    try:
        await transition(session, op_id, "in_flight", actor_id=actor_id, actor_email=actor_email)
    except IllegalTransition as raced:
        # **The entry is `try_`-shaped now**, for `try_transition`'s
        # reason and stated in its words: the row already says something truer
        # than what this caller was about to write. Every caller of this block
        # reads a state, decides it is sendable, and only then arrives here, and
        # not one of those reads is under the row lock — `transition`'s
        # `SELECT ... FOR UPDATE` is the first and only place the question is
        # settled. So the loser of any such race landed here, and landed as a
        # bare `IllegalTransition` with no handler anywhere: a 500 on a submit,
        # which is exactly what provokes the resubmit that pays twice.
        #
        # Re-raised rather than swallowed, because this block cannot answer for
        # its callers: a route owes the operator a page and the dispatch loop
        # owes the batch its next row. What changes is that the answer is no
        # longer guesswork — `OperationAdvanced` says "somebody else has this",
        # and everything else `transition` refuses still arrives as the plain
        # exception it was.
        #
        # Commit rather than roll back, exactly as `try_transition` does:
        # `transition` raises before any DML, the row lock still has to be
        # released, and a rollback would expire every ORM object the caller is
        # holding — including the `op` it passed in.
        await session.commit()
        log.info(
            "operation %s: already %s, not starting the send we were about to make",
            op_id,
            raced.from_state,
        )
        raise OperationAdvanced(op_id, raced.from_state, "in_flight") from None
    result = _Result(session, op_id, actor_id, actor_email)
    try:
        yield result
    except BaseException as exc:
        if not result.recorded:
            await session.rollback()  # the block may have left a failed transaction
            await result.unknown(reason=type(exc).__name__)
        raise
    if not result.recorded:
        await result.unknown(reason="no result recorded")


async def find_stale_in_flight(
    session: AsyncSession, *, now: datetime | None = None
) -> Sequence[Operation]:
    """`in_flight` rows older than OP_CLIENT_TIMEOUT × OP_STALE_INFLIGHT_FACTOR —
    the process died mid-call (OPERATIONS_SPEC §2). The reconciler transitions
    them to `outcome_unknown`."""
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=get_settings().stale_inflight_seconds)
    return (
        (
            await session.execute(
                select(Operation).where(
                    Operation.state == "in_flight", Operation.in_flight_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )


async def abandon_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker TTL job: `created` rows never sent within OP_CREATED_TTL → abandoned."""
    cutoff = (now or datetime.now(UTC)) - timedelta(
        seconds=get_settings().op_created_ttl_seconds
    )
    expired = (
        (
            await session.execute(
                select(Operation.id).where(
                    Operation.state == "created", Operation.created_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    for op_id in expired:
        await transition(
            session,
            op_id,
            "abandoned",
            actor_id=audit.SYSTEM_ACTOR_ID,
            actor_email=audit.SYSTEM_ACTOR_EMAIL,
            detail={"reason": "created_ttl"},
        )
    return len(expired)


async def local_notes(session: AsyncSession, resource_ids: Sequence[str]) -> dict[str, dict]:
    """`{conduit resource id: {"reference", "legal_name"}}` for one rendered page.

    Two columns of the applications list come from here, because an application
    DTO carries neither. `reference` is the operator's own note: Conduit's
    `clientReferenceId` on the resource is `op.id` — `execute.outbound_body`
    overwrites the key with it — so the DTO's copy is this console's id and
    never what the operator typed. `legal_name` is the entity that was
    onboarded, read back out of the submitted body, which is the only place it
    exists before an approval turns it into a customer with a name.

    One statement per render, keyed on the ids already on screen — never a query
    per row. A body dropped by retention (`OP_BODY_RETENTION`) simply yields no
    name, and the template renders the dash it renders for every other miss.
    """
    ids = [i for i in resource_ids if i]
    if not ids:
        return {}
    rows = await session.execute(
        select(Operation.conduit_resource_id, Operation.reference, Operation.request_body)
        # `conduit_resource_id` is not unique across types.
        .where(Operation.type == "onboarding_submit", Operation.conduit_resource_id.in_(ids))
        # Newest attempt first, one note per resource. A resubmission leaves one
        # resource with several operations, and the current attempt is the note —
        # the row the resource's own page shows (`applications._operation_for`).
        # Unordered, whichever row Postgres happened to return last won instead.
        .order_by(Operation.created_at.desc(), Operation.id.desc())
    )
    notes: dict[str, dict] = {}
    for resource_id, reference, body in rows:
        key = str(resource_id)
        if key in notes:
            continue
        info = body.get("businessInfo") if isinstance(body, dict) else None
        notes[key] = {
            "reference": reference,
            "legal_name": str((info or {}).get("legalName") or "").strip() or None,
        }
    return notes


async def purge_request_bodies(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker retention job (OPERATIONS_SPEC §6): drop `request_body` **and
    `error`** once the operation has been `confirmed`/`rejected`/`abandoned` for
    OP_BODY_RETENTION days.

    The body only exists so the reconciler can replay it byte-for-byte, and it is the
    one column holding customer data. `error` is the second: Conduit's problem detail
    echoes submitted field values back — "recipient accountNumber 1234… is invalid" — so
    a purge that kept it kept the coordinates it was meant to drop, forever. Both go,
    together, on the same cutoff.

    `stalled` is deliberately NOT in that set: it is retryable, and purging it
    would turn the promised byte-identical retry into `json=None`. A stalled row
    keeps its body until it resolves or an admin abandons it.

    No audit row: this is retention, not an actor's decision on the operation,
    and the row itself records that the body is gone.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(
        days=get_settings().op_body_retention_days
    )
    purged = await session.execute(
        update(Operation)
        .where(
            Operation.state.in_(TERMINAL_STATES),
            Operation.resolved_at < cutoff,
            or_(Operation.request_body.is_not(None), Operation.error.is_not(None)),
        )
        .values(request_body=None, error=None)
    )
    await session.commit()
    return purged.rowcount
