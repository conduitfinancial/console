"""Retention for two more encrypted columns, and session revocation

Revision ID: d7b402e9c1a5
Revises: a93c15d0e7b6
Create Date: 2026-09-02 09:00:00.000000

Two unrelated-looking changes, one phase, so one migration.

**`webhook_events.raw_body` becomes nullable.** It is the last encrypted column
with no retention job: a delivery carries `destination.recipient` whole — account
number, legal name, postal address — and 0011 encrypted it but left it forever.
The worker now NULLs it once the event is processed and older than
`WEBHOOK_RAW_RETENTION_DAYS`; the row, its status, its error and everything
projected from it stay. NULL is the purged marker, exactly as it already is for
`document_blobs.data` (`app/crypto.EncryptedBytes`: NULL stays NULL).

**`session_revocations` is new.** One row per operator whose sessions have been
cut off, holding the instant before which a session cookie is no longer honoured
— see `app/auth/revocation.py` and `scripts/revoke_sessions.py`.

The downgrade re-imposes NOT NULL, and a row already purged has nothing to put
back: it gets the ciphertext of an empty body, which is the `purge_blobs`
precedent (a NOT NULL column is emptied rather than nulled) and keeps the column
readable — a literal `''` would not decrypt.
"""

from alembic import op
import sqlalchemy as sa

from app.crypto import fernet


revision = 'd7b402e9c1a5'
down_revision = 'a93c15d0e7b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("webhook_events", "raw_body", existing_type=sa.LargeBinary(), nullable=True)
    op.create_table(
        "session_revocations",
        # The actor's `sub` — `Actor.id`, which is what the session cookie
        # carries and what the audit trail attributes to. Primary key: one
        # cut-off per operator, and the revocation SELECT is a PK lookup.
        sa.Column("sub", sa.Text(), primary_key=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_revocations")
    op.get_bind().execute(
        sa.text("update webhook_events set raw_body = :empty where raw_body is null"),
        {"empty": fernet().encrypt(b"")},
    )
    op.alter_column("webhook_events", "raw_body", existing_type=sa.LargeBinary(), nullable=False)
