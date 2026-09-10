"""Same-currency transfers between Conduit accounts (plan v2 §7 Payments).

There is no transfer endpoint. A transfer **is** a payout on Conduit's
**virtual-account destination arm** — `FiatPayoutDto.destination.anyOf[1]` in
`contracts/openapi_production.json`:

    {"type": "virtual_account", "virtualAccountId": "vac_…",
     "remittance": {"reference": "≤140", "description": "≤280"}}

`additionalProperties: false`, `required: [type, virtualAccountId]`. **No rail
and no recipient**: you name the destination account and Conduit routes the
movement. Its rules, from Conduit's own guide and verified live: same currency on
both sides (`PAYOUT_DESTINATION_CURRENCY_MISMATCH` otherwise — a cross-currency
move between Conduit accounts is an *order*, so this screen points at Convert),
same organisation, **any** customer of it, and a destination that is not the
source account. `purpose` is still required by the DTO and is fixed to
`intercompany` (`payments.TRANSFER_PURPOSE`); `documents[]` applies exactly as on
the other arms.

**No discovery call.** `GET /v2/payouts/requirements` takes `rail` as a required
parameter and has no `virtual_account` value, so this arm is outside it. Nothing
on this screen is discovery-shaped: the fields are the three the DTO declares.
The optional-document widget stays — a route that requires none still accepts one
— but no `acceptedDocumentTypes` is claimed, because there is no discovery
response to have supplied one.

**Where this screen starts.** `/transfers/new` — no customer in the
URL — is what the ribbon's "Transfer" links at, and the SOURCE CUSTOMER is the
first control of step 1 rather than a prefix of the address. The customer-scoped
`/customers/{id}/transfers/new` is the same handler on a second path and is
unchanged: every action pill, contacts hand-off, way-back link, `?destination=`
deep link and bookmark still lands on this form with the customer filled in.
With nobody picked the page asserts nothing about anybody — the pickers are
empty and say "pick the customer", which is not the same sentence as "this
customer has no account".

**What the operator picks, and why it re-renders.** Out of: the source customer,
then one of their active accounts, named (`{customer} · {asset} · {vac_…}`). Into: a
destination *customer* first (names-first — `m.customer_datalist`, one bounded
`with_customer_names` read per render, never per row), then one of **their**
active accounts holding the source's currency. The source select re-renders the
whole form on change (`hx-get`, the payout page's `#route-row` idiom) rather than
carrying a copy of the balances and the currency label: a render-time copy went
stale the moment the select moved, and a USD selection showed the EUR account's
balances under an "Amount (EUR)" label. With JavaScript off the same form's
button is a plain GET.

**The destination suggestions are filtered by what this console has SEEN**. Once a
source account fixes the currency, the destination datalist is narrowed to customers
observed to hold an active account in it — one local SELECT over the `virtual_accounts`
projections (`web.accounts.holders`), because `GET /v2/customers` cannot filter by
holdings and a live read per candidate is the banned per-row pattern. The help sentence
says *observed* out loud: an account no webhook or read ever landed for is absent from
the list, not from Conduit. It filters the suggestion and nothing else — a typed id is
still accepted and still resolved live below, so the constraint is what it always was.
The evidence: an unfiltered list suggested a customer whose virtual-account feature is
not enabled (403 `FEATURE_NOT_ENABLED`), which the three-state handled honestly but
which should never have been offered.

**Three empty states, not one.** A destination customer may have no active
account holding the source currency, no active account at all, or a list this
console could not read — and "we could not ask" is never rendered as "there are
none" (`accounts.fetch_accounts` hands the failure back precisely so it cannot
be flattened). The Convert hand-off is offered only where it is true: between one
customer's *own* accounts. A cross-customer, cross-currency move has no console
flow, so that case states what is true and stops.

**Sandbox settles this arm (since 2026-09-01 ~20:00 UTC).** For its first hours
this screen carried a dated on-screen limitation: two probes earlier that day
were accepted (202, with no whitelist entry existing for the destination —
proving the arm needs none) and then failed `rail_unavailable`. Conduit's
2026-08-31 changelog entry ("Pay another Conduit account by its account id")
became true in the sandbox that evening — `tests/e2e/10_va_transfer_probe.py`
exited 2 and the note came off. Evidence of the settled pair, both halves:
`sandbox_evidence/va_transfer_settled.json` (txn_034HRmGsnk0798PRyU7zUE,
completed, fee-free) and `va_transfer_received_deposit.json` (the receiving
customer's deposit — `source.type: internal_transfer`, the originating
transaction id, and the sender's remittance reference carried through). The
probe remains the sentinel in the OTHER direction: exit 3 if the arm's create
behaviour ever changes.

**What left this screen.** The whitelist walkthrough, the registered-destination
picker, the rail / recipientType / destinationCountry controls and the
requirements fetch are gone from *here*, not from the console: an intercompany
payout to a **bank** destination still needs a whitelisted recipient and still
lives on the payout page's `purpose=intercompany` route, with the Contacts and
whitelist pages behind it. This screen links there.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import accounts, documents, forms, operations, payments
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Page, Problem
from app.web import (
    ALREADY_SPENT_ELSEWHERE,
    conduit,
    db,
    form_items,
    intent_of,
    local_problem,
    problem_of,
    problem_view,
    redirect,
    reference_of,
    render,
    with_customer_names,
)
from app.web.accounts import holders  # the observed-holdings query, local

router = APIRouter()

# Two paths, one handler. `/transfers/new` is where the ribbon lands
# and where the form posts its own re-renders; the customer-scoped path is what
# every action pill, hand-off and bookmark in this console already points at,
# and it keeps rendering the same form with the customer filled in. FastAPI
# reads `customer_id` off the path where the path has one and off the query
# string where it does not, which is exactly the difference between the two.
NEW = "/customers/{customer_id}/transfers/new"
GLOBAL = "/transfers/new"


def _problem(result, what: str, resource_id: str = "") -> dict:
    """Conduit's own problem-detail, titled with **which** read failed.

    Two reads can fail on this screen — this customer's accounts and the
    destination customer's — and an unattributed banner over two different empty
    states tells the operator nothing about which one to reload.
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


class Transfer:
    """What the operator picked, from a query string or a form body — parsed
    once so the GET and the POST cannot disagree."""

    def __init__(self, values) -> None:
        get = values.get
        self.account_id = (get("virtualAccountId") or "").strip()
        # `?destination=cus_…` still deep-links this screen; it now preselects
        # the destination *customer* rather than opening a registration
        # walkthrough.
        self.destination = (get("destination") or "").strip()
        self.destination_account_id = (get("destinationVirtualAccountId") or "").strip()
        self.amount = (get("amount") or "").strip()
        self.reference = (get("remittanceReference") or "").strip()
        self.description = (get("remittanceDescription") or "").strip()


def asset_of(account) -> str:
    return str(((account or {}).get("asset") or {}).get("code") or "")


async def _funded(
    client: ConduitClient, customer_id: str
) -> tuple[list[dict], dict | None]:
    """`(the customer's active virtual accounts, the problem card)` — the only
    ones a transfer can be funded from, and why the list is empty when it is.

    An unreadable list rendered as an empty one told the operator this customer
    has no active account, which is a fact about a client's money that nobody
    established (`accounts.active`).
    """
    available, failure = await accounts.active(client, customer_id)
    return available, (
        None
        if failure is None
        else _problem(failure, "This customer's virtual accounts", customer_id)
    )


async def _nobody() -> tuple[list[dict], dict | None]:
    """`_funded`'s answer for "no customer is picked yet": no accounts, and no
    problem card either, because nothing was asked and nothing failed."""
    return [], None


async def _destination_accounts(
    client: ConduitClient, destination: str
) -> tuple[list[dict], dict | None]:
    """`(the destination customer's active accounts, the problem card)`.

    Same shape and same rule as `_funded`. The narrowing to the source currency
    happens in the template, off the whole active list, so that "none in USD" and
    "none at all" stay two different sentences.
    """
    listed = await accounts.fetch_accounts(client, destination)
    if not isinstance(listed, Page):
        return [], _problem(listed, "That customer's accounts", destination)
    return [a for a in listed.items if a.get("status") == "active"], None


async def _render(
    request: Request,
    customer_id: str,
    transfer: Transfer,
    *,
    client: ConduitClient,
    session: AsyncSession,
    errors: forms.FormErrors | None = None,
    document_ids: list[str] | None = None,
    problems: list[dict] | None = None,
    reference: str | None = None,
    can_act: bool = True,
    status_code: int = 200,
) -> Response:
    """One renderer for every state of the screen.

    Both reads happen here, on every render, so the screen an operator gets after
    a 422 is the screen they submitted from — with the balances and the currency
    label belonging to the account that is actually selected. Nothing about this
    page is carried forward from an earlier render.

    `customer_id` may be empty: the ribbon lands here with nobody
    picked. That is a state to render honestly, not a customer to read — no
    accounts call is made for the empty string, and the template says "pick the
    customer" rather than asserting an absence about somebody it never asked
    about.
    """
    problems = [p for p in (problems or []) if p]

    # One bounded, gathered customers read (`with_customer_names`): this page
    # names a customer in its lede, on every source-account option, in the
    # SOURCE picker's suggestions and in the destination picker's. Never one read
    # per row; a miss renders the bare id rather than a guess.
    (available, unreadable_accounts), listed = await with_customer_names(
        client, _funded(client, customer_id) if customer_id else _nobody()
    )
    problems += [p for p in (unreadable_accounts,) if p]

    account = next(
        (a for a in available if a.get("id") == transfer.account_id),
        available[0] if available else None,
    )
    asset = asset_of(account)

    destination_accounts: list[dict] = []
    destination_listed = True
    if transfer.destination:
        destination_accounts, failure = await _destination_accounts(
            client, transfer.destination
        )
        destination_listed = failure is None
        problems += [p for p in (failure,) if p]

    # The two refusals this arm has, decided once here so the option, its stated
    # reason and the server's own check cannot come to different conclusions:
    # a destination in another currency (Conduit answers
    # `PAYOUT_DESTINATION_CURRENCY_MISMATCH`) and a destination that is the
    # source. Offered **disabled with the reason**, never filtered out — the
    # payout page's rule for the same situation: an account that vanished from a
    # picker is an account the operator goes looking for.
    destination_options = [
        {
            "id": str(item.get("id") or ""),
            "asset": asset_of(item),
            "usable": bool(asset)
            and asset_of(item) == asset
            and item.get("id") != (account or {}).get("id"),
            "is_source": item.get("id") == (account or {}).get("id"),
        }
        for item in destination_accounts
    ]

    # The DESTINATION suggestions, narrowed to customers this console has
    # OBSERVED to hold an active account in the source currency. One local
    # SELECT over the virtual-account
    # projections, intersected with the bounded names read above — no second
    # Conduit call, and never one per candidate. It filters the *suggestion*
    # only: a typed id is still accepted and still resolved live against
    # Conduit's own answer in `_destination_accounts`, so the constraint is
    # exactly what it was. With no source account picked there is no currency to
    # filter on, and the list is the unfiltered one it has always been.
    #
    # The evidence for the narrowing: an unfiltered list suggested a customer
    # whose virtual-account feature is not enabled (403 FEATURE_NOT_ENABLED).
    # The three-state handled it honestly; it should not have been offered.
    # `None` from holders means the console has observed nothing at all (fresh
    # deploy, no webhook yet) — the filter has no information and stays out of
    # the way rather than emptying the picker (see holders' docstring).
    observed = await holders(session, asset) if asset else None
    destination_suggestions = (
        [item for item in listed.items if str(item.get("id") or "") in observed]
        if observed is not None
        else listed.items
    )

    # The engine is not shaping this form — the arm has no discovery response —
    # but the document widget is the house one, and it reads its chips and its
    # errors off a render model. An empty model is exactly that and nothing more.
    rm = forms.render_model(
        forms.FormModel(),
        forms.FormValues(document_ids=list(document_ids or [])),
        errors or forms.FormErrors(),
    )

    return render(
        request,
        "transfers/new.html",
        section="orders",  # Transact: where this flow starts
        status_code=status_code,
        customer_id=customer_id,
        transfer=transfer,
        purpose=payments.TRANSFER_PURPOSE,
        accounts=available,
        accounts_listed=unreadable_accounts is None,
        account=account,
        asset=asset,
        balances=accounts.balance_rows(account),
        customer_name=listed.names.get(customer_id, ""),
        customers=listed.items,
        customers_more=listed.more,
        # The destination's own list, and the currency it was narrowed by — the
        # help sentence states the filter rather than leaving a shorter list
        # unexplained.
        destination_customers=destination_suggestions,
        destination_filter=asset if observed is not None else "",
        destination_name=listed.names.get(transfer.destination, ""),
        destination_accounts=destination_options,
        # Four facts, not one empty select: no account at all, none in this
        # currency, none *other than* the source account, or a list that could
        # not be read. Each is a different thing to tell the operator.
        destination_matching=[o for o in destination_options if o["asset"] == asset and asset],
        destination_usable=[o for o in destination_options if o["usable"]],
        # "we could not read them" is not "there are none".
        destination_listed=destination_listed,
        # The Convert hand-off is true only between one customer's OWN accounts:
        # a cross-customer cross-currency move has no console flow, so that case
        # states what is true and stops rather than pointing at a screen that
        # cannot perform it.
        own_accounts=bool(transfer.destination) and transfer.destination == customer_id,
        remittance_limits=dict(payments.REMITTANCE_LIMITS),
        rm=rm,
        document_purpose=payments.DOCUMENT_PURPOSE,
        problems=problems,
        reference=reference or "",
        can_act=can_act,
    )


@router.get(GLOBAL, response_class=HTMLResponse)
@router.get(NEW, response_class=HTMLResponse)
async def new(
    request: Request,
    customer_id: str = "",
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The same handler on both paths. On `/customers/{id}/transfers/new`
    FastAPI binds `customer_id` from the path; on `/transfers/new` there is no
    such path parameter, so the default makes it an ordinary query parameter —
    which is what the form's own source-customer control sends."""
    return await _render(
        request,
        customer_id,
        Transfer(request.query_params),
        client=client,
        session=session,
        can_act=actor.can("transfer.create"),
    )


@router.post(NEW)
async def create(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("transfer.create")),
) -> Response:
    items = await form_items(request)
    submitted = dict(items)
    transfer = Transfer(submitted)
    values = forms.parse_submission(forms.FormModel(), items)  # `documentIds` only
    amount = payments.amount(transfer.amount)

    errors = forms.FormErrors()

    # Every one of these is re-decided here against Conduit's own answer, not
    # against what the browser sent: the selects are a convenience, and a
    # destination account is a place money goes.
    available, _unreadable = await _funded(client, customer_id)
    account = next((a for a in available if a.get("id") == transfer.account_id), None)
    asset = asset_of(account)
    if account is None:
        errors.form.append(forms.Message("Pick an active virtual account to transfer out of."))
    if amount is None:
        errors.form.append(forms.Message(payments.AMOUNT_MESSAGE))
    # `MONEY_CEILING`, on the arm that does not go through `payout_errors`
    #. Here, with the rest of the refusals, so it lands
    # before `operations.start` like every other one of them.
    if refusal := payments.over_ceiling(amount):
        errors.form.append(forms.Message(refusal))

    destination_account = None
    if not transfer.destination_account_id:
        errors.form.append(forms.Message(payments.SAME_ACCOUNT_MESSAGE))
    elif transfer.destination_account_id == transfer.account_id:
        errors.form.append(forms.Message(payments.SAME_ACCOUNT_MESSAGE))
    elif not transfer.destination:
        errors.form.append(forms.Message("Pick the customer being transferred to."))
    else:
        into, failure = await _destination_accounts(client, transfer.destination)
        if failure is not None:
            # The same refusal `payments.resolve_recipient` makes about an
            # unreadable whitelist: the destination was not verified, so nothing
            # is sent — and the problem card above says which read failed.
            errors.form.append(
                forms.Message(
                    "That customer's accounts could not be read, so the destination account "
                    "was not verified. Nothing was sent — reload and try again."
                )
            )
        else:
            destination_account = next(
                (a for a in into if a.get("id") == transfer.destination_account_id), None
            )
            if destination_account is None:
                errors.form.append(
                    forms.Message(
                        "That is not one of this customer's active virtual accounts. Pick a "
                        "destination account from the list."
                    )
                )
            elif asset and (into_asset := asset_of(destination_account)) != asset:
                errors.form.append(
                    forms.Message(
                        payments.TRANSFER_CURRENCY_MESSAGE.format(
                            destination=into_asset or "another currency", asset=asset
                        )
                    )
                )

    # Same rule as the payout form: an attachment must be one this operator
    # uploaded here for this purpose, or nothing is sent (`documents.attachable`).
    # Checked before `operations.start`, and reported on the widget it belongs to.
    if refused := await documents.unattachable(
        session, values.document_ids, purpose=payments.DOCUMENT_PURPOSE, actor_id=actor.id
    ):
        errors.documents.append(
            forms.Message(documents.REFUSED_ATTACHMENT.format(count=len(refused)))
        )

    if not errors.ok:
        return await _render(
            request,
            customer_id,
            transfer,
            client=client,
            session=session,
            errors=errors,
            document_ids=values.document_ids,
            reference=reference_of(submitted),
            status_code=422,
        )

    body = payments.virtual_account_body(
        customer_id=customer_id,
        virtual_account_id=account["id"],
        destination_account_id=destination_account["id"],
        asset=asset,
        amount_text=amount or "",
        reference=transfer.reference,
        description=transfer.description,
        document_ids=values.document_ids,
    )
    op, is_new = await operations.start(
        session,
        # It IS a payout on the wire, so it is a `payout_create` — the
        # reconciler's recipe for one (`GET /v2/transactions?type=withdrawal&
        # clientReferenceId=…`, OPERATIONS_SPEC §3) resolves this arm unchanged.
        type="payout_create",
        actor_id=actor.id,
        actor_email=actor.email,
        path=payments.PAYOUT_PATH,
        body=body,
        customer_id=customer_id,
        intent=intent_of(submitted),
        # Console-local: on the row, not in `body`, so neither Conduit nor
        # `request_hash` ever sees it.
        reference=reference_of(submitted),
    )
    # **A nonce that resolved onto somebody else's request**. This form
    # takes its nonce verbatim out of a hidden field and mints a
    # `payout_create` — the same type batch dispatch mints — and `start`
    # resolves on the nonce alone, comparing nothing but the type. So a submit
    # carrying a spent nonce came back here as `is_new=False` with a real,
    # confirmed operation about a different payment, fell through the two arms
    # below on `op.state == "confirmed"`, and landed the operator on that
    # payment's transaction page: a settled-looking receipt for a transfer that
    # was never made.
    #
    # `operations.resolved_elsewhere` recomputes the request hash over
    # the path and the canonical body and asks whether the resolution is about
    # what was just submitted. No `scope`, because this form has none — for a
    # form an identical body IS the same payment, which is the ordinary
    # double-submit and must stay silent. Placed before `execute_operation` so
    # a real double-submit still cannot send twice.
    #
    # `ALREADY_SPENT_ELSEWHERE` rather than `payouts.ALREADY_SUBMITTED_DIFFERENTLY`:
    # the four sibling routes settled on one shared sentence and one destination —
    # the resolved operation, so "what you are looking at" is literally true —
    # and the payout form's variant names a *payout*, which is not the word the
    # operator pressed here. What both have to make impossible is reading the
    # page they land on as a receipt, and the shared sentence says that first.
    if not is_new and operations.resolved_elsewhere(op, payments.PAYOUT_PATH, body):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SPENT_ELSEWHERE)
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed" and op.conduit_resource_id:
        # 202 is *accepted*, not settled — on sandbox this arm then fails
        # `rail_unavailable`, which the transaction page renders off Conduit's own
        # `failureCode`/`failureMessage` with no new code here.
        return redirect(request, f"/transactions/{op.conduit_resource_id}")
    if op.state == "rejected":
        return await _render(
            request,
            customer_id,
            transfer,
            client=client,
            session=session,
            document_ids=values.document_ids,
            problems=[problem_of(op.error, op.conduit_resource_id)],
            reference=reference_of(submitted),
            status_code=422,
        )
    return redirect(request, f"/operations/{op.id}")
