#!/usr/bin/env python
"""Live sandbox probe: does Conduit refuse a EUR-funded fedwire payout?

The console is about to refuse this
combination itself, before an operation row exists — and a refusal this console
writes must be calibrated against what Conduit actually says, not against what
we assume it says. So one real attempt, on the sandbox, with a ZZZTEST customer:

* if Conduit REFUSES, its code and sentence are quoted in the console's copy;
* if Conduit ACCEPTS, the design question changes and the script says STOP —
  a console refusing what the rail actually carries would be the console lying
  in the other direction.

Sandbox only, same two refusals as `07_counterparties.py`: a `ck_sandbox_` key
and the sandbox host, both checked before anything is sent.

    .venv/bin/python tests/e2e/09_rail_asset_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"
AMOUNT = "1.00"


def load_env() -> str:
    values = {}
    for line in (ROOT.parent / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    key = values.get("CONDUIT_SANDBOX_API_KEY") or ""
    host = (values.get("CONDUIT_SANDBOX_HOST") or "").rstrip("/")
    if not key.startswith("ck_sandbox_"):
        sys.exit("refusing to run: CONDUIT_SANDBOX_API_KEY is not a ck_sandbox_ key")
    if host != SANDBOX_HOST:
        sys.exit(f"refusing to run: CONDUIT_SANDBOX_HOST is not {SANDBOX_HOST}")
    return key


KEY = load_env()

import httpx  # noqa: E402


async def main() -> int:
    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )
    print(f"\nrail/asset probe — host {SANDBOX_HOST}\n")

    # A ZZZTEST customer with an ACTIVE EUR account. Discovered, never pinned.
    customer_id = account_id = ""
    listed = await api.get("/customers", params={"limit": 50})
    for customer in listed.json().get("data") or []:
        if "ZZZTEST" not in json.dumps(customer):
            continue
        found = await api.get(f"/customers/{customer['id']}/virtual-accounts")
        if found.status_code >= 400:
            continue
        for account in found.json().get("data") or []:
            if account.get("status") == "active" and (
                (account.get("asset") or {}).get("code") == "EUR"
            ):
                customer_id, account_id = customer["id"], account["id"]
                break
        if account_id:
            break
    if not account_id:
        print("  SKIP — no ZZZTEST customer holds an active EUR virtual account")
        await api.aclose()
        return 0
    print(f"  customer {customer_id}\n  EUR account {account_id}")

    # This route gates on documents (live: `DOCUMENTATION_REQUIRED`), and that
    # refusal fires *before* Conduit judges the currency — so the first run of
    # this probe proved nothing about the rail. One real upload gets the request
    # past the gate, and only then is the answer about EUR-over-fedwire.
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    uploaded = await api.post(
        "/documents",
        files={"file": ("zzztest-railprobe.pdf", pdf, "application/pdf")},
        data={"purpose": "transaction_support", "customerId": customer_id},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    document_id = (uploaded.json() or {}).get("id") or ""
    print(f"  document {document_id} (HTTP {uploaded.status_code})")

    body = {
        "customerId": customer_id,
        "virtualAccountId": account_id,
        "assetAmount": {"code": "EUR", "amount": AMOUNT},
        "purpose": "payment_for_goods_or_services",
        "clientReferenceId": f"ZZZTEST-railprobe-{uuid.uuid4().hex[:10]}",
        "documents": [document_id] if document_id else [],
        "destination": {
            "type": "fiat",
            "rail": "fedwire",
            "recipient": {
                "type": "BUSINESS",
                "accountType": "CHECKING",
                "legalName": "ZZZTEST Rail Probe LLC",
                "accountNumber": "1234567890",
                "routingNumber": "021000021",
                "bankAddress": {
                    "addressLine1": "270 Park Ave",
                    "city": "New York",
                    "country": "US",
                },
                "postalAddress": {
                    "addressLine1": "500 Market St",
                    "city": "New York",
                    "country": "US",
                    "postalCode": "10010",
                },
            },
        },
    }
    print("\n  POST /payouts — EUR account, fedwire rail")
    response = await api.post(
        "/payouts", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    print(f"  HTTP {response.status_code}")
    print("  " + response.text[:1200].replace("\n", "\n  "))

    if response.status_code >= 300:
        print(
            "\n  Conduit refuses this at create. That is NOT what this probe recorded on "
            "2026-08-31 (202 then a post-review failure) — re-read the console's copy "
            "before trusting it.\n"
        )
        await api.aclose()
        return 1

    # Accepted. The question is what happens *next*: a 202 that settles is a
    # supported corridor, a 202 that fails after review is a doomed attempt worth
    # refusing before the ledger row exists. Only the second justifies the
    # console's guard, so the probe drives it to a terminal state and says which.
    transaction_id = (response.json() or {}).get("id") or ""
    print(f"\n  accepted as {transaction_id} — driving it through review")
    await api.post(
        f"/sandbox/payouts/{transaction_id}/simulate-review-approve",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
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
    if status == "failed" and code == "rail_unavailable":
        print(
            "\n  CONFIRMED — accepted at create, `rail_unavailable` after review, no funds "
            "moved. This is the evidence `payments.RAIL_ASSET_MESSAGE` quotes, and the "
            "reason the console refuses the pair before the operation row exists.\n"
        )
        return 0
    print(
        f"\n  *** UNEXPECTED *** terminal status {status!r} / failureCode {code!r}. The "
        "console's refusal copy quotes `rail_unavailable`; if this corridor now settles, "
        "that refusal is refusing something Conduit performs — report before shipping.\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
