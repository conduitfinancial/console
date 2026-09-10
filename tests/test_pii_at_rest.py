"""PII at rest in the columns no encryption and no retention job covers.

Every other store of the same data subtree is encrypted — `counterparties.
recipient`, `operations.request_body`, `drafts.payload`, `payout_batch_rows.
payload` are `EncryptedJSON`, `document_blobs.data` is `EncryptedBytes`. These
two were not, and neither has a retention job to age the exposure out:

* `projections.payload` is **minimized** — four code paths read it through SQL
  JSONB pointers, so it cannot be encrypted; the coordinate and address subtrees
  are stripped at the single write boundary instead.
* `webhook_events.raw_body` is **encrypted** — read only by primary key, never
  filtered or queried by content, so encryption costs nothing and the exact
  signed artifact is still what gets replayed.

Section D is the third column and a different fault. `payout_batch_rows.errors`
is written by this console's own validators rather than by ingress, so what is
at rest there is a sentence this code chose to write — and three of those
sentences quoted the operator's typed cell verbatim. It is plaintext
JSONB, it outlives the encrypted `payload` its row is purged of, and its first
message is printed into the results CSV.

The assertions are absence-of-the-value, not presence-of-a-mask: a coordinate
can leak through a second field even when the first one is handled, and only
"the digits are nowhere in the column" catches that.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, select, text

from app import projections, worker
from app.config import get_settings
from app.crypto import fernet
from app.models import Projection
from tests.conftest import ROOT, alembic_config
from tests.payments_fixtures import CID
from tests.test_batches import CONTACT_ROW, ROW, make_app, routes, stub, uploaded
from tests.test_webhooks import post
from tests.web_harness import signed_in

WITHDRAWAL = json.loads(
    (Path(ROOT) / "tests/fixtures/payout_live_withdrawal.json").read_text()
)

# Everything in the live withdrawal that identifies the payee's account or where
# it (or its bank) is. Each is asserted absent on its own: `destination.
# recipient` holds all of them, but a fix that only knew about the account
# number would leave the rest.
COORDINATES = (
    "000123456789",  # destination.recipient.accountNumber
    "021000021",  # destination.recipient.routingNumber
    "ZZZTEST Globex Supplies 940a80c3b6c7",  # destination.recipient.legalName
    "500 Market St",  # postalAddress + bankAddress addressLine1
    "10010",  # postalAddress.postalCode
)


def delivery(event_id: str, event_type: str, data: dict) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "apiVersion": "2",
            "mode": "sandbox",
            "createdAt": "2026-08-28T18:36:01.331Z",
            "data": data,
        }
    ).encode()


async def raw_column(session, sql: str):
    """One column read as the database holds it — never through the ORM, which
    would decrypt or deserialize the very thing under test."""
    return (await session.execute(text(sql))).scalars().all()


# --- A. the projection payload -----------------------------------------------------


async def test_a_live_withdrawal_delivery_leaves_no_payee_coordinates_at_rest(session):
    """Real ingress, end to end: signed POST → inbox → worker → projection.

    Not a hand-built row — the point is that the *delivery path* minimizes, so
    the fixture goes in exactly as Conduit sends it and the raw SQL column is
    what gets searched.
    """
    raw = delivery("evt_pii_1", "transaction.created", WITHDRAWAL)
    assert (await post(raw)).status_code == 200
    assert await worker.process_pending(session) == {"processed": 1}

    (stored,) = await raw_column(session, "select payload::text from projections")
    for value in COORDINATES:
        assert value not in stored, f"{value!r} survived into projections.payload"


async def test_the_projection_still_carries_what_the_console_reads(session):
    """The minimization is a scalpel: everything the four readers pointer into is
    still there, and so is the resource's own identity."""
    raw = delivery("evt_pii_2", "transaction.created", WITHDRAWAL)
    assert (await post(raw)).status_code == 200
    await worker.process_pending(session)

    row = (await session.execute(select(Projection))).scalar_one()
    assert row.resource_id == WITHDRAWAL["id"]
    assert row.state == "pending"
    # `customerId` and `customerName` are the dashboard's naming pair; the money
    # itself, the stage and the purpose are the row's meaning.
    assert row.payload["customerId"] == WITHDRAWAL["customerId"]
    assert row.payload["customerName"] == WITHDRAWAL["customerName"]
    assert row.payload["destination"]["assetAmount"] == {"amount": "11.00", "code": "USD"}
    assert row.payload["source"]["virtualAccountId"] == "vac_033tVRIHe2Ej5mLRrW2KdD"
    assert row.payload["purpose"] == "payment_for_goods_or_services"


async def test_an_unknown_state_is_still_stored_verbatim(session):
    """The verbatim rule (plan v2 §7) yields for PII and for nothing else: a
    status this build has never heard of is still stored exactly as it arrived,
    and so is the unknown field that carried it."""
    await projections.apply_observation(
        session,
        resource_kind="transactions",
        resource_id="txn_unknown_state",
        observed={"id": "txn_unknown_state", "status": "quantum_settling", "novelField": 7},
    )
    row = (await session.execute(select(Projection))).scalar_one()
    assert row.state == "quantum_settling"
    assert row.payload["novelField"] == 7


async def test_a_virtual_accounts_read_keeps_its_deposit_instructions_out(session):
    """The other coordinate-bearing subtree the sweep and the webhooks can carry:
    `depositInstructions[]` — account number, routing numbers, the beneficiary's
    postal address. Nothing reads it from the projection (the account page reads
    Conduit live), so it is stripped like the rest, while the account-index
    filters — `customerId`, `asset.code`, `activatedAt` — survive.
    """
    account = json.loads(
        (Path(ROOT) / "tests/fixtures/virtual_account_live_usd.json").read_text()
    )
    account["customerId"] = "cus_pii"
    await projections.apply_observation(
        session,
        resource_kind="virtual_accounts",
        resource_id=account["id"],
        observed=account,
    )

    (stored,) = await raw_column(session, "select payload::text from projections")
    for value in ("0000000000000000", "000000000", "123 Sandbox Way", "Testville"):
        assert value not in stored

    row = (await session.execute(select(Projection))).scalar_one()
    assert row.payload["customerId"] == "cus_pii"
    assert row.payload["asset"]["code"] == "USD"
    assert row.payload["activatedAt"] == "2026-08-20T20:40:51.662Z"


async def test_a_whitelist_recipient_keeps_its_coordinates_out(session):
    """`WhitelistRecipientResponseDto` carries the coordinates at the *top* level
    — `accountNumber`, `routingNumber`, `iban`, `bic` — with no subtree to strip,
    which is why the scrub is by key at every depth rather than by path."""
    recipient = json.loads(
        (Path(ROOT) / "tests/fixtures/whitelist_recipient_live.json").read_text()
    )
    await projections.apply_observation(
        session,
        resource_kind="whitelist_recipients",
        resource_id=recipient["id"],
        observed=recipient,
    )
    (stored,) = await raw_column(session, "select payload::text from projections")
    assert "000123456789" not in stored and "021000021" not in stored
    row = (await session.execute(select(Projection))).scalar_one()
    assert row.state == "pending_review"
    assert row.payload["customerId"] == recipient["customerId"]


# --- B. the raw body ---------------------------------------------------------------


async def test_the_stored_delivery_is_unreadable_at_rest_and_replays_exactly(session):
    raw = delivery("evt_pii_3", "transaction.created", WITHDRAWAL)
    assert (await post(raw)).status_code == 200

    (stored,) = await raw_column(session, "select raw_body from webhook_events")
    # Whatever the column's type is, the question is what the bytes on disk say.
    blob = bytes(stored) if isinstance(stored, bytes | memoryview) else str(stored).encode()
    assert b"000123456789" not in blob
    assert b"txn_" not in blob  # not even the resource id is legible
    # …and it is still the exact signed artifact, byte for byte: that is what the
    # column is for, and what the worker replays.
    assert fernet().decrypt(blob) == raw

    # The worker still reads and projects it, through the same column.
    assert await worker.process_pending(session) == {"processed": 1}


async def test_the_event_id_hash_is_still_computed_over_the_raw_bytes(session):
    """`app/webhooks/inbox.py` dedupes an id-less delivery on `sha256(raw_body)`,
    computed at store time. Encryption is not deterministic, so if that hash ever
    moved to the stored column the same delivery would insert twice."""
    raw = b'{"type":"transaction.created","data":{"id":"txn_no_event_id"}}'
    for _ in range(2):
        assert (await post(raw)).status_code == 200
    assert await raw_column(session, "select count(*) from webhook_events") == [1]


# --- C. the rows that were already there -------------------------------------------

# The revision before the fix. Downgrading to it puts the two columns back the
# way production holds them today, so the seeded rows are genuinely pre-existing
# ones rather than rows the new code wrote.
BEFORE = "f2a7c31e9b04"

SEEDED_PROJECTION = "11111111-2222-3333-4444-555555555555"
SEEDED_EVENT = "66666666-7777-8888-9999-000000000000"


def test_the_migration_scrubs_and_encrypts_rows_that_already_exist(schema):
    """A defect that only stops for future rows leaves the exposure in the
    database. Seed the plaintext under the old schema, migrate, and the
    coordinates are gone from one column and unreadable in the other."""
    cfg = alembic_config()
    engine = create_engine(get_settings().database_url)
    raw = delivery("evt_pii_migrated", "transaction.created", WITHDRAWAL)

    command.downgrade(cfg, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into projections (id, resource_kind, resource_id, state, payload) "
                "values (:id, 'transactions', 'txn_migrated', 'pending', :payload)"
            ),
            {"id": SEEDED_PROJECTION, "payload": json.dumps(WITHDRAWAL)},
        )
        # NULL and non-dict payloads exist in the wild (an observation with no
        # body, a resource that answered a bare array) — the migration must step
        # over both rather than fail the deploy.
        conn.execute(
            text(
                "insert into projections (id, resource_kind, resource_id, payload) values "
                "(gen_random_uuid(), 'transactions', 'txn_null_payload', null), "
                "(gen_random_uuid(), 'transactions', 'txn_list_payload', '[1, 2]')"
            )
        )
        conn.execute(
            text(
                "insert into webhook_events (id, event_id, event_type, raw_body, status) "
                "values (:id, 'evt_pii_migrated', 'transaction.created', :body, 'pending')"
            ),
            {"id": SEEDED_EVENT, "body": raw.decode()},
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        stored = conn.execute(
            text("select payload::text from projections where id = :id"),
            {"id": SEEDED_PROJECTION},
        ).scalar_one()
        body = conn.execute(
            text("select raw_body from webhook_events where id = :id"), {"id": SEEDED_EVENT}
        ).scalar_one()
        survivors = conn.execute(
            text("select resource_id, payload::text from projections order by resource_id")
        ).all()

    for value in COORDINATES:
        assert value not in stored
    # Minimized, not emptied: the row still says what it is about.
    assert json.loads(stored)["customerId"] == WITHDRAWAL["customerId"]
    assert bytes(body) != raw and fernet().decrypt(bytes(body)) == raw
    # The odd-shaped rows came through untouched rather than dropped.
    assert dict(survivors)["txn_null_payload"] is None
    assert dict(survivors)["txn_list_payload"] == "[1, 2]"

    with engine.begin() as conn:
        conn.execute(text("delete from projections"))
        conn.execute(text("delete from webhook_events"))
    engine.dispose()


# --- identity, not just coordinates (2026-09-01) ---------------------------------------


async def test_an_application_delivery_leaves_no_beneficial_owners_at_rest(session):
    """`CustomerOnboardingApplicationDto.persons[]` is the beneficial owners and
    directors — names, dates of birth, identity documents. Nothing reads it from
    a projection (the onboarding wizard reads `Draft.payload`, which is
    `EncryptedJSON` and a different shape), so holding it in a cache with no
    retention job buys nothing and risks the sharpest data in the system.
    """
    application = {
        "id": "app_pii_persons",
        "customerId": "cus_pii_1",
        "status": "processing",
        "persons": [
            {
                "firstName": "Zzztest",
                "lastName": "Beneficialowner",
                "dateOfBirth": "1980-04-01",
                "taxId": "999-88-7777",
                "contactEmail": "owner@zzztest.example",
                "phone": "+1-555-0100",
            }
        ],
    }
    raw = delivery("evt_pii_persons", "application.created", application)
    assert (await post(raw)).status_code == 200
    await worker.process_pending(session)

    stored = (
        await session.execute(text("select payload::text from projections"))
    ).scalar_one()
    for secret in (
        "Beneficialowner",
        "1980-04-01",
        "999-88-7777",
        "owner@zzztest.example",
        "+1-555-0100",
    ):
        assert secret not in stored, f"{secret!r} survived in projections.payload"
    # The row is still the row: identity and state are what the console reads.
    row = (await session.execute(select(Projection))).scalar_one()
    assert row.resource_id == "app_pii_persons"
    assert row.state == "processing"
    assert row.payload["customerId"] == "cus_pii_1"


async def test_an_rfi_delivery_leaves_no_response_bodies_at_rest(session):
    """`ClientRfiDetailDto.responses[]` carries the answer text and the email of
    whoever submitted it. The reconciler's lookup-before-replay recipe does match
    on `submittedByEmail` — but it reads the RFI live from Conduit
    (`app/reconciliation/service.py`), never from this cache, so stripping it
    here cannot weaken the guard that stops a duplicate RFI response."""
    rfi = {
        "id": "rfi_pii_1",
        "customerId": "cus_pii_1",
        "status": "responded",
        "subjects": [{"kind": "customer"}],
        "responses": [
            {
                "id": "rsp_1",
                "message": "Attached is the ZZZTEST ownership chart for Beneficialowner.",
                "submittedByEmail": "ops@zzztest.example",
            }
        ],
    }
    raw = delivery("evt_pii_rfi", "rfi.response_submitted", rfi)
    assert (await post(raw)).status_code == 200
    await worker.process_pending(session)

    stored = (
        await session.execute(text("select payload::text from projections"))
    ).scalar_one()
    assert "ops@zzztest.example" not in stored
    assert "ownership chart" not in stored
    # `subjects` is the key the stale sweep filters on — it must survive.
    row = (await session.execute(select(Projection))).scalar_one()
    assert row.payload.get("subjects"), "the stale sweep's own predicate was scrubbed"
    assert row.state == "responded"


def test_the_scrub_keeps_what_the_console_reads():
    """The two sets must never intersect. Stripping a key some surface pointers
    into would not fail loudly — it would render an empty dashboard cell or a
    silently-narrowed sweep, which is the honesty failure this console exists to
    avoid, arrived at through a privacy fix.
    """
    from app.projections import _READ_FROM_PROJECTIONS, _is_pii

    stripped = sorted(key for key in _READ_FROM_PROJECTIONS if _is_pii(key))
    assert not stripped, f"{stripped} are read from projections and must not be scrubbed"
    # And non-vacuous: the guard only means something if `_is_pii` really bites.
    assert _is_pii("persons") and _is_pii("accountNumber") and _is_pii("postalAddress")


# --- D. the column an operator writes ---------------------------------------------------
#
# `payout_batch_rows.errors` is the third store of typed data with no retention
# job — plaintext JSONB, kept after `purge_row_payloads` has taken the row's
# encrypted `payload`, and printed into the results CSV's `problem` column. The
# other two columns above are written by ingress; this one is written by this
# console's own validators, so the leak is not a subtree that arrived but a
# sentence this code chose to write.
#
# The value is a 17-digit number in cells that hold none: a contact name, a
# two-value enum and a purpose key. That is not a contrived input — it is one
# spreadsheet pasted a column over, which is the fault these three refusals
# exist to report, and the reason the refusal is holding an account number at
# exactly the moment it is loudest.

PASTED = "12345678901234567"


def longest_run(number: str, text: str) -> str:
    """The longest run of `number`'s own digits still readable in `text`.

    Absence-of-the-value, generalized: asserting only that the whole number is
    gone would pass on a message that dropped one digit, and asserting that a
    particular mask is present would pass on a second field that leaked the
    rest beside it.
    """
    runs = (
        number[start:end]
        for start in range(len(number))
        for end in range(start + 1, len(number) + 1)
        if number[start:end] in text
    )
    return max(runs, key=len, default="")


async def batch_of(rows: list[dict]) -> str:
    """One file uploaded through the real path, and its results CSV. The stored
    column is read back with `raw_column`, as everything else in this file is:
    the question is what the database holds, not what the ORM hands back."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, rows)
        exported = await web.get(
            f"/export/batch_rows.csv?customerId={CID}&batchId={url.rsplit('/', 1)[-1]}"
        )
    assert exported.status_code == 200, exported.text
    return exported.text


async def test_an_account_number_pasted_into_the_contact_column_is_masked_before_it_is_stored(
    session,
):
    """The reproduction from the ticket, end to end: before this fix the stored
    error read `No live contact called '12345678901234567' …` in cleartext and
    the results CSV printed the same sentence, which made a mis-paste the only
    permanent, unencrypted copy of that number on the console."""
    exported = await batch_of([{**CONTACT_ROW, "contact": PASTED}])
    (stored,) = await raw_column(session, "select errors::text from payout_batch_rows")

    assert "No live contact called" in stored, "the refusal itself must survive the masking"
    assert len(longest_run(PASTED, stored)) <= 4, stored
    assert len(longest_run(PASTED, exported)) <= 4, exported
    # Masked, not withheld: the tail is what tells two bad contacts apart in a
    # 500-row file, and the column that holds it is named beside it.
    assert "'••••4567'" in stored and "'••••4567'" in exported
    assert '"column": "contact"' in stored


async def test_a_pasted_number_in_an_enum_or_purpose_cell_is_masked_before_it_is_stored(session):
    """The same paste, one column further along in each direction: the enum
    membership message (`app/forms.py`) and the unknown-purpose refusal. Both
    are written per row into the same column and both quoted the cell whole."""
    exported = await batch_of(
        [
            {**ROW, "destination.recipient.accountType": PASTED},
            {**ROW, "purpose": PASTED},
        ]
    )
    enum_row, purpose_row = await raw_column(
        session, "select errors::text from payout_batch_rows order by row_number"
    )

    assert "is not an accepted value" in enum_row
    assert "is not a purpose this route can be read for" in purpose_row
    for stored in (enum_row, purpose_row):
        assert len(longest_run(PASTED, stored)) <= 4, stored
    assert len(longest_run(PASTED, exported.split("\r\n")[1])) <= 4, exported

    # …and the residue this fix does not reach, stated rather than implied: the
    # purpose *cell* is stored verbatim in its own column (`models.py` keeps it
    # unconstrained so the report can show what the file said) and the results
    # export prints it. Masking the sentence is worth doing anyway — it is what
    # the `contact` and enum cells have no equivalent of — but a reader of this
    # file should not conclude the row holds nothing typed.
    (purpose_cell,) = await raw_column(
        session, "select purpose from payout_batch_rows where row_number = 2"
    )
    assert purpose_cell == PASTED
