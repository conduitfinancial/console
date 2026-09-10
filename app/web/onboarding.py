"""The onboarding wizard (plan v2 §7).

Country → a pinned requirements snapshot → one long grouped form → review →
`POST /v2/onboarding` through the operations ledger.

Two rules shape every route here:

* the draft's own snapshot is what gets rendered and validated, always
  (`drafts.model`) — never a fresh discovery fetch mid-form;
* the server's `forms` run is the gate. `static/conditions.js` decides what the
  operator *sees*; `forms.validate` / `forms.assemble` decide what is submitted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from markupsafe import escape
from sqlalchemy import LargeBinary, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import documents, forms, operations
from app.auth.actor import Actor
from app.auth.tokens import seal, unseal
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, FieldError, Problem
from app.models import Draft, Operation
from app.onboarding import drafts, requirements
from app.webhooks.inbox import read_capped
from app.web import (
    conduit,
    db,
    form_items,
    intent_of,
    offset_of,
    offset_pager,
    page_size,
    problem_note,
    problem_of,
    problem_view,
    redirect,
    reference_of,
    render,
)
from app.web.countries import resolve_country

router = APIRouter()

KIND = "onboarding"
PATH = "/v2/onboarding"
PURPOSE = "organization_onboarding"
PERSON_PURPOSE = "kyc"


# --- draft payload <-> FormValues ----------------------------------------------------


def dump(values: forms.FormValues) -> dict:
    return {
        "root": values.root,
        "persons": [
            {"role": p.role, "values": p.values, "document_ids": p.document_ids}
            for p in values.persons
        ],
        "document_ids": values.document_ids,
    }


def load(payload: dict | None) -> forms.FormValues:
    payload = payload or {}
    return forms.FormValues(
        root=payload.get("root") or {},
        persons=[
            forms.PersonValues(
                values=p.get("values") or {},
                role=str(p.get("role") or ""),
                document_ids=list(p.get("document_ids") or []),
            )
            for p in payload.get("persons") or []
        ],
        document_ids=list(payload.get("document_ids") or []),
    )


def seed(model: forms.FormModel) -> forms.FormValues:
    """A fresh draft starts with the minimum cast of people the country asks
    for, so the form shows what has to be answered instead of an empty page."""
    return forms.FormValues(
        persons=[
            forms.PersonValues(role=row.role)
            for row in model.persons
            for _ in range(max(row.min_count, 0))
        ]
    )


def field_errors(error: dict | None) -> list[FieldError]:
    """`operations.error` (a stored `ValidationErrorDto`) → what the engine's
    mapper consumes."""
    return [
        FieldError(
            pointer=str(e.get("pointer") or ""),
            detail=str(e.get("detail") or ""),
            category=e.get("category"),
            allowed_values=list(e.get("allowedValues") or []),
        )
        for e in (error or {}).get("errors") or []
        if isinstance(e, dict)
    ]


# --- what the reviewer read ---------------------------------------------------
#
# The review page renders the stored draft; submit re-reads the stored draft and
# sends it. Nothing used to connect the two, and saves are last-write-wins — so
# anybody holding `onboarding.edit` on that draft could change the answers in the
# window between the reviewer reading the page and pressing the button, and the
# new values went to Conduit under the *reviewer's* actor id with no record that
# what was sent differed from what was read. Minutes wide, not milliseconds.
#
# So the review render seals a hash of what it rendered, the form carries it back,
# and submit recomputes the hash from the stored draft. The seal is what binds the
# two reads together; it is not a lock, and it does not try to be one — an editor
# may still save whenever they like. What they cannot do any more is have that
# save ride out under a review it was never part of.


# How long a review render stays submittable. **Deliberately generous**, for the
# same reason `INTENT_TTL_SECONDS` is: an expired seal costs the reviewer
# their submit, and reading a long onboarding draft carefully — a person at a
# time, against the documents — is the normal case here, not the slow one. The
# TTL is not what bounds anything: the hash does. A seal that outlives its
# usefulness stops matching the moment the draft moves.
REVIEWED_TTL_SECONDS = 86_400


def payload_digest(values: forms.FormValues) -> str:
    """sha256 over the draft's answers, canonically serialised.

    The serialisation is `operations.request_hash`'s, verbatim — key-sorted, no
    whitespace — so two runs over the same answers hash alike no matter what
    order the keys came back out of the database in. It is deliberately *not* a
    call into `request_hash` itself: that function salts with a path and a scope
    because it is the ledger's duplicate guard, and borrowing it would tie this
    question ("are these the same answers?") to the shape of an idempotency key.

    `dump(values)` rather than `draft.payload` on purpose: both sides of the
    comparison then hash the same normalisation, so a payload that round-trips
    through `load`/`dump` differently from how it was stored — an older draft
    missing a key `dump` always writes, say — does not read as a change nobody
    made.
    """
    canonical = json.dumps(dump(values), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seal_reviewed(draft: Draft, values: forms.FormValues) -> str:
    """What this render put in front of the reviewer, signed for the round trip.

    The draft id is in the payload as well as the hash. Without it a seal earned
    on one draft's review would authorise a submit of any *other* draft whose
    answers happened to hash the same — two drafts seeded from one country and
    filled in from one template are not a hypothetical in this console.
    """
    return seal(
        {"draft": str(draft.id), "reviewed": payload_digest(values)},
        ttl=REVIEWED_TTL_SECONDS,
    )


def reviewed_matches(token: str | None, draft: Draft, values: forms.FormValues) -> bool:
    """Is `token` this console's seal for exactly these answers on this draft?

    Absent, unreadable and expired all answer False, the same as a stale hash,
    and for the same reason: treating a token that will not unseal as "no token"
    and carrying on hands the forger precisely the behaviour they were reaching
    for. There is no fallback path to fall back to here anyway;
    the only way to hold a valid seal is to have been shown the review page.
    """
    payload = unseal(token) if token else None
    if not isinstance(payload, dict) or payload.get("draft") != str(draft.id):
        return False
    # Encoded before comparing: `compare_digest` raises on a non-ascii `str`, and
    # the sealed value is whatever was in the token's JSON. Only this app can
    # mint one, so it is always a hex digest — but a guard that can raise is not
    # a guard, and the exception would surface as a 500 rather than a refusal.
    return hmac.compare_digest(
        str(payload.get("reviewed") or "").encode("utf-8", "replace"),
        payload_digest(values).encode(),
    )


def _without_person_documents(body: Mapping) -> dict:
    ownership = body.get("ownership")
    if not isinstance(ownership, Mapping) or not ownership.get("persons"):
        return dict(body)
    persons = [
        {k: v for k, v in person.items() if k != "documentIds"}
        for person in ownership["persons"]
    ]
    return {**body, "ownership": {**ownership, "persons": persons}}


# --- loading -------------------------------------------------------------------------


def _uuid(value: str | None) -> uuid.UUID:
    try:
        return uuid.UUID(value or "")
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None


async def _draft(session: AsyncSession, draft_id: uuid.UUID, actor: Actor) -> Draft:
    """The actor's own draft, or 404.

    A draft holds a customer's unsubmitted identity documents and answers; a
    draft id in someone else's URL is not authorization to read or edit it. The
    refusal is a 404 rather than a 403 on purpose — a 403 would confirm the id
    exists. Admins are the explicit exception: they already hold the operation
    release valve, and someone has to be able to pick up a colleague's draft.
    """
    draft = await drafts.load(session, draft_id)
    if draft is None or not (draft.actor_id == actor.id or actor.can("onboarding.access_any")):
        raise HTTPException(status_code=404, detail="draft not found")
    return draft


async def _save(session: AsyncSession, draft: Draft, request: Request) -> forms.FormValues:
    """Parse the posted form against the draft's own model and store it."""
    model = drafts.model(draft)
    values = forms.parse_submission(model, await form_items(request))
    await drafts.update_payload(session, draft, dump(values))
    return values


def _person_rows(model: forms.FormModel, values: forms.FormValues) -> list[dict]:
    """Add/remove affordances, per `individualRequirements` row (spec §8)."""
    rows = []
    for row in model.persons:
        # `forms.satisfies`, not card membership: the same rule the validator
        # counts by (`forms.validate` §6), so the affordance cannot ask for a
        # person the submission does not need. One person answering
        # `roles: [BENEFICIAL_OWNER, CONTROLLING_PERSON, LEGAL_REPRESENTATIVE]`
        # satisfies all three rows at once — counting the card they were typed
        # into showed "0 of 1" on the other two and offered an Add button for
        # people the form would have accepted without.
        count = sum(1 for p in values.persons if forms.satisfies(row, p, model))
        rows.append(
            {
                "role": row.role,
                "min_count": row.min_count,
                "max_count": row.max_count,
                "count": count,
                "can_add": row.max_count is None or count < row.max_count,
                "can_remove": count > row.min_count,
            }
        )
    return rows


def _context(
    request: Request,
    draft: Draft,
    values: forms.FormValues,
    errors: forms.FormErrors | None = None,
    actor: Actor | None = None,
    **extra,
) -> dict:
    model = drafts.model(draft)
    return {
        "section": "drafts",
        "draft": draft,
        "model": model,
        "rm": forms.render_model(model, values, errors),
        "person_rows": _person_rows(model, values),
        "can_edit": bool(actor and actor.can("onboarding.edit")),
        # Filling a draft in and sending it to Conduit are separate permissions
        # — the review page's one button is the second of them.
        "can_submit": bool(actor and actor.can("onboarding.submit")),
        "purpose": PURPOSE,
        "person_purpose": PERSON_PURPOSE,
        **extra,
    }


# --- routes --------------------------------------------------------------------------


# What the state filter may ask for, and what each one means in SQL. `""` is the
# unfiltered page and is what an unknown value falls back to — the house rule for
# an out-of-vocabulary query parameter (`web/rfis.py`, `web/accounts.py`).
DRAFT_STATES = {
    "open": Draft.submitted_at.is_(None),
    "submitted": Draft.submitted_at.is_not(None),
}


@router.get("/drafts", response_class=HTMLResponse)
async def draft_list(
    request: Request,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    # Columns, not entities. `Draft.payload` is `EncryptedJSON`, so loading the
    # whole row would run its result processor *inside the query* — and one
    # corrupt ciphertext, or a key rotated away from under the rows, would then
    # 500 this page. During a key incident this list is exactly where an
    # operator goes to see what survived, so it must not need the key.
    # `drafts.list_for_actor(include_submitted=True)`'s filter verbatim: this
    # actor's own, submitted included, most recently touched first.
    #
    # The payload is now selected as a raw `LargeBinary` blob and decrypted per
    # row by `drafts.name_of` — the tolerant pattern `app/counterparties.py`
    # uses — so the applicant's legal name can head each row (names over ids)
    # while an unreadable row costs only its own name. No plaintext column and
    # no schema change: the name is read at render time and never stored.
    #
    # The state filter exists because the Overview's "Open drafts" tile links
    # here: that tile counts this actor's *unsubmitted* drafts, and a link that
    # landed on the unfiltered page would show submitted and purged ones too —
    # more rows than the number the operator just read (the tiles-are-
    # links rule). `?state=open` is exactly the tile's own predicate.
    #
    # **Paged**. This query had no limit at all: every draft
    # this actor ever started, each one costing a Fernet decrypt for its name.
    # That is the accounts page's finding one surface over — a list with no
    # bottom — and it is the house offset pager here for the same reason it is
    # there, `?state=` carried across the turn.
    state = request.query_params.get("state") or ""
    state = state if state in DRAFT_STATES else ""
    size, offset = page_size(request.query_params), offset_of(request.query_params)
    query = select(
        Draft.id,
        Draft.country,
        Draft.client_reference_id,
        Draft.updated_at,
        Draft.submitted_at,
        Draft.purged_at,
        type_coerce(Draft.payload, LargeBinary).label("blob"),
    ).where(Draft.actor_id == actor.id)
    if state:
        query = query.where(DRAFT_STATES[state])
    # `Draft.id` breaks ties: two drafts saved in the same instant must not swap
    # places between page one and page two, which is how offset paging loses a
    # row. One extra row is how the pager knows there is a Next.
    listed = (
        await session.execute(
            query.order_by(Draft.updated_at.desc(), Draft.id).limit(size + 1).offset(offset)
        )
    ).all()
    return render(
        request,
        "drafts.html",
        section="drafts",
        state=state,
        pager=offset_pager(
            "/drafts", {"state": state}, offset=offset, size=size, more=len(listed) > size
        ),
        drafts=[
            {
                "id": row.id,
                "country": row.country,
                "reference": row.client_reference_id,
                "updated_at": row.updated_at,
                "submitted_at": row.submitted_at,
                "purged_at": row.purged_at,
                "name": drafts.name_of(row.blob),
            }
            # The sentinel row is counted, never rendered — decrypting a name
            # nobody is going to see would spend the budget the cap exists for.
            for row in listed[:size]
        ],
        can_edit=actor.can("onboarding.edit"),
    )


@router.get("/onboarding", response_class=HTMLResponse)
async def start(request: Request, actor: Actor = Depends(require("console.view"))) -> Response:
    return render(
        request,
        "onboarding/start.html",
        section="drafts",
        reference="",
        can_edit=actor.can("onboarding.edit"),
    )


@router.post("/onboarding", response_class=HTMLResponse)
async def create(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    submitted = dict(await form_items(request))
    # `resolve_country` first, so "Bulgaria" reaches discovery as `BGR`. An
    # exact full name only: anything else — a code, a typo, a country this table
    # does not carry — is passed to Conduit exactly as typed, and Conduit's
    # `COUNTRY_NOT_SUPPORTED` is what the operator reads (the `.upper()` is the
    # pre-existing behaviour for codes, unchanged).
    country = resolve_country(submitted.get("country", "")).strip().upper()
    # The operator's own note for this application. It is
    # collected here, on the one screen that exists before the draft does, and
    # stored on `Draft.client_reference_id` — the column /drafts and the
    # dashboard have been rendering all along with nothing ever writing to it.
    # It is NOT sent to Conduit: `forms.assemble` only ever puts a
    # `clientReferenceId` in the body from `FormValues.client_reference_id`,
    # which the wizard's dump/load round trip never carries, and
    # `execute.outbound_body` overwrites that key with `op.id` regardless. See
    # `submit` for the other half.
    reference = reference_of(submitted)
    if not country:
        return render(
            request,
            "onboarding/start.html",
            section="drafts",
            can_edit=True,
            reference=reference,
            error="Enter the country of the customer's primary jurisdiction.",
            status_code=400,
        )
    # The one thing this route does refuse, and it refuses on *shape*, not on
    # policy: `drafts.country` is `varchar(3)` because the parameter is an ISO
    # alpha-2/alpha-3 code, so a value longer than that cannot be a code and
    # cannot be stored either. It reaches here only when `resolve_country` found
    # no exact name — the browser used to refuse it with a `pattern` that also
    # refused every country *name*, which is the input the resolver exists to
    # take. Saying which of the two it wanted beats a 500 from the insert.
    if len(country) > 3:
        return render(
            request,
            "onboarding/start.html",
            section="drafts",
            can_edit=True,
            country=submitted.get("country", ""),
            reference=reference,
            error=(
                "Not a country code, and not a country name this console has a code for. "
                "Use the two- or three-letter code (BGR, IT), or the exact name from the "
                "suggestions."
            ),
            status_code=400,
        )
    snapshot = await requirements.fetch_snapshot(client, country)
    if not isinstance(snapshot, dict):
        return render(
            request,
            "onboarding/start.html",
            section="drafts",
            can_edit=True,
            country=country,
            reference=reference,
            problem=problem_view(snapshot) if isinstance(snapshot, Problem) else None,
            error=None if isinstance(snapshot, Problem) else "Conduit is unreachable right now.",
            status_code=502,
        )
    try:
        model = forms.parse(snapshot)
    except forms.SchemaVersionMismatch as mismatch:
        # Never render best-effort (FORM_ENGINE_SPEC §1).
        return render(
            request,
            "onboarding/start.html",
            section="drafts",
            can_edit=True,
            reference=reference,
            error=f"{mismatch} — this console needs an update before it can onboard.",
            status_code=502,
        )
    draft = await drafts.create(
        session,
        kind=KIND,
        actor_id=actor.id,
        requirements_snapshot=snapshot,
        payload=dump(seed(model)),
        country=country,
        client_reference_id=reference,
    )
    return redirect(request, f"/onboarding/{draft.id}")


@router.get("/onboarding/{draft_id}", response_class=HTMLResponse)
async def edit(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """`?operation=<id>` re-opens the form over a rejected submission: the 422
    body is on the operation row, so the mapping is rebuilt here rather than
    carried through a redirect — and survives a reload."""
    draft = await _draft(session, draft_id, actor)
    model, values = drafts.model(draft), load(draft.payload)
    errors, problem = None, None
    if op_id := request.query_params.get("operation"):
        op = await session.get(Operation, _uuid(op_id))
        if op is not None and op.draft_id == draft.id and op.error:
            errors = forms.map_validation_errors(model, field_errors(op.error))
            problem = problem_of(op.error)
    return render(
        request,
        "onboarding/form.html",
        **_context(request, draft, values, errors, actor=actor),
        problem=problem,
        status_code=422 if errors else 200,
    )


@router.post("/onboarding/{draft_id}/save", response_class=HTMLResponse)
async def save(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    draft = await _draft(session, draft_id, actor)
    await _save(session, draft, request)
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    return HTMLResponse(f'<span class="muted">Draft saved {stamp}</span>')


@router.post("/onboarding/{draft_id}/persons", response_class=HTMLResponse)
async def add_person(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    draft = await _draft(session, draft_id, actor)
    values = await _save(session, draft, request)
    model = drafts.model(draft)
    role = dict(await form_items(request)).get("new_role") or (
        model.persons[0].role if model.persons else ""
    )
    row = next((r for r in _person_rows(model, values) if r["role"] == role), None)
    if row and row["can_add"]:
        values.persons.append(forms.PersonValues(role=role))
        await drafts.update_payload(session, draft, dump(values))
    return render(request, "onboarding/_persons.html", **_context(request, draft, values, actor=actor))


@router.post("/onboarding/{draft_id}/persons/{index}/remove", response_class=HTMLResponse)
async def remove_person(
    request: Request,
    draft_id: uuid.UUID,
    index: int,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    draft = await _draft(session, draft_id, actor)
    model = drafts.model(draft)
    # Parsed, not yet stored. `_save` would commit the posted body first, and
    # `drafts.remove_person` has to be able to see what the draft held *before*
    # this request to tell a real removal from a replay of one.
    values = forms.parse_submission(model, await form_items(request))
    if 0 <= index < len(values.persons):
        role = values.persons[index].role
        row = next((r for r in _person_rows(model, values) if r["role"] == role), None)
        if row and row["can_remove"]:
            values.persons.pop(index)
            # Answers and learned indices together, in one write: the cards above
            # just slid down one, and the demands are positions in that same list.
            # Render what the draft now holds, not what this request posted: a
            # stale removal is declined, and showing the operator their own
            # rejected card list would tell them it had been stored.
            stored = await drafts.remove_person(session, draft, dump(values), index)
            return render(
                request,
                "onboarding/_persons.html",
                **_context(request, draft, load(stored), actor=actor),
            )
    await drafts.update_payload(session, draft, dump(values))
    return render(request, "onboarding/_persons.html", **_context(request, draft, values, actor=actor))


@router.post("/documents", response_class=HTMLResponse)
async def upload(
    request: Request,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("document.upload")),
) -> Response:
    """Raw-body upload (`static/app.js`): the file's bytes are the request body,
    its name is a query parameter. `documents.intake` is the trust boundary — it
    decides the content type from the bytes themselves.

    Shared by every uploader in the console (onboarding checklist, person cards,
    RFI responses); `purpose` names which, `person` scopes the resulting chip to
    one person card's `documentIds[]`, `draft` ties the operation to a draft.
    """
    requested = request.query_params.get("draft")
    draft = await _draft(session, _uuid(requested), actor) if requested else None
    person = request.query_params.get("person")
    if person is not None and not person.isdigit():
        return _chip_error("Unknown person card.")
    name = (request.query_params.get("filename") or "document").rsplit("/", 1)[-1][:255]
    # Streamed under a cap, not buffered: `await request.body()` reads a chunked
    # body with no Content-Length in full before anything objects, so a declared
    # size was a promise, not a limit. Same helper the webhook receiver uses.
    data = await read_capped(request, documents.MAX_BYTES)
    if data is None:
        return _chip_error(
            f"The file is larger than {documents.MAX_BYTES // (1024 * 1024)} MB.",
            status_code=413,
        )
    try:
        op, is_new = await documents.intake(
            session,
            data=data,
            filename=name,
            purpose=(
                PERSON_PURPOSE
                if person is not None
                else request.query_params.get("purpose") or PURPOSE
            ),
            actor_id=actor.id,
            actor_email=actor.email,
            draft_id=draft.id if draft else None,
            intent=intent_of(request.query_params),
        )
    except documents.Rejected as refused:
        return _chip_error(str(refused).capitalize() + ".")
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state != "confirmed" or not op.conduit_resource_id:
        # A fifth reader of the stored body, found by A3's own grep pin: this
        # printed Conduit's `detail` — developer prose — onto the upload chip.
        return _chip_error(
            problem_note(op.error, "Upload not confirmed — check the operations log."),
            operation_id=op.id,
        )
    field = "documentIds" if person is None else f"p.{int(person)}.documentIds"
    return render(
        request,
        "onboarding/_chip.html",
        field=field,
        doc_id=op.conduit_resource_id,
        filename=name,
    )


def _chip_error(
    detail: str, operation_id: uuid.UUID | None = None, status_code: int = 200
) -> HTMLResponse:
    """The one hand-built fragment in the app, so the one that has to escape by
    hand. `detail` is frequently Conduit's own `detail` string — attacker-shaped
    input as far as this console is concerned — and the chip is inserted with
    `insertAdjacentHTML`, which executes markup. Everything interpolated goes
    through `escape()`; nothing here is trusted."""
    link = (
        f' <a href="/operations/{escape(str(operation_id))}">operation</a>'
        if operation_id
        else ""
    )
    return HTMLResponse(
        f'<span class="chip" role="alert">{escape(detail)}{link}</span>',
        status_code=status_code,
    )


@router.post("/onboarding/{draft_id}/review", response_class=HTMLResponse)
async def review(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    """Save what is on screen, then hand over to the GET. Post/Redirect/Get, so
    the review page can be reloaded, linked and re-read without re-posting."""
    draft = await _draft(session, draft_id, actor)
    await _save(session, draft, request)
    return redirect(request, f"/onboarding/{draft_id}/review")


@router.get("/onboarding/{draft_id}/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    draft = await _draft(session, draft_id, actor)
    model, values = drafts.model(draft), load(draft.payload)
    errors = forms.validate(model, values)
    return render(
        request,
        "onboarding/review.html",
        **_context(request, draft, values, errors, actor=actor),
        body=forms.assemble(model, values),
        errors=errors,
        # Sealed here and nowhere else: this is the render whose content the
        # reviewer is about to read, so this is the only place that can honestly
        # say what was reviewed.
        reviewed=seal_reviewed(draft, values),
    )


@router.post("/onboarding/{draft_id}/submit", response_class=HTMLResponse)
async def submit(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("onboarding.submit")),
) -> Response:
    """`POST /v2/onboarding` through the ledger. The body comes from the stored
    draft, which the review step has just saved — never from this request, so a
    resubmitted confirmation page cannot smuggle different values past the
    review the operator actually read.

    That half was always true; the other half is `reviewed`. The request
    channel cannot *supply* values, but until this guard existed it could not
    vouch for them either: the stored draft is read twice, once to render the
    review and once here, and any save landing between the two — the realistic
    one being a colleague holding `onboarding.edit`, in a deployment where
    editing and submitting are different people's jobs — went to Conduit under
    the submitter's actor id with nothing on the record saying the content had
    moved. The form carries back a seal over what the review render showed; if
    the stored answers no longer hash to it, this refuses and sends the reviewer
    back to re-read the page rather than signing for a body they never saw."""
    items = dict(await form_items(request))
    draft = await _draft(session, draft_id, actor)
    model = drafts.model(draft)
    values = load(draft.payload)
    if not forms.validate(model, values).ok:
        return redirect(request, f"/onboarding/{draft_id}/review")
    if not reviewed_matches(items.get("reviewed"), draft, values):
        # Back to the review page, not to an error page: the point is that the
        # reviewer reads the content that is actually there now. The GET mints a
        # fresh seal, so re-reading and pressing the button again is the whole
        # of the recovery.
        return redirect(
            request,
            f"/onboarding/{draft_id}/review",
            err="The draft changed since it was reviewed. Re-read it before submitting.",
        )

    # A `doc_` id in a draft is an assertion too. The checklist's ids and
    # every person card's ride onto this body as the identity evidence for the
    # customer being onboarded, and until this guard existed any id the operator
    # could guess or had seen on the operations panel went out with them — so
    # one customer's passport could be the proof of identity on another's
    # application, with Conduit's org-wide validation waving it through.
    #
    # **Scoped to the draft, not to the actor.** The wizard is the one attaching
    # flow whose uploads carry their subject (`upload` passes `draft`), and that
    # is the tighter of the two questions as well as the one this is about: an
    # operator running two applications at once is the ordinary case, and "you
    # uploaded it" would let the other one's identity documents through. The
    # draft is already an access boundary (`_draft`), so it does not need the
    # actor behind it — and requiring the actor as well would refuse the
    # split-role deployment, where the editor uploads and a *reviewer* submits.
    # See `documents.attachable` for the rule in full.
    #
    # Off the assembled body, so what is checked is exactly what would be sent,
    # and after the `reviewed` seal above — the seal answers "are these the
    # answers you read", this answers "are those documents this application's",
    # and both are answered before `operations.start`, so a refusal sends
    # nothing.
    body = forms.assemble(model, values)
    attached = forms.attached_document_ids(body)
    in_persons = {
        doc_id
        for person in (body.get("ownership") or {}).get("persons") or []
        for doc_id in person.get("documentIds") or []
    }
    elsewhere = set(forms.attached_document_ids(_without_person_documents(body)))
    refused = await documents.unattachable(
        session,
        [i for i in attached if i in elsewhere],
        purpose=PURPOSE,
        draft_id=draft.id,
    ) + await documents.unattachable(
        session,
        [i for i in attached if i in in_persons],
        purpose=PERSON_PURPOSE,
        draft_id=draft.id,
    )
    refused = list(dict.fromkeys(refused))
    if refused:
        # Back to the review page for the same reason the stale-seal refusal
        # goes there: it is the screen that lists the attachments, so it is
        # where re-uploading them is one action away.
        return redirect(
            request,
            f"/onboarding/{draft_id}/review",
            err=documents.REFUSED_ATTACHMENT.format(count=len(refused)),
        )

    op, is_new = await operations.start(
        session,
        type="onboarding_submit",
        actor_id=actor.id,
        actor_email=actor.email,
        path=PATH,
        body=body,
        draft_id=draft.id,
        intent=intent_of(items),
        # The draft's note, carried onto the operation so the submission is
        # findable by it too. Onto the *column*, never into the body: the body
        # is `forms.assemble`'s and `execute.outbound_body` owns
        # `clientReferenceId` (it is `op.id`, the reconciler's matcher).
        reference=draft.client_reference_id,
    )
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    # Not new: a double-click, a second tab or a restart resolved to the row
    # already in flight (OPERATIONS_SPEC §1) — show it, never send twice.
    if op.state == "confirmed" and op.conduit_resource_id:
        return redirect(request, f"/applications/{op.conduit_resource_id}")
    if op.state == "rejected" and (rejected := field_errors(op.error)):
        # Back to the form, with the server's own field errors mapped onto it.
        # The 422 body lives on the operation row, so the GET can render it
        # without this response having to carry it (and a reload keeps working).
        #
        # First, learn: a pointer this snapshot never advertised (reliance, see
        # `forms.learn`) becomes an input on the way back, so the operator has
        # somewhere to answer it instead of reading a refusal they cannot act on.
        await drafts.learn_fields(
            session, draft, forms.learn(model, rejected, values.persons)
        )
        return redirect(request, f"/onboarding/{draft_id}?operation={op.id}")
    return redirect(request, f"/operations/{op.id}")


@router.post("/onboarding/{draft_id}/discard", response_class=HTMLResponse)
async def discard(
    request: Request,
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(db),
    actor: Actor = Depends(require("onboarding.edit")),
) -> Response:
    draft = await _draft(session, draft_id, actor)  # ownership first, then delete
    if await drafts.discard(session, draft.id):
        return redirect(request, "/drafts", msg="Draft discarded")
    # Submitted drafts are refused: they are an application's provenance.
    return redirect(
        request,
        f"/onboarding/{draft_id}",
        err="Submitted drafts are kept as the application's record.",
    )
