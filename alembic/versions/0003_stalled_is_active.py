"""stalled joins the double-submit guard; webhook claim leases

Revision ID: b4f27a10c8e5
Revises: 7c1e0a4b93d2
Create Date: 2026-08-28 01:00:00.000000

Two fixes. A stalled operation is unresolved and retryable, so an identical
resubmit must resolve to it instead of minting a second idempotency key. And a
crash between claiming and processing stranded the claimed batch in `processing`
forever.
"""

from alembic import op
import sqlalchemy as sa

revision = 'b4f27a10c8e5'
down_revision = '7c1e0a4b93d2'
branch_labels = None
depends_on = None

INDEX = 'uq_operations_active_request'
OLD_PREDICATE = "state in ('created', 'in_flight', 'outcome_unknown')"
NEW_PREDICATE = "state in ('created', 'in_flight', 'outcome_unknown', 'stalled')"


def upgrade() -> None:
    op.drop_index(INDEX, table_name='operations', postgresql_where=sa.text(OLD_PREDICATE))
    op.create_index(
        INDEX,
        'operations',
        ['type', 'request_hash'],
        unique=True,
        postgresql_where=sa.text(NEW_PREDICATE),
    )
    op.add_column(
        'webhook_events', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('webhook_events', 'claimed_at')
    # Widening to narrowing: an existing stalled duplicate would break the old
    # index, so release stalled rows from the guard before rebuilding it.
    op.drop_index(INDEX, table_name='operations', postgresql_where=sa.text(NEW_PREDICATE))
    op.create_index(
        INDEX,
        'operations',
        ['type', 'request_hash'],
        unique=True,
        postgresql_where=sa.text(OLD_PREDICATE),
    )
