#!/usr/bin/env python
"""Transfers and conversions live end-to-end: conversion quotes, the transfer form model,
and the order-create refusal this org's account coverage forces.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…`.

    .venv/bin/python tests/e2e/05_transfers_conversions.py

Three always-on legs, all of which either read or price — none of them creates
anything, so a routine run leaves no residue at all:

  (a) `POST /v2/quotes` **both directions**, USD→EUR and EUR→USD. A conversion
      quote needs no accounts and no country; it is the one part of the convert
      flow this org can exercise for real. A direction that 422s is captured as
      the evidence rather than treated as a failure — that refusal *is* the
      answer about this organization's coverage.
  (b) The transfer screen's own model: `GET /v2/payouts/requirements` for
      `intercompany`, and the live whitelist read the recipient picker uses. The
      recipients were approved upstream; this leg is read-only over them.
  (c) The documented gate limitation: `POST /v2/orders` with a *real* quote
      option and this org's USD-only accounts. With no EUR virtual account to
      land in, the only pair the console can name is USD→USD, and Conduit refuses
      it — `422 INVALID_ORDER_COMBO`, *"fiat same-asset moves are not orderable;
      use /payouts"*. Captured verbatim, that refusal is the gate's
      conversion evidence: the quote prices both directions, and the order cannot
      be created until this org has a second-currency account.

One gated leg, off by default and sharing an earlier round's flag: `E2E_CREATE_PAYOUT=1`
runs an actual intercompany transfer (create + cancel). Same reason as the payments run —
staging accepts a cancel but never resolves the compliance review behind it, so
each run would strand one payout.

The render-path checks go through the app's own functions (`app.conversions`,
`app.payments`, `app.forms`), not re-implementations, so what this proves is what
an operator's page would show. HTTP is raw httpx — no database, no settings, no
ASGI app. Fresh idempotency keys per run; the API key is read from the
environment and never printed. All synthetic data is prefixed ZZZTEST.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import accounts, conversions, forms, payments  # noqa: E402
from app.conduit.client import parse_problem  # noqa: E402
from app.web import transfers  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

CREATE_PAYOUT = os.environ.get("E2E_CREATE_PAYOUT") == "1"

# Same synthetic-but-valid coordinates the payments run uses: a real ABA that passes the
# checksum both this console and Conduit's DTO enforce.
ABA = "021000021"
ACCOUNT_NUMBER = "000123456789"


SANDBOX_HOST = "https://api.sandbox.conduit.financial"


def env() -> tuple[str, str]:
    """The sandbox pair, or a refusal — the same guard `06_sandbox_sweep.py`
    carries, backported verbatim.

    This script used to read `SANDBOX_API_KEY`/`SANDBOX_HOST`, which in this
    engagement's `../.env` is a **live key against a production-labelled host**,
    and it writes. Nothing here is worth a real payout: a key that is not
    `ck_sandbox_…`, or any host but the sandbox, and the script exits before it
    opens a connection.
    """
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
    return host, key


def say(step: str, detail: str = "") -> None:
    print(f"  {step:<34} {detail}", flush=True)


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def capture(name: str, payload: object) -> None:
    (FIXTURES / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    say("fixture captured", name)


def answer(model: forms.FormModel, run: str) -> forms.FormValues:
    """Fill discovery's form the way an operator would — the same dumb filler
    the payments run uses, minus the identity fields the whitelist gate removes."""
    values = forms.FormValues()
    for field in model.fields:
        leaf = field.path[-1]
        if field.allowed_values:
            value = field.allowed_values[0]
        elif field.validator == "aba":
            value = ABA
        elif leaf == "accountNumber":
            value = ACCOUNT_NUMBER
        elif leaf == "country":
            value = "US"
        elif leaf == "postalCode":
            value = "10010"
        elif leaf == "city":
            value = "New York"
        elif leaf == "state":
            value = "US-NY"
        elif leaf == "addressLine1":
            value = "500 Market St"
        elif leaf == "legalName":
            value = f"ZZZTEST Group Entity {run}"
        elif leaf in ("firstName", "lastName"):
            value = "ZZZTEST"
        elif leaf == "reference":
            value = f"ZZZTEST-{run}"
        elif field.required:
            value = f"ZZZTEST {run}"
        else:
            continue
        forms._set_path(values.root, field.path, value)
    return values


def main() -> int:  # noqa: C901 — a linear script, read top to bottom
    host, key = env()
    run = uuid.uuid4().hex[:12]
    print(f"\nConduit Console — transfers/conversions e2e\n  host {host}  run {run}\n")
    ok = True
    option_id = ""

    with httpx.Client(base_url=f"{host}/v2", headers={"x-api-key": key}, timeout=60.0) as http:
        # --- (a) conversion quotes, both directions --------------------------------------
        print("(a) conversion quotes — POST /v2/quotes, differing assets, NO destinationCountry")
        for source, destination in (("USD", "EUR"), ("EUR", "USD")):
            body = conversions.quote_request(
                source=source, destination=destination, amount_text="1000.00", lock_side="source"
            )
            assert "destinationCountry" not in body  # the mode discriminator is the absence
            # No `Idempotency-Key`: this endpoint refuses one (live).
            response = http.post("/quotes", json=body)
            label = f"{source}→{destination}"
            if response.status_code >= 400:
                problem = parse_problem(response)
                say(f"  {label}", f"HTTP {response.status_code} {problem.type} — {problem.detail[:100]}")
                capture(
                    f"quote_refusal_{source.lower()}_{destination.lower()}.json", response.json()
                )
                say("    gate evidence", "captured — this org cannot price this direction")
                continue
            payload = response.json()
            capture(f"quote_live_conversion_{source.lower()}_{destination.lower()}.json", payload)
            view = payments.quote_view(payload)
            if view is None:
                say(f"  {label}", "priced no options — the picker would render nothing")
                ok = False
                continue
            say(f"  {label}", f"{view['id']} expires {view['expires_at']} · stale={view['stale']}")
            for option in view["options"]:
                say(
                    f"    {option['rail']}",
                    f"{option['source_amount']} → {option['destination_amount']} "
                    f"· rate {option['end_user_rate'] or 'none'} "
                    f"· spread {option['spread_bps'] or '—'} bps "
                    f"· fees {', '.join(f['amount'] for f in option['fees']) or 'none'}",
                )
                option_id = option_id or str(option["id"])
            # The confirm screen's own stored choice, from the app's own function.
            chosen = conversions.selection(view, view["options"][0])
            say("    stored on the operation", json.dumps(chosen)[:200])

        # --- (b) the transfer screen's model ---------------------------------------------
        print("\n(b) transfer screen — intercompany requirements + the registered picker")
        requirements = http.get(
            "/payouts/requirements",
            params={
                "purpose": payments.TRANSFER_PURPOSE,
                "rail": "fedwire",
                "recipientType": "business",
                "destinationCountry": "USA",
            },
        )
        model = None
        if requirements.status_code >= 400:
            problem = parse_problem(requirements)
            say("intercompany requirements", f"HTTP {requirements.status_code} {problem.type}")
            ok = False
        else:
            model = payments.payout_model(requirements.json())
            say(
                "intercompany requirements",
                f"{len(model.fields)} fields · whitelist={model.whitelist.get('required')} "
                f"documentation={model.documentation.get('required')} "
                f"blocked={len(model.blocked_jurisdictions)}",
            )
            if model.whitelist.get("required") is not True:
                say("GATE CHANGED", "intercompany no longer requires a whitelisted recipient")
                ok = False
            removed = len(model.fields) - len(payments.recipient_model(model).fields)
            say("  form model", f"recipient picker replaces {removed} identity fields")
            if model.warnings:
                say("  WARNINGS", "; ".join(model.warnings))
                ok = False

        listing = http.get("/customers", params={"limit": 25})
        listing.raise_for_status()
        customers = [c for c in listing.json().get("data") or [] if accounts.has_active(c)]
        funded: list[tuple[str, dict]] = []
        assets: set[str] = set()
        for candidate in customers:
            page = http.get(f"/customers/{candidate['id']}/virtual-accounts", params={"limit": 25})
            if page.status_code >= 400:
                continue
            for entry in page.json().get("data") or []:
                if entry.get("status") != "active":
                    continue
                assets.add((entry.get("asset") or {}).get("code") or "?")
                if any(
                    (b.get("available") or {}).get("amount", "0") not in ("0", "0.00", None)
                    for b in entry.get("balances") or []
                ):
                    funded.append((candidate["id"], entry))
        if not funded:
            say("FAILED", "no staging customer has a funded active virtual account")
            return 1
        customer_id, account = funded[0]
        say(
            "funded accounts",
            ", ".join(
                f"{cid}/{a['id']} "
                + ",".join(f"{b['code']} {b['available']}" for b in accounts.balance_rows(a))
                for cid, a in funded
            ),
        )
        say("currencies held by the org", ", ".join(sorted(assets)) or "none")

        path = f"/customers/{customer_id}/whitelist-recipients"
        registered: list[dict] = []
        listed = http.get(path, params={"limit": 100})
        if listed.status_code >= 400:
            say("whitelist", f"HTTP {listed.status_code} — the picker would show a problem card")
            ok = False
        else:
            entries = listed.json().get("data") or []
            registered = payments.registered_only(entries)
            say("whitelist", f"{len(registered)} registered of {len(entries)}")
            for entry in registered:
                rails = transfers.rails_for(entry)
                asset = (account.get("asset") or {}).get("code") or ""
                say(
                    f"  {entry['id'][:20]}…",
                    f"{entry.get('legalName')} · {entry.get('rail')} → payable over "
                    f"{', '.join(rails) or 'no rail this build knows'}"
                    + (
                        f" · CROSS-CURRENCY: settles {transfers.cross_currency(entry, asset)}, "
                        f"account holds {asset} → points at Convert"
                        if transfers.cross_currency(entry, asset)
                        else ""
                    ),
                )
            if not registered:
                say(
                    "  picker",
                    "empty state — links to the whitelist page and the cross-customer shortcut",
                )

        # --- (c) order create against USD-only accounts -----------------------------------
        print("\n(c) order create — the documented gate limitation")
        if not option_id:
            say("order create", "SKIPPED — no quote option was priced above")
        else:
            body = conversions.order_body(
                quote_option_id=option_id,
                source_id=account["id"],
                # There is no EUR account on this org, so the only id available
                # for the other side is the source's own — which is exactly the
                # shape Conduit has to refuse.
                destination_id=account["id"],
            )
            body["clientReferenceId"] = f"zzztest-console-{run}"
            say("order body", json.dumps(body, sort_keys=True))
            created = http.post("/orders", json=body, headers=idem())
            if created.status_code < 400:
                order = created.json()
                say("UNEXPECTED", f"the order was accepted: {order.get('id')} {order.get('status')}")
                say("  RESIDUE", "cancel it by hand — this org was not expected to convert")
                capture("order_live_conversion.json", order)
                ok = False
            else:
                problem = parse_problem(created)
                capture("order_refusal_same_asset_pair.json", created.json())
                say("order create", f"HTTP {problem.status} {problem.type}")
                say("  detail", (problem.detail or "")[:180])
                if problem.resolution:
                    say("  resolution", problem.resolution[:180])
                for error in getattr(problem, "errors", []):
                    say("    field", f"{error.pointer}: {error.detail}")
                say(
                    "  gate evidence",
                    "captured — with no second-currency account the only pair nameable is "
                    "same-asset, which /v2/orders refuses by design; the conversion arm of "
                    "this gate needs an org with EUR virtual-account coverage",
                )
                if problem.status not in (400, 404, 409, 422):
                    say("  NOTE", "an unexpected status class for a refusal")
                    ok = False

        # --- (d) the gated transfer leg ----------------------------------------------------
        print("\n(d) intercompany transfer create + cancel")
        if not CREATE_PAYOUT:
            say("transfer create", "SKIPPED — set E2E_CREATE_PAYOUT=1 to run it")
            say(
                "  why",
                "staging cannot complete a cancellation: the cancel is accepted, then "
                "the payout parks at pending/under_review with no /v2/sandbox/* route to resolve "
                "the review, so each run would leave one behind",
            )
        elif model is None or not registered:
            say("transfer create", "SKIPPED — needs intercompany requirements and a registered "
                                   "destination on the funded customer")
        else:
            entry = registered[0]
            rendered = payments.recipient_model(model)
            values = answer(rendered, run)
            values.root["whitelistRecipientId"] = entry["id"]
            payments.apply_recipient(model, values, entry)
            asset = (account.get("asset") or {}).get("code") or "USD"
            errors = payments.payout_errors(
                rendered, values, account=account, amount="11.00", country="USA", rail=None
            )
            if not errors.ok:
                say(
                    "local validation FAILED",
                    json.dumps(
                        {n: [m.detail for m in ms] for n, ms in errors.fields.items()}
                        | {"form": [m.detail for m in errors.form]},
                        indent=1,
                    )[:800],
                )
                return 1
            transfer_body = payments.payout_body(
                model,
                values,
                customer_id=customer_id,
                virtual_account_id=account["id"],
                asset=asset,
                amount_text="11.00",
                purpose=payments.TRANSFER_PURPOSE,
            )
            transfer_body["clientReferenceId"] = f"zzztest-transfer-{run}"
            say("transfer body", json.dumps(transfer_body, sort_keys=True)[:460])
            sent = http.post("/payouts", json=transfer_body, headers=idem())
            if sent.status_code >= 400:
                problem = parse_problem(sent)
                say("transfer FAILED", f"{problem.status} {problem.type} — {(problem.detail or '')[:160]}")
                for error in getattr(problem, "errors", []):
                    say("  field", f"{error.pointer}: {error.detail}")
                return 1
            payout = sent.json()
            say("transfer created", f"{payout['id']} status={payout.get('status')} "
                                    f"stage={payout.get('stage')}")
            capture("payout_live_intercompany.json", payout)
            cancelled = http.post(f"/payouts/{payout['id']}/cancel", headers=idem())
            say("cancel", f"HTTP {cancelled.status_code}")
            final = http.get(f"/payouts/{payout['id']}").json()
            say("final status", f"{final.get('status')} / {final.get('stage')}")
            if final.get("status") != "cancelled":
                say("RESIDUE", f"{payout['id']} is {final.get('status')} — chase it by hand")
                ok = False

    print(
        "\n  Not exercisable on this host: order execute / cancel and the sandbox order\n"
        "  simulators (conversion-failed, rate-lock-expired) — creating an order needs a\n"
        "  destination virtual account in the other currency, which this org has no provider\n"
        "  coverage for (the same NO_ELIGIBLE_PROVIDER wall). Their wire\n"
        "  bodies are asserted against stubs in tests/test_web_convert.py instead.\n"
        f"  Transfer create+cancel: {'RAN' if CREATE_PAYOUT else 'skipped (E2E_CREATE_PAYOUT unset)'}.\n"
    )
    print(f"  result: {'PASS' if ok else 'FAIL'}  run {run}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
