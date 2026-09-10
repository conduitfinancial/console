"""PII at rest: encrypt webhook_events.raw_body, minimize projections.payload

Revision ID: a93c15d0e7b6
Revises: f2a7c31e9b04
Create Date: 2026-09-01 10:00:00.000000

Two columns held full payee bank coordinates and postal addresses in the clear,
while every sibling store of the same data subtree in this codebase is encrypted
(`counterparties.recipient`, `operations.request_body`, `drafts.payload`,
`payout_batch_rows.payload` are `EncryptedJSON`; `document_blobs.data` is
`EncryptedBytes`). A withdrawal delivery carries `destination.recipient` whole —
account number, routing number, legal name, and the payee's postal address — and
both ingress paths persisted it verbatim, forever: unlike `request_body`, which
`operations.purge_request_bodies` ages out, neither column has a retention job.

**This migration converts the rows that are already there.** Stopping the defect
for new writes only would leave every existing dump, replica and backup exposed,
which is where the whole of the risk sits.

* `webhook_events.raw_body` → `EncryptedBytes`. The column is read only by
  primary key in the worker, never filtered and never queried by content, so
  encryption costs nothing. It is converted through a second column and a rename
  rather than `ALTER … USING`: no SQL cast produces ciphertext, so the bytes go
  out to Python and back.

* `projections.payload` is **minimized, not encrypted** — four code paths read it
  through SQL JSONB pointers (`app/web/accounts.py`, `app/web/dashboard.py`,
  `app/onboarding/drafts.py`, `app/reconciliation/service.py`), and encrypting it
  would break all four. The backfill calls `app.projections.scrub`, imported
  rather than copied (the 0001 precedent for reaching into `app.crypto`): the
  enumeration of what counts as a coordinate must not drift between the write
  boundary and this sweep.

Both loops are keyset-batched, so neither reads a whole production table into
memory, and both skip what they cannot handle: a NULL payload and a payload that
is not a JSON object are left exactly as they are.

**The downgrade is deliberately lossy.** It puts the column types back and
decrypts the bodies, but the subtrees scrubbed out of `projections.payload` are
gone — a projection is a cache of Conduit's own truth, and the stale sweep
re-reads any row that still matters.
"""

import json
import uuid

from alembic import op
import sqlalchemy as sa

from app.crypto import fernet
from app.projections import scrub


revision = 'a93c15d0e7b6'
down_revision = 'f2a7c31e9b04'
branch_labels = None
depends_on = None

BATCH = 500
# Sorts before every real uuid, so the first keyset page starts at the beginning.
START = uuid.UUID(int=0)


def _scrub_payloads(connection) -> None:
    """Strip the coordinate and address subtrees out of every stored payload.

    `jsonb_typeof(payload) = 'object'` is the filter because `scrub` is about
    keys: a NULL payload and an array-shaped one have none, and are passed over
    rather than rewritten or failed on.
    """
    last = START
    while True:
        rows = connection.execute(
            sa.text(
                "select id, payload from projections "
                "where id > :last and jsonb_typeof(payload) = 'object' "
                "order by id limit :limit"
            ),
            {"last": last, "limit": BATCH},
        ).all()
        if not rows:
            return
        for row_id, payload in rows:
            last = row_id
            minimized = scrub(payload)
            if minimized != payload:
                connection.execute(
                    sa.text(
                        "update projections set payload = cast(:payload as jsonb) "
                        "where id = :id"
                    ),
                    {"id": row_id, "payload": json.dumps(minimized)},
                )


def _convert_bodies(connection, source: str, target: str, convert) -> None:
    """Move every `raw_body` through `convert` into the column that replaces it.

    Keyset by id over the rows not yet converted — `target is null` is the
    progress marker, so an interrupted run resumes instead of starting over.
    """
    while True:
        rows = connection.execute(
            sa.text(
                f"select id, {source} from webhook_events "
                f"where {target} is null order by id limit :limit"
            ),
            {"limit": BATCH},
        ).all()
        if not rows:
            return
        for row_id, body in rows:
            connection.execute(
                sa.text(f"update webhook_events set {target} = :body where id = :id"),
                {"id": row_id, "body": convert(body)},
            )


def upgrade() -> None:
    connection = op.get_bind()
    _scrub_payloads(connection)

    op.add_column('webhook_events', sa.Column('raw_body_enc', sa.LargeBinary(), nullable=True))
    box = fernet()
    _convert_bodies(
        connection, 'raw_body', 'raw_body_enc', lambda body: box.encrypt(body.encode())
    )
    op.drop_column('webhook_events', 'raw_body')
    op.alter_column('webhook_events', 'raw_body_enc', new_column_name='raw_body')
    op.alter_column('webhook_events', 'raw_body', nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    op.add_column('webhook_events', sa.Column('raw_body_txt', sa.Text(), nullable=True))
    box = fernet()
    _convert_bodies(
        connection,
        'raw_body',
        'raw_body_txt',
        lambda body: box.decrypt(bytes(body)).decode('utf-8', 'replace'),
    )
    op.drop_column('webhook_events', 'raw_body')
    op.alter_column('webhook_events', 'raw_body_txt', new_column_name='raw_body')
    op.alter_column('webhook_events', 'raw_body', nullable=False)
