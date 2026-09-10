"""Reusable counterparties — the design decision, then the code.

## The decision, and the spec evidence for it

The first question was whether "save this recipient for reuse" is
Conduit's whitelist surfaced better, or a console-side record — and required the
answer to come **from the API, not from assumption**. It is both, split by route,
and the split is forced:

* **`whitelist-recipients` is Conduit's only server-side recipient store.** The
  pinned spec (`contracts/openapi_production.json`, 67 paths) exposes exactly
  four recipient-shaped operations — `GET`/`POST
  /customers/{customerId}/whitelist-recipients` and `GET`/`DELETE …/{id}` — under
  one tag, *Whitelist Recipients*. Scanning every path, schema name, tag and
  operation body for `recipient|counterpart|benefic|contact|payee|address.?book`
  turns up no second store, and no `Counterparty*`/`Beneficiary*`/`Contact*`
  schema exists. `test_the_pinned_spec_still_has_exactly_one_recipient_store`
  pins this so a spec re-pin that adds a real address book fails here rather than
  leaving this table quietly redundant.

* **That store is the intercompany gate, not an address book.** Its own POST
  description: *"Registers a bank recipient as an intercompany counterparty for
  this customer… Only registered entries satisfy purpose=intercompany payouts."*
  Entries are compliance-reviewed (`pending_review` → `registered`), need at
  least one evidence document, and 409 on a conflicting re-registration. None of
  that is what "remember this supplier's bank details" wants.

* **A payout cannot reference a stored recipient.** `FiatPayoutDto`'s properties
  are `assetAmount, clientReferenceId, customerId, destination, documents,
  markupAmount, markupBps, purpose, virtualAccountId`, and `destination.recipient`
  is the full inline object in every branch of its `oneOf`. There is no
  `recipientId` to send.

* **The live sandbox agrees.** `https://api.sandbox.conduit.financial/v2/api-docs/
  openapi.json` (92 paths, read-only fetch 2026-08-30) carries the pinned tag list
  plus `Sandbox`, and every one of its 25 live-only paths is a `/sandbox/*`
  simulator. Nothing has appeared since the pin.

So a whitelist-gated route's "saved counterparties" *are* its registered entries
— already built, untouched here — and every free-form route
(goods/services, payroll, treasury, investments, other) gets the console-side
record this file tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import select, text

from app import counterparties, payments
from app.db import sessionmaker
from app.models import AuditEvent, Counterparty, Operation
from tests.payments_fixtures import (
    CID,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    PAYOUT,
    REGISTERED,
    SEPA_BUSINESS,
    USD_ACCOUNT,
    WHITELIST_PATH,
    encoded,
    page,
    payout_form,
)
from tests.web_harness import (
    client,
    documents_stub,
    make_app,
    minted_intent,
    post,
    signed_in,
    signed_in_as,
    stub,
    upload,
)

NEW = f"/customers/{CID}/payouts/new"
INDEX = f"/customers/{CID}/contacts"
ROUTE = "?purpose=payment_for_goods_or_services&rail=fedwire&recipientType=business&destinationCountry=USA"
OTHER = "cus_034Abbx1XrOVaY6sXUBtGX"

ACCOUNT_NUMBER = "000123456789"


def routes(requirements=FEDWIRE_BUSINESS, customer=CID, extra=None):
    return {
        ("GET", "/v2/payouts/requirements"): httpx.Response(200, json=requirements),
        ("GET", f"/v2/customers/{customer}/virtual-accounts"): page([USD_ACCOUNT]),
        ("GET", f"/v2/customers/{customer}/whitelist-recipients"): page([REGISTERED]),
        ("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT),
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


def saving(**overrides) -> bytes:
    return encoded(
        payout_form(documentIds="doc_support_1", save_counterparty="1", **overrides)
    )


def field_value(html: str, name: str) -> str | None:
    """The `value` one rendered input carries. `m.field` puts `name` and `value`
    on separate lines, so this reads the whole tag rather than a substring."""
    tag = re.search(rf'<input [^>]*name="{re.escape(name)}"[^>]*>', html)
    if tag is None:
        return None
    value = re.search(r'value="([^"]*)"', tag.group(0))
    return value.group(1) if value else None


async def store(session, customer_id=CID, label="Globex", **overrides) -> uuid.UUID:
    values = {
        "recipient": {
            "accountNumber": ACCOUNT_NUMBER,
            "routingNumber": "021000021",
            "legalName": "ZZZTEST Globex Supplies LLC",
        },
        "rail_family": "us",
        "recipient_type": "business",
        "destination_country": "USA",
        **overrides,
    }
    cp_id = await counterparties.save(
        session,
        customer_id=customer_id,
        label=label,
        actor_id="usr_1",
        actor_email="ops@example.com",
        **values,
    )
    await session.commit()
    return cp_id


# --- the decision, pinned ------------------------------------------------------------

SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "openapi_production.json").read_text()
)
RECIPIENT_WORDS = re.compile(r"recipient|counterpart|benefic|contact|payee|address.?book", re.I)


def test_the_pinned_spec_still_has_exactly_one_recipient_store():
    """The load-bearing half of this slice's design decision.

    If Conduit ever ships a real counterparty store, this table stops being the
    right answer — the recipients would belong on Conduit, shared across every
    integration, rather than in one console's database. This assertion is how
    that day announces itself: a spec re-pin that adds such a store fails here.
    """
    stores = sorted(p for p in SPEC["paths"] if RECIPIENT_WORDS.search(p))
    assert stores == [
        "/customers/{customerId}/whitelist-recipients",
        "/customers/{customerId}/whitelist-recipients/{id}",
    ]
    # And it is the intercompany gate, in Conduit's own words.
    post_op = SPEC["paths"][stores[0]]["post"]["description"]
    assert "intercompany" in post_op and "Only registered entries" in post_op
    # Keyed to the word "recipient"; the gate test below covers what that
    # cannot see.
    schemas = {n for n in SPEC["components"]["schemas"] if RECIPIENT_WORDS.search(n)}
    assert schemas == {
        "CryptoWhitelistRecipientDto",
        "UkDomesticWhitelistRecipientDto",
        "RecipientRequirementsResponseDto",
        "SepaWhitelistRecipientDto",
        "SwiftWhitelistRecipientDto",
        "UsWhitelistRecipientDto",
        "WhitelistRecipientListResponseDto",
        "WhitelistRecipientResponseDto",
    }


# Pinned as Conduit's sentence rather than as path or schema names: the second
# source it grants (`wallets/registered-addresses`) carries no recipient-shaped
# word, so the scan above cannot see it.
INTERCOMPANY_PROOF = (
    "**RECIPIENT_NOT_WHITELISTED**: purpose=intercompany requires whitelist proof "
    "for the destination: a registered intercompany whitelist entry, or a "
    "registered self-custody wallet address."
)


def test_the_intercompany_gate_still_takes_two_proofs():
    found: set[str] = set()
    for item in SPEC["paths"].values():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            for response in (operation.get("responses") or {}).values():
                for para in (response.get("description") or "").split("\n\n"):
                    if para.startswith("**RECIPIENT_NOT_WHITELISTED**"):
                        found.add(para.strip())
    assert found == {INTERCOMPANY_PROOF}


def test_a_payout_cannot_name_a_stored_recipient():
    """The other half: even with a store, `FiatPayoutDto` has no by-id form for a
    *recipient* — on every rail the recipient is the full inline object, which is
    why a saved counterparty is *pasted into* the body rather than referenced by
    it (OPERATIONS_SPEC §1).

    2026-08-31 pin refresh: Conduit wrapped the rail `oneOf` in an `anyOf` whose
    second branch is a new `{type: "virtual_account", virtualAccountId}`
    destination — a payout straight to another VA in the same organization. That
    branch names an *account*, not a stored recipient, so the claim above stands;
    the console does not send it (transfers cover the intra-org case)."""
    dto = SPEC["components"]["schemas"]["FiatPayoutDto"]
    assert set(dto["properties"]) == {
        "assetAmount",
        "clientReferenceId",
        "customerId",
        "destination",
        "documents",
        "markupAmount",
        "markupBps",
        "purpose",
        "virtualAccountId",
    }
    rails, va = SPEC["components"]["schemas"]["FiatPayoutDto"]["properties"]["destination"]["anyOf"]
    for branch in rails["oneOf"]:
        assert "recipient" in branch["properties"]
        assert branch["properties"]["recipient"].get("oneOf"), "recipient is an inline object"
    assert va["properties"]["type"]["enum"] == ["virtual_account"]
    assert "recipient" not in va["properties"], "the VA branch names an account, not a recipient"


# --- storage -------------------------------------------------------------------------


async def test_the_recipient_is_encrypted_at_rest(session):
    """Same posture as `drafts.payload` and `operations.request_body`: bank
    coordinates are PII and the database never holds them in the clear."""
    await store(session)

    raw = (await session.execute(text("select recipient from counterparties"))).scalar_one()
    assert isinstance(bytes(raw), bytes)
    assert ACCOUNT_NUMBER.encode() not in bytes(raw)
    assert b"Globex" not in bytes(raw)
    assert bytes(raw).startswith(b"gAAAAA")  # a Fernet token, not JSON

    # ...and it reads back intact through the type.
    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.recipient["accountNumber"] == ACCOUNT_NUMBER


async def test_an_unreadable_row_does_not_take_the_list_down(session):
    """One corrupt ciphertext (or a key rotated away from under a row) must cost
    that row's coordinates, not the whole customer's address book. A review
    found this class of failure on the dashboard; it does not get to reappear on
    a page that has to decrypt in order to render at all."""
    await store(session, label="Readable")
    await store(session, label="Corrupt")
    await session.execute(
        text("update counterparties set recipient = :junk where label = 'Corrupt'"),
        {"junk": b"not-a-fernet-token"},
    )
    await session.commit()

    rows = {r["label"]: r for r in await counterparties.rows(session, CID)}
    assert rows["Corrupt"]["recipient"] is None
    assert rows["Readable"]["recipient"]["accountNumber"] == ACCOUNT_NUMBER


async def test_saving_under_a_used_name_updates_rather_than_duplicating(session):
    """The chosen semantics (DESIGN.md). Always-insert is shorter and fills the
    picker with rows an operator cannot tell apart."""
    first = await store(session)
    again = await store(
        session,
        label="globex",  # same name, different case — one name in an address book
        recipient={"accountNumber": "999888777", "legalName": "ZZZTEST Globex Supplies LLC"},
    )
    assert first == again

    rows = await counterparties.rows(session, CID)
    assert len(rows) == 1
    assert rows[0]["label"] == "globex"  # the newest casing wins
    assert rows[0]["recipient"]["accountNumber"] == "999888777"


async def test_an_archived_name_stops_blocking_a_new_one(session):
    old = await store(session)
    assert await counterparties.archive(session, CID, str(old))
    await session.commit()

    new = await store(session)
    assert new != old
    assert [r["label"] for r in await counterparties.rows(session, CID)] == ["Globex"]


# --- per-customer isolation -----------------------------------------------------------


async def test_the_query_never_crosses_customers(session):
    """Query level. Every read in this module takes `customer_id` and filters on
    it — there is one place cross-customer leakage could be introduced."""
    mine = await store(session, customer_id=CID, label="Mine")
    await store(session, customer_id=OTHER, label="Theirs")

    assert [r["label"] for r in await counterparties.rows(session, CID)] == ["Mine"]
    assert [r["label"] for r in await counterparties.rows(session, OTHER)] == ["Theirs"]
    # An id that is real, but not this customer's, resolves to nothing at all —
    # the same answer a made-up id gets.
    assert await counterparties.get(session, OTHER, str(mine)) is None
    assert await counterparties.get(session, CID, str(mine)) is not None
    assert await counterparties.get(session, CID, "not-a-uuid") is None


async def test_the_list_page_never_shows_another_customers_counterparties(session):
    await store(session, customer_id=CID, label="ZZZTEST Mine Ltd")
    await store(session, customer_id=OTHER, label="ZZZTEST Theirs Ltd")

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        mine = await web.get(INDEX)
        theirs = await web.get(f"/customers/{OTHER}/contacts")

    assert "ZZZTEST Mine Ltd" in mine.text and "ZZZTEST Theirs Ltd" not in mine.text
    assert "ZZZTEST Theirs Ltd" in theirs.text and "ZZZTEST Mine Ltd" not in theirs.text


async def test_the_picker_never_offers_another_customers_counterparties(session):
    await store(session, customer_id=OTHER, label="ZZZTEST Theirs Ltd")

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    assert "ZZZTEST Theirs Ltd" not in response.text
    assert "No contact has been saved for this customer yet" in response.text


async def test_prefill_refuses_another_customers_counterparty(session):
    """Route level. A URL naming this customer and someone else's counterparty
    prefills nothing and says so — it does not silently render a blank form."""
    theirs = await store(session, customer_id=OTHER)

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{NEW}{ROUTE}&counterparty={theirs}")

    assert ACCOUNT_NUMBER not in response.text
    assert "That contact could not be used" in response.text


# --- masking --------------------------------------------------------------------------


def test_mask_keeps_four_digits_and_never_more():
    assert counterparties.mask("000123456789") == "••••6789"
    assert counterparties.mask("DE89370400440532013000") == "••••3000"
    # Nothing to keep: a short coordinate is masked whole rather than "partially"
    # shown, which would be showing it.
    assert counterparties.mask("1234") == "••••"
    assert counterparties.mask("") == "••••"
    assert counterparties.mask(None) == "••••"


async def test_the_list_shows_four_digits_and_never_the_coordinate(session):
    await store(
        session,
        recipient={
            "accountNumber": ACCOUNT_NUMBER,
            "iban": "DE89370400440532013000",
            "routingNumber": "021000021",
            "legalName": "ZZZTEST Globex Supplies LLC",
        },
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(INDEX)

    assert "••••6789" in response.text and "••••3000" in response.text
    # The whole point of the page's masking rule, asserted as an absence.
    assert ACCOUNT_NUMBER not in response.text
    assert "DE89370400440532013000" not in response.text
    # Bank routing data is not an account coordinate, and is not printed here at
    # all — there is nothing to mask because there is nothing to show.
    assert "021000021" not in response.text
    # The name is what identifies the row, and it is not a coordinate.
    assert "ZZZTEST Globex Supplies LLC" in response.text


async def test_the_picker_option_is_masked_too(session):
    await store(session)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    assert "••••6789" in response.text
    assert ACCOUNT_NUMBER not in response.text


# --- the picker: compatibility + prefill ------------------------------------------------


def test_the_rail_family_table_is_read_both_ways():
    """`family_of` is `payments.RAILS_FOR` inverted, and nothing else. A saved
    us-family destination is payable over four rails; keying the picker to the
    rail it was saved on would hide it from three of them."""
    assert payments.family_of("fedwire") == "us"
    assert payments.family_of("ACH") == "us"
    assert payments.family_of("sepa") == "sepa"
    assert payments.family_of("swift") == "swift"
    assert payments.family_of("chaps") == "uk_domestic"
    assert payments.family_of("faster_payments") == "uk_domestic"
    assert payments.family_of("moonbeam") == ""
    for family, rails in payments.RAILS_FOR.items():
        for rail in rails:
            assert payments.family_of(rail) == family


async def test_the_picker_offers_the_whole_book_whatever_the_route_says(session):
    """SUPERSEDES `test_the_picker_filters_by_family_type_and_country` and
    `test_a_sepa_route_offers_only_sepa_family_counterparties`.

    The picker used to sit UNDER the route and was filtered to what that route
    could pay — family, recipient type and country. It is step 2 now and the
    contact is what ANSWERS the route, so a filter would hide exactly the contact
    the operator came to pick: the sepa supplier is invisible on the fedwire route
    they happen to have landed on, and there is no way to get to it from here.

    The filter's job has not disappeared, it has moved into a sentence: the form
    is still built from the route, and a contact whose corridor disagrees with an
    already-answered route is SAID to disagree (the test below) rather than
    silently paired with it.
    """
    await store(session, label="ZZZTEST US Business USA")
    await store(session, label="ZZZTEST SEPA One", rail_family="sepa", destination_country="DEU")
    await store(session, label="ZZZTEST US Individual", recipient_type="individual")
    await store(session, label="ZZZTEST US Business CAN", destination_country="CAN")

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        fedwire = await web.get(NEW + ROUTE)
        # …and before any route is answered at all, which is where the contact
        # step is meant to be used from.
        bare = await web.get(NEW)

    for html in (fedwire.text, bare.text):
        for label in (
            "ZZZTEST US Business USA",
            "ZZZTEST SEPA One",
            "ZZZTEST US Individual",
            "ZZZTEST US Business CAN",
        ):
            assert label in html, label


async def test_a_contact_whose_corridor_fights_the_answered_route_says_so(session):
    """The other half of dropping the filter. The route the operator answered
    wins — it is theirs, and it is what the form below is built from — so the
    disagreement is stated instead of being quietly paired."""
    cp_id = await store(
        session,
        label="ZZZTEST SEPA One",
        rail_family="sepa",
        destination_country="DEU",
        recipient={"iban": "DE89370400440532013000", "legalName": "ZZZTEST Bonn GmbH"},
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{NEW}{ROUTE}&counterparty={cp_id}")

    body = " ".join(response.text.split())
    # Comma-joined, and the contact named: the design pass replaced a list built
    # by repeating "and a different" per item, and the box holds a UUID, so the
    # contact's label is what tells the operator which contact disagrees.
    assert "asks for a different rail family, destination country than" in body
    assert "nothing of this contact was filled in where the two disagree" in body
    # …and that clause is TRUE (gate finding m3). It used to be printed over a
    # form whose recipient boxes held the contact's coordinates: the console
    # saying one thing and doing another, on the page where the destination is
    # decided. A disagreeing corridor now prefills nothing at all — not the
    # coordinates, not the amount or currency of the last payment to it, and no
    # sentence about either.
    assert "DE89370400440532013000" not in response.text
    assert field_value(response.text, "f.destination.recipient.legalName") in (None, "")
    assert field_value(response.text, "amount") == ""
    assert "could be read back" not in body and "was USD" not in body
    # The contact is still the one picked — the note is about its values, not
    # about the choice — so the row carries it and the box still names it.
    row = response.text.split('id="route-row"')[1].split("</form>")[0]
    assert f'name="counterparty" value="{cp_id}"' in row


async def test_choosing_a_counterparty_prefills_the_recipient_fields(session):
    cp_id = await store(
        session,
        recipient={
            "accountNumber": ACCOUNT_NUMBER,
            "routingNumber": "021000021",
            "accountType": "CHECKING",
            "legalName": "ZZZTEST Globex Supplies LLC",
            "bankAddress": {"addressLine1": "270 Park Ave", "city": "New York", "country": "US"},
        },
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{NEW}{ROUTE}&counterparty={cp_id}")

    html = response.text
    # Prefilled — including a nested subtree, which is the half a flat copy loses.
    assert field_value(html, "f.destination.recipient.accountNumber") == ACCOUNT_NUMBER
    assert field_value(html, "f.destination.recipient.routingNumber") == "021000021"
    assert field_value(html, "f.destination.recipient.bankAddress.city") == "New York"
    # **Prefill, not lock.** No `readonly`/`disabled` reaches a recipient input:
    # a console-local convenience has no authority to freeze what an operator
    # typed, unlike the whitelist gate, which does.
    for match in re.finditer(r'<input [^>]*name="f\.destination\.recipient[^>]*>', html):
        assert "readonly" not in match.group(0) and "disabled" not in match.group(0)


async def test_an_edited_prefill_is_what_gets_sent(session):
    """The consequence of prefill-not-lock: what Conduit receives is the
    submission, never the stored record."""
    cp_id = await store(session)
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    counterparty=str(cp_id),
                    **{"f.destination.recipient.accountNumber": "555000111"},
                )
            ),
        )
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert sent["destination"]["recipient"]["accountNumber"] == "555000111"


async def test_an_archived_counterparty_is_gone_from_the_picker_and_from_prefill(session):
    cp_id = await store(session, label="ZZZTEST Retired Ltd")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        before = await web.get(NEW + ROUTE)
        assert "ZZZTEST Retired Ltd" in before.text

        archived = await post(web, f"{INDEX}/{cp_id}/archive")
        assert archived.headers["HX-Redirect"].startswith(INDEX)

        after = await web.get(NEW + ROUTE)
        prefill = await web.get(f"{NEW}{ROUTE}&counterparty={cp_id}")

    assert "ZZZTEST Retired Ltd" not in after.text
    assert "No contact has been saved for this customer yet" in after.text
    # And it cannot be reached by keeping the URL from before it was archived.
    assert ACCOUNT_NUMBER not in prefill.text
    assert "That contact could not be used" in prefill.text


# --- saving on acceptance ---------------------------------------------------------------


async def test_a_payout_saves_its_counterparty_on_202_acceptance(session):
    """Saved when Conduit **accepts**, not when the payout settles. Acceptance is
    the point at which these coordinates were validated; settlement is a fact
    about the payment, and waiting for it would mean saving hours later on behalf
    of an operator who has closed the tab."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(web, NEW, saving(counterparty_label="ZZZTEST Globex"))

    # The payout was accepted (202) and is nowhere near settled...
    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.state == "confirmed" and op.conduit_resource_id == PAYOUT["id"]

    # ...and the counterparty is already on file.
    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.label == "ZZZTEST Globex" and row.customer_id == CID
    assert row.rail_family == "us" and row.recipient_type == "business"
    assert row.destination_country == "USA"
    assert row.recipient["accountNumber"] == ACCOUNT_NUMBER
    assert row.recipient["bankAddress"]["city"] == "New York"
    # The *payment's* facts are not the counterparty's: an invoice reference
    # belongs to one payout, not to whoever is being paid.
    assert "remittance" not in row.recipient and "rail" not in row.recipient


async def test_saving_is_audited_and_is_not_an_operations_row(session):
    """OPERATIONS_SPEC §1's new paragraph, asserted: a counterparty save is a
    plain audited write, because it mutates nothing at Conduit."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving())

    types = (await session.execute(select(Operation.type))).scalars().all()
    # The upload is ledgered (it is a Conduit mutation); the save is not. That
    # contrast is the assertion — no `counterparty_*` operation exists.
    assert types == ["document_upload", "payout_create"]
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalar_one()
    assert event.actor_email == "ops@example.com"
    assert event.detail["customer"] == CID and event.detail["rail_family"] == "us"


async def test_nothing_is_saved_unless_the_box_is_ticked(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await post(web, NEW, encoded(payout_form(documentIds="doc_1")))
    assert (await session.execute(select(Counterparty))).scalars().all() == []


async def test_the_label_falls_back_to_the_recipients_legal_name(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving(counterparty_label=""))
    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.label == "ZZZTEST Globex Supplies LLC"


async def test_a_rejected_payout_saves_nothing(session):
    """Acceptance is the trigger; a refusal is not one. `DOCUMENTATION_REQUIRED`
    means no payout was created, and there is nothing Conduit validated."""
    refusal = httpx.Response(
        422,
        json={
            "type": "DOCUMENTATION_REQUIRED",
            "title": "A document is required",
            "detail": "Attach an invoice.",
        },
    )
    app = make_app(stub(routes(extra={("POST", "/v2/payouts"): refusal})))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(web, NEW, saving())

    assert response.status_code == 422
    assert (await session.execute(select(Counterparty))).scalars().all() == []
    # ...and the tick survives the re-render, so fixing the form does not
    # silently drop the operator's intent to save.
    assert 'name="save_counterparty" value="1" checked' in response.text


# --- a save that takes over a live contact says so -------------------------------
#
# `save` upserts on `(customer, lower(label))`, and the payout form's help text
# discloses it: saving under a name you already use *is* a correction of that
# contact (DESIGN.md, "Two semantics worth stating"). What the trail did not say
# was **which** contact and what its coordinates had been. That absence is the
# hole: an operator holding `payout.create` and not `contact.edit` can point an
# existing name at their own account from the payout form, and once
# `OP_BODY_RETENTION` purges the operation body holding the old coordinates, the
# console has no record of what the name used to reach. The record is the fix —
# the upsert is not being taken away, because the form says it happens.

VICTIM_ACCOUNT = "000555444333"


async def occupied(session, label="ZZZTEST Acme Payroll") -> uuid.UUID:
    """A live contact somebody else created, holding somebody else's account."""
    cp_id = await counterparties.save(
        session,
        customer_id=CID,
        label=label,
        recipient={
            "accountNumber": VICTIM_ACCOUNT,
            "routingNumber": "021000021",  # unchanged by the takeover, so undiffed
            "legalName": "ZZZTEST Acme Payroll Services LLC",
            "bankAddress": {"city": "Chicago", "country": "US"},
        },
        rail_family="us",
        recipient_type="business",
        destination_country="USA",
        actor_id="usr_finance",
        actor_email="finance@example.com",
    )
    await session.commit()
    return cp_id


async def test_a_save_cannot_read_a_label_a_concurrent_rename_is_taking():
    label = "ZZZTEST Contested Name"
    async with sessionmaker()() as owner:
        victim = await occupied(owner, label="ZZZTEST Original Name")

    async with sessionmaker()() as renamer, sessionmaker()() as saver:
        assert await counterparties.rename(renamer, CID, str(victim), label) == ""

        reading = asyncio.create_task(counterparties.live_by_label(saver, CID, label))
        answered, _ = await asyncio.wait({reading}, timeout=2)
        assert not answered, "the save read the label as free while a rename was taking it"

        await renamer.commit()
        before = await asyncio.wait_for(reading, timeout=10)
        assert before is not None
        assert before["id"] == victim
        await saver.rollback()


async def test_two_saves_creating_the_same_new_label_cannot_both_read_it_as_free():
    """The half a row lock could not reach: a label **nobody** holds yet.

    `live_by_label` takes `FOR UPDATE`, which orders every save against an
    *existing* contact. It could order nothing against a contact that does not
    exist, so two payout submits creating the same new label both read it free,
    the loser's upsert quietly updated the winner's fresh row, and its audit
    recorded a create — the one detail shape that tells a reader nothing was
    overwritten. `hold_label` locks the conflict target instead of the row, so
    the key exists before the row does.

    Two sessions, therefore two connections: the claim is about transactions
    racing, and one session cannot block on itself.
    """
    label = "ZZZTEST Contested Name"
    coordinates = dict(
        recipient={"accountNumber": "000111222333", "legalName": "ZZZTEST First LLC"},
        rail_family="us",
        recipient_type="business",
        destination_country="USA",
    )

    async with sessionmaker()() as first, sessionmaker()() as second:
        assert await counterparties.live_by_label(first, CID, label) is None
        winner = await counterparties.save(
            first,
            customer_id=CID,
            label=label,
            actor_id="usr_first",
            actor_email="first@example.com",
            **coordinates,
        )

        # Uncommitted, so `second` cannot see the row by any means — the only
        # thing that can hold it back is the lock on the name itself.
        reading = asyncio.create_task(counterparties.live_by_label(second, CID, label))
        answered, _ = await asyncio.wait({reading}, timeout=2)
        assert not answered, "the second save read the label as free while the first held it"

        await first.commit()
        before = await asyncio.wait_for(reading, timeout=10)
        # Not None is the whole assertion: the loser now knows it is overwriting
        # a contact, so its audit records the takeover it is about to perform.
        assert before is not None
        assert before["id"] == winner
        await second.rollback()


async def test_a_save_that_replaces_a_live_contact_records_what_it_overwrote(session):
    """The attack, and the row that has to outlive the operation body.

    `payout.create` alone, an existing label, and the operator's own account
    underneath it. The contact keeps its id, its name and its `created_by`, so
    nothing on the Contacts page says a thing happened — which is precisely why
    the audit row has to, in `counterparty.edited`'s own masked shape.
    """
    replaced = await occupied(session)

    app = make_app(stub(routes()))
    # `contact.edit` is deliberately absent: this operator may send payouts and
    # attach their paperwork, and may not touch the address book — which is the
    # boundary the save walks around.
    async with signed_in_as(app, "payout.create", "document.upload") as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(web, NEW, saving(counterparty_label="ZZZTEST Acme Payroll"))
        assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"

        row = (
            await session.execute(
                select(Counterparty).execution_options(populate_existing=True)
            )
        ).scalar_one()
        # One row, still the victim's, now pointed at the payout's account — and
        # still attributed to the operator who created it.
        assert row.id == replaced
        assert row.recipient["accountNumber"] == ACCOUNT_NUMBER
        assert row.created_by_actor_email == "finance@example.com"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "counterparty.save")
            )
        ).scalar_one()
        # The id is the thread back to what was overwritten after the operation
        # body is purged; the diff is what it used to reach.
        assert event.detail.get("replaced") == str(replaced), event.detail
        assert event.detail.get("identity") == {
            "accountNumber": "••••4333→••••6789",
            "legalName": "ZZZTEST Acme Payroll Services LLC→ZZZTEST Globex Supplies LLC",
        }, event.detail
        # Everything else that moved, by name only — a bank address is not a
        # thing an audit row prints (`counterparties.changed_keys`).
        assert "bankAddress.city" in event.detail.get("fields", []), event.detail
        # This console's one masking policy, on a durable record: neither
        # account number appears in full anywhere in the row.
        assert VICTIM_ACCOUNT not in json.dumps(event.detail)
        assert ACCOUNT_NUMBER not in json.dumps(event.detail)

        # And it reads back where an operator would look for it: the contact's
        # own history drawer already renders `detail.identity` for any action.
        panel = await web.get(f"{INDEX}/{replaced}/history")

    assert "••••4333→••••6789" in panel.text


async def test_a_takeover_that_re_spells_the_name_records_the_spelling_it_replaced(session):
    """`lower(label)` is the conflict target, so a save under a different casing
    takes over the same contact **and** rewrites how its name is spelled — the
    one part of a takeover that is visible on the Contacts page, and the one the
    operator who typed it did not necessarily mean. Recorded under
    `counterparty.edited`'s own word for it, which the history drawer already
    prints as "from X to Y"."""
    replaced = await occupied(session)

    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.create", "document.upload") as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving(counterparty_label="zzztest acme payroll"))

    row = (
        await session.execute(
            select(Counterparty).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.id == replaced and row.label == "zzztest acme payroll"
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalar_one()
    assert event.detail["replaced"] == str(replaced)
    assert event.detail["renamed_from"] == "ZZZTEST Acme Payroll"


async def test_a_save_that_creates_a_contact_records_no_diff(session):
    """The other half. A free name replaced nothing, and a before/after here
    would be inventing a contact that never existed — `counterparties.diff`
    refuses to fabricate an absence for the same reason on the repair path."""
    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.create", "document.upload") as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving(counterparty_label="ZZZTEST Nobody Owns This"))

    saved = (await session.execute(select(Counterparty))).scalar_one()
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalar_one()
    assert event.detail["counterparty"] == str(saved.id)
    assert "replaced" not in event.detail
    assert "identity" not in event.detail and "fields" not in event.detail


async def test_an_archived_namesake_is_not_something_a_save_replaced(session):
    """An archived row never blocks a new one of the same name (the partial
    index is over the live rows), so the save that reuses a retired name inserts
    — and must not claim to have overwritten the contact it did not touch."""
    retired = await occupied(session)
    assert await counterparties.archive(session, CID, str(retired))
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.create", "document.upload") as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving(counterparty_label="ZZZTEST Acme Payroll"))

    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalar_one()
    assert event.detail["counterparty"] != str(retired)
    assert "replaced" not in event.detail and "identity" not in event.detail
    # The retired row still holds what it always held.
    old = (
        await session.execute(
            select(Counterparty)
            .where(Counterparty.id == retired)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert old.recipient["accountNumber"] == VICTIM_ACCOUNT


async def test_a_whitelist_gated_route_offers_neither_picker_nor_save(session):
    """The routes Conduit *does* have a store for keep that store, unchanged. A
    second, unreviewed list beside the registered entries would be offering a
    payout Conduit refuses."""
    await store(session, label="ZZZTEST Console Book Ltd")
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        response = await web.get(
            NEW + "?purpose=intercompany&rail=fedwire&recipientType=business"
            "&destinationCountry=USA"
        )

    assert "Use saved contact" not in response.text
    assert "Save as contact" not in response.text
    assert "ZZZTEST Console Book Ltd" not in response.text
    # Conduit's own picker is still there, untouched.
    assert 'name="whitelistRecipientId"' in response.text
    assert REGISTERED["legalName"] in response.text


async def test_a_gated_payout_saves_nothing_even_if_the_field_is_forged(session):
    """A hand-built body carrying `save_counterparty` on a gated route is refused
    by the route, not by the absence of a checkbox in the HTML."""
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        await post(
            web,
            NEW,
            encoded(
                payout_form(
                    purpose="intercompany",
                    whitelistRecipientId=REGISTERED["id"],
                    save_counterparty="1",
                    counterparty_label="ZZZTEST Forged",
                )
            ),
        )
    assert (await session.execute(select(Counterparty))).scalars().all() == []


# --- management page --------------------------------------------------------------------


async def test_renaming_a_counterparty(session):
    cp_id = await store(session, label="ZZZTEST Old Name")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, f"{INDEX}/{cp_id}/rename", encoded({"label": "ZZZTEST New Name"}))

    assert "Contact+renamed" in response.headers["HX-Redirect"]
    assert [r["label"] for r in await counterparties.rows(session, CID)] == ["ZZZTEST New Name"]
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.rename"))
    ).scalar_one()
    assert event.detail["label"] == "ZZZTEST New Name"


@pytest.mark.parametrize(
    "label, expected",
    [("", "needs a name"), ("  ", "needs a name"), ("Taken", "already has a contact")],
)
async def test_a_rename_that_cannot_stand_is_refused_with_a_sentence(session, label, expected):
    await store(session, label="Taken")
    cp_id = await store(session, label="ZZZTEST Renameable")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, f"{INDEX}/{cp_id}/rename", encoded({"label": label}))
    assert expected in response.headers["HX-Redirect"].replace("+", " ").replace("%27", "'")
    assert {r["label"] for r in await counterparties.rows(session, CID)} == {
        "Taken",
        "ZZZTEST Renameable",
    }


async def test_archiving_is_a_soft_delete(session):
    cp_id = await store(session)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await post(web, f"{INDEX}/{cp_id}/archive")

    assert await counterparties.rows(session, CID) == []
    # Still on file, behind the payouts that named it.
    survived = (await session.execute(select(Counterparty))).scalar_one()
    assert survived.id == cp_id and survived.archived_at is not None
    assert (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.archive"))
    ).scalar_one() is not None


async def test_archiving_another_customers_counterparty_is_refused(session):
    theirs = await store(session, customer_id=OTHER)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, f"{INDEX}/{theirs}/archive")

    assert "No+such+contact" in response.headers["HX-Redirect"]
    still = (await session.execute(select(Counterparty))).scalar_one()
    assert still.archived_at is None


async def test_the_empty_list_names_the_action_that_fills_it(session):
    # No saved contacts AND no registrations: the empty state is about both
    # halves of the merged list, so both have to be empty for it to appear.
    app = make_app(stub(routes(extra={("GET", WHITELIST_PATH): page([])})))
    async with signed_in(app) as web:
        response = await web.get(INDEX)
    assert "No contacts for this customer yet" in response.text
    assert f'href="/customers/{CID}/payouts/new"' in response.text


# --- roles and CSRF ----------------------------------------------------------------------


async def test_a_viewer_reads_the_list_but_cannot_change_it(session):
    cp_id = await store(session, label="ZZZTEST Readable Ltd")
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        listing = await web.get(INDEX)
        archive = await post(web, f"{INDEX}/{cp_id}/archive")
        rename = await post(web, f"{INDEX}/{cp_id}/rename", encoded({"label": "ZZZTEST Nope"}))

    assert listing.status_code == 200
    assert "ZZZTEST Readable Ltd" in listing.text
    assert "••••6789" in listing.text  # masked for a viewer too
    # The contacts page names one permission per gated action.
    assert "<code>contact.archive</code>" in listing.text
    assert 'class="danger">Archive' not in listing.text
    assert archive.status_code == 403 and rename.status_code == 403
    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.label == "ZZZTEST Readable Ltd" and row.archived_at is None


async def test_a_viewer_cannot_save_a_counterparty(session):
    """The save rides on `POST payouts/new`, which is already operator-only — so
    the guard is the payout's, and this pins that it stays that way."""
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(web, NEW, saving())
    assert response.status_code == 403
    assert (await session.execute(select(Counterparty))).scalars().all() == []


async def test_the_mutations_need_a_csrf_token(session):
    cp_id = await store(session)
    app = make_app(stub(routes()))
    async with client(app) as web:
        await web.get(INDEX)  # take the cookie, then post without the header
        for url, body in (
            (f"{INDEX}/{cp_id}/archive", b""),
            (f"{INDEX}/{cp_id}/rename", encoded({"label": "ZZZTEST Nope"})),
        ):
            response = await web.post(
                url,
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403, url

    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.label == "Globex" and row.archived_at is None


# --- which counterparty a payout used --------------------------------
#
# The mechanism is the operation's **audit detail**, not a column: the trail
# already carries an `operation_id` FK for exactly this, `convert.py` already
# reads an operation's console-local facts back off it, and a label recorded at
# the time is what the payout was addressed from even if the counterparty is
# later renamed or archived. Label only — a payment page is not an address book.

TX_DETAIL = f"/transactions/{PAYOUT['id']}"


def with_transaction(extra=None):
    return routes(extra={("GET", f"/v2/transactions/{PAYOUT['id']}"): httpx.Response(
        200, json=PAYOUT), **(extra or {})})


async def test_a_payout_that_used_a_saved_counterparty_names_it_on_its_operation(session):
    cp_id = await store(session, label="ZZZTEST Globex Supplies")
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        sent = await post(web, NEW, encoded(payout_form(documentIds="doc_1",
                                                        counterparty=str(cp_id))))
        assert sent.headers["HX-Redirect"] == TX_DETAIL
        page_html = (await web.get(TX_DETAIL)).text

    assert "<th>Contact</th>" in page_html and "ZZZTEST Globex Supplies" in page_html
    # The label, never the coordinates — masked or otherwise.
    assert ACCOUNT_NUMBER not in page_html and "••••" not in page_html
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.used"))
    ).scalar_one()
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert event.operation_id == op.id and event.detail["counterparty"] == str(cp_id)
    assert "accountNumber" not in json.dumps(event.detail)


async def test_a_payout_that_saved_a_counterparty_names_it_too(session):
    """The save runs after the 202, so its audit row is the one that carries the
    name the destination ended up stored under."""
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, saving(counterparty_label="ZZZTEST Newly Saved"))
        page_html = (await web.get(TX_DETAIL)).text

    assert "<th>Contact</th>" in page_html and "ZZZTEST Newly Saved" in page_html
    event = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalar_one()
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert event.operation_id == op.id
    # The id `save` returned, under the key `counterparties.attached` reads
    # Without it this panel could name the
    # contact it had just created and not link to it — while a payout that
    # merely *used* a saved contact could, which is the same panel behaving two
    # ways for no reason an operator could see.
    saved = (
        await session.execute(select(Counterparty).where(Counterparty.label == "ZZZTEST Newly Saved"))
    ).scalar_one()
    assert event.detail["counterparty"] == str(saved.id)
    assert f'href="/customers/{CID}/contacts#contact-{saved.id}"' in page_html
    # Still the label and the id only: never a coordinate, masked or otherwise.
    assert "accountNumber" not in json.dumps(event.detail)


async def test_the_operation_page_names_the_counterparty_too(session):
    """Panel parity. `/operations/{id}` renders the same panel as the
    transaction page and was the one copy of it that never filled the row in —
    so following the operation link lost the fact."""
    cp_id = await store(session, label="ZZZTEST Globex Supplies")
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(web, NEW, encoded(payout_form(documentIds="doc_1", counterparty=str(cp_id))))
        op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
        page_html = (await web.get(f"/operations/{op.id}")).text

    assert "<th>Contact</th>" in page_html and "ZZZTEST Globex Supplies" in page_html
    assert ACCOUNT_NUMBER not in page_html and "••••" not in page_html


async def test_an_operation_with_no_counterparty_shows_no_row(session):
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(web, NEW, encoded(payout_form(documentIds="doc_1")))
        op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
        page_html = (await web.get(f"/operations/{op.id}")).text
    assert "<th>Contact</th>" not in page_html


async def test_a_hand_typed_destination_shows_no_counterparty_row(session):
    """Absent, not an em-dash: a payout addressed by hand did not use one."""
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await post(web, NEW, encoded(payout_form(documentIds="doc_1")))
        page_html = (await web.get(TX_DETAIL)).text
    assert "<th>Contact</th>" not in page_html


async def test_a_replayed_submit_does_not_record_a_second_use(session):
    """The double-submit guard resolves to the same operation, and using one
    counterparty once is not using it twice."""
    cp_id = await store(session)
    app = make_app(stub(with_transaction()))
    body = encoded(payout_form(documentIds="doc_1", counterparty=str(cp_id), intent=minted_intent()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(web, NEW, body)
        await post(web, NEW, body)

    ops = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalars().all()
    assert len(ops) == 1, "the intent nonce should have resolved the replay to one operation"
    used = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.used"))
    ).scalars().all()
    assert len(used) == 1


# --- regressions on the whitelist split ------------------------------------------


async def test_a_replayed_submit_never_re_saves_the_counterparty(session):
    """HIGH. The save used to run on `op.state == "confirmed"` alone, so any
    resubmission the intent nonce resolved to an already-terminal operation
    re-ran it with the *old* body — overwriting, under the same label, whatever
    that destination had legitimately been re-saved as since. A replay confirms
    nothing new and must store nothing new.
    """
    app = make_app(stub(with_transaction()))
    body = saving(counterparty_label="ZZZTEST Supplier", intent=minted_intent())
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(web, NEW, body)
        first = (await session.execute(select(Counterparty))).scalar_one()
        stored, saved_at = dict(first.recipient), first.updated_at

        # The destination is then re-saved under the same label with different
        # coordinates — the newer truth the replay must not roll back.
        await counterparties.save(
            session,
            customer_id=CID,
            label="ZZZTEST Supplier",
            recipient={**stored, "accountNumber": "000999888777"},
            rail_family="us",
            recipient_type="business",
            destination_country="USA",
            actor_id="usr_2",
            actor_email="other@example.com",
        )
        await session.commit()

        replay = await post(web, NEW, body)

    assert replay.headers["HX-Redirect"] == TX_DETAIL  # the operator still lands right
    rows = (
        await session.execute(
            select(Counterparty).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.recipient["accountNumber"] == "000999888777", "the replay overwrote a newer save"
    assert row.updated_at >= saved_at
    saves = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "counterparty.save"))
    ).scalars().all()
    assert len(saves) == 1


async def test_a_forged_counterparty_id_never_earns_a_used_row(session):
    """MEDIUM. The hidden id says which record was *offered*; the body says where
    the money went. A submission naming a saved counterparty but carrying
    different coordinates did not use it, and the operation panel must not say it
    did (`USE_ACTIONS` does not read the weaker action back).
    """
    cp_id = await store(session, label="ZZZTEST Globex Supplies")
    app = make_app(stub(with_transaction()))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    counterparty=str(cp_id),
                    **{"f.destination.recipient.accountNumber": "000000000001"},
                )
            ),
        )
        page_html = (await web.get(TX_DETAIL)).text

    actions = [
        e.action
        for e in (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action.like("counterparty.%"))
            )
        ).scalars()
    ]
    assert actions == ["counterparty.modified_prefill"]
    assert "<th>Contact</th>" not in page_html
    # …and the honest case still earns the real row.
    assert counterparties.same_destination(
        {"accountNumber": ACCOUNT_NUMBER, "routingNumber": "021000021",
         "legalName": "ZZZTEST Globex Supplies LLC"},
        {"accountNumber": ACCOUNT_NUMBER, "routingNumber": "021000021",
         "legalName": "ZZZTEST Globex Supplies LLC"},
    )


async def test_an_unreadable_counterparty_refuses_politely_and_can_still_be_managed(session):
    """MEDIUM. `get` selected the ORM entity, so `EncryptedJSON`'s result
    processor ran inside the query and one corrupt or key-rotated row 500ed the
    prefill, the rename and the archive — while `rows()` had tolerated exactly
    that row all along. Renaming and archiving never needed the coordinates.
    """
    cp_id = await store(session, label="ZZZTEST Corrupt")
    await session.execute(
        text("update counterparties set recipient = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": cp_id},
    )
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        prefill = await web.get(f"{NEW}{ROUTE}&counterparty={cp_id}")
        renamed = await post(web, f"{INDEX}/{cp_id}/rename", encoded({"label": "ZZZTEST Renamed"}))
        listing = await web.get(INDEX)
        archived = await post(web, f"{INDEX}/{cp_id}/archive", b"")

    assert prefill.status_code == 200
    assert "That contact could not be used" in prefill.text
    assert "unreadable" in prefill.text
    assert "Contact renamed." in unquote_plus(renamed.headers["HX-Redirect"])
    assert listing.status_code == 200 and "ZZZTEST Renamed" in listing.text
    assert "archived" in unquote_plus(archived.headers["HX-Redirect"])
    # Read the columns, not the entity: loading `recipient` is exactly what this
    # row cannot survive, which is the whole point of the fix.
    row = (
        await session.execute(select(Counterparty.label, Counterparty.archived_at))
    ).one()
    assert row.label == "ZZZTEST Renamed" and row.archived_at is not None


async def test_the_registered_destination_pickers_never_print_a_whole_account(session):
    """HIGH. Conduit's whitelist entries rendered in full on three surfaces while
    this console's own address book had masked the same fields
    — so the payout form was a better place to harvest an account number
    than the page written to protect them.

    Two surfaces now rather than three: the transfers screen stopped rendering
    whitelist entries (it moves money between Conduit accounts and
    names no bank coordinate at all), so the third loop entry would have asserted
    nothing about masking — a vacuous pin. The rule it held is unweakened: the
    payout picker below IS the surface that inherited that screen's registered-
    destination list, and `tests/test_web_transfers.py` pins the transfers screen
    printing no coordinate keys at all.
    """
    entry = {**REGISTERED, "iban": "DE89370400440532013000", "bic": "COBADEFFXXX"}
    app = make_app(
        stub(
            routes(
                FEDWIRE_INTERCOMPANY,
                extra={("GET", WHITELIST_PATH): page([entry])},
            )
        )
    )
    async with signed_in(app) as web:
        picker = await web.get(NEW + "?purpose=intercompany&rail=fedwire"
                                     "&recipientType=business&destinationCountry=USA")
        contacts = await web.get(f"/customers/{CID}/contacts")

    for name, html in (("payout picker", picker.text), ("contacts list", contacts.text)):
        assert entry["accountNumber"] not in html, f"{name} printed a whole account number"
        assert entry["iban"] not in html, f"{name} printed a whole IBAN"
        # Still identifiable: last four, the legal name and the rail.
        assert "••••" in html, f"{name} lost the masked coordinate"
        assert entry["legalName"] in html, f"{name} lost the name"
    # An ABA is public routing data and identifies the bank, not the account —
    # it stays whole, which is the policy `counterparties.MASKED_KEYS` states.
    # An ABA is public routing data and identifies the bank, not the account. It
    # is the *fallback* identity on Contacts (`counterparties.identify`'s own
    # policy: masked account coordinates, else the public bank identifier), so an
    # entry that states an account number shows that instead — which is what this
    # one does. The masked assertions above are the load-bearing half.


def test_the_sweep_script_refuses_a_database_that_is_not_disposable():
    """HIGH. `DATABASE_URL` is read with `setdefault`, so an inherited DSN wins —
    and this script deletes rows. `--check-config` runs the two refusals and
    exits before anything else, which is the only way to prove a guard fires
    without running the script it guards.
    """
    script = Path(__file__).parent / "e2e" / "07_counterparties.py"
    sweep = "postgresql+psycopg://mc_bot@/conduit_console_sweep?host=/tmp"

    def run(url: str):
        # Fake sandbox credentials so the key/host refusals pass and the
        # database refusal is what fires — hermetic: no `../.env` needed (CI
        # has none), and no dependence on a real key anywhere.
        return subprocess.run(
            [sys.executable, str(script), "--check-config"],
            env={
                **os.environ,
                "DATABASE_URL": url,
                "CONDUIT_SANDBOX_API_KEY": "ck_sandbox_fake_for_check_config",
                "CONDUIT_SANDBOX_HOST": "https://api.sandbox.conduit.financial",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

    refused = run("postgresql+psycopg://mc_bot@/conduit_console?host=/tmp")
    assert refused.returncode != 0
    assert "not a disposable database" in refused.stdout + refused.stderr
    assert run(sweep).returncode == 0
