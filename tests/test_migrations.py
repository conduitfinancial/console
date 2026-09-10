"""Migrations round-trip against the real database."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.config import get_settings
from tests.conftest import ROOT, alembic_config

TABLES = {
    "operations",
    "webhook_events",
    "audit_events",
    "projections",
    "drafts",
    "document_blobs",
    "counterparties",
    "payout_batches",
    "payout_batch_rows",
    "session_revocations",
    "operation_intents",
}

PARTIAL_INDEX = """
select indexdef from pg_indexes
where tablename = 'operations' and indexname = 'uq_operations_active_request'
"""


def test_downgrade_then_upgrade(schema):
    cfg = alembic_config()
    engine = create_engine(get_settings().database_url)

    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert TABLES - set(inspect(conn).get_table_names()) == TABLES

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert TABLES <= set(inspect(conn).get_table_names())
        indexdef = conn.execute(text(PARTIAL_INDEX)).scalar_one()

    # The double-submit guard (OPERATIONS_SPEC §1) must be a *partial* unique index.
    assert "UNIQUE INDEX" in indexdef
    assert "type, request_hash" in indexdef
    # `stalled` is inside the predicate: it is unresolved and retryable, so an
    # identical resubmit must collide with it rather than mint a second
    # idempotency key (spec corrected 2026-08-28).
    for state in ("created", "in_flight", "outcome_unknown", "stalled"):
        assert state in indexdef
    for state in ("confirmed", "rejected", "abandoned"):
        assert state not in indexdef
    engine.dispose()


INTENT_INDEX = """
select indexdef from pg_indexes
where tablename = 'operations' and indexname = 'uq_operations_intent'
"""


def test_the_intent_nonce_is_unique_in_every_state(schema):
    """The counterpart of the partial guard above: the nonce has to keep matching
    after an operation settles, which is the whole point of it (OPERATIONS_SPEC
    §1) — so its index carries no state predicate at all."""
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        indexdef = conn.execute(text(INTENT_INDEX)).scalar_one()
    assert "UNIQUE INDEX" in indexdef and "(intent)" in indexdef
    assert "state" not in indexdef
    # Partial only on NULL: an operation opened by something other than a form
    # render carries no nonce, and those must not collide with each other.
    assert "intent IS NOT NULL" in indexdef
    engine.dispose()


# --- concurrent `alembic upgrade head` (deploy/README §4) -----------------------------

CONCURRENT_DB = "conduit_console_concurrent_test"


def _named(database: str) -> str:
    """The configured DSN pointed at another database on the same server.
    `make_url` rather than a regex: the test DSN carries `?host=/tmp`, and a
    path-shaped query parameter is exactly what a regex gets wrong."""
    url = make_url(get_settings().database_url).set(database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def fresh_database():
    """An empty database, created and dropped around the test. `create/drop
    database` cannot run inside a transaction, hence AUTOCOMMIT."""
    admin = create_engine(_named("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'drop database if exists "{CONCURRENT_DB}" with (force)'))
        conn.execute(text(f'create database "{CONCURRENT_DB}"'))
    try:
        yield _named(CONCURRENT_DB)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'drop database if exists "{CONCURRENT_DB}" with (force)'))
        admin.dispose()


def test_two_concurrent_upgrades_produce_one_schema(fresh_database):
    """`RUN_MIGRATIONS=1` on two replicas starting together (alembic/env.py).

    Without the advisory lock both processes read "current is base", both run
    the first revision, and the loser dies on `relation already exists`. With
    it, the second waits and then finds nothing to do.
    """
    environment = {**os.environ, "DATABASE_URL": fresh_database}
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [
            pool.submit(
                subprocess.run,
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=str(ROOT),
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            for _ in range(2)
        ]
        results = [run.result() for run in runs]

    for result in results:
        assert result.returncode == 0, result.stderr

    migrated = create_engine(fresh_database)
    with migrated.connect() as conn:
        assert TABLES <= set(inspect(conn).get_table_names())
        # One row, one head: neither runner double-stamped the version table.
        assert conn.execute(text("select count(*) from alembic_version")).scalar_one() == 1
    migrated.dispose()

    # Exactly one of the two did the work; the other found the schema at head.
    ran = [r for r in results if "Running upgrade" in r.stderr]
    assert len(ran) == 1, [r.stderr for r in results]


COUNTERPARTY_INDEXES = """
select indexname, indexdef from pg_indexes where tablename = 'counterparties'
"""


def test_counterparties_are_scoped_and_labelled_uniquely_among_the_living(schema):
    """Migration 0007. Two index facts are load-bearing, not decorative.

    The customer index is what makes "this customer's address book" the shape of
    every read (`app.counterparties.rows` filters on it and nothing else does the
    scoping). The label index is the *update-on-same-name* semantics: without
    `unique` it is a hint, without the `archived_at is null` predicate an archived
    row blocks a new one of the same name forever, and without `lower(label)`
    "Globex" and "globex" are two pickable rows an operator cannot tell apart.
    """
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        indexes = dict(conn.execute(text(COUNTERPARTY_INDEXES)).all())
    engine.dispose()

    label = indexes["uq_counterparties_live_label"]
    assert "UNIQUE INDEX" in label
    assert "customer_id" in label and "lower((label)::text)" in label
    assert "archived_at IS NULL" in label

    scope = indexes["ix_counterparties_customer"]
    assert "customer_id" in scope and "archived_at IS NULL" in scope


BATCH_ROW_INDEXES = """
select indexname, indexdef from pg_indexes where tablename = 'payout_batch_rows'
"""
BATCH_STATUS_CHECK = """
select pg_get_constraintdef(oid) from pg_constraint where conname = 'ck_payout_batches_status'
"""


def test_batch_rows_are_numbered_uniquely_within_their_batch(schema):
    """Migration 0008. `(batch_id, row_number)` is unique because dispatch derives
    each row's intent nonce from exactly that pair (OPERATIONS_SPEC §1): two rows
    sharing a number would be two payouts resolving to one operation, which is a
    payment silently not made."""
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        indexes = dict(conn.execute(text(BATCH_ROW_INDEXES)).all())
        statuses = conn.execute(text(BATCH_STATUS_CHECK)).scalar_one()
    engine.dispose()

    number = indexes["uq_payout_batch_rows_number"]
    assert "UNIQUE INDEX" in number and "batch_id" in number and "row_number" in number
    # The two dispatch states are declared up front so dispatch migrates no constraint.
    for status in ("validating", "ready", "partially_dispatched", "dispatched", "abandoned"):
        assert status in statuses


BATCH_COLUMNS = """
select table_name, column_name, is_nullable from information_schema.columns
where table_name in ('payout_batches', 'payout_batch_rows')
  and column_name = 'purpose'
"""


def test_purpose_moves_from_the_batch_to_the_row(schema):
    """Migration f2a7c31e9b04. A batch is a corridor and one file may carry seven
    purposes, so the column that used to identify the batch identifies the row."""
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        found = {
            (table, column): nullable
            for table, column, nullable in conn.execute(text(BATCH_COLUMNS)).all()
        }
    engine.dispose()
    assert ("payout_batches", "purpose") not in found
    assert found[("payout_batch_rows", "purpose")] == "NO"


def test_the_purpose_move_round_trips_with_its_data(schema):
    """Up and down, with rows in the table: the upgrade backfills every row from
    its batch (which is what a single-purpose batch meant) and the downgrade puts
    the first row's purpose back on the batch, because a mixed batch cannot be
    put into one column and the first row is the honest choice."""
    cfg = alembic_config()
    engine = create_engine(get_settings().database_url)

    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into payout_batches (id, customer_id, rail, recipient_type, "
                "destination_country, virtual_account_id, asset, fingerprint, "
                "template_fingerprint, filename, actor_id, actor_email) values "
                "('11111111-1111-1111-1111-111111111111', 'cus_mig', 'fedwire', 'business', "
                "'USA', 'vac_1', 'USD', 'f', 'f', 'b.csv', 'u', 'u@example.com')"
            )
        )
        for number, purpose in ((1, "payroll"), (2, "intercompany")):
            conn.execute(
                text(
                    "insert into payout_batch_rows (id, batch_id, row_number, purpose, amount) "
                    "values (gen_random_uuid(), "
                    "'11111111-1111-1111-1111-111111111111', :n, :p, '1.00')"
                ),
                {"n": number, "p": purpose},
            )

    command.downgrade(cfg, "e1c7a4b93d52")
    with engine.connect() as conn:
        assert (
            conn.execute(text("select purpose from payout_batches")).scalar_one() == "payroll"
        )
        assert "purpose" not in {
            row[0]
            for row in conn.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_name = 'payout_batch_rows'"
                )
            ).all()
        }

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        # Every row now carries the batch's purpose — the single-purpose meaning,
        # preserved rather than invented.
        assert sorted(
            row[0]
            for row in conn.execute(text("select purpose from payout_batch_rows")).all()
        ) == ["payroll", "payroll"]
    # This test seeded rows the rest of the suite must not see.
    with engine.begin() as conn:
        conn.execute(text("delete from payout_batches where customer_id = 'cus_mig'"))
    engine.dispose()


# --- migration b8e41d7c2a95: operation_intents ---------------------------------------------

PRIOR = "a93c15d0e7b6"  # the head b8e41d7c2a95 was written against

BACKFILLED_OP = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
BACKFILLED_INTENT = "3f1c2ad4-5e6b-4788-9a01-b2c3d4e5f601"

LIVE_OPERATION = """
insert into operations (id, type, actor_id, actor_email, request_path, request_hash,
                        idempotency_key, intent, state)
values (:op, 'payout_create', 'a', 'a@example.test', '/v2/payouts', 'hash',
        gen_random_uuid(), :intent, 'confirmed')
"""


def test_the_intent_backfill_carries_every_live_nonce_forward(schema):
    """Migration `b8e41d7c2a95`.

    `operations.by_intent` reads `operation_intents` and nowhere else after this
    revision, so a deploy that created the table without filling it would forget
    every nonce a live browser tab is holding — and the next replay of one of
    those forms would read as a deliberate new attempt and pay again. The
    backfill is the migration's whole safety property, and the round-trip test
    above runs it over an empty table, which proves exactly nothing about it.
    """
    cfg = alembic_config()
    engine = create_engine(get_settings().database_url)

    command.downgrade(cfg, PRIOR)
    with engine.begin() as conn:
        assert "operation_intents" not in inspect(conn).get_table_names()
        conn.execute(text(LIVE_OPERATION), {"op": BACKFILLED_OP, "intent": BACKFILLED_INTENT})

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        carried = conn.execute(
            text("select operation_id from operation_intents where intent = :i"),
            {"i": BACKFILLED_INTENT},
        ).scalar_one()
    assert str(carried) == BACKFILLED_OP

    with engine.begin() as conn:
        conn.execute(text("delete from operations where id = :op"), {"op": BACKFILLED_OP})
    engine.dispose()
