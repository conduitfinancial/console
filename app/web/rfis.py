"""The global RFI index (plan v2 §7).

Every other RFI surface in this console is scoped to one subject: the
application detail page reads
`/v2/rfis?subjectType=application&subjectId=…` and renders the panel that
acknowledges and responds. That answers *"what is Conduit asking about this
application"*. It never answers *"what is Conduit asking, at all"* — and an
RFI's `dueAt` is the one deadline in this app that nothing else is holding.

This page is that second question and nothing else: a read-only single cursor
page across every subject, defaulting to the two statuses that still owe
Conduit an answer. **It has no respond form.** A response is a ledgered
mutation that belongs to the subject it is about (`applications.respond`
carries the subject and redirects back to it), so every row here links
to the subject page where that form already lives — one respond UI, not two
that could drift apart. That is literally one panel
(`templates/rfis/_panel.html`) rendered by two subject pages.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from starlette.responses import HTMLResponse, Response

from app import rfis
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem
from app.web import (
    conduit,
    local_problem,
    page_size,
    pager,
    problem_view,
    render,
    with_customer_names,
)

router = APIRouter()

# What "still owes Conduit an answer" means, and what the toggle adds.
#
# `draft` is deliberately in neither tuple: "Draft RFIs are never visible on
# this surface" (the pinned `GET /v2/rfis` description), so asking for them
# would be asking for rows Conduit will not send. `PILL_TONES["rfis"]` still
# knows the word, because a status that reached the console anyway must render
# as itself rather than as `Unknown: draft`.
OPEN_STATUSES = ("open", "responded")
SETTLED_STATUSES = ("resolved", "cancelled")

# The subject vocabulary lives in `app/rfis.py` — three web modules share it.
SUBJECT_TYPES = rfis.SUBJECT_TYPES


def _subjects(rfi: dict) -> list[dict]:
    """`subjects[]` → `[{kind, id, url}]`.

    An RFI carries a *list* of subjects, not one (`ClientRfiPaginatedResponseDto`),
    so the column renders all of them. `url` is empty for a kind this console
    has no page for, and the template renders that as plain text.
    """
    rows = []
    for subject in rfi.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        kind = str(subject.get("subjectType") or "")
        subject_id = str(subject.get("subjectId") or "")
        if not subject_id:
            continue
        rows.append(
            {
                "kind": kind or "unknown",
                "id": subject_id,
                "url": rfis.subject_url(kind, subject_id),
            }
        )
    return rows


def _overdue(rfi: dict, now: datetime) -> bool:
    """Past `dueAt` and still owed.

    A resolved or cancelled RFI is not late, it is finished. A date this
    console cannot parse is not asserted to be either — the same rule the `ts`
    filter follows when it renders an unrecognised stamp verbatim rather than
    guessing at it.
    """
    if rfi.get("status") not in OPEN_STATUSES:
        return False
    raw = rfi.get("dueAt")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (due if due.tzinfo else due.replace(tzinfo=UTC)) < now


def list_query(query) -> dict:
    """This page's filters as the Conduit query they produce — paging aside.
    One parser for the page and for its CSV export (`app/web/exports.py`)."""
    subject_type = query.get("subjectType") or ""
    if subject_type not in SUBJECT_TYPES:
        # A value outside the enum is a 400 from Conduit. A typo in a URL
        # should show the unfiltered page, not an error page.
        subject_type = ""
    settled = query.get("include") == "settled"
    return {
        "status": list(OPEN_STATUSES) + (list(SETTLED_STATUSES) if settled else []),
        "subjectType": subject_type or None,
    }


@router.get("/rfis", response_class=HTMLResponse)
async def index(
    request: Request,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """Read-only, viewer-visible: nothing on this page mutates."""
    query = request.query_params
    wire = list_query(query)
    subject_type = wire["subjectType"] or ""
    # The toggle's own state, for rendering it — `list_query` has already turned
    # it into the statuses that go on the wire.
    settled = query.get("include") == "settled"
    limit = page_size(query)
    rfis_read = client.page(
        "/v2/rfis",
        cursor=query.get("cursor") or None,
        direction=query.get("direction") or None,
        limit=limit,
        **wire,
    )
    # `customer` is one of the four `subjectType`s (`rfis.SUBJECT_TYPES`), and a
    # subject cell that says only `cus_…` makes the operator open the row to
    # find out who Conduit is asking about. One bounded, gathered, silent-on-
    # failure customers read names them — the same resolver the two other
    # Conduit-backed lists use. The other three kinds (application, transaction,
    # organization) have no name to resolve and stay id-only, honestly.
    page, customers = await with_customer_names(client, rfis_read)
    problem = None
    if not isinstance(page, Page):
        problem = (
            problem_view(page)
            if isinstance(page, Problem)
            else local_problem("Conduit is unreachable", "The RFI list could not be read.")
        )
        page = Page(items=[], next_cursor=None, prev_cursor=None, total=None)
    now = datetime.now(UTC)
    rows = []
    for rfi in page.items:
        subjects = _subjects(rfi)
        rows.append(
            {
                "rfi": rfi,
                "subjects": subjects,
                # Where the respond flow for this RFI lives: the first subject
                # this console has a page for.
                "target": next((s["url"] for s in subjects if s["url"]), ""),
                "overdue": _overdue(rfi, now),
            }
        )
    return render(
        request,
        "rfis/list.html",
        section="rfis",
        page=page,
        pager=pager(
            "/rfis",
            {"include": "settled" if settled else "", "subjectType": subject_type},
            page,
            size=limit,
        ),
        problem=problem,
        rows=rows,
        settled=settled,
        subject_type=subject_type,
        subject_types=SUBJECT_TYPES,
        names=customers.names,
        limit=limit,
    )
