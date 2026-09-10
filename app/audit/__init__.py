"""Actor-attributed audit trail (plan v2 §3).

Deliberately not committing: audit rows are written inside the caller's
transaction so an operation transition and its audit row land together or not at
all.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent

SYSTEM_ACTOR_ID = "system"
SYSTEM_ACTOR_EMAIL = "system@conduit-console"


def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: str,
    actor_email: str,
    operation_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        action=action,
        actor_id=actor_id,
        actor_email=actor_email,
        operation_id=operation_id,
        detail=detail,
    )
    session.add(event)
    return event
