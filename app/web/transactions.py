"""The transactions ledger: All by default, one tab per type, typed detail views.

`type` is a repeatable **array** on `GET /v2/transactions` (pinned spec), and
Conduit answers a multi-type request with ONE newest-first feed carrying ONE
cursor (live probe, 2026-09-02). So the ledger opens on **All** —
one read asking for the four fiat kinds this console works in — and the four
single-kind tabs are the filter beside it. No fan-out, no merge, no second
cursor: the paging is the same single-page passthrough every list here uses.

The filters are the ones the endpoint actually offers (customer, status, created
window, both references), and they ride on the All read exactly as they ride on
a single-kind one.

The detail view is typed off the transaction's own `type`, which is what
discriminates the `PublicTransactionsPageDto` union:

* **withdrawal** — this is the payout detail page. Status pill plus `stage` as a
  sub-label (informational: the spec is explicit that it replaces nothing), fees
  and any declared markup, `failureCode`/`failureMessage`, the RFI panel when
  `hasRfi`, `swiftUetr` when the payment carried one, a cancel button while
  pending, and the console's own operation panel when a local mutation created
  or cancelled it.

**Held on an RFI**. `hasRfi`/`rfiId` have been on the
withdrawal DTO and used to render as a line of text pointing the
operator at the customer's application. They now gate a live
`GET /v2/rfis?subjectType=transaction&subjectId=…` and the console's one RFI
panel is rendered *here*, so the round is answered on the page about the payment
it is holding up. This is also the announced-future seam: a payout that Conduit
accepts and then raises an RFI against arrives on this page as a held
transaction with an answerable round, and no code above knows or cares which of
the two worlds produced it (see `web/payouts.py`, OPERATIONS_SPEC §5).
* **deposit** — who sent it and over which rail, from the inbound side's own
  `sender` block.
* **anything else** — every scalar Conduit sent, labelled by its key path. A
  transaction kind this build has never heard of is shown, never inferred and
  never blank.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import audit, counterparties, operations, payments, projections, rfis
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem, Success
from app.web import (
    MOVE_MONEY,
    conduit,
    db,
    form_items,
    is_sandbox,
    local_problem,
    offset_of,
    offset_pager,
    page_size,
    pager,
    problem_line,
    problem_view,
    redirect,
    render,
    with_customer_names,
)
# The one operation panel in the console (OPERATIONS_SPEC §5) — the same view the
# application detail page renders, rather than a second one that could disagree.
from app.web.applications import _operation_view

router = APIRouter()

# `fiat_conversion` joined these later: the typed summary already shows what
# a conversion leg needs — amounts, fees, both sides, and the link to the order
# that owns it — so folding it in beat a fourth near-identical template.
TYPED_TEMPLATES = {"withdrawal", "deposit", "fiat_conversion"}


def _problem(result, what: str, resource_id: str = "") -> dict:
    return (
        problem_view(result)
        if isinstance(result, Problem)
        else local_problem(
            "Conduit is unreachable", f"{what} could not be read.", resource_id=resource_id
        )
    )


# The scalar filters, in one place: the page reads them back for its inputs and
# its pager, and `list_query` puts them on the wire.
FILTER_KEYS = (
    "customerId",
    "externalReference",
    # Two different references, both offered because they answer different
    # questions: `externalReference` is the *provider's* (the sending bank's on
    # a deposit, the payout provider's on a withdrawal), while
    # `clientReferenceId` is ours — and today "ours" means the operation id
    # `app/conduit/execute.py` stamps on every payout and order it sends.
    # /orders has offered its own; this ledger never did.
    "clientReferenceId",
    "createdAfter",
    "createdBefore",
)


# The contact filter is **not** on this list: `GET /v2/transactions` has no
# recipient-shaped parameter at all (verified against the pin).
# It is answered console-side, from this console's own trail, and these are the
# only wire keys a contact-filtered view keeps — see `list_query`.
KEPT_WITH_CONTACT = ("type", "customerId", "sortBy", "sortOrder")


def kind_of(query) -> str:
    """The single-kind tab in force, or `""` for the All feed — the default.

    A `?type=` this build does not know falls back to All rather than to one of
    the kinds: it is the same treatment every other out-of-vocabulary parameter
    on this page gets, and All is the one answer that cannot silently hide rows
    of the kind the operator asked for.
    """
    kind = (query.get("type") or "").strip()
    return kind if kind in payments.TRANSACTION_TYPES else ""


def contact_of(query) -> str:
    """The contact filter's value, or `""` when it is not in force.

    Two preconditions, both of them facts about what a contact *is* rather than
    taste: a contact belongs to exactly one customer (`counterparties.rows` is
    customer-scoped everywhere), so the customer filter has to be set; and the
    payments this console can link to one are payouts, which land on the ledger
    as `withdrawal`. On any other tab the filter could only ever return nothing,
    and an affordance that can only return nothing is a lie about what it does.
    The template disables the field and says which half is missing.
    """
    contact = (query.get("contact") or "").strip()
    customer = (query.get("customerId") or "").strip()
    return contact if contact and customer and kind_of(query) == "withdrawal" else ""


def list_query(query) -> dict:
    """This page's filters as the Conduit query they produce — paging aside.

    **One parser for the page and for its CSV export** (`app/web/exports.py`):
    an export that applied nearly the page's filters would be a file nobody can
    account for, and a second copy of this function is one `if` away from
    becoming that.

    A contact-filtered view keeps only `KEPT_WITH_CONTACT`. The rows come from
    the trail and are then read one id at a time, so there is no list request for
    Conduit's own filters to ride on — and a filter this console does not apply
    is one it must not carry either, in the URL, in the pager, in the export's
    filename or in its audit row. The tray renders the dropped ones disabled.
    """
    kind = kind_of(query)
    wire = {
        # All = the four fiat kinds as a repeated `type` parameter, which is one
        # request and one cursor. A single-kind tab sends its one value, so a
        # `?type=deposit` deep link means exactly what it has always meant.
        "type": kind or list(payments.TRANSACTION_TYPES),
        "status": [s for s in query.getlist("status") if s in payments.TRANSACTION_STATUSES]
        or None,
        "sortBy": "createdAt",
        "sortOrder": "desc",
        **{key: query.get(key) or None for key in FILTER_KEYS},
    }
    if contact_of(query):
        return {key: (value if key in KEPT_WITH_CONTACT else None) for key, value in wire.items()}
    return wire


async def _by_contact(
    session: AsyncSession,
    client: ConduitClient,
    *,
    customer_id: str,
    contact_id: str,
    limit: int,
    offset: int,
) -> tuple[list[dict], bool]:
    """The console-linked payouts to one contact, as ledger rows. `(rows, more)`.

    **The trail decides which transactions, and only then is Conduit asked about
    them** — the order the "no pretense of server-side filtering that
    doesn't exist" requires. `counterparties.linked_transactions` is one query
    over this console's own audit rows; this then reads exactly the ids of
    the page being rendered, gathered, never one page-worth of reads per row of
    something else.

    Bounded by the operator's own rows-per-page (25 by default, 100 at most —
    the same control every list here carries), and paged by offset over the id
    list because the trail is local and has no cursor to carry. One extra id is
    asked for so the pager can offer a Next without a second count query, which
    is the idiom `/contacts` and the dashboard already use.

    A transaction the trail names and Conduit will not answer for renders as a
    row that says so. Dropping it would be the one thing this filter must never
    do: silently return fewer payments than were sent.
    """
    ids = await counterparties.linked_transactions(
        session, customer_id=customer_id, contact_id=contact_id, limit=limit + 1, offset=offset
    )
    more = len(ids) > limit
    ids = ids[:limit]
    read = await asyncio.gather(*(payments.fetch_transaction(client, tid) for tid in ids))
    return [
        found if isinstance(found, dict) else {"id": tid, "unreadable": True}
        for tid, found in zip(ids, read)
    ], more


@router.get("/transactions", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    query = request.query_params
    wire = list_query(query)
    # `kind` is the TAB — `""` on All, where the wire carries the four kinds as a
    # list. Everything on screen (the tab strip, the hidden field, the pager, the
    # Reset link) is about the tab, so none of them may take the wire value.
    kind, statuses = kind_of(query), wire["status"] or []
    filters = {key: wire[key] for key in FILTER_KEYS}
    # 25/50/100, validated against the whitelist rather than clamped — and 100
    # is Conduit's own maximum for `limit`, so the top of the control is the top
    # of what the endpoint serves.
    limit = page_size(query)
    customer_id = (query.get("customerId") or "").strip()
    contact_id = contact_of(query)
    # The picker's options: one local SELECT, and only where the field is live.
    # A contact is customer-scoped, so without a customer there is nothing to
    # offer — and the field says so rather than standing there empty.
    ready = bool(customer_id) and kind == "withdrawal"
    contacts = await counterparties.rows(session, customer_id) if ready else []
    contact = next((c for c in contacts if str(c["id"]) == contact_id), None)
    if contact is None and contact_id:
        # **A bookmarked filter keeps working after the contact is archived.**
        # Archiving retires a contact from the pickers; it does not unsend the
        # payments that named it, and answering a link to that history with "No
        # such contact" was the console forgetting what it recorded. The picker
        # above stays live-only — an archived contact is not offered — and the
        # filtered view labels it.
        contact = await counterparties.get(
            session, customer_id, contact_id, include_archived=True
        )

    page = Page(items=[], next_cursor=None, prev_cursor=None, total=None)
    problem = None
    customers = None
    if contact is not None:
        offset = offset_of(query)
        items, more = await _by_contact(
            session,
            client,
            customer_id=customer_id,
            contact_id=contact_id,
            limit=limit,
            offset=offset,
        )
        page = Page(items=items, next_cursor=None, prev_cursor=None, total=None)
        nav = offset_pager(
            "/transactions",
            {"type": kind, "customerId": customer_id, "contact": contact_id},
            offset=offset,
            size=limit,
            more=more,
        )
    elif contact_id:
        # A contact id this customer does not have is answered with a refusal and
        # no rows — never with the unfiltered list, which is a page that looks
        # like an answer to a question it ignored (the `/contacts` rule). A
        # cross-customer id lands here too: `counterparties.rows` is scoped, so
        # another customer's contact is simply not among the options.
        problem = local_problem(
            "No such contact",
            "That contact is not this customer\u2019s, or it has been archived.",
            "Pick one from the list, or clear the filter.",
            resource_id=contact_id,
        )
        nav = offset_pager(
            "/transactions",
            {"type": kind, "customerId": customer_id},
            offset=0,
            size=limit,
            more=False,
        )
    else:
        transactions_read = client.page(
            payments.TRANSACTIONS_PATH,
            cursor=query.get("cursor") or None,
            direction=query.get("direction") or None,
            limit=limit,
            **wire,
        )
        # The customer filter takes an id, so the names for it are fetched
        # alongside the ledger — gathered, never serialised, and silent on
        # failure. `app.web.with_customer_names` holds the whole rule (and the
        # reason). The same read also names the rows: every transaction DTO
        # carries its own `customerName`, so the map is only the fallback for
        # the ones Conduit omitted it on.
        found, customers = await with_customer_names(client, transactions_read)
        if isinstance(found, Page):
            page = found
        else:
            problem = _problem(found, "The transaction list")
        nav = pager(
            "/transactions", {"type": kind, "status": statuses, **filters}, page, size=limit
        )
    return render(
        request,
        "transactions/list.html",
        section="transactions",
        page=page,
        pager=nav,
        problem=problem,
        kind=kind,
        kinds=payments.TRANSACTION_TYPES,
        statuses=payments.TRANSACTION_STATUSES,
        selected_statuses=statuses,
        # `customerId` is carried even on a contact-filtered view, where
        # `list_query` drops every other wire filter: it is the scope the contact
        # itself lives in, and the tray must keep showing it.
        filters={k: v or "" for k, v in filters.items()} | {"customerId": customer_id},
        customers=customers.items if customers else [],
        customers_more=bool(customers and customers.more),
        names=customers.names if customers else {},
        limit=limit,
        amount_of=payments.amount_of,
        converted=payments.converted,
        # One muted sentence per transaction state (spec §4.1). The dict, not a
        # helper: the template asks it for the row's own status and renders
        # nothing when there is no answer.
        next_step=payments.TRANSACTION_NEXT_STEP,
        # The same function the detail page uses. The list rendered the RAW
        # `stage` ("under_review") while the detail rendered "under review" two
        # clicks away — one console, two words for one fact. `stage_label` also
        # carries the unknown-stage fallback ("stage: <raw>"), which the bare
        # value did not.
        stage_label=payments.stage_label,
        can_act=actor.can_any(*MOVE_MONEY),
        # The contact filter: the options, the one in force,
        # and whether the field is live at all.
        contacts=contacts,
        contact=contact,
        contact_id=contact_id,
        contact_ready=ready,
    )


async def _linked_operation(session: AsyncSession, transaction_id: str) -> dict | None:
    """The console's own record of the mutation behind this transaction, newest
    first — a payout that was created and then cancelled has two.

    The cancel is matched by its request path as well as by resource id: an
    action operation only earns a `conduit_resource_id` when it *confirms*, so a
    cancel sitting in `outcome_unknown` — the one state where the operator most
    needs to see a panel instead of a button — was invisible here.
    """
    view = _operation_view(
        await operations.for_resource(
            session, transaction_id, f"{payments.PAYOUT_PATH}/{transaction_id}/cancel"
        )
    )
    if view is not None:
        # Which saved destination the operator addressed this payment from, off
        # the operation's own audit trail. One indexed read,
        # and only here: the other three pages that include this panel are about
        # resources that cannot have one. `{}` renders no row at all. Since slice
        # 2 a whitelist-gated transfer can have one too — the transfer route
        # records the contact whose coordinates Conduit's registered entry
        # proved to be (`app/web/transfers.py`).
        view["counterparty"] = await counterparties.attached(session, view["op"].id)
    return view


@router.get("/transactions/{transaction_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    transaction_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    result = await payments.fetch_transaction(client, transaction_id)
    transaction = result if isinstance(result, dict) else None
    problem = None if transaction else _problem(result, "The transaction", transaction_id)
    kind = str((transaction or {}).get("type") or "")
    # `hasRfi` is Conduit's own derived answer — "at least one published
    # (non-draft, non-cancelled) RFI targets this transaction. Always present;
    # derived at read time, no stored column" (pinned spec, every transaction
    # view DTO). So it gates the second read rather than duplicating it: the
    # overwhelmingly common transaction has no RFI and costs no extra call, and
    # when it does, the panel below is the same one the application page renders.
    held = bool((transaction or {}).get("hasRfi"))
    subject_rfis: list[dict] = []
    if held:
        found, _ok = await rfis.for_subject(client, "transaction", transaction_id)
        # This page needs no `ok` flag: `hasRfi` already told it there is one, so
        # an empty list here can only mean the read did not land, and the
        # template has said so since this panel shipped.
        subject_rfis = await rfis.mark_unacknowledged(session, found)
    # The receiving leg of Conduit's internal transfer arm (live since
    # 2026-09-01). Only this exact shape is typed: `source.type` alone decides,
    # so an `originatingTransactionId` that ever arrives under another type
    # stays a generic row rather than being labelled a transfer it may not be.
    source = (transaction or {}).get("source")
    received_via = (
        "internal_transfer"
        if isinstance(source, dict) and source.get("type") == "internal_transfer"
        else ""
    )
    originating_id = str(source.get("originatingTransactionId") or "") if received_via else ""
    # `side_rows` already drops `type` for every side (`_SKIP_IN_GENERIC`); the
    # originating id joins it only when the typed row above states it in full,
    # and only on this side — a destination carrying the same key keeps its
    # whole walk, because nothing up there would say it.
    if originating_id:
        source = {k: v for k, v in source.items() if k != "originatingTransactionId"}
    return render(
        request,
        "transactions/detail.html",
        section="transactions",
        transaction_id=transaction_id,
        txn=transaction,
        kind=kind,
        typed=kind in TYPED_TEMPLATES,
        problem=problem,
        stage=payments.stage_label(transaction),
        converted=payments.converted(transaction),
        fees=payments.fee_rows(transaction),
        markup=payments.markup_row(transaction),
        source_rows=payments.side_rows(source),
        received_via=received_via,
        originating_id=originating_id,
        destination_rows=payments.side_rows((transaction or {}).get("destination")),
        generic_rows=payments.generic_rows(transaction or {}),
        amount_of=payments.amount_of,
        can_cancel=payments.can_cancel(transaction) and actor.can("payout.cancel"),
        terminal=projections.is_terminal("transactions", (transaction or {}).get("status")),
        operation=await _linked_operation(session, transaction_id),
        can_act=actor.can("payout.cancel"),
        can_simulate=actor.can("sandbox.simulate"),
        can_rfi=actor.can("rfi.respond"),
        can_retry=actor.can("operation.retry"),
        can_abandon=actor.can("operation.abandon"),
        held=held,
        rfis=subject_rfis,
        # `open` alone is what "held, and it is on us" means.
        # `responded` is Conduit's turn: the answer is in, and only
        # `rfi.more_info_requested` — which sets the status back to `open` —
        # puts the ball back on this side. A resolved RFI still sets `hasRfi`
        # and is history, not a to-do.
        awaiting=any(r.get("status") == "open" for r in subject_rfis),
        # Answered but not decided: neither "held, act now" nor "settled".
        answered=any(r.get("status") == "responded" for r in subject_rfis),
        actor_email=actor.email,
    )


# --- sandbox payout simulation --------------------------------------------------------------

# Sandbox host only; the pinned production spec has no `/v2/sandbox` path at all.
REVIEW_PATH = "/v2/sandbox/payouts/{id}/simulate-review-{outcome}"
SETTLE_PATH = "/v2/sandbox/payouts/{id}/simulate/settled"
# The transaction-level forcer, which is what settles a conversion's own leg —
# the payout simulators above are withdrawal-shaped and refuse anything else
# (sandbox spec, read 2026-08-28).
TERMINAL_PATH = "/v2/sandbox/transactions/{id}/simulate/terminal"
REVIEW_OUTCOMES = ("approve", "reject")
SETTLE_OUTCOMES = ("completed", "failed")
TERMINAL_OUTCOMES = ("completed", "failed")


@router.post("/transactions/{transaction_id}/simulate")
async def simulate(
    request: Request,
    transaction_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("sandbox.simulate")),
) -> Response:
    """Review approve/reject and settle, for the sandbox host only.

    Outside the operations ledger by the rule in `app.web`: fake money, fake
    state, and Conduit itself refuses a settle that is premature — that refusal
    is passed through verbatim rather than rewritten, because "you have to
    approve the review first" is the whole answer.
    """
    back = f"/transactions/{transaction_id}"
    if not is_sandbox():
        return redirect(request, back, err="Payout simulation is sandbox-only.")
    values = dict(await form_items(request))
    action = (values.get("action") or "").strip()
    outcome = (values.get("outcome") or "").strip()

    if action == "review" and outcome in REVIEW_OUTCOMES:
        path, body = REVIEW_PATH.format(id=transaction_id, outcome=outcome), {}
    elif action == "settle" and outcome in SETTLE_OUTCOMES:
        path, body = SETTLE_PATH.format(id=transaction_id), {"outcome": outcome}
    elif action == "terminal" and outcome in TERMINAL_OUTCOMES:
        path, body = TERMINAL_PATH.format(id=transaction_id), {"outcome": outcome}
    else:
        return redirect(request, back, err="Unknown simulated action.")

    result = await client.mutate("POST", path, json=body)
    ok = isinstance(result, Success)
    audit.record(
        session,
        action="sandbox.simulate_payout",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"transaction": transaction_id, "action": action, "outcome": outcome, "ok": ok},
    )
    await session.commit()
    if ok:
        return redirect(request, back, msg=f"Simulated {action} {outcome}.")
    # Conduit's own guard message ("settle a payout that has not passed review")
    # is the useful sentence here — passed through, detail included.
    if isinstance(result, Problem):
        note = problem_line(result, "")
        return redirect(request, back, err=note or "Simulation refused.")
    return redirect(request, back, err="Conduit is unreachable")
