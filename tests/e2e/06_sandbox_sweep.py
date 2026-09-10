#!/usr/bin/env python
"""Live sandbox sweep: the full money lifecycle through the real console app.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…`. The staging pair in `../.env`
(`SANDBOX_API_KEY`, a live key) is never read.

Unlike the staging scripts (02–05), which had to stop where staging's manual
compliance queue starts, the sandbox has `/v2/sandbox/*` simulators — so this
sweep drives the whole lifecycle end to end, and it drives it through the
*console's own ASGI app* wherever a page or form exists: what is asserted is
what an operator would actually see.

    onboarding submit → simulate approve → customer → deposit → recipient
    whitelist → simulate approve → payout quote → payout → review approve →
    settle → conversion quote — plus the rejection branch, the 422 branch,
    the idempotent-replay branch and the double-submit guard, live.

    .venv/bin/python tests/e2e/06_sandbox_sweep.py

Safe to re-run: fresh clientReferenceId and idempotency keys each run, all
records ZZZTEST-prefixed, all money fake by construction. Uses the local
`conduit_console_test` database (same as the unit suite) for the operations
ledger; each run leaves its ledger rows behind as evidence.

The API key is read from the environment and never printed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import html as html_mod
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"
EVIDENCE = Path(__file__).parent / "sandbox_evidence"


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

# Environment before any app import — Settings is cached at first read. The
# config's own allowlist pins CONDUIT_ENV=sandbox to SANDBOX_HOST, so there is
# no combination of these values that can reach staging or production.
import os  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402

os.environ["CONDUIT_ENV"] = "sandbox"
os.environ["CONDUIT_API_KEY"] = KEY
# The sweep gets its own database: sharing conduit_console_test with the unit
# suite means a concurrently running pytest truncates the ledger mid-request.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://mc_bot@/conduit_console_sweep?host=/tmp"
)
os.environ.setdefault("SESSION_SECRET", "sandbox-sweep-session")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("AUTH_MODE", "disabled")
os.environ.setdefault("CONDUIT_WEBHOOK_SECRET", "whsec_" + "a1b2c3d4" * 8)

import httpx  # noqa: E402

from app import accounts, forms, payments  # noqa: E402
from app.auth.providers import ProxyProvider  # noqa: E402
from app.conduit import ConduitClient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.onboarding.requirements import merge_policy_subjects  # noqa: E402
from tests import web_harness as harness  # noqa: E402

# The onboarding answer machinery from the phase-2 script — loaded by path
# (the filename starts with a digit), its env()/main() never called.
_spec = importlib.util.spec_from_file_location(
    "onboarding_e2e", Path(__file__).parent / "02_onboarding.py"
)
ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ob)

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, note: str = "") -> None:
    verdict = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((name, verdict, note))
    print(f"  [{verdict}] {name:<44} {note}", flush=True)


def capture(name: str, payload: object) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


IDENTIFIER_KEYS = ("taxId", "taxIdNumber", "registrationNumber")


def stamp_identifiers(node: object, seed: int) -> None:
    """Give every tax/registration identifier in the body run-unique digits —
    the sandbox 409s an onboarding whose tax id an approved customer holds."""
    digits = str(seed)[-10:].rjust(10, "7")
    if isinstance(node, dict):
        for key, value in node.items():
            if key in IDENTIFIER_KEYS and isinstance(value, str) and value:
                node[key] = digits
            else:
                stamp_identifiers(value, seed)
    elif isinstance(node, list):
        for item in node:
            stamp_identifiers(item, seed)


def flash_of(response: httpx.Response) -> str:
    """The console's redirect target — `HX-Redirect` for htmx callers (204),
    `location` for plain ones (303). Its query string carries the outcome."""
    return str(response.headers.get("hx-redirect") or response.headers.get("location", ""))


def redirected(response: httpx.Response) -> bool:
    return response.status_code in (204, 302, 303) and bool(flash_of(response))


def ok_redirect(response: httpx.Response) -> bool:
    return redirected(response) and "err=" not in flash_of(response)


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")


async def poll(fn, want, *, tries: int = 20, delay: float = 1.0):
    """Re-read until `want(value)` or the tries run out; returns the last value."""
    value = None
    for _ in range(tries):
        value = await fn()
        if want(value):
            return value
        await asyncio.sleep(delay)
    return value


async def main() -> int:
    run = uuid.uuid4().hex[:10]
    print(f"\nConduit Console — live sandbox sweep\n  host {SANDBOX_HOST}  run {run}\n")
    migrate()

    app = create_app()
    app.state.conduit = ConduitClient()  # the real sandbox, via Settings
    app.state.auth_provider = ProxyProvider(harness.PROXY)

    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )

    async with harness.signed_in(app) as web:
        # ---- A. onboarding: discovery → assembly → 422 → submit → idempotent replay
        print("(A) onboarding, raw API")
        requirements = (await api.get("/onboarding/requirements", params={"country": "BGR"})).json()
        for axis in ("INDUSTRY", "REGULATED_ACTIVITY"):
            catalog = await api.get("/onboarding/policy-subjects", params={"axis": axis})
            merge_policy_subjects(requirements, catalog.json().get("data") or [])
        model = forms.parse(requirements)
        record("discovery", bool(model.fields), f"{len(model.fields)} fields")

        upload = await api.post(
            "/documents",
            files={"file": ("zzztest-sweep.pdf", ob.PDF, "application/pdf")},
            data={"purpose": "organization_onboarding", "name": "ZZZTEST sweep evidence"},
            headers=idem(),
        )
        document_id = upload.json().get("id", "") if upload.status_code < 400 else ""
        record("document upload", upload.status_code == 201 or bool(document_id), document_id)

        values = ob.answer(model, document_id)
        body = forms.assemble(model, values)
        body["businessInfo"]["legalName"] = f"ZZZTEST Sweep {run} EOOD"
        body["clientReferenceId"] = f"zzztest-sweep-{run}"
        # The sandbox refuses onboarding a tax identifier an approved customer
        # already holds (409 CUSTOMER_ALREADY_ONBOARDED) — so every application
        # this sweep submits carries its own.
        stamp_identifiers(body, uuid.uuid4().int)

        # The 422 contract: a required section missing must come back as a
        # ValidationErrorDto with pointers, not a 500 and not a silent accept.
        incomplete = {k: v for k, v in body.items() if k != "ownership"}
        response = await api.post("/onboarding", json=incomplete, headers=idem())
        shape = response.status_code == 422 and isinstance(response.json().get("errors"), list)
        record("incomplete submit → 422 + pointers", shape, f"HTTP {response.status_code}")
        if shape:
            capture("onboarding_422.json", response.json())

        key = idem()
        submitted = await api.post("/onboarding", json=body, headers=key)
        application = submitted.json() if submitted.status_code < 500 else {}
        application_id = application.get("id", "")
        record(
            "submit application",
            submitted.status_code in (200, 201, 202) and bool(application_id),
            f"HTTP {submitted.status_code} {application_id}",
        )
        if not application_id:
            capture("onboarding_submit_failure.json", application)
            print(json.dumps(application, indent=2)[:1500])
            return await finish(api)

        # Same body, same Idempotency-Key: the API must answer with the same
        # application, not create a twin.
        replay = await api.post("/onboarding", json=body, headers=key)
        record(
            "idempotent replay → same application",
            replay.status_code < 400 and replay.json().get("id") == application_id,
            f"HTTP {replay.status_code} {replay.json().get('id', '?')}",
        )

        # ---- B. the console over the pending application, then simulate approve
        print("(B) application pages + simulated decision, console")
        page = await web.get("/applications")
        record(
            "console: applications list renders",
            page.status_code == 200 and application_id in page.text,
            f"HTTP {page.status_code}",
        )
        detail = await web.get(f"/applications/{application_id}")
        record("console: application detail renders", detail.status_code == 200)

        sim = await harness.post(
            web,
            f"/applications/{application_id}/simulate",
            harness.form(outcome="approved"),
        )
        record(
            "console: simulate approve accepted",
            ok_redirect(sim),
            flash_of(sim)[:90],
        )

        async def read_app():
            return (await api.get(f"/applications/{application_id}")).json()

        current = await poll(read_app, lambda a: a.get("status") not in ("pending", "in_review"))

        async def find_customer():
            page = await api.get("/customers", params={"limit": 100})
            rows = page.json().get("data") or []
            return next((c for c in rows if c.get("applicationId") == application_id), {})

        customer = await poll(find_customer, lambda c: bool(c), tries=10)
        customer_id = customer.get("id") or ""
        record(
            "application approved → customer exists",
            current.get("status") == "approved" and bool(customer_id),
            f"status={current.get('status')} customer={customer_id}",
        )
        capture("application_approved.json", current)
        if not customer_id:
            return await finish(api)

        # The rejection branch: a second application, simulated rejected with a
        # category+field, rendered by the console.
        # First, the conflict contract: the same tax id again must 409 with the
        # existing customer named, not create a twin.
        dup = await api.post("/onboarding", json=body | {"clientReferenceId": f"zzztest-dup-{run}"}, headers=idem())
        dup_body = dup.json() if dup.status_code < 500 else {}
        record(
            "duplicate taxId → 409 names existing customer",
            dup.status_code == 409
            and dup_body.get("type") == "CUSTOMER_ALREADY_ONBOARDED"
            and dup_body.get("details", {}).get("customerId") == customer_id,
            f"HTTP {dup.status_code} {dup_body.get('type', '?')}",
        )

        body2 = json.loads(json.dumps(body))
        body2["businessInfo"]["legalName"] = f"ZZZTEST Sweep-R {run} EOOD"
        body2["clientReferenceId"] = f"zzztest-sweep-r-{run}"
        stamp_identifiers(body2, uuid.uuid4().int)
        second = await api.post("/onboarding", json=body2, headers=idem())
        second_id = second.json().get("id", "") if second.status_code < 400 else ""
        if second_id:
            sim = await harness.post(
                web,
                f"/applications/{second_id}/simulate",
                # `reason` is refused by the real sandbox (400 Unrecognized key,
                # verified 2026-08-28) even though the panel offers it — a
                # console defect this sweep found; see the findings report.
                harness.form(outcome="rejected", category="document_mismatch", field="tax_id"),
            )

            async def read_second():
                return (await api.get(f"/applications/{second_id}")).json()

            rejected = await poll(read_second, lambda a: a.get("status") == "rejected")
            page = await web.get(f"/applications/{second_id}")
            record(
                "rejection branch: status + console render",
                rejected.get("status") == "rejected" and page.status_code == 200,
                f"status={rejected.get('status')} resubmittable={rejected.get('resubmittable')}",
            )
            capture("application_rejected.json", rejected)
        else:
            record("rejection branch", None, f"second submit HTTP {second.status_code}")

        # Replay the decision on the already-approved application — the panel's
        # contract says an idempotent 200, not a conflict.
        sim = await harness.post(
            web, f"/applications/{application_id}/simulate", harness.form(outcome="approved")
        )
        record(
            "replayed decision → 409 surfaced, not crash",
            redirected(sim) and "err=" in flash_of(sim),
            flash_of(sim)[:90],
        )

        # ---- C. customer + virtual account + simulated deposit
        print("(C) customer, account, deposit — console")
        page = await web.get(f"/customers/{customer_id}")
        record("console: customer page renders", page.status_code == 200 and customer_id in page.text)

        result = await accounts.fetch_accounts(app.state.conduit, customer_id)
        items = getattr(result, "items", []) or []
        account = next((a for a in items if a.get("status") == "active"), None)
        if account is None and items:
            account = items[0]
        if account is None:
            # No account yet: request one through the console's feature form.
            page = await web.get(f"/customers/{customer_id}/request-account")
            response = await harness.post(
                web,
                f"/customers/{customer_id}/request-account",
                harness.form(f__asset__code="USD", intent=harness.minted_intent()),
            )
            record("request USD account", ok_redirect(response), flash_of(response)[:110])

            # The confirmed feature request is an application of its own —
            # nothing materialises until it too is simulated approved.
            feature_app = ""
            match = re.search(r"app_[A-Za-z0-9]+", flash_of(response))
            if match:
                feature_app = match.group(0)
            else:
                listing = await api.get("/applications", params={"limit": 20})
                feature_app = next(
                    (
                        a["id"]
                        for a in (listing.json().get("data") or [])
                        if a.get("type") != "customer_onboarding"
                        and a.get("status") == "pending"
                    ),
                    "",
                )
            if feature_app:
                sim = await harness.post(
                    web,
                    f"/applications/{feature_app}/simulate",
                    harness.form(outcome="approved"),
                )
                decided = ok_redirect(sim) or "Already+Decided" in flash_of(sim)
                record("feature application approved", decided, feature_app)

            async def read_accounts():
                page = await accounts.fetch_accounts(app.state.conduit, customer_id)
                return getattr(page, "items", []) or []

            # Existence is not usability: a fresh account sits in
            # pending_activation first, and a deposit against it is refused.
            items = await poll(
                read_accounts,
                lambda i: any(a.get("status") == "active" for a in i),
                tries=30,
            )
            account = next((a for a in items if a.get("status") == "active"), None)
        if account is None:
            record("virtual account", False, "no account materialised")
            return await finish(api)
        account_id = account.get("id", "")
        record("virtual account", True, f"{account_id} status={account.get('status')}")

        page = await web.get(f"/customers/{customer_id}/accounts/{account_id}")
        record("console: account page renders", page.status_code == 200)

        sim = await harness.post(
            web,
            f"/customers/{customer_id}/accounts/{account_id}/simulate-deposit",
            harness.form(amount="250.00", code="USD"),
        )
        record(
            "console: simulate USD deposit",
            ok_redirect(sim),
            flash_of(sim)[:90],
        )

        async def read_account():
            return await accounts.fetch_account(app.state.conduit, customer_id, account_id)

        def available(account_body) -> float:
            total = 0.0
            for b in (account_body or {}).get("balances") or []:
                total += float(((b.get("available") or {}).get("amount")) or 0)
            return total

        funded = await poll(
            read_account, lambda a: isinstance(a, dict) and available(a) > 0, tries=20
        )
        record(
            "deposit lands in available balance",
            isinstance(funded, dict) and available(funded) > 0,
            f"available={available(funded) if isinstance(funded, dict) else '?'} USD",
        )
        capture("account_funded.json", funded if isinstance(funded, dict) else {})

        # ---- D. whitelist recipient: register via console form, simulate approve
        print("(D) whitelist recipient — console")
        response = await harness.post(
            web,
            f"/customers/{customer_id}/recipients",
            harness.form(
                rail="us",
                f__routingNumber="021000021",
                f__accountNumber="000123456789",
                f__relationship="self",
                f__legalName=f"ZZZTEST Sweep {run} EOOD",
                f__label="ZZZTEST sweep recipient",
                documentIds=document_id,
                intent=harness.minted_intent(),
            ),
        )
        recipients = await payments.fetch_recipients(app.state.conduit, customer_id)
        entry = next(
            (
                i
                for i in (getattr(recipients, "items", []) or [])
                if i.get("label") == "ZZZTEST sweep recipient"
                or str(i.get("legalName", "")).startswith(f"ZZZTEST Sweep {run}")
            ),
            None,
        )
        recipient_id = (entry or {}).get("id", "")
        record(
            "register us recipient",
            response.status_code < 400 and bool(recipient_id),
            f"{recipient_id} status={(entry or {}).get('status')}",
        )

        if recipient_id:
            sim = await harness.post(
                web,
                f"/customers/{customer_id}/recipients/{recipient_id}/simulate",
                harness.form(outcome="approve"),
            )

            async def read_recipients():
                page = await payments.fetch_recipients(app.state.conduit, customer_id)
                return next(
                    (i for i in (getattr(page, "items", []) or []) if i.get("id") == recipient_id),
                    {},
                )

            entry = await poll(read_recipients, lambda e: e.get("status") == "registered")
            record(
                "simulate whitelist approve → registered",
                entry.get("status") == "registered",
                f"status={entry.get('status')}",
            )
            capture("recipient_registered.json", entry)

        # ---- E. payout: quote → create → double-submit guard → review → settle
        print("(E) payout lifecycle — console")
        route = {
            "purpose": "intercompany",
            "rail": "fedwire",
            "recipientType": "business",
            "destinationCountry": "USA",
        }
        quote = await harness.post(
            web,
            f"/customers/{customer_id}/payouts/quote",
            harness.form(amount="10.00", virtualAccountId=account_id, **route),
        )
        record(
            "console: payout quote renders",
            quote.status_code == 200 and "problem" not in quote.text.lower()[:200],
            f"HTTP {quote.status_code}",
        )

        # The discovered route model is the authority on the fields a payout
        # needs — fill it the way the phase-4 script proved, but flattened to
        # the form names the console's own parser reads.
        snapshot = await payments.fetch_requirements(
            app.state.conduit,
            purpose=route["purpose"],
            rail=route["rail"],
            recipient_type=route["recipientType"],
            destination_country=route["destinationCountry"],
        )
        model = payments.payout_model(snapshot)
        rendered = (
            payments.recipient_model(model) if model.whitelist.get("required") else model
        )
        discovered: dict[str, str] = {}
        for field in rendered.fields:
            leaf = field.path[-1]
            name = forms.field_name(field.path)
            if field.allowed_values:
                value = field.allowed_values[0]
            elif field.validator == "aba":
                value = "021000021"
            elif leaf == "accountNumber":
                value = "000123456789"
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
                value = f"ZZZTEST Sweep {run} EOOD"
            elif leaf in ("firstName", "lastName"):
                value = "ZZZTEST"
            elif leaf == "reference":
                value = f"ZZZTEST-{run}"
            elif field.required:
                value = f"ZZZTEST {run}"
            else:
                continue
            discovered[name] = str(value)

        # A payout's supporting document must be purpose `transaction_support`
        # — Conduit refuses the onboarding document outright (verified live).
        upload = await api.post(
            "/documents",
            files={"file": ("zzztest-payout.pdf", ob.PDF, "application/pdf")},
            data={"purpose": "transaction_support", "name": "ZZZTEST payout evidence"},
            headers=idem(),
        )
        payout_doc = upload.json().get("id", "") if upload.status_code < 400 else ""
        record("transaction_support document uploaded", bool(payout_doc), payout_doc)

        intent = harness.minted_intent()
        payout_form = dict(
            amount="10.00",
            virtualAccountId=account_id,
            whitelistRecipientId=recipient_id,
            documentIds=payout_doc,
            intent=intent,
            **route,
            **discovered,
        )
        created = await harness.post(
            web, f"/customers/{customer_id}/payouts/new", harness.form(**payout_form)
        )
        location = flash_of(created)
        transaction_id = location.rsplit("/", 1)[-1] if "/transactions/" in location else ""
        record(
            "console: payout created → transaction",
            redirected(created) and bool(transaction_id),
            location[:90] or f"HTTP {created.status_code}: {created.text[:200]}",
        )
        if created.status_code == 422:
            capture("payout_422.html", {"html": created.text})

        if transaction_id:
            # The same form again, same intent: the ledger's double-submit guard
            # must reuse the operation, not send a second payout.
            replay = await harness.post(
                web, f"/customers/{customer_id}/payouts/new", harness.form(**payout_form)
            )
            record(
                "double-submit guard reuses operation",
                flash_of(replay) == location,
                flash_of(replay)[:90],
            )

            page = await web.get(f"/transactions/{transaction_id}")
            record("console: transaction page renders", page.status_code == 200)

            async def read_tx():
                result = await app.state.conduit.get(f"/v2/transactions/{transaction_id}")
                return getattr(result, "data", {}) or {}

            # Review approve, then settle *only if it is still needed*: an
            # approved review carries this route to `completed` on its own, so
            # the forced settle is a fallback rather than a step. Sending it
            # unconditionally earned a guaranteed 422
            # (`SANDBOX_TRANSACTION_NOT_FORCE_TERMINAL_READY`) in every run's log
            # for a transaction that was always going to settle — a warning that
            # teaches the reader the wrong thing. Same shape as 07's.
            done = (lambda t: t.get("status") == "completed")
            await harness.post(
                web,
                f"/transactions/{transaction_id}/simulate",
                harness.form(action="review", outcome="approve"),
            )
            tx = await poll(read_tx, done, tries=12)
            if not done(tx):
                await harness.post(
                    web,
                    f"/transactions/{transaction_id}/simulate",
                    harness.form(action="settle", outcome="completed"),
                )
                tx = await poll(read_tx, done)
            record(
                "simulate review+settle → completed",
                done(tx),
                f"status={tx.get('status')}",
            )
            capture("payout_completed.json", tx)

            # A second, cancellable payout.
            cancel_form = payout_form | {"intent": harness.minted_intent(), "amount": "5.00"}
            second = await harness.post(
                web, f"/customers/{customer_id}/payouts/new", harness.form(**cancel_form)
            )
            second_tx = flash_of(second).rsplit("/", 1)[-1] if "/transactions/" in flash_of(second) else ""
            if second_tx:
                cancelled = await harness.post(
                    web,
                    f"/transactions/{second_tx}/cancel",
                    harness.form(intent=harness.minted_intent()),
                )
                record(
                    "payout cancel accepted",
                    ok_redirect(cancelled),
                    flash_of(cancelled)[:90],
                )
            else:
                record("payout cancel", None, "second payout did not confirm")

        # ---- F. conversion quotes, console
        print("(F) conversion quotes — console")
        page = await web.get(f"/customers/{customer_id}/convert")
        record("console: convert page renders", page.status_code == 200)
        quote = await harness.post(
            web,
            f"/customers/{customer_id}/convert/quote",
            harness.form(source=account_id, destination=account_id, amount="10.00", lockSide="source"),
        )
        record(
            "console: same-asset convert refused, rendered",
            quote.status_code in (200, 422) and "Traceback" not in quote.text,
            f"HTTP {quote.status_code}",
        )

        quote_body = {
            "source": {"code": "USD"},
            "destination": {"code": "EUR"},
            "lockSide": "source",
            "amount": "10.00",
        }
        refused = await api.post("/quotes", json=quote_body, headers=idem())
        record(
            "quote with Idempotency-Key → refused by contract",
            refused.status_code == 400,
            f"HTTP {refused.status_code}",
        )
        raw = await api.post("/quotes", json=quote_body)
        record(
            "raw USD→EUR quote",
            raw.status_code in (200, 201, 422),
            f"HTTP {raw.status_code} {raw.json().get('type', 'ok') if raw.status_code >= 400 else ''}",
        )
        if raw.status_code < 400:
            capture("conversion_quote.json", raw.json())

        # ---- F2. cross-currency: EUR account → USD→EUR order → execute → settle
        print("(F2) conversion order lifecycle — console")
        response = await harness.post(
            web,
            f"/customers/{customer_id}/request-account",
            harness.form(f__asset__code="EUR", intent=harness.minted_intent()),
        )
        match = re.search(r"app_[A-Za-z0-9]+", flash_of(response))
        if match:
            sim = await harness.post(
                web, f"/applications/{match.group(0)}/simulate", harness.form(outcome="approved")
            )

        async def read_eur():
            page = await accounts.fetch_accounts(app.state.conduit, customer_id)
            return next(
                (
                    a
                    for a in (getattr(page, "items", []) or [])
                    if (a.get("asset") or {}).get("code") == "EUR" and a.get("status") == "active"
                ),
                None,
            )

        eur_account = await poll(read_eur, lambda a: a is not None, tries=20)
        record(
            "EUR account requested + approved",
            eur_account is not None,
            (eur_account or {}).get("id", "no EUR account"),
        )

        if eur_account:
            quoted = await harness.post(
                web,
                f"/customers/{customer_id}/convert/quote",
                harness.form(
                    source=account_id,
                    destination=eur_account["id"],
                    amount="20.00",
                    lockSide="source",
                ),
            )
            selections = [
                html_mod.unescape(m)
                for m in re.findall(r'name="selection" value="([^"]+)"', quoted.text)
            ]
            record(
                "USD→EUR quote offers options",
                quoted.status_code == 200 and bool(selections),
                f"{len(selections)} option(s)",
            )

        order_id = ""
        if eur_account and selections:
            picked = await harness.post(
                web,
                f"/customers/{customer_id}/convert/select",
                harness.form(selection=selections[0], intent=harness.minted_intent()),
            )
            op_url = flash_of(picked)
            record("option selected → confirm screen", "/convert/" in op_url, op_url[:90])
            if "/convert/" in op_url:
                confirmed = await harness.post(
                    web, op_url, harness.form(intent=harness.minted_intent())
                )
                order_url = flash_of(confirmed)
                order_id = order_url.rsplit("/", 1)[-1] if "/orders/" in order_url else ""
                record(
                    "order created from signed option",
                    bool(order_id),
                    order_url[:90] or f"HTTP {confirmed.status_code}",
                )

        if order_id:
            page = await web.get(f"/orders/{order_id}")
            record("console: order page renders", page.status_code == 200)
            executed = await harness.post(
                web, f"/orders/{order_id}/execute", harness.form(intent=harness.minted_intent())
            )
            record(
                "order executed",
                redirected(executed) and "err=" not in flash_of(executed),
                flash_of(executed)[:90],
            )

            async def read_order():
                result = await app.state.conduit.get(f"/v2/orders/{order_id}")
                return getattr(result, "data", {}) or {}

            order = await poll(
                read_order,
                lambda o: o.get("status") not in ("", None, "created", "pending"),
                tries=15,
            )
            capture("order_executed.json", order)
            record(
                "order reaches a live status",
                bool(order.get("status")),
                f"status={order.get('status')}",
            )

            # Settle the conversion's own transaction if the order names one.
            txns = [
                t if isinstance(t, str) else (t or {}).get("id", "")
                for t in (order.get("transactionIds") or order.get("transactions") or [])
            ]
            txn = next((t for t in txns if t), "")
            if txn:
                sim = await harness.post(
                    web,
                    f"/transactions/{txn}/simulate",
                    harness.form(action="terminal", outcome="completed"),
                )

                async def read_conv_tx():
                    result = await app.state.conduit.get(f"/v2/transactions/{txn}")
                    return getattr(result, "data", {}) or {}

                tx = await poll(read_conv_tx, lambda t: t.get("status") == "completed")
                record(
                    "conversion settled via simulate/terminal",
                    tx.get("status") == "completed",
                    f"status={tx.get('status')}",
                )
                capture("conversion_completed.json", tx)
            else:
                record("conversion settle", None, "order named no transaction")

        # The unhappy branch: a second order driven to rate-lock expiry.
        if eur_account and len(selections) >= 1:
            requote = await harness.post(
                web,
                f"/customers/{customer_id}/convert/quote",
                harness.form(
                    source=account_id,
                    destination=eur_account["id"],
                    amount="5.00",
                    lockSide="source",
                ),
            )
            fresh = [
                html_mod.unescape(m)
                for m in re.findall(r'name="selection" value="([^"]+)"', requote.text)
            ]
            expired_order = ""
            if fresh:
                picked = await harness.post(
                    web,
                    f"/customers/{customer_id}/convert/select",
                    harness.form(selection=fresh[0], intent=harness.minted_intent()),
                )
                if "/convert/" in flash_of(picked):
                    confirmed = await harness.post(
                        web, flash_of(picked), harness.form(intent=harness.minted_intent())
                    )
                    if "/orders/" in flash_of(confirmed):
                        expired_order = flash_of(confirmed).rsplit("/", 1)[-1]
            if expired_order:
                sim = await harness.post(
                    web,
                    f"/orders/{expired_order}/simulate",
                    harness.form(action="rate-lock-expired"),
                )
                record(
                    "simulate rate-lock expiry accepted",
                    ok_redirect(sim),
                    flash_of(sim)[:90],
                )
            else:
                record("rate-lock expiry branch", None, "no second order")

        # ---- G. the error contracts
        print("(G) error contracts")
        page = await web.get("/customers/cus_zzztest_does_not_exist")
        record(
            "console: unknown customer → error page, not 500",
            page.status_code in (200, 404) and "Traceback" not in page.text,
            f"HTTP {page.status_code}",
        )

        async with httpx.AsyncClient(
            base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": "ck_sandbox_invalid"}, timeout=30.0
        ) as bad:
            response = await bad.get("/customers")
            body_json = response.json() if "json" in response.headers.get("content-type", "") else {}
            record(
                "bad key → 401 problem-detail",
                response.status_code == 401 and bool(body_json.get("type")),
                f"HTTP {response.status_code} {body_json.get('type', '?')}",
            )

        response = await api.get("/transactions/txn_zzztest_missing")
        record(
            "malformed transaction id → 400 problem-detail",
            response.status_code == 400,
            f"HTTP {response.status_code}",
        )
        response = await api.get("/transactions/txn_034FD00000000000000000")
        record(
            "well-formed missing transaction → 404",
            response.status_code == 404,
            f"HTTP {response.status_code}",
        )

    return await finish(api)


async def finish(api: httpx.AsyncClient) -> int:
    await api.aclose()
    failures = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n  {len(RESULTS)} scenarios: "
          f"{sum(1 for r in RESULTS if r[1] == 'PASS')} pass, "
          f"{len(failures)} fail, "
          f"{sum(1 for r in RESULTS if r[1] == 'SKIP')} skip")
    for name, verdict, note in failures:
        print(f"    FAIL {name} — {note}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
