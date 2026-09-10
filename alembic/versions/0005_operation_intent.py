"""operations.intent — the one-use form nonce

Revision ID: c3a95f61d827
Revises: e91c5d3a7f04
Create Date: 2026-08-28 20:00:00.000000

OPERATIONS_SPEC §1, "Intent nonce". The active-state
hash guard releases the moment an operation confirms, so a browser mechanically
re-POSTing the same form after a lost redirect minted a *second* operation and
paid twice. Every mutation form render now carries a one-use `intent` uuid;
`start()` resolves by it first, in **every** state, so one rendered form maps to
one operation forever. Unique across all states — that is the whole point.
"""

from alembic import op
import sqlalchemy as sa

revision = 'c3a95f61d827'
down_revision = 'e91c5d3a7f04'
branch_labels = None
depends_on = None

INDEX = 'uq_operations_intent'


def upgrade() -> None:
    op.add_column('operations', sa.Column('intent', sa.UUID(), nullable=True))
    # Nullable + partial: operations opened by something that is not a rendered
    # form (the reconciler's own work, older rows) carry no intent, and NULLs
    # must not collide with each other.
    op.create_index(
        INDEX,
        'operations',
        ['intent'],
        unique=True,
        postgresql_where=sa.text('intent is not null'),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name='operations', postgresql_where=sa.text('intent is not null'))
    op.drop_column('operations', 'intent')
