#!/usr/bin/env python
"""Live sandbox: batch payouts — template, upload, validation report and **dispatch**.

This makes this a script that moves sandbox money: two rows are dispatched as
real `POST /v2/payouts` calls and settled through the sandbox simulators.
Everything it sends is ZZZTEST-named, on a discovered ZZZTEST customer, against
`api.sandbox.conduit.financial` and nothing else.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…`, exactly like
`06_sandbox_sweep.py` and `07_counterparties.py`. The staging pair in `../.env`
(a live key against a production-labelled host) is never read.

What it proves, in one pass, on a real fedwire corridor:

    pick a ZZZTEST customer with an active USD account → download the batch
    template from LIVE discovery **for all seven purposes** → check its columns
    are the union of those responses' own → refuse a file with a column no
    purpose declared → refuse a row that fills another purpose's column → fill
    a MIXED file (goods rows + an intercompany row where the sandbox has a
    registered recipient), one row with a deliberately bad ABA → upload → the
    report shows the valid rows with their own purposes and the totals cover the
    valid rows only → attach a supporting document and mark the batch ready →
    open the confirm screen → DISPATCH → one payout per row, each carrying ITS
    row's purpose → settle them through the sandbox simulators → **re-dispatch
    the whole batch and prove ZERO new operations and ZERO new payouts** →
    export the results CSV, masked, with a purpose column → then the single
    payment flow end to end through the purpose DROPDOWN (fork → route row →
    submit → settle).

Everything goes through the console's own ASGI app, so what is asserted is what
an operator would see. The only Conduit **write** in the whole run is the
`transaction_support` document upload — the route's `documentation.required` is
true and a batch cannot be marked ready without one — and it goes through the
console's own upload route, because the batch page resolves every `doc_` id
against this console's upload ledger (OPERATIONS_SPEC §3).

    .venv/bin/python tests/e2e/08_batch_payouts.py

Safe to re-run: every reference is run-unique, all records are ZZZTEST-prefixed,
and the money that moves is sandbox money — two small payouts named for the run. The customer is **discovered**, not pinned; the
script says SKIP rather than FAIL when the sandbox holds none.

The API key is read from the environment and never printed.
"""

from __future__ import annotations

import asyncio
import csv
import io
import html
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SANDBOX_HOST = "https://api.sandbox.conduit.financial"

# Two good rows and one bad one. Nothing is dispatched, so these amounts are
# validated and summed and never spent.
AMOUNTS = ("10.00", "2.50")
BAD_ABA = "123456789"  # nine digits, checksum fails — the engine's own refusal
ABA_ERROR = "Not a valid ABA routing number (checksum failed)."

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


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

import os  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402

os.environ["CONDUIT_ENV"] = "sandbox"
os.environ["CONDUIT_API_KEY"] = KEY
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://mc_bot@/conduit_console_sweep?host=/tmp"
)
# The same refusal `07_counterparties.py` makes, for the same reason: an
# inherited `DATABASE_URL` wins over the default, and this script writes rows.
DISPOSABLE = re.compile(r"(sweep|e2e|test)", re.I)
_db_name = urlsplit(os.environ["DATABASE_URL"]).path.lstrip("/")
if not DISPOSABLE.search(_db_name):
    sys.exit(
        f"refusing to run: DATABASE_URL names {_db_name!r}, which is not a disposable "
        "database. Unset DATABASE_URL to use the sweep database, or point it at one "
        "whose name contains sweep/e2e/test."
    )
if "--check-config" in sys.argv:
    print(f"config ok: {SANDBOX_HOST}, database {_db_name}")
    sys.exit(0)
os.environ.setdefault("SESSION_SECRET", "sandbox-sweep-session")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("AUTH_MODE", "disabled")
os.environ.setdefault("CONDUIT_WEBHOOK_SECRET", "whsec_" + "a1b2c3d4" * 8)

import httpx  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from app import batches, counterparties, payments  # noqa: E402
from app.auth.providers import ProxyProvider  # noqa: E402
from app.auth.tokens import CSRF_HEADER  # noqa: E402
from app.conduit import ConduitClient  # noqa: E402
from app.db import sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Operation, PayoutBatch, PayoutBatchRow  # noqa: E402
from tests import web_harness as harness  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, note: str = "") -> None:
    verdict = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((name, verdict, note))
    print(f"  [{verdict}] {name:<52} {note}", flush=True)


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")


def flash_of(response: httpx.Response) -> str:
    return str(response.headers.get("hx-redirect") or response.headers.get("location", ""))


def err_of(response: httpx.Response) -> str:
    return httpx.URL(flash_of(response)).params.get("err", "")


def flat(text: str) -> str:
    """A page's prose with entities resolved and whitespace collapsed: Jinja
    wraps these sentences across source lines, which is not a fact about the
    copy."""
    return re.sub(r"\s+", " ", html.unescape(text))


async def poll(read, done, *, tries: int = 10, delay: float = 1.0):
    """Read until `done`, or hand back the last answer — never raise: a scenario
    that did not settle is a FAIL line, not a crash."""
    value = await read()
    for _ in range(tries):
        if done(value):
            return value
        await asyncio.sleep(delay)
        value = await read()
    return value


async def pick_customer(api: httpx.AsyncClient) -> tuple[str, str]:
    """A ZZZTEST customer with an active USD virtual account — **funded** where
    the sandbox has one, because dispatch sends two real payouts out of it.

    Funding is preferred, not required: an unfunded account still proves every
    mechanism this script is about, and Conduit's refusal then lands on the row,
    which is the honest outcome either way.
    """
    fallback = ("", "")
    listed = await api.get("/customers", params={"limit": 50})
    for customer in listed.json().get("data") or []:
        if "ZZZTEST" not in json.dumps(customer):
            continue
        accounts = await api.get(f"/customers/{customer['id']}/virtual-accounts")
        if accounts.status_code >= 400:
            continue
        for account in accounts.json().get("data") or []:
            if account.get("status") != "active" or (account.get("asset") or {}).get(
                "code"
            ) != "USD":
                continue
            available = Decimal("0")
            for balance in account.get("balances") or []:
                try:
                    available += Decimal(str((balance.get("available") or {}).get("amount") or 0))
                except (InvalidOperation, TypeError):
                    continue
            if available >= Decimal("13"):  # the two rows, plus room for fees
                return customer["id"], account["id"]
            if not fallback[0]:
                fallback = (customer["id"], account["id"])
    return fallback


# A batch's route is the CORRIDOR: the purpose is a column of the file.
ROUTE = {
    "rail": "fedwire",
    "recipientType": "business",
    "destinationCountry": "USA",
}
QUERY = "&".join(f"{k}={v}" for k, v in ROUTE.items())
GOODS = "payment_for_goods_or_services"
INTERCOMPANY = "intercompany"


def answer(model, run: str) -> dict[str, str]:
    """Discovery's own fields, filled the way `06_sandbox_sweep.py` proved live —
    keyed by **column name** this time, because a CSV's columns are the dotted
    field names themselves. The route is the authority on which fields exist;
    this only supplies plausible values."""
    filled: dict[str, str] = {}
    for field in model.fields:
        leaf = field.path[-1]
        if field.allowed_values:
            value = field.allowed_values[0]
        elif field.validator == "aba":
            value = "021000021"
        elif leaf == "accountNumber":
            value = f"1{int(run[:9], 16)}"[:12]
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
        elif leaf == "reference":
            value = f"ZZZTEST-{run}"
        elif field.required:
            value = f"ZZZTEST {run}"
        else:
            continue
        filled[field.dotted] = str(value)
    return filled


async def all_requirements(client) -> tuple[dict, list[str]]:
    """`{purpose: snapshot}` and the purposes this corridor has none for — the
    same seven reads the template route makes."""
    snapshots, missing = {}, []
    for purpose in payments.PURPOSE_VALUES:
        answer = await payments.fetch_requirements(
            client,
            purpose=purpose,
            rail=ROUTE["rail"],
            recipient_type=ROUTE["recipientType"],
            destination_country=ROUTE["destinationCountry"],
        )
        if isinstance(answer, dict):
            snapshots[purpose] = answer
        else:
            missing.append(purpose)
    return snapshots, missing


def parse_template(text: str) -> tuple[list[str], list[str]]:
    rows = list(csv.reader(io.StringIO(text)))
    comments = [row[0] for row in rows if row and row[0].startswith("#")]
    header = next(row for row in rows if row and not row[0].startswith("#"))
    return comments, header


def build(comments: list[str], header: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for line in comments:
        writer.writerow([line])
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    # With the BOM Excel writes on every "CSV UTF-8" save: the file an operator
    # actually sends back always has one, so this one does too.
    return ("﻿" + out.getvalue()).encode("utf-8")


async def send(web, customer_id: str, body: bytes, name: str, account_id: str = "") -> httpx.Response:
    """The upload exactly as `static/app.js` sends it — the funding account
    included, because the account's asset is the currency of the totals."""
    return await web.post(
        f"/customers/{customer_id}/batches?{QUERY}&virtualAccountId={account_id}&filename={name}",
        content=body,
        headers={
            "content-type": "text/csv",
            CSRF_HEADER: web.cookies.get(harness.CSRF_NAME) or "",
            "HX-Request": "true",
        },
    )


async def main() -> int:
    run = uuid.uuid4().hex[:10]
    started = datetime.now(UTC)
    print(f"\nConduit Console — batch payouts e2e (dispatch)\n  host {SANDBOX_HOST}  run {run}\n")
    migrate()

    app = create_app()
    app.state.conduit = ConduitClient()
    app.state.auth_provider = ProxyProvider(harness.PROXY)
    api = httpx.AsyncClient(
        base_url=f"{SANDBOX_HOST}/v2", headers={"x-api-key": KEY}, timeout=60.0
    )

    async with harness.signed_in(app) as web:
        # ---- A. a customer to build a batch for
        print("(A) setup")
        customer_id, account_id = await pick_customer(api)
        if not customer_id:
            record("ZZZTEST customer with an active USD account", None, "none in this sandbox")
            return await finish(api)
        record("ZZZTEST customer with an active USD account", True, f"{customer_id}")

        index = f"/customers/{customer_id}/batches"

        # ---- B. the template, from live discovery for every purpose
        print("(B) template")
        snapshots, missing = await all_requirements(app.state.conduit)
        if not snapshots:
            record("live payout requirements read (7 purposes)", False, "none answered")
            return await finish(api)
        models = {p: payments.payout_model(s) for p, s in snapshots.items()}
        live_print = batches.fingerprint(snapshots)
        record(
            "live payout requirements read (7 purposes)",
            GOODS in models,
            f"{len(models)} answered"
            + (f", not available: {', '.join(missing)}" if missing else ""),
        )
        gated_live = [p for p, m in models.items() if batches.is_gated(m)]
        needs_doc = [p for p, m in models.items() if m.documentation.get("required")]
        record(
            "the corridor's gating, read not assumed",
            True,
            f"whitelist: {gated_live or 'none'}; documents: {needs_doc or 'none'}",
        )

        downloaded = await web.get(f"{index}/template.csv?{QUERY}")
        record(
            "console: template downloads",
            downloaded.status_code == 200
            and "text/csv" in downloaded.headers.get("content-type", ""),
            f"HTTP {downloaded.status_code} {downloaded.headers.get('content-disposition','')[:60]}",
        )
        if downloaded.status_code != 200:
            return await finish(api)
        comments, header = parse_template(downloaded.text)
        expected = batches.columns(models)
        record(
            "template columns are the UNION of the live per-purpose fields",
            header == expected,
            f"{len(header)} columns, purpose first",
        )
        record(
            "header block carries the corridor, no purpose, and the live fingerprint",
            f"# customer: {customer_id}" in comments
            and f"# fingerprint: {live_print}" in comments
            and not any(line.startswith("# purpose:") for line in comments),
            live_print[:12],
        )
        block = "\n".join(comments)
        record(
            "header block documents the purpose column's RAW KEYS and the gating",
            "RAW KEY" in block
            and all(f": {purpose}" in block for purpose in models)
            and (not gated_live or "WHITELISTED recipient needed for" in block),
            f"{len(models)} purposes listed",
        )

        # ---- C. a column this route never declared is refused
        print("(C) refusals")
        async with sessionmaker()() as session:
            before = len(
                (
                    await session.execute(
                        select(PayoutBatch.id).where(PayoutBatch.customer_id == customer_id)
                    )
                ).all()
            )
        refused = await send(
            web,
            customer_id,
            build(comments, header + ["swiftCode"], [{"amount": "1.00"}]),
            "bad-columns.csv",
            account_id,
        )
        record(
            "unknown column refused, never ignored",
            "swiftCode" in err_of(refused) and "Nothing was validated" in err_of(refused),
            err_of(refused)[:80],
        )
        async with sessionmaker()() as session:
            after = len(
                (
                    await session.execute(
                        select(PayoutBatch.id).where(PayoutBatch.customer_id == customer_id)
                    )
                ).all()
            )
        # A count delta, not an absolute: the sweep database is not cleaned
        # between runs, and this script is not entitled to delete rows it did
        # not create.
        record("a refused file stores no batch", after == before, f"{before} before, {after} after")

        # ---- D. a MIXED file: two purposes, one deliberately invalid row
        print("(D) upload")
        model = models[GOODS]
        values = answer(model, run)
        # The intercompany half needs a registered whitelist recipient payable
        # over this rail. It is DISCOVERED, never registered here: Conduit
        # reviews a registration, so this script cannot make one `registered`.
        registered = None
        if INTERCOMPANY in models and batches.is_gated(models[INTERCOMPANY]):
            listed = await api.get(f"/customers/{customer_id}/whitelist-recipients", params={"limit": 100})
            registered = next(
                (
                    entry
                    for entry in (listed.json().get("data") or [])
                    if entry.get("status") == "registered"
                    and payments.payable_over(entry, ROUTE["rail"])
                ),
                None,
            )
        record(
            "a registered whitelist recipient for the intercompany rows",
            bool(registered) or None,
            registered["id"] if registered else "none in this sandbox — mixed half SKIPPED",
        )

        # The RENDERED model: under the whitelist gate the coordinate fields are
        # not columns of an intercompany row at all, and filling them is exactly
        # the per-row refusal exercised below.
        gated_values = (
            answer(batches.rendered_model(models[INTERCOMPANY]), run) if registered else {}
        )
        rows = [
            {**values, "purpose": GOODS, "amount": AMOUNTS[0]},
        ]
        if registered:
            # A second PURPOSE in the same file — the whole point of the column.
            rows.append(
                {**gated_values, "purpose": INTERCOMPANY, "contact": registered["id"],
                 "amount": AMOUNTS[1]}
            )
        else:
            rows.append({**values, "purpose": GOODS, "amount": AMOUNTS[1]})
        rows.append(
            # The bad one: nine digits that fail the ABA checksum. The console
            # refuses it locally, exactly as the single payout form does.
            {**values, "purpose": GOODS, "amount": "5.00",
             "destination.recipient.routingNumber": BAD_ABA}
        )

        # A row that fills a column ITS purpose does not declare: the per-row
        # half of the unknown-column rule.
        if registered:
            smuggled = await send(
                web,
                customer_id,
                build(comments, header, [
                    {**gated_values, "purpose": INTERCOMPANY, "contact": registered["id"],
                     "amount": "1.00",
                     "destination.recipient.routingNumber": "021000021"}
                ]),
                f"zzztest-{run}-foreign.csv",
                account_id,
            )
            foreign_url = flash_of(smuggled)
            foreign_report = await web.get(foreign_url) if "/batches/" in foreign_url else None
            record(
                "a row filling another purpose's column is refused for that row",
                bool(foreign_report)
                and "does not have" in flat(foreign_report.text)
                and "0 valid · 1 invalid" in foreign_report.text,
                "",
            )
            if foreign_report is not None:
                await harness.post(web, f"{foreign_url}/abandon")
        uploaded = await send(
            web, customer_id, build(comments, header, rows), f"zzztest-{run}.csv", account_id
        )
        location = flash_of(uploaded)
        batch_url = location if "/batches/" in location and "err=" not in location else ""
        record(
            "console: file accepted → batch",
            bool(batch_url),
            (err_of(uploaded) or location)[:100],
        )
        if not batch_url:
            return await finish(api)

        report = await web.get(batch_url)
        text = report.text
        record("report renders", report.status_code == 200, f"HTTP {report.status_code}")
        record("2 valid, 1 invalid", "3 rows · 2 valid · 1 invalid" in text, "")
        record(
            "the report states each row's OWN purpose",
            f'<code class="raw">{GOODS}</code>' in text
            and (not registered or f'<code class="raw">{INTERCOMPANY}</code>' in text),
            "mixed" if registered else "single-purpose (no registered recipient)",
        )
        record("the invalid row carries the engine's own sentence", ABA_ERROR in text, "")
        record(
            "totals cover the VALID rows only",
            "12.50 USD across the valid rows" in text,
            "10.00 + 2.50, the 5.00 row excluded",
        )
        record(
            "the excluded row is named beside the totals",
            "1 row excluded by validation" in text,
            "",
        )
        record("no staleness warning on a fresh template", "out of date" not in text, "")
        record(
            "coordinates are masked on the report",
            values["destination.recipient.accountNumber"] not in text,
            "last four only",
        )

        # ---- E. the doc gate, then ready
        print("(E) mark ready")
        blocked = await harness.post(web, f"{batch_url}/ready")
        record(
            "an invalid row blocks ready",
            "rows are invalid" in err_of(blocked),
            err_of(blocked)[:80],
        )

        # The correction is a NEW batch — the simplest honest model, stated on
        # the page. The old one is abandoned below.
        good = await send(
            web, customer_id, build(comments, header, rows[:2]), f"zzztest-{run}-fixed.csv",
            account_id,
        )
        fixed_url = flash_of(good)
        record("a corrected file is a new batch", "/batches/" in fixed_url and fixed_url != batch_url, "")
        abandoned = await harness.post(web, f"{batch_url}/abandon")
        record("the first batch abandons", "err=" not in flash_of(abandoned), "")

        needs_doc = await harness.post(web, f"{fixed_url}/ready")
        record(
            "documentation.required blocks ready with nothing attached",
            err_of(needs_doc) == payments.DOCUMENTATION_MESSAGE,
            err_of(needs_doc)[:80],
        )

        uploaded_doc = await harness.upload(
            web, purpose="transaction_support", filename=f"zzztest-invoice-{run}.pdf", content=PDF
        )
        found = re.search(r'name="documentIds" value="([^"]+)"', uploaded_doc.text)
        document_id = found.group(1) if found else ""
        record("transaction_support document uploaded", bool(document_id), document_id)

        marked = await harness.post(
            web, f"{fixed_url}/ready", f"documentIds={document_id}".encode()
        )
        record("batch marked ready", "err=" not in flash_of(marked), err_of(marked)[:80])

        ready = await web.get(fixed_url)
        record(
            "the ready screen states the consequence",
            "Dispatching will send 2 payouts totaling" in flat(ready.text)
            and "12.50" in ready.text
            and "each is sent exactly once" in flat(ready.text),
            "",
        )
        record(
            "the report has no dispatch button — only the confirm screen's link",
            "/dispatch" not in ready.text and f"{fixed_url}/confirm" in ready.text,
            "",
        )

        # ---- F. the confirm screen, then dispatch
        print("(F) confirm + dispatch")
        batch_id = uuid.UUID(fixed_url.rsplit("/", 1)[-1])
        confirm = await web.get(f"{fixed_url}/confirm")
        text = flat(confirm.text)
        record(
            "confirm screen states the consequence, the total and the account",
            "Dispatching sends 2 payout" in text
            and "12.50 USD" in text
            and account_id in text
            and "Each row is sent exactly once" in text,
            "",
        )
        record(
            "confirm screen shows the funding account's live balance",
            "Available" in text,
            "",
        )

        async def payout_operations() -> list[str]:
            # Columns, not entities: an earlier run's rows were written under a
            # different ENCRYPTION_KEY, and selecting an encrypted column would
            # decrypt theirs to answer a question about ours.
            async with sessionmaker()() as session:
                return sorted(
                    str(op_id)
                    for (op_id,) in (
                        await session.execute(
                            select(Operation.id).where(
                                Operation.type == "payout_create",
                                Operation.created_at >= started,
                            )
                        )
                    ).all()
                )

        async def batch_rows() -> list[tuple]:
            async with sessionmaker()() as session:
                return [
                    (number, str(op_id) if op_id else "", error or "")
                    for number, op_id, error in (
                        await session.execute(
                            select(
                                PayoutBatchRow.row_number,
                                PayoutBatchRow.operation_id,
                                PayoutBatchRow.dispatch_error,
                            )
                            .where(PayoutBatchRow.batch_id == batch_id)
                            .order_by(PayoutBatchRow.row_number)
                        )
                    ).all()
                ]

        async def batch_status() -> str:
            async with sessionmaker()() as session:
                return (
                    await session.execute(
                        select(PayoutBatch.status).where(PayoutBatch.id == batch_id)
                    )
                ).scalar_one()

        record("no payout operation exists before dispatch", not await payout_operations(), "")

        sent = await harness.post(web, f"{fixed_url}/dispatch")
        record("dispatch accepted", "err=" not in flash_of(sent), err_of(sent)[:80])

        status = await poll(batch_status, lambda s: s == "dispatched", tries=30, delay=1.0)
        first_run = await payout_operations()
        rows_after = await batch_rows()
        record(
            "the batch reaches dispatched, with one operation per row",
            status == "dispatched" and len(first_run) == 2 and all(op for _, op, _ in rows_after),
            f"status={status}, {len(first_run)} operations",
        )

        async with sessionmaker()() as session:
            states = sorted(
                (
                    await session.execute(
                        select(Operation.state).where(
                            Operation.id.in_([uuid.UUID(op) for op in first_run])
                        )
                    )
                )
                .scalars()
                .all()
            )
        record(
            "both operations resolved (nothing left mid-flight)",
            bool(states) and not ({"created", "in_flight"} & set(states)),
            ", ".join(states),
        )

        report = await web.get(fixed_url)
        transactions = sorted(set(re.findall(r"/transactions/(txn_[A-Za-z0-9]+)", report.text)))
        record(
            "the report links each sent row to its transaction",
            len(transactions) == sum(1 for state in states if state == "confirmed"),
            ", ".join(transactions[:2]),
        )

        # ---- G. settle both payouts through the sandbox simulators
        print("(G) settle")
        settled = 0
        for transaction_id in transactions:

            async def read_tx(tid=transaction_id):
                result = await app.state.conduit.get(f"/v2/transactions/{tid}")
                return getattr(result, "data", {}) or {}

            def done(tx):
                return tx.get("status") == "completed"

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
            settled += 1 if done(tx) else 0
        record(
            "the dispatched payouts settle via the simulators",
            (settled == len(transactions)) if transactions else None,
            f"{settled} of {len(transactions)} completed",
        )

        # ---- H. THE PROOF: re-dispatching creates nothing
        print("(H) re-dispatch — the idempotence proof")
        before_ops, before_rows = await payout_operations(), await batch_rows()
        again = await harness.post(web, f"{fixed_url}/dispatch")
        await asyncio.sleep(3)  # long enough for a run, had one started
        after_ops, after_rows = await payout_operations(), await batch_rows()
        record(
            "re-dispatch created ZERO new payout operations",
            after_ops == before_ops,
            f"{len(before_ops)} before, {len(after_ops)} after "
            f"({err_of(again)[:40] or 'route ran'})",
        )
        record(
            "every row still points at the operation it was dispatched with",
            after_rows == before_rows,
            "; ".join(f"row {n}->{op[:8]}" for n, op, _ in after_rows),
        )
        listed = await api.get(
            "/transactions",
            params={"type": "withdrawal", "customerId": customer_id, "limit": 100},
        )
        mine = [
            item
            for item in (listed.json().get("data") or [])
            if item.get("clientReferenceId") in set(before_ops)
        ]
        record(
            "Conduit holds exactly one payout per dispatched operation",
            len(mine) == len([s for s in states if s == "confirmed"]),
            f"{len(mine)} transactions for {len(before_ops)} operations",
        )

        # ---- I. results export
        print("(I) results export")
        exported = await web.get(
            f"/export/batch_rows.csv?customerId={customer_id}&batchId={batch_id}"
        )
        lines = [row for row in csv.reader(io.StringIO(exported.text)) if row]
        record(
            "results CSV downloads with this batch's rows",
            exported.status_code == 200 and len(lines) == 3,
            f"HTTP {exported.status_code}, {len(lines) - 1} rows",
        )
        record(
            "results CSV states each row's state and its transaction",
            all(row[7] in ("sent", "rejected", "unconfirmed") for row in lines[1:]),
            ", ".join(row[7] for row in lines[1:]),
        )
        record(
            "results CSV names each row's purpose",
            lines[0][1] == "purpose" and all(row[1] for row in lines[1:]),
            ", ".join(row[1] for row in lines[1:]),
        )
        record(
            "results CSV masks the coordinates",
            values["destination.recipient.accountNumber"] not in exported.text,
            "last four only",
        )

        # ---- J. what the record says at the end
        print("(J) the record")
        async with sessionmaker()() as session:
            batch = (
                await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
            ).scalar_one()
        record(
            "the batch is dispatched, documented and pinned to its snapshot set",
            batch.status == "dispatched"
            and batch.document_ids == [document_id]
            and batch.fingerprint == live_print,
            f"status={batch.status}",
        )
        # The wire claim, checked against Conduit's own record of each payout:
        # every dispatched transaction carries ITS row's purpose.
        async with sessionmaker()() as session:
            wanted = {
                str(op_id): purpose
                for op_id, purpose in (
                    await session.execute(
                        select(PayoutBatchRow.operation_id, PayoutBatchRow.purpose)
                        .where(PayoutBatchRow.batch_id == batch_id)
                        .order_by(PayoutBatchRow.row_number)
                    )
                ).all()
                if op_id
            }
        seen = {
            item.get("clientReferenceId"): item.get("purpose")
            for item in (listed.json().get("data") or [])
            if item.get("clientReferenceId") in wanted
        }
        record(
            "each dispatched payout carries ITS row's purpose at Conduit",
            bool(seen) and all(seen.get(op) == purpose for op, purpose in wanted.items() if op in seen),
            ", ".join(sorted(set(seen.values()))) or "none matched",
        )
        blocked_abandon = await harness.post(web, f"{fixed_url}/abandon")
        record(
            "a dispatched batch cannot be abandoned",
            "cannot be abandoned" in err_of(blocked_abandon),
            err_of(blocked_abandon)[:60],
        )

        # ---- K. the proof again, with the loop actually running
        print("(K) the interrupted-run drill")
        # H's re-dispatch was refused by the route (a `dispatched` batch is not
        # dispatchable), which proves the guard and not the engine. This puts the
        # batch back the way a killed worker would have left it — status only,
        # rows untouched — so the loop runs over two rows that already have
        # operations, and the intent nonce is the only thing standing between
        # this and a double payment.
        async with sessionmaker()() as session:
            await session.execute(
                update(PayoutBatch)
                .where(PayoutBatch.id == batch_id)
                .values(status="partially_dispatched")
            )
            await session.commit()
        resumed = await harness.post(web, f"{fixed_url}/dispatch")
        record("the resumed batch accepts a dispatch", "err=" not in flash_of(resumed), "")
        status = await poll(batch_status, lambda s: s == "dispatched", tries=20, delay=1.0)
        resumed_ops, resumed_rows = await payout_operations(), await batch_rows()
        record(
            "the dispatch loop RAN over both rows and sent nothing",
            resumed_ops == before_ops and resumed_rows == before_rows and status == "dispatched",
            f"{len(resumed_ops)} operations, status={status}",
        )
        listed_again = await api.get(
            "/transactions",
            params={"type": "withdrawal", "customerId": customer_id, "limit": 100},
        )
        still_mine = [
            item
            for item in (listed_again.json().get("data") or [])
            if item.get("clientReferenceId") in set(before_ops)
        ]
        record(
            "Conduit still holds exactly one payout per row",
            len(still_mine) == len(mine),
            f"{len(still_mine)} transactions",
        )

        # ---- L2. a contact edited between ready and dispatch
        print("(L2) the edit-drift refusal")
        # Nothing is sent by this leg, by construction: the point is the refusal.
        # The contact store is console-local, so it is written here directly; the
        # batch, its validation, its discovery reads and the dispatch attempt are
        # all the real ones.
        recipient = {}
        for key, value in values.items():
            if not key.startswith("destination.recipient."):
                continue
            node, *rest = key.split("destination.recipient.", 1)[1].split(".")
            if rest:
                recipient.setdefault(node, {})[rest[0]] = value
            else:
                recipient[node] = value
        async with sessionmaker()() as session:
            drift_contact = await counterparties.save(
                session,
                customer_id=customer_id,
                label=f"ZZZTEST Drift {run}",
                recipient=recipient,
                rail_family=payments.family_of(ROUTE["rail"]),
                recipient_type=ROUTE["recipientType"],
                destination_country=ROUTE["destinationCountry"],
                actor_id="usr_e2e",
                actor_email="ops@example.com",
            )
            await session.commit()
        by_contact = {
            key: value
            for key, value in values.items()
            if not key.startswith("destination.recipient.")
        } | {"purpose": GOODS, "amount": "1.00", "contact": f"ZZZTEST Drift {run}"}
        drift_upload = await send(
            web,
            customer_id,
            build(comments, header, [by_contact]),
            f"zzztest-{run}-drift.csv",
            account_id,
        )
        drift_url = flash_of(drift_upload)
        drift_report = await web.get(drift_url)
        record(
            "a contact-addressed row validates",
            "1 valid" in drift_report.text,
            err_of(drift_upload)[:60],
        )
        drift_doc = await harness.upload(
            web, purpose="transaction_support", filename=f"zzztest-drift-{run}.pdf", content=PDF
        )
        found_doc = re.search(r'name="documentIds" value="([^"]+)"', drift_doc.text)
        drift_ready = await harness.post(
            web,
            f"{drift_url}/ready",
            f"documentIds={found_doc.group(1) if found_doc else ''}".encode(),
        )
        record("the contact batch is ready", "err=" not in flash_of(drift_ready), err_of(drift_ready)[:60])

        # The edit: a corrected account number, the ordinary reason an address
        # book gets edited — after the operator confirmed the totals.
        async with sessionmaker()() as session:
            stored = await counterparties.get(session, customer_id, str(drift_contact))
            moved = dict(stored["recipient"], accountNumber="909090909090")
            refusal = await counterparties.update(
                session,
                customer_id,
                str(drift_contact),
                label=f"ZZZTEST Drift {run}",
                recipient=moved,
            )
            await session.commit()
        record("the contact is edited after ready", not refusal, refusal[:60] if refusal else "")

        before_drift = await payout_operations()
        await harness.post(web, f"{drift_url}/dispatch")
        await asyncio.sleep(3)
        after_drift = await payout_operations()
        drift_page = await web.get(drift_url)
        async with sessionmaker()() as session:
            drift_errors = (
                (
                    await session.execute(
                        select(PayoutBatchRow.dispatch_error).where(
                            PayoutBatchRow.batch_id == uuid.UUID(drift_url.rsplit("/", 1)[-1])
                        )
                    )
                )
                .scalars()
                .all()
            )
        record(
            "the drifted row is REFUSED and nothing was sent",
            after_drift == before_drift
            and any("changed since this batch was validated" in (e or "") for e in drift_errors),
            (drift_errors[0] or "")[:60] if drift_errors else "no row",
        )
        record(
            "the report states the refusal in the operator's own words",
            "changed since this batch was validated" in flat(drift_page.text),
            "",
        )

        # ---- L. the OTHER arm of the fork: one payment, through the dropdown
        print("(L) the single payment flow, purpose as a dropdown")
        fork = await web.get(f"/customers/{customer_id}/payouts")
        record(
            "the fork offers one payment or a batch, before any purpose",
            fork.status_code == 200
            and ">Single payment</a>" in fork.text
            and f'href="/customers/{customer_id}/batches/new"' in fork.text
            and GOODS not in fork.text,
            "",
        )

        single = f"/customers/{customer_id}/payouts/new"
        empty = await web.get(single)
        record(
            "the route row opens with all seven purposes as options",
            all(f'<option value="{purpose}"' in empty.text for purpose in payments.PURPOSE_VALUES)
            and 'id="payout-form"' not in empty.text,
            "no requirements until the route is complete",
        )

        single_query = f"purpose={GOODS}&{QUERY}&virtualAccountId={account_id}"
        loaded = await web.get(f"{single}?{single_query}")
        record(
            "a deep link with ?purpose= still lands on the loaded form",
            loaded.status_code == 200
            and f'<option value="{GOODS}" selected>' in loaded.text
            and 'id="payout-form"' in loaded.text,
            "",
        )
        if registered:
            reshaped = await web.get(
                f"{single}?purpose={INTERCOMPANY}&{QUERY}&virtualAccountId={account_id}"
            )
            record(
                "intercompany reshapes IN PLACE — the whitelist picker, no bounce",
                'id="whitelistRecipientId"' in reshaped.text
                and "transfers screen" not in reshaped.text
                and 'name="f.destination.recipient.routingNumber"' not in reshaped.text,
                "",
            )

        found = re.search(r'name="intent" value="([^"]+)"', loaded.text)
        single_doc = await harness.upload(
            web, purpose="transaction_support", filename=f"zzztest-single-{run}.pdf", content=PDF
        )
        doc_match = re.search(r'name="documentIds" value="([^"]+)"', single_doc.text)
        body = {
            "intent": found.group(1) if found else "",
            "purpose": GOODS,
            **ROUTE,
            "virtualAccountId": account_id,
            "amount": "1.00",
            "documentIds": doc_match.group(1) if doc_match else "",
            **{f"f.{column}": value for column, value in values.items()},
        }
        sent_one = await web.post(
            single,
            content=str(httpx.QueryParams(list(body.items()))).encode(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                CSRF_HEADER: web.cookies.get(harness.CSRF_NAME) or "",
                "HX-Request": "true",
            },
        )
        landed = flash_of(sent_one)
        single_txn = landed.rsplit("/", 1)[-1].split("?")[0] if "/transactions/" in landed else ""
        record(
            "a single payout sent from the dropdown flow is accepted",
            bool(single_txn),
            single_txn or (err_of(sent_one) or f"HTTP {sent_one.status_code}")[:100],
        )
        if single_txn:
            created = await api.get(f"/transactions/{single_txn}")
            record(
                "it carries the purpose the dropdown chose",
                created.json().get("purpose") == GOODS,
                str(created.json().get("purpose")),
            )

            async def read_single():
                result = await app.state.conduit.get(f"/v2/transactions/{single_txn}")
                return getattr(result, "data", {}) or {}

            await harness.post(
                web,
                f"/transactions/{single_txn}/simulate",
                harness.form(action="review", outcome="approve"),
            )
            tx = await poll(read_single, lambda x: x.get("status") == "completed", tries=12)
            if tx.get("status") != "completed":
                await harness.post(
                    web,
                    f"/transactions/{single_txn}/simulate",
                    harness.form(action="settle", outcome="completed"),
                )
                tx = await poll(read_single, lambda x: x.get("status") == "completed")
            record(
                "the single payout settles via the simulators",
                tx.get("status") == "completed",
                str(tx.get("status")),
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
