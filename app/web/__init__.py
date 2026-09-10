"""Server-rendered operator UI (plan v2 §1 `app/web/`).

Shared plumbing for the route modules: the template environment, the status
pill's vocabulary, the request-body reader, and the two dependencies every route
needs (a DB session, the process-wide Conduit client).

**Which mutations go through `app/operations/`.** Every Conduit call that creates
or moves something the operator must never duplicate — an onboarding submission,
an RFI response, a document upload — goes through `operations.start` +
`execute_operation`, per OPERATIONS_SPEC §1. Four calls in this package
deliberately do not, and are audited instead:

* `POST /v2/rfis/{id}/acknowledge` — 204, creates nothing, naturally repeatable;
* `POST …/persons/{referenceId}/idv-link` — returns a bearer credential we are
  told not to persist, so an operation row would hold nothing worth reconciling;
* the sandbox decision simulator, and the sandbox deposit simulator —
  sandbox-only test scaffolding.

The first two cannot produce a duplicate resource, and neither has an outcome
that could stay unknown: the operator sees the answer or the error, immediately.
The sandbox pair can duplicate on a double-click — two simulated deposits — but
only on the host whose money is fake by construction, and both routes re-check
that host server-side before sending. Everything else is ledgered.

**A fifth exception, of a different kind.** Saving, renaming
and archiving a **contact** (`app/web/contacts.py`, and the save on payout
acceptance in `app/web/payouts.py`) makes no Conduit call at all: the console's
own record of a destination is console-local by necessity — Conduit's only recipient store is
`whitelist-recipients` and it is the `intercompany` gate — and its fields are
pasted into `FiatPayoutDto` at use rather than registered anywhere. There is no
remote resource to duplicate, no idempotency key to reuse and no outcome that
can stay unknown, so `operations.start` would be ceremony over a local INSERT.
All three are audited writes (`counterparty.save` / `.rename` / `.archive`)
committed in the caller's transaction, exactly like the four above. **Editing and
deleting one are the same kind of write** (the contact-editing round):
`counterparty.edited` carries masked before→after diffs for the identity keys
that changed and names — never values — for the rest, and `counterparty.deleted`
carries no coordinate at all, because there are none left (OPERATIONS_SPEC §1).

**How an error re-render reaches the screen.** htmx 2 swaps 2xx responses and
silently drops everything else, so a route that answers a POST with a
re-rendered page and a non-2xx status would render nothing at all: the operator
clicks submit and watches the page sit there. Two halves fix that, and both are
global rather than per-form:

1. `base.html` ships a `<meta name="htmx-config">` whose `responseHandling`
   adds **400, 422 and 502** — the app's intentional render statuses, and only
   those — to the swap list. 401/403/404/409/429 and the other 5xx keep htmx's
   default no-swap: those bodies come from the auth middleware or FastAPI, not
   from a page this app meant to show inside itself.
2. Every form that can receive one carries
   `hx-target="main" hx-select="main" hx-swap="outerHTML"`. The response is a
   whole document, so `hx-select` lifts its `<main>` out — without it the swap
   would inject a second copy of `<script src="app.js">` and double every
   delegated listener in it, which for the upload handler means two uploads per
   file.

Those same forms carry `hx-sync="this:drop"` + `hx-disabled-elt`, so a
double-click is one request rather than a second submission queued behind the
first — which would otherwise land *after* the operations guard released and
create a second application.

Routes that answer only with `redirect()` need neither: `HX-Redirect` is
honoured on a 204.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app import forms, projections
from app.models import Operation, Projection
from app.counterparties import identify as _identify
from app.auth.tokens import seal, unseal
from app.auth.web import csrf_hx_headers, csrf_token
from app.web.countries import (
    COUNTRIES,
    country_name,
    enum_label,
    enum_option,
    humanize_enum,
    option_label,
)
from app.conduit.client import ConduitClient, Page, Problem
from app.conduit.problems import code_of, console_words
from app.config import get_settings
from app.db import sessionmaker

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC = ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES))


# --- dependencies -------------------------------------------------------------------


async def db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def conduit(request: Request) -> ConduitClient:
    """The one pooled client built by the lifespan (plan v2 §4)."""
    return request.app.state.conduit


async def attention_badges(request: Request, session: AsyncSession = Depends(db)) -> None:
    """The ribbon's action-required counts, or `None` each: `rfi_open`,
    `ops_stalled`, `apps_resubmittable` on `request.state`.

    Three counts, one rule (2026-09-02): a red nav badge means an
    OPERATOR'S action is required behind that item — open RFIs to answer,
    stalled operations only a human can settle, rejected-but-resubmittable
    applications waiting for a corrected submission. Nothing routine is badged:
    a badge that is always lit teaches operators to ignore badges.

    The chrome renders on every page, and `base.html` had no way to ask the
    database anything: `env_badge()` reads settings, `request.state.actor` is set
    by the auth middleware, and Jinja here is synchronous, so a template cannot
    await a query. This is that missing mechanism, and it is deliberately the
    smallest one that works — **a router-level dependency, not a new middleware**:

    * it hangs off `install_web`'s router only, so `/health/*` and `/webhooks/*`
      keep their current behaviour (a probe that started failing because the
      database was down would be a worse console, not a better one);
    * `Depends(db)` is the same callable the routes use, so FastAPI's dependency
      cache hands both this and the page's own handler **one** session — the
      badge costs a query, never a second connection.

    **Local only.** One `COUNT` over the `rfis` projections this installation has
    observed; no Conduit call is added anywhere, which is what keeps the
    dashboard's and `/accounts`' zero-call contracts true on every page that
    wears the ribbon.

    Two different silences, both honest, and the template renders neither:
    a zero (nothing is open — an empty inbox needs no badge) and a `None` (the
    read failed). A failed count must never be paintable as a confident "0", so
    the exception path stores `None` and rolls back — the session is shared with
    the handler that is about to run, and leaving it in a failed transaction
    would take the page down over an ornament.
    """
    try:
        request.state.rfi_open = int(
            await session.scalar(
                select(func.count())
                .select_from(Projection)
                .where(
                    Projection.resource_kind == "rfis",
                    Projection.state.in_(sorted(projections.OPEN_RFI_STATES)),
                )
            )
            or 0
        )
        # `stalled` exactly, not ACTIVE_STATES: the other active states resolve
        # themselves (worker, reconciler); stalled is the one the machine has
        # STOPPED working and a human owns — which is what a red badge claims.
        request.state.ops_stalled = int(
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.state == "stalled")
            )
            or 0
        )
        # Rejected AND resubmittable — the pair the applications list renders as
        # the amber "· resubmittable" pill: parked work with a next step. A
        # final rejection has no operator action and earns no badge.
        request.state.apps_resubmittable = int(
            await session.scalar(
                select(func.count())
                .select_from(Projection)
                .where(
                    Projection.resource_kind == "applications",
                    Projection.state == "rejected",
                    Projection.payload["resubmittable"].astext == "true",
                )
            )
            or 0
        )
    except Exception:  # noqa: BLE001 — an ornament may not take a page down
        log.warning("attention badge counts failed; rendering no badges", exc_info=True)
        request.state.rfi_open = None
        request.state.ops_stalled = None
        request.state.apps_resubmittable = None
        await session.rollback()


# --- request bodies -----------------------------------------------------------------


class UnmintedIntent(Exception):
    """A submission carrying an `intent` this console never minted.

    The sibling of `operations.IntentTypeMismatch`, one step earlier: that one is
    a real nonce pointed at the wrong form, this one is not a nonce at all. Both
    are refusals rather than fallbacks, and for the same reason — see `intent_of`
    for why "present but unreadable" cannot be quietly treated as "absent".

    The token is kept for the log line's sake only. It is not secret (it rode
    through the browser) but it is not echoed to the page either: an attacker's
    own string reflected into the response is a gadget, and there is nothing an
    operator could do with it.
    """

    def __init__(self, submitted: str) -> None:
        super().__init__("submission token was not minted by this console")
        self.submitted = submitted


# How long a render's nonce stays submittable. **Deliberately generous.** An
# expired seal is "present but invalid", so it is a 422 that loses whatever the
# operator had typed — and a payout form opened before lunch and submitted after
# it is an ordinary day in an operations console, not an attack. The TTL is not
# what bounds replay: `operation_intents` makes the nonce one-use no matter how
# long it stays readable, so shortening this would buy nothing and cost work.
INTENT_TTL_SECONDS = 86_400


def seal_intent(nonce: uuid.UUID) -> str:
    """`nonce`, signed for the round trip through the browser and back.

    One payload key, `n`. The seal is what makes the nonce *ours*: before it, any
    uuid a submitter could compute was accepted as a nonce, so an attacker could
    pre-claim one and have the operation ledger answer to a value this console
    had never put on a page.
    """
    return seal({"n": str(nonce)}, ttl=INTENT_TTL_SECONDS)


def new_intent() -> str:
    """A one-use nonce for one mutation form render (OPERATIONS_SPEC §1).

    Every operation-backed form carries one in a hidden field; `operations.start`
    resolves by it before anything else, in every state. That is what makes a
    lost redirect — the browser mechanically re-POSTing a form whose operation
    already confirmed — land back on that same operation instead of paying
    twice. Minted per render, so re-rendering a corrected form is genuinely a
    new attempt.

    Sealed, so that `intent_of` can tell a nonce this console minted from a
    string that merely parses as a uuid.
    """
    return seal_intent(uuid.uuid4())


# The page sizes a Conduit-backed list offers, smallest first — the first is the
# default. 100 is Conduit's own maximum for `limit` (pinned spec), so the top of
# this tuple is the top of what the API will serve rather than a number picked
# here.
PAGE_SIZES = (25, 50, 100)


def page_size(query) -> int:
    """The rows-per-page the operator asked for, validated server-side.

    A whitelist, not a bound: `?limit=1000` is not clamped to 100 and
    `?limit=abc` is not an error page — both fall back to the default, which is
    what every other out-of-vocabulary query parameter in this console does (an
    unknown transaction `type`, an unknown account `status`). The value reaches
    Conduit only after passing through here.
    """
    try:
        chosen = int((query.get("limit") or "").strip())
    except (AttributeError, TypeError, ValueError):
        return PAGE_SIZES[0]
    return chosen if chosen in PAGE_SIZES else PAGE_SIZES[0]


def pager(path: str, filters: dict, page, size: int = PAGE_SIZES[0]) -> dict:
    """`{prev, next, sizes, size}` for one cursor page, built server-side.

    Two bugs in one line of Jinja otherwise: the links were
    concatenated from raw cursor strings — an opaque token Conduit is free to
    put an `&` or a `+` in — and they carried only the tab, so paging past page
    one silently dropped every status, customer and date filter the operator had
    set and showed them a different query's second page.

    `filters` may hold lists (repeated `status` parameters); `doseq` keeps each
    one. Empty values are dropped so the URL says only what was asked for.

    `size` is the rows-per-page control, and it rides on the same URLs for the
    same reason the filters do: a page turn that quietly reverted to 25 would be
    the console forgetting what it was asked. **The size links deliberately drop
    the cursor** — a cursor is a position in a page*ing*, not in a list, so
    re-slicing at a different size and keeping the token would ask Conduit to
    continue a pagination that no longer exists.
    """
    kept = [
        (key, str(v))
        for key, value in filters.items()
        for v in (value if isinstance(value, (list, tuple)) else [value])
        if v not in (None, "")
    ]

    def link(cursor: str | None, direction: str = "") -> str:
        if not cursor:
            return ""
        extra = [("limit", str(size)), ("cursor", cursor)] + (
            [("direction", direction)] if direction else []
        )
        return f"{path}?{urlencode(kept + extra, quote_via=quote_plus)}"

    return {
        "prev": link(page.prev_cursor, "backward"),
        "next": link(page.next_cursor),
        "size": size,
        "sizes": [
            {
                "n": n,
                "on": n == size,
                "url": f"{path}?{urlencode(kept + [('limit', str(n))], quote_via=quote_plus)}",
            }
            for n in PAGE_SIZES
        ],
    }


def offset_pager(path: str, filters: dict, *, offset: int, size: int, more: bool) -> dict:
    """`{prev, next, sizes, size}` for a local, offset-paged list.

    `pager` above builds the same shape around a Conduit **cursor**; a list this
    console assembles itself has none, so the position is an offset. Same output
    contract, so `m.table_foot` renders both, and the size links drop the offset
    for the same reason the cursor ones do — re-slicing at a different size makes
    the old position meaningless.

    Written for `/contacts` and moved here when the ledger's
    contact filter became its second caller: that view is a set of
    transaction ids out of the console's own trail, which is a local list wearing
    a Conduit list's clothes.
    """
    kept = [(key, str(value)) for key, value in filters.items() if value]

    def link(start: int) -> str:
        page = [("limit", str(size))] + ([("offset", str(start))] if start else [])
        return f"{path}?{urlencode(kept + page, quote_via=quote_plus)}"

    return {
        "prev": link(max(offset - size, 0)) if offset else "",
        "next": link(offset + size) if more else "",
        "size": size,
        "sizes": [
            {
                "n": n,
                "on": n == size,
                "url": f"{path}?{urlencode(kept + [('limit', str(n))], quote_via=quote_plus)}",
            }
            for n in PAGE_SIZES
        ],
    }


def offset_of(query) -> int:
    """`?offset=` for an offset-paged local list — never negative, never an error
    page (the `page_size` rule: an unusable value falls back to the default)."""
    try:
        return max(int((query.get("offset") or "").strip()), 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def export_url(request: Request, surface: str, **scope: str) -> str:
    """The CSV link for the view this request is rendering (`app/web/exports.py`).

    The page's own query string, verbatim, so the file is the view the operator
    is looking at — minus `cursor`/`direction`/`limit`, which say *where in the
    paging* they are and what a screen holds. An export is the whole filtered
    set, so carrying them across would be describing a position that the file
    does not have.

    `scope` is for the customer-scoped surfaces, whose page carries the customer
    in the path and whose export carries it in the query.
    """
    kept = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in ("cursor", "direction", "limit", "offset") and value
    ] + [(key, value) for key, value in scope.items() if value]
    query = urlencode(kept, quote_via=quote_plus)
    return f"/export/{surface}.csv" + (f"?{query}" if query else "")


# --- names over ids ------------------------------------------------------------------


def display_name(customer: Mapping | None) -> str:
    """The name Conduit registered for one customer, or `""`.

    One fallback chain for the whole console (DESIGN.md's names-over-ids row):
    the business legal name, else the individual's two names, else nothing — and
    "nothing" is a real answer, not an id, because the caller decides what to
    show when there is no name. Four templates carried character-for-character
    copies of this expression before it lived here.
    """
    record = customer if isinstance(customer, Mapping) else {}
    legal = str(record.get("legalName") or "").strip()
    both = f"{record.get('firstName') or ''} {record.get('lastName') or ''}".strip()
    return legal or both


def customer_names(customers) -> dict[str, str]:
    """`{cus_…: display name}` for one page of customers.

    Ids with no resolvable name are simply absent: a miss and "we fetched a
    blank" have to look the same to the templates, and both render as the id.
    """
    named = {}
    for item in customers or []:
        identifier = str((item or {}).get("id") or "") if isinstance(item, Mapping) else ""
        name = display_name(item)
        if identifier and name:
            named[identifier] = name
    return named


# One bounded customers read, everywhere. Not the list's own page size: this is a
# lookup table, and widening it would spend Conduit's budget on names nobody
# asked for.
NAME_LIMIT = 25


class CustomerNames(NamedTuple):
    """What one bounded customers read gives a page: the rows (for a datalist or
    a picker), the id→name map, whether it was truncated, and — for the one
    caller whose tray is the point of the page — the failure itself."""

    items: list[dict]
    names: dict[str, str]
    more: bool
    failure: Any | None


async def with_customer_names(client: ConduitClient, read) -> tuple[Any, CustomerNames]:
    """`(the read's own result, CustomerNames)` — the console's one customer-name
    resolver.

    Neither `GET /v2/customers` nor `GET /v2/transactions` nor
    `GET /v2/applications` offers a name or search parameter, so a name can only
    become an id in the browser (`m.customer_datalist`) — and an id can only
    become a name here. The shape of the call is the point:

    * **One read per render, never per row.** A list of 100 transactions
      resolves its customers from this single bounded page or not at all; a page
      that fetched a name per row would turn one screen into a hundred requests.
      The cost is that ids beyond the first `NAME_LIMIT` customers resolve to
      nothing, which every surface renders as the bare id rather than as a
      guess.
    * **Gathered, never serialised.** `read` is the page's own read, passed in
      un-awaited. `client.page` returns its failures as values, so neither can
      take the other down (only a cancellation propagates, which `gather`
      re-raises exactly as a bare `await` would), and an unreachable Conduit
      cannot spend two retry budgets end to end on a page an operator opened to
      find out what happened. That lesson is kept in one place rather
      than re-derived per route.
    * **Failure is silent — to the reader.** No problem card: the list renders
      exactly as it did before, minus the names and the suggestions. A second
      banner over a list that read fine would be the console reporting an outage
      it does not have. `failure` is handed back for `/orders`, whose launcher
      is a *tray you operate* rather than a convenience: an empty picker with no
      explanation is a page missing its point, so that one caller says so.

    `more` says the suggestions are the first cursor page rather than the
    customer list — a completion list that quietly stops at 25 would let an
    operator conclude a customer does not exist, so every caller says so.
    """
    listed, result = await asyncio.gather(client.page("/v2/customers", limit=NAME_LIMIT), read)
    if isinstance(listed, Page):
        return result, CustomerNames(
            listed.items, customer_names(listed.items), bool(listed.next_cursor), None
        )
    return result, CustomerNames([], {}, False, listed)


async def no_second_read() -> None:
    """`with_customer_names` gathers its customers page with the page's *own*
    read; a page whose own reads are local SQL on a session that is not safe to
    await concurrently has nothing to gather with. The resolver still earns its
    place: it is where "bounded, silent on failure, `more` stated out loud" is
    written down once, so the two local-rows pages (`/accounts`, the Overview)
    get names on exactly the terms every other surface gets them."""
    return None


def intent_of(values) -> uuid.UUID | None:
    """The nonce a submission carried, or None when it carried none at all.

    Three cases, and the distinction between the last two is the whole guard:

    * **Absent or empty** → None, which is not an error. It falls back to the
      request-hash guard, which is what an older tab, a non-form caller and the
      document upload get; hundreds of honest posts arrive this way.
    * **Present and it unseals** → the nonce inside, which this console minted.
    * **Present and it does not** → `UnmintedIntent`, and the submission is
      refused before anything is sent.

    That third case used to be the second. `uuid.UUID(...)` parsed *any* uuid out
    of the field, so a submitter could name a nonce it had computed itself and
    have `operations.start` resolve by it — pre-claiming a value no render had
    ever issued. Refusing rather than falling back to None is what closes
    it: treating an unreadable token as "no token" would hand the forger exactly
    the behaviour they were trying to get, one step further along. It also
    catches the honest cases — a tampered field, a seal past its TTL — where
    proceeding would silently drop the double-submit guard the form was relying
    on, and pay twice.
    """
    try:
        submitted = (values.get("intent") or "").strip()
    except AttributeError:  # not a mapping at all — the same as carrying nothing
        return None
    if not submitted:
        return None
    payload = unseal(submitted)
    try:
        return uuid.UUID(str((payload or {}).get("n")))
    except (TypeError, ValueError):
        raise UnmintedIntent(submitted) from None


# The flash for a nonce that resolved onto an operation about something else
# (`operations.resolved_elsewhere`). Five routes across four call sites
# share it — payout cancel, order execute, order cancel, whitelist revoke and
# the RFI response — and one sentence rather than five is the point: an operator
# who has learned to read it on one screen has learned to read it everywhere,
# and five variants is how "so it did go through on the other page" gets
# believed. All four sites redirect to the resolved operation, so "what you are
# looking at" is literally true.
#
# Deliberately NOT `payouts.ALREADY_SUBMITTED_DIFFERENTLY`, which is the same
# shape of refusal for the payout *create* form: that one names the money ("no
# second payout was made"), which would be a false claim on a revoke and a
# meaningless one on an RFI answer. What both have to make impossible is reading
# the page they land on as a receipt for the thing just pressed, so both say what
# was NOT sent before they say what to do next.
ALREADY_SPENT_ELSEWHERE = (
    "This submission token had already been used for a different request, and what you "
    "are looking at is that request. Nothing was sent just now and nothing here changed. "
    "Reload the page you came from and press the button again if you still mean to."
)


# The operator's console-local note. One name, one bound, one
# reader — the four capture points must not each invent their own.
REFERENCE_FIELD = "internal_reference"
REFERENCE_MAX = 128  # one bound for both `Operation.reference` and `Draft.client_reference_id`


def reference_of(values) -> str | None:
    """The internal reference a submission carried, or None.

    Empty **is** None: an operator who tabs through the box has not written a
    note, and a row full of empty strings is a column that lies about how often
    the field is used. Truncated rather than refused — the note is a label, and
    refusing a payout over the length of a comment would be the console getting
    in the way of the money.
    """
    text = (values.get(REFERENCE_FIELD) or "").strip()
    return text[:REFERENCE_MAX] or None


async def form_items(request: Request) -> list[tuple[str, str]]:
    """`(name, value)` pairs from an `application/x-www-form-urlencoded` body —
    exactly what `forms.parse_submission` consumes.

    `parse_qsl` rather than `await request.form()`. Starlette's form
    parser needs `python-multipart`, which this slice may not add; file uploads
    arrive as raw bodies (`static/app.js`), so nothing else in the app wants a
    multipart parser either.
    """
    body = await request.body()
    return parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)


# --- status pills (plan v2 §7: unknown statuses render neutrally) --------------------

# tone -> a CSS class in base.html. `wait` is in-progress, `warn` needs eyes,
# `bad` is a terminal no, `muted` is over-without-outcome.
PILL_TONES: dict[str, dict[str, str]] = {
    "applications": {
        "pending": "wait",
        "processing": "wait",
        "approved": "ok",
        "rejected": "bad",
        "cancelled": "muted",
    },
    "rfis": {
        "draft": "muted",
        "open": "warn",
        "responded": "wait",
        "resolved": "ok",
        "cancelled": "muted",
    },
    "operations": {
        "created": "muted",
        "in_flight": "wait",
        "confirmed": "ok",
        "rejected": "bad",
        "outcome_unknown": "warn",
        "stalled": "bad",
        "abandoned": "muted",
    },
    "transactions": {
        "pending": "wait",
        "processing": "wait",
        "completed": "ok",
        "failed": "bad",
        "cancelled": "muted",
    },
    # Conversion orders. Conduit's own vocabulary (`OrderExternalResponseDto`,
    # pinned in contracts/openapi_production.json) and `projections._LADDERS`
    # agree on these four; the drift guard in tests/test_web_convert.py keeps
    # this dict and that ladder from parting company again. `succeeded` is the
    # ok here, not `completed` — orders and transactions do not share a word for
    # the same ending, which is exactly how this table came to be missing.
    "orders": {
        "pending": "wait",
        "succeeded": "ok",
        "failed": "bad",
        "cancelled": "muted",
    },
    "virtual_accounts": {
        "pending_activation": "wait",
        "active": "ok",
        "disabled": "muted",
    },
    # A batch payout's own lifecycle — this console's states, not
    # Conduit's: a batch is a file it has validated, and Conduit is never told
    # one exists.
    #
    # Corrected at the slice-2 design gate, on the amber-means-yours rule:
    # `validating` is machine work resolving without anybody (wait — summoning a
    # human to watch a validator run is a false alarm), and `ready` is the state
    # where the machine has finished and the decision is the operator's, which is
    # exactly what amber means in this console. `dispatched` is `ok` about the
    # BATCH's own job — every row has an answer — and deliberately says nothing
    # about settlement: that lives on each row's transaction, and a green batch
    # over a failed payment would be the console's worst kind of lie.
    "payout_batches": {
        "validating": "wait",
        "ready": "warn",
        "partially_dispatched": "warn",
        "dispatched": "ok",
        "abandoned": "muted",
    },
    # One dispatched row, derived from its operation rather
    # than stored (`batches.state_of`). `refused` is amber not red: this console
    # declined to send it, nothing reached Conduit, and dispatching again
    # retries it — which is the operator's move. `rejected` is Conduit's no and
    # is final in the batch.
    "payout_batch_rows": {
        "invalid": "bad",
        "pending": "muted",
        "sending": "wait",
        "sent": "ok",
        "rejected": "bad",
        "unconfirmed": "warn",
        "abandoned": "muted",
        "refused": "warn",
    },
    # `registered` is the only status a payout may name; the rest are all
    # "not payable", for four different reasons the operator has to tell apart.
    "whitelist_recipients": {
        "pending_review": "wait",
        "registered": "ok",
        "suspended": "warn",
        "revoked": "muted",
        "rejected": "bad",
    },
}

OPERATION_LABELS = {
    "outcome_unknown": "Result being confirmed",
    "stalled": "Couldn't confirm — needs attention",
    "in_flight": "Sending",
}


def pill(kind: str, value: object) -> dict:
    """`{label, tone}` for one status.

    A value this build has never heard of renders as `Unknown: <raw>`, neutrally
    toned, and is logged with the resource kind — never guessed at, never
    inferred to be terminal (plan v2 §7).
    """
    raw = "" if value is None else str(value)
    known = PILL_TONES.get(kind, {})
    if raw not in known:
        log.warning("unknown %s status %r rendered neutrally", kind, raw)
        return {"label": f"Unknown: {raw}", "tone": "unknown", "known": False}
    label = OPERATION_LABELS.get(raw) or raw.replace("_", " ").capitalize()
    return {"label": label, "tone": known[raw], "known": True}


# `failureCode` on a rejected application, verbatim from the pinned spec
# (`CustomerOnboardingApplicationDto` and its three siblings all carry the same
# two-value enum). A code outside this pair is NOT guessed at: the raw string is
# shown in mono, exactly as an unknown status is — this build has no idea what a
# code it has never seen means, and inventing a sentence for one would be the
# console speaking for Conduit's reviewers.
FAILURE_CODES = {
    "rejected_by_ops": "Rejected by operations review",
    "compliance_denied": "Declined by compliance",
}


def failure_label(code: object) -> str | None:
    """The English for a `failureCode`, or None when there is no code at all.
    An unrecognised code comes back verbatim — the template renders it in mono,
    which is this console's mark for "this is Conduit's word, not ours"."""
    raw = "" if code is None else str(code)
    return FAILURE_CODES.get(raw, raw) if raw else None


def application_pill(item: object) -> dict:
    """The applications pill, plus the one distinction the DTO makes and a
    `(kind, value)` table cannot.

    `rejected` is one word for two different situations, and the difference is
    the whole of what an operator does next: `resubmittable: true` means correct
    it and send a fresh application — actionable, which is exactly what the warn
    family means in this console (DESIGN.md's fourth family) — while `false`
    means the decision is final and belongs in the exception family with the
    other terminal noes.

    The boolean is `Omitted on non-rejected applications` per the pin, and a
    rejection that arrives without it (an older payload, a projection row, a
    read that failed) is rendered as today's plain "Rejected": which of the two
    it is has not been established, and this console does not guess at money
    facts. `PILL_TONES` is untouched, so every other kind reads exactly as it
    did.
    """
    row = item if isinstance(item, Mapping) else {}
    p = pill("applications", row.get("status"))
    resubmittable = row.get("resubmittable")
    if row.get("status") != "rejected" or not isinstance(resubmittable, bool):
        return p
    return {
        **p,
        "label": "Rejected · resubmittable" if resubmittable else "Rejected · final",
        "tone": "warn" if resubmittable else "bad",
    }


def is_exception(kind: str, value: object) -> bool:
    """Whether this status is the terminal-failure family — the 3px left flag a
    row wears (DESIGN_DIRECTION "Tables").

    Reads the same table `pill` reads, and deliberately does **not** log: a row
    asks this question about a value it is also about to render as a pill, and
    an unknown status must not produce two warnings for one cell. An unknown
    status is not an exception here for the same reason it is never terminal —
    this build has no idea what it means (plan v2 §7).
    """
    return PILL_TONES.get(kind, {}).get("" if value is None else str(value)) == "bad"


def terminal(kind: str, value: object) -> bool:
    """Whether a status ends the story — the auto-refresh stop condition. An
    unknown value is never terminal (and never enables a state-dependent action)."""
    return projections.is_terminal(kind, str(value) if value is not None else None)


def env_badge() -> dict:
    settings = get_settings()
    return {
        "env": settings.conduit_env,
        "host": urlsplit(settings.conduit_base_url).hostname or settings.conduit_base_url,
        # For the ribbon's env strip: how this console decided who the operator
        # is. A name, never a secret — `proxy` / `oidc` / `disabled` is the mode,
        # and no credential, issuer or shared secret goes near the template.
        "auth": settings.auth_mode,
        # Anything that is not the throwaway environment gets the amber warning:
        # staging carries a real key against a production-labelled host.
        "tone": "ok" if settings.conduit_env == "sandbox" else "warn",
        # `MONEY_CEILING`, canonically formatted, or "" when unset. On the strip
        # beside the host because a refusal the operator did not know was coming
        # reads as a broken console.
        "ceiling": format(settings.ceiling, "f") if settings.ceiling is not None else "",
    }


def is_sandbox() -> bool:
    return get_settings().conduit_env == "sandbox"


# The three ways money leaves an account. Four surfaces show one launcher for
# all of them ("Move money", the customer's action row, an account's action
# row), so the launcher earns its place if the operator holds ANY of them — and
# the page it opens gates each verb on its own permission.
MOVE_MONEY: tuple[str, ...] = ("payout.create", "transfer.create", "order.create")


# --- problem details (OPERATIONS_SPEC §5) -------------------------------------------


def problem_view(result: Problem) -> dict:
    """Conduit's own problem-detail, ready for `_problem.html`."""
    return {
        "title": result.title,
        "detail": result.detail,
        "resolution": result.resolution,
        "correlation_id": result.correlation_id,
        "status": result.status,
        "local": False,
    }


def local_problem(title: str, detail: str, resolution: str = "", resource_id: str = "") -> dict:
    """A rejection this console minted itself. It has no `correlationId` because
    Conduit never issued one — the UI says so, and shows the resource id instead
    (OPERATIONS_SPEC §5)."""
    return {
        "title": title,
        "detail": detail,
        "resolution": resolution,
        "correlation_id": None,
        "resource_id": resource_id,
        "status": None,
        "local": True,
    }


# A Conduit resource id: `cus_034Abbx1XrOVaY6sXUBtGT`, `txn_…`, `doc_…`. The
# shape every id in this API has, and the gate for anything the vendor hands us
# that this console then prints (`named_resources`).
CONDUIT_ID = re.compile(r"[a-z]{2,8}_[A-Za-z0-9]{6,}")


def named_resources(error: dict | None) -> list[tuple[str, str]]:
    """`details.customerId: "cus_1"` → `[("customer", "cus_1")]`.

    A refusal that names a *pre-existing other* resource (OPERATIONS_SPEC §4's
    definitive conflicts — `CUSTOMER_ALREADY_ONBOARDED` carries the existing
    customer) is only useful to an operator if the console shows the id it
    named. Any `details.*Id` string renders, so a conflict type nobody
    has written a template for still surfaces its pointer.

    "Any string" was too generous (A3 gate, m5): this is vendor-supplied text
    printed verbatim beside a refusal, so it is held to the shape of a Conduit
    id — prefix, underscore, alphanumerics. A value that is not one is not a
    resource this console can link to or an operator can quote, and rendering
    it would put arbitrary vendor prose back on the page through the one door
    the translation does not cover.
    """
    details = (error or {}).get("details")
    if not isinstance(details, dict):
        return []
    return [
        (re.sub(r"(?<!^)(?=[A-Z])", " ", key[:-2]).lower(), value)
        for key, value in details.items()
        if key.endswith("Id") and len(key) > 2 and isinstance(value, str) and CONDUIT_ID.fullmatch(value)
    ]


def problem_of(error: dict | None, resource_id: str | None = None) -> dict | None:
    """The stored `operations.error` snapshot as a problem view. A snapshot with
    no `correlationId` was minted locally (the reconciler read the resource's own
    state) — labelled as such rather than shown as a Conduit answer.

    **Translated on the way out, by the same table `parse_problem` uses.** The
    ledger stores Conduit's body verbatim on purpose — it is the evidence of
    what was actually said, and A3 does not touch it — so this replay path is
    the LAST place a vendor `title`/`detail` could reach a screen — every reader
    of `Operation.error` goes through here (A3 gate, M1: four did not).
    `console_words` is the whole guard: the code decides the words, the stored
    prose is never read, and a snapshot this console minted itself keeps its own
    sentence (`problems.MINTED_HERE`) rather than being called a refusal.
    """
    if not error:
        return None
    status = error.get("status")
    title, resolution = console_words(
        code_of(error.get("type")),
        status if isinstance(status, int) else 0,
        str(error.get("resolution") or ""),
    )
    return {
        "existing": named_resources(error),
        "title": title,
        "detail": "",
        "resolution": resolution,
        "correlation_id": error.get("correlationId"),
        "resource_id": resource_id or "",
        "status": error.get("status"),
        "local": not error.get("correlationId"),
    }


def problem_line(result: object, fallback: str = "Conduit is unreachable") -> str:
    """One flash-banner line from a LIVE result: the translated title, and the
    resolution behind it.

    The resolution and never `detail`: since A3 a parsed problem's `detail` is
    always `""`, and for a code this console has no sentence of its own for the
    resolution is the only thing left an operator can act on (A3 gate, m3). Six
    routes end a mutation with one of these banners; they share the join so the
    seventh cannot quietly go back to printing something else.
    """
    if not isinstance(result, Problem):
        return fallback
    return " — ".join(part for part in (result.title, result.resolution) if part) or fallback


def problem_note(error: dict | None, fallback: str) -> str:
    """One flash-banner line from a stored `operations.error`.

    The three routes that end a mutation with a redirect (revoke a whitelisting,
    cancel a payout, execute or cancel an order) used to lift `title` — and one
    of them `detail` — straight out of the stored vendor body, which is the same
    leak `parse_problem` closes for everything that renders a card (A3 gate, M1).
    They share this instead: the translated title, and the resolution behind it,
    which is what an operator acts on when the code is one this console has no
    sentence for.
    """
    view = problem_of(error)
    if view is None:
        return fallback
    return " — ".join(part for part in (view["title"], view["resolution"]) if part) or fallback



# --- rendering ----------------------------------------------------------------------


def ts(value: datetime | str | None) -> Markup:
    """A timestamp as an operator reads it: `Aug 28, 22:19 UTC`, full ISO on hover.

    The server always renders UTC, and always *says* UTC — never a bare `Z`,
    which is a wire convention an operator has to translate. This is the truth
    of the row and the fallback for a browser that cannot do better; `app.js`
    then rewrites the display text into the viewer's own zone with that zone
    named, so both times on screen are labelled and neither can be mistaken for
    the other. `.num` puts it in the mono stack with the rest of the machine
    values (DESIGN.md).

    Takes a string as well as a datetime, because half the timestamps on screen
    are ours (SQLAlchemy hands us datetimes) and half are Conduit's (JSON, so
    ISO strings) and the operator should not be able to tell which row came from
    where. A string this console cannot parse is rendered **verbatim** rather
    than dropped or raised on: a payload with a date format nobody anticipated
    must not blank a cell, and must never take a page down.

    The year is printed whenever it is not the current one — `Jan 01 2099,
    00:10 UTC` — and dropped when it is. The console never lies about the state of
    money, and a date is part of that state: without the rule, every January
    renders December's rows as if they were this year's, and an order lock
    expiring in 2099 reads as one expiring today.
    """
    raw = value if isinstance(value, str) else None
    if raw is not None and raw.strip():
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return Markup('<time class="num">{raw}</time>').format(raw=raw)
    if not isinstance(value, datetime):
        return Markup('<span class="muted">—</span>')
    try:
        moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        # Read now, never cached: a long-lived process must not keep rendering
        # last year's compact form after midnight on the 31st.
        form = (
            "%b %d, %H:%M UTC"
            if moment.year == datetime.now(UTC).year
            else "%b %d %Y, %H:%M UTC"
        )
        return Markup('<time class="num" datetime="{iso}" title="{iso}">{short}</time>').format(
            iso=moment.isoformat(), short=moment.strftime(form)
        )
    except (ValueError, OverflowError, OSError):
        # Parsing is not the only way this fails. `0001-01-01T00:00:00+14:00`
        # parses fine and then *overflows* on the shift to UTC; the far end of
        # the range does the same, and `strftime` is a libc call with its own
        # opinions about ancient years. All of it lands here, verbatim and
        # escaped, rather than 500ing the page — which is what the promise
        # above is actually worth.
        return Markup('<time class="num">{raw}</time>').format(raw=raw or value.isoformat())


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


# --- failure paths (spec §5) --------------------------------------

# status -> (heading, sentence). The three the spec names plus a floor, because
# `exports.py` alone raises 400 and 409 and a bare framework JSON body is the
# finding this slice closes — one page that says which of the four happened
# beats three pages and a hole. The sentence never names the resource, the URL
# or the reason: a 404 that echoed the path back would be a reflected-content
# surface, and a 403 that said WHICH permission is missing is a map of the
# console's gates drawn for whoever probed it.
ERROR_COPY: dict[int, tuple[str, str]] = {
    400: (
        "That request did not make sense here",
        "Something in the address or the form was not something this page could use. "
        "Nothing was changed.",
    ),
    403: (
        "You do not hold this action",
        "Your console roles do not include it, so it was not carried out. "
        "An administrator can grant it; the roles you do hold are named at the foot "
        "of the navigation.",
    ),
    404: (
        "There is nothing at this address",
        "The link may be old, the record may have been removed, or the address may "
        "have been mistyped. Nothing is wrong with your session.",
    ),
    409: (
        "That no longer fits the state of this record",
        "It has moved on since the page you are looking at was drawn. Reload it and "
        "read where it stands before trying again.",
    ),
    500: (
        "Something in this console broke",
        # The console never lies about the state of money, and this page cannot
        # know how far the request got: it must not say "nothing happened".
        "The failure is recorded under the reference below. This page cannot say "
        "whether what you pressed took effect — open the record and read it before "
        "repeating anything that moves money.",
    ),
}
ERROR_FALLBACK = (
    "This console could not answer that",
    "The request reached the console and came back unanswered. Nothing was changed.",
)


def render_error(
    request: Request, status_code: int, reference: str = "", detail: str = ""
) -> HTMLResponse:
    """One failure page, inside the chrome, in the product's voice.

    `detail` is a sentence THIS CONSOLE wrote about this particular refusal —
    `exports.py` raises a dozen of them ("A whitelist capability is answerable
    for one customer at a time…") and they are the useful half of those pages.
    The caller decides what may be passed: nothing from Conduit ever reaches
    here (upstream problems are translated at the client boundary and rendered
    by the `problem` macro, not by this page), and `main.http_error` passes
    neither a framework stock phrase nor anything on a 403.

    `reference` is the only thing a 500 carries beyond its sentence — the id
    logged beside the traceback, so an operator can name the failure to whoever
    runs the install without the page telling them (or anyone who reached it)
    anything else about it. There is deliberately no exception text, no path, no
    host, no permission name and no request body on this page.
    """
    heading, sentence = ERROR_COPY.get(status_code, ERROR_FALLBACK)
    return render(
        request,
        "error.html",
        status_code=status_code,
        status=status_code,
        heading=heading,
        sentence=sentence,
        detail=detail,
        reference=reference,
    )


# The flash banner's replay ceiling: a flash is consumed by the very
# next page load, so five minutes is generous and bounds how long a leaked or
# bookmarked `?msg=`/`?err=` link keeps working.
FLASH_TTL = 300
# The banner is one sentence, not a payload; this is a cap on what `redirect`
# signs, applied before both the plaintext param and its signature so the two
# always agree.
FLASH_MAX_CHARS = 300


def redirect(request: Request, url: str, *, msg: str = "", err: str = "") -> Response:
    """Post/Redirect/Get, for both kinds of caller.

    Every mutating form in this console is an htmx form: CSRF is a header
    (`csrf_hx_headers` on `<body>`), and a plain browser form post cannot send
    one. htmx cannot usefully follow a 303 — it would swap a whole page into a
    fragment — so it gets `HX-Redirect`, which makes the browser navigate for
    real and keeps the address bar honest.

    `msg` / `err` are the flash banners `base.html` renders. `redirect` is the
    only legitimate producer of them, so each carries a signature alongside it —
    `msgsig` / `errsig` — that `base.html` checks before rendering anything: a
    plain `?msg=` on a crafted link has no signature and earns no banner. The
    plaintext param stays plaintext rather than becoming the sealed token
    itself, on purpose: turning `?msg=Payout+sent.` into
    `?msg=eyJleHAi...` would keep the address bar honest about *that* a
    redirect happened but not about *what* it says, which is the property this
    docstring claims. The query param names stay `msg` and `err` — every other
    call site keeps passing plain text and never sees the signature. They go
    through
    `quote_plus` here and nowhere else: call sites used to build the query by
    hand with `text.replace(" ", "+")`, which is not encoding — an `&`, `#`,
    `%` or `=` in the text (Conduit problem titles and asset codes both reach
    these) truncated the banner or invented a second parameter.

    The signature binds `url`'s own path, not just the text: without that, a
    genuine "Payout sent." earned on one page is a valid, live signature for the
    *same sentence* copy-pasted onto any other page for the whole `FLASH_TTL` —
    an operator-authored version of the same forgery, just gated behind having
    triggered a real flash once. `urlsplit` takes the path alone: `url` may
    already carry a query string of its own (`?operation=`, `?registered=`), and
    that must not
    be part of what gets bound — it is not part of what `flash()` will be
    asked to compare against, and the `msg`/`msgsig` pair this call is about
    to append is not on `url` yet either way.
    """
    if msg or err:
        path = urlsplit(url).path
        capped = {k: v[:FLASH_MAX_CHARS] for k, v in (("msg", msg), ("err", err)) if v}
        params: dict[str, str] = {}
        for key, text in capped.items():
            params[key] = text
            params[f"{key}sig"] = seal({"text": text, "path": path}, ttl=FLASH_TTL)
        url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote_plus)
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def flash(text: str | None, sig: str | None, path: str) -> str:
    """`text` back, if `sig` is a live signature for exactly `text` on exactly
    `path` — otherwise `""`.

    A template cannot try/except, so this is the one place that turns
    `unseal`'s `None` into something `{% if %}` can act on directly. Two
    equality checks, both load-bearing: `sig` alone proves *some* text was
    sealed by this app within `FLASH_TTL`, not that it was sealed for *this*
    `text` — without that check, a stale or borrowed `msgsig` would validate a
    different `msg` a crafted link swapped in. The path check is what stops a
    genuine flash from being copy-pasted onto a page it was never rendered
    for: `redirect()` binds the page it sent the operator to, so the same
    live, correctly-matched signature is only ever a flash on that one page.
    What is rendered is the signed copy, not the query param, so this is the
    authoritative text even though the two are equal by the time this
    returns.
    """
    payload = unseal(sig) if sig else None
    if not payload:
        return ""
    sealed_text = str(payload.get("text") or "")
    sealed_path = str(payload.get("path") or "")
    if sealed_text != (text or "") or sealed_path != path:
        return ""
    return sealed_text


templates.env.globals.update(
    csrf_token=csrf_token,
    csrf_hx_headers=csrf_hx_headers,
    # Display-only country names (app/web/countries.py): what an option *says*,
    # never what it submits, and never what is allowed.
    countries=COUNTRIES,
    country_name=country_name,
    # One masking policy for bank coordinates, everywhere any page renders them
    # — this console's own address book and Conduit's whitelist entries alike
    # (`counterparties.identify`; DESIGN.md's last-4 row).
    coords=_identify,
    humanize_enum=humanize_enum,
    option_label=option_label,
    # Operator language for the raw API enums this console filters and picks by
    # (QA F-002). Display only, in the strictest sense: every one of these
    # surfaces still submits, links and compares the wire value — the label is
    # what the row *says*, and the raw value stays on screen beside it.
    enum_label=enum_label,
    enum_option=enum_option,
    # The applications pill's one exception (`resubmittable`) and the English
    # for a `failureCode`. Display layer both: neither decides anything.
    application_pill=application_pill,
    failure_label=failure_label,
    env_badge=env_badge,
    is_sandbox=is_sandbox,
    new_intent=new_intent,
    pill=pill,
    # The row flag's question, asked without logging (see `is_exception`).
    is_exception=is_exception,
    # The one customer-name fallback chain, for the pages that hold a customer
    # DTO rather than a name map (`customers/list.html`, the pickers).
    display_name=display_name,
    terminal=terminal,
    # The Export CSV pill's href, built from the request the page is answering.
    export_url=export_url,
    # The `?msg=`/`?err=` flash text, authenticated against its `msgsig`/
    # `errsig` companion AND the page it was minted for, or "" for
    # anything a template's `{% if %}` should treat as no banner at all.
    flash=flash,
)
templates.env.filters["ts"] = ts
# The submitted-body view labels its rows the way group titles are labelled.
templates.env.filters["humanize"] = forms.humanize


class RevalidatedStatics(StaticFiles):
    """Stable asset paths with no fingerprint, so a cached copy must be asked
    about rather than reused."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def install_web(app: FastAPI) -> None:
    """Mount the static assets and every UI router. Called from `create_app`."""
    from app.web import (
        accounts,
        applications,
        batches,
        contacts,
        convert,
        customers,
        dashboard,
        exports,
        onboarding,
        payouts,
        recipients,
        rfis,
        sandbox_actions,
        transactions,
        transfers,
    )

    app.mount("/static", RevalidatedStatics(directory=str(STATIC)), name="static")
    # One parent router so the ribbon's open-RFI count is resolved for every UI
    # route and for nothing else — `/health/*` and `/webhooks/*` are mounted
    # straight onto the app in `create_app` and keep their current, database-free
    # behaviour. The mount above is a Starlette Mount, so `/static` never sees it
    # either.
    ui = APIRouter(dependencies=[Depends(attention_badges)])
    for router in (
        dashboard.router,
        onboarding.router,
        applications.router,
        rfis.router,
        customers.router,
        accounts.router,
        recipients.router,
        contacts.router,
        payouts.router,
        batches.router,
        transfers.router,
        convert.router,
        transactions.router,
        exports.router,
        sandbox_actions.router,
    ):
        ui.include_router(router)
    app.include_router(ui)
