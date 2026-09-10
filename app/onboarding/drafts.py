"""Durable onboarding drafts (plan v2 §3 + §7 Onboarding).

A draft is the operator's raw form values — encrypted at rest — plus the
requirements snapshot they were rendered from. Two rules do most of the work:

**The snapshot is pinned.** A draft is re-rendered and validated against *its
own* copy for its whole life (`model()`), never against a fresh discovery fetch.
Requirements change; a draft that started answering one questionnaire must
finish answering that one, or half-given answers silently become invalid (or,
worse, silently valid).

**The payload dies on success, not on failure.** A *rejected* application is not
a success: its draft keeps its payload, which is exactly what rejection
correction re-opens — edit, resubmit, new operation, new idempotency key, same
`client_reference_id`.

Success is the *application* being accepted, not the submit call returning 202.
`POST /v2/onboarding` answers 202 immediately and the application is decided
hours or days later, so purging when the operation confirms would destroy every
answer before the only event that needs them — a rejection — could happen, and
Conduit does not hand the submitted payload back (`ApplicationDto` carries
status, persons and failure fields, not the submission). So `submitted()` stamps
the submission and `purge_settled()` (worker) drops the payload once the
application is finally settled: approved, cancelled, or rejected with
`resubmittable: false`. `OP_BODY_RETENTION` is the backstop for a submission
whose application this installation never observes.
*(Corrected 2026-08-28: the rule above was this module's
documented contract from the start; the code implemented purge-on-202.)*

Unsubmitted drafts are discarded after `DRAFT_TTL_DAYS` by the worker.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import forms
from app.config import get_settings
from app.crypto import fernet
from app.models import Draft, Operation, Projection


class DraftClosed(Exception):
    """The draft was submitted and purged — there is nothing left to edit."""


async def create(
    session: AsyncSession,
    *,
    kind: str,
    actor_id: str,
    requirements_snapshot: dict,
    payload: dict | None = None,
    customer_id: str | None = None,
    country: str | None = None,
    client_reference_id: str | None = None,
) -> Draft:
    """Pin the snapshot and store the (possibly empty) answers. Commits."""
    draft = Draft(
        kind=kind,
        actor_id=actor_id,
        customer_id=customer_id,
        country=country,
        client_reference_id=client_reference_id,
        payload=payload or {},
        requirements_snapshot=requirements_snapshot,
    )
    session.add(draft)
    await session.commit()
    return draft


async def load(session: AsyncSession, draft_id: uuid.UUID) -> Draft | None:
    return (
        await session.execute(
            select(Draft).where(Draft.id == draft_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def update_payload(
    session: AsyncSession,
    draft: Draft,
    payload: dict,
    *,
    client_reference_id: str | None = None,
) -> Draft:
    """Replace the answers. Commits.

    The snapshot is never touched: re-pinning it mid-draft is the one thing the
    pinning rule exists to prevent.
    """
    if draft.purged_at is not None:
        raise DraftClosed(f"draft {draft.id} was submitted and purged")
    draft.payload = payload
    if client_reference_id is not None:
        draft.client_reference_id = client_reference_id
    await session.commit()
    return draft


def name_of(blob: object) -> str:
    """The legal name inside one draft's ciphertext, or `""`.

    **The tolerant per-row decrypt, exactly as `app/counterparties.py` does it**
    (and for the same reason a review found on the dashboard): the column
    is read raw as `LargeBinary` and decrypted here, one row at a time, so a
    corrupt ciphertext — or a key rotated away from under the row — costs that
    row its name and nothing else. During a key incident `/drafts` is exactly
    where an operator goes to see what survived, so the page must still render
    with no key at all: every row falls back to its reference or its id.

    `""` covers all four of "no payload" (purged), "unreadable", "nothing named
    in it" and "named blank". None of them is a name, and the list renders none
    of them as one.

    Two levels of the answers, not a recursive search: discovery puts the field
    at `root.businessInfo.legalName` on every questionnaire this console has
    seen, and a deep hunt for any key called `legalName` would eventually find
    somebody *else's* — a recipient's, a bank's — and print it as the applicant.
    """
    if blob is None:
        return ""
    try:
        payload = json.loads(fernet().decrypt(bytes(blob)))  # type: ignore[arg-type]
    except (InvalidToken, ValueError, TypeError):
        return ""
    root = payload.get("root") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        return ""
    candidates = [root] + [value for value in root.values() if isinstance(value, dict)]
    for node in candidates:
        name = node.get("legalName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


async def list_for_actor(
    session: AsyncSession, actor_id: str, *, include_submitted: bool = False
) -> Sequence[Draft]:
    """The actor's own drafts, most recently touched first."""
    query = select(Draft).where(Draft.actor_id == actor_id)
    if not include_submitted:
        query = query.where(Draft.submitted_at.is_(None))
    return (await session.execute(query.order_by(Draft.updated_at.desc()))).scalars().all()


async def discard(session: AsyncSession, draft_id: uuid.UUID) -> bool:
    """Operator abandons a draft. Commits.

    Deletes the row: an unsubmitted draft has no history worth keeping, and any
    operation that ever pointed at it keeps its own record (the FK is
    `ON DELETE SET NULL`).

    **Unsubmitted only.** A submitted draft is the provenance of a live
    application — what was answered, against which pinned questionnaire, and the
    thing rejection correction re-opens. Deleting it destroys that and orphans
    the operation's `draft_id`. Returns False when it refuses, so the caller can
    say so rather than report a deletion that did not happen.
    """
    deleted = await session.execute(
        delete(Draft).where(Draft.id == draft_id, Draft.submitted_at.is_(None))
    )
    await session.commit()
    return bool(deleted.rowcount)


async def submitted(session: AsyncSession, draft_id: uuid.UUID) -> None:
    """The draft's operation confirmed: stamp `submitted_at`.

    The answers stay until the *application* settles (`purge_settled`) — a
    submitted application can still be rejected, and correcting it is exactly
    what re-opens this payload.

    Deliberately does NOT commit — it is called from inside
    `operations.transition`, so the stamp and the `confirmed` transition land in
    one transaction.
    """
    await session.execute(
        update(Draft)
        .where(Draft.id == draft_id, Draft.submitted_at.is_(None))
        .values(submitted_at=datetime.now(UTC))
    )


# Application states that end the story. `rejected` is deliberately absent: it is
# the one outcome whose answers are still wanted — unless Conduit says the
# decision is final.
SETTLED_APPLICATION_STATES = ("approved", "cancelled")


async def purge_settled(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker retention job: drop the answers behind settled submissions.

    Purged when the application projection says approved or cancelled, or says
    rejected with `resubmittable: false` (Conduit's own "do not resubmit"), and
    unconditionally `OP_BODY_RETENTION` days after submission — the backstop for
    an application whose outcome this installation never observed. A rejection
    that is still correctable, or one whose `resubmittable` flag never arrived,
    keeps its payload until that window: the conservative direction, since the
    alternative destroys the only copy of the operator's work.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().op_body_retention_days)
    final = (
        select(Operation.draft_id)
        .join(Projection, Projection.resource_id == Operation.conduit_resource_id)
        .where(
            Operation.draft_id.is_not(None),
            Projection.resource_kind == "applications",
            or_(
                Projection.state.in_(SETTLED_APPLICATION_STATES),
                and_(
                    Projection.state == "rejected",
                    Projection.payload["resubmittable"].astext == "false",
                ),
            ),
        )
    )
    purged = await session.execute(
        update(Draft)
        .where(
            Draft.submitted_at.is_not(None),
            Draft.payload.is_not(None),
            or_(Draft.id.in_(final), Draft.submitted_at < cutoff),
        )
        .values(payload=None, purged_at=now)
    )
    await session.commit()
    return purged.rowcount


async def discard_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker retention job: unsubmitted drafts untouched for DRAFT_TTL_DAYS.

    Submitted drafts are kept — their payload is already gone and the rest is
    the application's provenance.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=get_settings().draft_ttl_days)
    discarded = await session.execute(
        delete(Draft).where(Draft.submitted_at.is_(None), Draft.updated_at < cutoff)
    )
    await session.commit()
    return discarded.rowcount


def model(draft: Draft) -> forms.FormModel:
    """The form this draft is rendered and validated against — always its own
    pinned snapshot (plan v2 §3). The single reason this function exists is that
    every call site that reaches for `forms.parse(...)` on a draft has to reach
    for *this* snapshot, not a fresh one."""
    snapshot = draft.requirements_snapshot or {}
    return forms.with_learned(forms.parse(snapshot), snapshot.get(forms.LEARNED_KEY) or [])


async def remove_person(session: AsyncSession, draft: Draft, payload: dict, index: int) -> dict:
    """Store `payload` — the answers without the card at `index` — and renumber the
    learned demands that outlive it, in one transaction. Returns the answers the
    draft now holds, which is what the caller must render.

    **One write, under a row lock, because the two halves disagree about what they
    are.** The payload is written absolutely (the whole list, from the request);
    the renumber is a relative shift of stored indices. Split across two commits
    they raced: two overlapping removals — two tabs, a double-fired Remove — each
    read the same indices and each shifted by their own, so a shift was lost or
    applied twice and a demand landed on a card Conduit never named.

    **Everything is decided from the locked row, including whether the draft is
    still open.** `draft` is the caller's view, taken when its request began; a
    submission can purge the row in the window before the lock, and a check
    against that stale object let the write land afterwards — restoring identity
    answers retention had just dropped.

    **The payload is written only when this request's card list is what removing
    `index` from the stored one would give.** Otherwise the draft's structure moved
    under this request — another tab added or removed a card — and writing it
    absolutely would undo that. Nothing is written, and the locked answers come
    back for rendering so the operator sees what is actually stored rather than
    what they posted.

    The comparison is on the cards' *shape*, not on their values: `hx-include`
    posts the whole wizard, so answers the operator has typed since the last save
    ride along with the Remove and legitimately differ from what is stored.
    Comparing whole payloads cannot tell "somebody else edited this" from "I
    edited this", and refuses both — measured: with a full-payload compare, an
    operator who types a name and then clicks Remove gets no removal and loses the
    name. A concurrent *value* edit being overwritten is the wizard's
    last-write-wins model, shared with `add_person` and every other `_save`, and
    closing it needs a version token on the draft rather than a check here.
    """
    locked = (
        await session.execute(
            select(Draft)
            .where(Draft.id == draft.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if locked.purged_at is not None:
        raise DraftClosed(f"draft {draft.id} was submitted and purged")

    stored = locked.payload or {}
    shape = [p.get("role") for p in (stored.get("persons") or [])]
    if [p.get("role") for p in (payload.get("persons") or [])] != shape[:index] + shape[index + 1 :]:
        return stored

    locked.payload = payload
    snapshot = locked.requirements_snapshot or {}
    existing = list(snapshot.get(forms.LEARNED_KEY) or [])
    rewritten = forms.forget_person_index(existing, index)
    if rewritten != existing:
        locked.requirements_snapshot = {**snapshot, forms.LEARNED_KEY: rewritten}
    await session.commit()
    return payload


async def learn_fields(session: AsyncSession, draft: Draft, descriptors: list[dict]) -> bool:
    """Record fields Conduit demanded that this draft's snapshot never advertised,
    beside the pinned discovery keys and never touching them (FORM_ENGINE_SPEC §7).
    Returns whether anything changed."""
    snapshot = draft.requirements_snapshot or {}
    existing = list(snapshot.get(forms.LEARNED_KEY) or [])
    merged = forms.merge_learned(existing, descriptors)
    if merged == existing:
        return False
    # A new dict, not a mutation: SQLAlchemy does not track in-place edits of a
    # JSONB value, and a silently unsaved schema is the whole bug again.
    draft.requirements_snapshot = {**snapshot, forms.LEARNED_KEY: merged}
    await session.commit()
    return True
