"""Tables per OPERATIONS_SPEC §1/§4 and plan v2 §3.

All four tables in one module — they are cross-module shared state and
splitting them per feature package buys nothing but imports.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.crypto import EncryptedBytes, EncryptedJSON
from app.db import Base

# OPERATIONS_SPEC §3
OPERATION_TYPES = (
    "onboarding_submit",
    "customer_update",
    "feature_request",
    "document_upload",
    "rfi_respond",
    "whitelist_create",
    "whitelist_revoke",
    "payout_create",
    "payout_cancel",
    "order_create",
    "order_execute",
    "order_cancel",
    "webhook_endpoint_register",
)

# OPERATIONS_SPEC §2
OPERATION_STATES = (
    "created",
    "in_flight",
    "confirmed",
    "rejected",
    "outcome_unknown",
    "abandoned",
    "stalled",
)
# Active = unresolved, and therefore covered by the §1 double-submit guard.
# `stalled` is active: it is retryable and the original call may well have
# landed, so an identical resubmit must resolve to it rather than mint a second
# idempotency key. It leaves the guard only by resolving or by an explicit,
# audited admin abandonment.
ACTIVE_STATES = ("created", "in_flight", "outcome_unknown", "stalled")
# Truly finished: nothing more will be sent, so the request body can be purged
# and the guard is released.
TERMINAL_STATES = ("confirmed", "rejected", "abandoned")

ACTIVE_STATE_SQL = "state in ('created', 'in_flight', 'outcome_unknown', 'stalled')"

# Webhook inbox lifecycle (OPERATIONS_SPEC §4). `processing` is the worker's
# claim marker; `processed_ignored` is a well-formed event this app subscribes
# to nothing for; `failed` is a poison event past its attempt budget.
WEBHOOK_STATUSES = ("pending", "processing", "processed", "processed_ignored", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in (" + ", ".join(f"'{v}'" for v in values) + ")"


class Draft(Base):
    """Onboarding/form draft + the requirements snapshot it was rendered from.

    `payload` (the operator's raw answers) is the sensitive half and is purged on
    successful submission; everything else is metadata the application detail
    view still needs afterwards, so it outlives the purge.
    """

    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(255))
    customer_id: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(3))
    # The operator's own reference for this application. Stable across a
    # rejection→correction→resubmit cycle (each resubmit is a new operation with
    # a new idempotency key; this is what says they are the same attempt).
    client_reference_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict | None] = mapped_column(EncryptedJSON)
    # Pinned at creation. A draft is always re-rendered and validated against
    # this copy, never against a fresh discovery fetch (plan v2 §3).
    requirements_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Operation(Base):
    """One row per logical mutation attempt (OPERATIONS_SPEC §1)."""

    __tablename__ = "operations"

    # id doubles as clientReferenceId — the durable matcher for reconciliation.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(255))
    actor_email: Mapped[str] = mapped_column(String(255))
    customer_id: Mapped[str | None] = mapped_column(String(64))
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"))
    request_path: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    request_body: Mapped[dict | None] = mapped_column(EncryptedJSON)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    # The one-use nonce minted by the form render that produced this operation
    # (OPERATIONS_SPEC §1). Unique across *all* states, unlike the hash guard —
    # one rendered form is one operation forever, even after it settles.
    intent: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The operator's own note for this operation. Console-local
    # and free text: it is NEVER sent to Conduit, and it is deliberately NOT part
    # of `request_body` — therefore not part of `request_hash`, so the §1
    # double-submit guard behaves exactly as it did before this column existed.
    # Resubmitting an identical body under a different note while the first
    # operation is active still resolves to that operation: it is the same
    # payment, and a note is not an intent to pay twice. (Conduit's own
    # `clientReferenceId` is a different thing entirely — it is `op.id`, injected
    # at call time by `app.conduit.execute.outbound_body`.)
    reference: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="created", index=True)
    conduit_resource_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[dict | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reconcile_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    in_flight_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("type", OPERATION_TYPES), name="ck_operations_type"),
        CheckConstraint(_in("state", OPERATION_STATES), name="ck_operations_state"),
        # The double-submit guard (OPERATIONS_SPEC §1).
        Index(
            "uq_operations_active_request",
            "type",
            "request_hash",
            unique=True,
            postgresql_where=text(ACTIVE_STATE_SQL),
        ),
        # The intent nonce, unique in every state — the guard the hash one
        # cannot be: it has to keep matching after the operation confirms.
        Index(
            "uq_operations_intent",
            "intent",
            unique=True,
            postgresql_where=text("intent is not null"),
        ),
    )


class OperationIntent(Base):
    """Every nonce that has ever resolved to an operation (OPERATIONS_SPEC §1).

    `operations.intent` records the nonce that *created* the row, and its unique
    index is what makes two concurrent posts of one nonce a single operation.
    That column could never record the OTHER nonces: a submit whose nonce missed
    and which the active-state request-hash guard then resolved to an existing
    operation left its own nonce written down nowhere. Once that operation went
    terminal the hash guard released, and a mechanical re-POST of that same form
    — a lost redirect, a connection retry — carried a nonce the console had no
    memory of, missed both guards, and minted a second payment with a second
    idempotency key. The nonce existed precisely to stop that.

    So the mapping is its own table: one row per nonce, pointing at the operation
    that nonce reached, whether it created it or merely resolved to it. `intent`
    is the primary key, which keeps the "one nonce, one operation, forever"
    promise as a database constraint rather than a convention, and `by_intent`
    reads this table alone. Written in the same transaction as the operation it
    names, so the two can never disagree.
    """

    __tablename__ = "operation_intents"

    intent: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentBlob(Base):
    """The bytes behind a `document_upload` operation (OPERATIONS_SPEC §3).

    `POST /v2/documents` is multipart, so the file cannot live in the operation's
    JSON `request_body` — but the reconciler still has to replay it byte for
    byte with the original idempotency key. One blob per operation; the
    operation's `request_body` carries the metadata (`fileSha256`, `purpose`,
    `name`) that `request_hash` is computed over.

    `data` is purged on the same schedule and the same rules as `request_body`:
    terminal operations only, never while `stalled`.
    """

    __tablename__ = "document_blobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), unique=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    # Sniffed from the file's magic bytes at intake — never the browser's header.
    content_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[bytes | None] = mapped_column(EncryptedBytes)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    """Inbox (OPERATIONS_SPEC §4). Raw body kept for replay/debugging.

    A delivery carries the resource in full — a withdrawal's `destination.
    recipient` is the payee's account number, legal name and postal address — so
    `raw_body` is `EncryptedBytes`, the `document_blobs.data` posture. The column
    is read only by primary key in the worker and is never filtered or queried by
    content, so encryption costs nothing here, and keeping the exact signed bytes
    (rather than a redacted copy) is what makes a replay a replay.
    """

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Conduit event id, or sha256 of the raw body when the payload has none.
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    event_type: Mapped[str | None] = mapped_column(String(128))
    # The delivery byte-for-byte — the signature was computed over exactly these
    # bytes, and a reserialization would not verify (`app/webhooks/signature.py`).
    # Nullable because it is purged: `worker.purge_raw_bodies` empties it once
    # the event is processed and past WEBHOOK_RAW_RETENTION. NULL is the purged
    # marker (the `document_blobs.data` posture); every other column survives,
    # so the inbox's own record of what arrived and what became of it is intact.
    raw_body: Mapped[bytes | None] = mapped_column(EncryptedBytes)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the worker took this row. A claim older than the lease is reclaimed:
    # the worker that took it died before recording an outcome.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", WEBHOOK_STATUSES), name="ck_webhook_events_status"),
    )


class Projection(Base):
    """Read projection of a Conduit resource (plan v2 §3).

    Written only by `app.projections.apply_observation` — webhooks and the
    reconciler both funnel through it, which is what makes duplicate and
    out-of-order deliveries no-ops (OPERATIONS_SPEC §4).
    """

    __tablename__ = "projections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_kind: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    # Stored verbatim, including values this app has never heard of (plan v2 §7).
    state: Mapped[str | None] = mapped_column(String(64))
    # Verbatim *except* for bank coordinates and postal addresses, which
    # `projections.scrub` strips at the write boundary. The verbatim rule is
    # there so an unknown **state** is never lost or guessed at — `state` above
    # still obeys it exactly, and so does every unknown field that is not a
    # coordinate — not so that payee bank details sit forever in the one column
    # that cannot be encrypted (four readers pointer into it in SQL) and has no
    # retention job. `app/projections.py` carries the enumeration and the reason.
    payload: Mapped[dict | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("resource_kind", "resource_id", name="uq_projections_resource"),
    )


class Counterparty(Base):
    """A destination this console remembers for one customer.

    **Console-local by necessity, not by preference.** Conduit's only
    server-side recipient store is `whitelist-recipients`, and it exists for the
    `intercompany` gate (migration 0007 carries the spec evidence). A free-form
    payout route — goods/services, payroll, treasury, investments, other — has
    nowhere on Conduit to save a destination, and `FiatPayoutDto.destination.
    recipient` is an inline object with no by-id form. So the address book is
    here, and a row of it is never sent to Conduit as a record: its fields are
    pasted into the payout body at use (OPERATIONS_SPEC §1).

    `recipient` is the `destination.recipient` subtree of a payout that was
    accepted — bank coordinates and postal addresses, so `EncryptedJSON`, the
    same posture as `drafts.payload` and `operations.request_body`.
    """

    __tablename__ = "counterparties"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Indexed, and every query filters on it: counterparties are per-customer and
    # are never shared across customers.
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128))
    recipient: Mapped[dict | None] = mapped_column(EncryptedJSON)
    # `payments.RAILS_FOR`'s key (us / sepa / swift), not a payout rail — one
    # us-family destination is payable over fedwire, ach, rtp and fednow.
    rail_family: Mapped[str] = mapped_column(String(16))
    recipient_type: Mapped[str] = mapped_column(String(32))
    destination_country: Mapped[str] = mapped_column(String(8))
    created_by_actor_id: Mapped[str] = mapped_column(String(255))
    created_by_actor_email: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Soft delete: gone from every picker and list, still on file behind the
    # payouts that named it.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Only `validating`, `ready` and `abandoned` are reachable in
# this slice — dispatch owns the other two, and they are declared here
# so the constraint does not have to be migrated twice for one feature.
BATCH_STATUSES = ("validating", "ready", "partially_dispatched", "dispatched", "abandoned")


class PayoutBatch(Base):
    """One uploaded batch-payout CSV, pinned to the route it was validated for.

    **The corridor is on the row, not in a query string**, because everything
    the batch means depends on it: which columns the file was allowed to have,
    which discovery snapshots validated it, and — at dispatch — what body each
    row becomes. `fingerprint` is the sha256 of the canonical JSON of *every
    purpose's* requirements this batch was validated against (the purpose is a
    column now); `template_fingerprint` is what the *file* said
    it was built from. They differ when discovery moved under a template an
    operator saved last week, which is a warning and not a refusal: the rows are
    revalidated against the live snapshot either way, and the report says so.

    `virtual_account_id` + `asset` are chosen at upload rather than at dispatch
    because the totals an operator approves are in a currency, and the funding
    account is the only thing that states one — a CSV amount column carries
    digits, never a currency.

    Nothing here is sent to Conduit. A batch is console-local bookkeeping until
    Dispatch turns each row into a `payout_create` operation.
    """

    __tablename__ = "payout_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    # No `purpose`: it is a column of the file and lives on the row that names it
    # (migration f2a7c31e9b04). What every row of one file genuinely shares is
    # the corridor below — which is what the template is keyed by.
    rail: Mapped[str] = mapped_column(String(32))
    recipient_type: Mapped[str] = mapped_column(String(32))
    destination_country: Mapped[str] = mapped_column(String(8))
    virtual_account_id: Mapped[str] = mapped_column(String(128))
    asset: Mapped[str] = mapped_column(String(16))
    fingerprint: Mapped[str] = mapped_column(String(64))
    template_fingerprint: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    # `documentation.required` as the live snapshot answered it at upload, so the
    # mark-ready gate does not have to re-ask discovery a question it already got
    # an answer to (and cannot be told a different answer by a later read).
    documents_required: Mapped[bool] = mapped_column(Boolean, default=False)
    # `documentation.acceptedDocumentTypes` as that same snapshot listed them, so
    # the batch's own upload widget says what Conduit accepts in the words the
    # downloaded template's header block used — with no Conduit read on a page
    # that deliberately makes none.
    accepted_document_types: Mapped[list | None] = mapped_column(JSONB)
    # Batch-level shared documents (`transaction_support`), resolved through
    # `documents.attachable` exactly as the single-payout form resolves its own.
    document_ids: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="validating", index=True)
    actor_id: Mapped[str] = mapped_column(String(255))
    actor_email: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("status", BATCH_STATUSES), name="ck_payout_batches_status"),
    )


class PayoutBatchRow(Base):
    """One line of an uploaded batch, validated.

    `payload` is the assembled `destination` subtree — bank coordinates and two
    postal addresses — so it gets `EncryptedJSON`, the same posture as
    `counterparties.recipient` and `operations.request_body`. `amount` stays a
    plain decimal **string** in its own column: it is not identifying on its own,
    and the batch totals must be summable without decrypting every row.

    `contact_id` is a string rather than a foreign key on purpose: a free-form
    row names one of this console's saved contacts (a uuid), a whitelist-gated
    row names Conduit's own registered recipient (`wlr_…`). `contact_label` is
    what it was called *at upload*, which a rename must not rewrite — the same
    rule the `counterparty.used` audit row follows.

    `operation_id` is dispatch's: it mints one `payout_create` per row.
    """

    __tablename__ = "payout_batch_rows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payout_batches.id", ondelete="CASCADE"), index=True
    )
    # 1-based over the file's data rows — the number dispatch derives its intent
    # from, so it is stable for the life of the batch.
    row_number: Mapped[int] = mapped_column(Integer)
    # The row's own purpose, verbatim from its cell — deliberately unconstrained:
    # a row naming a purpose this route has none for is still a row, it carries
    # the error that says so, and the report must show what the file said.
    purpose: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(EncryptedJSON)
    amount: Mapped[str | None] = mapped_column(String(64))
    contact_id: Mapped[str | None] = mapped_column(String(128))
    contact_label: Mapped[str | None] = mapped_column(String(128))
    # `[]` is a valid row; a list of `{column, label, detail}` is not.
    errors: Mapped[list | None] = mapped_column(JSONB)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id", ondelete="SET NULL")
    )
    # The one dispatch outcome the ledger cannot hold: a row this console
    # declined to send (its contact was archived, or Conduit no longer offers
    # the registered recipient it was addressed to), so no operation exists to
    # carry the reason. Separate from `errors` because that column is what
    # validation said about the uploaded file — the totals are counted off it —
    # and because nothing was sent, which makes this refusal retryable.
    dispatch_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_payout_batch_rows_number"),
    )


class SessionRevocation(Base):
    """One operator whose issued sessions have been cut off.

    A session cookie is signed and self-contained, so signing out an operator who
    still holds one used to mean rotating `SESSION_SECRET` — which signs out
    everybody. This is the per-operator version: `not_before` is the instant the
    cut-off was made, and `app.auth.revocation.revoked` refuses any cookie issued
    before it. Cookies issued after (a fresh sign-in) are honoured, so revoking
    is "end the sessions that exist", not "ban the account" — role removal is
    what bans an account.

    `sub` is `Actor.id`. No `revoked_by` / `reason` columns: the operator running
    `scripts/revoke_sessions.py` has database access, and the audit trail this
    console keeps is of actions taken *in* the console.

    No UI. The script is the whole feature; a permission plus a button
    on an operator page is the upgrade path, when someone asks for one.
    """

    __tablename__ = "session_revocations"

    sub: Mapped[str] = mapped_column(Text, primary_key=True)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only actor-attributed trail (plan v2 §3)."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(255))
    actor_email: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id", ondelete="SET NULL"), index=True
    )
    detail: Mapped[dict | None] = mapped_column(JSONB)
