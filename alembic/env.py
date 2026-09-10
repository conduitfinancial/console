"""Alembic environment.

Migrations run on psycopg's *sync* driver — same DATABASE_URL, no
async ceremony. Only the app itself needs the async engine.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import Base
from app import models  # noqa: F401 — registers tables on Base.metadata

if context.config.config_file_name:
    # disable_existing_loggers=False: a migration run inside the app's process
    # (or a test session) must not silence every logger that already exists.
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    # postgresql+psycopg:// serves both create_engine (sync) and
    # create_async_engine (async), so the app URL needs no rewriting.
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


# One fixed key, for one purpose: "somebody is migrating this database". Any
# constant would do; this one is arbitrary and must simply never change.
MIGRATION_LOCK_KEY = 8244170313  # `conduit-console schema`


def run_migrations_online() -> None:
    """Migrate, holding a Postgres advisory lock for the whole run.

    `RUN_MIGRATIONS=1` on more than one replica means several processes call
    `alembic upgrade head` at the same moment on a cold start. Alembic locks the
    `alembic_version` row, which serialises the *writes* — but two runners can
    still both read "current is X", both decide to run the same revision, and
    the loser then fails on a duplicate object (`relation already exists`),
    taking a replica down with it on the one day the whole fleet restarts.

    A session-level advisory lock makes the second runner wait instead: it
    blocks here, and by the time it gets the lock the schema is already at head,
    so it runs nothing and exits 0. Session-level (not `_xact_`) so it survives
    the commits Alembic makes between revisions.

    Compose's one-shot `migrate` service is still the pattern to copy — this is
    the belt for deployments that cannot express "run once, then start N".
    """
    engine = create_engine(_url(), poolclass=None)
    with engine.connect() as connection:
        connection.execute(text("select pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        connection.commit()  # session locks outlive the transaction that took them
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("select pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
