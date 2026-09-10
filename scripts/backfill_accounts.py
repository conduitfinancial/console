#!/usr/bin/env python
"""Seed the virtual-account projections from a live walk — once, deliberately.

The projections are fed by webhooks, and an installation without a registered
webhook endpoint (a fresh deploy before registration; local development, where
there is no public URL for Conduit to call) has observed nothing: the Accounts
index is honestly empty and the transfer screen's observed-holders filter has
nothing to filter with. This script closes that bootstrap gap the architecture's
own way: it reads every customer's virtual accounts live and writes each through
`projections.apply_observation` — the single write boundary, so the PII scrub
and the monotonic state rules apply exactly as they do to a webhook delivery.
An observation is an observation, whichever way it arrived.

    .venv/bin/python scripts/backfill_accounts.py

Deliberately a script, not a worker job: it is one bounded walk (a page of
customers, a page of accounts each) run when an operator decides the records
should catch up — registering the webhook endpoint is what keeps them caught up.
Safe to re-run: `apply_observation` upserts, and a newer webhook observation is
never regressed by this walk's older one (the monotonic ladder decides).
Read-only against Conduit; writes only this console's own database.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Same env contract as the app: CONDUIT_ENV/CONDUIT_API_KEY/DATABASE_URL must
# already be what the running console uses — this script reads the same
# settings machinery rather than inventing a second configuration path.
from app import projections  # noqa: E402
from app.conduit import ConduitClient  # noqa: E402
from app.conduit.client import Page  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import sessionmaker  # noqa: E402

PAGE_LIMIT = 100
MAX_CUSTOMERS = 1000  # a bootstrap walk, not a sync engine; stated when hit


async def main() -> int:
    settings = get_settings()
    client = ConduitClient(settings=settings)
    walked = accounts_seen = written = 0
    capped = False

    async with sessionmaker()() as session:
        cursor = None
        while True:
            page = await client.page("/v2/customers", cursor=cursor, limit=PAGE_LIMIT)
            if not isinstance(page, Page):
                print(f"customers read failed: {page!r} — seeded {written} so far", flush=True)
                return 1
            for customer in page.items:
                cid = str(customer.get("id") or "")
                if not cid:
                    continue
                walked += 1
                if walked > MAX_CUSTOMERS:
                    capped = True
                    break
                accounts = await client.page(
                    f"/v2/customers/{cid}/virtual-accounts", limit=PAGE_LIMIT
                )
                if not isinstance(accounts, Page):
                    print(f"  {cid}: accounts read failed, skipped ({accounts!r})", flush=True)
                    continue
                for account in accounts.items:
                    vid = str(account.get("id") or "")
                    if not vid:
                        continue
                    accounts_seen += 1
                    # The account DTO itself is the observation; customerId is
                    # what the index and the holders filter read, and the DTO
                    # carries it. The write boundary scrubs and rank-guards.
                    await projections.apply_observation(
                        session,
                        resource_kind="virtual_accounts",
                        resource_id=vid,
                        observed={**account, "customerId": cid},
                    )
                    written += 1
            if capped or not page.next_cursor:
                break
            cursor = page.next_cursor

    print(
        f"seeded {written} observation(s) from {accounts_seen} account(s) "
        f"across {min(walked, MAX_CUSTOMERS)} customer(s)"
        + (f" — CAPPED at {MAX_CUSTOMERS} customers; run again or register the webhook"
           if capped else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
