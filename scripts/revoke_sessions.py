#!/usr/bin/env python
"""Sign one operator out of every session they currently hold — now, deliberately.

A session cookie is signed and self-contained, so before this existed the only
way to end a live one was to rotate `SESSION_SECRET`, which ends everybody's. Use
this when a laptop goes missing, when someone leaves, or when a session may have
been taken: it records the instant, and every cookie minted before it stops being
honoured on the operator's next request.

    .venv/bin/python scripts/revoke_sessions.py --sub <sub>

`--sub` is the operator's subject as this console knows it — `Actor.id`: the
`sub` claim from the IdP, and the `actor_id` column in `audit_events`, which is
where to look it up if you only have an email:

    select distinct actor_id, actor_email from audit_events where actor_email = '…';

**It does not ban the account.** The operator can sign in again immediately and
gets a working session — revocation ends the sessions that exist. To stop
someone signing in at all, remove them upstream in the IdP or take their role
away in `ROLES_FILE`; do both if you mean both.

**`AUTH_MODE=oidc` only.** In proxy mode this console holds no session — the
proxy asserts identity on every request and owns sign-out, so revoking here
would write a row nothing reads. The script says so rather than pretending.

Writes one row of this console's own database; no Conduit call, and nothing else
is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Same env contract as the app: DATABASE_URL must already be what the running
# console uses — this script reads the same settings machinery rather than
# inventing a second configuration path.
from sqlalchemy import select  # noqa: E402

from app.auth.revocation import revoke  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import engine, sessionmaker  # noqa: E402
from app.models import AuditEvent  # noqa: E402


def clean_sub(value: str) -> str:
    """The subject as typed, minus what a copy-paste brings with it.

    A trailing newline or space off a terminal, a spreadsheet cell or a chat
    message is invisible on screen and is *not* the operator's `sub`: it would
    write a revocation row nothing ever matches and report success. Empty after
    stripping is refused for the same reason, one step earlier.
    """
    if not (sub := value.strip()):
        raise argparse.ArgumentTypeError("--sub cannot be empty")
    return sub


async def main(sub: str) -> int:
    mode = get_settings().auth_mode
    if mode != "oidc":
        print(
            f"AUTH_MODE is {mode!r}: this console issues no session cookie, so there is "
            "nothing here to revoke. Sign the operator out at the proxy or IdP instead.",
            file=sys.stderr,
        )
        return 2
    # Which database is about to be written, said out loud before it is written:
    # this script is run from a shell whose `DATABASE_URL` the operator cannot
    # see, and revoking on staging while believing it was production is a silent
    # no-op during an incident. Host and database only — never the password.
    url = engine().url
    print(f"writing to {url.host or 'localhost'}/{url.database}", file=sys.stderr)
    async with sessionmaker()() as session:
        moment = await revoke(session, sub)
        seen = await session.scalar(
            select(AuditEvent.actor_id).where(AuditEvent.actor_id == sub).limit(1)
        )
    print(f"revoked every session issued to {sub!r} before {moment.isoformat()}")
    if seen is None:
        # The row is written either way — a subject with no audit history is
        # possible (a brand-new operator who has done nothing yet). But the far
        # likelier cause is a typo, and a typo that prints "revoked" and leaves
        # the real session live is the worst outcome this script has.
        print(
            f"warning: no audit_events row has actor_id = {sub!r}. If this is a typo the "
            "operator's sessions are still live; check the subject with:\n"
            "  select distinct actor_id, actor_email from audit_events;",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sub", required=True, type=clean_sub, help="the operator's subject (Actor.id)"
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().sub)))
