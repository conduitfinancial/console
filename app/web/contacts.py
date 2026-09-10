"""Contacts — one surface for the two recipient stores.

Replaces the two pages that used to sit beside each other per customer: *Saved
counterparties* (this console's own address book, `app/counterparties.py`) and
*Whitelist recipients* (Conduit's registered destinations, read live). They were
never two kinds of thing to an operator — they were one contact wearing one or
two **capabilities**:

* **Saved for payouts** — a console record. Prefills a free-form payout's
  destination; Conduit is never told it exists.
* **Registered for intercompany payouts** — a Conduit registration. The only
  thing an `intercompany` payout to an external **bank** account may be
  addressed to. Named "Whitelisted for transfers" until transfers moved
  onto the `virtual_account` arm, which names the destination account directly
  and gates on nothing: the chip had been describing a capability transfers no
  longer use.

**One entity can hold both, and the page says so on one row — but only where
the coordinates prove it.** The join is `counterparties.merge`, which asks
`same_destination` — the *transfer gate's own* identity test, not a second one
written for this page. A near-miss (a digit, a different legal name, a different
rail) stays two rows, because two rows is the honest answer when nothing has
established that they are one destination.

**A pending registration is not a capability.** A contact bridged to the
whitelist wears "Whitelisting" plus Conduit's own status pill until the review
lands; the "Registered for intercompany payouts" badge appears on `registered`
and on nothing else. The stores stay visually distinct until Conduit says otherwise —
the console does not promote its own request into a fact.

**Routes.** `/customers/{id}/contacts` is canonical;
`/customers/{id}/counterparties` 301s to it so existing bookmarks and links keep
working. `/customers/{id}/recipients` keeps its route because it is the
*registration form* — three query modes and a POST target — and now renders only
that; its table lives here.

**`/contacts` is the cross-customer view**, and it is honest about the half it
cannot show: Conduit has no global whitelist endpoint (the only list is nested
under one customer, exactly as for virtual accounts), so the rows are this
console's own saved contacts — one local SELECT — and the whitelist capability
is resolved only when the customer filter names one customer, which is one read
for one list rather than one per row.

No mutation here is an operations-ledger row: renaming, editing, archiving or
deleting a console-local record changes nothing at Conduit, so all of them are
plain audited writes (the exception is documented in `app/web/__init__.py`).
Revoking and registering are Conduit's, and stay ledgered in
`app/web/recipients.py`.

**Removal has two tiers, and they are different promises.** *Archive* is the
everyday one, an operator's: the contact leaves every picker and this list, and
its coordinates stay on file behind the payouts that named it. *Delete* is
admin-only and destroys those coordinates (`counterparties.purge`) — but keeps
the record: the id, the label, the actor and the whole trail survive, so the
ledger's sent-to-contact filter can still say where the money went and the
history panel still reads. A row that has been through it renders as "deleted —
coordinates purged" wherever an archived row can be seen, and appears nowhere
active rows do.

**Editing is discovery's form, and it states its consequence before it saves.**
See the section below for both halves: why the fields come from
`GET /v2/payouts/requirements` rather than from the stored keys, and why an edit
that moves an identity key under a whitelist registration is a two-step confirm.
"""

from __future__ import annotations

import asyncio
import dataclasses

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app import audit, counterparties, forms, payments
from app.web import customers, recipients
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem
from app.models import AuditEvent, Operation
from app.web import (
    NAME_LIMIT,
    conduit,
    customer_names,
    db,
    form_items,
    local_problem,
    offset_of,
    offset_pager,
    page_size,
    problem_view,
    redirect,
    render,
)

router = APIRouter()

INDEX = "/customers/{customer_id}/contacts"
OLD_INDEX = "/customers/{customer_id}/counterparties"

# The capability filter's vocabulary, as the cross-customer page validates it.
# `saved` is answerable from the local table alone; the other two need a
# customer, because that is the only scope Conduit will list a whitelist for.
CAPABILITIES = ("saved", "whitelisted", "both")
NEEDS_CUSTOMER = ("whitelisted", "both")

# How much of one customer's address book a scoped view assembles before paging
# it. Every row costs a Fernet decrypt, so this is a real ceiling and it is
# stated on the page when it is reached — never a short list rendered as a
# complete one. Two orders of magnitude above any address book this console has
# seen; the CSV export's own cap is the way out above it.
SCOPED_CAP = 500


def _problem(result, what: str, resource_id: str = "") -> dict:
    return (
        problem_view(result)
        if isinstance(result, Problem)
        else local_problem(
            "Conduit is unreachable", f"{what} could not be read.", resource_id=resource_id
        )
    )


# The whitelist walk's ceiling. Conduit serves 100 entries a page, so this is
# five requests for a customer with more registrations than any this console has
# seen — and it is a *stated* ceiling: `_whitelist` reports hitting it, and every
# caller says so on screen rather than rendering a short list as a complete one.
WHITELIST_PAGES = 5


async def _whitelist(
    client: ConduitClient, customer_id: str
) -> tuple[list[dict], dict | None, bool]:
    """This customer's registered destinations, the problem card if the list
    could not be read, and whether the walk stopped at its cap.

    An unreadable whitelist is never rendered as an empty one — "there are none"
    and "we could not ask" are different facts, and only one of them means it is
    safe to register a duplicate.

    **The cursor is walked**. `fetch_recipients`
    answers one page, and this used to take it as the whole list: a customer with
    more registrations than that page had the rest missing from Contacts *and*
    from the capability join, so a saved contact whose registration sat on page 2
    read as "not whitelisted" — the console stating a fact about Conduit's review
    that was really a fact about pagination. Bounded exactly like the export's
    walk (`exports._walk`): a cap, and the truncation surfaced rather than
    swallowed. A mid-walk failure is a failure of the whole read for the same
    reason the first page's is — a half-list cannot answer "is this destination
    registered".
    """
    entries: list[dict] = []
    cursor: str | None = None
    for _ in range(WHITELIST_PAGES):
        found = await payments.fetch_recipients(client, customer_id, cursor=cursor)
        if not isinstance(found, Page):
            return [], _problem(found, "The whitelist", customer_id), False
        entries += found.items
        cursor = found.next_cursor
        if not cursor:
            return entries, None, False
    return entries, None, True


async def _shortcut_targets(session: AsyncSession, items: list[dict]) -> dict[str, str]:
    """`{whitelist entry id: target customer id}` for the entries this console
    registered through the cross-customer shortcut.

    **Derived from the ledger, not from a new table and not from a crawl.** A
    whitelist entry is Conduit's resource and carries no customer reference on
    the far side, so the only honest sources are (a) this console's own record
    of having created it, or (b) matching its coordinates against every
    customer's virtual accounts — which is a request per customer per render, on
    a page an operator opens to check one registration. The operation that
    created the entry already stores the entry's id (`conduit_resource_id`), so
    one join to its audit row answers for the whole page in a single query.

    An entry registered by hand, by another console, or through Conduit directly
    resolves to nothing and is rendered exactly as it is today: the console names
    a target only where it can prove one.
    """
    ids = [
        str(i["id"])
        for i in items
        if i.get("id") and i.get("relationship") == "group_entity"
    ]
    if not ids:
        return {}
    rows = await session.execute(
        select(Operation.conduit_resource_id, AuditEvent.detail)
        .join(AuditEvent, AuditEvent.operation_id == Operation.id)
        .where(
            Operation.type == "whitelist_create",
            Operation.conduit_resource_id.in_(ids),
            AuditEvent.action == recipients.SHORTCUT_ACTION,
        )
    )
    return {
        entry_id: str((detail or {}).get("target_customer") or "")
        for entry_id, detail in rows
        if (detail or {}).get("target_customer")
    }


# --- one customer's contacts --------------------------------------------------------


@router.get(OLD_INDEX, response_class=HTMLResponse)
async def moved(request: Request, customer_id: str) -> Response:
    """The original path, permanently. A 301 rather than a re-render: the page has
    one address now, and a bookmark that keeps resolving to the old one would be
    a second name for the same list forever."""
    query = request.url.query
    return RedirectResponse(
        INDEX.format(customer_id=customer_id) + (f"?{query}" if query else ""), status_code=301
    )


@router.get(INDEX, response_class=HTMLResponse)
async def index(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Saved records and registered destinations, merged by coordinates.

    `?registered=` is the entry a registration just created, whose status this
    render explains — the status comes from the list Conduit answered *this*
    request with, never from what the submit assumed.
    """
    saved = await counterparties.rows(session, customer_id)
    entries, problem, capped = await _whitelist(client, customer_id)

    registered = (request.query_params.get("registered") or "").strip()
    just = {}
    if registered:
        entry = next((i for i in entries if str(i.get("id")) == registered), None)
        just = {"id": registered, "status": str((entry or {}).get("status") or "")}

    return render(
        request,
        "contacts/list.html",
        # Customer-scoped, so the ribbon stays on Customers (DESIGN.md, the
        # detail-page rule) rather than on the Contacts browse entry.
        section="customers",
        customer_id=customer_id,
        rows=counterparties.merge(saved, entries),
        targets=await _shortcut_targets(session, entries),
        listed=problem is None,
        capped=capped,
        whitelist_pages=WHITELIST_PAGES,
        problems=[p for p in (problem,) if p],
        just_registered=just,
        # One flag per action, because this list carries five of them: a role
        # that may rename a contact is not thereby a role that may revoke its
        # registration. `can_act` gates only the column they share.
        can_act=actor.can_any(
            "contact.edit", "contact.archive", "whitelist.revoke", "sandbox.simulate"
        ),
        can_edit=actor.can("contact.edit"),
        can_archive=actor.can("contact.archive"),
        can_revoke=actor.can("whitelist.revoke"),
        can_register=actor.can("whitelist.register"),
        can_simulate=actor.can("sandbox.simulate"),
    )


@router.post(INDEX + "/{cp_id}/archive")
async def archive(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("contact.archive")),
) -> Response:
    back = INDEX.format(customer_id=customer_id)
    done = await counterparties.archive(session, customer_id, cp_id)
    if not done:
        return redirect(request, back, err="No such contact for this customer.")
    audit.record(
        session,
        # The stored action value is history and is never re-labelled — see
        # `app/counterparties.py`. Only what an operator reads changed.
        action="counterparty.archive",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"customer": customer_id, "counterparty": cp_id},
    )
    await session.commit()
    return redirect(
        request, back, msg="Contact archived — it is no longer offered for payouts."
    )


@router.post(INDEX + "/{cp_id}/rename")
async def rename(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("contact.edit")),
) -> Response:
    back = INDEX.format(customer_id=customer_id)
    label = (dict(await form_items(request)).get("label") or "").strip()
    refusal = await counterparties.rename(session, customer_id, cp_id, label)
    if refusal:
        return redirect(request, back, err=refusal)
    audit.record(
        session,
        action="counterparty.rename",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"customer": customer_id, "counterparty": cp_id, "label": label},
    )
    await session.commit()
    return redirect(request, back, msg="Contact renamed.")


# --- editing one contact -------------------------------------------------------------
#
# **The edit form is discovery's, not this module's.** A contact is the
# `destination.recipient` subtree of a payout Conduit accepted, so the honest way
# to re-type one is the form that produced it: `GET /v2/payouts/requirements` for
# this contact's own route, its recipient fields only, through the same
# `forms.parse_submission` / `forms.validate` / `forms.render_model` objects the
# payout screen uses. That buys the ABA and IBAN validators, the required-field
# pass, the enum widgets and the 422 mapper — and, more importantly, it means the
# console can only store coordinates it would have been willing to send.
#
# The alternative — a text box per stored key, validated by nothing — was
# considered and rejected: it is *more* code (a bespoke widget loop instead of
# `m.field`), and it turns the address book into a place where an invalid ABA can
# be saved and only discovered later, on a payout form, by somebody else.
#
# The cost is stated on the page rather than hidden: when discovery cannot be
# read there is no form at all (the payout screen's own behaviour), and a route's
# requirements can have moved since the contact was saved — in which case the
# form asks for what Conduit asks for *now*, which is the only useful question.

# The purpose the edit form asks about. A contact stores a rail family, a
# recipient type and a country — three quarters of a route; the missing quarter
# is the purpose, and it is not a property of the destination (the same supplier
# is paid under `payroll` one month and `payment_for_goods_or_services` the
# next). Every route that can save a contact is a **free-form** one — a
# whitelist-gated route stores nothing here, by construction — so the form asks
# about the commonest of them and the page says which. The recipient subtree is
# the rail's shape, not the purpose's; where a purpose ever declares a field this
# one does not, the merge below keeps the stored value rather than dropping it.
EDIT_PURPOSE = "payment_for_goods_or_services"


def edit_route(contact: dict) -> dict:
    """The `fetch_requirements` arguments for one stored contact.

    The rail is the **first** of its family (`payments.RAILS_FOR`) — fedwire for
    `us`, and the family's only member for `sepa` and `swift`. A family this
    build has never seen yields no rail, and the page refuses rather than
    guessing, exactly as `payments.family_of` refuses in the other direction.
    """
    rails = payments.RAILS_FOR.get(str(contact.get("rail_family") or "")) or ()
    return {
        "purpose": EDIT_PURPOSE,
        "rail": rails[0] if rails else "",
        "recipient_type": str(contact.get("recipient_type") or ""),
        "destination_country": str(contact.get("destination_country") or ""),
    }


def contact_model(model: forms.FormModel) -> forms.FormModel:
    """Discovery's `destination.recipient` subtree, and nothing else.

    The mirror of `payments.recipient_model`, which keeps everything *except* the
    coordinates: a payout is a payment to a destination, and a contact is only
    ever the destination half. Amount, funding account, remittance and the
    document widget are properties of a payment and have no business on a page
    about an address book entry.
    """
    return dataclasses.replace(
        model,
        fields=[
            f
            for f in model.fields
            if f.path[: len(counterparties.RECIPIENT_PATH)] == counterparties.RECIPIENT_PATH
        ],
    )


def owned_keys(model: forms.FormModel) -> set[str]:
    """The top-level recipient keys this form is responsible for."""
    depth = len(counterparties.RECIPIENT_PATH)
    return {f.path[depth] for f in model.fields if len(f.path) > depth}


def merged_recipient(stored: dict | None, model: forms.FormModel, values) -> dict:
    """The stored payload with the form's own keys replaced by what was typed.

    Two rules, and the second one is why this is a merge rather than an
    assignment:

    * a key the form **owns** is whatever the form says, *including nothing* —
      clearing an optional field has to be able to remove it, so the owned keys
      are dropped from the stored payload first and only the submitted ones come
      back (`forms.assemble` omits empties, the same rule that keeps `""` out of
      a payout body);
    * a key the form does **not** own is kept untouched. Discovery describes one
      route on one day; a payload saved under a route that declared a field this
      one does not must not lose it silently, and the page says how many such
      keys there are.

    Granularity is the top-level key, so a whole `bankAddress` either belongs to
    the form or does not — which it always does when discovery declares any of
    its leaves. A leaf *inside* a declared subtree that discovery does not
    declare is the one thing this drops, and it is the same thing a payout would
    have dropped when the contact was saved.
    """
    owned = owned_keys(model)
    typed = counterparties.recipient_of(forms.assemble(model, values))
    return {k: v for k, v in (stored or {}).items() if k not in owned} | typed


async def _capability(
    session: AsyncSession, client: ConduitClient, customer_id: str, cp_id
) -> tuple[dict | None, bool]:
    """`(the registered whitelist twin, whether the whitelist could be read)`.

    Asked through `counterparties.merge` — the Contacts page's own join — and not
    through a second matching rule written for this screen. That is the same
    decision `merge` itself records: a near-miss is not a match, an *ambiguous*
    row (two saved records sharing one destination) claims no twin at all, and a
    registration under review is not a capability. If this function disagreed
    with the list about which contact is whitelisted, the consequence sentence
    below would be a warning about a capability the list never showed.

    An unreadable whitelist answers `(None, False)`, which the caller must not
    read as "no twin" — see `_drops_whitelist`.
    """
    saved = await counterparties.rows(session, customer_id)
    entries, problem, _capped = await _whitelist(client, customer_id)
    row = next(
        (
            r
            for r in counterparties.merge(saved, entries)
            if r["saved"] and str(r["saved"]["id"]) == str(cp_id)
        ),
        None,
    )
    entry = (row or {}).get("entry") or {}
    return (entry if entry.get("status") == payments.USABLE_STATUS else None), problem is None


def _drops_whitelist(changed: dict, twin: dict | None, listed: bool) -> bool:
    """Whether saving this edit needs the operator to answer for the whitelist.

    True when identity keys changed **and** the console cannot rule out a
    registration pointing at the old ones — which is either a twin it can see, or
    a whitelist it could not read. The unreadable case deliberately gates too
    (the three-state rule the badges follow): the *absence* of a twin from a
    failed read is not evidence that there is none, and a silent save on that
    absence would drop a capability without ever mentioning it.
    """
    return bool(changed) and (twin is not None or not listed)


# The sentence itself, in one place because the form states it before the edit
# and the confirm step states it again over the diff.
WHITELIST_CONSEQUENCE = (
    "Saving these changes will drop “Registered for intercompany payouts” — the registration "
    "still points at the old coordinates; re-register to restore it."
)
WHITELIST_UNKNOWN = (
    "Conduit's whitelist could not be read, so this console cannot say whether a registration "
    "points at the old coordinates. If one does, saving drops “Registered for intercompany "
    "payouts” and "
    "re-registering is the way back."
)


async def _edit_render(
    request: Request,
    customer_id: str,
    contact: dict,
    *,
    session: AsyncSession,
    client: ConduitClient,
    can_delete: bool,
    values: forms.FormValues | None = None,
    errors: forms.FormErrors | None = None,
    problems: list[dict] | None = None,
    label: str | None = None,
    confirm: dict | None = None,
    known: tuple | None = None,
    clone: bool = False,
    rail: str = "",
    status_code: int = 200,
) -> Response:
    """One renderer for every state of the edit page — the form, a 422, the
    whitelist confirm step and the delete confirm step.

    `known` is `(requirements snapshot, (twin, listed))` for the caller that has
    already read both — the POST, which had to, to judge the submission. Passed
    rather than re-read so a consequence step is not two more Conduit round trips
    for the two answers the handler is holding. The GET passes nothing and this
    reads them itself, gathered.
    """
    # A clone may leave the family (`?rail=`): the whole `RAILS_FOR` set is
    # offered, so a `us` contact can be re-saved as the `sepa` destination the
    # same payee is also reachable at. Everything else about the route is the
    # source's, because a clone is the same payee.
    route = edit_route(contact) | ({"rail": rail} if clone and rail else {})
    problems = [p for p in (problems or []) if p]
    if known is not None:
        snapshot, (twin, listed) = known
    elif route["rail"]:
        snapshot, (twin, listed) = await asyncio.gather(
            payments.fetch_requirements(client, **route),
            _capability(session, client, customer_id, contact["id"]),
        )
    else:
        snapshot, twin, listed = None, None, True
        problems.append(
            local_problem(
                "This contact's route is not one this console knows",
                f"It was saved for the {contact['rail_family']!r} rail family, which this build "
                "cannot describe — so there is no form to edit it with.",
                resource_id=str(contact["id"]),
            )
        )

    model = rm = None
    if isinstance(snapshot, dict):
        model = contact_model(payments.payout_model(snapshot))
        if values is None:
            values = forms.FormValues(
                root={"destination": {"recipient": dict(contact["recipient"] or {})}}
            )
        # A clone prefills from the source and is then a form like any other:
        # what it stores is what this route's fields hold, which is the same
        # rule the payout save follows ("only coordinates it would send"). The
        # source's own wider payload is NOT carried across — `kept` below names
        # what is being left behind rather than copying it silently.
        rm = forms.render_model(model, values, errors)
    elif snapshot is not None:
        problems.append(_problem(snapshot, "The route's field requirements"))

    return render(
        request,
        "contacts/edit.html",
        # Customer-scoped, like the Contacts list it is reached from.
        section="customers",
        status_code=status_code,
        customer_id=customer_id,
        contact=contact,
        route=route,
        purpose_label=payments.purpose_label(EDIT_PURPOSE),
        rm=rm,
        model=model,
        # The stored keys this route does not describe, named so an operator can
        # see that the form is not the whole record. Values are never printed
        # here — the point is what the form does not touch, not what it holds.
        kept=sorted(set(contact["recipient"] or {}) - owned_keys(model)) if model else [],
        # A clone needs a NEW name and starts with none: prefilling the source's
        # would make the commonest mistake (save over the original) the default
        # keystroke, and `counterparties.save` upserts on the label.
        label=("" if clone else contact["label"]) if label is None else label,
        clone=clone,
        rails=payments.RAILS if clone else (),
        twin=twin,
        listed=listed,
        consequence=WHITELIST_CONSEQUENCE,
        unknown_whitelist=WHITELIST_UNKNOWN,
        confirm=confirm or {},
        can_delete=can_delete,
        problems=problems,
    )


@router.get(INDEX + "/{cp_id}/edit", response_class=HTMLResponse)
async def edit(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("contact.edit")),
) -> Response:
    """The stored destination, in discovery's own fields, editable.

    **Live contacts only** (`get` without `include_archived`): archiving retires
    a contact, and editing one would put a retired destination's coordinates back
    into circulation without putting the contact back in the pickers. A deleted
    contact is archived too, so this refuses one for the same reason without
    having to know what deletion is.
    """
    contact = await counterparties.get(session, customer_id, cp_id)
    if contact is None:
        return redirect(
            request,
            INDEX.format(customer_id=customer_id),
            err=counterparties.NOT_EDITABLE,
        )
    # **Cloning is a mode of this page, not a page of its own**. It is
    # the same permission (`contact.edit`), the same form, the same validation
    # and the same handler with one branch — and a second route would have been a
    # row in the permission matrix for an action the matrix already covers. The
    # mode is a query parameter, so it is linkable and re-loadable exactly like
    # every other step in this console, and `?rail=` widens it: a clone may leave
    # the family, which reshapes the form through this same render.
    clone = bool(request.query_params.get("clone"))
    return await _edit_render(
        request,
        customer_id,
        contact,
        session=session,
        client=client,
        can_delete=actor.can("contact.delete"),
        clone=clone,
        rail=_rail(request.query_params) if clone else "",
        # A row whose ciphertext will not decrypt has nothing to prefill and
        # nothing to merge into: the form would present empty boxes and saving
        # them would overwrite coordinates nobody could read but which are still
        # what the payments went to.
        problems=[
            local_problem(
                "This contact's stored coordinates cannot be read",
                # A clone of an unreadable row is not a repair and must not be
                # described as one: it writes a NEW record and leaves the
                # unreadable one exactly as it is.
                (
                    "Its ciphertext no longer decrypts, so there is nothing to copy — the form "
                    "below starts empty. What you type becomes a NEW contact; the unreadable "
                    "row is not touched, repaired or replaced by saving it."
                    if clone
                    else "Its ciphertext no longer decrypts, so the form below starts empty "
                    "rather than from the record. Saving it REPLACES a destination this "
                    "console cannot show you — which is the only way to repair this row, and "
                    "is not a correction of anything you can see. Cancelling loses nothing "
                    "that was not already lost: the unreadable coordinates stay exactly as "
                    "they are, and the payments already made to them are unaffected either way."
                ),
                "Retype the destination in full, or delete the contact and save it again from "
                "a payout form.",
                resource_id=str(contact["id"]),
            )
        ]
        if contact["recipient"] is None
        else None,
    )


def _rail(values) -> str:
    """The clone's rail, or `""` for anything that is not one of Conduit's.

    A whitelist, not a bound — the `page_size` rule: an unusable value falls back
    to the default (the source's family's first rail, `edit_route`) rather than
    being sent to discovery to be refused there.
    """
    rail = (values.get("rail") or "").strip().lower()
    return rail if rail in payments.RAILS else ""


# The refusal that keeps `counterparties.merge` able to answer. Two saved rows
# with one destination are AMBIGUOUS there by construction: neither may claim the
# whitelist registration (sort order used to decide it silently), and every
# surface that attributes a payment to a contact inherits that. A clone is the
# one write that can produce the pair on purpose, so it is the one write that
# refuses to.
CLONE_MERGES = (
    "A clone has to be a different destination. These coordinates are already saved for this "
    "customer as {label!r} — two contacts at one destination cannot be told apart by anything "
    "this console matches on, so neither would be able to claim a whitelist registration or a "
    "payment. Change the rail or the account details, or rename {label!r} instead."
)


async def _save_clone(
    request: Request,
    customer_id: str,
    contact: dict,
    submitted: dict,
    items,
    *,
    session: AsyncSession,
    client: ConduitClient,
    actor: Actor,
) -> Response:
    """"Save as a new contact" — the same form, a new row, the source untouched.

    The ask was for "the ability to clone a contact: same contact, but change
    the rail and account details", and the shape of it is deliberate:

    * **no new route and no new permission.** It is `contact.edit` on the edit
      URL with `clone=1` in the body, so the role matrix does not move for an
      action it already covers;
    * **the same write path as every other contact this console holds**
      (`counterparties.save`, after `forms.validate` against discovery's own
      fields for the chosen rail) — so a clone is not a second kind of contact,
      and what is stored is what a payout could send;
    * **what the form owns, and nothing else.** The source's payload may be wider
      than this route describes; those keys are *named* on the page rather than
      copied, because a clone that silently carried a `us` account number into a
      `sepa` record would be storing coordinates its own route would never send.

    Two refusals, both before anything is written: a label another live contact
    wears (`save` upserts on `(customer, lower(label))`, so this is data loss, not
    a duplicate row), and a destination another live contact already holds — see
    `CLONE_MERGES`.
    """
    back = INDEX.format(customer_id=customer_id)
    rail = _rail(submitted)
    route = edit_route(contact) | ({"rail": rail} if rail else {})
    snapshot = (
        await payments.fetch_requirements(client, **route) if route["rail"] else None
    )
    if not isinstance(snapshot, dict):
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            clone=True,
            rail=rail,
            label=(submitted.get("label") or "").strip(),
            status_code=502,
        )

    model = contact_model(payments.payout_model(snapshot))
    values = forms.parse_submission(model, items)
    errors = forms.validate(model, values)
    label = (submitted.get("label") or "").strip()
    recipient = counterparties.recipient_of(forms.assemble(model, values))

    # One local read, and it answers both refusals.
    book = await counterparties.rows(session, customer_id)
    if not label:
        errors.form.append(forms.Message(counterparties.NO_NAME))
    elif any(row["label"].lower() == label.lower() for row in book):
        errors.form.append(forms.Message(counterparties.taken_message(label)))
    twin = next(
        (
            row
            for row in book
            if counterparties.same_destination(recipient, row["recipient"])
        ),
        None,
    )
    if twin is not None:
        errors.form.append(forms.Message(CLONE_MERGES.format(label=twin["label"])))

    if not errors.ok:
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            values=values,
            errors=errors,
            label=label,
            clone=True,
            rail=rail,
            status_code=422,
        )

    # **A plain INSERT, never `save`'s upsert** (gate finding m1). `save` updates
    # the live row already wearing the label, which is right for "save this
    # destination again" and catastrophic for a clone: the label check above is a
    # READ, and a row committed between it and the write would have been
    # overwritten — a different contact's coordinates replaced, and this clone's
    # audit row landing on it. The refusal now comes from the partial unique
    # index itself, which no race can get past.
    saved_id = await counterparties.insert(
        session,
        customer_id=customer_id,
        label=label,
        recipient=recipient,
        rail_family=payments.family_of(route["rail"]),
        recipient_type=contact["recipient_type"],
        destination_country=contact["destination_country"],
        actor_id=actor.id,
        actor_email=actor.email,
    )
    if saved_id is None:
        errors.form.append(forms.Message(counterparties.taken_message(label)))
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            values=values,
            errors=errors,
            label=label,
            clone=True,
            rail=rail,
            status_code=422,
        )

    audit.record(
        session,
        action="counterparty.cloned",
        actor_id=actor.id,
        actor_email=actor.email,
        # On the NEW row, naming the source: this is the first thing that ever
        # happened to the clone, and where it came from is the one fact its own
        # trail cannot otherwise carry. Labels and ids only — the coordinates are
        # never in an audit detail (`counterparties.diff`'s rule).
        detail={
            "customer": customer_id,
            "counterparty": str(saved_id),
            "label": label,
            "cloned_from": str(contact["id"]),
            "source_label": contact["label"],
            "rail_family": payments.family_of(route["rail"]),
        },
    )
    await session.commit()
    return redirect(
        request,
        back,
        msg=f"Cloned {contact['label']!r} as {label!r}. "
        "Conduit has never accepted this destination — the first payout to it is what proves "
        "the coordinates.",
    )


@router.post(INDEX + "/{cp_id}/edit")
async def save_edit(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("contact.edit")),
) -> Response:
    """Validate, state the consequence, then store.

    The order matters and is the whole of this handler: nothing is written until
    discovery's own validation passes **and** — where identity keys moved under a
    registration — the operator has answered for the capability they are about to
    lose. Both refusals re-render the form with everything they typed still in
    it.
    """
    back = INDEX.format(customer_id=customer_id)
    items = await form_items(request)
    submitted = dict(items)
    contact = await counterparties.get(session, customer_id, cp_id)
    if contact is None:
        return redirect(request, back, err=counterparties.NOT_EDITABLE)

    # "Save as a new contact" — one branch, not one route (see `_save_clone`).
    # Taken before anything below runs: a clone writes a NEW row and touches the
    # source not at all, so none of the edit path's machinery — the merge into
    # the stored payload, the whitelist consequence, the confirm step — applies
    # to it.
    if submitted.get("clone"):
        return await _save_clone(
            request,
            customer_id,
            contact,
            submitted,
            items,
            session=session,
            client=client,
            actor=actor,
        )

    route = edit_route(contact)
    snapshot = (
        await payments.fetch_requirements(client, **route) if route["rail"] else None
    )
    if not isinstance(snapshot, dict):
        # Not stored: the payload is judged by the schema a payout would be
        # judged by, and right now there isn't one. `_edit_render` renders the
        # failure itself, from its own read.
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            label=(submitted.get("label") or "").strip(),
            status_code=502,
        )

    model = contact_model(payments.payout_model(snapshot))
    values = forms.parse_submission(model, items)
    errors = forms.validate(model, values)
    label = (submitted.get("label") or "").strip()
    recipient = merged_recipient(contact["recipient"], model, values)
    changed = counterparties.diff(contact["recipient"], recipient)

    # Read once, and handed to every render below rather than re-read by each:
    # the twin decides whether this save has a consequence, and the snapshot is
    # the form itself.
    capability = twin, listed = await _capability(session, client, customer_id, contact["id"])
    known = (snapshot, capability)
    if not errors.ok:
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            values=values,
            errors=errors,
            label=label,
            known=known,
            status_code=422,
        )

    # The two-step confirm, the same shape as every risky button in this console:
    # the consequence is stated over the *actual* diff, and the second click
    # carries the nonce of the render that stated it. Not a security boundary —
    # CSRF is that, and this operator is allowed to save — but a body replayed
    # from the first screen carries no `confirm` at all and therefore cannot
    # skip the sentence.
    if _drops_whitelist(changed, twin, listed) and not _confirmed(submitted):
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            values=values,
            label=label,
            confirm={"kind": "save", "changed": changed},
            known=known,
        )

    refusal = await counterparties.update(
        session, customer_id, cp_id, label=label, recipient=recipient
    )
    if refusal:
        errors.form.append(forms.Message(refusal))
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=actor.can("contact.delete"),
            values=values,
            errors=errors,
            label=label,
            known=known,
            status_code=422,
        )

    fields = counterparties.changed_keys(contact["recipient"], recipient)
    dropped = twin if _drops_whitelist(changed, twin, listed) else None
    audit.record(
        session,
        action="counterparty.edited",
        actor_id=actor.id,
        actor_email=actor.email,
        # **Masked diffs for the identity keys, names for the rest, and no
        # coordinate in full anywhere** (`counterparties.diff` /
        # `.changed_keys`). A label is not PII and is recorded in the clear —
        # it is what the operator called this record, and a history panel that
        # could not say "renamed from X to Y" would be useless.
        detail={
            "customer": customer_id,
            "counterparty": str(contact["id"]),
            "label": label,
            **({"renamed_from": contact["label"]} if label != contact["label"] else {}),
            **({"identity": changed} if changed else {}),
            **({"fields": fields} if fields else {}),
            # Recorded as a fact about this edit, because the capability it names
            # is a live join: re-derived later it would answer about today's
            # coordinates, which are exactly the ones this row says changed.
            **({"dropped_whitelist": str(dropped["id"])} if dropped else {}),
        },
    )
    await session.commit()
    return redirect(
        request,
        back,
        msg="Contact updated."
        + (
            " Its whitelist registration still points at the old coordinates — re-register to "
            "restore transfers."
            if dropped
            else ""
        ),
    )


def _confirmed(submitted: dict) -> bool:
    """Whether this submission came from a render that stated the consequence.

    The confirm field echoes that render's own one-use nonce, so the first
    screen's body — which carries no `confirm` — can never pass, and a confirm
    from a *stale* consequence screen saves the values that screen showed.
    """
    token = (submitted.get("confirm") or "").strip()
    return bool(token) and token == (submitted.get("intent") or "").strip()


# --- deleting a contact ---------------------------------------------------------------


@router.post(INDEX + "/{cp_id}/delete")
async def delete(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("contact.delete")),
) -> Response:
    """The second tier: destroy the coordinates, keep the record.

    **Admin-gated**, unlike archiving — the everyday removal is an operator's and
    is reversible in the only sense that matters (the coordinates are still
    there); this one is not reversible at all. Two-step, over the same nonce echo
    the whitelist consequence uses.

    What survives is deliberate and is not negotiable for the console's honesty:
    the row keeps its id, its label and its trail, so the payouts that named this
    contact still resolve to a name (`app/web/transactions.py`) and its history
    panel still reads. Deleting the row itself would leave the ledger unable to
    say where money went.
    """
    back = INDEX.format(customer_id=customer_id)
    submitted = dict(await form_items(request))
    contact = await counterparties.get(session, customer_id, cp_id)
    if contact is None:
        return redirect(request, back, err=counterparties.NOT_EDITABLE)
    if not _confirmed(submitted):
        return await _edit_render(
            request,
            customer_id,
            contact,
            session=session,
            client=client,
            can_delete=True,
            confirm={"kind": "delete"},
        )
    if not await counterparties.purge(session, customer_id, cp_id):
        return redirect(request, back, err=counterparties.NOT_EDITABLE)
    audit.record(
        session,
        action="counterparty.deleted",
        actor_id=actor.id,
        actor_email=actor.email,
        # The label and the route, which is what the shell keeps rendering with.
        # No coordinates — there are none left, and a masked one here would be
        # the console keeping a trace of the thing it was told to destroy.
        detail={
            "customer": customer_id,
            "counterparty": str(contact["id"]),
            "label": contact["label"],
            "rail_family": contact["rail_family"],
        },
    )
    await session.commit()
    return redirect(
        request,
        back,
        msg=f"Deleted “{contact['label']}” — its coordinates are purged. The record stays "
        "behind the payments that named it.",
    )


# --- one contact's history ------------------------------------------------------------

# What this console's own audit actions mean, in operator language. An action
# this build has never heard of is rendered **raw** rather than dropped — the
# unknown-status rule (plan v2 §7) applied to a trail: a row nobody wrote a
# sentence for is still something that happened to this contact.
HISTORY_LABELS = {
    "counterparty.save": "Saved from a payout",
    "counterparty.used": "Used on a payout",
    "counterparty.modified_prefill": "Prefill edited before sending",
    "counterparty.rename": "Renamed",
    "counterparty.edited": "Edited",
    "counterparty.archive": "Archived",
    "counterparty.deleted": "Deleted — coordinates purged",
    "counterparty.bridged": "Put forward for whitelisting",
    "counterparty.cloned": "Cloned from another contact",
}

# How much of one contact's trail the drawer shows. `counterparties.history`'s
# own default, named here because the panel now states it.
HISTORY_LIMIT = 50


@router.get(INDEX + "/{cp_id}/history", response_class=HTMLResponse)
async def contact_history(
    request: Request,
    customer_id: str,
    cp_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The drawer body: one contact's whole trail (`counterparties.history`).

    A read, so a viewer sees it. Archived and deleted contacts resolve here on
    purpose — history is exactly what does not go away when a contact does — and
    the panel says which of the two it is looking at.

    Always 200, including the not-found case: this is the quick-view drawer's
    contract (`app/web/applications.py`), where a non-2xx is dropped by htmx
    without a swap and the operator sees a click that did nothing.
    """
    contact = await counterparties.get(session, customer_id, cp_id, include_archived=True)
    # `counterparties.history` has always capped at 50 and the drawer has never
    # said so: a contact with a long trail showed its 50 newest
    # entries as if they were all of them. A drawer is not a place for a pager —
    # it is a panel you glance at — so the honest form here is the *stated* cap
    # the sweep's other axis allows, with the fuller answer named. One extra row
    # is how it knows.
    trail = (
        await counterparties.history(
            session,
            customer_id,
            cp_id,
            label=(contact or {}).get("label") or "",
            since=(contact or {}).get("created_at"),
            limit=HISTORY_LIMIT + 1,
        )
        if contact
        else []
    )
    return render(
        request,
        "contacts/_history.html",
        customer_id=customer_id,
        contact=contact,
        rows=trail[:HISTORY_LIMIT],
        capped_trail=len(trail) > HISTORY_LIMIT,
        history_limit=HISTORY_LIMIT,
        labels=HISTORY_LABELS,
    )


# --- every customer's contacts ------------------------------------------------------


async def resolve_customers(client, typed: str) -> tuple[list[str], int, bool, dict | None]:
    """A typed customer filter → the ids it means. `(ids, matched, capped, problem)`.

    Name **or** id. A `cus_…` value is itself; anything else is a name,
    resolved through the customers directory's own bounded walk
    (`web.customers.walk_named`) — the same search, the same cap, the same
    honesty sentence, rather than a second name-matching rule that could disagree
    with the one on the directory page.

    `matched` is how many customers the name found, because "3 customers match
    'acme'" is the difference between a filtered list and a wrong one; `capped`
    rides along so the page can say the search stopped.

    **Three states, not two**. A walk that *failed* used to
    flatten into "no ids", which every caller then rendered as "no customer
    matches that name" — an outage reported as an absence, and the one shape of
    wrong answer this console is written not to give. The problem card comes back
    so the page can say the directory is unavailable and the export can refuse
    rather than hand over a file shaped by a search that never ran.
    """
    if not typed:
        return [], 0, False, None
    if typed.startswith("cus_"):
        return [typed], 1, False, None
    found, capped, problem = await customers.walk_named(client, typed, {})
    if problem is not None:
        return [], 0, False, problem
    return [str(c.get("id")) for c in found if c.get("id")], len(found), capped, None


def list_filters(query) -> dict:
    """This page's three filters, validated as the page validates them — one
    parser for the page and for its CSV export (`app/web/exports.py`), the local
    surfaces' equivalent of the Conduit lists' `list_query`.

    A value outside a vocabulary is dropped rather than sent to the database as a
    filter that can only match nothing: a typo in a URL shows the unfiltered
    page, not an empty one (the rule `/accounts` and `/rfis` follow).
    """
    family = (query.get("family") or "").strip().lower()
    capability = (query.get("capability") or "").strip().lower()
    return {
        # A name or an id — `resolve_customers` decides which, on the page and in
        # the export alike.
        "customerId": (query.get("customerId") or "").strip(),
        # Console-side, over the label and the decrypted legal name.
        "contactName": (query.get("contactName") or "").strip(),
        "family": family if family in payments.RAILS_FOR else "",
        "capability": capability if capability in CAPABILITIES else "",
    }


def name_matches(row: dict, needle: str) -> bool:
    """Whether one saved row answers to a typed contact name.

    Label **and** legal name, case-insensitively: the label is what the operator
    called it and the legal name is what the bank calls it, and either is a
    reasonable thing to type. The legal name lives inside the encrypted blob, so
    this runs after the decrypt the page already pays for — never as SQL, which
    could only ever see the label.
    """
    recipient = row.get("recipient") or {}
    hay = f"{row.get('label') or ''} {recipient.get('legalName') or ''}"
    return needle.lower() in hay.lower()


def _capable(row: dict, capability: str) -> bool:
    """Whether one merged row has the capability that was filtered for.
    `whitelisted` means Conduit's answer is `registered` — a review in progress
    is not a capability (see the module docstring)."""
    whitelisted = (row.get("entry") or {}).get("status") == payments.USABLE_STATUS
    if capability == "saved":
        return row.get("saved") is not None
    if capability == "whitelisted":
        return whitelisted
    return row.get("saved") is not None and whitelisted


@router.get("/contacts", response_class=HTMLResponse)
async def everyone(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Contacts across every customer.

    The rows are local (`counterparties.everyones`) and cost no Conduit call. Two
    reads can happen on top of that, and both are bounded and stated on the page:
    the console's one 25-customer name resolver, and — only when the customer
    filter names a single customer — that customer's whitelist, which is the only
    scope Conduit will list one for.
    """
    query = request.query_params
    filters = list_filters(query)
    typed, capability = filters["customerId"], filters["capability"]
    needle = filters["contactName"]
    size, offset = page_size(query), offset_of(query)

    # A typed customer is a name or an id. One resolved id is the
    # scoped view exactly as before — whitelist join and all; several is a
    # narrowed cross-customer view, because Conduit lists a whitelist for one
    # customer and "these three" is not one customer.
    ids, matched, name_capped, directory_problem = await resolve_customers(client, typed)
    customer_id = ids[0] if len(ids) == 1 else ""
    scoped = bool(customer_id)
    # An outage is not an absence: when the directory could not be read, the page
    # says so (the problem card below) and does **not** claim the name matched
    # nothing.
    unknown_customer = bool(typed and not ids and directory_problem is None)

    entries: list[dict] = []
    problems: list[dict] = [directory_problem] if directory_problem else []
    capped = False
    listed = True
    unreadable = 0

    # Assembled-then-paged whenever a filter needs the whole set to be right:
    # the capability filter needs both stores merged, and the contact
    # name filter needs the decrypted legal name, which no SQL predicate can see.
    whole = bool(capability or needle) or scoped
    if unknown_customer or directory_problem is not None:
        # No rows in either case, and for the same reason: this view is "the
        # contacts of the customers that name means", and right now that set is
        # either empty or unknown. Falling through would list **every**
        # customer's contacts under a filter the operator typed — a wider answer
        # than the question, which is the worse of the two failures.
        found, more, rows = [], False, []
    elif whole:
        if scoped:
            # The whitelist read is per customer, which is why the rows have to
            # be too: merging one customer's registrations against another's
            # saved records would be the page inventing a relationship.
            entries, problem, capped = await _whitelist(client, customer_id)
            problems += [p for p in (problem,) if p]
            listed = problem is None
            book = await counterparties.rows(
                session, customer_id, family=filters["family"], limit=SCOPED_CAP + 1
            )
        else:
            book = await counterparties.everyones(
                session,
                family=filters["family"],
                customer_ids=ids or None,
                limit=SCOPED_CAP + 1,
            )
        if len(book) > SCOPED_CAP:
            problems.append(
                local_problem(
                    "More contacts than this page reads",
                    f"The first {SCOPED_CAP} saved contacts are merged, filtered and paged "
                    "here; anything past that is on no page of this view.",
                    "Export the CSV, or narrow by rail family.",
                    resource_id=customer_id or "all customers",
                )
            )
            book = book[:SCOPED_CAP]
        if needle:
            # An unreadable row cannot match a name — its legal name is exactly
            # what cannot be read — so it is COUNTED rather than silently absent.
            # Counting is the cheap honest option: surfacing every unreadable row
            # under every name filter would answer a search with rows that do not
            # match it.
            unreadable = sum(1 for row in book if row["recipient"] is None)
            book = [row for row in book if name_matches(row, needle)]
        found = counterparties.merge(book, entries)
        # A capability answer needs both halves. With the whitelist unread the
        # rows are shown unfiltered under a stated refusal rather than filtered
        # against a `False` nobody established.
        if capability and (listed and (scoped or capability == "saved")):
            found = [row for row in found if _capable(row, capability)]
        more = len(found) > offset + size
        rows = found[offset : offset + size]
    else:
        # Nothing here needs the whole set, so paging stays exact in SQL: one
        # extra row tells the pager whether there is a Next (the dashboard's
        # idiom). Nothing to merge — Conduit lists no whitelist across customers.
        found = await counterparties.everyones(
            session,
            family=filters["family"],
            customer_ids=ids or None,
            limit=size + 1,
            offset=offset,
        )
        more = len(found) > size
        rows = counterparties.merge(found[:size], [])

    # Names, never ids, where a name exists. One bounded page of customers, the
    # console's own `NAME_LIMIT` — `with_customer_names` is the wrong tool here
    # because there is no second read to gather it *with*: the rows are local.
    # Silent on failure, exactly as that helper is: the list renders unchanged,
    # minus the names, rather than growing a banner about an outage it survived.
    # Named `directory`, not `listed`: `listed` is this page's whitelist-readable
    # flag, and a customers read that shadowed it made every row render as
    # "readable" no matter what Conduit had answered.
    directory = await client.page("/v2/customers", limit=NAME_LIMIT)
    names = customer_names(directory.items) if isinstance(directory, Page) else {}
    # The same bounded page, as datalist suggestions: a name typed here is
    # searched whether or not it is among them (`resolve_customers` walks), so
    # this suggests and never constrains.
    suggestions = directory.items if isinstance(directory, Page) else []
    return render(
        request,
        "contacts/index.html",
        section="contacts",
        rows=rows,
        names=names,
        suggestions=suggestions,
        pager=offset_pager("/contacts", filters, offset=offset, size=size, more=more),
        problems=problems,
        scoped=scoped,
        listed=listed,
        capped=capped,
        # What a typed customer name resolved to, and whether that search stopped
        # at its own cap — both stated, never implied by a shorter list.
        matched_customers=matched if typed and not typed.startswith("cus_") else 0,
        unknown_customer=unknown_customer,
        name_capped=name_capped,
        walk_pages=customers.NAME_WALK_PAGES * customers.NAME_WALK_LIMIT,
        unreadable_count=unreadable,
        # The capability filter can only be answered in full for one customer —
        # said on the page rather than silently returning fewer rows. An
        # unreadable whitelist is the second way it cannot be answered.
        unanswerable=bool(capability in NEEDS_CUSTOMER and not scoped),
        unresolvable=bool(capability and scoped and not listed),
        filtered=any(filters.values()),
        families=tuple(payments.RAILS_FOR),
        capabilities=CAPABILITIES,
        **filters,
    )
