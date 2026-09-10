"""merge retention/revocation and operation intents

Revision ID: 94fadcbc9c01
Revises: d7b402e9c1a5, b8e41d7c2a95
Create Date: 2026-09-02 17:23:38.859277
"""

from alembic import op
import sqlalchemy as sa


revision = '94fadcbc9c01'
down_revision = ('d7b402e9c1a5', 'b8e41d7c2a95')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
