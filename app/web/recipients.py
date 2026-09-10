"""Whitelisting a contact for transfers: the registration form, revoke, and the
sandbox review simulator.

A whitelist entry is Conduit's registered record of a destination the customer is
allowed to pay — `intercompany` payouts are refused without one
(`RECIPIENT_NOT_WHITELISTED`), and the payout form's recipient picker offers only
the `registered` ones.

**The list moved; the form stayed**. Registered destinations
are now one half of `/customers/{id}/contacts`, where they sit beside the saved
records they may be the same destination as. This route keeps its URL because it
is a *form* with three query modes — `?rail=`, the `?target=&account=`
cross-customer shortcut, and the new `?contact=` bridge — and a 301 would delete
the feature rather than move it. A successful registration redirects to Contacts,
which is where its status is explained and where it can be revoked.

**The `?contact=` bridge is the shortcut in reverse** (the "promote
to whitelist"). The shortcut lifts coordinates off *another customer's virtual
account*; the bridge lifts them off *this customer's saved contact*, which this
console encrypted itself. Same defence, for the same reason: the record is read
again server-side at submit and the browser's copy of every coordinate key is
dropped before the fresh read is overlaid, so a prefilled form cannot be edited
into registering an account the saved record does not name. The relationship is
**not** decided here — a saved payout destination proves nothing about whether
the customer owns it — so the operator answers that, and Conduit reviews the
registration exactly as it reviews every other one.

The form is static rather than discovered: `POST /customers/{id}/whitelist-
recipients` takes a `oneOf` discriminated by `rail` with no requirements endpoint
in front of it. The three shapes are written down in `app.payments` as Dialect B
payloads, so the engine renders and validates them exactly as it does discovery's
own forms — including the ABA checksum and the IBAN mod-97.

**The shortcut narrates itself**. Reached with
`?target=&account=`, the form states whose account is being registered onto whose
whitelist, why the relationship is `group_entity`, and what Conduit does next;
the submission redirects with `?registered=<entry id>` so the list can explain
*that entry's* status rather than flash one sentence for every outcome; and a
`group_entity` row names the target customer wherever this console's own ledger
can prove it (`contacts._shortcut_targets`). None of the mechanics moved.

Both mutations are ledgered (`whitelist_create`, `whitelist_revoke`): a
double-clicked registration would otherwise be two entries for one bank account,
and a revoke whose response was lost would leave the operator unsure whether the
destination is still payable. The sandbox approve/reject buttons are not — same
rule as every other simulator (see `app.web`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import accounts, audit, counterparties, documents, forms, operations, payments
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Problem, Success
from app.web import (
    ALREADY_SPENT_ELSEWHERE,
    conduit,
    db,
    form_items,
    intent_of,
    is_sandbox,
    local_problem,
    problem_line,
    problem_of,
    problem_note,
    problem_view,
    redirect,
    render,
)
from app.web import onboarding  # `field_errors`: one 422→engine mapping in the app

router = APIRouter()


def _problem(result, what: str, resource_id: str = "") -> dict:
    return (
        problem_view(result)
        if isinstance(result, Problem)
        else local_problem(
            "Conduit is unreachable", f"{what} could not be read.", resource_id=resource_id
        )
    )


async def _shortcut(
    client: ConduitClient, target_id: str, account_id: str, *, customer_id: str = ""
) -> tuple[dict, dict | None]:
    """The "whitelist another customer's account" prefill.

    Reads the *target* customer's virtual account and lifts the coordinates a
    payer would be handed out of its own deposit instructions. Nothing is
    invented: an account that publishes no registrable block yields no prefill
    and the operator fills the form by hand.

    Run again at submit, not only at render: the coordinates that
    reach Conduit are this fresh read's, so a browser cannot keep the shortcut's
    `group_entity` framing while typing a different account number into the
    prefilled fields. Two refusals close the rest of that gap: a target that is
    the customer itself is not a *cross-customer* registration at all, and an
    account that is not `active` is not a destination anyone should be paid at.
    """
    if customer_id and target_id == customer_id:
        return {}, local_problem(
            "That is this customer's own account",
            "The shortcut registers another customer's account as a group entity. Register the "
            "customer's own account with the `self` relationship instead.",
            resource_id=account_id,
        )
    result = await accounts.fetch_account(client, target_id, account_id)
    if not isinstance(result, dict):
        return {}, _problem(result, "The target account", account_id)
    if result.get("status") != "active":
        return {}, local_problem(
            "That account is not active",
            f"It is {result.get('status') or 'in an unknown state'}; Conduit publishes deposit "
            "instructions for an active account, and only those coordinates can be registered.",
            resource_id=account_id,
        )
    prefill = payments.prefill_from_account(result)
    if not prefill:
        return {}, local_problem(
            "Nothing to prefill",
            "That account publishes no deposit instructions this console can register.",
            "Register the recipient by hand, or pick another account.",
            resource_id=account_id,
        )
    # `group_entity` by construction: another customer's account is by definition
    # not this customer's own.
    prefill["values"]["relationship"] = "group_entity"
    prefill["target"] = {"customer_id": target_id, "account_id": account_id}
    # The shortcut prints coordinates for the same non-reason the bridge did:
    # `SERVER_OWNED` is stripped and re-read at submit, so these are a statement,
    # not an input, the same as the bridge's sibling caller.
    prefill["sealed"] = _seal(prefill["values"], payments.COORDINATE_KEYS)
    return prefill, None


# Which customer's account a `group_entity` entry was registered from. Written
# by `create` when the shortcut produced the registration, read back by the list
# so a row can name the target instead of showing a bare account number.
SHORTCUT_ACTION = "whitelist.group_entity"
# The bridge's own audit action — "this registration was put forward from that
# saved contact". Named in the `counterparty.` family because it is a fact about
# a contact (its history panel reads it), not about the shortcut above.
BRIDGE_ACTION = "counterparty.bridged"

# Everything the shortcut decides for itself: the coordinates it lifts off the
# target account's own deposit instructions (`payments.prefill_from_account`),
# the legal name that comes with them, and the relationship it proves. On a
# shortcut submission these are dropped from the browser's values before the
# fresh read is overlaid, so no forged coordinate can survive in a slot this
# particular account did not publish.
#
# `payments.COORDINATE_KEYS` is the console's one list of "what identifies a
# destination" — the same tuple the whitelist gate copies onto a gated payout —
# so this is that list plus the one fact the shortcut *proves* rather than reads.
SERVER_OWNED = payments.COORDINATE_KEYS + ("relationship",)
# The bridge owns the coordinates and nothing else: a saved payout destination is
# evidence of where money went, never of whose account it is, so `relationship`
# stays the operator's answer and Conduit's to review.
CONTACT_OWNED = payments.COORDINATE_KEYS


def _seal(values: dict, owned: tuple) -> dict:
    """The server-owned coordinates as a **display** map, masked by the console's
    one policy (`counterparties.identify`'s rule).

    A bridged or shortcut prefill is not the operator's input: the POST re-reads
    the record and overlays these keys whatever the browser sent, so rendering
    them as editable inputs printed a full account number onto a page for a value
    the form does not use. The form-input exception
    to the masking rule covers fields an operator *types into*; these are not
    that. Account coordinates are masked; a routing number and a BIC identify a
    bank and are public routing data, so they stay whole exactly as they do in
    every list cell.
    """
    # Keyed by the *rendered field name* (`f.accountNumber`), because that is
    # what the template matches against — the values map is keyed bare.
    return {
        forms.field_name((key,)): (
            counterparties.mask(values[key])
            if key in counterparties.MASKED_KEYS
            else str(values[key])
        )
        for key in owned
        if values.get(key) not in (None, "")
    }


async def _from_contact(
    session: AsyncSession, customer_id: str, contact_id: str
) -> tuple[dict, dict | None]:
    """The "whitelist this saved contact" prefill — `{rail, values}`, or a refusal.

    `counterparties.get` filters on `customer_id`, so a contact of another
    customer resolves to nothing and gets the same answer an invented id does.
    The fields prefilled are **the rail variant's own** (`WHITELIST_VARIANTS`),
    intersected with what the record actually holds: nothing is invented for a
    slot the saved destination never had, and no key is copied that this
    registration DTO has no place for.
    """
    saved = await counterparties.get(session, customer_id, contact_id)
    if saved is None or not saved["recipient"]:
        return {}, local_problem(
            "That contact could not be used",
            "It is archived, unreadable, or not this customer's. Register the destination by "
            "hand, or pick another contact.",
            resource_id=contact_id,
        )
    rail = str(saved["rail_family"] or "")
    if rail not in payments.WHITELIST_VARIANTS:
        return {}, local_problem(
            "That contact cannot be registered from here",
            f"It was saved for the {rail or 'unknown'} rail family, which is not one of the "
            f"{', '.join(payments.WHITELIST_RAILS)} shapes Conduit registers.",
            "Register the destination by hand.",
            resource_id=contact_id,
        )
    wanted = {field["name"] for field in payments.WHITELIST_VARIANTS[rail]["fields"]}
    values = {
        key: str(value)
        for key, value in saved["recipient"].items()
        if key in wanted and value not in (None, "")
    }
    if not any(key in values for key in payments.COORDINATE_KEYS):
        return {}, local_problem(
            "Nothing to prefill",
            "That contact holds no coordinate this rail's registration asks for.",
            "Register the recipient by hand, or pick another contact.",
            resource_id=contact_id,
        )
    # The operator's own name for the contact, in the entry's optional label —
    # so the registration is findable under the name the address book uses.
    values.setdefault("label", saved["label"])
    sealed = _seal(values, CONTACT_OWNED)
    # `values` stays complete — the POST's overlay writes exactly these onto the
    # body. `sealed` is what the *page* states instead of rendering inputs for
    # them; `_values` is the render-side adapter that drops them.
    return {"rail": rail, "values": values, "sealed": sealed, "contact": saved}, None


def _render_form(
    request: Request,
    customer_id: str,
    *,
    rail: str,
    values: forms.FormValues | None = None,
    errors: forms.FormErrors | None = None,
    problems: list[dict] | None = None,
    shortcut: dict | None = None,
    contact: dict | None = None,
    sealed: dict | None = None,
    can_act: bool = True,
    status_code: int = 200,
) -> Response:
    """One renderer for every state of the registration form.

    **No whitelist read on any path**: the table this page
    used to carry is on Contacts now, so neither the render nor a 422 re-render
    spends a list call on rows nobody is looking at.
    """
    model = payments.whitelist_model(rail)
    return render(
        request,
        "recipients/list.html",
        section="customers",
        status_code=status_code,
        customer_id=customer_id,
        rail=rail,
        rails=payments.WHITELIST_RAILS,
        model=model,
        rm=forms.render_model(model, values, errors),
        purpose=payments.EVIDENCE_PURPOSE,
        problems=[p for p in (problems or []) if p],
        shortcut=shortcut or {},
        contact=contact or {},
        # The server-owned coordinates, masked, for the page to state rather than
        # render as inputs.
        sealed=sealed or {},
        can_act=can_act,
    )


def _values(prefill: dict | None) -> forms.FormValues | None:
    """The prefill as the **form** sees it: everything except the server-owned
    coordinates, which the page states masked instead (`_seal`).

    Render-side only. The submit path never comes through here — it overlays
    `prefill["values"]` whole onto the parsed submission, which is what makes
    dropping them here safe: the value that reaches Conduit is the stored
    record's either way, and the page no longer prints it to prefill a field
    whose contents are discarded.
    """
    if not prefill:
        return None
    sealed = prefill.get("sealed") or {}
    return forms.FormValues(
        root={
            key: value
            for key, value in (prefill.get("values") or {}).items()
            if forms.field_name((key,)) not in sealed
        }
    )


@router.get("/customers/{customer_id}/recipients", response_class=HTMLResponse)
async def index(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The registration form. `?rail=` picks the DTO variant; `?target=&account=`
    prefills it from another customer's virtual account; `?contact=` prefills it
    from one of this customer's saved contacts.

    Registered destinations themselves are listed on Contacts — this route reads
    no whitelist at all now, on any path.
    """
    rail = request.query_params.get("rail") or payments.WHITELIST_RAILS[0]
    if rail not in payments.WHITELIST_VARIANTS:
        return redirect(request, f"/customers/{customer_id}/recipients", err="Unknown rail.")

    prefill: dict = {}
    problem = None
    target, account_id = request.query_params.get("target"), request.query_params.get("account")
    contact_id = (request.query_params.get("contact") or "").strip()
    if target and account_id:
        prefill, problem = await _shortcut(client, target, account_id, customer_id=customer_id)
    elif contact_id:
        prefill, problem = await _from_contact(session, customer_id, contact_id)
    rail = prefill.get("rail") or rail
    return _render_form(
        request,
        customer_id,
        rail=rail,
        values=_values(prefill),
        problems=[problem],
        shortcut=prefill if prefill.get("target") else {},
        contact=prefill if prefill.get("contact") else {},
        sealed=prefill.get("sealed") or {},
        can_act=actor.can("whitelist.register"),
    )


@router.post("/customers/{customer_id}/recipients")
async def create(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("whitelist.register")),
) -> Response:
    items = await form_items(request)
    submitted = dict(items)
    rail = (submitted.get("rail") or "").strip()
    if rail not in payments.WHITELIST_VARIANTS:
        return redirect(
            request,
            f"/customers/{customer_id}/recipients",
            err="Pick a rail this console can register.",
        )

    model = payments.whitelist_model(rail)
    values = forms.parse_submission(model, items)

    # The shortcut, re-run server-side. What the render prefilled is *not* what
    # gets registered — this read is, so the browser cannot keep the shortcut's
    # framing while substituting coordinates of its own.
    shortcut: dict = {}
    contact: dict = {}
    target, account_id = submitted.get("target"), submitted.get("account")
    contact_id = (submitted.get("contact") or "").strip()
    if target and account_id:
        shortcut, refusal = await _shortcut(client, target, account_id, customer_id=customer_id)
        prefill, owned = shortcut, SERVER_OWNED
    elif contact_id:
        # The bridge, re-run server-side for the same reason the shortcut is: the
        # coordinates that reach Conduit are this fresh read's, so a browser
        # cannot keep the "whitelist my saved contact" framing while registering
        # an account the contact does not name.
        contact, refusal = await _from_contact(session, customer_id, contact_id)
        prefill, owned = contact, CONTACT_OWNED
    else:
        prefill, owned, refusal = {}, (), None
    if prefill or refusal is not None:
        if refusal is not None or not prefill:
            return _render_form(
                request,
                customer_id,
                rail=rail,
                values=values,
                problems=[refusal],
                status_code=422,
            )
        rail = prefill["rail"]
        model = payments.whitelist_model(rail)
        values = forms.parse_submission(model, items)
        # **Strip first, then overlay.** Overlaying alone only replaced the keys
        # this account happens to publish: a SWIFT or SEPA block yields no
        # `accountNumber`, so a forged one typed into the prefilled form
        # survived the "re-read at submit" defence and reached `whitelist_body`
        # beside the real coordinates. These keys belong to the fresh read or to
        # nothing — the browser's copy of them is never evidence of anything.
        for key in owned:
            values.root.pop(key, None)
        values.root |= prefill["values"]
        if shortcut:
            # `group_entity` because it provably is one: the target is another
            # customer, checked above. The bridge proves no such thing — a saved
            # payout destination says nothing about who owns the account — so it
            # leaves `relationship` exactly as the operator answered it.
            values.root["relationship"] = "group_entity"

    # The seal belongs to the SURFACE, not to the GET. `values` now carries the
    # freshly re-read coordinates (the overlay above), so any re-render from here
    # on must state them masked rather than print them into inputs whose contents
    # this handler discards — the same rule `_values`/`_seal` enforce on render.
    sealed = prefill.get("sealed") or {}

    errors = forms.validate(model, values)
    payments.whitelist_errors(rail, values, errors)
    # A `doc_` id on this form is an assertion, exactly as it is on a payout
    # (`documents.attachable`). These ids are the *evidence* for the
    # relationship being registered — the one thing on the body a reviewer at
    # Conduit reads to decide whether this account may be paid — so an id the
    # operator did not upload here for this purpose is another customer's
    # paperwork standing in for evidence nobody has. Reported on the documents
    # widget and before `operations.start`, so a refusal re-renders the form with
    # everything the operator typed and nothing has been sent.
    if refused := await documents.unattachable(
        session, values.document_ids, purpose=payments.EVIDENCE_PURPOSE, actor_id=actor.id
    ):
        errors.documents.append(
            forms.Message(documents.REFUSED_ATTACHMENT.format(count=len(refused)))
        )
    if not errors.ok:
        return _render_form(
            request,
            customer_id,
            rail=rail,
            values=values,
            errors=errors,
            shortcut=shortcut,
            contact=contact,
            sealed=sealed,
            status_code=422,
        )

    op, is_new = await operations.start(
        session,
        type="whitelist_create",
        actor_id=actor.id,
        actor_email=actor.email,
        path=payments.WHITELIST_PATH.format(customer_id=customer_id),
        body=payments.whitelist_body(rail, values, model),
        customer_id=customer_id,
        intent=intent_of(submitted),
    )
    if is_new and shortcut:
        # The one fact the created entry will not carry: whose account it is.
        # Recorded against the operation before the call, so the list can name
        # the target on every later render (`_shortcut_targets`). Both values
        # were verified by `_shortcut` above — a self-registration and an
        # inactive account never reach here.
        audit.record(
            session,
            action=SHORTCUT_ACTION,
            actor_id=actor.id,
            actor_email=actor.email,
            operation_id=op.id,
            detail={"target_customer": target, "target_account": account_id},
        )
        await session.commit()
    if is_new and contact:
        # The bridge's own fact: **which saved contact** this registration was put
        # forward from. Nothing else records it — a whitelist entry is Conduit's
        # resource and carries no console id, and the coordinate match that makes
        # the two one row on the Contacts page is a live join, so it says nothing
        # about the past. Written before the call, like the shortcut's row above,
        # and carrying no coordinates: the id and the label are what a history
        # panel needs (`counterparties.history`).
        audit.record(
            session,
            action=BRIDGE_ACTION,
            actor_id=actor.id,
            actor_email=actor.email,
            operation_id=op.id,
            detail={
                "customer": customer_id,
                "counterparty": contact_id,
                "label": str((contact.get("contact") or {}).get("label") or ""),
            },
        )
        await session.commit()
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed":
        # `?registered=` names the entry just created, so **Contacts** can say
        # what its status means and what to do next instead of a one-line flash
        # that is the same sentence whatever Conduit did with it. The list is
        # there now, and so is the row this registration just became.
        back = f"/customers/{customer_id}/contacts"
        if op.conduit_resource_id:
            return redirect(request, f"{back}?registered={op.conduit_resource_id}")
        return redirect(
            request, back, msg="Contact registered — Conduit reviews it before it can be paid."
        )
    if op.state == "rejected":
        # Conduit's own refusal on the operator's own values: field errors land
        # on their fields through the same mapper onboarding uses.
        return _render_form(
            request,
            customer_id,
            rail=rail,
            values=values,
            errors=forms.map_validation_errors(model, onboarding.field_errors(op.error)),
            problems=[problem_of(op.error, op.conduit_resource_id)],
            shortcut=shortcut,
            contact=contact,
            sealed=sealed,
            status_code=422,
        )
    return redirect(request, f"/operations/{op.id}")


@router.post("/customers/{customer_id}/recipients/{recipient_id}/revoke")
async def revoke(
    request: Request,
    customer_id: str,
    recipient_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("whitelist.revoke")),
) -> Response:
    """`DELETE …/{id}` through the ledger. The executor knows this type sends a
    DELETE with no body (`app.conduit.execute.MUTATION_METHOD`)."""
    back = f"/customers/{customer_id}/contacts"
    intent = intent_of(dict(await form_items(request)))
    path = f"{payments.WHITELIST_PATH.format(customer_id=customer_id)}/{recipient_id}"
    op, is_new = await operations.start(
        session,
        type="whitelist_revoke",
        actor_id=actor.id,
        actor_email=actor.email,
        path=path,
        body=None,
        customer_id=customer_id,
        intent=intent,
    )
    # **A spent nonce replayed at a different contact**. The nonce alone
    # resolves, and it is scoped to the operation *type*, so a revoke token
    # spent on contact A answered this revoke of contact B — and the
    # "Whitelisting revoked" below then said so about a registration that is
    # still live, so B stays registered and payable while the operator believes
    # it is not. The recipient is in the path, and the path is inside
    # `request_hash`, so one comparison is the whole check.
    if not is_new and operations.resolved_elsewhere(op, path):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SPENT_ELSEWHERE)
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed":
        return redirect(request, back, msg="Whitelisting revoked — the contact stays saved.")
    if op.state == "rejected":
        return redirect(request, back, err=problem_note(op.error, "Revoke refused."))
    return redirect(request, f"/operations/{op.id}")


# --- sandbox review simulation ------------------------------------------------------------

# `POST /v2/sandbox/whitelist-recipients/{id}/simulate-approve|-reject` — not
# nested under the customer, unlike the read paths. Empty body.
SIMULATE_PATH = "/v2/sandbox/whitelist-recipients/{id}/simulate-{outcome}"
SIMULATE_OUTCOMES = ("approve", "reject")


@router.post("/customers/{customer_id}/recipients/{recipient_id}/simulate")
async def simulate(
    request: Request,
    customer_id: str,
    recipient_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("sandbox.simulate")),
) -> Response:
    """Sandbox-only, refused server-side as well as hidden in the template."""
    back = f"/customers/{customer_id}/contacts"
    if not is_sandbox():
        return redirect(request, back, err="Whitelist review simulation is sandbox-only.")
    outcome = (dict(await form_items(request)).get("outcome") or "").strip()
    if outcome not in SIMULATE_OUTCOMES:
        return redirect(request, back, err="Unknown simulated outcome.")

    result = await client.mutate(
        "POST", SIMULATE_PATH.format(id=recipient_id, outcome=outcome), json={}
    )
    audit.record(
        session,
        action="sandbox.simulate_whitelist",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"recipient": recipient_id, "outcome": outcome, "ok": isinstance(result, Success)},
    )
    await session.commit()
    if isinstance(result, Success):
        return redirect(request, back, msg=f"Simulated {outcome}.")
    return redirect(request, back, err=problem_line(result))
