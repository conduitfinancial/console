"""Reconciler pass (OPERATIONS_SPEC §3–§4, plan v2 §5).

One `reconcile_pass()` = sweep stale `in_flight` rows, then resolve every due
`outcome_unknown` operation by its §3 recipe:

    1. reference lookup (clientReferenceId = operation id) — did Conduit get it?
    2. only if that missed: replay the *original* body with the *original*
       idempotency key. Safe precisely because step 1 proved the resource does
       not exist; never a fresh key.
    3. after an ambiguous replay: look up again.

A lookup that cannot be performed (network down, 5xx after retries) raises
`LookupUnavailable` and the operation is left `outcome_unknown` — replaying on a
failed lookup is the one move that could duplicate a payout.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import and_, not_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, documents, operations, projections
from app.conduit.client import ConduitClient, Outcome, Page, Problem, Result, Success, classify
from app.conduit.execute import resource_id, send, target_id
from app.config import get_settings
from app.models import Operation, Projection

log = logging.getLogger(__name__)

# A bounded walk — 20 × 100 = 2000 recent records is far past any
# plausible burst for a resource that is minutes old. Past that the honest
# answer is "could not determine", not "absent". Raise it if a real deployment
# ever trips the ceiling; the log line names the path.
MAX_PAGES = 20
PAGE_SIZE = 100

SYSTEM = {"actor_id": audit.SYSTEM_ACTOR_ID, "actor_email": audit.SYSTEM_ACTOR_EMAIL}

# §3: ~1, 2, 5, 15, 60 minutes. Measured from `unknown_since` (the row carries no
# last-attempt timestamp), so attempt N happens roughly DELAYS[N] after the
# outcome went unknown. Last entry repeats for any attempt beyond the schedule.
DELAYS_SECONDS = (60, 120, 300, 900, 3600)


class LookupUnavailable(Exception):
    """The recipe's read failed. Not a miss — resolution is postponed."""


class BudgetExhausted(Exception):
    """This pass has spent its request allowance; the rest waits for the next one."""


@dataclass(frozen=True)
class Found:
    """A recipe's positive answer — and, when the recipe actually *read* the
    resource, the resource itself.

    A found-by-reference lookup and a by-status read are observations of exactly
    the kind a webhook carries, so they belong in the projection too (§4's
    single-writer rule). Carrying them out
    on the return value is what keeps the recipes' own signature unchanged:
    every recipe already has the resource in hand at the moment it decides.

    `kind`/`observed` are absent where there is nothing to project — an RFI
    response, a webhook endpoint, or a resource identified only by a replay's
    response body.
    """

    resource_id: str | None
    kind: str | None = None
    observed: dict | None = None


@dataclass(frozen=True)
class Reject:
    error: dict


LookupResult = Found | Reject | None
Lookup = Callable[["_Budgeted", Operation], Awaitable[LookupResult]]


class _Budgeted:
    """Wraps the client so every reconciler request is counted (§3: reconciliation
    must never contribute rate-limit pressure against interactive traffic).

    Charges what actually went over the wire, retries included — otherwise a
    rate-limited read would cost one unit and make ten requests."""

    def __init__(self, client: ConduitClient, budget: int) -> None:
        self._client, self.remaining = client, budget

    async def _spend(self, call, *args: Any, **kwargs: Any):
        if self.remaining <= 0:
            raise BudgetExhausted
        before = self._client.requests
        try:
            return await call(*args, **kwargs)
        finally:
            self.remaining -= self._client.requests - before

    async def get(self, path: str, **params: Any) -> Result:
        # max_attempts = what is left: a read with one unit remaining must make
        # one wire request, not four retries that take the counter negative.
        return await self._spend(self._client.get, path, max_attempts=self.remaining, **params)

    async def page(self, path: str, **params: Any) -> Page | Result:
        return await self._spend(self._client.page, path, max_attempts=self.remaining, **params)

    async def mutate(self, *args: Any, **kwargs: Any) -> Result:
        return await self._spend(self._client.mutate, *args, **kwargs)


# --- recipe building blocks ------------------------------------------------------


async def _read(client: _Budgeted, path: str) -> dict:
    result = await client.get(path)
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    if isinstance(result, Problem) and result.status == 404:
        return {}  # a definitive "no such resource" is a miss, not an outage
    raise LookupUnavailable(path)


async def _list(client: _Budgeted, path: str, **params: Any) -> list[dict]:
    """One page. Only safe where the server did the filtering for us — an empty
    result then really is Conduit saying "no such resource"."""
    result = await client.page(path, **params)
    if isinstance(result, Page):
        return result.items
    raise LookupUnavailable(path)


async def _walk(
    client: _Budgeted, path: str, *, stop_before: datetime | None = None, **params: Any
) -> list[dict]:
    """Read a collection far enough that *absence* from the result is provable.

    Stops early at the first item older than `stop_before` — valid only on a
    newest-first sort, and used only there: the resource we are hunting cannot
    predate the operation that would have created it. Otherwise it walks until
    the cursor runs out.

    If neither happens within MAX_PAGES, that is "I could not tell", which is
    `LookupUnavailable` — never an empty list. An empty list is what authorizes
    a replay, so "it wasn't on the pages I read" must never be able to spell it
    — one busy minute can age an accepted payout off page one.
    """
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await client.page(path, cursor=cursor, limit=PAGE_SIZE, **params)
        if not isinstance(page, Page):
            raise LookupUnavailable(path)
        items.extend(page.items)
        if stop_before is not None and any(
            not _at_or_after(item.get("createdAt"), stop_before) for item in page.items
        ):
            return items
        if not page.next_cursor:
            return items
        cursor = page.next_cursor
    raise LookupUnavailable(f"{path}: reached {MAX_PAGES} pages without a boundary")


def _collection(path: str) -> str:
    """`/v2/transactions` → `transactions`: the projection kind is the collection
    the recipe read from, so a reconciler observation lands on the same row a
    webhook for that resource would."""
    return path.rstrip("/").rsplit("/", 1)[-1]


async def _by_reference(path: str, client: _Budgeted, op: Operation, **extra: Any) -> LookupResult:
    """`?clientReferenceId={op.id}` — supported by /transactions and /orders.
    Server-side and exact, so one page is the whole answer.

    `extra` carries the filters the endpoint *requires*: `/v2/transactions` makes
    `type` mandatory, and omitting it made the payout lookup a 400 — which is
    `LookupUnavailable`, so the recipe could never confirm and every unresolved
    payout walked to `stalled`. Fail-safe, but never resolving.
    """
    items = await _list(client, path, clientReferenceId=str(op.id), limit=2, **extra)
    return Found(items[0].get("id"), _collection(path), items[0]) if items else None


async def _applications(client: _Budgeted, op: Operation) -> list[dict]:
    """/applications has no clientReferenceId filter in the pinned spec,
    so we pull recent applications (narrowed by customerId when we have it) and
    match locally, walking back only as far as the operation's own timestamp."""
    return await _walk(
        client,
        "/v2/applications",
        stop_before=_since(op),
        sortBy="createdAt",
        sortOrder="desc",
        customerId=op.customer_id,
    )


async def _application_by_reference(client: _Budgeted, op: Operation) -> LookupResult:
    """onboarding_submit / customer_update — their DTOs carry clientReferenceId."""
    for item in await _applications(client, op):
        if item.get("clientReferenceId") == str(op.id):
            return Found(item.get("id"), "applications", item)
    return None


def _since(op: Operation) -> datetime:
    """The operation's own creation time, with a minute of slack for clock skew
    between our database and Conduit. Nothing Conduit created *for this
    operation* can predate it."""
    return (op.created_at or datetime.min.replace(tzinfo=UTC)) - timedelta(seconds=60)


async def _feature_application(client: _Budgeted, op: Operation) -> LookupResult:
    """feature_request has no reference field (FeatureRequestDto), so match the
    application on what the request did say: customer + feature type + asset, and
    only applications not older than this operation."""
    body = op.request_body or {}
    asset = (body.get("asset") or {}).get("code")
    since = _since(op)
    for item in await _applications(client, op):
        if (
            item.get("type") == body.get("type")
            and (asset is None or (item.get("asset") or {}).get("code") == asset)
            and _at_or_after(item.get("createdAt"), since)
        ):
            return Found(item.get("id"), "applications", item)
    return None


def _at_or_after(timestamp: Any, moment: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")) >= moment
    except ValueError:
        return True  # unparseable/missing timestamp: don't let it exclude a match


async def _rfi_response(client: _Budgeted, op: Operation) -> LookupResult:
    """Message text alone is not an identity: RFI rounds repeat, and an operator
    answering "see attached" twice would have the first round's response confirm
    the second. The request DTO carries no round id, so the
    discriminators available are the documents, the submitter, and the fact that
    our response cannot predate our operation — which is what separates rounds.
    """
    rfi = await _read(client, f"/v2/rfis/{target_id(op)}")
    body = op.request_body or {}
    submitter = (body.get("submittedBy") or {}).get("email")
    documents = sorted(body.get("documentIds") or [])
    since = _since(op)
    for response in rfi.get("responses") or []:
        if (
            isinstance(response, dict)
            and response.get("message") == body.get("message")
            and sorted(response.get("documentIds") or []) == documents
            and (submitter is None or response.get("submittedByEmail") == submitter)
            and _at_or_after(response.get("createdAt"), since)
        ):
            return Found(response.get("id"))
    return None


# The statuses a *newly created* registration can be in. A `revoked`,
# `suspended` or `rejected` entry is the residue of an earlier registration of
# the same account — matching it would confirm our operation against somebody
# else's dead row.
WHITELIST_FRESH = ("pending_review", "registered")


async def _whitelist_recipient(client: _Budgeted, op: Operation) -> LookupResult:
    """Match on everything the request actually stated, not just the name.

    `POST /customers/{id}/whitelist-recipients` carries no `clientReferenceId`,
    so the match is over the body's own fields — and it has to be narrow enough
    that a *different* registration cannot satisfy it. Legal name plus
    coordinates was not: the same account registered twice (a revoke and a
    re-register, two rails for one bank) matched an older entry, and the second
    operation confirmed against the first's id. So: the rail it was registered
    on, the relationship declared, a status a fresh registration can actually be
    in, and a `createdAt` no older than this operation.
    """
    body = op.request_body or {}
    coordinates = {
        k: body[k] for k in ("accountNumber", "routingNumber", "iban", "bic") if body.get(k)
    }
    since = _since(op)
    for item in await _walk(client, op.request_path):
        if (
            item.get("legalName") == body.get("legalName")
            and item.get("rail") == body.get("rail")
            and item.get("relationship") == body.get("relationship")
            and item.get("status") in WHITELIST_FRESH
            and _at_or_after(item.get("createdAt"), since)
            and all(item.get(k) == v for k, v in coordinates.items())
        ):
            return Found(item.get("id"), "whitelist_recipients", item)
    return None


async def _webhook_endpoint(client: _Budgeted, op: Operation) -> LookupResult:
    url = (op.request_body or {}).get("url")
    for item in await _walk(client, "/v2/webhooks/endpoints"):
        if item.get("url") == url:
            return Found(item.get("id"))
    return None


def _known(kind: str) -> tuple[str, ...]:
    """The status vocabulary for a resource kind, taken from the one place it is
    written down (`app.projections`) so the reconciler and the projections can
    never disagree about what a known status is."""
    return tuple(projections.STATE_RANKS[kind])


def _by_status(
    path: Callable[[Operation], str],
    *,
    kind: str,
    confirmed: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
    confirmed_unless: tuple[str, ...] = (),
) -> Lookup:
    """Cancels and executes: read the resource and let its status decide.
    `confirmed_unless` = "anything past this status counts as done" (order_execute).
    Anything else is a miss → replay.

    A status outside `known` resolves to nothing at all. `confirmed_unless` used
    to read every unrecognised value as success, which is
    exactly the inference plan v2 §7 forbids: a status we have never seen is not
    evidence that a cancel or an execute took effect.

    `kind` names both the status vocabulary and the projection this read
    advances — one value, so the two can never drift apart.
    """
    known = _known(kind)

    async def lookup(client: _Budgeted, op: Operation) -> LookupResult:
        resource = await _read(client, path(op))
        status = resource.get("status")
        if status is None:
            return None
        if status not in known:
            log.warning(
                "operation %s: unknown status %r on %s — refusing to infer an outcome",
                op.id,
                status,
                path(op),
            )
            raise LookupUnavailable(f"unknown status {status!r}")
        if status in confirmed or (confirmed_unless and status not in confirmed_unless):
            return Found(resource.get("id") or target_id(op), kind, resource)
        if status in rejected:
            return Reject(
                {
                    "type": "RESOURCE_NOT_ACTIONABLE",
                    "title": "Too late to apply",
                    "status": 409,
                    "detail": f"The resource is already {status}; the action can no longer apply.",
                    "resolution": "No action needed — the resource reached a terminal state on its own.",
                    "observedStatus": status,
                }
            )
        return None

    return lookup


async def _no_reference(client: _Budgeted, op: Operation) -> LookupResult:
    """document_upload has no reference field (§3): straight to replay. A
    duplicate document is harmless — worst case an unused `doc_` id."""
    return None


def _drop_suffix(op: Operation) -> str:
    """`/v2/payouts/{id}/cancel` → `/v2/payouts/{id}`."""
    return op.request_path.rsplit("/", 1)[0]


@dataclass(frozen=True)
class Recipe:
    lookup: Lookup
    replay: bool = True
    # order_create: a rejected replay (QUOTE_EXPIRED / QUOTE_ALREADY_USED) can
    # still mean *our* order consumed the option — look again before rejecting.
    relookup_on_reject: bool = False


RECIPES: dict[str, Recipe] = {
    "onboarding_submit": Recipe(_application_by_reference),
    "customer_update": Recipe(_application_by_reference),
    "feature_request": Recipe(_feature_application),
    "document_upload": Recipe(_no_reference),
    "rfi_respond": Recipe(_rfi_response),
    "whitelist_create": Recipe(_whitelist_recipient),
    "whitelist_revoke": Recipe(
        _by_status(
            lambda op: op.request_path,
            kind="whitelist_recipients",
            confirmed=("revoked",),
        )
    ),
    # `type` is a required query parameter on /v2/transactions, and a payout is
    # the `withdrawal` branch of that unified view.
    "payout_create": Recipe(partial(_by_reference, "/v2/transactions", type="withdrawal")),
    # A payout IS a transaction (verified in phase-0 API research and against the
    # pinned spec: `PublicTransactionsPageDto` is the unified view "across
    # deposit / … / withdrawal / …, discriminated by `type`", and
    # `GET /v2/payouts/{id}` returns that withdrawal view under the same `txn_`
    # id). So both payout recipes project under `transactions` — the same row a
    # `transaction.*` webhook writes. Two kinds for one resource would have meant
    # two rows telling the same truth and a list view picking the stale one.
    "payout_cancel": Recipe(
        _by_status(
            _drop_suffix,
            kind="transactions",
            confirmed=("cancelled",),
            rejected=("completed", "failed"),
        )
    ),
    "order_create": Recipe(partial(_by_reference, "/v2/orders"), relookup_on_reject=True),
    "order_execute": Recipe(
        _by_status(_drop_suffix, kind="orders", confirmed_unless=("pending",))
    ),
    "order_cancel": Recipe(
        _by_status(
            _drop_suffix,
            kind="orders",
            confirmed=("cancelled",),
            rejected=("succeeded", "failed"),
        )
    ),
    "webhook_endpoint_register": Recipe(_webhook_endpoint),
}


# OPERATIONS_SPEC §4: conflict types that name a *pre-existing other* resource.
# At mutation time every 409 is ambiguous and `classify()` stays that way — but
# here the recipe's reference lookup has already missed, which proves our
# operation created nothing, so a refusal pointing at somebody else's resource is
# a settled answer rather than an unknown. Conflicts meaning "Conduit already
# holds *our* resource" (IDEMPOTENCY_KEY_CONFLICT, ONBOARDING_ALREADY_SUBMITTED)
# are deliberately absent: those still resolve by looking the resource up.
# An explicit allowlist, not a heuristic over 409 bodies. An unlisted
# conflict keeps riding the backoff — the conservative direction.
DEFINITIVE_CONFLICTS = frozenset(
    {"CUSTOMER_ALREADY_ONBOARDED", "WHITELIST_RECIPIENT_CONFLICT", "APPLICATION_ALREADY_DECIDED"}
)


def _definitive_conflict(result: Result) -> bool:
    return (
        isinstance(result, Problem)
        and result.status == 409
        and result.type in DEFINITIVE_CONFLICTS
    )


# --- the pass ---------------------------------------------------------------------


def _due(op: Operation, now: datetime) -> bool:
    if op.unknown_since is None:
        return True
    delay = DELAYS_SECONDS[min(op.reconcile_count, len(DELAYS_SECONDS) - 1)]
    return (now - op.unknown_since).total_seconds() >= delay


async def _try_transition(
    session: AsyncSession, op_id: uuid.UUID, to_state: str, **kwargs
) -> bool:
    """Every reconciler transition can lose a race to a webhook observing the
    same settlement (§4); `operations.try_transition` owns that convergence."""
    return await operations.try_transition(session, op_id, to_state, **SYSTEM, **kwargs) is not None


async def _project(session: AsyncSession, found: Found) -> None:
    """A positive read is an observation, and observations have exactly one
    writer (§4). Called *after* the operation transition so that the transition
    reports its own verdict rather than losing the race to the observation's own
    resolution path.
    """
    if found.kind and found.resource_id and found.observed is not None:
        await projections.apply_observation(
            session,
            resource_kind=found.kind,
            resource_id=found.resource_id,
            observed=found.observed,
        )


# Kinds whose resource can be read back from its id alone. `virtual_accounts`
# and `whitelist_recipients` are deliberately absent: their read paths are nested
# under `/v2/customers/{customerId}/…` and the projection row does not carry the
# customer id, so there is nothing to build a URL from. Both are covered by
# webhooks and by their operations' own recipes; add them here the day the
# projection stores the customer, not before.
PROJECTION_READ_PATH: dict[str, str] = {
    "transactions": "/v2/transactions/{id}",
    "applications": "/v2/applications/{id}",
    "orders": "/v2/orders/{id}",
    # `GET /v2/rfis/{id}` takes the id alone and answers `ClientRfiDetailDto`,
    # whose `status` is required (pinned spec) — sweepable, and for RFIs the
    # sweep is load-bearing rather than a repair of last resort: an `rfi.*`
    # delivery carries only `{"rfiId": …}`, so this read is the only place the
    # console ever learns an RFI's `dueAt`, subjects or rounds from the worker.
    "rfis": "/v2/rfis/{id}",
}

# A flat per-tick row cap rather than a per-kind settle model. The
# request budget is the real limiter; this just stops one tick from queueing
# hundreds of reads before the budget check gets a turn.
PROJECTION_SWEEP_LIMIT = 50


def _sweepable():
    """The SQL half of "which projections still owe us a read".

    **Both halves have to be in the query, not in the loop**:
    the `LIMIT` runs before any Python filtering, so fifty old settled rows —
    which the ledger accumulates by design — filled the batch and were then all
    skipped, and a newer stale row behind them was never reached. Not slowly:
    never, on every tick, for as long as those fifty stayed oldest. Terminality
    is per-kind (`projections.TERMINAL`), so it is expressed here as the pairs
    it actually is rather than as a list of words that mean different things to
    different kinds.

    The one exception is an RFI that arrived already settled. Its event carried
    `{"rfiId": …}` and nothing else, so the projection holds an id, a status and
    no subjects at all — and being terminal, it would never be read again and
    would stay a row that cannot say what it was about. One
    hydration read fixes that permanently: `apply_observation` stores the full
    `ClientRfiDetailDto`, `subjects` appears, and the predicate stops selecting
    it. A resolved RFI with subjects is settled and is left alone.
    """
    settled = tuple_(Projection.resource_kind, Projection.state).in_(
        [(kind, state) for kind, states in projections.TERMINAL.items() for state in states]
    )
    return or_(
        # A row observed without a status is not settled — and `NOT (NULL IN …)`
        # is NULL, which would silently drop it.
        Projection.state.is_(None),
        not_(settled),
        and_(
            Projection.resource_kind == "rfis",
            or_(
                Projection.payload.is_(None),
                not_(Projection.payload.has_key("subjects")),  # noqa: W601 — JSONB `?`
            ),
        ),
    )


async def sweep_stale(
    session: AsyncSession, client: _Budgeted, *, now: datetime | None = None
) -> dict[str, int]:
    """Repair projections that stopped moving (plan v2 §5's promised repair job).

    Webhooks are at-least-once, not exactly-once: a delivery that never arrives
    leaves a transaction stuck at `processing` forever, and nothing else looks —
    the operation reconciler only ever visits rows with an unresolved
    *operation*, which a settled-but-mis-projected resource does not have. So
    every non-terminal projection older than `PROJECTION_STALE_SECONDS` is read
    back exactly, by id, and fed through the same single writer
    (`apply_observation`) the webhook path uses. A monotonic apply means a read
    that agrees with what we already had is a no-op.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=get_settings().projection_stale_seconds)
    counts: Counter[str] = Counter()
    stale = (
        (
            await session.execute(
                select(Projection)
                .where(
                    Projection.resource_kind.in_(tuple(PROJECTION_READ_PATH)),
                    Projection.updated_at < cutoff,
                    _sweepable(),
                )
                .order_by(Projection.updated_at)
                .limit(PROJECTION_SWEEP_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for row in stale:
        # No terminality check here any more: `_sweepable()` is the one place
        # that decides, so the LIMIT and the skip can no longer disagree.
        if client.remaining <= 0:
            counts["budget_exhausted"] += 1
            break
        path = PROJECTION_READ_PATH[row.resource_kind].format(id=row.resource_id)
        try:
            observed = await _read(client, path)
        except (LookupUnavailable, BudgetExhausted) as exc:
            # Unreadable is not evidence of anything. Leave it stale; the next
            # tick tries again.
            log.warning("projection sweep: %s not readable (%s)", path, type(exc).__name__)
            counts["unreadable"] += 1
            if isinstance(exc, BudgetExhausted):
                counts["budget_exhausted"] += 1
                break
            continue
        if not observed:
            counts["missing"] += 1
            continue
        applied = await projections.apply_observation(
            session,
            resource_kind=row.resource_kind,
            resource_id=row.resource_id,
            observed=observed,
        )
        counts["repaired" if applied else "unchanged"] += 1
    return dict(counts)


async def _project_safely(session: AsyncSession, found: Found) -> None:
    """Never let a projection write undo an operation's verdict.

    The transition committed first (it has to — `apply_observation` resolves
    operations itself, and would otherwise steal the verdict this pass just
    earned). If the projection write then fails, the operation is already
    correctly `confirmed`; the projection is merely stale, and `sweep_stale`
    below is the durable repair path for exactly that. Raising here would turn a
    resolved operation into an exception the caller counts as unresolved.
    """
    try:
        await _project(session, found)
    except Exception:  # noqa: BLE001 — a stale projection is a repairable state
        await session.rollback()
        log.exception(
            "projection write failed for %s %s — left stale for the sweep",
            found.kind,
            found.resource_id,
        )


async def _resolve(session: AsyncSession, client: _Budgeted, op: Operation) -> str:
    recipe = RECIPES[op.type]
    outcome = await recipe.lookup(client, op)

    if outcome is None and recipe.replay:
        # §4: the *original* request, byte for byte, with the original key —
        # `send` reproduces it in whatever encoding the endpoint speaks.
        result = await send(session, op, client)
        match classify(result):
            case Outcome.CONFIRMED:
                outcome = Found(resource_id(result, op))
            case Outcome.REJECTED:
                outcome = Reject(result.raw)  # type: ignore[union-attr]
                if recipe.relookup_on_reject:
                    outcome = await recipe.lookup(client, op) or outcome
            case _ if _definitive_conflict(result):
                # §4 short-circuit: the lookup above already missed, so this
                # conflict is about a resource that was never ours. Rejecting now
                # saves the operator an hour of backoff on a settled answer.
                outcome = Reject(result.raw)  # type: ignore[union-attr]
            case _:
                outcome = await recipe.lookup(client, op)  # §4: look up again

    if isinstance(outcome, Found):
        applied = await _try_transition(
            session,
            op.id,
            "confirmed",
            conduit_resource_id=outcome.resource_id,
            detail={"reason": "reconciled"},
        )
        await _project_safely(session, outcome)
        return "confirmed" if applied else "already_resolved"
    if isinstance(outcome, Reject):
        applied = await _try_transition(
            session, op.id, "rejected", error=outcome.error, detail={"reason": "reconciled"}
        )
        return "rejected" if applied else "already_resolved"
    return "unresolved"


async def reconcile_pass(
    session: AsyncSession, client: ConduitClient, *, now: datetime | None = None
) -> dict[str, int]:
    """One worker tick. Returns a count per outcome, for logs and tests."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    counts: Counter[str] = Counter()

    for stale in await operations.find_stale_in_flight(session, now=now):
        if await _try_transition(
            session, stale.id, "outcome_unknown", detail={"reason": "stale_in_flight"}
        ):
            counts["swept"] += 1

    budgeted = _Budgeted(client, settings.reconcile_request_budget)
    # Due-ness is filtered in Python — the unresolved set is tiny by
    # construction, and the schedule is a CASE expression nobody wants in SQL.
    unresolved = (
        (
            await session.execute(
                select(Operation)
                .where(Operation.state == "outcome_unknown")
                .order_by(Operation.unknown_since)
            )
        )
        .scalars()
        .all()
    )
    for op in unresolved:
        if not _due(op, now):
            continue
        if op.reconcile_count >= settings.reconcile_max_attempts:
            if await _try_transition(
                session, op.id, "stalled", detail={"reason": "attempts_exhausted"}
            ):
                counts["stalled"] += 1
            continue
        if budgeted.remaining <= 0:  # don't spend an attempt we can't make requests for
            counts["budget_exhausted"] += 1
            break
        op.reconcile_count += 1  # the attempt is spent whether or not it resolves
        await session.commit()
        try:
            counts[await _resolve(session, budgeted, op)] += 1
        except BudgetExhausted:
            counts["budget_exhausted"] += 1
            break
        except (LookupUnavailable, documents.BlobUnavailable) as exc:
            # Either we could not find out, or we could not honestly re-send.
            # Both leave the row `outcome_unknown` for the next pass — the one
            # thing that must never happen here is a guess.
            log.warning("operation %s: not resolvable this pass (%s)", op.id, exc)
            counts["unresolved"] += 1

    # Same budget, same tick: operations first (an unresolved mutation is the
    # more urgent unknown), then whatever allowance is left goes to repairing
    # projections nothing else is watching.
    for name, count in (await sweep_stale(session, budgeted, now=now)).items():
        counts[f"projection_{name}"] += count
    return dict(counts)
