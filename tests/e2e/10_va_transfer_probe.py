#!/usr/bin/env python
"""Live sandbox probe: does the **virtual-account** payout arm settle yet?

The transfers screen sends `{"type": "virtual_account",
"virtualAccountId": …}` with no rail and no whitelist entry, and the
probes on **2026-09-01** recorded the same answer twice: Conduit **accepts** the
transfer (`202`) and then **fails** it `rail_unavailable` — "No funds were
moved" — including out of an account holding $8,124 (txn_034HMkuxCGvgjuxWSySE19,
txn_034HMmLPcTRWBmoffHAIbJ). The arm is real and gates on nothing; the sandbox
simply does not settle it.

That observation is printed **on the transfers screen** on sandbox installs, so
it has to be re-askable rather than remembered. This script is the ask, and its
exit code is the answer:

    0  accepted, then failed `rail_unavailable` — today's baseline, nothing to do
    2  SETTLED — the sandbox opened the corridor; DROP the on-screen note in
       `app/web/templates/transfers/new.html` and the paragraph in
       `app/web/transfers.py`'s docstring
    3  REFUSED AT CREATE — the arm changed; the console's whole transfers flow
       needs re-reading against the spec before it ships another one
    1  neither: some other terminal state, reported verbatim

Sandbox only, the same two refusals as `07_counterparties.py` and
`09_rail_asset_probe.py`: a `ck_sandbox_` key and the sandbox host, both checked
before anything is sent. The key is read from the environment (`../.env` as a
fallback for a developer machine) and is never printed.

    .venv/bin/python tests/e2e/10_va_transfer_probe.py
    .venv/bin/python tests/e2e/10_va_transfer_probe.py --check-config   # guards only

**A failed transfer moves no money and leaves nothing to clean up** — that is
precisely what the baseline outcome means — so this is re-runnable. ZZZTEST data
only: both customers are the ZZZTEST Console SBX pair, and the amount is $1.00.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"

# The ZZZTEST Console SBX pair, pinned rather than discovered: this probe is
# about ONE corridor, and a run that silently picked a different pair of accounts
# would answer a different question from the one the on-screen note quotes.
SOURCE_CUSTOMER = "cus_034FCeDANniGVBQX8P3qoV"
SOURCE_ACCOUNT = "vac_034FCiCByMrhNurskZcYDB"
DESTINATION_ACCOUNT = "vac_034FClZQ2SjTWTH9yBxxky"
AMOUNT = "1.00"

BASELINE = "rail_unavailable"


def load_env() -> str:
    # Process environment first, `../.env` as fallback — the file does not exist
    # on CI, where only `--check-config` runs (with fake credentials).
    values: dict[str, str] = {}
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    key = os.environ.get("CONDUIT_SANDBOX_API_KEY") or values.get("CONDUIT_SANDBOX_API_KEY") or ""
    host = (
        os.environ.get("CONDUIT_SANDBOX_HOST") or values.get("CONDUIT_SANDBOX_HOST") or ""
    ).rstrip("/")
    if not key.startswith("ck_sandbox_"):
        sys.exit("refusing to run: CONDUIT_SANDBOX_API_KEY is not a ck_sandbox_ key")
    if host != SANDBOX_HOST:
        sys.exit(f"refusing to run: CONDUIT_SANDBOX_HOST is not {SANDBOX_HOST}")
    return key


KEY = load_env()

# `--check-config` stops here: everything above is the two refusals, and nothing
# below them has run. It is what `tests/test_web_transfers.py` exercises, because
# the only other way to prove a guard fires is to run the script it guards.
if "--check-config" in sys.argv:
    print(f"config ok: {SANDBOX_HOST}")
    sys.exit(0)

import httpx  # noqa: E402


async def main() -> int:
    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )
    print(f"\nvirtual-account transfer probe — host {SANDBOX_HOST}\n")
    print(f"  source      {SOURCE_CUSTOMER} / {SOURCE_ACCOUNT}")
    print(f"  destination {DESTINATION_ACCOUNT}")

    body = {
        "customerId": SOURCE_CUSTOMER,
        "virtualAccountId": SOURCE_ACCOUNT,
        "assetAmount": {"code": "USD", "amount": AMOUNT},
        "purpose": "intercompany",
        "clientReferenceId": f"ZZZTEST-vaprobe-{uuid.uuid4().hex[:10]}",
        # Exactly what `payments.virtual_account_body` emits: no rail, no
        # recipient. If the console's builder and this probe ever disagree, the
        # probe is answering about a body nobody sends.
        "destination": {
            "type": "virtual_account",
            "virtualAccountId": DESTINATION_ACCOUNT,
            "remittance": {"reference": "ZZZTEST-VA-PROBE"},
        },
    }
    print(f"\n  POST /payouts — {AMOUNT} USD on the virtual-account arm")
    response = await api.post(
        "/payouts", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    print(f"  HTTP {response.status_code}")
    print("  " + response.text[:1200].replace("\n", "\n  "))

    if response.status_code >= 300:
        await api.aclose()
        print(
            "\n  *** REFUSED AT CREATE *** — the arm has changed. On 2026-09-01 this same "
            "body was accepted with a 202 and no whitelist entry for the destination "
            "customer. Re-read `FiatPayoutDto.destination` against the live spec before "
            "the transfers screen sends another one.\n"
        )
        return 3

    transaction_id = (response.json() or {}).get("id") or ""
    print(f"\n  accepted as {transaction_id} — polling for a terminal state")
    final: dict = {}
    for _ in range(20):
        read = await api.get(f"/transactions/{transaction_id}")
        final = read.json() if read.status_code == 200 else {}
        if final.get("status") in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(1.0)
    await api.aclose()

    status = final.get("status")
    code = final.get("failureCode") or ""
    print(f"  terminal status: {status}  failureCode: {code}")
    print(f"  failureMessage: {final.get('failureMessage') or ''}")

    if status == "failed" and code == BASELINE:
        print(
            "\n  BASELINE UNCHANGED — accepted at create, `rail_unavailable` after review, "
            "no funds moved. This is what the sandbox note on the transfers screen quotes; "
            "leave it in place. Nothing to clean up.\n"
        )
        return 0
    if status == "completed":
        print(
            "\n  *** SETTLED *** — the sandbox now performs this corridor. DROP the sandbox "
            "note from `app/web/templates/transfers/new.html` and the paragraph from "
            "`app/web/transfers.py`'s docstring: they would be describing a limitation that "
            "no longer exists.\n"
        )
        return 2
    print(
        f"\n  *** UNEXPECTED *** terminal status {status!r} / failureCode {code!r}. Neither "
        "the recorded baseline nor a settlement — report before changing the console's copy "
        "in either direction.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
