"""Batch payouts: template out, filled file in, validation report, and dispatch
(multi-purpose since the payout-flow restructure).

Eight surfaces, all hanging off one customer:

    GET  …/batches/new            pick the route, download a template, upload one
    GET  …/batches/template.csv   the blank template for that route
    POST …/batches                a filled file → a validated batch
    GET  …/batches                this customer's batches
    GET  …/batches/{id}           the validation report / the live results
    GET  …/batches/{id}/confirm   the money moment — the ONLY dispatch button
    POST …/batches/{id}/dispatch  send every valid row, once
    POST …/batches/{id}/ready|abandon

**A batch's route is rail + recipient type. `purpose` is a column.** The batch
screen is therefore its own entry off the Transact fork rather than a tray
inside the single-payout form: the form's route includes a purpose and a
batch's does not, and hanging one off the other made every batch inherit a
purpose it did not need. `…/batches/new` asks for the two-part route and the
funding account, and that is the whole of it. Destination country is not part
of it: discovery's schema is chosen by rail and recipient type, so one file's
rows may name recipients in any number of countries.

**The dispatch button lives on the confirm screen and nowhere else.** The report
is a page an operator reads; the confirm screen is a page an operator answers,
and it re-derives everything it states — totals, exclusions, the funding
account's balance, the shared documents — so nothing on it is inherited from the
screen before it. `POST …/dispatch` is guarded by role, by CSRF, by
`hx-sync`/`hx-disabled-elt` and by the batch's status; none of those is what
makes it safe to press twice. That is `batches.intent_for` (OPERATIONS_SPEC §1).

**The template is generated from the live requirements responses**, never from a
file in this repository (`app/batches.py` explains why the fingerprint in its
header block matters). One read per purpose — seven, bounded, gathered — and the
purposes that answer are the ones the template's `purpose` column accepts. A
template can only be produced for a route at least one purpose exists on.

**An upload always revalidates against fresh discovery**, for every purpose
again. The fingerprint the file carries is compared, and a difference is reported
as a *stale template* warning on the batch. It never selects which schema
validates: the rows are judged by what Conduit says today, because that is what
Conduit will judge the payouts by.

**Every refusal is the whole file's.** Unknown columns, a missing required
column, the wrong route, a file that is not UTF-8, one too big or too long: no
batch row is written and the operator is sent back to the form with the sentence.
A *row* can be invalid — that is what the report is for — but a file this console
cannot account for is not stored at all.

**Permissions.** `console.view` reads the report and downloads a template (it
contains no customer data — the route's field names and nothing else). Uploading,
attaching documents, marking ready and abandoning need `batch.upload`; sending
the rows needs `batch.dispatch`.

The batch itself is still not an operations-ledger row: uploading, attaching,
marking ready and abandoning make no Conduit call, create no remote resource and
have no outcome that can stay unknown (OPERATIONS_SPEC §1; the same exception the
contacts writes hold). All of them are audited. **Dispatch is the opposite**:
every row it sends is a `payout_create` operation with everything §2 gives one,
and the batch row simply points at it.
"""

from __future__ import annotations

from urllib.parse import urlencode

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response, StreamingResponse

from app import accounts, audit, batches, counterparties, documents, forms, payments
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem
from app.db import sessionmaker
from app.web import (
    conduit,
    db,
    export_url,
    form_items,
    local_problem,
    offset_of,
    offset_pager,
    page_size,
    redirect,
    render,
)
from app.web import payouts
from app.web.exports import _cell, _stream, filename
from app.webhooks.inbox import read_capped

log = logging.getLogger(__name__)
router = APIRouter()

INDEX = "/customers/{customer_id}/batches"
NEW = INDEX + "/new"
TEMPLATE = INDEX + "/template.csv"
DETAIL = INDEX + "/{batch_id}"

READY_STATES = ("validating",)
# Abandoning is legal **only before dispatch starts** (enforced by
# `batches.LEGAL_STATUS` as well as here):
# once a row has an operation, "abandoned" is a word this console cannot make
# true. The exits after that are the rows' own terminal states and the batch's
# completion.
LIVE_STATES = ("validating", "ready")
# The two statuses the confirm screen and the dispatch route accept. A partially
# dispatched batch is included on purpose: a re-dispatch is how a crashed run
# finishes, and it goes through the same confirmation as the first one.
DISPATCHABLE = ("ready", "partially_dispatched")


# The sentence each of these three routes refuses a click with when the batch is
# not in a status that click is for. They are lifted out of the routes' own
# up-front checks so that a **lost race** can be refused in the same words
#: `batches.set_status` is a guarded UPDATE now and reports a legal
# transition that arrived a moment too late as `False` rather than as an
# exception, and the operator on the losing side is owed the sentence they would
# have got had they clicked a moment later — never a 500, never a silent no-op.
# One phrasing per route and no new one: these are still the route's own words.


def _unchangeable(status: str) -> str:
    return f"This batch is {status.replace('_', ' ')} — it cannot change."


def _undispatchable(status: str) -> str:
    return f"This batch is {status.replace('_', ' ')} — it cannot be dispatched."


def _unabandonable(status: str) -> str:
    return (
        "This batch has been dispatched — it cannot be abandoned. Each row now ends at "
        "its own payout."
        if status in ("partially_dispatched", "dispatched")
        else "This batch is already closed."
    )


def _corridor(route: payouts.Route) -> bool:
    """A batch's corridor: rail + recipient type. Country is not part of
    it — see the module docstring."""
    return route.rail in payments.RAILS and route.recipient_type in payments.RECIPIENT_TYPES


def _route_query(route: payouts.Route) -> str:
    return urlencode(
        {
            "rail": route.rail,
            "recipientType": route.recipient_type,
            "virtualAccountId": route.virtual_account_id,
        }
    )


def _back(customer_id: str, route: payouts.Route) -> str:
    """The batch screen for this route — where a refused upload is sent back to,
    because that is where the template that produced the file came from."""
    return f"{NEW.format(customer_id=customer_id)}?{_route_query(route)}"


def _refusal(result, what: str) -> str:
    """One sentence out of a failed Conduit read, for the flash banner."""
    if isinstance(result, Problem):
        return f"{what} could not be read: {result.title}"
    return f"{what} could not be read — Conduit is unreachable."


async def _requirements(
    client: ConduitClient, route: payouts.Route
) -> tuple[dict[str, dict], list[str]]:
    """`({purpose: snapshot}, [the purposes that would not answer])` for one
    corridor — every purpose, gathered.

    Seven concurrent reads, which is the whole reason a batch template can be
    keyed by the corridor alone: the union of what they declare is the file's
    columns, and the ones that answer are what the `purpose` column accepts. A
    purpose Conduit refuses for this corridor is **named**, not guessed at and
    not fatal — losing all seven because `prefunding` 4xx'd on a corridor nobody
    was going to use it for is a worse answer than saying which six are live.
    """
    answers = await asyncio.gather(
        *(
            payments.fetch_requirements(
                client,
                purpose=purpose,
                rail=route.rail,
                recipient_type=route.recipient_type,
            )
            for purpose in payments.PURPOSE_VALUES
        )
    )
    snapshots, missing = {}, []
    for purpose, answer in zip(payments.PURPOSE_VALUES, answers, strict=True):
        if isinstance(answer, dict):
            snapshots[purpose] = answer
        else:
            missing.append(purpose)
    return snapshots, missing


def _models(snapshots: dict[str, dict]) -> dict[str, forms.FormModel]:
    return {purpose: payments.payout_model(snapshot) for purpose, snapshot in snapshots.items()}


def _batch_route(batch) -> payouts.Route:
    """A stored batch read back as the corridor it was uploaded for — one parser
    for the query string and the row, so the confirm screen's drift check asks
    discovery exactly what the upload asked it."""
    return payouts.Route(
        {
            "rail": batch.rail,
            "recipientType": batch.recipient_type,
            "destinationCountry": batch.destination_country,
            "virtualAccountId": batch.virtual_account_id,
        }
    )


# --- the route screen ---------------------------------------------------------------------


@router.get(NEW, response_class=HTMLResponse)
async def new(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Pick the corridor, download the template, upload a filled one.

    The batch half of the Transact fork. It asks for two things and a funding
    account, and deliberately **not** a purpose: the purpose is a column of the
    file, so asking for one here would be asking the operator to choose something
    the file then overrides seven times.
    """
    route = payouts.Route(request.query_params)
    corridor = _corridor(route)
    problems: list[dict] = []
    snapshots: dict[str, dict] = {}
    missing: list[str] = []
    if corridor:
        snapshots, missing = await _requirements(client, route)
        if not snapshots:
            problems.append(
                local_problem(
                    "No purpose can be paid over this route",
                    "Conduit answered with a problem for every one of the seven purposes on "
                    f"this corridor, so there are no columns to build a template from. "
                    f"Refused: {', '.join(missing)}.",
                )
            )
    available, accounts_problem = await payouts._funding(client, customer_id)
    doomed = {
        str(a.get("id")): payments.doomed_rail(route.rail, payouts._asset_of(a))
        for a in available
    }
    account = next(
        (a for a in available if a.get("id") == route.virtual_account_id),
        next((a for a in available if not doomed.get(str(a.get("id")))), None),
    )
    return render(
        request,
        "batches/new.html",
        section="orders",
        customer_id=customer_id,
        route=route,
        corridor=corridor,
        rails=payments.RAILS,
        recipient_types=payments.RECIPIENT_TYPES,
        models=_models(snapshots),
        purpose_help=payments.PURPOSE_HELP,
        missing=missing,
        accounts=available,
        accounts_problem=accounts_problem,
        doomed_accounts=doomed,
        rail_message=payments.RAIL_ASSET_MESSAGE,
        account=account,
        asset=payouts._asset_of(account),
        query=_route_query(route),
        problems=problems,
        can_act=actor.can("batch.upload"),
    )


# --- the template ----------------------------------------------------------------------


@router.get(TEMPLATE)
async def template(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The blank template for one route, straight off `GET /v2/payouts/
    requirements`.

    A plain GET anchor, so it needs no JavaScript and can be re-downloaded from
    history. A route whose requirements cannot be read produces **no file**: a
    template with guessed columns is the one artefact that would quietly turn
    into a wrong payment later.
    """
    route = payouts.Route(request.query_params)
    if not _corridor(route):
        return redirect(
            request,
            NEW.format(customer_id=customer_id),
            err="Pick a rail and a recipient type before downloading a template.",
        )
    snapshots, missing = await _requirements(client, route)
    if not snapshots:
        return redirect(
            request,
            _back(customer_id, route),
            err="This route's requirements could not be read for any purpose, so there are no "
            "columns to build a template from.",
        )
    try:
        models = _models(snapshots)
    except forms.SchemaVersionMismatch as mismatch:  # pragma: no cover - Dialect B has none
        return redirect(request, _back(customer_id, route), err=str(mismatch))
    header = batches.columns(models)
    lines = batches.header_block(
        customer_id=customer_id,
        rail=route.rail,
        recipient_type=route.recipient_type,
        digest=batches.fingerprint(snapshots),
        models=models,
        missing=missing,
    )
    filters = {
        "rail": route.rail,
        "recipientType": route.recipient_type,
    }
    return StreamingResponse(
        # The export module's writer, whole: same quoting, and the same
        # injection-safe cell rule on every emitted value (a discovery label
        # starting with `=` is text, not a formula).
        _stream(tuple(_cell(name) for name in header), [], [], preamble=lines),
        media_type="text/csv; charset=utf-8",
        headers={
            "content-disposition": (
                f'attachment; filename="{filename("batch_template", filters)}"'
            )
        },
    )


# --- upload ----------------------------------------------------------------------------


@router.post(INDEX)
async def upload(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("batch.upload")),
) -> Response:
    """A filled template → one validated batch, or a refusal and no batch at all.

    The body is the file's raw bytes under a streaming cap (`read_capped`, the
    document uploader's idiom — Starlette's form parser would need a multipart
    dependency this console deliberately does not have). Everything else rides in
    the query string, which is the route the operator is on.
    """
    route = payouts.Route(request.query_params)
    back = _back(customer_id, route)
    if not _corridor(route):
        return redirect(
            request,
            NEW.format(customer_id=customer_id),
            err="Pick a rail and a recipient type before uploading a batch.",
        )

    data = await read_capped(request, batches.MAX_BYTES)
    if data is None:
        return redirect(request, back, err=batches.TOO_LARGE)
    parsed, refusal = batches.parse(data)
    if parsed is None:
        return redirect(request, back, err=refusal)

    # Fresh discovery, unconditionally and for every purpose: the file's
    # fingerprint is evidence about the template, never the schema the rows are
    # judged by.
    snapshots, _missing = await _requirements(client, route)
    if not snapshots:
        return redirect(
            request,
            back,
            err="This route's requirements could not be read for any purpose, so nothing in "
            "this file could be validated. Nothing was stored.",
        )
    models = _models(snapshots)

    refusal = batches.check_route(
        parsed,
        {
            "customer": customer_id,
            "rail": route.rail,
            "recipientType": route.recipient_type,
        },
    ) or batches.check_columns(
        parsed, batches.columns(models), batches.required_columns(models)
    )
    if refusal:
        return redirect(request, back, err=refusal)
    # Emptiness last: a header-only file whose header is also wrong gets told
    # about the header, which is the answer that fixes both.
    if not parsed.rows:
        return redirect(request, back, err=batches.NO_ROWS)

    # The payout page's own "which accounts may fund this" read, reused rather
    # than re-derived: one definition of an active account, two screens.
    available, _ = await payouts._funding(client, customer_id)
    account = next(
        (a for a in available if a.get("id") == route.virtual_account_id),
        available[0] if available else None,
    )
    if account is None:
        return redirect(
            request,
            back,
            err="Pick an active virtual account to fund the batch — its currency is the "
            "currency of the totals.",
        )
    # The currency guard, once for the whole batch: the funding account
    # is picked once and every row inherits it, so a doomed pair here is not one
    # failed payout — it is the whole file, each row accepted at create and each
    # failing after review with `rail_unavailable`. Refused before the batch row
    # exists, for the same reason the single payout is refused before its
    # operation does.
    if pinned := payments.doomed_rail(
        route.rail, payouts._asset_of(account)
    ):
        return redirect(
            request,
            back,
            err=payments.RAIL_ASSET_MESSAGE.format(
                rail=route.rail, pinned=pinned, asset=payouts._asset_of(account)
            ),
        )

    # One read of each store for the whole file. A mixed file needs both: a
    # gated row names Conduit's registered recipient, a free-form row names this
    # console's saved contact, and a per-row lookup would be one query (or one
    # Conduit request) per payout.
    contacts: dict[str, dict] = {}
    entries: list[dict] = []
    if any(batches.is_gated(model) for model in models.values()):
        page = await payments.fetch_recipients(client, customer_id)
        if not isinstance(page, Page):
            # Refused rather than validated: a gated row names a registered
            # recipient, so an unreadable whitelist would produce a batch whose
            # gated rows are all invalid for a reason that is not the file's.
            return redirect(request, back, err=payments.UNREADABLE_WHITELIST)
        entries = list(page.items)
    if not all(batches.is_gated(model) for model in models.values()):
        saved = await counterparties.rows(
            session,
            customer_id,
            family=payments.family_of(route.rail),
            recipient_type=route.recipient_type,
        )
        for row in saved:
            contacts[str(row["label"] or "").lower()] = row
            contacts.setdefault(str(row["id"]).lower(), row)

    validator = batches.Validator(
        models=models,
        rail=route.rail,
        contacts=contacts,
        entries=entries,
    )
    rows = [
        validator.row(cells, ragged=number in parsed.ragged)
        for number, cells in enumerate(parsed.rows, start=1)
    ]
    # The document gate is per purpose and the widget is per batch, so the batch
    # stores the question asked of the purposes it actually holds: does anything
    # in this file need one, and what does Conduit accept for those rows.
    documents_required, accepted = batches.documentation_of(
        models, [row["purpose"] for row in rows if not row["errors"]]
    )

    digest = batches.fingerprint(snapshots)
    batch = await batches.create(
        session,
        customer_id=customer_id,
        rail=route.rail,
        recipient_type=route.recipient_type,
        virtual_account_id=str(account.get("id") or ""),
        asset=payouts._asset_of(account),
        digest=digest,
        template_digest=parsed.template_fingerprint,
        filename=(request.query_params.get("filename") or "batch.csv").rsplit("/", 1)[-1],
        documents_required=documents_required,
        accepted_document_types=accepted,
        actor_id=actor.id,
        actor_email=actor.email,
        rows=rows,
    )
    invalid = sum(1 for row in rows if row["errors"])
    audit.record(
        session,
        action="payout_batch.uploaded",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={
            "customer": customer_id,
            "batch": str(batch.id),
            "filename": batch.filename,
            "rows": len(rows),
            "invalid": invalid,
            # The template's own claim, kept: which discovery snapshot the
            # operator was working from is a fact about the upload.
            "stale_template": parsed.template_fingerprint != digest,
        },
    )
    await session.commit()
    return redirect(request, DETAIL.format(customer_id=customer_id, batch_id=batch.id))


# --- the batches, and one batch --------------------------------------------------------


@router.get(INDEX, response_class=HTMLResponse)
async def index(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """This customer's batches. One local SELECT plus one grouped count — no
    Conduit call, so this page is readable when Conduit is not.

    Paged: `summaries` was capped at 50 and said nothing
    about it, so a customer's 51st batch was simply not on any page.
    """
    size, offset = page_size(request.query_params), offset_of(request.query_params)
    found = await batches.summaries(session, customer_id, limit=size + 1, offset=offset)
    return render(
        request,
        "batches/list.html",
        section="orders",
        customer_id=customer_id,
        rows=found[:size],
        pager=offset_pager(
            INDEX.format(customer_id=customer_id),
            {},
            offset=offset,
            size=size,
            more=len(found) > size,
        ),
        can_act=actor.can("batch.upload"),
    )


@router.get(DETAIL, response_class=HTMLResponse)
async def detail(
    request: Request,
    customer_id: str,
    batch_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The validation report — and, once every row is valid and it has been
    marked ready, the screen that states what dispatching would do."""
    batch = await batches.get(session, customer_id, batch_id)
    if batch is None:
        raise HTTPException(404, "No such batch for this customer.")
    rows = await batches.rows_of(session, batch.id)
    return render(
        request,
        "batches/detail.html",
        section="orders",
        customer_id=customer_id,
        batch=batch,
        rows=rows,
        tally=batches.tally(rows),
        purposes=batches.purpose_tally(rows),
        # The polling stop condition, computed here rather than in Jinja: the
        # fragment that carries the `hx-trigger` is the fragment that gets
        # replaced, so a run that has finished swaps in a fragment with no
        # trigger and the page stops asking (the house auto-refresh pattern —
        # `operations/detail.html`, `applications/_status.html`).
        # Poll only while something is actually moving. A run that could not
        # start writes its reason onto the rows it did not send (`batches._abort`)
        # rather than leaving them pending, so this condition also ends the
        # polling for the batch nobody is dispatching.
        polling=batch.status == "partially_dispatched"
        and any(row["state"] in ("pending", "sending") for row in rows),
        # The ruling, in the module that owns it rather than in the template: a
        # row Conduit refused keeps its operation and is corrected by a new
        # batch, never re-sent under the same identity.
        rejected_is_final=batches.REJECTED_IS_FINAL,
        results_csv=export_url(request, "batch_rows", customerId=customer_id, batchId=str(batch.id)),
        totals=batches.totals(rows, batch.asset),
        # What this batch still owes — the same derivation, off the same set, as
        # the confirm screen's `sending` and the dispatch route's audited
        # `pending`. Counting `pending` rows instead was the bug: `_abort` writes
        # a refusal onto every un-sent row, so a run that could not start leaves
        # a batch with nothing pending and everything owed, and the page said
        # "0 still to go" over it.
        sending=batches.totals(
            [row for row in rows if row["state"] in batches.SENDABLE_ROW_STATES], batch.asset
        ),
        # `m.attach_documents` reads exactly two things off a render model, and
        # a batch has no form to build one from — the widget is the same widget,
        # the chips are the same `documentIds` hidden inputs, and Jinja resolves
        # both names on a plain dict.
        rm={"document_ids": list(batch.document_ids or []), "document_errors": []},
        # The drift sentinel, read at render: the file said it was built from
        # one snapshot and the rows were validated against another.
        stale=batch.template_fingerprint != batch.fingerprint,
        document_purpose=payments.DOCUMENT_PURPOSE,
        can_act=actor.can("batch.upload"),
        can_dispatch=actor.can("batch.dispatch"),
    )


# --- the two state changes ---------------------------------------------------------------


@router.post(DETAIL + "/ready")
async def ready(
    request: Request,
    customer_id: str,
    batch_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("batch.upload")),
) -> Response:
    """Attach the batch's shared documents, and — unless the submit was the
    *save* button — mark it ready.

    Two buttons on one form because they are two halves of one decision, and a
    document attached but not saved would be a chip that disappears on reload.
    """
    back = DETAIL.format(customer_id=customer_id, batch_id=batch_id)
    batch = await batches.get(session, customer_id, batch_id)
    if batch is None:
        raise HTTPException(404, "No such batch for this customer.")
    if batch.status not in READY_STATES:
        return redirect(request, back, err=_unchangeable(batch.status))

    submitted = await form_items(request)
    ids = [value for name, value in submitted if name == "documentIds" and value.strip()]
    # A `doc_` id on this form is an assertion, exactly as it is on the payout
    # form: it must be a document this operator uploaded here for this purpose
    # (OPERATIONS_SPEC §3's attachment rule).
    if refused := await documents.unattachable(
        session, ids, purpose=payments.DOCUMENT_PURPOSE, actor_id=actor.id
    ):
        return redirect(
            request, back, err=documents.REFUSED_ATTACHMENT.format(count=len(refused))
        )
    batches.set_documents(batch, ids)
    audit.record(
        session,
        action="payout_batch.documents",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"customer": customer_id, "batch": str(batch.id), "documents": len(ids)},
    )

    if dict(submitted).get("save"):
        await session.commit()
        return redirect(request, back, msg="Attachments saved.")

    rows = await batches.rows_of(session, batch.id)
    invalid = sum(1 for row in rows if row["errors"])
    if invalid:
        await session.commit()  # the attachments still happened
        return redirect(
            request,
            back,
            err=f"{invalid} of {len(rows)} rows are invalid. Correct the file and upload it "
            "again — that is a new batch, and this one can be abandoned.",
        )
    if batch.documents_required and not ids:
        await session.commit()
        return redirect(request, back, err=payments.DOCUMENTATION_MESSAGE)

    if not await batches.set_status(session, batch, "ready"):
        # Abandoned out from under this click between its read and its write. The
        # attachments and their audit row go back with it, exactly as they never
        # happened at all when the check at the top of this route is the one that
        # catches this.
        # Read the status BEFORE rolling back. `set_status` refreshed it from
        # the table on its way to returning False, and a rollback expires a
        # refreshed attribute — so reading it afterwards attempts the reload as
        # IO outside the greenlet and raises `MissingGreenlet`, a 500 on exactly
        # the path this sentence exists to replace. `dispatch` and `abandon`
        # phrase the same refusal inline because neither of them rolls back.
        status = batch.status
        await session.rollback()
        return redirect(request, back, err=_unchangeable(status))
    audit.record(
        session,
        action="payout_batch.ready",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"customer": customer_id, "batch": str(batch.id), "rows": len(rows)},
    )
    await session.commit()
    return redirect(request, back, msg="Batch marked ready. Nothing has been sent.")


# --- the money moment: confirm, then dispatch ---------------------------------------------


@router.get(DETAIL + "/confirm", response_class=HTMLResponse)
async def confirm(
    request: Request,
    customer_id: str,
    batch_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("batch.dispatch")),
) -> Response:
    """The screen that states what dispatching does, and the **only** place the
    dispatch button exists.

    Operator-only for a reason a viewer would notice: this page is not a better
    report, it is the confirmation. Everything on it is derived — the totals from
    the validated rows, the exclusions named beside them, the funding account's
    own balance read from Conduit, the shared documents that will ride with every
    payout — so there is nothing here for an operator to take on trust from a
    previous screen.
    """
    batch = await batches.get(session, customer_id, batch_id)
    if batch is None:
        raise HTTPException(404, "No such batch for this customer.")
    back = DETAIL.format(customer_id=customer_id, batch_id=batch_id)
    if batch.status not in DISPATCHABLE:
        return redirect(
            request,
            back,
            err=f"This batch is {batch.status.replace('_', ' ')} — there is nothing to confirm.",
        )
    rows = await batches.rows_of(session, batch.id)
    available, _ = await payouts._funding(client, customer_id)
    account = next((a for a in available if a.get("id") == batch.virtual_account_id), None)
    # The drift sentinel again, live: discovery may have moved since these rows
    # were validated. It is a warning rather than a refusal — Conduit judges each
    # row on the way in, and a row it now refuses comes back as that row's own
    # rejection rather than as a batch this console has stranded. Over the whole
    # purpose set, because that is what the batch's fingerprint covers.
    snapshots, _missing = await _requirements(client, _batch_route(batch))
    return render(
        request,
        "batches/confirm.html",
        section="orders",
        customer_id=customer_id,
        batch=batch,
        rows=rows,
        tally=batches.tally(rows),
        purposes=batches.purpose_tally(rows),
        totals=batches.totals(rows, batch.asset),
        # What *this* click would send, which on a re-dispatch is not the whole
        # batch: a row that already has a payout is not sent again, so putting
        # the batch's total behind the button would be describing money that is
        # already gone.
        sending=batches.totals(
            [row for row in rows if row["state"] in batches.SENDABLE_ROW_STATES], batch.asset
        ),
        account=account,
        # `None` where the account could not be read: an unstated balance is
        # never a zero on this page of all pages (`accounts.balance_rows`).
        balances=accounts.balance_rows(account) if account else None,
        drifted=bool(snapshots) and batches.fingerprint(snapshots) != batch.fingerprint,
        unreadable_route=not snapshots,
        can_act=True,
    )


async def _run(client: ConduitClient, batch_id, customer_id: str, actor_id: str, actor_email: str) -> None:
    """The dispatch loop, off the request.

    Its own session: FastAPI has already closed the request's by the time a
    background task runs, and this loop commits per row on purpose (a crash at
    row 40 must leave 39 dispatched rows on the record).

    Every failure path leaves the batch `partially_dispatched`, which is both
    true and recoverable: dispatching again resolves every already-sent row to
    its existing operation through the intent nonce and picks up where this
    stopped.

    The one exit that is not a failure path and does not leave that status is the
    stand-down below: a run that finds the batch in any other status
    sends nothing and leaves the status alone, because whatever put it there is
    more recent than the click that scheduled this.
    """
    async with sessionmaker()() as session:
        try:
            batch = await batches.get(session, customer_id, str(batch_id))
            if batch is None:  # pragma: no cover - the route just read it
                return
            # **Re-read the authorisation, do not inherit it**. The route
            # decided this batch was dispatchable and committed
            # `partially_dispatched`; this task starts afterwards, in another
            # session, and the only status a dispatch may run under is the one
            # that click left behind. `abandoned` here means an abandon landed in
            # between, and a run that reads it and sends anyway puts a payment on
            # the wire for a batch the console has told an operator is closed —
            # and then raises out of `abandoned -> dispatched` on the way past,
            # taking this run's own audit row with it while the per-row commits
            # stay. `dispatched` means a concurrent run already finished the
            # batch, so there is nothing left to send either.
            #
            # The guarded UPDATE in `batches.set_status` is what stops that
            # abandon from landing at all; this is the belt to it, and it is
            # cheap — one status comparison on a row this task has already read.
            # A background task is the one caller that cannot be told "no" by a
            # route.
            if batch.status != "partially_dispatched":
                log.warning(
                    "batch %s: dispatch run stood down, status is %s", batch_id, batch.status
                )
                return
            result = await batches.dispatch(
                session, client, batch, actor_id=actor_id, actor_email=actor_email
            )
            rows = await batches.rows_of(session, batch.id)
            if not result["aborted"] and batches.complete(rows):
                # The return value is deliberately not branched on: losing this
                # one means a concurrent run of the same batch already wrote the
                # same `dispatched`, which is the answer this run was going to
                # give. `set_status` has re-read `batch.status` either way, so
                # the audit row below states what the table says rather than what
                # this run assumed.
                await batches.set_status(session, batch, "dispatched")
            audit.record(
                session,
                action="payout_batch.dispatched",
                actor_id=actor_id,
                actor_email=actor_email,
                detail={
                    "customer": customer_id,
                    "batch": str(batch.id),
                    "status": batch.status,
                    **{k: v for k, v in result.items() if v},
                },
            )
            await session.commit()
        except Exception:  # noqa: BLE001 — a background task may not die silently
            log.exception("batch %s: dispatch run failed", batch_id)
            await session.rollback()


@router.post(DETAIL + "/dispatch")
async def dispatch(
    request: Request,
    customer_id: str,
    batch_id: str,
    background: BackgroundTasks,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("batch.dispatch")),
) -> Response:
    """Send every valid row, once.

    The loop runs as a background task and the operator is redirected to the
    report, which polls itself while the batch is `partially_dispatched` — a
    200-row batch is minutes of paced work, and a request held open for it would
    make a browser timeout look like a failed batch.

    **Clicking this twice is not a risk this route has to manage**, and it
    deliberately does not pretend to: the second run resolves every row to the
    operation the first one made (`batches.intent_for`), so the guarantee lives
    in the ledger rather than in a flag some future edit could get wrong. The
    status check below is about telling the operator what is going on, not about
    safety.

    The `ready -> partially_dispatched` write is a different matter and IS a
    guard: the batch may have been abandoned between this request's read
    and that line, and `batches.set_status` refuses to write over a status this
    request did not decide on. Losing it ends the request before the background
    task is ever scheduled — a second dispatch is harmless, an abandoned batch
    dispatching is not.
    """
    back = DETAIL.format(customer_id=customer_id, batch_id=batch_id)
    batch = await batches.get(session, customer_id, batch_id)
    if batch is None:
        raise HTTPException(404, "No such batch for this customer.")
    if batch.status not in DISPATCHABLE:
        return redirect(request, back, err=_undispatchable(batch.status))
    rows = await batches.rows_of(session, batch.id)
    invalid = sum(1 for row in rows if row["errors"])
    if invalid:  # pragma: no cover - `ready` cannot be reached with one
        return redirect(request, back, err=f"{invalid} rows are invalid; this batch cannot be sent.")

    # `MONEY_CEILING` against the BATCH TOTAL, on the sum this
    # click would actually send — the same `sending` figure the confirm screen
    # states, not the whole batch, because a re-dispatch does not resend a row
    # that already has a payout. Per-row ceilings are `batches.dispatch`'s; a
    # batch of small rows that adds up past the ceiling is refused here, before
    # the status flips and before any row's operation exists.
    sending = batches.totals(
        [row for row in rows if row["state"] in batches.SENDABLE_ROW_STATES], batch.asset
    )
    if refusal := payments.over_ceiling(sending.by_currency.get(batch.asset)):
        return redirect(request, back, err=f"Batch total: {refusal}")

    if batch.status == "ready" and not await batches.set_status(
        session, batch, "partially_dispatched"
    ):
        # Somebody abandoned this batch between this request's read and this
        # line, so this click is not the one that gets to send it. Nothing has
        # been written yet and nothing goes on the wire: the background task is
        # never scheduled.
        return redirect(request, back, err=_undispatchable(batch.status))
    audit.record(
        session,
        action="payout_batch.dispatch",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={
            "customer": customer_id,
            "batch": str(batch.id),
            "rows": len(rows),
            # What is actually about to be attempted: a re-dispatch of a partial
            # batch attempts only what has no operation yet.
            "pending": sum(1 for row in rows if row["state"] in ("pending", "refused")),
        },
    )
    await session.commit()
    background.add_task(_run, client, batch.id, customer_id, actor.id, actor.email)
    return redirect(
        request,
        back,
        msg="Dispatching. Each row is sent once — this page follows it.",
    )


@router.post(DETAIL + "/abandon")
async def abandon(
    request: Request,
    customer_id: str,
    batch_id: str,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("batch.upload")),
) -> Response:
    """Close a batch out, **before** anything has been dispatched.

    Console-local and audited: a batch that has never dispatched created no
    payout, so there is nothing at Conduit to undo. Once dispatch has started
    this route refuses — abandoning would be this console claiming to have
    stopped payments it has already handed to Conduit (by ruling;
    `batches.LEGAL_STATUS` refuses the same transition one layer down).
    """
    back = DETAIL.format(customer_id=customer_id, batch_id=batch_id)
    batch = await batches.get(session, customer_id, batch_id)
    if batch is None:
        raise HTTPException(404, "No such batch for this customer.")
    if batch.status not in LIVE_STATES:
        return redirect(request, back, err=_unabandonable(batch.status))
    previous = batch.status
    if not await batches.set_status(session, batch, "abandoned"):
        # A dispatch of this batch started between this request's read and this
        # line. It is too late to abandon, and saying otherwise would be this
        # console claiming to have stopped payments it has already handed to
        # Conduit — the same claim the check above exists to refuse, from the
        # same status, in the same words.
        return redirect(request, back, err=_unabandonable(batch.status))
    audit.record(
        session,
        action="payout_batch.abandoned",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"customer": customer_id, "batch": str(batch.id), "was": previous},
    )
    await session.commit()
    return redirect(request, back, msg="Batch abandoned. Nothing was sent.")
