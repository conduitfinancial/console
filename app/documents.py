"""Document upload: encrypted blob at rest, real multipart on the wire.

Closes the Phase-1 deferral recorded in OPERATIONS_SPEC §3. `POST /v2/documents`
is `multipart/form-data`, which the JSON executor could not send and
`EncryptedJSON` could not store — so the operation was refused outright. Now:

* **Intake** validates at the trust boundary — ≤10 MB, and PDF/JPEG/PNG decided
  by the file's *magic bytes*, exactly as Conduit's own spec says it decides
  (the filename and the browser's `Content-Type` are both caller-supplied).
* **The bytes** live in `document_blobs`, Fernet-encrypted with the same key as
  every other customer payload. The operation's `request_body` holds only
  `{fileSha256, purpose, name, scope}` — which makes `request_hash` (sha256 over
  path + canonical JSON of those four) byte-stable, and makes the §1
  double-submit guard work for uploads for free: the same file, for the same
  purpose and the same subject, while an upload is active resolves to the same
  operation and the same idempotency key. `scope` is what keeps that from
  over-matching across customers.
* **Replay** rebuilds the identical multipart body from the blob, so the §3
  recipe ("no reference field: replay with the same key") is honest.

Blob bytes are purged on the same schedule and the same rules as
`request_body`: terminal operations only, never while `stalled`.

Cross-module by design (onboarding, RFI responses and payout documentation all
upload), which is why this is a top-level module rather than living under
`app/onboarding/`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import operations
from app.config import get_settings
from app.models import TERMINAL_STATES, DocumentBlob, Operation

PATH = "/v2/documents"
MAX_BYTES = 10 * 1024 * 1024

# Header + end-marker checks, not a dependency. Three formats is the
# whole of what Conduit accepts; `filetype`/`python-magic` would be a package to
# install, pin and scan for a dozen bytes of comparison. Ceiling: this proves the
# file starts and ends like the format it claims, not that its interior parses —
# Conduit re-validates, and a real parser is the upgrade path if that ever stops
# being enough.
MAGIC: tuple[tuple[bytes, str, Callable[[bytes], bool]], ...] = (
    # A PDF ends with %%EOF (possibly followed by whitespace).
    (b"%PDF-", "application/pdf", lambda data: b"%%EOF" in data[-1024:]),
    # JPEG: SOI at the front, EOI at the back.
    (b"\xff\xd8\xff", "image/jpeg", lambda data: b"\xff\xd9" in data[-32:]),
    # PNG: the signature is followed by the IHDR chunk's length + type.
    (b"\x89PNG\r\n\x1a\n", "image/png", lambda data: data[12:16] == b"IHDR"),
)

# DocumentUploadDto's `purpose` enum, from the pinned spec.
PURPOSES = (
    "organization_onboarding",
    "kyc",
    "feature_request",
    "transaction_support",
    "rfi_response",
)


class Rejected(ValueError):
    """The file cannot be uploaded. Raised before an operation row is *created*,
    so a refused upload leaves nothing behind.

    One of them is raised after `operations.start` has run — the replayed-nonce
    refusal below — and the claim still holds: that branch is
    reached only when `start` *resolved* to an operation some earlier upload
    created, so there is no new row, no new blob and nothing to roll back.
    """


class BlobUnavailable(Exception):
    """The bytes behind an upload operation are gone (purged, or never stored).

    Never send an empty file in their place: an empty `file` part with the
    original idempotency key would either be rejected as a corrupt document or,
    worse, accepted as one.
    """


def sniff(data: bytes) -> str:
    """The content type the file's own bytes claim. Raises `Rejected`.

    A leading signature alone is a weak claim: a truncated upload, or arbitrary
    bytes with `%PDF-` glued on the front, passes it. Each format therefore also
    has to look structurally whole — the check is a gate, not a guarantee, and
    Conduit re-validates server-side either way.
    """
    if not data:
        raise Rejected("the file is empty")
    if len(data) > MAX_BYTES:
        raise Rejected(f"the file is {len(data)} bytes; the limit is {MAX_BYTES}")
    for signature, content_type, whole in MAGIC:
        if data.startswith(signature):
            if not whole(data):
                raise Rejected(f"the {content_type} file looks truncated or corrupt")
            return content_type
    raise Rejected("only PDF, JPEG and PNG files are accepted (checked by magic bytes)")


async def intake(
    session: AsyncSession,
    *,
    data: bytes,
    filename: str,
    purpose: str,
    actor_id: str,
    actor_email: str,
    name: str | None = None,
    customer_id: str | None = None,
    draft_id: uuid.UUID | None = None,
    intent: uuid.UUID | None = None,
) -> tuple[Operation, bool]:
    """Validate, store the encrypted blob, and open the upload operation.

    Returns `(operation, is_new)` with the same meaning as `operations.start`:
    `is_new=False` means this exact file, for this purpose and name **and the
    same subject**, is already being uploaded — the caller shows that operation
    instead of sending a second one. So a returned operation always describes
    the bytes just handed in; an `is_new=False` resolution that describes some
    *other* file is `Rejected` rather than returned (below), because the
    caller's only move on a resolution is to name its `doc_` id.
    """
    if purpose not in PURPOSES:
        raise Rejected(f"unknown document purpose {purpose!r}")
    content_type = sniff(data)
    digest = hashlib.sha256(data).hexdigest()

    # The metadata IS the hashed body (§3): path + file sha256 + purpose + name
    # + scope, canonically serialised, so it is byte-stable across processes.
    #
    # `scope` is what keeps the §1 double-submit guard from over-matching.
    # `customer_id`/`draft_id`/`actor_id` are columns on the operation, not part
    # of `request_body`, so without it the hash was the file alone: two operators
    # attaching the *same* document (a shared certificate, a blank template) to
    # two different customers resolved to one operation, the second blob insert
    # was dropped by ON CONFLICT DO NOTHING, and the second upload silently
    # became the first one's `doc_` id.
    body = {
        "fileSha256": digest,
        "purpose": purpose,
        "name": name,
        "scope": str(customer_id or draft_id or actor_id),
    }
    op, is_new = await operations.start(
        session,
        type="document_upload",
        actor_id=actor_id,
        actor_email=actor_email,
        path=PATH,
        body=body,
        customer_id=customer_id,
        draft_id=draft_id,
        intent=intent,
    )
    # **A spent nonce replayed with a different file**. `start` resolves
    # an intent nonce on the nonce alone, scoped to the operation type, so the
    # nonce the first upload spent resolved this one onto it — a *different*
    # file, whose bytes were then dropped by the ON CONFLICT DO NOTHING below,
    # exactly the over-matching the `scope` comment above describes but reached
    # through the nonce instead of the hash. The caller went on to render the
    # chip from `op.conduit_resource_id`, so the operator saw the FIRST file's
    # `doc_` id beside the name of the file they had just picked, and attached
    # it to a compliance answer or a payment believing it was that file.
    #
    # This lives here rather than at the route because the route never sees the
    # file's digest, so it cannot recompute the hash to compare. `Rejected` is
    # what every other intake refusal raises and what the upload route already
    # catches, so no new plumbing hangs off this.
    if not is_new and operations.resolved_elsewhere(op, PATH, body):
        raise Rejected(
            "this upload's submission token had already been used for a different "
            "file, so nothing was uploaded and nothing was stored — reload the page "
            "and pick the file again"
        )
    # Insert-if-absent rather than insert-if-new: `operations.start` commits
    # before this runs, so a crash in between would otherwise leave an upload
    # operation whose bytes never arrived — and every retry would resolve to
    # that same blob-less row until the TTL reaped it. Re-storing is provably
    # safe: the file's digest is part of the hash that matched.
    await session.execute(
        pg_insert(DocumentBlob)
        .values(
            id=uuid.uuid4(),
            operation_id=op.id,
            filename=filename,
            content_type=content_type,
            sha256=digest,
            size=len(data),
            purpose=purpose,
            name=name,
            data=data,
        )
        .on_conflict_do_nothing(index_elements=["operation_id"])
    )
    await session.commit()
    return op, is_new


async def attachable(
    session: AsyncSession,
    ids: Sequence[str],
    *,
    purpose: str,
    actor_id: str | None = None,
    draft_id: uuid.UUID | None = None,
) -> set[str]:
    """Which of these `doc_` ids were uploaded from this console, for this
    purpose, inside the scope the caller names. Everything else is not theirs to
    attach.

    **A document id on a form is an assertion, not a fact**.
    Three routes forwarded whatever `documentIds[]` the browser sent straight
    into a Conduit body — the RFI response, and the payout and transfer submits
    that share `payments.payout_body`'s `documents[]` — so any `doc_` id an
    operator could guess or had seen elsewhere (another customer's passport
    scan, a colleague's upload) could be published to Conduit under this
    organisation's name, and attached to a *payment*. Nothing downstream
    re-checked it: Conduit's own validation is org-wide, which is exactly the
    boundary this crosses.

    All six attaching routes are under the same rule — whitelist registration
    and the account request (both `feature_request` evidence) and the onboarding
    wizard's submit (`organization_onboarding`) included. The exposure is
    identical on each, and `OPERATIONS_SPEC §3` records it.

    **Uploader-actor is the tightest rule that is also honest here**, and the
    reason is structural rather than a preference: neither an `rfi_response` nor
    a `transaction_support` upload carries a customer (`web/onboarding.upload`
    passes `draft_id` and the actor, and a draft only exists for onboarding), so
    there is no customer scope on the row to compare against — a "same customer"
    rule would have to invent the association it claims to check. What the row
    does carry is who uploaded it and what for, and both are read from columns
    that outlive the retention purge (`purge_blobs` empties the bytes and the
    names, never `purpose`).

    **`draft_id` is the scope onboarding uses instead**, because onboarding is
    the one flow whose uploads *do* carry their subject: `web/onboarding.upload`
    passes the draft, and a draft is one customer's application. It is the
    tighter question of the two — "was this uploaded for this application", not
    merely "did you upload it" — and it is the one the exposure is about, since
    an operator running two applications at once has, on the actor rule alone,
    one customer's identity documents attachable to the other's submission.

    It replaces the actor rather than adding to it, and the reason is that the
    draft already *is* an access boundary: `web/onboarding._draft` 404s a draft
    that is neither yours nor reachable with `onboarding.access_any`, and the
    upload route goes through it, so nothing can be uploaded against a draft by
    someone with no business in that draft. Requiring the actor on top of it
    would refuse the split-role deployment — the editor fills the draft in and
    uploads, a reviewer reads and submits — where the
    submitter is *never* the uploader and every attachment would be refused.
    That is a supported shape, not a loophole, and a rule that breaks it is
    reporting a compliance failure at a colleague.

    So: `actor_id` alone for the five routes with no subject on the row,
    `draft_id` alone for the one that has one. Both together is expressible and
    nothing asks for it today. Neither, on the other hand, is a rule that says
    nothing — `unattachable` refuses to be called that way.
    """
    if not ids:
        return set()
    rows = await session.execute(
        select(Operation.conduit_resource_id)
        .join(DocumentBlob, DocumentBlob.operation_id == Operation.id)
        .where(
            Operation.type == "document_upload",
            Operation.state == "confirmed",
            Operation.conduit_resource_id.in_(list(ids)),
            DocumentBlob.purpose == purpose,
            *([Operation.actor_id == actor_id] if actor_id is not None else []),
            *([Operation.draft_id == draft_id] if draft_id is not None else []),
        )
    )
    return {row for row in rows.scalars() if row}


# One sentence for every attaching route. Two screens phrasing the same refusal
# differently is how "optional here, required there" gets into an operator's
# head — and this one has to be read the same way on a payment form as on a
# compliance answer, an account application or an onboarding submission.
REFUSED_ATTACHMENT = (
    "Attach only documents you uploaded here for this purpose — {count} of the "
    "attachments could not be matched to one. Upload the file again from this form "
    "and resend."
)


async def unattachable(
    session: AsyncSession,
    ids: Sequence[str],
    *,
    purpose: str,
    actor_id: str | None = None,
    draft_id: uuid.UUID | None = None,
) -> list[str]:
    """The ids from `ids` that this caller may **not** attach — `[]` when all of
    them are in scope.

    The decision every attaching route shares, in one place, so the six of them
    cannot come to differ about what "not yours" means. The *refusal* stays with
    each route, because they show it differently: the money forms re-render with
    the operator's values intact, the RFI panel redirects to the subject page,
    the wizard sends the reviewer back to the review.

    `actor_id`/`draft_id` are passed straight through, so the narrower onboarding
    rule is the *same* rule with a different column compared, not a second
    implementation of it (`attachable` has the argument).
    """
    if actor_id is None and draft_id is None:
        # A scopeless call would answer "everything anyone ever uploaded for this
        # purpose is attachable" — which is not a weaker rule, it is the absence
        # of one, and it would read at the call site exactly like a rule. Louder
        # than a default, because the only way to reach it is a caller that
        # forgot to say whose documents these are.
        raise ValueError("unattachable needs a scope: an actor, a draft, or both")
    allowed = await attachable(
        session, ids, purpose=purpose, actor_id=actor_id, draft_id=draft_id
    )
    return [doc for doc in ids if doc not in allowed]


async def blob(session: AsyncSession, operation_id: uuid.UUID) -> DocumentBlob | None:
    return (
        await session.execute(
            select(DocumentBlob)
            .where(DocumentBlob.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def multipart(session: AsyncSession, op: Operation) -> dict:
    """`httpx` kwargs for the upload — the same body on the first attempt and on
    every reconciler replay, which is what makes replaying with the original
    idempotency key safe."""
    stored = await blob(session, op.id)
    if stored is None or stored.data is None:
        raise BlobUnavailable(f"operation {op.id}: the uploaded file's bytes are not available")
    # From the blob's own columns, not the operation's `request_body`: the blob
    # is the record of what was sent, and it is the row that has to still be
    # sufficient on its own when a replay happens.
    fields = {"purpose": stored.purpose}
    if stored.name is not None:
        fields["name"] = stored.name
    return {"files": {"file": (stored.filename, stored.data, stored.content_type)}, "data": fields}


async def purge_blobs(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker retention job — `operations.purge_request_bodies`' rules, applied to
    the one column those rules do not reach.

    Same window (`OP_BODY_RETENTION`), same state set: `stalled` is excluded, so
    a retryable upload keeps the file it would have to resend.

    The bytes are not the only personal data on the row: `filename` and `name`
    are operator- and customer-supplied strings that routinely carry a person's
    identity ("Jane_Doe_passport_123456789.pdf"), and `sha256` is a fingerprint
    of content we no longer hold. All four go together — retention that leaves
    the label behind has not really deleted anything. What survives is the
    shape of the record: content type, size, purpose, timestamps.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().op_body_retention_days)
    purged = await session.execute(
        update(DocumentBlob)
        .where(
            DocumentBlob.data.is_not(None),
            DocumentBlob.operation_id.in_(
                select(Operation.id).where(
                    Operation.state.in_(TERMINAL_STATES), Operation.resolved_at < cutoff
                )
            ),
        )
        # `filename`/`sha256` are NOT NULL: emptied rather than nulled, which
        # says "purged" just as clearly and needs no migration. Nothing reads
        # either after the bytes are gone — `multipart()` raises BlobUnavailable
        # first, and the digest also lives in the operation's `request_body`,
        # which `purge_request_bodies` drops on the same tick.
        .values(data=None, filename="", name=None, sha256="", purged_at=now)
    )
    await session.commit()
    return purged.rowcount
