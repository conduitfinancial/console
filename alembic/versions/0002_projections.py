"""projections + webhook inbox claim statuses

Revision ID: 7c1e0a4b93d2
Revises: 209f35325d09
Create Date: 2026-08-27 23:59:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '7c1e0a4b93d2'
down_revision = '209f35325d09'
branch_labels = None
depends_on = None

OLD_STATUSES = "status in ('pending', 'processed', 'failed')"
NEW_STATUSES = (
    "status in ('pending', 'processing', 'processed', 'processed_ignored', 'failed')"
)


def upgrade() -> None:
    op.create_table(
        'projections',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('resource_kind', sa.String(length=64), nullable=False),
        sa.Column('resource_id', sa.String(length=128), nullable=False),
        sa.Column('state', sa.String(length=64), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('resource_kind', 'resource_id', name='uq_projections_resource'),
    )
    op.drop_constraint('ck_webhook_events_status', 'webhook_events', type_='check')
    op.create_check_constraint('ck_webhook_events_status', 'webhook_events', NEW_STATUSES)


def downgrade() -> None:
    op.execute("update webhook_events set status = 'pending' where status = 'processing'")
    op.execute("update webhook_events set status = 'processed' where status = 'processed_ignored'")
    op.drop_constraint('ck_webhook_events_status', 'webhook_events', type_='check')
    op.create_check_constraint('ck_webhook_events_status', 'webhook_events', OLD_STATUSES)
    op.drop_table('projections')
