"""Cutting off one operator's issued sessions.

A session cookie is signed and self-contained: nothing in this console had to be
consulted for it to be honoured, which is what made it cheap. The cost is that
there was no way to end one — a laptop lost with a live cookie stayed live until
`SESSION_MAX_AGE` ran out, and the only lever was rotating `SESSION_SECRET`,
which signs *everybody* out.

This is the per-operator lever. `session_revocations` holds one row per cut-off
operator; a cookie is refused when it was minted before that instant. A later
sign-in mints a later `iat` and works normally, so revoking ends the sessions
that exist rather than banning the account — removing the operator upstream (or
from `ROLES_FILE`) is what bans an account.

No route, no permission, no button. `scripts/revoke_sessions.py` and an
operator with database access is the whole feature; a `session.revoke` permission
and a control on a person's page is the upgrade path, when someone asks for one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import sessionmaker
from app.models import SessionRevocation


async def revoked(sub: str, issued_at: int) -> bool:
    """Was this operator's session minted before their cut-off?

    One primary-key SELECT per authenticated request.

    **No cache, deliberately.** This is one indexed lookup of one small row on a
    connection the request already needs for its own reads — it is noise beside
    the page's own queries, and this console serves an operations team, not the
    public: tens of requests a minute, not thousands a second. A cache here would
    buy nothing measurable and cost the thing revocation is for, which is that a
    cut-off takes effect on the *next* request rather than after a TTL. The
    upgrade path, if this ever fronts real volume, is a process-local
    `{sub: not_before}` with a short TTL plus a version counter to invalidate it
    — and the first thing to check before building it is whether the query even
    shows up in a profile.

    **The boundary is `<=`, and it spans two clocks.** `issued_at` is whole
    seconds from `time.time()` on the *web* host (`session_value`); `not_before`
    is `datetime.now(UTC)` on whichever host ran `scripts/revoke_sessions.py`.
    A cookie minted in the same second as the revocation is therefore refused
    rather than kept — at the boundary, cutting off is the safe direction, and
    the operator signs in again a second later. Nothing synchronises those two
    clocks: if the script host runs `s` seconds behind the web host, the cut-off
    lands `s` seconds earlier than the operator intended (a session minted in
    that window survives); ahead by `s`, it reaches `s` seconds further forward
    (a session minted just after the revocation is refused, and signing in again
    fixes it). Keep both hosts on NTP if that matters; it is one second in
    practice, and both failure directions end in "sign in again".

    Its own session rather than the request's: authentication happens in
    middleware, before any route has opened one.
    """
    async with sessionmaker()() as session:
        not_before = (
            await session.execute(
                select(SessionRevocation.not_before).where(SessionRevocation.sub == sub)
            )
        ).scalar_one_or_none()
    return not_before is not None and issued_at <= not_before.timestamp()


async def revoke(session: AsyncSession, sub: str, *, now: datetime | None = None) -> datetime:
    """Cut off every session `sub` currently holds. Idempotent: re-running moves
    the instant forward, which is the honest meaning of doing it twice."""
    moment = now or datetime.now(UTC)
    await session.execute(
        insert(SessionRevocation)
        .values(sub=sub, not_before=moment)
        .on_conflict_do_update(index_elements=["sub"], set_={"not_before": moment})
    )
    await session.commit()
    return moment
