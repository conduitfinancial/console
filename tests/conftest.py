"""Test harness: real local Postgres, schema built by the real migrations."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent

# Shape-correct fixture secret: `whsec_` + 64 hex, exactly as Conduit issues it.
WEBHOOK_SECRET = "whsec_" + "a1b2c3d4" * 8

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://mc_bot@/conduit_console_test?host=/tmp"
)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CONDUIT_ENV", "sandbox")
os.environ.setdefault("AUTH_MODE", "disabled")
os.environ.setdefault("CONDUIT_API_KEY", "test-key-not-real")
os.environ.setdefault("CONDUIT_WEBHOOK_SECRET", WEBHOOK_SECRET)

# `setdefault` means an inherited `DATABASE_URL` wins — and `clean_tables`
# truncates every table in it before each test. A shell exporting a real DSN, or
# `--env-file .env` on pytest, therefore empties the database somebody works in,
# encrypted drafts and all, and those have no other copy. So the resolved database
# has to *name itself* disposable first, exactly as `tests/e2e/07_counterparties.py`
# demands of the script that deletes rows. Whole words, so `latest_console` is not
# a test database; `cc_…` is this project's per-phase scratch database.
DISPOSABLE = re.compile(r"(^|_)(test|e2e|sweep)($|_)|^cc_", re.I)
DB_OVERRIDE = "ALLOW_DESTRUCTIVE_TEST_DB"


def require_disposable_database(url: str, override: str | None = None) -> str:
    """The database `url` names, once it is one this suite may truncate.

    Called at import so it lands before collection, and importable so both
    answers are covered (`test_config`) — not only the one CI happens to take.
    """
    name = urlsplit(url).path.lstrip("/")
    # Not truthiness: `false`, `no` and `0` are non-empty strings.
    if override != "1" and not DISPOSABLE.search(name):
        raise RuntimeError(
            f"refusing to run: DATABASE_URL names {name!r}, which is not a disposable "
            "database — this suite truncates every table in it before each test. Never "
            "pass `--env-file .env` to pytest: unset DATABASE_URL for the default "
            "`conduit_console_test`, or point it at a database whose name says "
            f"test/e2e/sweep (or a `cc_…` phase database). To empty {name!r} on "
            f"purpose, set {DB_OVERRIDE}=1."
        )
    return name


require_disposable_database(os.environ["DATABASE_URL"], os.environ.get(DB_OVERRIDE))

from sqlalchemy import text  # noqa: E402

from app.db import sessionmaker  # noqa: E402

TABLES = (
    "operations, audit_events, webhook_events, projections, drafts, document_blobs, "
    "counterparties, payout_batches, payout_batch_rows, session_revocations"
)


def pytest_collection_modifyitems(items):
    """Run `tests/browser` last, whatever order collection found it in.

    Playwright's synchronous API leaves an event loop running in the main thread
    for the rest of the session, and pytest-asyncio cannot set up an async
    fixture while one is running — so every async test collected *after* a
    browser test errors in `clean_tables` before it starts. The suites are
    independent, so ordering is the whole fix. (`sort` is stable: nothing else
    moves.)
    """
    items.sort(key=lambda item: "tests/browser" in item.nodeid.replace("\\", "/"))


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def schema():
    from alembic import command

    command.upgrade(alembic_config(), "head")


@pytest.fixture(autouse=True)
async def clean_tables(schema):
    async with sessionmaker()() as session:
        await session.execute(text(f"truncate {TABLES} restart identity cascade"))
        await session.commit()
    yield


@pytest.fixture
async def session():
    async with sessionmaker()() as s:
        yield s


@pytest.fixture
def actor():
    return {"actor_id": "usr_1", "actor_email": "operator@example.com"}


@contextmanager
def settings_override(**values):
    """Poke the cached Settings for one test, then put it back."""
    from app.config import get_settings

    settings = get_settings()
    previous = {k: getattr(settings, k) for k in values}
    for key, value in values.items():
        setattr(settings, key, value)
    try:
        yield settings
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)
