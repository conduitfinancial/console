"""operations.reference — the operator's console-local note

Revision ID: a7d21f4b6e90
Revises: c3a95f61d827
Create Date: 2026-08-30 09:00:00.000000

It was established that Conduit's own
`clientReferenceId` cannot carry an operator's note: it is ledger-owned
(`app/conduit/execute.py` injects `op.id` at call time, and the reconciler's
§3 recipes and the webhook→operation resolution both match on it), and Conduit
demands org-uniqueness with no spaces. So the note lives here, in this console,
and is never sent anywhere.

Additive only: one nullable column, no backfill, no index. No index because
nothing queries by it — the reference is *displayed* wherever the operation
renders, and there is no operations index page to filter on yet.

**It is not in `request_body`, so it is not in `request_hash`** — which is the
whole point. OPERATIONS_SPEC §1's double-submit guard is byte-for-byte
unchanged by this migration: an identical body resubmitted under a different
note while the first operation is still active resolves to that same operation,
because it is the same payment. A note is a label on an attempt, not a reason
to make a second one.
"""

from alembic import op
import sqlalchemy as sa

revision = 'a7d21f4b6e90'
down_revision = 'c3a95f61d827'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 128 chars: long enough for an invoice number plus the sentence around it,
    # short enough that a table cell stays a table cell — and the same width as
    # `drafts.client_reference_id`, so the wizard's capture point needs no
    # second bound. Free text, spaces included: it answers to this console, not
    # to Conduit's validators.
    op.add_column('operations', sa.Column('reference', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('operations', 'reference')
