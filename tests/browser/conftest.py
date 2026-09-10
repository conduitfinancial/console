"""The browser suite's rig: the real app, served for real, driven by chromium.

Everything below the browser is the deployed thing — uvicorn, the auth
middleware, CSRF, Jinja, the form engine, the operations ledger, a real local
Postgres. Only Conduit is stubbed, and it is stubbed at the HTTP layer with the
same `httpx.MockTransport` the route tests use, so the fixtures are the ones
captured from the live API (`tests/payments_fixtures`, `tests/fixtures/`).

Why a stub rather than the live host: these tests assert on *the browser's*
behaviour — a hidden field, a disabled button, one operation from two clicks —
and that has to be deterministic and offline. The live host is covered by
`tests/e2e/*`, which is a different question ("does Conduit still answer this
way?") asked on purpose against real money-shaped endpoints.

    .venv/bin/python -m playwright install chromium     # once
    .venv/bin/python -m pytest -q tests/browser

The server runs in a thread rather than a subprocess so the stub stays an
ordinary Python object: no control plane, no fixture files, no IPC. Assertions
about database rows go through a *synchronous* psycopg connection, because the
Playwright API used here is the sync one and cannot await the session fixture.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn

from tests.payments_fixtures import (
    CID,
    OTHER_CID,
    CONVERSION_QUOTE,
    EUR_ACTIVE,
    EXPIRED_QUOTE,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    ORDER,
    PAYOUT,
    PENDING,
    QUOTE,
    REGISTERED,
    USD_ACCOUNT,
    WHITELIST_PATH,
    page as conduit_page,
)
from tests.conftest import ROOT, TABLES
from tests.web_harness import PROXY_SECRET, make_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# The transfer journey's destination accounts (`tests/test_web_transfers`).
DEST_USD = {**USD_ACCOUNT, "id": "vac_dest0000000000000usd"}
DEST_EUR = {**EUR_ACTIVE, "id": "vac_dest0000000000000eur"}

ONBOARDING = fixture("onboarding_requirements_USA.json")
INDUSTRY = fixture("policy_subjects_industry.json")
ACTIVITY = fixture("policy_subjects_regulated_activity.json")

# Two applications, so "does the poller stop?" is a question about status rather
# than about mutating a stub mid-test.
APP_OPEN = {
    "id": "app_open",
    "type": "customer_onboarding",
    "status": "processing",
    "createdAt": "2026-08-28T05:00:00.000Z",
    "updatedAt": "2026-08-28T05:00:00.000Z",
}
# Approved, so it names a customer — which is what the quick view's second
# action ("Open customer") hangs off, and what the poller journey ignores.
APP_SETTLED = {**APP_OPEN, "id": "app_settled", "status": "approved", "customerId": CID}
APPLICATIONS = {a["id"]: a for a in (APP_OPEN, APP_SETTLED)}

# The amounts that select a non-default answer from the quote endpoint. Encoding
# the variation in the request keeps the stub stateless: tests run in any order
# and never have to put a mutated route back.
STALE_AMOUNT = "9.99"  # → a withdrawal quote that expired in 2020
EXPIRING_AMOUNT = "3.00"  # → a conversion quote with ~4s of life left
SLOW_AMOUNT = "7.77"  # → a conversion quote that takes ~1s, so a test can type mid-flight


def _conversion_quote(amount: str) -> dict:
    if amount != EXPIRING_AMOUNT:
        return CONVERSION_QUOTE
    soon = datetime.now(UTC) + timedelta(seconds=4)
    return {**CONVERSION_QUOTE, "expiresAt": soon.isoformat().replace("+00:00", "Z")}


def conduit_stub(request: httpx.Request) -> httpx.Response:
    """Every Conduit endpoint the journeys touch, keyed on path *and* query —
    the payout requirements differ per route, which a path-keyed map cannot say."""
    method, path, query = request.method, request.url.path, request.url.params

    if method == "GET":
        if path == "/v2/onboarding/requirements":
            # A country this org cannot onboard: the wizard renders Conduit's own
            # problem-detail with a 502, which only reaches the screen if htmx's
            # responseHandling config swaps it (base.html).
            if (query.get("country") or "").upper().startswith("Z"):
                return httpx.Response(
                    422,
                    json={
                        "type": "VALIDATION_ERROR",
                        "title": "Country not supported",
                        "detail": "Onboarding is not available for that jurisdiction.",
                        "correlationId": "cor_browser_1",
                    },
                )
            return httpx.Response(200, json=ONBOARDING)
        if path == "/v2/onboarding/policy-subjects":
            return httpx.Response(
                200, json=INDUSTRY if query.get("axis") == "INDUSTRY" else ACTIVITY
            )
        if path == "/v2/payouts/requirements":
            gated = query.get("purpose") == "intercompany"
            return httpx.Response(
                200, json=FEDWIRE_INTERCOMPANY if gated else FEDWIRE_BUSINESS
            )
        if path == "/v2/customers":  # the Transact page's move-money launcher
            return conduit_page(
                [
                    {"id": CID, "legalName": "ZZZTEST Ltd", "type": "business"},
                    {"id": OTHER_CID, "legalName": "ZZZTEST Globex", "type": "business"},
                ]
            )
        if path == f"/v2/customers/{CID}/virtual-accounts":
            return conduit_page([USD_ACCOUNT, EUR_ACTIVE])
        # The transfer journey's destination customer: one USD account (payable
        # from the USD source) and one EUR account (offered disabled, with the
        # reason on the option).
        if path == f"/v2/customers/{OTHER_CID}/virtual-accounts":
            return conduit_page([DEST_USD, DEST_EUR])
        if path == WHITELIST_PATH:
            return conduit_page([REGISTERED, PENDING])
        if path == "/v2/applications":
            return conduit_page(list(APPLICATIONS.values()))
        if path.startswith("/v2/applications/"):
            found = APPLICATIONS.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if path == "/v2/rfis":
            return conduit_page([])
        if path == "/v2/transactions":
            return conduit_page([PAYOUT])
        if path.startswith("/v2/transactions/"):
            return httpx.Response(200, json=PAYOUT)
        if path == "/v2/orders":
            return conduit_page([ORDER])
        if path.startswith("/v2/orders/"):
            return httpx.Response(200, json=ORDER)

    if method == "POST":
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.read() or b"{}")
        if path == "/v2/onboarding":
            return httpx.Response(202, json=APP_OPEN)
        if path == "/v2/documents":
            return httpx.Response(201, json={"id": "doc_browser_1"})
        if path == "/v2/quotes":
            amount = str(body.get("amount") or "")
            if body.get("destinationCountry"):  # withdrawal mode (payout pricing)
                return httpx.Response(
                    201, json=EXPIRED_QUOTE if amount == STALE_AMOUNT else QUOTE
                )
            if amount == SLOW_AMOUNT:
                # Blocks this thread on purpose: the browser needs a window in
                # which the request is genuinely in flight and the operator can
                # still type into the form that is about to be swapped away.
                time.sleep(1.0)
            return httpx.Response(201, json=_conversion_quote(amount))
        if path == "/v2/payouts":
            return httpx.Response(202, json=PAYOUT)
        if path == "/v2/orders":
            return httpx.Response(202, json=ORDER)
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"id": path.split("/")[-2]})

    return httpx.Response(
        404,
        json={"type": "NOT_FOUND", "title": f"no browser stub for {method} {path}"},
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def live_server() -> str:
    """The console on a real port, for the whole session."""
    app = make_app(conduit_stub)
    stubbed = app.state.conduit

    # `create_app`'s lifespan builds a *real* pooled ConduitClient on startup,
    # which under uvicorn would replace the mock transport. Swapping the
    # lifespan is what keeps this suite offline.
    @asynccontextmanager
    async def offline(_app):
        _app.state.conduit = stubbed
        try:
            yield
        finally:
            await stubbed.aclose()

    app.router.lifespan_context = offline

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the live server did not start"
    try:
        yield f"http://localhost:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    """Overrides pytest-base-url's fixture, so `page.goto("/drafts")` works."""
    return live_server


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Every request carries what the auth proxy would inject: the shared secret
    plus the identity headers. Without the secret the app refuses the lot —
    which is the point of AUTH_MODE=proxy."""
    return {
        **browser_context_args,
        "extra_http_headers": {
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "ops@example.com",
            "X-Auth-Request-Email": "ops@example.com",
            "X-Auth-Request-Groups": "ops",
        },
    }


# --- database assertions (sync: the Playwright API used here is the sync one) ---------


def _dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def sql(query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else []


# The two package-level fixtures, re-declared **synchronously** for this
# directory. Playwright's sync API runs inside its own greenlet-driven loop, and
# pytest-asyncio cannot set up an async fixture for a test that is not itself a
# coroutine — so `tests/conftest.py`'s async `schema`/`clean_tables` would error
# out every browser test. Same work, no event loop: alembic runs in a
# subprocess, the truncate goes through the same sync connection the assertions
# use.


@pytest.fixture(scope="session", autouse=True)
def schema():
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def clean_tables(schema):
    sql(f"truncate {TABLES} restart identity cascade")
