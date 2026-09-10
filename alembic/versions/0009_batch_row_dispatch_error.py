"""batch dispatch columns: rows.dispatch_error + batches.accepted_document_types

Revision ID: e1c7a4b93d52
Revises: d4f80b1c6a27
Create Date: 2026-08-31 15:10:00.000000

One column, because dispatch has exactly one
outcome the operations ledger cannot express: a row this console **declined to
send**.

Every other outcome of a dispatched row is the operation's own — `operation_id`
already points at it, and `state` there says confirmed / rejected /
outcome_unknown. But a row whose contact was archived between "ready" and
"dispatch" never reaches `operations.start`: there is no body to send, so there
is no operation to carry the reason. That sentence lives here.

It is deliberately **not** merged into `errors`: that column is what validation
said about the uploaded file, the totals are counted off it, and a batch's
report must keep saying what it said when the operator approved it. A refusal
recorded here is also retryable — nothing was sent, so dispatching again
re-resolves the contact — which is the other reason it is not a validation
error.

The second column is the design gate's: `documentation.acceptedDocumentTypes`
as the snapshot answered it **at upload**, so the batch's own upload widget can
state what Conduit accepts in the same words the downloaded template's header
block used. Stored rather than re-read for the same reason `documents_required`
is: the report and the confirm screen make no Conduit call, and a later read
could answer differently from the one this batch was validated against.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'e1c7a4b93d52'
down_revision = 'd4f80b1c6a27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'payout_batch_rows',
        sa.Column('dispatch_error', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'payout_batches',
        sa.Column('accepted_document_types', JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('payout_batches', 'accepted_document_types')
    op.drop_column('payout_batch_rows', 'dispatch_error')
