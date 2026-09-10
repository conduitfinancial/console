#!/usr/bin/env python
"""Accounts live end-to-end: accounts, deposit instructions, and the EUR refusal.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…`. Exactly one
write is made — a `virtual_account` feature request — and it is cancelled before
the script exits, so the run leaves no residue.

    .venv/bin/python tests/e2e/03_accounts.py

The render-path checks go through the app's own functions (`app.accounts`,
`app.forms`, `app.conduit.client.parse_problem`), not through re-implementations
here: what this proves is what an operator's page would show. HTTP is raw httpx
so no database, no settings and no ASGI app are needed to talk to staging.

Fresh idempotency keys per run; the API key is read from the environment and
never printed.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import accounts, forms  # noqa: E402
from app.conduit.client import parse_problem  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


SANDBOX_HOST = "https://api.sandbox.conduit.financial"


def _env_file() -> Path:
    """The engagement's `.env` — `ROOT`'s own, or the main checkout's.

    A git worktree's root carries no copy, so the search walks up; but only a
    worktree does, and only as far as the first ancestor owning a `.git`
    directory. It used to walk to `/` and take the first hit, and `env()`'s
    guards below cannot catch that: they prove the key is shaped `ck_sandbox_…`
    and the host is the sandbox, never *whose* sandbox. Another engagement's
    `.env` passes both, and this script writes — accounts, deposits — into that
    organisation.

    Nothing names the file from outside, deliberately: an environment variable
    is the same wrong-organisation risk with an easier trigger, since a stale
    one is inherited rather than found.
    """
    if (ROOT / ".env").is_file():
        return ROOT / ".env"
    if not (ROOT / ".git").is_dir():
        for parent in ROOT.parents:
            if (parent / ".git").is_dir():
                if (parent / ".env").is_file():
                    return parent / ".env"
                break
    sys.exit("refusing to run: no .env at the repository root or at its checkout")


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
    for line in _env_file().read_text().splitlines():
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


# A 300-byte file that really is a PDF — `app.documents.sniff` and Conduit both
# decide the type from the bytes, not the name.
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def answer(model: forms.FormModel, asset: str) -> forms.FormValues:
    """Fill the feature form the way an operator would.

    Script-local, deliberately dumb: the currency that was picked, `true` for
    the certification the customer has to give, `false` for the regulatory
    history questions. Anything else takes discovery's first allowed value.
    """
    values = forms.FormValues()
    for field in forms.active_fields(model, values):
        if field.path == accounts.ASSET_PATH:
            forms._set_path(values.root, field.path, asset)
        elif field.effective_type == "boolean":
            forms._set_path(values.root, field.path, field.path[0] == "certification")
        elif field.allowed_values:
            forms._set_path(values.root, field.path, field.allowed_values[0])
        elif field.required:
            forms._set_path(values.root, field.path, "ZZZTEST console e2e")
    return values


def main() -> int:  # noqa: C901 — a linear script, read top to bottom
    host, key = env()
    run = uuid.uuid4().hex[:12]
    print(f"\nConduit Console — accounts live e2e\n  host {host}  run {run}\n")
    ok = True

    with httpx.Client(base_url=f"{host}/v2", headers={"x-api-key": key}, timeout=60.0) as http:
        # 1. customers, split by what Conduit says their features are.
        listing = http.get("/customers", params={"limit": 25})
        listing.raise_for_status()
        customers = listing.json().get("data") or []
        funded = [c for c in customers if accounts.has_active(c)]
        unfunded = [c for c in customers if not accounts.has_active(c)]
        say("customers", f"{len(customers)} on page one — {len(funded)} with an active "
                         f"virtual_account, {len(unfunded)} without")
        if not funded:
            say("FAILED", "no customer on page one has an active virtual_account")
            return 1
        customer = funded[0]
        customer_id = customer["id"]
        rows = accounts.feature_rows(customer)
        say("feature rows", ", ".join(f"{r['feature']}={'active' if r['is_active'] else 'off'}"
                                      f"{'' if r['supported'] else ' (inert)'}" for r in rows))

        # 2. the accounts list, then one account in full.
        page = http.get(f"/customers/{customer_id}/virtual-accounts", params={"limit": 25})
        page.raise_for_status()
        listed = page.json().get("data") or []
        say("virtual accounts", ", ".join(
            f"{a.get('id')} {a.get('asset', {}).get('code')} {a.get('status')}" for a in listed
        ) or "none")
        if not listed:
            say("FAILED", f"{customer_id} advertises the feature but lists no accounts")
            return 1
        active = next((a for a in listed if a.get("status") == "active"), listed[0])
        detail = http.get(f"/customers/{customer_id}/virtual-accounts/{active['id']}")
        detail.raise_for_status()
        account = detail.json()

        # 3. the render path: the card an operator copies coordinates out of.
        cards = accounts.deposit_cards(account)
        balances = accounts.balance_rows(account)
        say("deposit instructions", ", ".join(
            f"{c['type']}({len(c['rows'])} rows{'' if c['known'] else ', UNKNOWN TYPE'})"
            for c in cards
        ) or "none published")
        say("balances", ", ".join(
            f"{b['code']} avail {b['available']} pending {b['pending']}" for b in balances
        ) or "none")
        if not cards or not all(c["rows"] for c in cards):
            say("FAILED", "an active account published no copyable deposit coordinates")
            ok = False
        for card in cards:
            labels = [r["label"] for r in card["rows"]]
            say(f"  {card['type']} rows", ", ".join(labels))
            if not card["known"]:
                say("  NOTE", "instruction variant unknown to this build — rendered generically")

        target = FIXTURES / f"virtual_account_live_{(account.get('asset') or {}).get('code', 'x').lower()}.json"
        target.write_text(json.dumps(account, indent=2, sort_keys=True) + "\n")
        say("fixture captured", target.name)

        # 4. deposit history, narrowed locally (there is no per-account filter).
        history = http.get(
            "/transactions",
            params={"customerId": customer_id, "type": "deposit", "limit": 25},
        )
        if history.status_code >= 400:
            say("deposit history", f"HTTP {history.status_code} {history.json().get('type')}")
        else:
            items = history.json().get("data") or []
            mine = accounts.deposits_for(items, account["id"])
            say("deposits", f"{len(mine)} into this account of {len(items)} for the customer"
                            + (f" — {accounts.amount_of(mine[0])} {mine[0].get('status')}" if mine else ""))

        # 5. THE CATALOG PROBE — the call the picker is actually built on.
        #    `asset` is omitted, which the pinned contract says resolves the
        #    feature over every eligible provider. This is the one fact the unit
        #    suite cannot prove: its stub answers whatever we told it to. If the
        #    unassetted response carries no `/asset/code` field, `allowed_assets`
        #    returns [] and the picker is empty on every customer, with no error
        #    to say why — so the failure is asserted here, live, rather than
        #    inferred from a sentence in the spec.
        catalog = http.get(
            f"/customers/{customer_id}/features/requirements",
            params={"type": "virtual_account"},
        )
        if catalog.status_code != 200:
            problem = parse_problem(catalog)
            say("catalog probe FAILED", f"HTTP {catalog.status_code} {problem.type}")
            ok = False
        else:
            offered = accounts.allowed_assets(catalog.json())
            say("catalog", f"unassetted discovery names {offered or 'NOTHING'}")
            if not offered:
                say("catalog probe FAILED",
                    "no /asset/code allowedValues on the unassetted response — "
                    "the picker would render empty for every customer")
                ok = False

        # 6. THE EUR PROBE — the gate's second arm.
        eur = http.get(
            f"/customers/{customer_id}/features/requirements",
            params={"type": "virtual_account", "asset": "EUR"},
        )
        if eur.status_code == 200:
            # EUR-scoped, so this is what the EUR providers allow — not the
            # picker, which comes from the unassetted call in step 6's note.
            allowed = accounts.allowed_assets(eur.json())
            say("EUR requirements", f"HTTP 200 — this org now has EUR coverage, assets {allowed}")
        else:
            problem = parse_problem(eur)
            say("EUR requirements", f"HTTP {eur.status_code} {problem.type}")
            # What `problem_view` hands `macros.html`: title, detail, resolution,
            # correlationId. All four have to be there or the operator gets a
            # blank refusal.
            missing = [
                name
                for name in ("title", "detail", "resolution", "correlation_id")
                if not getattr(problem, name)
            ]
            if problem.type != "NO_ELIGIBLE_PROVIDER" or missing:
                say("EUR probe FAILED", f"type={problem.type} missing={missing}")
                ok = False
            else:
                say("  renders as", f"{problem.title} · correlationId {problem.correlation_id}")
                say("  resolution", problem.resolution[:120] + "…")

        # 7. discovery for the currency we will actually request.
        usd = http.get(
            f"/customers/{customer_id}/features/requirements",
            params={"type": "virtual_account", "asset": "USD"},
        )
        usd.raise_for_status()
        snapshot = usd.json()
        model = forms.parse(snapshot)
        options = accounts.allowed_assets(snapshot)
        # `allowed_assets` is the schema's own list, verbatim — and this call
        # passed `asset=USD`, so it answers only for the providers that can hold
        # USD. The picker the route builds comes from an *unassetted* call,
        # which resolves over every eligible provider and can therefore be
        # wider than this; a per-currency refusal (EUR, probed above) is named
        # beside that picker rather than taken off it.
        say("USD requirements", f"schemaVersion {model.schema_version}, {len(model.fields)} fields, "
                                f"schema allows {options}, warnings={model.warnings or 'none'}")

        # 8. the one write. Prefer a customer with no active account: the request
        #    is then a first account rather than a second, and the cancel below
        #    leaves that customer exactly as it was found.
        requester = (unfunded or funded)[0]
        requester_id = requester["id"]
        if requester_id != customer_id:
            fresh = http.get(
                f"/customers/{requester_id}/features/requirements",
                params={"type": "virtual_account", "asset": "USD"},
            )
            if fresh.status_code >= 400:
                say("requester discovery", f"HTTP {fresh.status_code} — falling back to {customer_id}")
                requester_id, requester = customer_id, customer
            else:
                model = forms.parse(fresh.json())
        values = answer(model, "USD")
        if model.min_documents or any(d.min_count for d in model.documents):
            # Discovery asks this customer for evidence: the console's upload
            # widget posts it with `purpose=feature_request`, so this does too.
            upload = http.post(
                "/documents",
                files={"file": ("zzztest-console-e2e.pdf", PDF, "application/pdf")},
                data={"purpose": accounts.PURPOSE, "name": f"ZZZTEST feature evidence {run}"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            if upload.status_code >= 400:
                say("document upload FAILED", f"{upload.status_code} {upload.text[:200]}")
                return 1
            values.document_ids = [upload.json().get("id", "")]
            say("document uploaded", f"{values.document_ids[0]} "
                                     f"(minDocuments {model.min_documents})")
        errors = forms.validate(model, values)
        if not errors.ok:
            say("local validation FAILED", json.dumps(
                {name: [m.detail for m in messages] for name, messages in errors.fields.items()}
                | {"form": [m.detail for m in errors.form]}
                | {"documents": [m.detail for m in errors.documents]}, indent=1)[:800])
            return 1
        body = accounts.request_body(model, values)
        say("request body", json.dumps(body, sort_keys=True))

        submitted = http.post(
            f"/customers/{requester_id}/features",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if submitted.status_code >= 400:
            problem = parse_problem(submitted)
            say("feature request FAILED", f"{problem.status} {problem.type} — {problem.detail}")
            if problem.resolution:
                say("  resolution", problem.resolution[:160])
            return 1
        application = submitted.json()
        application_id = application["id"]
        say("feature request", f"{application_id} HTTP {submitted.status_code} "
                               f"status={application.get('status')} "
                               f"asset={(application.get('asset') or {}).get('code')}")

        # 9. it is in the dashboard's own list before anything else happens —
        #    and matchable by the reconciler's recipe (customer + type + asset).
        dashboard = http.get(
            "/applications",
            params={"customerId": requester_id, "sortBy": "createdAt", "sortOrder": "desc",
                    "limit": 25},
        )
        dashboard.raise_for_status()
        listed_apps = dashboard.json().get("data") or []
        found = next((a for a in listed_apps if a.get("id") == application_id), None)
        matchable = found is not None and found.get("type") == body["type"] and (
            found.get("asset") or {}
        ).get("code") == body["asset"]["code"]
        say("in applications list", f"{'yes' if found else 'NO'} "
                                    f"(reconciler-matchable: {matchable})")
        ok = ok and bool(found) and matchable

        # 10. clean up: cancel it, leaving the customer as it was found.
        cancelled = http.post(f"/applications/{application_id}/cancel")
        say("cancel", f"HTTP {cancelled.status_code}")
        final = http.get(f"/applications/{application_id}").json()
        say("final status", final.get("status", "?"))
        if final.get("status") != "cancelled":
            say("RESIDUE", f"{application_id} is {final.get('status')} — cancel it by hand")
            ok = False

    print(
        "\n  Not exercisable on this host: deposit simulation and account activation.\n"
        "  Staging has no /v2/sandbox/* routes (verified 2026-08-28), so the simulate panel's\n"
        "  wire body is asserted against a stub in tests/test_web_accounts.py instead.\n"
    )
    print(f"  result: {'PASS' if ok else 'FAIL'}  run {run}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
