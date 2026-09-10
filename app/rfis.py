"""RFIs as a shared vocabulary: subjects, the subject-scoped read, the ack ledger.

Three web modules need the same three facts about an RFI — the application
detail page, the transaction detail page and the global `/rfis` index — so they
live here rather than in whichever page happened to need them first. Nothing in
this module renders anything or touches a `Request`; it is the same layer as
`app/payments.py`.

**An RFI is never created by this console.** Conduit's compliance side publishes
one; there is no endpoint that opens one and therefore no `operations` row that
could produce one. What the console owns is the *answer* (`rfi_respond`,
OPERATIONS_SPEC §3) and a local "we have seen this" marker, which is what
`ACK_ACTION` is.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conduit.client import ConduitClient, Page
from app.models import AuditEvent

# `subjectType`'s enum (`ClientRfiPaginatedResponseDto`), and where this console
# keeps each kind. `organization` is in Conduit's enum and has no page here:
# filterable, and rendered as text. A kind added to the API later lands in the
# same unlinked branch rather than in a link to a 404.
SUBJECT_PATHS = {
    "application": "/applications",
    "transaction": "/transactions",
    "customer": "/customers",
}
SUBJECT_TYPES = ("application", "transaction", "customer", "organization")

ACK_ACTION = "rfi.acknowledged"


def subject_url(subject_type: str, subject_id: str) -> str:
    """The console page for one subject, or `""` for a kind we have no page for."""
    base = SUBJECT_PATHS.get(subject_type)
    return f"{base}/{subject_id}" if base and subject_id else ""


async def for_subject(
    client: ConduitClient, subject_type: str, subject_id: str
) -> tuple[list[dict], bool]:
    """`(rfis, ok)` — every RFI Conduit holds against one subject, newest page
    only, and whether the read actually landed.

    **The flag is the whole point**. Flattening a failure to
    `[]` let a 503 render as "Conduit has not asked for anything more" — the
    console stating an absence it had not established, which is the one thing
    every other read on this app is careful not to do. `ok is False` means we do
    not know; `([], True)` means Conduit answered and there are none.
    """
    page = await client.page(
        "/v2/rfis", subjectType=subject_type, subjectId=subject_id, limit=50
    )
    return (page.items, True) if isinstance(page, Page) else ([], False)


async def acknowledged(session: AsyncSession, rfi_id: str) -> bool:
    """The local "first view" marker. Written only on a successful acknowledge,
    so a failed one is retried on the next render rather than swallowed."""
    return bool(
        await session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == ACK_ACTION, AuditEvent.detail["rfi"].astext == rfi_id
            )
        )
    )


async def mark_unacknowledged(session: AsyncSession, rfis: list[dict]) -> list[dict]:
    """Flag the open RFIs this console has not yet told Conduit it has seen.

    The panel fires the acknowledging POST from the flag; a GET must never
    mutate (`web/applications.detail`).
    """
    for rfi in rfis:
        rfi["needs_ack"] = bool(
            rfi.get("status") == "open"
            and rfi.get("id")
            and not await acknowledged(session, rfi["id"])
        )
    return rfis
