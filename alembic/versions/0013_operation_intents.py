"""operation_intents: every nonce that ever resolved to an operation

Revision ID: b8e41d7c2a95
Revises: a93c15d0e7b6
Create Date: 2026-09-02 10:00:00.000000

`operations.intent` records the nonce that CREATED a row and
nothing else, so a submit whose nonce missed and which the active-state
request-hash guard then resolved to an existing operation left its own nonce
recorded nowhere. Once that operation went terminal the hash guard released, and
a mechanical re-POST of the same form carrying that same nonce missed both
guards and minted a second payment with a second idempotency key — which is the
exact failure the nonce was added to prevent (OPERATIONS_SPEC §1).

The mapping becomes its own table: one row per nonce, pointing at whichever
operation that nonce reached. `by_intent` reads this table alone.

**The backfill is not optional.** Every operation already on the table has a
nonce that some browser tab may still be holding, and after this migration
`by_intent` looks nowhere else — so an un-backfilled deployment would forget
every live form's nonce at the moment of deploy and treat the next replay as a
new attempt. `INSERT … SELECT` off `operations.intent`, which the unique index
`uq_operations_intent` guarantees is already one-to-one, so the primary key
cannot collide.

`operations.intent` is deliberately KEPT. Its unique index is what arbitrates
two concurrent posts of one nonce into a single operation (the insert races on
it before any intent row exists), and the column then reads as the creating
nonce — a different fact from "a nonce that reached this operation", and the one
the row itself should carry. The table is written in the same transaction as the
operation, so the two cannot drift.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b8e41d7c2a95"
down_revision = "a93c15d0e7b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_intents",
        sa.Column("intent", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_operation_intents_operation_id", "operation_intents", ["operation_id"]
    )
    op.execute(
        """
        insert into operation_intents (intent, operation_id, created_at)
        select intent, id, created_at from operations where intent is not null
        """
    )


def downgrade() -> None:
    # Nothing to restore: `operations.intent` was never dropped, so a downgrade
    # loses only the nonces that merely *resolved* to an operation — which is
    # precisely the state this migration found.
    op.drop_index("ix_operation_intents_operation_id", table_name="operation_intents")
    op.drop_table("operation_intents")
