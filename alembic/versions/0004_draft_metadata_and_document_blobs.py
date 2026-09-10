"""draft metadata columns; document_blobs

Revision ID: e91c5d3a7f04
Revises: b4f27a10c8e5
Create Date: 2026-08-28 09:00:00.000000

Drafts gain the two metadata fields that must survive the
post-submission payload purge (country, client_reference_id); `document_blobs`
carries the encrypted file behind a `document_upload` operation so the §3 recipe
can replay it (OPERATIONS_SPEC §3, closing the Phase-1 multipart deferral).
"""

from alembic import op
import sqlalchemy as sa

revision = 'e91c5d3a7f04'
down_revision = 'b4f27a10c8e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('drafts', sa.Column('country', sa.String(length=3), nullable=True))
    op.add_column(
        'drafts', sa.Column('client_reference_id', sa.String(length=128), nullable=True)
    )
    op.create_table(
        'document_blobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('operation_id', sa.UUID(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        # Fernet ciphertext, like operations.request_body and drafts.payload.
        sa.Column('data', sa.LargeBinary(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['operation_id'], ['operations.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('operation_id', name='uq_document_blobs_operation'),
    )


def downgrade() -> None:
    op.drop_table('document_blobs')
    op.drop_column('drafts', 'client_reference_id')
    op.drop_column('drafts', 'country')
