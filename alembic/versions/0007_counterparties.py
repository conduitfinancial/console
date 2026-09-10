"""counterparties — the console's own address book for free-form payout routes

Revision ID: b8e4c07a2f13
Revises: a7d21f4b6e90
Create Date: 2026-08-30 12:00:00.000000

**Why this table exists at all** — decided from the API, not assumed. Conduit has
exactly one server-side recipient store, and it is not an address book:

* Pinned spec (`contracts/openapi_production.json`, 67 paths): the only
  recipient-shaped paths are `/customers/{customerId}/whitelist-recipients`
  (`get`,`post`) and `…/{id}` (`get`,`delete`), tagged **Whitelist Recipients**.
  No counterparty/beneficiary/payee/contact/address-book path or schema exists.
* The live sandbox spec (`api.sandbox.conduit.financial/v2/api-docs/openapi.json`,
  92 paths, fetched read-only 2026-08-30) adds nothing but `/sandbox/*`
  simulators — all 25 live-only paths are simulators, and its tag list is the
  pinned one plus `Sandbox`.
* `POST /customers/{id}/whitelist-recipients` describes itself as the
  **intercompany gate**: "Only registered entries satisfy purpose=intercompany
  payouts."
* `FiatPayoutDto` has no recipient-by-id at all: its `destination.recipient` is
  the full inline object, every time. There is nothing to reference.

So a whitelist-gated route already has "saved counterparties" — they are the
registered entries, and the payout form picks from them. A
free-form route (goods/services, payroll, treasury, investments, other) has
**no** server-side save, and its bank coordinates are retyped on every payment.
That is what this table holds, and it holds it here: a counterparty is never
sent to Conduit as a record; its fields are pasted into `FiatPayoutDto` at use
(OPERATIONS_SPEC §1).

**`recipient` is encrypted** (`EncryptedJSON`, the `drafts.payload` /
`operations.request_body` posture): it is the `destination.recipient` subtree —
account numbers, IBANs, a legal name and two postal addresses. PII at rest.

**Per customer, never shared.** `customer_id` is indexed and every query filters
on it; a counterparty saved under customer A is not offered to, listed for, or
reachable from customer B. Conduit's own whitelist is customer-scoped for the
same reason.

**Soft delete.** `archived_at` rather than a DELETE: an archived counterparty
drops out of every picker and list, but a payout sent to it last quarter still
has a row explaining where the money went.

**One label per customer, among the living.** The partial unique index makes
"save under a label you already used" an update rather than a second row — see
`app/counterparties.py` for why that semantics was chosen over always-insert.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = 'b8e4c07a2f13'
down_revision = 'a7d21f4b6e90'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'counterparties',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('customer_id', sa.String(length=64), nullable=False),
        # The operator's own name for this destination. Same 128-char bound as
        # `operations.reference` — a label, not a document.
        sa.Column('label', sa.String(length=128), nullable=False),
        # `destination.recipient` verbatim, encrypted. NOT NULL, so a row whose
        # payload reads back as nothing is *unreadable* rather than empty — the
        # list says so instead of showing a blank cell.
        sa.Column('recipient', sa.LargeBinary(), nullable=False),
        # The three facts that decide whether this destination can be reached by
        # a given route. `rail_family` is `payments.RAILS_FOR`'s key (us / sepa /
        # swift), not a payout rail: an ABA-addressed account is payable over
        # four of them and the picker must offer it on all four.
        sa.Column('rail_family', sa.String(length=16), nullable=False),
        sa.Column('recipient_type', sa.String(length=32), nullable=False),
        sa.Column('destination_country', sa.String(length=8), nullable=False),
        sa.Column('created_by_actor_id', sa.String(length=255), nullable=False),
        sa.Column('created_by_actor_email', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Every read is "this customer's live counterparties", so that is the index.
    op.create_index(
        'ix_counterparties_customer',
        'counterparties',
        ['customer_id'],
        postgresql_where=sa.text('archived_at is null'),
    )
    # Case-insensitive: "Globex" and "globex" are one name in an address book,
    # and two rows an operator cannot tell apart in a picker.
    op.create_index(
        'uq_counterparties_live_label',
        'counterparties',
        ['customer_id', sa.text('lower(label)')],
        unique=True,
        postgresql_where=sa.text('archived_at is null'),
    )


def downgrade() -> None:
    op.drop_index('uq_counterparties_live_label', table_name='counterparties')
    op.drop_index('ix_counterparties_customer', table_name='counterparties')
    op.drop_table('counterparties')
