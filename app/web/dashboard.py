"""The landing page: what needs a human, right now (plan v2 §7).

Four questions, in the order an operator asks them: what did this console send
that never got an answer, what is Conduit still deciding, what is half-typed,
and what moved recently.

Every answer comes from this installation's own database — the operations
ledger, the read projections (plan v2 §3: projections are what list views read),
and the drafts table. **This page renders completely with zero Conduit
answers.** That is the point: the one page an operator lands on when something
looks wrong must render when Conduit is the thing that is wrong. It is also
read-only — a dashboard that could mutate would be a dashboard that mutates on a
prefetch.

The one request it makes is the console's single bounded, gathered,
silent-on-failure customers read (`web.with_customer_names`), for the customer
NAMES the three panels show beside their ids — the /accounts amendment of Phase
18, one file over (human directive: "operators need names"; DESIGN.md's id-only
row is overruled). Availability survives it because the resolver's honest-miss
clause IS the degraded mode: no answer, no names, no banner, every row still on
screen with its id. **Read budget: 0 → 1** — one `GET /v2/customers?limit=25`,
on every path, whatever is in the four tables. Never one per row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import projections
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient
from app.models import ACTIVE_STATES, Draft, Operation, Projection
from app.web import conduit, db, no_second_read, render, with_customer_names

router = APIRouter()

# Ten rows is a glance; the section pages are for reading the rest.
LIMIT = 10

# What the operator does about each unresolved state — display only, never a
# gate. The pill says what the state *is*; this says whether it is theirs to act
# on. Sentences are OPERATIONS_SPEC §2's write sequence and §5's panel copy, in
# one line each: `created` is inserted before the HTTP call, `in_flight` is set
# immediately before it, `outcome_unknown` belongs to the reconciler (and never
# offers a retry), and `stalled` is retryable under the same idempotency key.
# A state not in here renders an em-dash rather than a guess — the same rule the
# status pills follow for an unknown enum.
#
# None of these repeat their pill: the pill already says "Result being confirmed"
# and "Couldn't confirm — needs attention" (`web.OPERATION_LABELS`), so a column
# echoing it would cost a column and add nothing.
NEXT_STEP = {
    # Not "queued": nothing picks a `created` row back up. OPERATIONS_SPEC §2's
    # write sequence sets `in_flight` in the same web request that inserted this
    # row, so a row still `created` is one whose process died between the two
    # commits — the reconciler only looks at `in_flight`/`outcome_unknown`, and
    # the single edge out is the worker's TTL abandon. Nothing was sent, so a
    # resubmit cannot double-pay. "Reload the form", not "submit again": a stale
    # open tab replays the same intent nonce, which `operations.start` checks
    # first and in every state, so it would resolve straight back to this dead
    # row — only a fresh render mints a new intent. The TTL is config
    # (`op_created_ttl_seconds`), so the sentence must not name a number this
    # dictionary cannot read.
    "created": (
        "The send was interrupted before it left this console — nothing reached Conduit. "
        "Reload the original form and submit again; this row expires on its own."
    ),
    "in_flight": "Sent — waiting for Conduit's answer.",
    "outcome_unknown": (
        "The reconciler is asking Conduit what happened. Nothing to do, and do not resubmit."
    ),
    "stalled": "Retrying is safe: same request, same idempotency key, so it cannot pay twice.",
}


def _age(since: datetime | None, now: datetime) -> str:
    """How long this has been someone's problem, in one cell's worth of text."""
    if since is None:
        return "—"
    # Clamped: the database's clock and this process's are not the same clock,
    # and a row stamped a second into the future must not read "-1m".
    minutes = max(0, int((now - since).total_seconds() // 60))
    if minutes >= 1440:
        return f"{minutes // 1440}d"
    if minutes >= 60:
        return f"{minutes // 60}h"
    return f"{minutes}m" if minutes else "just now"


def _customer(projection) -> dict:
    """The customer a projection's own payload names — `{id, name}`, both
    possibly `""`.

    **The row's own answer, which beats the resolver's map** (`m.customer_cell`'s
    contract): `customerId` is on every application and transaction payload, and
    `customerName` is on the transaction views (`PublicWithdrawalViewDto` and its
    siblings) but on nothing else. So a transaction row observed through a read
    already carries its name and needs no lookup, while a webhook-thin one and
    every application row fall through to the bounded map — and to the bare id
    when that misses too (DESIGN.md's honest-miss clause).
    """
    payload = projection.payload if isinstance(projection.payload, dict) else {}
    identifier = payload.get("customerId")
    name = payload.get("customerName")
    return {
        "id": identifier if isinstance(identifier, str) else "",
        "name": name if isinstance(name, str) else "",
    }


def _non_terminal(kind: str):
    """Projection rows of `kind` whose state does not end the story.

    Terminality comes from `projections.TERMINAL` — the same table the pills and
    the reconciler read — never from a second copy of the state list. `coalesce`
    because a projection created by an observation with no `status` has a NULL
    state, and NULL is not terminal (`projections.is_terminal`).
    """
    return and_(
        Projection.resource_kind == kind,
        func.coalesce(Projection.state, "").notin_(sorted(projections.TERMINAL[kind])),
    )


def _observed():
    """Projections carry the event time; `updated_at` is the fallback for a row
    written before one arrived."""
    return func.coalesce(Projection.observed_at, Projection.updated_at)


async def _count(session: AsyncSession, model, *where) -> int:
    """One number, without loading a row.

    The stat band's whole mechanism, and deliberately nothing more: same tables,
    same predicates as the lists below, still no Conduit call, and no entity
    load — an `EncryptedJSON` column is not decrypted to count the rows it is on.
    """
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    now = datetime.now(UTC)
    # One extra row, so the table can say there are more without a count query.
    #
    # Columns, not entities, here and for the drafts below. `Operation` and
    # `Draft` both carry an `EncryptedJSON` column, and loading the whole row
    # decrypts it — so rendering ten cells of type and age would reach for the
    # key and fail on the first corrupt ciphertext or rotated-away key. That
    # would take down the one page whose entire reason to exist is rendering
    # when something else is broken. Nothing below is on screen by accident:
    # the list is exactly what dashboard.html reads.
    unresolved = (
        await session.execute(
            select(
                Operation.id,
                Operation.type,
                Operation.state,
                Operation.customer_id,
                Operation.reference,
                Operation.created_at,
            )
            .where(Operation.state.in_(ACTIVE_STATES))
            .order_by(Operation.created_at)
            .limit(LIMIT + 1)
        )
    ).all()
    # `LIMIT + 1` for the same reason the operations query does it: the stat tile
    # above this table now *links* to it, so the table has to be able to say it
    # is showing ten of more rather than quietly ending at ten.
    pending = (
        (
            await session.execute(
                select(Projection)
                .where(_non_terminal("applications"))
                .order_by(_observed().desc())
                .limit(LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    recent = (
        (
            await session.execute(
                select(Projection)
                .where(Projection.resource_kind == "transactions")
                .order_by(_observed().desc())
                .limit(LIMIT)
            )
        )
        .scalars()
        .all()
    )
    # The stat band. Four counts of what the four tables below list —
    # the tables show the ten newest, these say how many there are. The first
    # cell is the only one that may take a colour, and only when it is not zero:
    # "needs a human" is the one number on this page that wants an operator's
    # eye, and DESIGN.md's rule is that a hue on screen means a state. A zero
    # stays ink, because nothing waiting is not a state to flag.
    #
    # **Each tile is a link to the population it counted, or it is not a link at
    # all**. The rule is the honesty rule applied to
    # navigation: a tile that lands somewhere showing a *different* set of rows
    # than the number it just showed is a worse lie than a tile that does not
    # navigate, because the operator now has two numbers and believes both. Each
    # `url` below was checked against the query directly above it:
    #
    # 1. Unresolved operations → `#needs-attention`, the section on this page,
    #    whose query is this same `state IN ACTIVE_STATES` (ten oldest, and the
    #    page says so). There is no operations index yet — it is in the
    #    queue — and `/applications` is a different population entirely.
    # 2. Pending applications → `#pending-applications`, same page, same
    #    `_non_terminal("applications")` predicate. NOT
    #    `/applications?status=pending&status=processing`: that reads Conduit
    #    live and would differ on three axes at once — every application ever
    #    versus the ones this console has *observed*, a NULL-state projection
    #    (an observation that carried no status) which no status filter can ask
    #    for, and any non-terminal status this build has never heard of, which
    #    `_non_terminal` counts and a two-value filter drops.
    # 3. Open drafts → `/drafts?state=open`, which filters exactly
    #    `actor_id = me AND submitted_at IS NULL` — the filter was added for
    #    this tile, because `/drafts` unfiltered also lists submitted and purged
    #    ones and would have shown more rows than the tile counted.
    # 4. Transactions observed in 24h → **still no link**, on the axes that were
    #    always the real ones. The ledger DOES have an all-kinds view now (its
    #    All tab, one multi-`type` read), so that half of this ruling's original
    #    reason is retired — but this counts what this console has OBSERVED
    #    while the ledger reads Conduit's own list, and the console's date
    #    filters are `<input type="date">`, so a rolling 24-hour window is not
    #    expressible there either. The Recent transactions table
    #    below is the ten newest of *any* age, which is a different set again.
    #    Faking the nearest filter would put a number on screen next to rows
    #    that do not add up to it.
    attention = await _count(session, Operation, Operation.state.in_(ACTIVE_STATES))
    stats = [
        {
            "label": "Unresolved operations",
            "value": attention,
            "note": "sent, no answer yet",
            "tone": "warn" if attention else "",
            "url": "#needs-attention",
        },
        {
            "label": "Pending applications",
            "value": await _count(session, Projection, _non_terminal("applications")),
            "note": "awaiting a decision",
            "tone": "",
            "url": "#pending-applications",
        },
        {
            "label": "Open drafts",
            "value": await _count(
                session, Draft, Draft.actor_id == actor.id, Draft.submitted_at.is_(None)
            ),
            "note": "yours, not yet submitted",
            "tone": "",
            "url": "/drafts?state=open",
        },
        {
            "label": "Transactions observed",
            "value": await _count(
                session,
                Projection,
                Projection.resource_kind == "transactions",
                _observed() >= now - timedelta(hours=24),
            ),
            "note": "in the last 24 hours",
            "tone": "",
            "url": "",
        },
    ]
    # The page's one Conduit read, made after the local ones for the reason
    # `no_second_read` states: there is nothing here safe to gather it with. Its
    # failure is the honest miss — ids alone, no banner, page unchanged.
    _local, listed = await with_customer_names(client, no_second_read())
    return render(
        request,
        "dashboard.html",
        section="overview",
        names=listed.names,
        stats=stats,
        operations=[
            {
                "op": op,
                "age": _age(op.created_at, now),
                "next_step": NEXT_STEP.get(op.state, "—"),
            }
            for op in unresolved[:LIMIT]
        ],
        more_operations=len(unresolved) > LIMIT,
        applications=[{"row": row, "customer": _customer(row)} for row in pending[:LIMIT]],
        more_applications=len(pending) > LIMIT,
        # `drafts.list_for_actor`'s filter, minus the entity load and with the
        # limit in SQL: this actor's own, still unsubmitted, freshest first.
        drafts=(
            await session.execute(
                select(Draft.id, Draft.country, Draft.client_reference_id, Draft.updated_at)
                .where(Draft.actor_id == actor.id, Draft.submitted_at.is_(None))
                .order_by(Draft.updated_at.desc())
                .limit(LIMIT)
            )
        ).all(),
        transactions=[{"row": row, "customer": _customer(row)} for row in recent],
        can_edit=actor.can("onboarding.edit"),
    )
