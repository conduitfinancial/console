"""Applications dashboard, RFIs, IDV links, and the operation status surface
(plan v2 §7, OPERATIONS_SPEC §5).

Lists and details read Conduit **live**, one cursor page at a time (plan v2 §4:
never eagerly walk cursors). The local `operations` row is what the operator
needs when Conduit's answer is not the whole story — a submission that never got
a definitive response is a row here long before it is an application there.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import audit, counterparties, documents, operations, projections, rfis
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Page, Problem, Success
from app.models import ACTIVE_STATES, Operation
from app.onboarding import drafts
from app.web import (
    ALREADY_SPENT_ELSEWHERE,
    conduit,
    db,
    form_items,
    intent_of,
    local_problem,
    page_size,
    pager,
    problem_of,
    problem_view,
    redirect,
    render,
    with_customer_names,
)

log = logging.getLogger(__name__)
router = APIRouter()

# The RFI routes live here for historical reasons — this was the only page with
# an RFI panel until the transaction page grew one. What both
# pages share is in `app/rfis.py` and `templates/rfis/_panel.html`; these two
# handlers are subject-agnostic and take the subject from the form.
ACK_ACTION = rfis.ACK_ACTION
APPLICATION_STATES = tuple(projections.STATE_RANKS["applications"])


# --- helpers -------------------------------------------------------------------------


async def _operation_for(session: AsyncSession, application_id: str) -> Operation | None:
    """The local row that produced this application, if this installation is the
    one that submitted it."""
    return (
        await session.execute(
            select(Operation)
            .where(Operation.conduit_resource_id == application_id)
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _operation_view(op: Operation | None) -> dict | None:
    """OPERATIONS_SPEC §5: what the operator is told, and what they may do."""
    if op is None:
        return None
    return {
        "op": op,
        "problem": problem_of(op.error, op.conduit_resource_id),
        # `outcome_unknown` never offers a retry button — the reconciler owns it.
        "can_retry": op.state == "stalled",
        "can_abandon": op.state == "stalled",
        "refresh": op.state in ("in_flight", "outcome_unknown"),
        # Unresolved: whatever this operation attempted may still be happening,
        # so the resource's own page shows this panel *instead of* offering the
        # same action again.
        "pending": op.state in ACTIVE_STATES,
    }


async def _application(client: ConduitClient, application_id: str):
    result = await client.get(f"/v2/applications/{application_id}")
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data, None
    if isinstance(result, Problem):
        return None, problem_view(result)
    return None, local_problem(
        "Conduit is unreachable",
        "The application could not be read just now.",
        "Refresh in a moment; nothing has changed on Conduit's side.",
        resource_id=application_id,
    )


# --- list ----------------------------------------------------------------------------


def list_query(query) -> dict:
    """This page's filters as the Conduit query they produce — paging aside.
    One parser for the page and for its CSV export (`app/web/exports.py`)."""
    return {
        "status": [v for v in query.getlist("status") if v] or None,
        "type": [v for v in query.getlist("type") if v] or None,
        "search": query.get("search") or None,
        "customerId": query.get("customerId") or None,
    }


@router.get("/applications", response_class=HTMLResponse)
async def index(
    request: Request,
    client: ConduitClient = Depends(conduit),
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    wire = list_query(request.query_params)
    status, types = wire["status"] or [], wire["type"] or []
    search, customer_id = wire["search"], wire["customerId"]
    limit = page_size(request.query_params)
    applications_read = client.page(
        "/v2/applications",
        cursor=request.query_params.get("cursor") or None,
        # Previous emits `direction=backward` and this route was dropping it, so
        # the back link re-read the same page forwards. Pre-existing; the
        # `pager` migration is what put a working Previous on the page.
        direction=request.query_params.get("direction") or None,
        limit=limit,
        **wire,
    )
    # On the sandbox, `search` matched a whole, case-exact `clientReferenceId`
    # and nothing name-shaped (probed 2026-08-29 — see the muted line on this
    # page). The pinned contract calls it free text, so that observation is
    # scoped to the environment it came from; either way `customerId` is the
    # reliable way to reach an application by *who it is for*. The customer link on every Customers row already sends this
    # parameter; the filter box and its name suggestions make it typeable.
    # The same bounded read that fills the name suggestions names the rows: an
    # application DTO carries `customerId` and no name at all, so the map is the
    # only source there is (misses render the bare id).
    page, customers = await with_customer_names(client, applications_read)
    problem = None
    if not isinstance(page, Page):
        problem = problem_view(page) if isinstance(page, Problem) else local_problem(
            "Conduit is unreachable", "The application list could not be read."
        )
        page = Page(items=[], next_cursor=None, prev_cursor=None, total=None)
    # After the Conduit read, never gathered with it: these are local rows on
    # the request's own session (`with_customer_names.no_second_read`). One
    # statement for the page — the legal entity and the operator's own
    # reference, neither of which an application DTO carries.
    notes = await operations.local_notes(session, [str(i.get("id") or "") for i in page.items])
    return render(
        request,
        "applications/list.html",
        section="applications",
        page=page,
        pager=pager(
            "/applications",
            {"search": search, "status": status, "type": types, "customerId": customer_id},
            page,
            size=limit,
        ),
        problem=problem,
        statuses=APPLICATION_STATES,
        # No hardcoded type vocabulary: offer what this page actually contains,
        # plus whatever is already filtered on.
        types=sorted({str(i.get("type")) for i in page.items if i.get("type")} | set(types)),
        selected_status=status,
        selected_types=types,
        search=search or "",
        customer_id=customer_id or "",
        customers=customers.items,
        customers_more=customers.more,
        names=customers.names,
        notes=notes,
        limit=limit,
    )


# --- detail --------------------------------------------------------------------------


async def _detail_context(
    request: Request,
    application_id: str,
    session: AsyncSession,
    client: ConduitClient,
    actor: Actor,
) -> dict:
    application, problem = await _application(client, application_id)
    op = await _operation_for(session, application_id)
    return {
        "section": "applications",
        "application_id": application_id,
        "application": application,
        "problem": problem,
        "operation": _operation_view(op),
        "terminal": projections.is_terminal(
            "applications", (application or {}).get("status")
        ),
        # What was actually sent, for the operator who may already see and change
        # onboarding answers — `onboarding.edit`, the same gate the draft itself
        # is behind. A viewer keeps the page they had: this body carries tax ids,
        # birth dates and addresses, and a read-only role is not a reason to
        # widen who sees them. Gone once retention drops the body
        # (`OP_BODY_RETENTION`), which the panel says out loud rather than
        # rendering an empty section.
        "submitted": op.request_body if op and actor.can("onboarding.edit") else None,
        "submitted_purged": bool(op and op.request_body is None and actor.can("onboarding.edit")),
        "can_idv": actor.can("onboarding.idv_link"),
        "can_resubmit": actor.can("onboarding.edit"),
        "can_simulate": actor.can("sandbox.simulate"),
        "can_rfi": actor.can("rfi.respond"),
        "can_retry": actor.can("operation.retry"),
        "can_abandon": actor.can("operation.abandon"),
    }


@router.get("/applications/{application_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    application_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Read-only, like every GET.

    Acknowledgement used to happen here, which made loading a page — a
    prefetch, a refresh, a crawler following a link — send a mutation to
    Conduit with no CSRF token in sight. The panel now marks each unacknowledged
    open RFI, and the template fires the explicit POST below on render.
    """
    context = await _detail_context(request, application_id, session, client, actor)
    found, rfis_ok = await rfis.for_subject(client, "application", application_id)
    return render(
        request,
        "applications/detail.html",
        rfis=await rfis.mark_unacknowledged(session, found),
        # "Conduit has not asked for anything" and "we could not ask" are
        # different facts, and only one of them is ours to state (pass B, P2).
        rfis_ok=rfis_ok,
        actor_email=actor.email,
        **context,
    )


@router.get("/applications/{application_id}/quick", response_class=HTMLResponse)
async def quick_view(
    request: Request,
    application_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The list's quick-view drawer body (DESIGN_DIRECTION "Detail drawer").

    **Budget.** Two Conduit reads, gathered, on demand — the same shape and the
    same cost as opening the application's own page, and none of it on the
    list's render: this route is only ever hit by a click. It is deliberately
    NOT fed from the list's context. The list already holds a `names` map, but
    the only way to get it here would be to put a customer's legal name into a
    query string — into a URL, a proxy log and a browser history — to save a
    call the detail pages already pay. `with_customer_names` is the console's
    one bounded resolver (25 customers, gathered never serialised, silent on
    failure), so an id past that page renders as the bare id here exactly as it
    does on the list.

    **No database.** No operation panel, no RFI panel: those are the detail
    page's, and a drawer that grew them would be a second detail page to keep in
    step with the first. Read-only for both roles — the detail page's own gate
    is `require("console.view")` and there is nothing narrower to be here.

    **Always 200.** An unreachable Conduit or a problem-detail is rendered
    *inside* the drawer (`_application` already returns the view for both), so
    the operator gets the house error card where the facts would have been
    rather than a click that does nothing. The statuses this route can still
    answer with are the middleware's — 401/403 — and a 5xx from a bug; htmx's
    `responseHandling` (base.html) drops those without a swap, so `static/app.js`
    renders them into the drawer itself.
    """
    result, customers = await with_customer_names(client, _application(client, application_id))
    application, problem = result
    return render(
        request,
        "applications/_quick.html",
        application_id=application_id,
        application=application,
        problem=problem,
        names=customers.names,
    )


@router.post("/rfis/{rfi_id}/acknowledge", response_class=HTMLResponse)
async def acknowledge(
    rfi_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("rfi.respond")),
) -> Response:
    """"We have seen this RFI" — fired by the panel on first render.

    A POST, so it carries the CSRF header and cannot be triggered by anything
    that merely loads a URL. Outside the operations ledger by the rule in
    `app.web`: 204, creates nothing, safely repeatable.
    """
    if await rfis.acknowledged(session, rfi_id):
        return HTMLResponse("")
    result = await client.mutate("POST", f"/v2/rfis/{rfi_id}/acknowledge")
    if not isinstance(result, Success):
        log.warning("rfi %s: acknowledge failed, will retry on next view", rfi_id)
        return HTMLResponse("")
    audit.record(
        session,
        action=ACK_ACTION,
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"rfi": rfi_id},
    )
    await session.commit()
    return HTMLResponse('<span class="muted">acknowledged</span>')


@router.get("/applications/{application_id}/status", response_class=HTMLResponse)
async def status_fragment(
    request: Request,
    application_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """The auto-refreshing half of the detail page. The returned fragment carries
    the `every 15s` trigger only while the application is non-terminal, so the
    polling stops by itself the moment it settles."""
    context = await _detail_context(request, application_id, session, client, actor)
    return render(request, "applications/_status.html", **context)


# --- rejection correction ------------------------------------------------------------


@router.post("/applications/{application_id}/resubmit", response_class=HTMLResponse)
async def resubmit(
    request: Request,
    application_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    """Re-open the draft behind a rejected application (plan v2 §7).

    Same draft, same `client_reference_id`: the corrected body is a different
    request hash, so it becomes a new operation with a new idempotency key while
    the two attempts stay recognisable as one application.
    """
    # Fail closed. Correcting an application is only meaningful when Conduit has
    # actually rejected it *and* said a corrected one would be considered — so
    # the live read has to succeed and say exactly that. Checking only for
    # `resubmittable is False` let an approved, pending, missing or unreadable
    # application through, and re-opened a draft whose answers Conduit had
    # already accepted.
    application, problem = await _application(client, application_id)
    refusal = None
    if application is None:
        refusal = (
            f"The application could not be read "
            f"({(problem or {}).get('title', 'unavailable')})."
        )
    elif application.get("status") != "rejected":
        refusal = (
            "Only a rejected application can be corrected "
            f"(this one is {application.get('status') or 'unknown'})."
        )
    elif application.get("resubmittable") is not True:
        refusal = "Conduit has not said a corrected application would be considered."
    if refusal:
        return redirect(request, f"/applications/{application_id}", err=refusal)

    op = await _operation_for(session, application_id)
    draft = await drafts.load(session, op.draft_id) if op and op.draft_id else None
    # Same ownership rule as every other draft surface (`web.onboarding._draft`):
    # re-opening someone else's draft through an application id is the same
    # read, one redirect later.
    if draft is not None and not (draft.actor_id == actor.id or actor.can("onboarding.access_any")):
        draft = None
    if draft is None:
        return redirect(
            request,
            f"/applications/{application_id}",
            err="No local draft for this application: it was submitted elsewhere.",
        )
    if draft.payload is None:
        # Retention already took the answers (drafts §purge_settled). The pinned
        # snapshot survives, so the operator re-answers the same questionnaire
        # rather than starting from a fresh discovery fetch.
        fresh = await drafts.create(
            session,
            kind="onboarding",
            actor_id=actor.id,
            requirements_snapshot=draft.requirements_snapshot,
            country=draft.country,
            client_reference_id=draft.client_reference_id,
        )
        return redirect(
            request,
            f"/onboarding/{fresh.id}",
            err="The submitted answers were purged by retention; re-enter them against the "
            "same pinned requirements.",
        )
    return redirect(request, f"/onboarding/{draft.id}", msg="Correct the application and submit again.")


# --- IDV -----------------------------------------------------------------------------


@router.post("/applications/{application_id}/persons/{reference_id}/idv-link")
async def idv_link(
    request: Request,
    application_id: str,
    reference_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("onboarding.idv_link")),
) -> Response:
    """Fetch one person's identity-verification link.

    Outside the ledger (see `app.web`): it creates no resource we own, and the
    URL is a bearer credential the spec tells us not to persist or log — so it
    is rendered once, to the operator who asked, and stored nowhere.
    """
    result = await client.mutate(
        "POST", f"/v2/applications/{application_id}/persons/{reference_id}/idv-link"
    )
    if isinstance(result, Success) and isinstance(result.data, dict):
        return render(
            request, "applications/_idv.html", reference_id=reference_id, url=result.data.get("url")
        )
    if isinstance(result, Problem) and result.status == 404:
        return render(
            request,
            "applications/_idv.html",
            reference_id=reference_id,
            note="Not ready yet — Conduit has not opened this person's verification. "
            "Try again shortly.",
        )
    if isinstance(result, Problem) and result.status == 409:
        return render(
            request,
            "applications/_idv.html",
            reference_id=reference_id,
            note="This person's verification is already settled; no link is needed.",
        )
    return render(
        request,
        "applications/_idv.html",
        reference_id=reference_id,
        problem=problem_view(result)
        if isinstance(result, Problem)
        else local_problem("Conduit is unreachable", "The link could not be requested."),
    )


# --- RFIs ----------------------------------------------------------------------------


@router.post("/rfis/{rfi_id}/respond")
async def respond(
    request: Request,
    rfi_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("rfi.respond")),
) -> Response:
    """Answer one round, from whichever subject page the operator is on.

    The subject is named by kind + id and the path is rebuilt from the fixed map
    in `app/rfis.py`, so the form cannot post a redirect target of its own
    choosing. A subject this console has no page for (or a missing one) lands
    back on the global index, which does list the RFI.
    """
    fields = await form_items(request)
    values = dict(fields)
    message = (values.get("message") or "").strip()
    back = (
        rfis.subject_url(values.get("subjectType") or "", values.get("subject") or "") or "/rfis"
    )
    if not message:
        return redirect(request, back, err="An RFI response needs a message.")
    # `submittedBy.email` is Conduit's compliance attribution for this answer, and
    # this console's own ledger (`actor_email` below, and the audit rows that
    # survive the body purge) already names the real actor. The form field is
    # only ever pre-filled with the actor's own address — pinning here, rather
    # than trusting whatever the field carries, means the two records can never
    # name different people.
    body: dict = {
        "message": message,
        "submittedBy": {"email": actor.email},
    }
    if actor.display_name:
        body["submittedBy"]["name"] = actor.display_name
    # A `doc_` id on a form is an assertion. Every one of them is resolved
    # against this console's own upload ledger before it can be published to
    # Conduit as part of a compliance answer — see `documents.attachable` for
    # what the rule is and why uploader-actor is the tightest honest one.
    # `[:20]` is `PublicRespondRfiBodyDto`'s `maxItems`.
    document_ids = [v for name, v in fields if name == "documentIds" and v][:20]
    if document_ids:
        if refused := await documents.unattachable(
            session, document_ids, purpose="rfi_response", actor_id=actor.id
        ):
            log.warning(
                "rfi %s: refused %s unattachable document id(s) from %s",
                rfi_id,
                len(refused),
                actor.email,
            )
            return redirect(
                request, back, err=documents.REFUSED_ATTACHMENT.format(count=len(refused))
            )
        body["documentIds"] = document_ids

    path = f"/v2/rfis/{rfi_id}/responses"
    op, is_new = await operations.start(
        session,
        type="rfi_respond",
        actor_id=actor.id,
        actor_email=actor.email,
        path=path,
        body=body,
        intent=intent_of(values),
    )
    # **A spent nonce replayed at a different RFI, or with a rewritten message**
    #. The nonce alone resolves, and it is scoped to the operation *type*,
    # so a token spent answering one RFI answered this submit against another —
    # and the "Response sent." below then said so about an RFI still waiting,
    # whose deadline runs out while the operator believes it is answered. Both
    # the RFI id (in the path) and the message (in the body) are inside
    # `request_hash`, so one comparison covers both replays.
    if not is_new and operations.resolved_elsewhere(op, path, body):
        return redirect(request, f"/operations/{op.id}", msg=ALREADY_SPENT_ELSEWHERE)
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed":
        return redirect(request, back, msg="Response sent.")
    return redirect(request, f"/operations/{op.id}")


# --- operations (OPERATIONS_SPEC §5) -------------------------------------------------


async def _operation(session: AsyncSession, op_id: uuid.UUID) -> Operation:
    op = await session.get(Operation, op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="operation not found")
    return op


@router.get("/operations/{op_id}", response_class=HTMLResponse)
async def operation_detail(
    request: Request,
    op_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    op = await _operation(session, op_id)
    view = _operation_view(op)
    # Same audit read the transaction page's copy of this panel does
    # (`transactions._linked_operation`): the panel has a Counterparty
    # row and this page was the one place rendering it that never filled it in,
    # so a payout addressed from a saved destination lost that fact the moment
    # the operator followed the operation link. `""` renders no row.
    view["counterparty"] = await counterparties.attached(session, op.id)
    return render(
        request,
        "operations/detail.html",
        section="applications",
        can_retry=actor.can("operation.retry"),
        can_abandon=actor.can("operation.abandon"),
        operation=view,
    )


@router.post("/operations/{op_id}/retry")
async def operation_retry(
    request: Request,
    op_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("operation.retry")),
) -> Response:
    """The audited `stalled → in_flight` retry: same row, same idempotency key."""
    op = await _operation(session, op_id)
    if op.state != "stalled":
        return redirect(request, f"/operations/{op_id}", err="Only a stalled operation can be retried.")
    await execute_operation(session, op, client=client, actor_id=actor.id, actor_email=actor.email)
    return redirect(request, f"/operations/{op_id}", msg="Retried.")


@router.post("/operations/{op_id}/abandon")
async def operation_abandon(
    request: Request,
    op_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("operation.abandon")),
) -> Response:
    """The §1 release valve — admin only, audited by the transition itself."""
    op = await _operation(session, op_id)
    if op.state != "stalled":
        return redirect(
            request, f"/operations/{op_id}", err="Only a stalled operation can be abandoned."
        )
    await operations.transition(
        session,
        op_id,
        "abandoned",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={"reason": "operator_abandoned"},
    )
    return redirect(request, f"/operations/{op_id}", msg="Marked abandoned.")
