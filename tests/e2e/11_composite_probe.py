#!/usr/bin/env python
"""Live sandbox probe: can a fiat→fiat conversion still not carry a payout?

The design asked Conduit to do the whole thing in one call — quote a *payout-mode*
conversion (different assets **plus** a `destinationCountry`), then redeem the
chosen option as an order carrying `autoPayout`, a chained external withdrawal
that fires when the conversion settles. The quote half is real. The redemption
half is not, and on **2026-09-01** the probes recorded why:

    422 INVALID_ORDER_COMBO — "autoPayout is not supported on a fiat-to-fiat
    conversion; create a payout from the destination virtual account instead."

…while the same option redeemed *without* `autoPayout` is a `400`: "was priced
for a SEPA payout; redeeming it requires autoPayout naming the recipient." A
fiat payout-mode option is quotable and, by a fiat-only console, not orderable.
That stopped there and the two-step hand-off shipped instead — the
payout page's rail/asset refusal now points at Convert, prefilled, and says in
so many words that a conversion and a payout are two operations with two
settlements.

That copy rests on a refusal, so the refusal has to be re-askable rather than
remembered. This script is the ask, and its exit code is the answer:

    0  refused exactly as recorded (`INVALID_ORDER_COMBO`) — today's baseline,
       the guided hand-off is still the honest shape, nothing to do
    2  ACCEPTED — fiat→fiat chaining has opened, so the composite the guided
       hand-off stands in for is buildable after all. The order this run creates
       is NOT executed (see below)
    3  the QUOTE mode itself changed shape — payout-mode no longer prices this
       pair, or prices it without a rail. The hand-off copy is not wrong, but
       everything downstream of the quote here is answering about a product that
       no longer exists; re-read `guides/quote-before-you-order` before trusting
       either exit above
    1  neither: some other refusal, reported verbatim

Sandbox only, the same two refusals as `09_rail_asset_probe.py` and
`10_va_transfer_probe.py`: a `ck_sandbox_` key and the sandbox host, both checked
before anything is sent. The key is read from the environment (`../.env` as a
fallback for a developer machine) and is never printed.

    .venv/bin/python tests/e2e/11_composite_probe.py
    .venv/bin/python tests/e2e/11_composite_probe.py --check-config   # guards only

**Nothing here moves money.** A quote creates nothing and reserves nothing, and
an order is *created* pending — `POST /v2/orders/{id}/execute` is the call that
spends, and this script never makes it. On the baseline path the order is never
created at all (the create is the thing that 422s). On the exit-2 path an order
DOES exist: the script cancels it and prints the id either way, because an
accepted composite is a finding to report, not a payment to make. ZZZTEST data
only — the ZZZTEST Console SBX pair, $10.00.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"
EVIDENCE = Path(__file__).parent / "sandbox_evidence"

# The ZZZTEST Console SBX pair, pinned rather than discovered — the same two
# accounts the 2026-09-01 evidence names, so a later run answers about the same
# corridor rather than about whichever accounts happened to sort first.
CUSTOMER = "cus_034FCeDANniGVBQX8P3qoV"
SOURCE_ACCOUNT = "vac_034FCiCByMrhNurskZcYDB"  # USD
DESTINATION_ACCOUNT = "vac_034FCiLdKtpwHn33PX9h13"  # EUR — `destination` is REQUIRED
AMOUNT = "10.00"
COUNTRY = "DEU"

BASELINE = "INVALID_ORDER_COMBO"


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
# below them has run. It is what `tests/test_web_payouts.py` exercises, because
# the only other way to prove a guard fires is to run the script it guards.
if "--check-config" in sys.argv:
    print(f"config ok: {SANDBOX_HOST}")
    sys.exit(0)

import httpx  # noqa: E402


def capture(name: str, payload: object) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"  captured tests/e2e/sandbox_evidence/{name}")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# The fiat `autoPayout`, minimal: `[rail, recipient, purpose]` are what the DTO
# requires and nothing optional is sent — no documents, no markup. The recipient
# is the one the 2026-09-01 probe used, so a refusal here is comparable with the
# refusal captured then rather than being a fresh argument about SEPA fields.
AUTO_PAYOUT = {
    "rail": "sepa",
    "purpose": "payment_for_goods_or_services",
    "recipient": {
        "type": "BUSINESS",
        "legalName": "ZZZTEST Console SBX Recipient GmbH",
        "iban": "DE89370400440532013000",
        "bic": "MOCKDEFFXXX",
        "bankName": "Sandbox Mock Bank",
        "postalAddress": {
            "addressLine1": "ZZZTEST Strasse 1",
            "city": "Berlin",
            "postalCode": "10115",
            # The match rule: the recipient's country is the quoted
            # `destinationCountry`, or the leg is refused for the wrong reason.
            "country": COUNTRY,
        },
        "bankAddress": {
            "addressLine1": "123 Sandbox Way",
            "city": "Testville",
            "postalCode": "00000",
            "country": COUNTRY,
        },
    },
}


async def main() -> int:
    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )
    print(f"\ncomposite convert-and-pay probe — host {SANDBOX_HOST}\n")
    print(f"  customer    {CUSTOMER}")
    print(f"  source      {SOURCE_ACCOUNT} (USD)")
    print(f"  destination {DESTINATION_ACCOUNT} (EUR)")

    # --- the quote half. Payout mode = different assets PLUS destinationCountry.
    # No `customerId` (quotes are org-scoped; a customer key is refused outright)
    # and no Idempotency-Key (this endpoint refuses one — the long-standing quirk).
    quote_body = {
        "source": {"code": "USD"},
        "destination": {"code": "EUR"},
        "destinationCountry": COUNTRY,
        "lockSide": "source",
        "amount": AMOUNT,
    }
    print(f"\n  POST /quotes — payout mode, USD→EUR/{COUNTRY}, {AMOUNT} USD")
    quoted = await api.post("/quotes", json=quote_body)
    print(f"  HTTP {quoted.status_code}")
    print("  " + quoted.text[:900].replace("\n", "\n  "))

    if quoted.status_code != 201:
        await api.aclose()
        print(
            "\n  *** QUOTE MODE CHANGED *** — payout mode priced this pair with a 201 on "
            "2026-09-01 and does not now. Nothing below this ran: re-read the quote guide "
            "before trusting either the hand-off copy or this probe's baseline.\n"
        )
        return 3

    quote = quoted.json() or {}
    capture(
        "composite_quote_payout_mode.json",
        {**quote, "_host": SANDBOX_HOST, "_observed_at": now(), "_probe": __doc__.splitlines()[0]},
    )
    options = quote.get("options") or []
    option = next((o for o in options if o.get("rail")), None)
    if option is None:
        await api.aclose()
        print(
            f"\n  *** QUOTE MODE CHANGED *** — {len(options)} option(s), none carrying a `rail`. "
            "A payout-mode option without a rail is not the product this probe redeems.\n"
        )
        return 3
    print(
        f"\n  option {option.get('id')} rail={option.get('rail')} "
        f"recipientAmount={(option.get('recipientAmount') or {}).get('amount')} "
        f"fees={[f.get('charge') for f in option.get('fees') or []]}"
    )

    # --- the redemption half. `destination` is REQUIRED even here.
    order_body = {
        "quoteOptionId": option.get("id"),
        "source": {"type": "virtual_account", "id": SOURCE_ACCOUNT},
        "destination": {"type": "virtual_account", "id": DESTINATION_ACCOUNT},
        "clientReferenceId": f"ZZZTEST-comp-{uuid.uuid4().hex[:8]}",
        "autoPayout": AUTO_PAYOUT,
    }
    print("\n  POST /orders — the same option, redeemed with a fiat autoPayout")
    created = await api.post(
        "/orders", json=order_body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    print(f"  HTTP {created.status_code}")
    print("  " + created.text[:1200].replace("\n", "\n  "))
    body = created.json() if created.text else {}
    capture(
        "composite_order_autopayout_refused.json",
        {
            "_host": SANDBOX_HOST,
            "_observed_at": now(),
            "_probe": "redeeming a payout-mode option with autoPayout on a fiat->fiat pair",
            "request": {"method": "POST", "path": "/v2/orders", "body": order_body},
            "response": {"status": created.status_code, "body": body},
        },
    )

    if created.status_code < 300:
        order_id = body.get("id") or ""
        print(f"\n  order {order_id} was CREATED — cancelling it; it is not executed here")
        cancelled = await api.post(
            f"/orders/{order_id}/cancel", headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        print(f"  cancel: HTTP {cancelled.status_code}")
        await api.aclose()
        print(
            "\n  *** ACCEPTED *** — fiat→fiat chaining has opened. `autoPayout` on a "
            "fiat conversion was 422 INVALID_ORDER_COMBO on 2026-09-01 and is not now. "
            "The composite this makes buildable means the hand-off copy on the payout "
            "page is no longer the only honest shape. Report the order id above and "
            "whether the cancel took "
            "before anything is built on it.\n"
        )
        return 2

    kind = str(body.get("type") or "")
    detail = str(body.get("detail") or "")
    if created.status_code == 422 and kind == BASELINE:
        await api.aclose()
        print(
            "\n  BASELINE UNCHANGED — the composite is still refused, no order exists, "
            "nothing to clean up. The payout page's guided hand-off (convert first, then "
            "pay — two operations, two settlements) is still the honest shape.\n"
        )
        return 0
    await api.aclose()
    print(
        f"\n  *** UNEXPECTED *** HTTP {created.status_code} / type {kind!r}. Neither the "
        f"recorded refusal ({BASELINE}) nor an acceptance:\n    {detail}\n  Report it before "
        "changing the console's copy in either direction — a refusal for a NEW reason says "
        "nothing about whether the composite has opened.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
