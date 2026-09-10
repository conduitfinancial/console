"""The Convert workflow and the orders ledger (plan v2 §7 Conversions).

One module for both because they are one journey: the order detail page *is* the
convert flow's last screen — where an order is followed, executed and cancelled —
and a second module would only mean a second set of imports around the same
three calls.

    GET  /convert                           the same form, source customer first
    GET  /customers/{id}/convert            pick source VA, destination VA, amount, lockSide
    POST /customers/{id}/convert/quote      POST /v2/quotes  (conversion mode, no country)
    POST /customers/{id}/convert/select     the operator picks an option → operation row
    GET  /convert/{operation}               confirm, with the option's own countdown
    POST /convert/{operation}               POST /v2/orders (QuoteRedemptionOrderDto)
    GET  /orders, /orders/{id}              the ledger and the follow screen

`/convert` is where the ribbon's "Convert" lands: the source customer is the
first control of the form rather than a prefix of the URL, and changing it
re-renders the page against their accounts (`#convert-route`, the separate-GET-
form idiom the payout page's `#prefill` uses — HTML has no nested forms). The
customer-scoped path is the same handler and is unchanged, so every action pill,
the payout page's convert hand-off, the re-quote link and every bookmark still
land here with the customer filled in. With nobody picked the form waits and the
three empty states below stay unsaid: they are facts about a customer that was
read, and nobody was.

**Why the operation row exists before the confirm.** OPERATIONS_SPEC §1 wants one
row per logical mutation, created *before* the call — and plan v2 §7 wants the
chosen option id and its expiry stored before confirmation. Selecting an option
therefore inserts the `order_create` row (state `created`, body =
`QuoteRedemptionOrderDto`, so the option id is on the row) and writes the rest of
the choice — expiry, rate, amounts, fees — to that operation's own audit event.
An audit row is an actor-attributed record of a decision, which is exactly what
"the operator chose this option at this price" is; it needs no new column and no
migration, and it is already joined to the operation by `operation_id`.

**The choice travels through the browser, signed.** There is no
`GET /v2/quotes/{id}` in the spec, so a re-read at select time is impossible: the
option id, its `expiresAt`, the price and the two account ids ride back in one
hidden field. That field is HMAC-signed with `SESSION_SECRET` (the same
`app.auth.tokens.sign` the session and CSRF cookies use) and verified before any
operation is created — because this payload is not merely displayed, it becomes
the order's destination and this operation's audit truth, and an unsigned one
made the browser the author of both. A token that is altered,
malformed or older than `SELECTION_TTL` is refused and re-quoted. The expiry is
then re-checked server-side at confirm, because a disabled button is a display
decision.

**Confirming after expiry is refused and re-quoted.** The unsent operation is
transitioned `created → abandoned` (which is what that transition is for) so it
stops holding the §1 double-submit guard, and the operator lands back on the
convert form with the same inputs, ready to re-quote.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import accounts, audit, conversions, operations, payments, projections
from app.auth.actor import Actor
from app.auth.tokens import seal, unseal
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Page, Problem, Success
from app.models import AuditEvent, Operation
from app.web import (
    ALREADY_SPENT_ELSEWHERE,
    MOVE_MONEY,
    conduit,
    db,
    form_items,
    intent_of,
    is_sandbox,
    local_problem,
    page_size,
    pager,
    problem_of,
    problem_note,
    problem_line,
    problem_view,
    redirect,
    reference_of,
    render,
    with_customer_names,
)
# One operation panel in the console (OPERATIONS_SPEC §5), not a second one that
# could disagree with it.
from app.web.applications import _operation_view

router = APIRouter()

CONVERT = "/customers/{customer_id}/convert"
# The same form without a customer in the URL: where the ribbon's
# "Convert" lands. One handler on both paths — FastAPI binds `customer_id` from
# the path where the path has one and from the query string where it does not —
# so the customer-scoped URL every action pill, way-back link and bookmark
# already points at keeps rendering the same form with the customer filled in.
GLOBAL = "/convert"
# The audit action that carries the chosen option's expiry and price.
OPTION_SELECTED = "conversion.option_selected"
# How long a signed selection stays redeemable. Comfortably longer than any quote
# option's own life (live: 5 minutes) — the expiry check below is what actually
# refuses a lapsed option; this only stops an ancient token being replayed.
SELECTION_TTL = 3600


def _problem(result, what: str, resource_id: str = "") -> dict:
    """Conduit's own problem-detail, titled with **which** read failed.

    The `what` used to be dropped whenever Conduit answered a problem at all, so
    a page carrying several reads said only "Conduit is unavailable" at the top
    while a field below it asserted an absence. Naming the read is what lets an
    operator connect the two.
    """
    if isinstance(result, Problem):
        view = problem_view(result)
        return {
            **view,
            "title": f"{what} could not be read"
            + (f" — {view['title']}" if view["title"] else ""),
        }
    return local_problem(
        "Conduit is unreachable", f"{what} could not be read.", resource_id=resource_id
    )


async def _accounts(client: ConduitClient, customer_id: str) -> tuple[list[dict], dict | None]:
    """`(this customer's active virtual accounts, the problem card)` — the shape
    `web.contacts._whitelist` uses, and for the same reason: an unreadable list
    is not an empty one, and this page's empty state sends the operator off to
    request an account."""
    available, failure = await accounts.active(client, customer_id)
    return available, (
        None
        if failure is None
        else _problem(failure, "This customer's virtual accounts", customer_id)
    )


def _asset(account: dict | None) -> str:
    return str(((account or {}).get("asset") or {}).get("code") or "")


def _pick(available: list[dict], wanted: str) -> dict | None:
    return next((a for a in available if a.get("id") == wanted), None)


# --- the convert form ------------------------------------------------------------------


async def _nobody() -> tuple[list[dict], dict | None]:
    """`_accounts`' answer for "no customer is picked yet": no accounts, and no
    problem card either, because nothing was asked and nothing failed."""
    return [], None


async def _render_form(
    request: Request,
    customer_id: str,
    values,
    *,
    client: ConduitClient,
    quote: dict | None = None,
    selections: list[str] | None = None,
    problems: list[dict] | None = None,
    can_act: bool = True,
    status_code: int = 200,
) -> Response:
    # One bounded, gathered customers read (`with_customer_names`) beside this
    # customer's accounts: it fills the source-customer picker at the head of the
    # form. `customer_id` may be empty — the ribbon lands here with nobody picked
    # — and that is a state to render, not a customer to read: no accounts call
    # is made for the empty string, and none of the three empty states below is
    # claimed about somebody nobody asked about.
    (available, unreadable), listed = await with_customer_names(
        client, _accounts(client, customer_id) if customer_id else _nobody()
    )
    problems = [p for p in (problems or []) if p] + [p for p in (unreadable,) if p]
    source = _pick(available, (values.get("source") or "").strip()) or (
        available[0] if available else None
    )
    asset = _asset(source)
    # The destination is any active account of the *other* currency — a
    # conversion between two accounts holding the same asset is not a thing.
    candidates = [a for a in available if a is not source and _asset(a) != asset]
    destination = _pick(candidates, (values.get("destination") or "").strip()) or (
        candidates[0] if candidates else None
    )
    # The way back, when this conversion was started from a payout the rail/asset
    # guard refused. It carries the payout page's four ROUTE
    # parameters and the account this conversion lands in — nothing else. No
    # amount (what the conversion delivers is what there is to send, so it is
    # answered again over there) and nothing nonce-like: the payout page mints
    # its intent per render (OPERATIONS_SPEC §1), so the return trip has to be
    # an ordinary GET whose render mints a fresh one.
    #
    # The link lives on this form only — the route travels in `values`,
    # so it survives the quote round trip through the hidden fields below the
    # same way `internal_reference` does, and stops at the signed selection. The
    # payout page's own hand-off sentence describes the whole round trip
    # (settlement included), which is what an operator returning tomorrow reads;
    # threading the route through the selection token, the operation row and the
    # order page would be four surfaces for one link.
    route = {key: str(values.get(key) or "").strip() for key in payments.ROUTE_KEYS}
    # The way-back names a funding account only when the ROUTE'S OWN RAIL can be
    # paid from it: an operator who arrived from
    # a fedwire payout and then flipped the selects to convert INTO EUR would
    # otherwise be promised a funding pick the payout page immediately disables
    # ("EUR — not on fedwire"). The bare way-back — route without an account — is
    # a branch the template already renders honestly. And no rail means no
    # payout provenance at all (minor 3): a lone crafted ?purpose= must not make
    # this page claim "started from a payout on ``" with an empty rail.
    # ...and no CUSTOMER means the same: the
    # global /convert can now carry route params with nobody picked, and a
    # way-back built without a customer was a provenance claim over a dead link
    # to /customers//payouts/new. The real hand-off always carries its customer.
    if not route.get("rail") or not customer_id:
        route = {}
    funding = str((destination or {}).get("id") or "")
    if funding and payments.doomed_rail(route.get("rail", ""), _asset(destination)):
        funding = ""
    payout_return = (
        f"/customers/{customer_id}/payouts/new?" + payments.route_query(route, virtualAccountId=funding)
        if any(route.values())
        else ""
    )
    return render(
        request,
        "convert/new.html",
        section="orders",  # Transact: form, quote re-render and confirm all agree
        status_code=status_code,
        customer_id=customer_id,
        customer_name=listed.names.get(customer_id, ""),
        customers=listed.items,
        customers_more=listed.more,
        accounts=available,
        # "we could not read them" is not "there are none" — the empty state
        # below branches on this rather than on the length of the list.
        accounts_listed=unreadable is None,
        candidates=candidates,
        source=source,
        destination=destination,
        asset=asset,
        destination_asset=_asset(destination),
        wanted=conversions.opposite_assets(asset),
        convert_assets=conversions.CONVERT_ASSETS,
        amount=(values.get("amount") or "").strip(),
        lock_side=(values.get("lockSide") or conversions.LOCK_SIDE_VALUES[0]).strip(),
        lock_sides=conversions.LOCK_SIDES,
        reference=(values.get("internal_reference") or "").strip(),
        quote=quote,
        selections=selections or [],
        payout_route=route,
        payout_return=payout_return,
        problems=problems,
        can_act=can_act,
    )


@router.get(GLOBAL, response_class=HTMLResponse)
@router.get(CONVERT, response_class=HTMLResponse)
async def new(
    request: Request,
    customer_id: str = "",
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    return await _render_form(
        request,
        customer_id,
        request.query_params,
        client=client,
        can_act=actor.can("order.create"),
    )


@router.post(CONVERT + "/quote", response_class=HTMLResponse)
async def quote(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """`POST /v2/quotes` in conversion mode.

    Not ledgered and carrying **no** `Idempotency-Key`: this endpoint refuses one
    (verified live, OPERATIONS_SPEC §5), and a quote creates nothing, reserves
    nothing, and can never leave an outcome unknown.
    """
    values = dict(await form_items(request))
    # The re-render below reads the accounts itself and carries the failure card;
    # this read only has to resolve the two ids the operator picked.
    available, _unreadable = await _accounts(client, customer_id)
    source = _pick(available, (values.get("source") or "").strip())
    destination = _pick(available, (values.get("destination") or "").strip())
    amount = payments.amount(values.get("amount") or "")
    lock_side = (values.get("lockSide") or "").strip()

    problem = None
    if source is None or destination is None:
        problem = local_problem(
            "Nothing to price", "Pick an active source and destination account first."
        )
    elif _asset(source) == _asset(destination):
        problem = local_problem(
            "Same currency on both sides",
            f"Both accounts hold {_asset(source)}. Moving money at one currency is a transfer.",
            "Use the transfers screen instead.",
        )
    elif amount is None:
        problem = local_problem(
            "Amount", "The amount must be a positive decimal, e.g. 1000.00."
        )
    elif lock_side not in conversions.LOCK_SIDE_VALUES:
        problem = local_problem("Lock side", "Pick which side of the conversion is fixed.")
    if problem is not None:
        return await _render_form(
            request, customer_id, values, client=client, problems=[problem], status_code=422
        )

    result = await client.mutate(
        "POST",
        conversions.QUOTE_PATH,
        json=conversions.quote_request(
            source=_asset(source),
            destination=_asset(destination),
            amount_text=amount or "",
            lock_side=lock_side,
        ),
    )
    if not isinstance(result, Success):
        return await _render_form(
            request,
            customer_id,
            values,
            client=client,
            problems=[_problem(result, "The quote")],
            status_code=422,
        )
    view = payments.quote_view(result.data)
    selections = [
        _sealed(
            {
                **conversions.selection(view, option),
                "amount": amount,
                # Signed alongside the price: the order's two accounts are part
                # of what the operator agreed to, not a separate field the
                # browser gets to restate at redemption time.
                "source": source["id"],
                "destination": destination["id"],
            }
        )
        for option in (view or {}).get("options") or []
    ]
    return await _render_form(
        request,
        customer_id,
        values,
        client=client,
        quote=view,
        selections=selections,
        can_act=actor.can("order.create"),
    )


def _sealed(chosen: dict) -> str:
    """The chosen option, signed for the round trip to the browser and back.

    The confirm screen's countdown, the price the operator agreed to, **and the
    two account ids the order will name** all travel through a hidden field, and
    all of them are then persisted as this operation's audit truth. Unsigned,
    that made the browser the author of the audit record and of the order's
    destination. Same seal helper payouts.py's quote uses (`app.auth.tokens`)
    — nothing new to get wrong.
    """
    return seal(chosen, ttl=SELECTION_TTL)


def _selection(raw: str) -> dict:
    """The signed selection, back from the browser — or `{}` for anything
    tampered with, malformed or older than `SELECTION_TTL`."""
    return unseal(raw) or {}


@router.post(CONVERT + "/select")
async def select_option(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("order.create")),
) -> Response:
    """Create the `order_create` operation row and record what was chosen. No
    Conduit call — the confirm screen sends it."""
    values = dict(await form_items(request))
    # Everything that matters comes out of the signed blob, including which
    # accounts the order names — a tampered or stale one yields `{}` and is
    # refused rather than partially trusted.
    chosen = _selection(values.get("selection") or "")
    available, _unreadable = await _accounts(client, customer_id)
    source = _pick(available, str(chosen.get("source") or ""))
    destination = _pick(available, str(chosen.get("destination") or ""))
    option_id = str(chosen.get("quoteOptionId") or "")

    if not option_id or source is None or destination is None:
        return await _render_form(
            request,
            customer_id,
            values,
            client=client,
            problems=[
                local_problem(
                    "That option could not be redeemed",
                    "The quote this console showed could not be verified — it was altered, or it "
                    "is too old. Nothing was sent.",
                    "Re-quote and pick an option again.",
                )
            ],
            status_code=422,
        )
    # `MONEY_CEILING` against what leaves the SOURCE account —
    # Conduit's own computed `sourceAmount` off the option, not the typed amount,
    # because on `lockSide=destination` the typed number is what LANDS, not what
    # goes. It is signed alongside the price, so the browser is not the author of
    # the number this guard reads. It is rendered money ("999.00 USD"), so the
    # figure is its first token; `over_ceiling` refuses anything it cannot then
    # read, and `test_web_convert.py` pins that this token parses.
    # `or "missing"` is load-bearing: `payments._money` renders an absent or
    # malformed amount block as `""`, and `over_ceiling("")` returns None — an
    # option that carried no source amount would have sailed straight past the
    # ceiling (minor 4). A non-numeric string takes the
    # fail-closed branch instead, which is the right answer to "the number this
    # guard needs is not there".
    source_amount = str(chosen.get("sourceAmount") or "").split(" ")[0]
    if refusal := payments.over_ceiling(source_amount or "missing"):
        return await _render_form(
            request,
            customer_id,
            values,
            client=client,
            problems=[local_problem("Above this deployment's ceiling", refusal)],
            status_code=422,
        )
    if payments.expired(chosen.get("expiresAt")):
        return await _render_form(
            request,
            customer_id,
            values,
            client=client,
            problems=[
                local_problem(
                    "That option has already expired",
                    "Quote options are short-lived. Re-quote and pick again.",
                )
            ],
            status_code=422,
        )

    op, is_new = await operations.start(
        session,
        type="order_create",
        actor_id=actor.id,
        actor_email=actor.email,
        path=conversions.ORDER_PATH,
        body=conversions.order_body(
            quote_option_id=option_id,
            source_id=source["id"],
            destination_id=destination["id"],
        ),
        customer_id=customer_id,
        intent=intent_of(values),
        # Console-local: on the row, never in the `QuoteRedemptionOrderDto` and
        # never in `request_hash`. It rides in on the Choose form's
        # `hx-include`, not in the signed selection — it is the operator's note,
        # not part of the price they agreed to.
        reference=reference_of(values),
    )
    if is_new:
        audit.record(
            session,
            action=OPTION_SELECTED,
            actor_id=actor.id,
            actor_email=actor.email,
            operation_id=op.id,
            detail=chosen,  # already carries source/destination, and is signed
        )
        await session.commit()
    return redirect(request, f"/convert/{op.id}")


# --- the confirm screen ----------------------------------------------------------------


async def _operation(session: AsyncSession, operation_id: str) -> Operation:
    try:
        op_id = uuid.UUID(operation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such operation")
    op = await session.get(Operation, op_id)
    if op is None or op.type != "order_create":
        raise HTTPException(status_code=404, detail="No such conversion")
    return op


async def _selection_of(session: AsyncSession, op: Operation) -> dict:
    """What the operator chose, off the operation's own audit trail."""
    row = (
        await session.execute(
            select(AuditEvent)
            .where(AuditEvent.operation_id == op.id, AuditEvent.action == OPTION_SELECTED)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(row.detail or {}) if row is not None else {}


def _requote_url(customer_id: str, chosen: dict) -> str:
    return (
        f"/customers/{customer_id}/convert?source={chosen.get('source', '')}"
        f"&destination={chosen.get('destination', '')}"
        f"&amount={chosen.get('amount', '')}&lockSide={chosen.get('lockSide', '')}"
    )


def _confirm(
    request: Request,
    op: Operation,
    chosen: dict,
    *,
    problem: dict | None = None,
    can_act: bool = True,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "convert/confirm.html",
        section="orders",  # Transact, same as the form it came from
        status_code=status_code,
        op=op,
        customer_id=op.customer_id or "",
        chosen=chosen,
        stale=payments.expired(chosen.get("expiresAt")),
        requote=_requote_url(op.customer_id or "", chosen),
        problem=problem,
        can_act=can_act,
    )


@router.get("/convert/{operation_id}", response_class=HTMLResponse)
async def confirm(
    request: Request,
    operation_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    op = await _operation(session, operation_id)
    if op.state != "created":
        return _sent(request, op)
    return _confirm(
        request, op, await _selection_of(session, op), can_act=actor.can("order.create")
    )


def _sent(request: Request, op: Operation) -> Response:
    """Where an already-sent conversion goes: its order if there is one, its
    operation status page otherwise (which is what `outcome_unknown` and
    `stalled` look like — never a second send button)."""
    if op.state == "confirmed" and op.conduit_resource_id:
        return redirect(request, f"/orders/{op.conduit_resource_id}")
    return redirect(request, f"/operations/{op.id}")


@router.post("/convert/{operation_id}")
async def send(
    request: Request,
    operation_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("order.create")),
) -> Response:
    op = await _operation(session, operation_id)
    if op.state != "created":
        return _sent(request, op)

    chosen = await _selection_of(session, op)
    if payments.expired(chosen.get("expiresAt")):
        # Server-side, because the disabled button is a display decision. The
        # unsent row is abandoned so it stops holding the §1 guard, and the
        # operator lands back on the form with the same inputs.
        await operations.transition(
            session,
            op.id,
            "abandoned",
            actor_id=actor.id,
            actor_email=actor.email,
            detail={"reason": "quote_option_expired"},
        )
        return redirect(
            request,
            _requote_url(op.customer_id or "", chosen),
            err="That quote option expired before it was confirmed — re-quote and pick again.",
        )

    op = await execute_operation(
        session, op, client=client, actor_id=actor.id, actor_email=actor.email
    )
    if op.state == "confirmed" and op.conduit_resource_id:
        return redirect(request, f"/orders/{op.conduit_resource_id}")
    if op.state == "rejected":
        # A refusal here is usually the option: expired between the check and the
        # call, or already redeemed. The re-quote link is on the page.
        return _confirm(
            request,
            op,
            chosen,
            problem=problem_of(op.error, op.conduit_resource_id),
            can_act=actor.can("order.create"),
            status_code=422,
        )
    return redirect(request, f"/operations/{op.id}")


# --- the orders ledger -----------------------------------------------------------------


def list_query(query) -> dict:
    """The orders list's filters as the Conduit query they produce — paging
    aside. One parser for the page and for its CSV export
    (`app/web/exports.py`)."""
    return {
        "status": [s for s in query.getlist("status") if s in conversions.ORDER_STATUSES] or None,
        "clientReferenceId": query.get("clientReferenceId") or None,
    }


@router.get("/orders", response_class=HTMLResponse)
async def index(
    request: Request,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The Transact page: the move-money launcher, then `GET /v2/orders`.

    Unlike `/v2/transactions`, `type` is optional on orders, so this list has an
    "everything" view — the filters are the ones the endpoint actually offers.

    The launcher is a second, independent read: an operator picks a customer and
    the browser sends them to that customer's payout, transfer or convert
    screen. It is fetched only for operators (a viewer cannot move money and is
    not shown the tray) and its failure is local — the orders list below renders
    either way.
    """
    query = request.query_params
    wire = list_query(query)
    statuses = wire["status"] or []
    limit = page_size(query)
    orders_read = client.page(
        conversions.ORDER_PATH,
        cursor=query.get("cursor") or None,
        direction=query.get("direction") or None,
        limit=limit,
        **wire,
    )
    # Two independent reads, so they wait together — `with_customer_names` is
    # that gather, and the reason it is one shared helper rather than a per-page
    # `asyncio.gather` is in its docstring.
    #
    # **The read is no longer operator-only** (it was: a viewer sees no
    # launcher, so the request that only filled it was skipped).
    # An order row carries `customerId` and no name, so the same read now answers
    # a question a *viewer* has too — who is this order for — and skipping it for
    # them would mean the same list naming its customers or not depending on who
    # is signed in. The budget is unchanged: one bounded page, gathered, and its
    # failure is still shown loudly to the operator whose tray it fills, and
    # silently to everyone else.
    page, customers = await with_customer_names(client, orders_read)
    customers_problem = (
        _problem(customers.failure, "The customer list")
        if customers.failure is not None and actor.can_any(*MOVE_MONEY)
        else None
    )
    problem = None
    if not isinstance(page, Page):
        problem = _problem(page, "The order list")
        page = Page(items=[], next_cursor=None, prev_cursor=None, total=None)
    return render(
        request,
        "orders/list.html",
        section="orders",
        page=page,
        pager=pager(
            "/orders",
            {"status": statuses, "clientReferenceId": query.get("clientReferenceId")},
            page,
            size=limit,
        ),
        problem=problem,
        statuses=conversions.ORDER_STATUSES,
        selected_statuses=statuses,
        view=conversions.order_view,
        customers=customers.items,
        customers_problem=customers_problem,
        customers_more=customers.more,
        names=customers.names,
        limit=limit,
        can_act=actor.can_any(*MOVE_MONEY),
    )


async def _linked_operation(session: AsyncSession, order_id: str) -> dict | None:
    """This console's own record of the newest mutation against this order.

    `order_execute` and `order_cancel` carry no `conduit_resource_id` until they
    confirm, so they are matched by their action paths too — otherwise an
    execute whose response was lost left the page still offering an Execute
    button, with nothing on screen saying one is already unresolved.
    """
    return _operation_view(
        await operations.for_resource(
            session,
            order_id,
            f"{conversions.ORDER_PATH}/{order_id}/execute",
            f"{conversions.ORDER_PATH}/{order_id}/cancel",
        )
    )


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    order_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    # The order's own `customerId` has no name on it, and the customers read
    # does not depend on the order — so the same gathered, bounded resolver the
    # list uses costs this page nothing in latency. A customer beyond the first
    # page resolves to nothing and the head shows the id, which is what it
    # showed before.
    result, customers = await with_customer_names(client, conversions.fetch_order(client, order_id))
    order = result if isinstance(result, dict) else None
    return render(
        request,
        "orders/detail.html",
        section="orders",
        order_id=order_id,
        order=order,
        names=customers.names,
        view=conversions.order_view(order),
        rows=conversions.order_rows(order),
        problem=None if order else _problem(result, "The order", order_id),
        terminal=projections.is_terminal("orders", (order or {}).get("status")),
        can_execute=conversions.can_execute(order) and actor.can("order.execute"),
        can_cancel=conversions.can_cancel(order) and actor.can("order.cancel"),
        simulations=conversions.SIMULATE_ACTIONS,
        operation=await _linked_operation(session, order_id),
        can_act=actor.can_any("order.execute", "order.cancel"),
        can_simulate=actor.can("sandbox.simulate"),
        can_retry=actor.can("operation.retry"),
        can_abandon=actor.can("operation.abandon"),
    )


async def _act(
    request: Request,
    session: AsyncSession,
    client: ConduitClient,
    actor: Actor,
    *,
    order_id: str,
    type: str,
    action: str,
    intent: uuid.UUID | None = None,
) -> Response:
    """`execute` and `cancel` — same three steps, different verb."""
    back = f"/orders/{order_id}"
    path = f"{conversions.ORDER_PATH}/{order_id}/{action}"
    op, is_new = await operations.start(
        session,
        type=type,
        actor_id=actor.id,
        actor_email=actor.email,
        path=path,
        body=None,
        intent=intent,
    )
    # **A spent nonce replayed at a different order**. The nonce alone
    # resolves, and it is scoped to the operation *type*, so an execute token
    # spent on order A answered this execute of order B — and the "Order
    # {action} accepted." below then said so about an order nobody touched,
    # which for an execute means the money did not move and for a cancel means
    # it still will. Both verbs come through here, so both are guarded once.
    if not is_new and operations.resolved_elsewhere(op, path):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SPENT_ELSEWHERE)
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed":
        return redirect(request, back, msg=f"Order {action} accepted.")
    if op.state == "rejected":
        # `INSUFFICIENT_FUNDS` is the one an operator acts on: the order stays
        # pending and executing again after funding is the fix, so the refusal
        # itself is what gets shown — in this console's words, translated from
        # the stored body by the one table (A3 gate, M1: this read `title` and
        # `detail` straight out of it).
        return redirect(
            request, back, err=problem_note(op.error, f"Order {action} refused.")
        )
    return redirect(request, f"/operations/{op.id}")


@router.post("/orders/{order_id}/execute")
async def execute(
    request: Request,
    order_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("order.execute")),
) -> Response:
    return await _act(
        request,
        session,
        client,
        actor,
        order_id=order_id,
        type="order_execute",
        action="execute",
        intent=intent_of(dict(await form_items(request))),
    )


@router.post("/orders/{order_id}/cancel")
async def cancel(
    request: Request,
    order_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("order.cancel")),
) -> Response:
    return await _act(
        request,
        session,
        client,
        actor,
        order_id=order_id,
        type="order_cancel",
        action="cancel",
        intent=intent_of(dict(await form_items(request))),
    )


@router.post("/orders/{order_id}/simulate")
async def simulate(
    request: Request,
    order_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("sandbox.simulate")),
) -> Response:
    """The sandbox order simulators, for the sandbox host only.

    Outside the operations ledger by the rule in `app.web`: fake money, fake
    state. `simulate/cosign` is deliberately absent — it is the non-custodial
    crypto signing leg, which v1 does not touch.
    """
    back = f"/orders/{order_id}"
    if not is_sandbox():
        return redirect(request, back, err="Order simulation is sandbox-only.")
    action = (dict(await form_items(request)).get("action") or "").strip()
    if action not in conversions.SIMULATE_VALUES:
        return redirect(request, back, err="Unknown simulated action.")

    result = await client.mutate(
        "POST", conversions.SIMULATE_PATH.format(id=order_id, action=action), json={}
    )
    ok = isinstance(result, Success)
    audit.record(
        session,
        action="sandbox.simulate_order",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"order": order_id, "action": action, "ok": ok},
    )
    await session.commit()
    if ok:
        return redirect(request, back, msg=f"Simulated {action}.")
    if isinstance(result, Problem):
        note = problem_line(result, "")
        return redirect(request, back, err=note or "Simulation refused.")
    return redirect(request, back, err="Conduit is unreachable")
