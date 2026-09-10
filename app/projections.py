"""`apply_observation()` — the ONLY projection writer (OPERATIONS_SPEC §4).

Webhook handlers and reconciler reads both funnel through here, which is what
makes the two safe to run at once: the projection row is locked, and an
observation is applied only if it *advances* the resource. Duplicate and
out-of-order deliveries are therefore no-ops rather than special cases, and the
last writer to arrive still cannot regress anything.

Precedence, highest first:

1. terminal beats non-terminal (a `completed` transaction never goes back to
   `processing`, whatever order the deliveries land in);
2. higher rank beats lower within the kind's ladder;
3. at equal rank, the newer `observed_at` wins — and if the *values* differ
   that is real drift, so it is logged.

Anything this app has not heard of — an unknown kind, an unknown status — is
stored verbatim, ranked as the kind's lowest non-terminal state, and never
inferred to be terminal (plan v2 §7). It can land on a fresh projection but can
never overwrite a state we know to be further along.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, operations
from app.models import Operation, Projection

log = logging.getLogger(__name__)

SYSTEM = {"actor_id": audit.SYSTEM_ACTOR_ID, "actor_email": audit.SYSTEM_ACTOR_EMAIL}

# Per-kind state ladders. Terminal states are the last tuple of each ladder;
# everything before it is a step on the way there.
_LADDERS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Payouts live here too: a payout IS a transaction (`GET /v2/payouts/{id}`
    # returns the withdrawal branch of the unified transaction view, same `txn_`
    # id), and `transaction.*` is the only event family Conduit emits for one.
    # A separate `payouts` kind would be a second row for the same resource.
    "transactions": (("pending",), ("processing",), ("completed", "failed", "cancelled")),
    "applications": (("pending",), ("processing",), ("approved", "rejected", "cancelled")),
    "orders": (("pending",), ("succeeded", "failed", "cancelled")),
    "virtual_accounts": (("pending_activation",), ("active",), ("disabled",)),
    # `open` and `responded` deliberately share a rank. An RFI ping-pongs
    # between them — `rfi.more_info_requested` re-opens one that was already
    # answered — so ranking `responded` above `open` would make a real Conduit
    # transition look like a regression, drop it, and pin the projection at
    # "answered" forever: the stale sweep would re-read the truth and be refused
    # every time. The cost of equal rank is one drift line in the log per round,
    # which is an honest description of what happened (`_advances`).
    # No second axis (round number) to make the ping-pong monotonic —
    # add one the day something renders these rows and the log noise bites.
    "rfis": (("draft",), ("open", "responded"), ("resolved", "cancelled")),
    # `rejected` is terminal at the same rank as `registered`, which is not —
    # which is exactly why terminality is tracked separately from rank.
    "whitelist_recipients": (
        ("pending_review",),
        ("registered", "rejected"),
        ("suspended", "revoked"),
    ),
}

STATE_RANKS: dict[str, dict[str, int]] = {
    kind: {state: rank for rank, states in enumerate(ladder) for state in states}
    for kind, ladder in _LADDERS.items()
}

TERMINAL: dict[str, frozenset[str]] = {
    "transactions": frozenset({"completed", "failed", "cancelled"}),
    "applications": frozenset({"approved", "rejected", "cancelled"}),
    "orders": frozenset({"succeeded", "failed", "cancelled"}),
    "virtual_accounts": frozenset({"disabled"}),
    # A resolved or cancelled RFI owes nobody an answer; `responded` does not
    # end it, because compliance may open another round.
    "rfis": frozenset({"resolved", "cancelled"}),
    "whitelist_recipients": frozenset({"rejected", "revoked"}),
}

# The rung an RFI sits on while it still owes Conduit an answer — read off the
# ladder rather than written out, so a change to `_LADDERS["rfis"]` moves this
# with it instead of leaving a second copy behind. `open`'s own rank is the
# anchor because that is the rung the question is about; `draft` (rank 0) is
# below it and is never even served on the RFI surface, and the terminal filter
# is there because rank alone does not imply non-terminal (see
# `whitelist_recipients`, where a terminal `rejected` shares a rank with a live
# `registered`).
OPEN_RFI_STATES: frozenset[str] = frozenset(
    state
    for state, rung in STATE_RANKS["rfis"].items()
    if rung == STATE_RANKS["rfis"]["open"] and state not in TERMINAL["rfis"]
)


# --- what a projection payload is not allowed to keep --------------------------------
#
# Bank coordinates and postal addresses, stripped at the write boundary. Every
# other store of this data subtree in this codebase is encrypted
# (`counterparties.recipient`, `operations.request_body`, `drafts.payload`,
# `payout_batch_rows.payload`, `document_blobs.data`); this column cannot be,
# because four code paths read it through SQL JSONB pointers — the accounts index
# (`customerId`, `asset.code`), the dashboard's customer naming (`customerId`,
# `customerName`), the drafts purge (`resubmittable`) and the reconciler's stale
# sweep (`? subjects`). So it is minimized instead: what cannot be read here is
# also never written here, and none of those keys is in the set below.
#
# Enumerated from the pinned spec (`contracts/openapi_production.json`) across
# every DTO these deliveries can carry — the five `Public*ViewDto` transaction
# views, `VirtualAccountResponseClass`, `WhitelistRecipientResponseDto` and its
# three rail variants, the order and application DTOs — and checked against the
# live fixtures in `tests/fixtures/`. By key at every depth rather than by path,
# because the same coordinates appear at three different depths:
# `destination.recipient.accountNumber` on a withdrawal,
# `depositInstructions[].accountNumber` on a virtual account, and a bare
# top-level `accountNumber` on a whitelist recipient.
#
# **The verbatim rule yields here, and only here.** `Projection.state` is stored
# verbatim including values this app has never heard of (plan v2 §7), and that is
# untouched: an unknown *status* is still recorded exactly as it arrived, and so
# is every unknown field that is not on this list. The rule exists so a state is
# never lost or guessed at — not so that payee bank details are hoarded in a
# column with no retention job, readable by anything holding a dump, a replica or
# a SELECT. Nothing in this console reads the stripped subtrees; the account and
# payment surfaces read them from Conduit, live, at the moment they render.
_PII_KEYS = frozenset(
    {
        "recipient",  # destination/source.recipient — the payee's whole block
        "sender",  # the counterparty block on a deposit's source
        "senderInfo",  # what the sandbox deposit simulator echoes back
        "depositInstructions",  # account number, IBAN, rails, beneficiary address
        "wireReceive",  # inbound-wire coordinates on a transaction side
        "accountNumber",
        "routingNumber",
        "iban",
        "bic",
        # --- identity, added 2026-09-01 -------------------------------------
        # The first pass stopped at account coordinates because that was the
        # class it was sent to fix. These are the rest of what these deliveries
        # carry, and `persons` is the sharpest of them:
        # `CustomerOnboardingApplicationDto.persons[]` is the beneficial owners
        # and directors — names, dates of birth, identity documents. That is
        # natural-person identity data sitting in a cache with no retention job,
        # which is a worse thing to hold than an account number, not a lesser
        # one.
        #
        # Timed deliberately: migration `a93c15d0e7b6` imports `scrub` rather
        # than copying it, and had not yet been run against any deployment, so
        # widening the set here is what that migration cleans. After a deploy the
        # same widening would need a second migration to reach rows the first one
        # left behind.
        "persons",  # ApplicationDto.persons[] — beneficial owners, directors
        "responses",  # ClientRfiDetailDto.responses[] — message + submittedByEmail
        "taxId",
        "contactEmail",
        "phone",
    }
)

# Read from `projections.payload` and therefore NOT strippable. `customerName` is
# the load-bearing one: `app/web/dashboard.py` renders it from here precisely so
# that the one page which must work while Conduit is down does not have to ask
# Conduit who these people are (its "Local data only" contract). It is a business
# customer's legal name — the same value the whitelist and payout surfaces show
# in the clear by the masking policy's own rule — not a natural person's.
# `test_the_scrub_keeps_what_the_console_reads` pins each of these.
_READ_FROM_PROJECTIONS = frozenset(
    {"customerId", "customerName", "asset", "activatedAt", "virtualAccountId",
     "resubmittable", "subjects", "status"}
)


def _is_pii(key: str) -> bool:
    # `address`, `postalAddress`, `bankAddress`, `beneficiaryAddress`,
    # `registeredAddress`, and whatever Conduit names the next one.
    return key in _PII_KEYS or key == "address" or key.endswith("Address")


def scrub(value: Any) -> Any:
    """The observation with its coordinate and address subtrees removed."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not _is_pii(k)}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


async def observed_names(session: AsyncSession, customer_ids) -> dict[str, str]:
    """`{customer id: legal name}` for the ids given, out of what this console has
    already OBSERVED — **no Conduit call**.

    `app/web/dashboard.py`'s `_customer` read in bulk, and for the same reason it
    exists there: `customerName` is on the transaction view DTOs, it is on the
    keep-list above deliberately, and a page that must render without Conduit
    cannot ask Conduit who these people are. The one caller is the cross-customer
    contact picker (`GET /payouts/contact`), whose read budget is zero
    — the console's usual resolver (`web.with_customer_names`) is one bounded
    Conduit read, and there is nothing on that page to gather it with.

    A customer no observation ever named is simply absent, and every caller
    renders that as the bare id (DESIGN.md's honest-miss clause). "Observed" is
    the same word the transfer screen's filtered datalist uses, and it means the
    same thing: this is what arrived, not what exists.

    One row per id by `DISTINCT ON`, scanned without an index — the
    same shape as the dashboard's own counts on this table. An expression index
    on `(payload->>'customerId')` is the upgrade path, and it belongs to the
    queued DB round that decides this table's indexes together.
    """
    ids = sorted({str(one) for one in customer_ids if one})
    if not ids:
        return {}
    identifier = Projection.payload["customerId"].astext
    name = Projection.payload["customerName"].astext
    found = await session.execute(
        select(identifier, name)
        .where(identifier.in_(ids), name.is_not(None))
        # Freshest first per customer, so a renamed customer reads under the name
        # the newest observation carried.
        .order_by(identifier, func.coalesce(Projection.observed_at, Projection.updated_at).desc())
        .distinct(identifier)
    )
    return {row[0]: row[1] for row in found.all() if row[1]}


def rank(resource_kind: str, state: str | None) -> int:
    """Unknown kind or unknown state → 0, the lowest non-terminal rung."""
    return STATE_RANKS.get(resource_kind, {}).get(state or "", 0)


def is_terminal(resource_kind: str, state: str | None) -> bool:
    return state is not None and state in TERMINAL.get(resource_kind, frozenset())


def _advances(
    resource_kind: str, current: Projection, state: str | None, observed_at: datetime
) -> bool:
    if current.state is None:
        return True
    was, now = is_terminal(resource_kind, current.state), is_terminal(resource_kind, state)
    old, new = rank(resource_kind, current.state), rank(resource_kind, state)
    forward = now > was or (now == was and new > old)
    backward = was > now or (now == was and new < old)
    newer = current.observed_at is None or observed_at > current.observed_at
    applied = forward or (not backward and newer)
    # A different value that is not a step forward is drift, whether we end up
    # keeping it (equal rank, newer) or dropping it (a regression).
    if state != current.state and not forward:
        log.warning(
            "projection drift: %s %s observed %r against %r — %s",
            resource_kind,
            current.resource_id,
            state,
            current.state,
            "kept the newer" if applied else "ignored",
        )
    return applied


async def apply_observation(
    session: AsyncSession,
    *,
    resource_kind: str,
    resource_id: str,
    observed: dict[str, Any],
    observed_at: datetime | None = None,
) -> bool:
    """Record what Conduit says about one resource. Returns whether it advanced.

    Also resolves any operation this observation proves landed — matched by
    `conduit_resource_id` or by `clientReferenceId` (which is the operation id).
    Commits.
    """
    observed_at = observed_at or datetime.now(UTC)
    # Upsert-then-lock: after this the row exists, and FOR UPDATE serializes the
    # webhook against the reconciler (and against a second worker).
    await session.execute(
        pg_insert(Projection)
        .values(resource_kind=resource_kind, resource_id=resource_id)
        .on_conflict_do_nothing(index_elements=["resource_kind", "resource_id"])
    )
    current = (
        await session.execute(
            select(Projection)
            .where(
                Projection.resource_kind == resource_kind,
                Projection.resource_id == resource_id,
            )
            .with_for_update()
        )
    ).scalar_one()

    state = observed.get("status")
    if not isinstance(state, str):
        state = None  # absent, or a shape we were not promised — not a state
    applied = _advances(resource_kind, current, state, observed_at)
    if applied:
        current.state = state
        # The single write boundary, so the reconciler's full-DTO reads are
        # minimized by the same line the webhook path is (`scrub`).
        current.payload = scrub(observed)
        current.observed_at = observed_at
    await session.commit()

    await _resolve_operations(session, resource_id, observed)
    return applied


async def _resolve_operations(
    session: AsyncSession, resource_id: str, observed: dict[str, Any]
) -> None:
    """An observation of the resource is proof Conduit accepted the mutation.

    Only the §2 transition path is used, so an already-terminal operation is
    untouched and a concurrent resolver loses the race harmlessly.

    `stalled` is in the match set: the reconciler stopped *asking* after N
    attempts, but evidence that arrives on its own still resolves the row (§2).
    The reconciler itself still skips stalled operations — only an observation
    moves them.
    """
    matches = [Operation.conduit_resource_id == resource_id]
    reference = observed.get("clientReferenceId")
    if isinstance(reference, str):
        try:
            matches.append(Operation.id == uuid.UUID(reference))
        except ValueError:
            pass  # not one of ours; the resource-id match still applies

    candidates = (
        (
            await session.execute(
                select(Operation.id).where(
                    or_(*matches),
                    Operation.state.in_(("in_flight", "outcome_unknown", "stalled")),
                )
            )
        )
        .scalars()
        .all()
    )
    for op_id in candidates:
        # try_ rather than plain transition: the web recorder or the reconciler
        # may resolve the row between our select and our row lock.
        await operations.try_transition(
            session,
            op_id,
            "confirmed",
            **SYSTEM,
            conduit_resource_id=resource_id,
            detail={"reason": "observed"},
        )
