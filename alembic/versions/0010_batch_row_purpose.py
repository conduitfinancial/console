"""purpose moves from the batch to the row — multi-purpose batches

Revision ID: f2a7c31e9b04
Revises: e1c7a4b93d52
Create Date: 2026-08-31 18:20:00.000000

The payout-flow restructure round. A batch used to be one route *including* a
purpose, so `payout_batches.purpose` was part of the batch's identity. It is now
a **column of the file**: one upload may carry a goods row and an intercompany
row, because that is what a payment run out of an accounting system looks like.
The batch keeps the corridor (rail + recipient type + destination country, which
every row of a file genuinely shares — they are what the template is keyed by);
the purpose moves down to the row that names it.

**Why no requirements snapshot is stored per purpose.** The obvious alternative
was a `JSONB` map of `{purpose: requirements}` on the batch. It is not here, and
deliberately: nothing in this feature is allowed to validate or dispatch against
a stored snapshot. Upload re-reads discovery for every purpose; dispatch re-reads
it for every purpose the batch actually holds (at most seven, usually one or
two) because the destination on a gated row comes from Conduit's own record at
send time. A stored snapshot would be a copy nothing is permitted to read — and
the one thing it *could* be read for, "what did we validate against", is already
answered by `fingerprint`, which now hashes the whole per-purpose set. So the
map would be a second source of truth for a question that already has one, kept
in step by nobody.

`purpose` on the row is **not** constrained to the seven keys: a row whose cell
held a purpose this route has none for is still a row, it carries its own error
saying so, and the report has to show what the file actually said.
"""

from alembic import op
import sqlalchemy as sa


revision = 'f2a7c31e9b04'
down_revision = 'e1c7a4b93d52'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'payout_batch_rows', sa.Column('purpose', sa.String(length=64), nullable=True)
    )
    # Every existing row's purpose is its batch's — which is exactly what a
    # single-purpose batch meant.
    op.execute(
        'UPDATE payout_batch_rows r SET purpose = b.purpose '
        'FROM payout_batches b WHERE b.id = r.batch_id'
    )
    op.alter_column('payout_batch_rows', 'purpose', nullable=False)
    op.drop_column('payout_batches', 'purpose')


def downgrade() -> None:
    op.add_column('payout_batches', sa.Column('purpose', sa.String(length=64), nullable=True))
    # A mixed batch cannot be put back into one column. The first row's purpose
    # is the honest choice — it is what a single-purpose reader would have to
    # believe — and a batch with no rows keeps the catch-all rather than a NULL
    # the old schema never allowed.
    op.execute(
        "UPDATE payout_batches b SET purpose = coalesce("
        "(SELECT r.purpose FROM payout_batch_rows r WHERE r.batch_id = b.id "
        "ORDER BY r.row_number LIMIT 1), 'other')"
    )
    op.alter_column('payout_batches', 'purpose', nullable=False)
    op.drop_column('payout_batch_rows', 'purpose')
