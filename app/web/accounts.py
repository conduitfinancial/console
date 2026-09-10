"""Virtual accounts: the all-customers index, and one account's detail page
(plan v2 §7 Accounts).

Two pages with opposite reading habits, which is why they read from opposite
places:

* **`GET /accounts`** is the operator's "show me every account" view. There is
  no global virtual-accounts endpoint — the only list Conduit offers is nested
  under one customer (`/v2/customers/{id}/virtual-accounts`, the pinned spec has
  no other) — so a cross-customer view could only be built by walking every
  customer, one request each. It is built from this installation's own
  projections instead: one local SELECT, so the page renders when Conduit is the
  thing that is wrong.

  **Its contract is availability, not asceticism**. It used to make *no Conduit call at
  all*, and that was the right rule while the alternative was a convenience datalist
  read that a black-holed host could stall the page on. What the page owes an operator
  is that it **renders completely with zero Conduit answers** — and the console's one
  resolver (`app.web.with_customer_names`) is exactly that shape: bounded, gathered, and
  silent on failure, so no answer means no names and no banner, with every row still on
  screen carrying its id. So the page now makes that one read, and the customer column
  shows people rather than `cus_…` strings, which is what the operator who reported this
  actually needed. One read serves both the names and the filter's suggestion list.

  Selecting a customer adds a second bounded read — Conduit's own list for that
  one customer, with real balances — and the charter holds through it the same
  way: when that read fails the page falls back to the observed rows, labelled,
  under a banner saying so. "Renders completely with zero Conduit answers" is a
  property of every path, not of the default one.

  `customerName` is deliberately NOT cached on the virtual-account projections:
  the payload keys that survive the PII scrub are a contract (`_READ_FROM_
  PROJECTIONS`), and a name copied into a webhook payload goes stale silently.
* **the detail page** is a live read, because the projection exists for list
  views and the reconciler, not to answer "what are this account's coordinates".

The
deposit-instruction card is the point of the page: those coordinates are what an
operator hands to whoever is sending the money, so every one of them is a
labelled copy row rather than a paragraph to squint at.

The simulator is sandbox-only, and refused server-side as well as hidden in the
template — a template condition is a display decision, not an authorization one.
It sits outside the operations ledger by the same rule as the decision simulator
(`app.web`): test scaffolding for an environment whose money is fake by
construction. A double-click there really would credit twice; on a throwaway
host that is a cheaper trade than a ledger row per fake deposit, and the action
is audited either way.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import accounts, audit, projections
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem, Result, Success
from app.models import Projection
from app.web import (
    MOVE_MONEY,
    conduit,
    db,
    form_items,
    is_sandbox,
    local_problem,
    no_second_read,
    offset_of,
    offset_pager,
    page_size,
    problem_line,
    problem_view,
    redirect,
    render,
    with_customer_names,
)
# The dashboard's "which timestamp is this row's" rule, not a second copy of it:
# a projection carries the event time and falls back to `updated_at` for a row
# written before one arrived. Same cross-module borrow `app/web/transactions.py`
# makes of `applications._operation_view`, and for the same reason.
from app.web.dashboard import _observed

router = APIRouter()

KIND = "virtual_accounts"
# The status vocabulary, read off the ladder the pills and the reconciler
# already share (`projections._LADDERS`) rather than written out again — in
# ladder order, which is the order an account moves through them.
STATUSES: tuple[str, ...] = tuple(projections.STATE_RANKS[KIND])


# --- the all-customers index ------------------------------------------------------------


def _payload(*path: str):
    """One JSONB pointer into `projections.payload`, as SQL.

    The keys are the ones a `virtual_account.activated` delivery carries —
    `virtualAccountId`, `customerId`, `asset.code`, `activatedAt` (confirmed
    against `GET /v2/webhooks/event-types` on the sandbox, 2026-08-29). A row
    whose payload lacks one is simply not matched by that filter; it is never
    guessed at.
    """
    column = Projection.payload[path[0]] if len(path) == 1 else Projection.payload[path]
    return column.astext


def list_filters(query) -> dict:
    """This page's three filters, validated as the page validates them.

    One parser for the page and for its CSV export (`app/web/exports.py`) — the
    local equivalent of the Conduit lists' `list_query`.
    """
    # A status outside the ladder is dropped rather than sent to the database as
    # a filter that can only match nothing: a typo in a URL should show the
    # unfiltered page, not an empty one (the rule `app/web/rfis.py` follows).
    status = query.get("status") or ""
    return {
        "asset": (query.get("asset") or "").strip().upper(),
        "status": status if status in STATUSES else "",
        "customerId": (query.get("customerId") or "").strip(),
    }


def where(filters: dict) -> list:
    """Those filters as SQL over the virtual-account projections."""
    clauses = [Projection.resource_kind == KIND]
    if filters["asset"]:
        clauses.append(_payload("asset", "code") == filters["asset"])
    if filters["status"]:
        clauses.append(Projection.state == filters["status"])
    if filters["customerId"]:
        clauses.append(_payload("customerId") == filters["customerId"])
    return clauses


def row_of(projection: Projection) -> dict:
    """One projection as the row both the table and the CSV are built from."""
    payload = projection.payload if isinstance(projection.payload, dict) else {}
    owner = payload.get("customerId")
    owner = owner if isinstance(owner, str) and owner else ""
    asset_block = payload.get("asset")
    code = asset_block.get("code") if isinstance(asset_block, dict) else None
    return {
        "id": projection.resource_id,
        "customer_id": owner,
        # The account detail page is customer-scoped, because Conduit's own read
        # path is. Without a customer id there is no URL to build, so the row
        # renders unlinked and says why rather than linking to a 404.
        "url": f"/customers/{owner}/accounts/{projection.resource_id}" if owner else "",
        "asset": code if isinstance(code, str) else "",
        "state": projection.state,
        "observed": projection.observed_at or projection.updated_at,
    }


async def holders(session: AsyncSession, asset: str) -> set[str] | None:
    """Customers this console has **observed** to hold an active `{asset}`
    account — one local SELECT, no Conduit call.

    `GET /v2/customers` cannot filter by holdings and a live read per candidate
    is the banned per-row pattern, so the only affordable answer is this
    installation's own projections. It is a suggestion filter and nothing more:
    *observed* is not *all* — an account no webhook or read ever landed for is
    simply absent — so every surface that narrows a picker with this says
    "observed" out loud and still accepts a typed id, which is resolved live.

    The clauses are `where`'s, not a second copy of them: the same three filters
    the accounts index validates, with `status` pinned to active.

    Returns `None` — no information, not an empty answer — when this
    installation has observed NO virtual accounts at all: a fresh deploy before
    its webhook is registered, or local development with no public URL. An empty
    observed set filtered against would be filtering on ignorance (every
    suggestion vanishes because the console knows nothing), and "observed none
    holding EUR" is only a meaningful narrowing when something HAS been
    observed. Callers treat None as "the filter has nothing to say" and leave
    the picker unfiltered. Found the hard way (2026-09-02): a webhook-less
    install rendered zero destination suggestions for every currency.
    """
    if not asset:
        return set()
    any_observed = await session.scalar(
        select(func.count()).select_from(Projection).where(*where({"asset": "", "status": "", "customerId": ""})).limit(1)
    )
    if not any_observed:
        return None
    rows = await session.scalars(
        select(distinct(_payload("customerId"))).where(
            *where({"asset": asset.upper(), "status": "active", "customerId": ""})
        )
    )
    return {row for row in rows if row}


@router.get("/accounts", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Every observed virtual account, across every customer. Read-only.

    Local SELECTs for the rows, plus the console's ONE bounded customers read
    for their names and the filter's suggestions — see the module docstring for
    why that read does not cost this page its availability.

    **Read budget: 1** (`GET /v2/customers`, limit 25) with no customer
    selected, **2** with one — the second being that customer's own accounts
    (`GET /v2/customers/{id}/virtual-accounts`), which is the read the selection
    exists to make. Both are bounded and neither is per-row: a cross-customer
    walk is still the thing this page refuses to do, whatever the filters say
    and however many rows are on screen.

    Availability survives the second read because a failure falls back to the
    projections rather than emptying the table — the page still renders with
    zero Conduit answers, which is the charter above.
    """
    query = request.query_params
    filters = list_filters(query)
    asset, status, customer_id = filters["asset"], filters["status"], filters["customerId"]
    size, offset = page_size(query), offset_of(query)

    # A failed live read falls through to the projections rather than rendering
    # a customer with no accounts. The charter above is availability: this page
    # renders completely with zero Conduit answers, and selecting a customer is
    # not allowed to be the one path that stops being true. What the operator
    # gets is the observed rows, labelled as observed, under the banner saying
    # the live read failed — which is strictly more than the empty table that
    # stood here, and honest about which of the two it is.
    live_problem = None
    if customer_id:
        live_page = await accounts.fetch_accounts(
            client,
            customer_id,
            cursor=query.get("accounts_cursor") or None,
            direction=query.get("accounts_direction") or None,
            asset=asset,
            limit=size,
        )
        if not isinstance(live_page, Page):
            live_problem = (
                problem_view(live_page)
                if isinstance(live_page, Problem)
                else local_problem(
                    "Conduit is unreachable", "This customer's accounts could not be read just now."
                )
            )
    if customer_id and live_problem is None:
        raw_items = live_page.items
        items = [i for i in raw_items if i.get("status") == status] if status else raw_items
        # This page's own asset options, the same "only what's on file" rule the
        # projection branch follows. The selected asset is unioned in because
        # Conduit now narrows the read to it: without that the select would hold
        # one option and could not render a value it had just been given. It
        # does mean switching straight from one asset to another is a two-step
        # (clear, then pick) — the honest cost of a single-read page, and
        # cheaper than the wrong answer it replaces.
        live_assets = sorted(
            {code for i in raw_items if (code := (i.get("asset") or {}).get("code"))}
            | ({asset} if asset else set())
        )
        _local, listed = await with_customer_names(client, no_second_read())
        return render(
            request,
            "accounts/list.html",
            section="accounts",
            live=True,
            rows=items,
            live_filtered_to_empty=bool(asset or status) and not items,
            pager=offset_pager("/accounts", filters, offset=offset, size=size, more=False),
            live_page=live_page,
            live_problem=live_problem,
            balance_rows=accounts.balance_rows,
            names=listed.names,
            customers=listed.items,
            customers_more=listed.more,
            assets=live_assets,
            statuses=STATUSES,
            asset=asset,
            status=status,
            customer_id=customer_id,
            filtered=True,
            can_request_account=actor.can("account.request"),
        )

    # The asset options are the assets actually on file — one SELECT DISTINCT,
    # so the select never offers a currency this console has no row for and
    # never hides one it does. Sorted in Python: Postgres refuses an ORDER BY
    # whose expression it cannot match to the DISTINCT select item, and a
    # handful of currency codes is not a sort worth arguing with the planner
    # about.
    assets = sorted(
        code
        for code in (
            (
                await session.execute(
                    select(distinct(_payload("asset", "code"))).where(
                        Projection.resource_kind == KIND
                    )
                )
            )
            .scalars()
            .all()
        )
        if code
    )
    # One extra row, so the pager knows whether there is a Next without a count
    # query (the dashboard's idiom, and `/contacts`' offset paging).
    #
    # `resource_id` is the tiebreaker, and it is not decoration: offset paging
    # over a non-deterministic order silently drops and repeats rows across the
    # page turn, and a backfill writes many projections with one timestamp.
    found = (
        (
            await session.execute(
                select(Projection)
                .where(*where(filters))
                .order_by(_observed().desc(), Projection.resource_id)
                .limit(size + 1)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    rows = [row_of(projection) for projection in found[:size]]
    # The page's one Conduit read: names for the column, suggestions for the
    # filter. Its failure is the honest miss — ids alone, no banner.
    _local, listed = await with_customer_names(client, no_second_read())
    return render(
        request,
        "accounts/list.html",
        section="accounts",
        live=False,
        rows=rows,
        # Set only when a live read for a selected customer failed and this
        # branch is standing in for it.
        live_problem=live_problem,
        names=listed.names,
        customers=listed.items,
        customers_more=listed.more,
        pager=offset_pager(
            "/accounts", filters, offset=offset, size=size, more=len(found) > size
        ),
        assets=assets,
        statuses=STATUSES,
        asset=asset,
        status=status,
        customer_id=customer_id,
        filtered=bool(asset or status or customer_id),
    )


# --- one account ------------------------------------------------------------------------

SIMULATE_PATH = "/v2/sandbox/customers/{cid}/virtual-accounts/{vid}/deposits/simulate"
# `SimulateFiatDepositDtoClass` (sandbox OpenAPI): `{assetAmount{code,amount},
# outcome?, rail?, externalReference?, detectedAt?, senderInfo?}`. `rail` is
# **uppercase** here — the only place in the API that spells rails that way.
OUTCOMES = ("completed", "frozen", "returned")
RAILS = ("ACH", "FEDWIRE", "RTP")


async def _deposits(client: ConduitClient, customer_id: str, virtual_account_id: str):
    """One page of the customer's deposits, narrowed to this account.

    `type` is a required query parameter on `/v2/transactions`; there is no
    virtual-account filter, so the narrowing is local (`accounts.deposits_for`).
    """
    page = await client.page(
        "/v2/transactions",
        customerId=customer_id,
        type="deposit",
        limit=25,
        sortBy="createdAt",
        sortOrder="desc",
    )
    if isinstance(page, Page):
        return accounts.deposits_for(page.items, virtual_account_id), None
    problem = (
        problem_view(page)
        if isinstance(page, Problem)
        else local_problem("Conduit is unreachable", "The deposit history could not be read.")
    )
    return [], problem


UNKNOWN_CURRENCY = (
    "This account's currency could not be read just now, and a simulated deposit is "
    "denominated in it. Refresh the page and try again."
)


def simulate_code(account: dict | Result | None) -> tuple[str | None, str]:
    """`(the one currency a deposit into this account lands in, "")`, or
    `(None, the sentence that refuses the simulation)`.

    A deposit's asset is fixed by the account it lands in, never by a separate
    currency list or by whatever a form posted. Nothing to derive from means no
    simulation: a guess here is a deposit in the wrong currency."""
    code = ((account if isinstance(account, dict) else {}).get("asset") or {}).get("code")
    return (code, "") if isinstance(code, str) and code else (None, UNKNOWN_CURRENCY)


@router.get("/customers/{customer_id}/accounts/{virtual_account_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    customer_id: str,
    virtual_account_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    # The head names the customer this account belongs to; the customers read
    # does not depend on the account read, so it is gathered with it through the
    # console's one resolver — no added latency, one bounded page, and a
    # customer beyond it leaves the head showing the `cus_…` it always showed.
    result, customers = await with_customer_names(
        client, accounts.fetch_account(client, customer_id, virtual_account_id)
    )
    account = result if isinstance(result, dict) else None
    problem = None
    if account is None:
        problem = (
            problem_view(result)
            if isinstance(result, Problem)
            else local_problem(
                "Conduit is unreachable",
                "The account could not be read just now.",
                "Refresh in a moment; nothing has changed on Conduit's side.",
                resource_id=virtual_account_id,
            )
        )
    deposits, deposits_problem = (
        await _deposits(client, customer_id, virtual_account_id) if account else ([], None)
    )
    return render(
        request,
        "accounts/detail.html",
        section="customers",
        customer_id=customer_id,
        customer_name=customers.names.get(customer_id, ""),
        virtual_account_id=virtual_account_id,
        account=account,
        problem=problem,
        cards=accounts.deposit_cards(account),
        balances=accounts.balance_rows(account),
        deposits=deposits,
        deposits_problem=deposits_problem,
        amount_of=accounts.amount_of,
        outcomes=OUTCOMES,
        rails=RAILS,
        can_act=actor.can_any(*MOVE_MONEY),
        can_simulate=actor.can("sandbox.simulate"),
    )


@router.post("/customers/{customer_id}/accounts/{virtual_account_id}/simulate-deposit")
async def simulate_deposit(
    request: Request,
    customer_id: str,
    virtual_account_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("sandbox.simulate")),
) -> Response:
    back = f"/customers/{customer_id}/accounts/{virtual_account_id}"
    if not is_sandbox():
        return redirect(request, back, err="Deposit simulation is sandbox-only.")
    values = dict(await form_items(request))
    amount = (values.get("amount") or "").strip()
    if not amount:
        return redirect(request, back, err="An amount is required.")
    outcome = (values.get("outcome") or "").strip()
    rail = (values.get("rail") or "").strip().upper()
    if outcome and outcome not in OUTCOMES:
        return redirect(request, back, err="Unknown simulated outcome.")
    if rail and rail not in RAILS:
        return redirect(request, back, err="Unknown deposit rail.")
    # The posted `code` is not read. The picker offers the account's own currency
    # and nothing else, but a picker is not a control — so the code on the wire is
    # derived from the account itself, and a stale or hand-rolled POST has nothing
    # left to disagree with.
    code, refusal = simulate_code(
        await accounts.fetch_account(client, customer_id, virtual_account_id)
    )
    if code is None:
        return redirect(request, back, err=refusal)

    body: dict = {"assetAmount": {"code": code, "amount": amount}}
    if outcome:
        body["outcome"] = outcome
    if rail:
        body["rail"] = rail

    result = await client.mutate(
        "POST", SIMULATE_PATH.format(cid=customer_id, vid=virtual_account_id), json=body
    )
    audit.record(
        session,
        action="sandbox.simulate_deposit",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"virtualAccount": virtual_account_id, "ok": isinstance(result, Success), **body},
    )
    await session.commit()
    if isinstance(result, Success):
        return redirect(request, back, msg=f"Simulated a {amount} {code} deposit.")
    return redirect(request, back, err=problem_line(result))
