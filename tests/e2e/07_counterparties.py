#!/usr/bin/env python
"""Live sandbox: reusable counterparties, end to end through the console's own app.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…`, exactly like `06_sandbox_
sweep.py`. The staging pair in `../.env` (a live key against a production-
labelled host) is never read.

What it proves, in one pass, on a **free-form** payout route
(`payment_for_goods_or_services` — the routes Conduit gives no recipient store
for; the intercompany gate is a different feature and is untouched):

    pick a funded ZZZTEST customer → upload a transaction_support document →
    send a payout with "save as counterparty" ticked → drive it to *completed*
    via the sandbox simulators → the counterparty is in the management list,
    masked → the picker offers it → prefill it and check field-by-field that
    what the form now holds is what was stored → send a second payout off that
    prefill → bridge it onto Conduit's whitelist and have the sandbox approve it
    → **edit its account number**: the console states the consequence, the
    confirmed save drops the whitelist capability against the real registration,
    and re-bridging the new coordinates restores it → read the whole story back
    off the history panel → archive it → it is gone from the picker and from
    prefill → **delete a second contact**: the coordinates are purged, the shell
    and the ledger's sent-to-contact filter survive.

Everything is driven through the console's ASGI app wherever a page or a form
exists, so what is asserted is what an operator would actually see — the
documents included: they go through `POST /documents`, the console's own upload
route, because every attaching path resolves an id against this console's upload
ledger and a document uploaded straight to Conduit is (correctly) refused.

    .venv/bin/python tests/e2e/07_counterparties.py

Safe to re-run: every counterparty label, client reference and idempotency key
is run-unique, all records are ZZZTEST-prefixed, and the money is fake by
construction. The customer is **discovered**, not pinned: any ZZZTEST customer
with an active, funded USD virtual account will do, and the script says SKIP
rather than FAIL when the sandbox holds none.

The API key is read from the environment and never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"
EVIDENCE = Path(__file__).parent / "sandbox_evidence"

# Two payouts of this each, out of a balance the script checks first.
SAVED = "Saved for payouts"
WHITELISTED = "Registered for intercompany payouts"
AMOUNT = "10.00"
NEEDED = 25.0

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def load_env() -> str:
    # Process environment first, `../.env` as fallback — the file does not
    # exist on CI, where only `--check-config` runs (with fake credentials).
    values = {}
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    key = os.environ.get("CONDUIT_SANDBOX_API_KEY") or values.get("CONDUIT_SANDBOX_API_KEY") or ""
    host = (os.environ.get("CONDUIT_SANDBOX_HOST") or values.get("CONDUIT_SANDBOX_HOST") or "").rstrip("/")
    if not key.startswith("ck_sandbox_"):
        sys.exit("refusing to run: CONDUIT_SANDBOX_API_KEY is not a ck_sandbox_ key")
    if host != SANDBOX_HOST:
        sys.exit(f"refusing to run: CONDUIT_SANDBOX_HOST is not {SANDBOX_HOST}")
    return key


KEY = load_env()

# Environment before any app import — Settings is cached at first read. The
# config's own allowlist pins CONDUIT_ENV=sandbox to SANDBOX_HOST.
from cryptography.fernet import Fernet  # noqa: E402

os.environ["CONDUIT_ENV"] = "sandbox"
os.environ["CONDUIT_API_KEY"] = KEY
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://mc_bot@/conduit_console_sweep?host=/tmp"
)
# `setdefault` means an inherited `DATABASE_URL` wins — and this script deletes
# rows. A shell that happens to export a real DSN would have had its
# counterparties wiped by a script whose name says "sandbox". So the resolved
# database has to *name itself* disposable before anything here writes to it.
DISPOSABLE = re.compile(r"(sweep|e2e|test)", re.I)
_db_name = urlsplit(os.environ["DATABASE_URL"]).path.lstrip("/")
if not DISPOSABLE.search(_db_name):
    sys.exit(
        f"refusing to run: DATABASE_URL names {_db_name!r}, which is not a disposable "
        "database — this script deletes rows. Unset DATABASE_URL to use the sweep "
        "database, or point it at one whose name contains sweep/e2e/test."
    )
# `--check-config` stops here: everything above is the two refusals (sandbox key
# + host, disposable database) and nothing below them has run. It is what
# `tests/test_counterparties.py` exercises, because the only other way to prove
# a guard fires is to run the script that would otherwise move money.
if "--check-config" in sys.argv:
    print(f"config ok: {SANDBOX_HOST}, database {_db_name}")
    sys.exit(0)
os.environ.setdefault("SESSION_SECRET", "sandbox-sweep-session")
# One key for the whole run: the counterparty this script saves has to be
# readable by the same process that saved it, and by nothing afterwards.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("AUTH_MODE", "disabled")
os.environ.setdefault("CONDUIT_WEBHOOK_SECRET", "whsec_" + "a1b2c3d4" * 8)

import httpx  # noqa: E402
from sqlalchemy import delete as sa_delete, select  # noqa: E402

from app import counterparties, forms, payments  # noqa: E402
from app.auth.providers import ProxyProvider  # noqa: E402
from app.conduit import ConduitClient  # noqa: E402
from app.db import sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import AuditEvent, Counterparty  # noqa: E402
from tests import web_harness as harness  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, note: str = "") -> None:
    verdict = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((name, verdict, note))
    print(f"  [{verdict}] {name:<48} {note}", flush=True)


def capture(name: str, payload: object) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def flash_of(response: httpx.Response) -> str:
    return str(response.headers.get("hx-redirect") or response.headers.get("location", ""))


def redirected(response: httpx.Response) -> bool:
    return response.status_code in (204, 302, 303) and bool(flash_of(response))


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")


async def poll(fn, want, *, tries: int = 20, delay: float = 1.0):
    value = None
    for _ in range(tries):
        value = await fn()
        if want(value):
            return value
        await asyncio.sleep(delay)
    return value


async def console_upload(web, purpose: str, name: str) -> str:
    """One document through the **console's own** upload route, and its id.

    Not the raw API: every attaching path resolves a `doc_` id
    against this console's own upload ledger before anything is sent, so a
    document uploaded straight to Conduit is refused at submit — correctly, and
    that refusal is what this script would otherwise be testing.
    """
    response = await harness.upload(web, purpose=purpose, filename=name, content=PDF)
    found = re.search(r'name="documentIds" value="([^"]+)"', response.text)
    return found.group(1) if found else ""


def field_value(html: str, name: str) -> str | None:
    """The value one rendered field carries, whichever widget `m.field` chose.

    Both branches are needed: a recipient subtree mixes free text
    (`accountNumber`, `city`) with enums rendered as `<select>` (`accountType`,
    `type`), and checking only the inputs would quietly skip exactly the fields
    a flat copy is most likely to lose. `name` and `value` sit on separate lines
    in the macro, so the whole tag is read rather than a substring.
    """
    tag = re.search(rf'<input [^>]*name="{re.escape(name)}"[^>]*>', html)
    if tag is not None:
        value = re.search(r'value="([^"]*)"', tag.group(0))
        return value.group(1) if value else None
    box = re.search(
        rf'<select [^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>', html, re.S
    )
    if box is None:
        return None
    chosen = re.search(r'<option value="([^"]*)" selected', box.group(1))
    return chosen.group(1) if chosen else None


def account_number(run: str) -> str:
    """A numeric account number unique to this run (see `answer`)."""
    return f"1{int(run[:9], 16)}"[:12]


def answer(model: forms.FormModel, run: str) -> dict:
    """Discovery's own fields, filled the way `06_sandbox_sweep.py` proved live —
    flattened to the form names the console's parser reads. The route is the
    authority on which fields exist; this only supplies plausible values."""
    filled: dict[str, str] = {}
    for field in model.fields:
        leaf = field.path[-1]
        if field.allowed_values:
            value = field.allowed_values[0]
        elif field.validator == "aba":
            value = "021000021"
        elif leaf == "accountNumber":
            # Run-unique: the bridge registers these exact coordinates with
            # Conduit, and Conduit 409s a conflicting re-registration — so a
            # fixed account number would make this script pass once and then
            # refuse for ever.
            value = account_number(run)
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
            value = f"ZZZTEST Globex {run} LLC"
        elif leaf in ("firstName", "lastName"):
            value = "ZZZTEST"
        elif leaf == "reference":
            value = f"ZZZTEST-{run}"
        elif field.required:
            value = f"ZZZTEST {run}"
        else:
            continue
        filled[forms.field_name(field.path)] = str(value)
    return filled


async def pick_customer(api: httpx.AsyncClient) -> tuple[str, str, str]:
    """A ZZZTEST customer with an active USD virtual account holding enough to
    fund two small payouts. Discovered, never pinned — this script must keep
    working after the sandbox is reset."""
    listed = await api.get("/customers", params={"limit": 50})
    for customer in listed.json().get("data") or []:
        if "ZZZTEST" not in json.dumps(customer):
            continue
        accounts = await api.get(f"/customers/{customer['id']}/virtual-accounts")
        if accounts.status_code >= 400:
            continue
        for account in accounts.json().get("data") or []:
            if account.get("status") != "active":
                continue
            if (account.get("asset") or {}).get("code") != "USD":
                continue
            for balance in account.get("balances") or []:
                available = ((balance or {}).get("available") or {}).get("amount") or "0"
                if float(available) >= NEEDED:
                    return customer["id"], account["id"], available
    return "", "", ""


ROUTE = {
    # A **free-form** route: no whitelist gate, so this is the half of the
    # payout surface Conduit stores nothing for. That is the whole point.
    "purpose": "payment_for_goods_or_services",
    "rail": "fedwire",
    "recipientType": "business",
    "destinationCountry": "USA",
}
QUERY = "&".join(f"{k}={v}" for k, v in ROUTE.items())


async def main() -> int:
    run = uuid.uuid4().hex[:10]
    label = f"ZZZTEST Globex {run}"
    print(f"\nConduit Console — counterparties e2e\n  host {SANDBOX_HOST}  run {run}\n")
    migrate()

    app = create_app()
    app.state.conduit = ConduitClient()
    app.state.auth_provider = ProxyProvider(harness.PROXY)
    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )

    async with harness.signed_in(app) as web:
        # ---- A. a customer to pay from
        print("(A) setup")
        customer_id, account_id, available = await pick_customer(api)
        if not customer_id:
            record(
                "funded ZZZTEST customer found",
                None,
                f"no ZZZTEST customer holds >= {NEEDED} USD — run 06_sandbox_sweep.py first",
            )
            return await finish(api)
        record("funded ZZZTEST customer found", True, f"{customer_id} {available} USD")

        new = f"/customers/{customer_id}/payouts/new"
        index = f"/customers/{customer_id}/contacts"
        wl_form = f"/customers/{customer_id}/recipients"

        # This script mints a fresh ENCRYPTION_KEY per run, so a previous run's
        # rows are undecryptable by construction — they would render as
        # `unreadable` and make the empty-list assertion below a lie about a page
        # that is not empty. Scoped to **this customer's ZZZTEST rows**, never
        # the table: a sweep script is not entitled to delete rows it did not
        # create, whatever database it is pointed at.
        async with sessionmaker()() as session:
            await session.execute(
                sa_delete(Counterparty).where(
                    Counterparty.customer_id == customer_id,
                    Counterparty.label.like("ZZZTEST%"),
                )
            )
            await session.commit()

        listing = await web.get(index)
        # Not "empty": a real sandbox customer may hold whitelist registrations
        # from an earlier sweep, and this page shows those too. What must be
        # true is that *this run's* contact is not there yet.
        record(
            "console: contacts page renders without this run's contact",
            listing.status_code == 200 and label not in listing.text,
            f"HTTP {listing.status_code}",
        )
        # A real ZZZTEST customer may already hold registrations from
        # `06_sandbox_sweep.py`, so every capability assertion below is about the
        # delta this run causes, never about an absolute count.
        baseline = listing.text.count(WHITELISTED)

        snapshot = await payments.fetch_requirements(
            app.state.conduit,
            purpose=ROUTE["purpose"],
            rail=ROUTE["rail"],
            recipient_type=ROUTE["recipientType"],
            destination_country=ROUTE["destinationCountry"],
        )
        if not isinstance(snapshot, dict):
            record("payout requirements read", False, str(snapshot)[:120])
            return await finish(api)
        model = payments.payout_model(snapshot)
        gated = bool(model.whitelist.get("required"))
        record(
            "the route is free-form (no whitelist gate)",
            not gated,
            f"whitelist.required={gated}, documentation.required="
            f"{bool(model.documentation.get('required'))}",
        )
        discovered = answer(model, run)

        document_id = await console_upload(
            web, "transaction_support", f"zzztest-invoice-{run}.pdf"
        )
        record("transaction_support document uploaded", bool(document_id), document_id)

        # ---- B. payout with "save as counterparty"
        print("(B) payout + save as counterparty")
        form = dict(
            amount=AMOUNT,
            virtualAccountId=account_id,
            documentIds=document_id,
            intent=harness.minted_intent(),
            save_counterparty="1",
            counterparty_label=label,
            **ROUTE,
            **discovered,
        )
        created = await harness.post(web, new, harness.form(**form))
        location = flash_of(created)
        transaction_id = location.rsplit("/", 1)[-1] if "/transactions/" in location else ""
        record(
            "console: payout created → transaction",
            redirected(created) and bool(transaction_id),
            location[:90] or f"HTTP {created.status_code}: {created.text[:300]}",
        )
        if not transaction_id:
            capture("counterparty_payout_422.json", {"html": created.text[:6000]})
            return await finish(api)
        # A redirect with no `err=` is the save reporting success; the row below
        # is the proof.
        record("save reported no failure", "err=" not in location, location[:90])

        async with sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(Counterparty).where(Counterparty.customer_id == customer_id)
                )
            ).scalars().all()
        saved = rows[0] if len(rows) == 1 else None
        record(
            "counterparty saved on 202 acceptance",
            saved is not None and saved.label == label,
            f"{len(rows)} row(s)" + (f", {saved.rail_family}/{saved.recipient_type}" if saved else ""),
        )
        if saved is None:
            return await finish(api)
        capture(
            "counterparty_saved.json",
            {
                "label": saved.label,
                "customer": saved.customer_id,
                "rail_family": saved.rail_family,
                "recipient_type": saved.recipient_type,
                "destination_country": saved.destination_country,
                # Masked here too: this file is committed evidence.
                "coordinates": counterparties.coordinates(saved.recipient),
                "recipient_keys": sorted(saved.recipient),
            },
        )

        # ---- C. drive the payout to settled
        print("(C) settle the payout")

        async def read_tx():
            result = await app.state.conduit.get(f"/v2/transactions/{transaction_id}")
            return getattr(result, "data", {}) or {}

        # An approved review carries this route to `completed` on its own — so
        # the forced settle is a *fallback*, not a step. Sending it
        # unconditionally earns a guaranteed 422
        # (`SANDBOX_TRANSACTION_NOT_FORCE_TERMINAL_READY`) in the log for a
        # transaction that was always going to settle, which is a warning that
        # teaches the reader the wrong thing.
        done = (lambda t: t.get("status") == "completed")
        await harness.post(
            web,
            f"/transactions/{transaction_id}/simulate",
            harness.form(action="review", outcome="approve"),
        )
        settled = await poll(read_tx, done, tries=12)
        if not done(settled):
            await harness.post(
                web,
                f"/transactions/{transaction_id}/simulate",
                harness.form(action="settle", outcome="completed"),
            )
            settled = await poll(read_tx, done)
        record(
            "simulate review+settle → completed",
            settled.get("status") == "completed",
            f"status={settled.get('status')}",
        )

        # ---- D. the management list, masked
        print("(D) contacts list")
        listing = await web.get(index)
        account_number = str(saved.recipient.get("accountNumber") or "")
        masked = counterparties.coordinates(saved.recipient)
        record(
            "console: contact listed under its label",
            listing.status_code == 200 and label in listing.text,
            f"HTTP {listing.status_code}",
        )
        record(
            "saved-only: one capability, and it is the saved one",
            SAVED in listing.text and listing.text.count(WHITELISTED) == baseline,
            f"saved badge present, whitelisted badges still {baseline}",
        )
        record(
            "list shows the last four digits only",
            bool(masked) and all(m in listing.text for m in masked),
            " ".join(masked),
        )
        record(
            "the full coordinate is nowhere in the page",
            bool(account_number) and account_number not in listing.text,
            f"account number {len(account_number)} chars, absent",
        )

        # ---- E. the picker prefills exactly what was saved
        print("(E) picker prefill")
        picker = await web.get(f"{new}?{QUERY}")
        record(
            "console: the picker offers the saved counterparty",
            label in picker.text and str(saved.id) in picker.text,
            "option present",
        )
        record(
            "the picker option is masked too",
            all(m in picker.text for m in masked) and account_number not in picker.text,
            " ".join(masked),
        )

        prefilled = await web.get(f"{new}?{QUERY}&counterparty={saved.id}")
        mismatches = []
        checked = 0
        for field in model.fields:
            if field.path[:2] != ("destination", "recipient"):
                continue
            stored = forms.lookup(field.path[2:], saved.recipient)
            if stored is forms.ABSENT or stored in (None, ""):
                continue
            checked += 1
            rendered = field_value(prefilled.text, forms.field_name(field.path))
            if rendered != str(stored):
                mismatches.append(f"{'.'.join(field.path)}: {rendered!r} != stored")
        record(
            "prefilled form == saved counterparty, field by field",
            not mismatches and checked > 0,
            f"{checked} fields compared" + (f"; {mismatches[:3]}" if mismatches else ""),
        )
        record(
            "prefill does not lock the fields",
            not re.search(
                r'<input [^>]*name="f\.destination\.recipient[^>]*(readonly|disabled)',
                prefilled.text,
            ),
            "no readonly/disabled on a recipient input",
        )

        # ---- F. a second payout off the prefill
        print("(F) second payout via the prefill")
        second_doc = await console_upload(
            web, "transaction_support", f"zzztest-invoice-{run}-b.pdf"
        )

        # The recipient half comes off the *rendered* page, not from
        # `discovered` — so the destination this payout sends is the one the
        # prefill produced. The rest of the form (remittance, the route's own
        # subtree) is the operator's per-payment answer and is filled as before.
        from_prefill = dict(discovered)
        for field in model.fields:
            if field.path[:2] != ("destination", "recipient"):
                continue
            name = forms.field_name(field.path)
            rendered = field_value(prefilled.text, name)
            if rendered:
                from_prefill[name] = rendered
            else:
                from_prefill.pop(name, None)
        second_form = dict(
            from_prefill,
            amount=AMOUNT,
            virtualAccountId=account_id,
            documentIds=second_doc,
            intent=harness.minted_intent(),
            counterparty=str(saved.id),
            **ROUTE,
        )
        second = await harness.post(web, new, harness.form(**second_form))
        second_location = flash_of(second)
        second_tx = (
            second_location.rsplit("/", 1)[-1] if "/transactions/" in second_location else ""
        )
        record(
            "console: second payout sent from the prefill",
            bool(second_tx),
            second_location[:90] or f"HTTP {second.status_code}: {second.text[:300]}",
        )
        if second_tx:
            detail = await app.state.conduit.get(f"/v2/transactions/{second_tx}")
            sent = getattr(detail, "data", {}) or {}
            recipient = ((sent.get("destination") or {}).get("recipient")) or {}
            record(
                "the second payout carries the saved destination",
                str(recipient.get("legalName") or "") == str(saved.recipient.get("legalName")),
                f"legalName={recipient.get('legalName')}",
            )

        async with sessionmaker()() as session:
            after = (
                await session.execute(
                    select(Counterparty).where(Counterparty.customer_id == customer_id)
                )
            ).scalars().all()
        record(
            "the prefilled resend did not create a second counterparty",
            len(after) == 1,
            f"{len(after)} row(s)",
        )

        # ---- F2. the ledger, filtered to this contact
        print("(F2) sent-to-contact filter")
        ledger = (
            f"/transactions?type=withdrawal&customerId={customer_id}&contact={saved.id}"
        )
        filtered = await web.get(ledger)
        # The first payout was typed and *saved* the contact; the second was sent
        # from the picker and matched. Only the second is a `counterparty.used`
        # row, and only the second is a match — the whole boundary, live.
        record(
            "console: the filter finds the payout sent FROM the contact",
            filtered.status_code == 200 and bool(second_tx) and second_tx in filtered.text,
            f"HTTP {filtered.status_code}, {second_tx}",
        )
        record(
            "console: the hand-typed payout that saved it is NOT a match",
            transaction_id not in filtered.text,
            f"{transaction_id} absent",
        )
        record(
            "the boundary is stated on the filtered view",
            "made outside this console" in filtered.text
            and "typed rather than picked" in filtered.text
            and "console-linked payout" in filtered.text,
            "boundary paragraph + result meta",
        )
        # Same set, same source, in the file the operator downloads.
        exported = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={customer_id}"
            f"&contact={saved.id}"
        )
        record(
            "the CSV export carries the same filter",
            exported.status_code == 200
            and bool(second_tx)
            and second_tx in exported.text
            and transaction_id not in exported.text
            and f"contact-{saved.id}" in exported.headers.get("content-disposition", ""),
            exported.headers.get("content-disposition", "")[:90],
        )
        # Without a customer there is nothing to scope a contact to, and the
        # field says so instead of standing there empty.
        bare = await web.get("/transactions?type=withdrawal")
        record(
            "no customer → the field is disabled, with its reason",
            '<select name="contact" disabled>' in bare.text
            and "pick a customer on the <em>Withdrawal</em> tab" in bare.text,
            "",
        )

        # ---- G. the bridge: saved contact → whitelist → both capabilities
        print("(G) whitelist the saved contact")
        evidence_id = await console_upload(web, "feature_request", f"zzztest-evidence-{run}.pdf")
        record("feature_request evidence uploaded", bool(evidence_id), evidence_id)

        bridged = await web.get(f"{wl_form}?contact={saved.id}")
        stored_account = str(saved.recipient.get("accountNumber") or "")
        # Set by the edit below, if the registration this section needs lands.
        new_account = ""
        # **Stated masked, not rendered as an input**: the POST re-reads the
        # stored record and overlays every coordinate,
        # so the form never used the value it was printing. What must be true is
        # that the page names the right destination without publishing it — and
        # the forgery-strip assertion below still proves the stored coordinates
        # are what Conduit is asked to register.
        record(
            "console: the bridge states the saved contact, masked",
            bridged.status_code == 200
            and counterparties.mask(stored_account) in bridged.text
            and stored_account not in bridged.text
            and field_value(bridged.text, "f.accountNumber") is None,
            f"HTTP {bridged.status_code}, {counterparties.mask(stored_account)}",
        )
        # The forgery strip, live: the form is submitted with a DIFFERENT account
        # number in the browser's field, and what Conduit is asked to register
        # has to be the stored one.
        registered = await harness.post(
            web,
            wl_form,
            harness.form(
                rail=saved.rail_family,
                contact=str(saved.id),
                intent=harness.minted_intent(),
                documentIds=evidence_id,
                **{
                    "f.relationship": "self",
                    "f.legalName": "ZZZTEST FORGED " + run,
                    "f.accountNumber": "999999999999",
                    "f.routingNumber": "011000015",
                    "f.label": label,
                },
            ),
        )
        registration = flash_of(registered)
        entry_id = registration.rsplit("registered=", 1)[-1] if "registered=" in registration else ""
        record(
            "console: bridged registration accepted",
            bool(entry_id),
            registration[:100] or f"HTTP {registered.status_code}: {registered.text[:300]}",
        )
        if entry_id:
            entry = await app.state.conduit.get(
                f"/v2/customers/{customer_id}/whitelist-recipients"
            )
            listed_entries = (getattr(entry, "data", {}) or {}).get("data") or []
            created = next((e for e in listed_entries if e.get("id") == entry_id), {})
            record(
                "the registration carries the STORED coordinates, not the form's",
                created.get("accountNumber") == stored_account
                and created.get("legalName") == saved.recipient.get("legalName"),
                f"accountNumber ends {str(created.get('accountNumber'))[-4:]}, "
                f"legalName={created.get('legalName')}",
            )
            capture(
                "contact_bridged_registration.json",
                {
                    "id": entry_id,
                    "rail": created.get("rail"),
                    "relationship": created.get("relationship"),
                    "status": created.get("status"),
                    "coordinates": counterparties.coordinates(created),
                },
            )

            pending_page = await web.get(index)
            record(
                "pending review is NOT rendered as a capability",
                "Whitelisting" in pending_page.text
                and pending_page.text.count(WHITELISTED) == baseline,
                "badge withheld until Conduit says registered",
            )

            approved = await harness.post(
                web,
                f"/customers/{customer_id}/recipients/{entry_id}/simulate",
                harness.form(outcome="approve"),
            )

            async def read_entry():
                result = await app.state.conduit.get(
                    f"/v2/customers/{customer_id}/whitelist-recipients"
                )
                rows = (getattr(result, "data", {}) or {}).get("data") or []
                return next((e for e in rows if e.get("id") == entry_id), {})

            settled_entry = await poll(read_entry, lambda e: e.get("status") == "registered")
            record(
                "sandbox approve → registered",
                settled_entry.get("status") == "registered",
                f"status={settled_entry.get('status')}; simulate {approved.status_code}",
            )

            both = await web.get(index)
            # One row gained the second badge — not a second row that merely
            # carries the same coordinates.
            record(
                "both capabilities land on ONE row, matched by coordinates",
                both.text.count(SAVED) == 1
                and both.text.count(WHITELISTED) == baseline + 1
                and both.text.count(label) >= 1,
                f"saved 1, whitelisted {both.text.count(WHITELISTED)} (was {baseline})",
            )

            # ---- G2. editing the coordinates out from under a live registration
            #
            # The whole consequence story against the real join: the capability
            # is `counterparties.merge` asking Conduit's own list, so the only
            # way to prove the edit drops it is to change the digits and look
            # again.
            print("(G2) edit → consequence → capability drops → re-bridge")
            entries_made = [entry_id]
            edit = f"{index}/{saved.id}/edit"
            both_filter = f"/contacts?customerId={customer_id}&capability=both"
            # Run-unique like the first one (the bridge registers these exact
            # digits with Conduit, which 409s a conflicting re-registration), and
            # one digit away from it so the diff is a real identity change.
            # `account_number` the *function* is shadowed by a local string in
            # this scope, so the value is derived from the stored one.
            new_account = stored_account[:-1] + ("7" if stored_account[-1] != "7" else "5")

            form_page = await web.get(edit)
            record(
                "console: the edit form is discovery's own recipient fields",
                form_page.status_code == 200
                and field_value(form_page.text, "f.destination.recipient.accountNumber")
                == stored_account
                and "This contact is registered for intercompany payouts" in form_page.text
                and entry_id in form_page.text,
                f"HTTP {form_page.status_code}",
            )
            # Every recipient field this route declares, as the payout sent them,
            # with one identity key moved.
            edit_form = {
                name: value
                for name, value in discovered.items()
                if name.startswith("f.destination.recipient.")
            }
            edit_form["f.destination.recipient.accountNumber"] = new_account
            edit_form["label"] = label

            asked = await harness.post(web, edit, harness.form(**edit_form))
            record(
                "console: the edit states the consequence and stores nothing yet",
                asked.status_code == 200
                and "This edit drops" in asked.text
                and f"{counterparties.mask(stored_account)}→{counterparties.mask(new_account)}"
                in asked.text,
                f"HTTP {asked.status_code}",
            )
            token = re.search(r'name="confirm" value="([^"]+)"', asked.text)
            before_edit = await web.get(both_filter)
            saved_edit = await harness.post(
                web,
                edit,
                harness.form(
                    **edit_form,
                    intent=token.group(1) if token else "",
                    confirm=token.group(1) if token else "",
                ),
            )
            after_edit = await web.get(both_filter)
            record(
                "the confirmed edit drops the whitelist capability, live",
                redirected(saved_edit)
                and label in before_edit.text
                and label not in after_edit.text,
                flash_of(saved_edit)[:90],
            )
            async with sessionmaker()() as session:
                stored_now = (
                    await session.execute(
                        select(Counterparty).where(Counterparty.id == saved.id)
                    )
                ).scalar_one()
            record(
                "the stored coordinates are the edited ones",
                (stored_now.recipient or {}).get("accountNumber") == new_account,
                f"ends {new_account[-4:]}",
            )
            async with sessionmaker()() as session:
                trail = await counterparties.history(
                    session, customer_id, str(saved.id), label=label
                )
            edited_row = next(
                (r for r in trail if r["action"] == "counterparty.edited"), {}
            )
            record(
                "the audit row carries a MASKED diff and no coordinate",
                bool(edited_row)
                and edited_row["detail"].get("identity", {}).get("accountNumber")
                == f"{counterparties.mask(stored_account)}→{counterparties.mask(new_account)}"
                and stored_account not in json.dumps(edited_row["detail"])
                and new_account not in json.dumps(edited_row["detail"]),
                str(edited_row.get("detail", {}).get("identity"))[:80],
            )

            # Re-register the new coordinates: the way back the sentence promises.
            rebridge = await harness.post(
                web,
                wl_form,
                harness.form(
                    rail=saved.rail_family,
                    contact=str(saved.id),
                    intent=harness.minted_intent(),
                    documentIds=await console_upload(
                        web, "feature_request", f"zzztest-evidence-{run}-2.pdf"
                    ),
                    **{
                        "f.relationship": "self",
                        "f.legalName": stored_now.recipient.get("legalName"),
                        "f.label": label,
                    },
                ),
            )
            second_registration = flash_of(rebridge)
            entry_two = (
                second_registration.rsplit("registered=", 1)[-1]
                if "registered=" in second_registration
                else ""
            )
            if entry_two:
                entries_made.append(entry_two)
                await harness.post(
                    web,
                    f"/customers/{customer_id}/recipients/{entry_two}/simulate",
                    harness.form(outcome="approve"),
                )

                async def read_second():
                    result = await app.state.conduit.get(
                        f"/v2/customers/{customer_id}/whitelist-recipients"
                    )
                    rows = (getattr(result, "data", {}) or {}).get("data") or []
                    return next((e for e in rows if e.get("id") == entry_two), {})

                again = await poll(read_second, lambda e: e.get("status") == "registered")
                restored = await web.get(both_filter)
                record(
                    "re-registering the edited coordinates restores the capability",
                    again.get("status") == "registered" and label in restored.text,
                    f"{entry_two} {again.get('status')}",
                )
            else:
                record(
                    "re-registering the edited coordinates restores the capability",
                    False,
                    second_registration[:100] or f"HTTP {rebridge.status_code}",
                )

            # Both registrations this run made — the first still points at the
            # coordinates the edit moved away from.
            for made in entries_made:
                revoked = await harness.post(
                    web,
                    f"/customers/{customer_id}/recipients/{made}/revoke",
                    harness.form(intent=harness.minted_intent()),
                )
            after_revoke = await web.get(index)
            record(
                "revoke drops the whitelist capability and keeps the saved one",
                redirected(revoked)
                and after_revoke.text.count(WHITELISTED) == baseline
                and SAVED in after_revoke.text
                and label in after_revoke.text,
                f"{len(entries_made)} revoked; {flash_of(revoked)[:60]}",
            )

        # ---- H. the per-contact history panel
        print("(H) history panel")
        listed_row = await web.get(index)
        panel = await web.get(f"{index}/{saved.id}/history")
        story = re.sub(r"(?s)<[^>]*>", " ", panel.text)
        record(
            "console: the list offers the history drawer, the panel tells the story",
            panel.status_code == 200
            and f'hx-get="{index}/{saved.id}/history"' in listed_row.text
            and 'id="drawer-body"' in listed_row.text
            and "Saved from a payout" in story
            and "Used on a payout" in story
            and "Edited" in story
            and "Put forward for whitelisting" in story,
            f"HTTP {panel.status_code}",
        )
        record(
            "the panel shows masked diffs and links the payouts, never a coordinate",
            counterparties.mask(stored_account) in panel.text
            and stored_account not in panel.text
            and bool(new_account)
            and new_account not in panel.text
            and bool(second_tx)
            and second_tx in panel.text,
            f"{counterparties.mask(stored_account)}→{counterparties.mask(new_account)}",
        )

        # ---- I. archive
        print("(I) archive")
        archived = await harness.post(web, f"{index}/{saved.id}/archive")
        record(
            "console: archive accepted",
            redirected(archived) and "err=" not in flash_of(archived),
            flash_of(archived)[:90],
        )
        gone_list = await web.get(index)
        gone_picker = await web.get(f"{new}?{QUERY}")
        gone_prefill = await web.get(f"{new}?{QUERY}&counterparty={saved.id}")
        # Archiving retires the **saved** capability, not the row: the revoked
        # registration this run created is Conduit's record and is still listed,
        # under the same name. That is the two stores staying distinct, which is
        # the whole design — so the assertion is about the capability, not about
        # the string.
        record(
            "archived: the saved capability is gone from the list",
            SAVED not in gone_list.text,
            "",
        )
        record(
            "archived: gone from the picker",
            label not in gone_picker.text
            and "No saved contact for this customer" in gone_picker.text,
            "",
        )
        record(
            "archived: an old prefill URL refuses",
            "That contact could not be used" in gone_prefill.text
            and account_number not in gone_prefill.text,
            "",
        )
        async with sessionmaker()() as session:
            still = (
                await session.execute(select(Counterparty).where(Counterparty.id == saved.id))
            ).scalar_one()
        record(
            "archive is a soft delete — the row survives",
            still.archived_at is not None,
            f"archived_at={still.archived_at}",
        )

        # ---- J. delete: the second tier
        #
        # A contact of its own, saved directly: deleting is entirely
        # console-local (no Conduit call, by design), so what this section has to
        # prove live is the *purge* and the shell that survives it — not another
        # payout.
        print("(J) delete")
        doomed_label = f"ZZZTEST Doomed {run}"
        async with sessionmaker()() as session:
            doomed = await counterparties.save(
                session,
                customer_id=customer_id,
                label=doomed_label,
                recipient=dict(still.recipient or {}),
                rail_family=still.rail_family,
                recipient_type=still.recipient_type,
                destination_country=still.destination_country,
                actor_id="usr_1",
                actor_email="ops@example.com",
            )
            # The payment this contact will have to keep resolving after its
            # coordinates are gone — the trail's own row, written the way the
            # payout route writes it.
            session.add(
                AuditEvent(
                    action=counterparties.USED_ACTION,
                    actor_id="usr_1",
                    actor_email="ops@example.com",
                    detail={"counterparty": str(doomed), "label": doomed_label},
                )
            )
            await session.commit()

        refused = await harness.post(web, f"{index}/{doomed}/delete")
        record(
            "delete is admin-only — an operator is refused",
            refused.status_code == 403,
            f"HTTP {refused.status_code}",
        )
        async with harness.signed_in(app, groups="admins") as admin:
            asked_delete = await harness.post(admin, f"{index}/{doomed}/delete")
            confirm = re.search(r'name="confirm" value="([^"]+)"', asked_delete.text)
            deleted = await harness.post(
                admin,
                f"{index}/{doomed}/delete",
                harness.form(
                    intent=confirm.group(1) if confirm else "",
                    confirm=confirm.group(1) if confirm else "",
                ),
            )
        record(
            "console: delete asks first, then accepts",
            asked_delete.status_code == 200
            and "destroys the stored coordinates" in asked_delete.text
            and redirected(deleted)
            and "err=" not in flash_of(deleted),
            flash_of(deleted)[:90],
        )
        async with sessionmaker()() as session:
            shell = (
                await session.execute(select(Counterparty).where(Counterparty.id == doomed))
            ).scalar_one()
        record(
            "the payload is purged and the shell remains",
            shell.recipient == {}
            and shell.label == doomed_label
            and shell.archived_at is not None,
            f"recipient={shell.recipient!r}, archived_at={shell.archived_at}",
        )
        filtered_after_delete = await web.get(
            f"/transactions?type=withdrawal&customerId={customer_id}&contact={doomed}"
        )
        record(
            "the ledger filter still resolves a deleted contact, by id",
            filtered_after_delete.status_code == 200
            and "No such contact" not in filtered_after_delete.text
            and doomed_label in filtered_after_delete.text
            and "coordinates were purged" in filtered_after_delete.text,
            f"HTTP {filtered_after_delete.status_code}",
        )
        deleted_panel = await web.get(f"{index}/{doomed}/history")
        record(
            "the history panel says which kind of gone it is",
            "deleted — coordinates purged" in re.sub(r"(?s)<[^>]*>", " ", deleted_panel.text)
            and stored_account not in deleted_panel.text,
            "",
        )

    return await finish(api)


async def finish(api: httpx.AsyncClient) -> int:
    await api.aclose()
    failures = [r for r in RESULTS if r[1] == "FAIL"]
    print(
        f"\n  {len(RESULTS)} scenarios: "
        f"{sum(1 for r in RESULTS if r[1] == 'PASS')} pass, "
        f"{len(failures)} fail, "
        f"{sum(1 for r in RESULTS if r[1] == 'SKIP')} skip"
    )
    for name, _, note in failures:
        print(f"    FAIL {name} — {note}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
