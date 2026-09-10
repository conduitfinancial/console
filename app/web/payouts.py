"""External fiat payouts, driven end to end by `GET /v2/payouts/requirements`
(plan v2 §7 Payments).

The page is one URL with a widening query string, so every step is linkable and
re-loadable and there is no draft row to keep in step with Conduit:

    GET  /payouts                    → the fork: one payment, or a batch
    /payouts/new?                    → the route row, empty
    …/payouts/new?purpose=…&rail=…&… → discovery's form for that route, below it
    POST                             → validate, then `POST /v2/payouts` via the ledger

Both screens answer on two paths: the bare ones above, which the
ribbon's "Send a payout" links at, and the customer-scoped
`/customers/{id}/payouts[/new]` every action pill, contacts hand-off, way-back
link and bookmark already points at. One handler each — the customer is the
first control of the route row rather than a prefix of the URL. With nobody
picked the route's own requirements still load (they are a fact about the route)
while the funding accounts, the whitelist and the payout form wait, and nothing
is claimed about anybody. The batch arm stays customer-scoped: a batch is
uploaded for one customer by design.

**The purpose is a dropdown, not a gate.** It used to be a screen of its own —
seven buttons, one per purpose, and nothing else on the page until one was
pressed. That made the commonest thing an operator does (send this supplier the
usual payment) a two-page errand, and it made the purpose feel like a decision
about the console rather than a field of the payment. It is now the first control
of the **route row** — purpose, rail, recipient type, destination country,
funding account, on one line — and the requirements form appears underneath the
moment the four route parts are answered, and re-appears reshaped whenever any of
them changes. `?purpose=…` still means what it always meant, so every bookmark
and every link the pills once produced still lands on the right page.

`intercompany` reshapes **in place**, through the same discovery flags every
other purpose obeys: `whitelist.required` comes back true, the coordinate fields
leave the form and the registered-recipient picker takes their place. There is no
bounce to the transfers screen — that screen is the *transfer* flow (a
same-currency A→B move between two of your customers, with its own summary and
its own copy) and it is unchanged; it is not where an intercompany payout has to
be made.

**The route metadata is obeyed, never assumed** (FORM_ENGINE_SPEC §1). Discovery
answers with three gates and this module reads all three off the response:

* `whitelist.required` → the recipient's identity fields are removed from the
  form and replaced by a picker over the customer's **registered** whitelist
  entries. The picked entry's coordinates are written onto the submitted values
  server-side, so what is sent is Conduit's own record of the destination rather
  than anything the browser typed.
* `documentation.required` → the shared upload widget appears with
  `purpose=transaction_support`, `acceptedDocumentTypes` is listed, and a submit
  with nothing attached is refused here rather than spent on a 422. When it is
  **false** the same widget is still on the page, collapsed (`m.attach_documents`,
  a route that does not ask for a document still accepts one,
  and what is attached rides the same `documents[]` either way. Whatever is
  attached must be a document **this operator uploaded here for this purpose** —
  the ids are resolved against the console's own upload ledger before anything is
  sent, and one that cannot be matched refuses the submit (OPERATIONS_SPEC §3's
  attachment rule; the transfer screen shares this body builder and the rule).
* `blockedJurisdictions` → a destination country (or any country answered in the
  form) that is on the list stops the submit.

**The known 422 trap.** `DOCUMENTATION_REQUIRED` means *no payout was created*.
The operation is `rejected`, which releases the §1 double-submit guard, so
attaching the document and resubmitting opens a **new** operation with a new
idempotency key — which is exactly right, and needs no special casing beyond
re-rendering the form with the operator's values still in it.

**Both worlds, one code path** (the contract is in
OPERATIONS_SPEC §5). Conduit has announced that a missing required document will
stop being a synchronous refusal: the transaction will be **accepted** and an RFI
raised against it instead. The pinned spec still describes today's synchronous
`422`, so nothing here assumes the flip has happened — and nothing has to:

* the submit below branches on `op.state`, never on a problem `type`. A refusal
  is a refusal (`rejected` → re-render with Conduit's own problem detail,
  whatever it says); an acceptance is an acceptance (`confirmed` → redirect to
  the transaction). `DOCUMENTATION_REQUIRED` is named in a comment and in this
  docstring, and in no condition;
* an accepted-then-questioned payout therefore lands on the transaction page,
  which reads `hasRfi` and renders the RFI panel — the held state and the
  answer form, on the page about the payment (`web/transactions.py`);
* a transaction-subject RFI is projected and swept whenever it arrives
  (`worker.KIND_BY_PREFIX["rfi"]`), which is not conditional on how the payout
  was submitted either.

So the flip is a change in which of two existing branches Conduit picks. There
are deliberately **no** speculative branches here on response shapes that do not
exist yet: the day a `202` with an RFI id appears, the thing to check is that
this docstring is still true, not to add a case.

**Quotes are indicative, and the claim to hold one is sealed**.
`POST /v2/quotes` prices the route, it does not reserve it: the panel says so,
shows `expiresAt`, and a quote whose expiry has passed disables the confirm
button until it is refreshed — client-side for the countdown, and again
server-side, because a disabled button is a display decision. That server-side
half used to read a plain `quoteExpiresAt` hidden field, which made the browser
the author of the console's own staleness check: rewriting it to a future
timestamp walked straight past the one refusal the panel exists to produce. The
panel now ships the timestamp **and** an HMAC seal over it — the same
`app.auth.tokens` idiom `web/convert.py` uses for its far more consequential
signed selection — and the submit refuses a claim whose seal is missing, altered,
or over a different moment than the one displayed. Carrying neither field claims
no quote, which stays a legal submit: a quote reserves nothing, and holding one
was never a condition of sending.

**Saved counterparties are this console's, and only on free-form routes**
(the evidence is in `app/counterparties.py`'s docstring).
Conduit's only recipient store is `whitelist-recipients` and it is the
`intercompany` gate — so a gated route's "saved counterparties" *are* its
registered entries and the picker above is already that feature, untouched here.
Every other route retypes its destination, and gets two things instead:

* `?counterparty=…` on the GET **prefills** the recipient fields from a saved
  record. Prefill, not lock: the fields stay editable, and what is validated and
  sent is the submission, not the record. The whitelist gate overwrites because
  Conduit will pay nothing else; a counterparty has no such authority.
* "Save as counterparty" on the POST stores the accepted `destination.recipient`
  **on 202 acceptance** — the moment `payout_create` confirms — not on
  settlement. The alternative is a save that happens hours later in a worker, on
  behalf of an operator who has closed the tab, for a destination Conduit has
  already agreed to send money to. Acceptance is the point at which Conduit
  validated these coordinates; settlement says something about the *payment*.
  A payout that later fails leaves a saved counterparty behind, which is correct:
  the address was fine, the transfer was not.

**Which counterparty a payout used is recorded on the operation's audit trail**, not in
a column: `counterparty.used` when a submission named a saved destination,
`counterparty.save` when one was stored, both carrying the `operation_id` and the
**label only**. The transaction page's operation panel reads the newest back
(`counterparties.attached_label`). A label kept at the time survives a later rename or
archive, which a foreign key would not; nothing about `request_body`, `request_hash` or
the wire changes (OPERATIONS_SPEC §1).

The save is **not** an operations-ledger row (see `app/web/__init__.py`): it
mutates nothing at Conduit, so it cannot duplicate a payment or leave an outcome
unknown. It is a plain audited write, and its failure never costs the operator
the payout's redirect.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import (
    accounts,
    audit,
    counterparties,
    documents,
    forms,
    operations,
    payments,
    projections,
)
from app.auth.actor import Actor
from app.auth.tokens import seal, unseal
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Page, Problem, Success
from app.web import (
    ALREADY_SPENT_ELSEWHERE,
    conduit,
    db,
    form_items,
    intent_of,
    local_problem,
    problem_of,
    problem_note,
    problem_view,
    redirect,
    reference_of,
    render,
    with_customer_names,
)
from app.web import onboarding  # `field_errors`
# One answer to "which rail describes this stored contact", shared with the
# contact edit page rather than written twice.
from app.web.contacts import edit_route
from app.web.countries import resolve_country

log = logging.getLogger(__name__)
router = APIRouter()

FORK = "/customers/{customer_id}/payouts"
NEW = FORK + "/new"
# The same two screens without a customer in the URL. The ribbon's
# "Send a payout" lands on GLOBAL_FORK; the source customer is a control inside
# each screen rather than a prefix of it. One handler serves both paths —
# FastAPI binds `customer_id` from the path where the path has one and from the
# query string where it does not — so the customer-scoped URLs every action
# pill, hand-off and bookmark already point at keep rendering the same screens.
GLOBAL_FORK = "/payouts"
GLOBAL_NEW = "/payouts/new"
# The cross-customer contact picker. A literal segment beside
# `/payouts/new`: this router owns no `/payouts/{something}` pattern for it to
# collide with (the customer-scoped screens are `/customers/{id}/payouts…`), so
# there is no ordering to get right and no id that could ever be read as this
# word.
CONTACT = "/payouts/contact"

# The flash for a spent nonce carrying a body that is not the one it was spent
# on — a back-button resubmit after the amount was edited, most plainly. The
# operation it resolves to is real and is shown; what this sentence has to make
# impossible is reading that page as a receipt for the amount just typed.
ALREADY_SUBMITTED_DIFFERENTLY = (
    "This form had already been submitted, and what you are looking at is that submission. "
    "What you just sent is different from it and was NOT sent — no second payout was made. "
    "Start a new payout if you meant to send it."
)


def _problem(result, what: str, resource_id: str = "") -> dict:
    """Conduit's own problem-detail, titled with **which** read failed.

    The `what` used to be dropped whenever Conduit answered a problem at all, so
    this page said "Conduit is unavailable" at the top while the recipient field
    below it asserted that no registration exists. Two reads fail differently
    here — the whitelist and the funding accounts — and the banner has to name
    the one it is about, or an operator cannot connect it to the field.
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


class Route:
    """The four query parameters that identify a payout route, plus the account
    it is funded from. Parsed once, from either a query string or a form body,
    so the GET and the POST can never disagree about what was chosen."""

    def __init__(self, values) -> None:
        get = values.get
        self.purpose = (get("purpose") or "").strip()
        self.rail = (get("rail") or "").strip().lower()
        self.recipient_type = (get("recipientType") or "").strip().lower()
        # `resolve_country`: a full country name typed in the box becomes its
        # alpha-3 code; anything that is not an exact name — a code, a typo, a
        # place this table has never heard of — passes through untouched, so an
        # unsupported destination surfaces as Conduit's own problem-detail
        # rather than as this console pretending to know the world.
        self.country = resolve_country(get("destinationCountry") or "").strip().upper()
        self.virtual_account_id = (get("virtualAccountId") or "").strip()

    @property
    def valid_purpose(self) -> bool:
        return self.purpose in payments.PURPOSE_VALUES

    @property
    def params(self) -> dict[str, str]:
        """The route under the names its own query string uses — what the
        Convert hand-off carries away and hands back (`payments.route_query`).
        The funding account is deliberately not in here: the round trip returns
        with a *different* one, the account the conversion landed in."""
        return {
            "purpose": self.purpose,
            "rail": self.rail,
            "recipientType": self.recipient_type,
            "destinationCountry": self.country,
        }

    @property
    def corridor(self) -> bool:
        """Rail + recipient type + country — everything a route needs except the
        purpose. It is its own property because a **batch's** route is exactly
        this: one file may carry seven purposes, so the purpose is a column
        there rather than part of the route (`app/web/batches.py`)."""
        return (
            self.rail in payments.RAILS
            and self.recipient_type in payments.RECIPIENT_TYPES
            and bool(self.country)
        )

    @property
    def complete(self) -> bool:
        return self.valid_purpose and self.corridor


async def _funding(
    client: ConduitClient, customer_id: str
) -> tuple[list[dict], dict | None]:
    """`(the customer's active virtual accounts, the problem card)` — the only
    accounts a payout can be funded from, and the reason the list is empty when
    it is.

    The `web.contacts._whitelist` shape, for the same reason: an unreadable list
    rendered as an empty one turns "Conduit did not answer" into "this customer
    has no account", which is a fact about a client's money that nobody
    established.
    """
    available, failure = await accounts.active(client, customer_id)
    return available, (
        None
        if failure is None
        else _problem(failure, "This customer's virtual accounts", customer_id)
    )


async def _nobody() -> tuple[list[dict], dict | None]:
    """`_funding`'s answer for "no customer is picked yet": no accounts, and no
    problem card either, because nothing was asked and nothing failed."""
    return [], None


def _asset_of(account: dict | None) -> str:
    return str(((account or {}).get("asset") or {}).get("code") or "")


async def _render(
    request: Request,
    customer_id: str,
    route: Route,
    *,
    client: ConduitClient,
    snapshot: dict | None = None,
    values: forms.FormValues | None = None,
    errors: forms.FormErrors | None = None,
    problems: list[dict] | None = None,
    quote: dict | None = None,
    amount: str = "",
    reference: str | None = None,
    session: AsyncSession | None = None,
    saved: list[dict] | None = None,
    chosen_counterparty: str = "",
    # What the last payment to the chosen contact was, when there was one
    # (`_resend`). It prefills nothing by itself — it biases the funding
    # account's default selection below, and the template says what it did.
    resend: dict | None = None,
    # Carried across a 422 so a validation fix does not silently drop the
    # operator's intent to save the destination they just corrected.
    save_checked: bool = False,
    save_label: str = "",
    can_act: bool = True,
    status_code: int = 200,
) -> Response:
    """One renderer for every state of the page — picker, form, 422 re-render."""
    problems = [p for p in (problems or []) if p]
    model = recipients = None
    rm = None
    if snapshot is not None:
        try:
            model = payments.payout_model(snapshot)
        except forms.SchemaVersionMismatch as mismatch:  # pragma: no cover - Dialect B has none
            problems.append(local_problem("Requirements schema not supported", str(mismatch)))
            status_code = 502
    # No customer, no customer-scoped read: the ribbon lands here with
    # nobody picked, and a whitelist or address-book read for the empty string is
    # a question about nobody. The route's own requirements still load — they are
    # a fact about the ROUTE, not about a customer — so the form still shows what
    # this route needs before anyone is chosen.
    gated = bool(model and model.whitelist.get("required")) and bool(customer_id)
    if model is not None:
        rendered = payments.recipient_model(model) if gated else model
        rm = forms.render_model(rendered, values, errors)
    whitelist_listed, whitelist_capped, off_rail = True, False, 0
    if gated:
        page = await payments.fetch_recipients(client, customer_id)
        whitelist_listed = isinstance(page, Page)
        # Registered *and* payable over the route's rail: offering a sepa IBAN on
        # a fedwire payout is offering a payout Conduit will refuse.
        registered = payments.registered_only(page.items) if whitelist_listed else []
        recipients = [e for e in registered if payments.payable_over(e, route.rail)]
        # How many the rail filter took, so an empty picker can say WHY it is
        # empty. "No registered recipient for this customer" is false when the
        # customer has three on another rail, and the operator who registered one
        # last week is told it is gone — the instruction (register one for this
        # rail) is right, the premise is not, and they cannot tell whether they
        # are adding a rail or creating a counterparty from scratch.
        off_rail = len(registered) - len(recipients)
        if not whitelist_listed:
            problems.append(_problem(page, "The whitelist", customer_id))
        else:
            # Page one, as this picker has always taken (`fetch_recipients`) —
            # but never silently. A registration past it is not offered here, and
            # the sentence under an empty picker must not claim it does not
            # exist. The house rule is that a cap is stated, not that every read
            # is exhaustive; five requests on the hot form path buys nothing an
            # honest sentence does not.
            whitelist_capped = bool(page.next_cursor)

    # The console's own address book, for the routes Conduit gives no store for.
    # A gated route's saved destinations are its registered whitelist entries and
    # the picker above already is them — offering a second, unreviewed list next
    # to it would be offering a payout Conduit refuses.
    #
    # **The customer's whole live book, and no route filter**. It used
    # to be filtered to the answered route's family, recipient type and country,
    # because the picker sat *under* the route and could only offer what that
    # route could pay. The choice now comes first and IMPLIES the route, so a
    # filter would hide every contact whose corridor is not the one already
    # showing — which is precisely the contact the operator came to pick. What
    # the route can carry is still decided by the route: the form is discovery's
    # answer for the implied corridor, and a contact whose corridor disagrees
    # with an already-answered route is said rather than silently offered
    # (`_resend`'s `mismatch`).
    if saved is None:
        saved = (
            []
            if gated or session is None or not customer_id
            else await counterparties.rows(session, customer_id)
        )

    # One bounded, gathered customers read (`with_customer_names`) beside this
    # customer's accounts: it fills the source-customer picker at the head of the
    # route row. Never one read per row, and a failure is silent to the reader —
    # the page renders minus the suggestions.
    (available, unreadable_accounts), listed = await with_customer_names(
        client, _funding(client, customer_id) if customer_id else _nobody()
    )
    problems += [p for p in (unreadable_accounts,) if p]
    # The currency guard, on the picker: an account whose asset this
    # rail cannot carry is shown, disabled, with the reason — never hidden. The
    # operator has to be able to see that the EUR account exists and why it is
    # not an option here, or the console has simply lost an account.
    doomed = {
        str(a.get("id")): payments.doomed_rail(
            route.rail, str((a.get("asset") or {}).get("code") or "")
        )
        for a in available
    }
    # …and the refusal is also a path. A doomed account can be
    # converted into the currency this rail settles in and the payout funded
    # from the account that receives it — two operations, two settlements, which
    # is what the copy under the picker says. Offered ONLY where the conversion
    # can land: a customer holding no active account in the pinned currency has
    # nothing to convert into, and a link would dead-end on Convert's own
    # "nothing to convert into" card. That case gets the fact instead.
    convert_links = {}
    for item in available:
        pinned = doomed.get(str(item.get("id")))
        into = next((a for a in available if _asset_of(a) == pinned), None) if pinned else None
        if into is not None:
            convert_links[str(item["id"])] = f"/customers/{customer_id}/convert?" + (
                payments.route_query(
                    route.params, source=str(item["id"]), destination=str(into["id"])
                )
            )
    # The default selection skips a doomed account: pre-selecting one would walk
    # the operator into a refusal they did not choose. Between the accounts that
    # are left, a resend prefers the one holding the currency that payment was in
    # — and where none does, the ordinary default stands and the
    # template says the account was not prefilled, rather than this page
    # selecting a currency the resend cannot claim.
    live = [a for a in available if not doomed.get(str(a.get("id")))]
    wanted = (resend or {}).get("asset") or ""
    account = (
        next((a for a in available if a.get("id") == route.virtual_account_id), None)
        or next((a for a in live if wanted and _asset_of(a) == wanted), None)
        or next(iter(live), None)
    )
    return render(
        request,
        "payouts/new.html",
        # Under the current IA this flow *starts* on Transact, so the
        # nav stays on Transact for every step of it. One `_render` serves the
        # form, the 422 re-render and the whitelist-refusal render, so the
        # highlight cannot flicker between them.
        section="orders",
        status_code=status_code,
        customer_id=customer_id,
        customer_name=listed.names.get(customer_id, ""),
        customers=listed.items,
        customers_more=listed.more,
        route=route,
        purposes=payments.PURPOSES,
        purpose_help=payments.PURPOSE_HELP,
        rails=payments.RAILS,
        recipient_types=payments.RECIPIENT_TYPES,
        model=model,
        rm=rm,
        # The route-owned controls' own errors: `amount`,
        # `virtualAccountId` and `whitelistRecipientId` are never in `model.fields`
        # (route-owned, or outside discovery entirely), so they carry no
        # `RenderField` of their own for `m.err_attrs` to read — this is the same
        # `errors.fields` dict, keyed the same way, for the template to look up by
        # name directly.
        field_errors=errors.fields if errors else {},
        whitelist_gated=gated,
        recipients=recipients,
        # Three states, not two: registered entries, none, or a read that never
        # answered. Only the middle one makes it safe to register a duplicate.
        whitelist_listed=whitelist_listed,
        whitelist_capped=whitelist_capped,
        off_rail=off_rail,
        # …or the one a deep link named. `?whitelistRecipientId=` is how the
        # Contacts page hands a just-registered destination straight to this
        # form; it used to hand it to the transfers screen, which no longer pays
        # a whitelist entry at all.
        selected_recipient=(values.root.get("whitelistRecipientId") if values else "")
        or request.query_params.get("whitelistRecipientId")
        or "",
        document_purpose=payments.DOCUMENT_PURPOSE,
        accounts=available,
        accounts_listed=unreadable_accounts is None,
        doomed_accounts=doomed,
        convert_links=convert_links,
        rail_message=payments.RAIL_ASSET_MESSAGE,
        account=account,
        asset=_asset_of(account),
        amount=amount,
        reference=reference or "",
        quote=quote,
        # The include's rule, kept here too: a rendered claim always carries its
        # seal. `_render`'s `quote` is `None` on every path today — the panel is
        # swapped in by its own fragment route — and a claim rendered with an
        # empty seal would be refused at submit rather than silently unchecked,
        # which is the right failure but a confusing one to debug.
        seal=_sealed(quote),
        saved=saved,
        chosen_counterparty=chosen_counterparty,
        resend=resend,
        save_checked=save_checked,
        save_label=save_label,
        mask=counterparties.coordinates,
        problems=problems,
        can_act=can_act,
    )


@router.get(GLOBAL_FORK, response_class=HTMLResponse)
@router.get(FORK, response_class=HTMLResponse)
async def fork(
    request: Request,
    customer_id: str = "",
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """One payment, or many — asked **before** any purpose or any requirements.

    It is the first question because it is the only one whose two answers are
    different flows rather than different values: a single payout is a form, a
    batch is a file, and everything after this point (which purpose, which
    columns, which confirmation, which ledger rows) differs. Asking it second —
    which is what a "many payouts at once" tray inside the single-payout form was
    doing — made an operator who came to upload a file first answer a purpose the
    file was going to override.

    One Conduit call, and only the bounded customers read: this is
    where the ribbon lands, so the fork has to be able to ask **who** as well as
    which. The batch arm stays customer-scoped — a batch is uploaded for one
    customer by design — so its link appears once that answer exists and says so
    while it does not.
    """
    _nothing, listed = await with_customer_names(client, _nobody())
    return render(
        request,
        "payouts/fork.html",
        section="orders",
        customer_id=customer_id,
        customer_name=listed.names.get(customer_id, ""),
        customers=listed.items,
        customers_more=listed.more,
        can_act=actor.can("batch.upload"),
    )


@router.get(GLOBAL_NEW, response_class=HTMLResponse)
@router.get(NEW, response_class=HTMLResponse)
async def new(
    request: Request,
    customer_id: str = "",
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    route = Route(request.query_params)
    problems: list[dict] = []

    # `?counterparty=…` prefills the recipient fields from this console's own
    # address book. `get` filters on `customer_id`, so an id belonging to another
    # customer resolves to nothing — the same answer a made-up one gets.
    #
    # Resolved BEFORE discovery, because the contact can now
    # answer part of the route: a `Pay a contact` hand-off arrives carrying the
    # contact alone, and the corridor it was saved for is a local fact about it.
    chosen = (request.query_params.get("counterparty") or "").strip()
    values = resend = None
    picked = await counterparties.get(session, customer_id, chosen) if chosen else None
    if chosen:
        # `recipient` is None for a row whose ciphertext no longer decrypts —
        # the refusal below, never an exception out of the read.
        if picked is not None and picked["recipient"]:
            values = forms.FormValues(
                root={"destination": {"recipient": dict(picked["recipient"])}}
            )
            resend = await _resend(client, session, customer_id, picked, route)
            # The other half of the mismatch rule (gate finding m3): a corridor
            # this route cannot pay puts nothing on the form. `chosen` stays —
            # the contact is still the one picked, and the note says why none of
            # its values are here.
            if resend["mismatch"]:
                values = None
        else:
            problems.append(
                local_problem(
                    "That contact could not be used",
                    "It is archived, unreadable, or not this customer's. The form is "
                    "unchanged — fill the destination in by hand or pick another.",
                    resource_id=chosen,
                )
            )
            chosen = ""

    snapshot = None
    if route.complete:
        result = await payments.fetch_requirements(
            client,
            purpose=route.purpose,
            rail=route.rail,
            recipient_type=route.recipient_type,
            destination_country=route.country,
        )
        if isinstance(result, dict):
            snapshot = result
        else:
            problems.append(_problem(result, "The payout requirements"))

    return await _render(
        request,
        customer_id,
        route,
        client=client,
        session=session,
        snapshot=snapshot,
        values=values,
        problems=problems,
        # The operator's own amount wins over a resend's: this page is reloaded
        # by every route change and by the Prefill button, both of which carry
        # the box's current contents.
        amount=request.query_params.get("amount") or (resend or {}).get("amount") or "",
        resend=resend,
        chosen_counterparty=chosen,
        can_act=actor.can("payout.create"),
    )


# --- pay a contact -----------------------------------------------------------------------
#
# The ask, phrased: "pick a contact and just resend the funds".
# Two halves, and **neither of them pays anybody**: a picker that turns a name
# into a customer-scoped payout URL, and one more thing the payout form's prefill
# knows. The operator lands on the ordinary form and submits it through the
# ordinary POST, with its ceiling, its intent nonce, its seal, its whitelist and
# document gating — there is no second way to send money here, which is why this
# whole section is GETs.

# How many contacts the picker's one local SELECT offers as suggestions. It is a
# datalist, not a list — the input accepts anything typed into it and the id is
# resolved on submit against the whole table (`counterparties.find`), so a
# contact past this bound is *unsuggested*, never unreachable. Stated on the page
# either way, as every bounded list in this console states its bound.
PICKER_CAP = 200

# Which rail actually carried a payment, from Conduit's own settlement reference
# for it. **The read-back cannot otherwise say**: a withdrawal's
# `destination.recipient.rail` is the whitelist FAMILY (`us`/`sepa`/`swift` — the
# enums in the pinned spec's own `PublicWithdrawalViewDto`), and four payout
# rails share `us`. These keys are the evidence that names one, because Conduit
# puts a Fedwire IMAD only on a payment that went over Fedwire; they appear once
# the payment has settled far enough to have one, and their absence prefills no
# rail rather than guessing between four.
RAIL_EVIDENCE = {
    "fedwireImad": "fedwire",
    "achTraceNumber": "ach",
    "rtpTransactionId": "rtp",
    "fedNowMessageId": "fednow",
}


async def _resend(
    client: ConduitClient,
    session: AsyncSession,
    customer_id: str,
    picked: dict,
    route: Route,
) -> dict:
    """What the last payment to this contact was — and the route parts it and the
    contact row can answer, written onto `route` where the URL left them empty.

    **The trail is the record, and the read-back is the truth.** The link is one
    `counterparty.used` audit row (`counterparties.linked_transactions`, newest
    first): that action is written only when what was *submitted* still was the
    saved destination, an edited prefill records `counterparty.modified_prefill`
    instead and is excluded by construction, and the row carries the contact's
    **id** — so a transaction belonging to another contact cannot arrive here.
    The amount and the purpose then come from reading that transaction back from
    Conduit, never from the operation's stored `request_body`, which is purged on
    the §6 retention schedule and is the wrong kind of evidence besides: what
    Conduit created is what was paid.

    Nothing here is a claim the form cannot survive. Every value it produces is
    an ordinary prefill — editable, and validated on submit exactly as a typed
    one is (`payments.payout_errors`, the ceiling included) — and every one it
    cannot produce is left empty and said in a sentence rather than guessed:

    * no `counterparty.used` row and no `counterparty.save` one either, or a read
      that did not land → `read` is False and the fallback sentence is on the
      form;
    * a purpose this build has never heard of → not selected (the row's options
      are the seven Conduit publishes, and an unknown one would silently show as
      unanswered anyway);
    * a rail no settlement reference names and no family determines → left for
      the operator, with the rails that share the family named;
    * a currency this customer holds no active account in → the funding account
      keeps the page's ordinary default and the template says it was not
      prefilled.

    `route` is mutated in place because it is this request's parsed query string
    and the URL is the state of this page: what the operator answered always
    wins, and only the blanks are filled.
    """
    found = await counterparties.linked_transactions(
        session, customer_id=customer_id, contact_id=str(picked["id"]), limit=1
    )
    transaction = found[0] if found else ""
    source = "used" if transaction else ""
    if not transaction:
        # **The payout that SAVED this contact.** `counterparty.save` is
        # written on the 202 of a payout
        # Conduit accepted, so that operation is provably a payment to this
        # destination — the exclusion it sits under is about the *claim*
        # "sent to contact X", which is about picking a saved record and is why
        # `linked_transactions` still refuses it. Reading a transaction back is a
        # different question, and this row answers it: without this fallback the
        # commonest resend of all — the first one after a contact was saved, when
        # no `used` row can exist yet — would prefill nothing.
        #
        # **Bound by ID, never by label** (gate finding M1). `history`'s label arm
        # exists so a *history panel* can show a save that predates the
        # `counterparty` key — a display, where the cost of a near-miss is a row
        # shown to a reader. Here the cost is a different contact's payment
        # prefilling this form: a label is renameable and an archived row does
        # not hold its name, so renaming A into a retired B's label made B's save
        # match A, and this page then prefilled B's amount and told the operator
        # A's coordinates had been edited since. Only `detail->>'counterparty'`
        # proves whose save it was; a pre-Phase-12 save carries no id and
        # correctly falls to the "could not be read back" sentence.
        saved_by = await counterparties.history(session, customer_id, str(picked["id"]))
        transaction = next(
            (
                row["transaction"]
                for row in saved_by
                if row["action"] == counterparties.SAVE_ACTION and row["transaction"]
            ),
            "",
        )
        source = "saved" if transaction else ""
    read = await payments.fetch_transaction(client, transaction) if transaction else None
    txn = read if isinstance(read, dict) else {}

    destination = txn.get("destination")
    destination = destination if isinstance(destination, dict) else {}
    money = destination.get("assetAmount")
    money = money if isinstance(money, dict) else {}
    recipient = destination.get("recipient")
    recipient = recipient if isinstance(recipient, dict) else {}

    purpose = str(txn.get("purpose") or "")
    named = sorted({rail for key, rail in RAIL_EVIDENCE.items() if destination.get(key)})
    family = str(picked["rail_family"] or "")
    shared = payments.RAILS_FOR.get(family, ())

    if not route.purpose and purpose in payments.PURPOSE_VALUES:
        route.purpose = purpose
    # A reference naming a rail outside the family this contact was saved for is
    # two records disagreeing, not evidence: the payout picker would not even
    # offer this contact on that rail (`rows` filters by family). Neither is
    # believed over the other — the rail is asked for instead.
    named = [rail for rail in named if not family or payments.family_of(rail) == family]
    implied = False
    if not route.rail:
        # One settlement reference names one rail. Two would name two, which
        # names none — the same refusal-to-guess `family_of` makes. Failing that
        # the family's FIRST rail, which is `web.contacts.edit_route`'s rule and
        # is imported from it rather than re-derived: the contact edit
        # page has answered this same question — "which rail describes this
        # stored destination" —, and two answers to it would be
        # two different forms for one contact. A guess between four is what it
        # is, so the note says the rail was implied and points at the control.
        route.rail = named[0] if len(named) == 1 else edit_route(picked)["rail"]
        # Implied whenever the record did not name exactly one rail — TWO
        # references name two, which names none, and that case fell through to
        # the family's first rail with the caveat unprinted (gate finding m2).
        implied = bool(route.rail) and len(named) != 1
    if not route.recipient_type:
        route.recipient_type = str(picked["recipient_type"] or "")
    if not route.country:
        route.country = str(picked["destination_country"] or "")

    # "The coordinates changed since that payment" is a claim, so it needs the
    # payment to have stated coordinates: a read-back that carries no recipient
    # block (Conduit omits it on some views) establishes nothing, and comparing
    # against nothing would call every such contact modified.
    proven = any(str(recipient.get(key) or "").strip() for key in counterparties.PROOF_KEYS)

    # Where the answered route contradicts the contact's own corridor. Only
    # reachable when the route was already answered — the fills above leave
    # nothing to contradict — and stated rather than overruled: a route the
    # operator typed is theirs, and a contact list that no longer filters by
    # route has to say when the two disagree instead of quietly pairing them.
    mismatch = [
        what
        for what, (theirs, answered) in (
            ("rail family", (family, payments.family_of(route.rail))),
            ("recipient type", (picked["recipient_type"], route.recipient_type)),
            ("destination country", (picked["destination_country"], route.country)),
        )
        if theirs and answered and str(theirs).upper() != str(answered).upper()
    ]
    return {
        "read": isinstance(read, dict),
        # Which trail row produced it: the payout that PICKED this contact
        # (`used`) or the one that SAVED it. The note says which, because they
        # are different sentences about different payments.
        "source": source,
        "transaction": transaction,
        "label": picked["label"],
        # **A disagreeing corridor prefills NOTHING** (gate finding m3). The
        # paragraph a mismatch prints says nothing of this contact was filled in,
        # and it has to be true: the route is the operator's, while this
        # contact's coordinates and this payment's currency belong to another
        # corridor — and half-filling a form from a record the route cannot pay
        # is exactly the claim this console refuses everywhere else. The caller
        # drops the recipient values on the same condition, so what the operator
        # gets is discovery's own empty form and one paragraph saying why.
        "amount": "" if mismatch else str(money.get("amount") or ""),
        "asset": "" if mismatch else str(money.get("code") or ""),
        "family": family,
        # The rails that share this family — named only when the rail was IMPLIED
        # from it rather than read off the payment, because then the other three
        # are what the operator may have meant.
        "rails": shared if implied and len(shared) > 1 else (),
        "mismatch": mismatch,
        # …and nothing is claimed about coordinates that are not on the form
        # either: "edited since that payment" is a statement about a prefill, and
        # a mismatch is the case where there is none.
        "changed": not mismatch
        and proven
        and not counterparties.same_destination(recipient, picked["recipient"]),
    }


# The refusals the picker makes, in the contacts page's own words — an archived
# contact "leaves every payout picker", and this is one.
ARCHIVED_REFUSAL = (
    "That contact is archived: archiving retires the saved-for-payouts capability, so it has "
    "left every payout picker and this one. There is no way back — save the destination again "
    "from a payout form if you need it."
)
UNKNOWN_CONTACT = (
    "No saved contact of this console has that id. Type a name and pick it from the "
    "suggestions: the box sends the contact behind the name, not the name."
)


@router.get(CONTACT, response_class=HTMLResponse)
async def pay_a_contact(
    request: Request,
    contact: str = "",
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Pick a contact, and land on that customer's ordinary payout form.

    **A read with a read budget of zero.** The suggestions are this console's own
    rows (`counterparties.everyones`, one bounded SELECT) and the customer names
    beside them are what this console has already observed
    (`projections.observed_names`, one more local SELECT, honest-miss to the bare
    id) — no `client` is injected here at all, so the page cannot ask Conduit
    anything and renders when Conduit is down.

    **It creates nothing.** The submit resolves the id and redirects to
    `/customers/{customerId}/payouts/new?counterparty=…` — the prefill
    URL, unchanged and reachable a dozen other ways — where the operator reviews
    a form and sends the payout through the one POST that pays anybody.

    Gated `console.view` like every other read: the *pill* that leads here is
    drawn only for `payout.create` (base.html), which is honesty rather than
    access control — never send an operator to a form whose submit they lack —
    and the access control stays where it has always been, on the POST.
    """
    problems: list[dict] = []
    if contact:
        found = await counterparties.find(session, contact)
        if found is not None and found["archived_at"] is None:
            return redirect(
                request,
                NEW.format(customer_id=found["customer_id"]) + f"?counterparty={found['id']}",
            )
        problems.append(
            local_problem(
                "That contact cannot be paid from here",
                ARCHIVED_REFUSAL if found is not None else UNKNOWN_CONTACT,
                resource_id=contact,
            )
        )

    rows = await counterparties.everyones(session, limit=PICKER_CAP + 1)
    more = len(rows) > PICKER_CAP
    rows = rows[:PICKER_CAP]
    return render(
        request,
        "payouts/contact.html",
        section="orders",
        rows=rows,
        names=await projections.observed_names(session, [row["customer_id"] for row in rows]),
        more=more,
        cap=PICKER_CAP,
        problems=problems,
        can_act=actor.can("payout.create"),
    )


# --- the indicative quote ------------------------------------------------------------------
#
# **The quote's expiry is sealed, exactly as Convert seals its selection.** The
# panel is swapped in on its own and the submit form is elsewhere on the page, so
# the one fact the submit reads out of the quote — `expiresAt` — has to travel
# through the browser in a hidden field. Unsigned, that made the browser the
# author of the console's own staleness check: editing the field to a future
# timestamp walked straight past "that quote has expired", which is the one
# refusal the panel exists to produce. Same HMAC the session and CSRF cookies use
# (`app.auth.tokens`), same idiom as `web/convert.py`'s `_sealed`/`_selection` —
# nothing new to get wrong.
#
# The seal binds authenticity, not identity. Dropping BOTH fields still
# submits, because a payout with no quote is a legal submit here (a quote
# reserves nothing and the payout is priced at send time) — so this can only be a
# claim about a quote, never a requirement to hold one. Binding the seal to the
# route and amount it was priced for is the upgrade path, and it belongs to
# whatever slice makes a quote mandatory.

QUOTE_SEAL = "quoteSeal"
# The replay ceiling on a seal, not the quote's own life: `payments.expired` on
# the sealed timestamp is what actually refuses a lapsed price. Convert's number,
# for the same reason it picked it.
SEAL_TTL = 3600


def _sealed(quote: dict | None) -> str:
    """The quote's expiry, signed for the round trip to the browser and back."""
    if not quote or not quote.get("expires_at"):
        return ""
    return seal({"quoteExpiresAt": quote["expires_at"]}, ttl=SEAL_TTL)


def _unsealed(raw: str) -> str | None:
    """The sealed `expiresAt`, or `None` for anything tampered with, malformed or
    older than `SEAL_TTL`."""
    payload = unseal(raw)
    if payload is None:
        return None
    return str(payload.get("quoteExpiresAt") or "")


@router.post("/customers/{customer_id}/payouts/quote", response_class=HTMLResponse)
async def quote(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """`POST /v2/quotes` → the indicative panel, swapped in on its own.

    A quote creates nothing and reserves nothing (the spec: same asset both sides
    plus a country *prices* a withdrawal), so it is not ledgered — there is no
    resource whose outcome could stay unknown.
    """
    values = dict(await form_items(request))
    route = Route(values)
    amount = payments.amount(values.get("amount") or "")
    # The currency is the funding account's, read from Conduit — never a hidden
    # field. The browser used to send `asset` alongside `virtualAccountId`, so a
    # quote could be priced in a currency the account does not hold and shown
    # next to a payout that would move a different one. The two
    # cannot disagree if only one of them exists.
    account = next(
        (
            a
            for a in (await _funding(client, customer_id))[0]
            if a.get("id") == route.virtual_account_id
        ),
        None,
    )
    asset = _asset_of(account)
    if not amount or not asset or not route.country:
        return render(
            request,
            "payouts/_quote.html",
            quote=None,
            problem=local_problem(
                "Nothing to quote",
                "An amount, an active funding account and a destination country are needed first.",
            ),
        )
    result = await client.mutate(
        "POST",
        payments.QUOTE_PATH,
        json=payments.quote_request(
            source=asset,
            destination=asset,
            destination_country=route.country,
            amount_text=amount,
        ),
    )
    if not isinstance(result, Success):
        return render(
            request,
            "payouts/_quote.html",
            quote=None,
            problem=_problem(result, "The quote"),
        )
    view = payments.quote_view(result.data)
    return render(
        request,
        "payouts/_quote.html",
        quote=view,
        seal=_sealed(view),
        problem=None,
    )


# --- submit ---------------------------------------------------------------------------------


@router.post(NEW)
async def create(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("payout.create")),
) -> Response:
    items = await form_items(request)
    submitted = dict(items)
    route = Route(submitted)
    if not route.complete:
        return redirect(
            request, NEW.format(customer_id=customer_id), err="Pick a purpose and a route first."
        )

    result = await payments.fetch_requirements(
        client,
        purpose=route.purpose,
        rail=route.rail,
        recipient_type=route.recipient_type,
        destination_country=route.country,
    )
    if not isinstance(result, dict):
        # Not sent: the body is judged by the schema Conduit will judge it by, and
        # right now there isn't one.
        return await _render(
            request,
            customer_id,
            route,
            client=client,
            problems=[_problem(result, "The payout requirements")],
            # Discovery did not answer, so there is no form on this render — it
            # falls back to the route picker, and the note and the amount have
            # nowhere to go. They are *told*, not carried: the picker's next
            # step is a GET form, so carrying them would put a free-text
            # operator note into a URL, a query log and a browser history. The
            # template says what was lost (`m.lost_inputs`) instead of dropping
            # it in silence.
            reference=reference_of(submitted),
            amount=submitted.get("amount") or "",
            status_code=502,
        )
    snapshot = result
    model = payments.payout_model(snapshot)
    gated = bool(model.whitelist.get("required"))
    rendered = payments.recipient_model(model) if gated else model

    values = forms.parse_submission(rendered, items)  # `documentIds` included
    amount = payments.amount(submitted.get("amount") or "")
    account_id = route.virtual_account_id
    available, _unreadable = await _funding(client, customer_id)
    account = next((a for a in available if a.get("id") == account_id), None)

    errors = payments.payout_errors(
        rendered,
        values,
        account=account,
        amount=amount,
        country=route.country,
        # The currency guard: this route's rail against the funding
        # account's asset, refused here — before `operations.start`, so a
        # proven-doomed payout never becomes a ledger row.
        rail=route.rail,
    )

    # The whitelist gate: the destination is the registered entry, full stop.
    chosen = (submitted.get("whitelistRecipientId") or "").strip()
    entry = None
    if gated:
        values.root["whitelistRecipientId"] = chosen
        # The problem card for an unreadable whitelist comes from `_render`'s own
        # read, so only the sentence is added here.
        entry, refusal = await payments.resolve_recipient(
            client, customer_id, chosen, route.rail
        )
        if refusal is not None:
            errors.form.append(refusal)
            # Also keyed to the control: `#whitelistRecipientId`
            # is hand-templated, outside the discovery model `errors.fields`
            # otherwise comes from, so the field-level binding needs its own entry.
            errors.add("whitelistRecipientId", refusal.detail)
        else:
            payments.apply_recipient(model, values, entry)

    # A quote is indicative, so it gates nothing about correctness — but a
    # confirmation made against a price that has already lapsed is exactly the
    # click the panel exists to prevent, and a disabled button is only a display
    # decision.
    #
    # The timestamp is read out of the SEAL, never off the form. The submit used
    # to trust a plain `quoteExpiresAt` hidden field, so the check it performed
    # was against a value the browser could rewrite — a staleness guard whose
    # input its subject controls is a display decision wearing a server's
    # clothes.
    #
    # `quoteExpiresAt` stays as the CLAIM and the seal is its proof, rather than
    # the seal being the only field. Collapsing the two would make "no seal" and
    # "no quote" the same submission, and this console would then skip its own
    # staleness check in silence the first time a template rendered the panel
    # without the seal. A claim with no proof — or with proof of a different
    # timestamp than the one the operator was shown — is refused instead.
    # Carrying neither claims no quote, which is a legal submit here and always
    # has been: a quote reserves nothing, and the payout is priced when it lands.
    claimed = (submitted.get("quoteExpiresAt") or "").strip()
    sealed = _unsealed((submitted.get(QUOTE_SEAL) or "").strip())
    if claimed and sealed != claimed:
        errors.form.append(
            forms.Message(
                "The quote on this form could not be verified — it was altered, or it is too "
                "old. Refresh the quote and send again; nothing was sent."
            )
        )
    elif claimed and payments.expired(sealed):
        errors.form.append(
            forms.Message("That quote has expired — refresh it before sending the payout.")
        )

    # Attachments ride into `documents[]` on the payout Conduit creates, so a
    # `doc_` id on this form is checked exactly like one on an RFI response:
    # uploaded here, for this purpose, by this operator (`documents.attachable`).
    # It lands in `errors.documents` rather than as its own branch — the widget
    # it belongs to is what should be carrying the complaint, and `not
    # errors.ok` already re-renders with everything the operator typed intact,
    # before `operations.start` and therefore before anything reaches Conduit.
    if refused := await documents.unattachable(
        session, values.document_ids, purpose=payments.DOCUMENT_PURPOSE, actor_id=actor.id
    ):
        errors.documents.append(
            forms.Message(documents.REFUSED_ATTACHMENT.format(count=len(refused)))
        )

    save_checked = counterparties.wants_save(submitted)
    save_label = (submitted.get("counterparty_label") or "").strip()

    if not errors.ok:
        return await _render(
            request,
            customer_id,
            route,
            client=client,
            session=session,
            snapshot=snapshot,
            values=values,
            errors=errors,
            amount=submitted.get("amount") or "",
            reference=reference_of(submitted),
            chosen_counterparty=(submitted.get("counterparty") or "").strip(),
            save_checked=save_checked,
            save_label=save_label,
            status_code=422,
        )

    body = payments.payout_body(
        model,
        values,
        customer_id=customer_id,
        virtual_account_id=account["id"],
        asset=_asset_of(account),
        amount_text=amount or "",
        purpose=route.purpose,
        document_ids=values.document_ids,
    )

    # **Which contact this payment was addressed from is decided BEFORE the
    # operation exists**. Both lookups below are ordinary database reads, and
    # both used to sit between `operations.start` — which has already committed
    # a `created` row — and `execute_operation`. A transient failure there 500'd
    # the request with the operation stranded `created`: the operator's retry
    # resolved by intent to that dead row, `is_new` was False, execution was
    # skipped, and the payment quietly never happened until the TTL abandoned it
    # (OPERATIONS_SPEC §2). Money-safe, and a lost payment. Here, a failure
    # lands before anything is started, so the retry is clean — and what
    # survives the move is only the `audit.record` call, which needs `op.id` and
    # cannot fail.
    attribution = await _attribution(
        session, customer_id, submitted, values, gated=gated, entry=entry
    )

    try:
        op, is_new = await operations.start(
            session,
            type="payout_create",
            actor_id=actor.id,
            actor_email=actor.email,
            path=payments.PAYOUT_PATH,
            body=body,
            customer_id=customer_id,
            intent=intent_of(submitted),
            # Console-local, and stored on the row rather than in `body` — so it
            # never reaches Conduit and never touches `request_hash`.
            reference=reference_of(submitted),
        )
    except operations.IntentTypeMismatch as wrong_form:
        # A nonce this form did not mint. Refused, not worked around: the nonce
        # already answers for another operation, and honouring it here would put
        # two operations of two kinds behind one render.
        return await _render(
            request,
            customer_id,
            route,
            client=client,
            session=session,
            snapshot=snapshot,
            values=values,
            problems=[
                local_problem(
                    "This form's submission token belongs to something else",
                    f"The token sent with this payout was minted for a {wrong_form.found} "
                    "operation, so nothing was sent.",
                    resolution="Reload this page and submit it again.",
                )
            ],
            amount=submitted.get("amount") or "",
            reference=reference_of(submitted),
            chosen_counterparty=(submitted.get("counterparty") or "").strip(),
            save_checked=save_checked,
            save_label=save_label,
            status_code=422,
        )

    # **A consumed nonce carrying a *different* body.** Guard 1
    # answers it with the operation this render already reached, which is right —
    # one render is one payment — but silently landing on the first submission
    # reads as "sent" for an amount nobody sent. Said out loud instead; every
    # other resolution keeps its own destination.
    #
    # The comparison itself is `operations.resolved_elsewhere`, shared with the
    # four other routes that need it. This route keeps its own sentence —
    # it is the only one of the five that can name money, and "no second payout
    # was made" is the fact an operator staring at a confirmed payout most needs.
    if not is_new and operations.resolved_elsewhere(op, payments.PAYOUT_PATH, body):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SUBMITTED_DIFFERENTLY)
    # Recorded against the operation before it is sent — so it is on the record
    # whatever the payout then does, a rejection included. Label only, never
    # coordinates (`counterparties.attached_label`). Only on a *new* operation: a
    # double-submit resolving to an existing one has not used anything twice.
    #
    # No commit of its own: the row rides `execute_operation`'s `created →
    # in_flight` transition, which commits (OPERATIONS_SPEC §2). A commit here
    # was a second failure point inside the window the operation is unsent in.
    if is_new and attribution is not None:
        audit.record(
            session,
            actor_id=actor.id,
            actor_email=actor.email,
            operation_id=op.id,
            **attribution,
        )

    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed" and op.conduit_resource_id:
        # `is_new` gates the save as well as the audit above. Without it, any
        # resubmission that resolves to this already terminal operation — a
        # mechanically re-POSTed form, a back-button replay, the intent nonce
        # doing its job — re-ran the save with the *old* body, overwriting a
        # counterparty that may have been legitimately re-saved under the same
        # label since, with stale coordinates. A replay confirms nothing new; it
        # must therefore store nothing new.
        note = (
            ""
            if gated or not save_checked or not is_new
            else await _save_counterparty(
                session, values, route, customer_id, actor, save_label, op
            )
        )
        return redirect(request, f"/transactions/{op.conduit_resource_id}", err=note)
    if op.state == "rejected":
        # `DOCUMENTATION_REQUIRED` lands here and means *nothing was created*.
        # The operation is terminal, so the guard is released: attaching a
        # document and resubmitting opens a new operation with a new key.
        return await _render(
            request,
            customer_id,
            route,
            client=client,
            session=session,
            snapshot=snapshot,
            values=values,
            errors=forms.map_validation_errors(rendered, onboarding.field_errors(op.error)),
            problems=[problem_of(op.error, op.conduit_resource_id)],
            amount=submitted.get("amount") or "",
            reference=reference_of(submitted),
            chosen_counterparty=(submitted.get("counterparty") or "").strip(),
            save_checked=save_checked,
            save_label=save_label,
            status_code=422,
        )
    return redirect(request, f"/operations/{op.id}")


async def _attribution(
    session: AsyncSession,
    customer_id: str,
    submitted: dict,
    values: forms.FormValues,
    *,
    gated: bool,
    entry: dict | None,
) -> dict | None:
    """`{"action", "detail"}` for the contact this payout was addressed from, or
    `None` when it was addressed from none.

    Both arms are **reads**, and they run before `operations.start` so that a
    failure in either is a failed request rather than a stranded operation
    — see the call site. What they decide is unchanged.

    *Free-form route.* The hidden id says which record was *offered*; the body
    says where the money is going, and only the body is evidence. A prefill is
    editable by design, so "this payout used counterparty X" is true only when
    what was submitted still *is* X. A forged id, or an edited coordinate,
    records the weaker fact under its own action — which
    `counterparties.USE_ACTIONS` deliberately does not read back, so the
    operation panel can never name a saved destination this payment did not go
    to.

    *Whitelisted route*. The
    picked entry's id never reaches `payout_body`, `request_hash` or the wire
    (`FiatPayoutDto` has no such field), and `request_body` — which holds the
    coordinates — is purged on the §6 retention schedule. So without this row
    the trail cannot name the destination, and matching one at query time would
    mean a Conduit read per ledger row. The match is `counterparties.merge`
    itself — the Contacts page's own join, against Conduit's registered record
    rather than against anything typed — so the filter and the badge cannot come
    to different conclusions about one pair of records. No saved twin means no
    row.
    """
    if gated:
        if entry is None:
            return None
        twin = next(
            (
                row["saved"]
                for row in counterparties.merge(
                    await counterparties.rows(session, customer_id), [entry]
                )
                if row["saved"] is not None and row["entry"] is not None
            ),
            None,
        )
        if twin is None:
            return None
        return {
            "action": counterparties.USED_ACTION,
            "detail": {
                "counterparty": str(twin["id"]),
                "label": twin["label"],
                # Which of the two evidences this row rests on: the free-form
                # branch proves it from what was submitted, this one from the
                # registered entry Conduit itself supplied the coordinates of.
                "via": "whitelist",
            },
        }
    used = (submitted.get("counterparty") or "").strip()
    if not used:
        return None
    picked = await counterparties.get(session, customer_id, used)
    if picked is None:
        return None
    matched = counterparties.same_destination(
        counterparties.recipient_of(values.root), picked["recipient"]
    )
    return {
        "action": counterparties.USED_ACTION if matched else "counterparty.modified_prefill",
        "detail": {"counterparty": str(picked["id"]), "label": picked["label"]},
    }


async def _save_counterparty(
    session: AsyncSession,
    values: forms.FormValues,
    route: Route,
    customer_id: str,
    actor: Actor,
    label: str,
    op,
) -> str:
    """Store the destination this payout was accepted with. `""`, or the sentence
    to flash on the transaction page when it could not be stored.

    **The payout has already been sent when this runs**, so nothing here may cost
    the operator their redirect. A save that fails is a bookkeeping loss and is
    reported as one; the money moved either way, and an exception at this point
    would hand the operator a 500 for a payout Conduit accepted.
    """
    recipient = counterparties.recipient_of(values.root)
    name = counterparties.label_for({"counterparty_label": label}, recipient)
    if not recipient or not name:
        return (
            "The payout was sent. The contact was not saved: it had no name and no "
            "legal name to borrow one from."
        )
    family = payments.family_of(route.rail)
    if not family:
        # A rail with no family cannot be matched back to a route later, so the
        # row would be saved and never offered — worse than not saving it.
        return (
            f"The payout was sent. The contact was not saved: this console does not "
            f"know which destinations a {route.rail} payout can reach."
        )
    try:
        # The row this save is about to overwrite, read under `FOR UPDATE` so the
        # upsert below cannot land against a different one (the lock and the one
        # case it cannot cover are argued in `live_by_label`).
        #
        # **Saving under a name you already use updates that contact** — the
        # form's own help text says so, and DESIGN.md argues the semantics — so
        # this is not a check and nothing here refuses. What was missing was the
        # *record*: the upsert touches only `updated_at`, so the contact keeps
        # its id, its name and the `created_by` the Contacts page prints, and an
        # operator holding `payout.create` and not `contact.edit` could point an
        # existing name at a new account leaving nothing behind that said so.
        # The operation body holds the old coordinates, and §6 retention purges
        # it — after which this row is the only thread back to them.
        before = await counterparties.live_by_label(session, customer_id, name)
        saved_id = await counterparties.save(
            session,
            customer_id=customer_id,
            label=name,
            recipient=recipient,
            rail_family=family,
            recipient_type=route.recipient_type,
            destination_country=route.country,
            actor_id=actor.id,
            actor_email=actor.email,
        )
        # `counterparty.edited`'s shape, deliberately (`app/web/contacts.py`):
        # masked before→after for the identity keys, changed leaf *names* for
        # the rest, no coordinate in full anywhere. An operator reading the trail
        # should not have to learn a second diff format because the edit arrived
        # through the payout form instead of the Contacts page.
        changed = counterparties.diff(before["recipient"], recipient) if before else {}
        fields = counterparties.changed_keys(before["recipient"], recipient) if before else []
        audit.record(
            session,
            action="counterparty.save",
            actor_id=actor.id,
            actor_email=actor.email,
            # Linked to the payout that produced it: this row is also what the
            # transaction's operation panel reads back to name the counterparty.
            operation_id=op.id,
            detail={
                "customer": customer_id,
                "label": name,
                "rail_family": family,
                # The id `save` just returned, under the key `counterparties.
                # attached` reads: without it a
                # payout that *saved* its destination named the contact and
                # could not link to it, while one that merely used a saved
                # contact could — the same panel, two behaviours, for no reason
                # an operator could see.
                "counterparty": str(saved_id),
                # The contact this save took over. It is the same id as above on
                # an upsert — that is not redundancy, it is the *claim*: present
                # means "a live contact was replaced", absent means "a new one
                # was created", and no reader has to infer which from the shape
                # of the rest of the row. A create says nothing about a before,
                # because there was none.
                **({"replaced": str(before["id"])} if before else {}),
                # The upsert writes the submitted casing over the stored one, so
                # a takeover can quietly re-spell the name too.
                **(
                    {"renamed_from": before["label"]}
                    if before and before["label"] != name
                    else {}
                ),
                **({"identity": changed} if changed else {}),
                **({"fields": fields} if fields else {}),
            },
        )
        await session.commit()
    except SQLAlchemyError:
        log.exception("counterparty save failed for %s", customer_id)
        await session.rollback()
        return "The payout was sent. The contact could not be saved — try again from the form."
    return ""


@router.post("/transactions/{transaction_id}/cancel")
async def cancel(
    request: Request,
    transaction_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("payout.cancel")),
) -> Response:
    """`POST /v2/payouts/{id}/cancel` through the ledger. A payout IS a
    transaction, so the id is the same one the detail page is at."""
    back = f"/transactions/{transaction_id}"
    path = f"{payments.PAYOUT_PATH}/{transaction_id}/cancel"
    op, is_new = await operations.start(
        session,
        type="payout_cancel",
        actor_id=actor.id,
        actor_email=actor.email,
        path=path,
        body=None,
        intent=intent_of(dict(await form_items(request))),
    )
    # **A spent nonce replayed at a different payout**. The nonce alone
    # resolves, and it is scoped to the operation *type*, so a cancel token spent
    # on payout A answered this cancel of payout B — and the "Payout cancelled."
    # below then said so about a payout that is still pending and will settle.
    # The resource is in the path, and the path is inside `request_hash`, so one
    # comparison is the whole check.
    if not is_new and operations.resolved_elsewhere(op, path):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SPENT_ELSEWHERE)
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed":
        return redirect(request, back, msg="Payout cancelled.")
    if op.state == "rejected":
        return redirect(
            request, back, err=problem_note(op.error, "Too late to cancel this payout.")
        )
    return redirect(request, f"/operations/{op.id}")
