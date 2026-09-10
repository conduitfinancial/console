"""payout_batches + payout_batch_rows — batch payouts, uploaded and validated

Revision ID: d4f80b1c6a27
Revises: b8e4c07a2f13
Create Date: 2026-08-31 14:00:00.000000

An earlier round dispatches
these rows; nothing here talks to Conduit.

**Why the route tuple and the fingerprint are columns.** A batch only means
anything against the discovery response it was validated by: that response
decides which columns the file may carry, which fields are required, and which
gate (whitelist / documentation) applies. `fingerprint` is the sha256 of the
canonical requirements JSON the rows were validated against; `template_
fingerprint` is what the uploaded file *claimed* it was built from. A difference
is a warning, never a refusal — the rows are always revalidated against a fresh
discovery read, and the report says the template was stale.

**Why the funding account is here and not left to dispatch.** The operator
approves a total, and a total needs a currency. The `amount` column of a CSV
carries digits; `assetAmount.code` on the payout comes from the virtual account.
So the account is picked at upload, and `asset` is its code as Conduit stated
it.

**`payload` is encrypted** (`EncryptedJSON`, the `counterparties.recipient` /
`operations.request_body` posture): it is the assembled `destination` subtree —
account numbers, IBANs, legal names, two postal addresses. `amount` stays a
plain string column so the totals an operator approves can be summed without
decrypting the whole batch.

**A correction is a new batch.** There is no row-editing surface and no
in-place revalidation: an operator fixes the CSV and uploads it again, which
mints a new batch id, and the old one is abandoned. That keeps a batch a
permanent record of one file, which is what the dispatch ledger will need to
point at.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = 'd4f80b1c6a27'
down_revision = 'b8e4c07a2f13'
branch_labels = None
depends_on = None

# An earlier round can only reach validating / ready / abandoned. The two dispatch states
# are declared now so the constraint is not migrated twice for one feature.
STATUSES = "'validating', 'ready', 'partially_dispatched', 'dispatched', 'abandoned'"


def upgrade() -> None:
    op.create_table(
        'payout_batches',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('customer_id', sa.String(length=64), nullable=False),
        # The route this batch is for, exactly as `GET /v2/payouts/requirements`
        # was asked for it.
        sa.Column('purpose', sa.String(length=64), nullable=False),
        sa.Column('rail', sa.String(length=32), nullable=False),
        sa.Column('recipient_type', sa.String(length=32), nullable=False),
        sa.Column('destination_country', sa.String(length=8), nullable=False),
        sa.Column('virtual_account_id', sa.String(length=128), nullable=False),
        sa.Column('asset', sa.String(length=16), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('template_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column(
            'documents_required', sa.Boolean(), nullable=False, server_default=sa.text('false')
        ),
        sa.Column('document_ids', JSONB(), nullable=True),
        sa.Column(
            'status', sa.String(length=32), nullable=False, server_default=sa.text("'validating'")
        ),
        sa.Column('actor_id', sa.String(length=255), nullable=False),
        sa.Column('actor_email', sa.String(length=255), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f'status in ({STATUSES})', name='ck_payout_batches_status'),
    )
    op.create_index('ix_payout_batches_customer', 'payout_batches', ['customer_id'])
    op.create_index('ix_payout_batches_status', 'payout_batches', ['status'])

    op.create_table(
        'payout_batch_rows',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'batch_id',
            UUID(as_uuid=True),
            sa.ForeignKey('payout_batches.id', ondelete='CASCADE'),
            nullable=False,
        ),
        # 1-based over the file's data rows. Dispatch derives its intent nonce
        # from (batch id, row number), so this number is the row's identity for
        # the life of the batch and is never renumbered.
        sa.Column('row_number', sa.Integer(), nullable=False),
        # The assembled `destination` subtree, encrypted. Nullable: a row whose
        # cells could not be assembled into one still exists, carrying its
        # errors.
        sa.Column('payload', sa.LargeBinary(), nullable=True),
        # The operator's own digits, verbatim — money is a decimal string end to
        # end (`payments.amount`), and no float ever touches it.
        sa.Column('amount', sa.String(length=64), nullable=True),
        # A saved contact's uuid on a free-form route, a `wlr_…` registration on
        # a whitelist-gated one — two stores, one column, so no foreign key.
        sa.Column('contact_id', sa.String(length=128), nullable=True),
        # What it was called at upload: a later rename must not rewrite what this
        # row was addressed from (the `counterparty.used` rule).
        sa.Column('contact_label', sa.String(length=128), nullable=True),
        sa.Column('errors', JSONB(), nullable=True),
        sa.Column(
            'operation_id',
            UUID(as_uuid=True),
            sa.ForeignKey('operations.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint('batch_id', 'row_number', name='uq_payout_batch_rows_number'),
    )
    op.create_index('ix_payout_batch_rows_batch', 'payout_batch_rows', ['batch_id'])


def downgrade() -> None:
    op.drop_index('ix_payout_batch_rows_batch', table_name='payout_batch_rows')
    op.drop_table('payout_batch_rows')
    op.drop_index('ix_payout_batches_status', table_name='payout_batches')
    op.drop_index('ix_payout_batches_customer', table_name='payout_batches')
    op.drop_table('payout_batches')
