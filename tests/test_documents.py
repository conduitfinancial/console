"""Multipart document upload, end to end (OPERATIONS_SPEC §3).

Closes the Phase-1 deferral: intake validation at the trust boundary, an
encrypted blob at rest, a real multipart body on the wire with the operation's
idempotency key, and a reconciler replay rebuilt from the same blob.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select, text, update

from app import documents, operations, reconciliation
from app.conduit import ConduitClient, execute_operation
from app.models import DocumentBlob, Operation

ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}

PDF = b"%PDF-1.7\n" + b"payslip for Jane Doe, 4,210.00 EUR\n" + b"%%EOF"
# Structurally whole, not just correctly-prefixed: intake checks the end marker
# (JPEG EOI) and the first chunk header (PNG IHDR) as well as the signature.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 32


class Recorder:
    """Captures the raw request so the multipart body can be asserted on."""

    def __init__(self, respond=None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda n: httpx.Response(201, json={"id": f"doc_{n}"}))

    def client(self) -> ConduitClient:
        def handle(request: httpx.Request) -> httpx.Response:
            request.read()
            self.requests.append(request)
            return self._respond(len(self.requests))

        return ConduitClient(transport=httpx.MockTransport(handle))

    @property
    def uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


async def upload(session, *, data=PDF, filename="payslip.pdf", purpose="kyc", name="Payslip"):
    return await documents.intake(
        session, data=data, filename=filename, purpose=purpose, name=name, **ACTOR
    )


async def reload(session, op_id) -> Operation:
    return (
        await session.execute(
            select(Operation).where(Operation.id == op_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- intake: the trust boundary --------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PDF, "application/pdf"),
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
    ],
)
def test_accepted_types_are_recognised_by_their_magic_bytes(data, expected):
    assert documents.sniff(data) == expected


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"", "empty"),
        (b"GIF89a" + b"\x00" * 32, "a GIF is not on the list"),
        (b"MZ\x90\x00" + b"\x00" * 32, "an executable"),
        (b"<html><body>not a document</body></html>", "HTML"),
        (b"\x00" * 32, "no signature at all"),
        (b"%PDF" , "a truncated signature"),
        (b"\x00%PDF-1.7", "the signature has to be at the start"),
        (b"%PDF-1.7" + b"\x00" * (documents.MAX_BYTES + 1) + b"%%EOF", "over 10 MB"),
    ],
)
def test_rejected_files_never_become_an_upload(data, why):
    with pytest.raises(documents.Rejected):
        documents.sniff(data)


def test_the_size_limit_is_ten_megabytes():
    assert documents.MAX_BYTES == 10 * 1024 * 1024
    assert documents.sniff(b"%PDF-1.7" + b"\x00" * (documents.MAX_BYTES - 13) + b"%%EOF")


async def test_a_lying_filename_and_content_type_do_not_get_a_vote(session):
    """The spec says Conduit inspects magic bytes rather than the filename or
    the header; so does intake, or a rejected upload would only be rejected
    after the file had already crossed our boundary."""
    with pytest.raises(documents.Rejected, match="magic bytes"):
        await upload(session, data=b"<script>alert(1)</script>", filename="scan.pdf")

    assert await session.scalar(select(Operation).limit(1)) is None  # nothing left behind


async def test_an_unknown_purpose_is_refused(session):
    with pytest.raises(documents.Rejected, match="purpose"):
        await upload(session, purpose="tax_return")


async def test_intake_stores_the_file_and_its_provenance(session):
    op, is_new = await upload(session)

    assert is_new and op.type == "document_upload" and op.request_path == "/v2/documents"
    blob = await documents.blob(session, op.id)
    assert blob is not None
    assert (blob.filename, blob.content_type, blob.purpose) == ("payslip.pdf", "application/pdf", "kyc")
    assert (blob.size, blob.data) == (len(PDF), PDF)
    assert blob.sha256 == op.request_body["fileSha256"]


async def test_the_file_is_encrypted_at_rest(session):
    op, _ = await upload(session)

    stored = await session.scalar(
        text("select data from document_blobs where operation_id = :id").bindparams(id=op.id)
    )
    assert b"Jane Doe" not in bytes(stored) and b"%PDF" not in bytes(stored)
    assert (await documents.blob(session, op.id)).data == PDF  # …and decrypts


# --- dedupe: the §1 guard, for uploads --------------------------------------------------


async def test_the_same_file_twice_while_active_is_one_operation(session):
    """The double-click / second-tab guard, reached the same way every other
    mutation reaches it: `request_hash` over path + file digest + purpose + name."""
    first, first_new = await upload(session)
    second, second_new = await upload(session)

    assert (first_new, second_new) == (True, False)
    assert second.id == first.id and second.idempotency_key == first.idempotency_key
    assert await session.scalar(select(DocumentBlob.id).where(DocumentBlob.operation_id == first.id))
    assert len((await session.execute(select(DocumentBlob))).scalars().all()) == 1


@pytest.mark.parametrize(
    "different",
    [{"data": PDF + b" v2"}, {"purpose": "rfi_response"}, {"name": "Payslip (corrected)"}],
)
async def test_any_change_to_the_hashed_metadata_is_a_new_operation(session, different):
    first, _ = await upload(session)
    second, is_new = await upload(session, **different)

    assert is_new and second.id != first.id
    assert second.idempotency_key != first.idempotency_key


async def test_the_hash_is_stable_across_processes(session):
    """Byte-stable: canonical JSON of the hashed fields, not a dict repr."""
    op, _ = await upload(session)
    assert op.request_hash == operations.request_hash(
        "/v2/documents",
        {
            "name": "Payslip",
            "purpose": "kyc",
            "fileSha256": op.request_body["fileSha256"],
            "scope": ACTOR["actor_id"],
        },
    )


# --- execution: a real multipart body ---------------------------------------------------


def parts(request: httpx.Request) -> bytes:
    return request.content


async def test_the_upload_is_sent_as_multipart_with_the_idempotency_key(session):
    recorder = Recorder()
    client = recorder.client()
    op, _ = await upload(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    (sent,) = recorder.uploads
    assert sent.url.path == "/v2/documents"
    assert sent.headers["Idempotency-Key"] == str(op.idempotency_key)
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")

    body = parts(sent)
    assert b'name="file"; filename="payslip.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert PDF in body  # the bytes themselves, not a base64 or JSON rendering
    assert b'name="purpose"' in body and b"kyc" in body
    assert b'name="name"' in body and b"Payslip" in body

    assert (op.state, op.conduit_resource_id) == ("confirmed", "doc_1")


async def test_an_optional_name_is_simply_absent_from_the_body(session):
    recorder = Recorder()
    client = recorder.client()
    op, _ = await upload(session, name=None)
    await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    body = parts(recorder.uploads[0])
    assert b'name="purpose"' in body
    assert b'name="name"' not in body  # never a literal "None"


async def test_a_crash_before_the_blob_landed_is_recoverable(session):
    """`operations.start` commits first, so a death in between leaves an upload
    operation with no bytes. The retry resolves to that same row (correct — the
    key must not change) and backfills the file rather than wedging on it."""
    recorder = Recorder()
    client = recorder.client()

    op, _ = await upload(session)
    await session.execute(delete(DocumentBlob).where(DocumentBlob.operation_id == op.id))
    await session.commit()

    again, is_new = await upload(session)
    assert (again.id, is_new) == (op.id, False)  # same row, same idempotency key
    assert (await documents.blob(session, op.id)).data == PDF

    await execute_operation(session, again, client=client, **ACTOR)
    await client.aclose()
    assert PDF in parts(recorder.uploads[0])


async def test_the_bytes_never_reach_the_operation_row(session):
    """`request_body` is the *metadata*: the digest is what identifies the file,
    and a 10 MB base64 blob in an encrypted JSON column is not a request body."""
    op, _ = await upload(session)
    assert set(op.request_body) == {"fileSha256", "purpose", "name", "scope"}
    assert PDF.decode("latin-1") not in str(op.request_body)


# --- reconciliation: replay from the blob ------------------------------------------------


async def test_a_lost_upload_is_replayed_from_the_blob_with_the_same_key(session):
    """§3's documented low-risk exception: uploads have no reference field, so
    the recipe replays. The replay has to be the *same* file — which is the
    whole reason the blob exists."""
    recorder = Recorder(
        respond=lambda n: httpx.Response(201, json={"id": "doc_1"}) if n > 1 else httpx.Response(504)
    )
    client = recorder.client()

    op, _ = await upload(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    assert op.state == "outcome_unknown"

    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(unknown_since=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()
    assert await reconciliation.reconcile_pass(session, client) == {"confirmed": 1}
    await client.aclose()

    first, replay = recorder.uploads
    assert replay.headers["Idempotency-Key"] == first.headers["Idempotency-Key"]
    assert PDF in parts(replay) and b'filename="payslip.pdf"' in parts(replay)
    assert (await reload(session, op.id)).conduit_resource_id == "doc_1"


async def test_a_missing_blob_is_never_replayed_as_an_empty_file(session):
    """If the bytes are gone the honest answer is "still unknown". Sending an
    empty `file` part under the original key would either be rejected as a
    corrupt document or, worse, accepted as one."""
    recorder = Recorder(respond=lambda n: httpx.Response(504))
    client = recorder.client()

    op, _ = await upload(session)
    op = await execute_operation(session, op, client=client, **ACTOR)
    await session.execute(
        update(DocumentBlob).where(DocumentBlob.operation_id == op.id).values(data=None)
    )
    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(unknown_since=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()

    assert await reconciliation.reconcile_pass(session, client) == {"unresolved": 1}
    await client.aclose()
    assert len(recorder.uploads) == 1  # the first attempt, and nothing after it
    assert (await reload(session, op.id)).state == "outcome_unknown"


# --- retention ----------------------------------------------------------------------------


async def resolve(session, op_id, state, *, days_ago: int) -> None:
    await operations.transition(session, op_id, "in_flight", **ACTOR)
    await operations.transition(session, op_id, state, **ACTOR, error={} if state == "rejected" else None)
    await session.execute(
        update(Operation)
        .where(Operation.id == op_id)
        .values(resolved_at=datetime.now(UTC) - timedelta(days=days_ago))
    )
    await session.commit()


async def test_blob_bytes_are_purged_on_the_request_body_schedule(session):
    op, _ = await upload(session)
    await resolve(session, op.id, "confirmed", days_ago=40)

    assert await documents.purge_blobs(session) == 1
    blob = await documents.blob(session, op.id)
    assert blob.data is None and blob.purged_at is not None
    # The *shape* of the record survives; every label that could name a person
    # goes with the bytes — a filename like "Jane_Doe_passport_123.pdf" is PII,
    # and a digest fingerprints content we no longer hold.
    assert (blob.filename, blob.name, blob.sha256) == ("", None, "")
    assert (blob.size, blob.purpose, blob.content_type) == (
        len(PDF),
        "kyc",
        "application/pdf",
    )
    assert await documents.purge_blobs(session) == 0  # idempotent


async def test_a_recently_resolved_upload_keeps_its_bytes(session):
    op, _ = await upload(session)
    await resolve(session, op.id, "confirmed", days_ago=3)

    assert await documents.purge_blobs(session) == 0
    assert (await documents.blob(session, op.id)).data == PDF


async def test_a_stalled_upload_is_never_purged(session):
    """The §6 rule that matters: `stalled` is retryable, and the retry has to
    send the same file. Purging it would turn the promised byte-identical replay
    into an empty upload."""
    op, _ = await upload(session)
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await operations.transition(session, op.id, "stalled", **ACTOR)
    await session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(resolved_at=datetime.now(UTC) - timedelta(days=400))
    )
    await session.commit()

    assert await documents.purge_blobs(session) == 0
    assert (await documents.blob(session, op.id)).data == PDF


async def test_the_worker_tick_purges_blobs(session):
    from app import worker

    op, _ = await upload(session)
    await resolve(session, op.id, "confirmed", days_ago=40)

    client = ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    await worker.tick(session, client)
    await client.aclose()

    assert (await documents.blob(session, op.id)).data is None


# --- upload-boundary findings ------------------------------------------------------------


async def test_the_same_file_for_two_subjects_is_two_uploads(session):
    """The §1 guard namespaces per subject.

    Without the scope in the hashed body the hash was the file alone: two
    operators attaching one shared certificate to two customers resolved to a
    single operation, the second blob insert was dropped by
    ON CONFLICT DO NOTHING, and the second upload silently became the first
    one's `doc_` id.
    """
    first, new_first = await documents.intake(
        session, data=PDF, filename="cert.pdf", purpose="kyc", name="Cert",
        customer_id="cus_1", **ACTOR
    )
    second, new_second = await documents.intake(
        session, data=PDF, filename="cert.pdf", purpose="kyc", name="Cert",
        customer_id="cus_2", **ACTOR
    )

    assert new_first and new_second
    assert first.id != second.id
    assert first.idempotency_key != second.idempotency_key
    # Both kept their own bytes — the second insert was not swallowed.
    assert (await documents.blob(session, second.id)).data == PDF
    assert first.request_body["scope"] == "cus_1"
    assert second.request_body["scope"] == "cus_2"


async def test_the_same_file_for_the_same_subject_is_still_one_upload(session):
    """The guard the scope must not break: a double-click is one operation."""
    first, new_first = await documents.intake(
        session, data=PDF, filename="cert.pdf", purpose="kyc", name="Cert",
        customer_id="cus_1", **ACTOR
    )
    second, new_second = await documents.intake(
        session, data=PDF, filename="cert.pdf", purpose="kyc", name="Cert",
        customer_id="cus_1", **ACTOR
    )
    assert new_first and not new_second
    assert first.id == second.id


async def test_a_spent_nonce_carrying_a_different_file_is_refused(session):
    """The intent nonce resolves on the
    nonce alone (`operations.start` guard 1), so the nonce the first upload spent
    resolved a *second*, different file onto the first upload's operation — and
    the blob insert's ON CONFLICT DO NOTHING then dropped the second file's
    bytes. `intake`'s contract is that the operation it returns describes the
    bytes handed in, so this resolution cannot be returned: its caller's only
    move is to print `conduit_resource_id` on a chip beside the filename the
    operator just picked.

    The route can't make this check itself — it never computes the digest that
    goes into the hashed body.
    """
    nonce = uuid.uuid4()
    first, is_new = await documents.intake(
        session, data=PDF, filename="payslip.pdf", purpose="kyc", name="Payslip",
        intent=nonce, **ACTOR
    )
    assert is_new
    with pytest.raises(documents.Rejected) as refused:
        await documents.intake(
            session, data=JPEG, filename="selfie.jpg", purpose="kyc", name="Payslip",
            intent=nonce, **ACTOR
        )
    assert "already been used" in str(refused.value)
    # No second operation, and the only blob is still the first file's.
    assert (await session.scalar(select(func.count(Operation.id)))) == 1
    blobs = (await session.execute(select(DocumentBlob))).scalars().all()
    assert [(b.operation_id, b.filename) for b in blobs] == [(first.id, "payslip.pdf")]


async def test_the_same_nonce_and_the_same_file_is_still_the_one_upload(session):
    """The non-vacuity guard on the refusal above: a mechanically re-sent upload
    of the SAME file is the ordinary double-click, and it must still resolve
    quietly to the operation the first one opened."""
    nonce = uuid.uuid4()
    first, new_first = await documents.intake(
        session, data=PDF, filename="payslip.pdf", purpose="kyc", name="Payslip",
        intent=nonce, **ACTOR
    )
    second, new_second = await documents.intake(
        session, data=PDF, filename="payslip.pdf", purpose="kyc", name="Payslip",
        intent=nonce, **ACTOR
    )
    assert new_first and not new_second
    assert first.id == second.id


async def test_two_actors_uploading_the_same_unscoped_file_do_not_collide(session):
    """No customer and no draft: the actor is the scope of last resort."""
    mine, _ = await documents.intake(
        session, data=PDF, filename="t.pdf", purpose="kyc", name="T",
        actor_id="usr_1", actor_email="a@example.com"
    )
    theirs, is_new = await documents.intake(
        session, data=PDF, filename="t.pdf", purpose="kyc", name="T",
        actor_id="usr_2", actor_email="b@example.com"
    )
    assert is_new and mine.id != theirs.id


@pytest.mark.parametrize(
    "data,why",
    [
        (b"%PDF-1.7\nbody without an end marker", "a PDF cut off before %%EOF"),
        (b"%PDF-" + b"\x00" * 4096, "%PDF- glued onto arbitrary bytes"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 64, "a JPEG with no EOI"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "a PNG whose first chunk is not IHDR"),
    ],
)
def test_structurally_broken_files_are_refused(data, why):
    """A leading signature is a claim, not proof (`sniff` is a gate — Conduit
    re-validates server-side)."""
    with pytest.raises(documents.Rejected, match="truncated or corrupt"):
        documents.sniff(data)


def test_whole_files_of_every_accepted_type_still_pass():
    assert documents.sniff(PDF) == "application/pdf"
    assert documents.sniff(JPEG) == "image/jpeg"
    assert documents.sniff(PNG) == "image/png"
    # Trailing whitespace after %%EOF is normal and must not fail the check.
    assert documents.sniff(PDF + b"\n\n") == "application/pdf"


async def test_a_confirmed_upload_does_not_mark_its_draft_submitted(session):
    """A document uploaded from inside the wizard is work *towards* a
    submission. Stamping the draft on the first uploaded passport marked it
    submitted — which blocked discarding it and started its retention clock
    while the operator was still filling the form in."""
    from app.onboarding import drafts

    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot={"schemaVersion": "3", "fields": []},
        payload={"root": {}, "persons": []},
    )
    op, _ = await documents.intake(
        session, data=PDF, filename="passport.pdf", purpose="kyc", name="Passport",
        draft_id=draft.id, **ACTOR
    )
    recorder = Recorder()
    client = recorder.client()
    await execute_operation(session, op, client=client, **ACTOR)
    await client.aclose()

    assert (await reload(session, op.id)).state == "confirmed"
    still_open = await drafts.load(session, draft.id)
    assert still_open.submitted_at is None and still_open.payload is not None
    assert await drafts.discard(session, draft.id) is True  # still discardable


async def test_a_confirmed_submission_does_mark_its_draft(session):
    """The converse — the guard must not stop the type that should stamp."""
    from app.onboarding import drafts

    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot={"schemaVersion": "3", "fields": []},
        payload={"root": {}, "persons": []},
    )
    op, _ = await operations.start(
        session, type="onboarding_submit", **ACTOR, path="/v2/onboarding",
        body={"a": 1}, draft_id=draft.id
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)

    assert (await drafts.load(session, draft.id)).submitted_at is not None


# --- the attachment rule ---------------------------------------------------------------


async def confirmed(session, *, doc_id: str, purpose: str = "organization_onboarding", **kwargs):
    """One upload taken all the way to `confirmed`, so it is attachable."""
    who = {
        "actor_id": kwargs.pop("actor_id", ACTOR["actor_id"]),
        "actor_email": kwargs.pop("actor_email", ACTOR["actor_email"]),
    }
    op, _ = await documents.intake(
        session, data=PDF, filename=f"{doc_id}.pdf", purpose=purpose, name=doc_id, **who, **kwargs
    )
    await operations.transition(session, op.id, "in_flight", **who)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id=doc_id, **who)
    return op


async def a_draft(session, actor_id: str = ACTOR["actor_id"]):
    from app.onboarding import drafts

    return await drafts.create(
        session,
        kind="onboarding",
        actor_id=actor_id,
        requirements_snapshot={"schemaVersion": "3", "fields": []},
        payload={"root": {}, "persons": []},
    )


async def test_the_actor_rule_does_not_see_another_actors_upload(session):
    await confirmed(session, doc_id="doc_mine", purpose="rfi_response")
    await confirmed(
        session,
        doc_id="doc_theirs",
        purpose="rfi_response",
        actor_id="usr_2",
        actor_email="other@example.com",
    )
    assert await documents.unattachable(
        session, ["doc_mine", "doc_theirs"], purpose="rfi_response", actor_id="usr_1"
    ) == ["doc_theirs"]


async def test_the_wrong_purpose_is_as_unattachable_as_the_wrong_actor(session):
    """Purpose is compared because Conduit's document types are not
    interchangeable: a payout's transaction support is not identity evidence,
    and neither is attachable in the other's place."""
    await confirmed(session, doc_id="doc_kyc", purpose="kyc")
    assert await documents.unattachable(
        session, ["doc_kyc"], purpose="transaction_support", actor_id="usr_1"
    ) == ["doc_kyc"]


async def test_a_draft_scoped_check_refuses_the_same_actors_other_draft(session):
    """One operator, one purpose, two applications: the
    id is theirs and it is the right kind of document, and it is still not this
    customer's evidence."""
    mine, other = await a_draft(session), await a_draft(session)
    await confirmed(session, doc_id="doc_mine", draft_id=mine.id)
    await confirmed(session, doc_id="doc_other_draft", draft_id=other.id)
    # …and one with no draft at all, the shape every non-wizard upload has.
    await confirmed(session, doc_id="doc_loose")

    assert await documents.unattachable(
        session,
        ["doc_mine", "doc_other_draft", "doc_loose", "doc_never_uploaded"],
        purpose="organization_onboarding",
        draft_id=mine.id,
    ) == ["doc_other_draft", "doc_loose", "doc_never_uploaded"]
    # The same three ids on the plain actor rule are all attachable — which is
    # exactly why onboarding does not use it.
    assert await documents.unattachable(
        session,
        ["doc_mine", "doc_other_draft", "doc_loose"],
        purpose="organization_onboarding",
        actor_id="usr_1",
    ) == []


async def test_an_upload_still_in_flight_is_not_yet_attachable(session):
    """Only `confirmed` counts: an id that has not come back from Conduit is not
    an id, and one whose upload failed never will be."""
    draft = await a_draft(session)
    op, _ = await documents.intake(
        session, data=PDF, filename="p.pdf", purpose="organization_onboarding",
        name="P", draft_id=draft.id, **ACTOR
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    assert op.conduit_resource_id is None
    assert await documents.unattachable(
        session, ["doc_anything"], purpose="organization_onboarding", draft_id=draft.id
    ) == ["doc_anything"]


async def test_a_scopeless_check_is_a_programming_error_not_a_pass(session):
    """Both scopes optional is a footgun the moment neither is passed: the query
    would answer "anything anyone uploaded for this purpose", which reads at the
    call site exactly like a rule and is the absence of one."""
    await confirmed(session, doc_id="doc_mine", purpose="kyc")
    with pytest.raises(ValueError, match="needs a scope"):
        await documents.unattachable(session, ["doc_mine"], purpose="kyc")
