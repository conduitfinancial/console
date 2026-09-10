""""Sent to contact X" on the ledger.

`GET /v2/transactions` has no name- or recipient-shaped filter — the design says
so and `test_the_ledger_endpoint_still_has_no_recipient_filter` keeps saying it
against the pin. So the filter is built out of **this console's own trail**, and
everything worth testing here is about the boundary of what that trail can prove:

* a `counterparty.used` audit row is a match — the operator picked the contact
  *and* what was submitted was still it (`same_destination` decided that before
  the row was written);
* `counterparty.modified_prefill` is **not** — the truth-in-audit
  decision: the prefill was edited, so the payment did not go to the saved
  contact;
* `counterparty.save` is not either, and for a mechanical reason worth pinning:
  its detail records a renameable *label*, never the contact id;
* a whitelisted (`intercompany`) payout is a match only where the registered
  entry Conduit paid *is* a saved contact by `counterparties.merge` — the
  Contacts page's own join;
* and no contact of one customer may ever surface another customer's payments.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import QueryParams

from app import audit, counterparties, operations
from app.models import AuditEvent, Operation
from tests.payments_fixtures import (
    CID,
    FEDWIRE_INTERCOMPANY,
    OTHER_CID,
    PAYOUT,
    PENDING,
    REGISTERED,
    REVOKED,
    USD_ACCOUNT,
    WHITELIST_PATH,
    encoded,
    page,
    payout_form,
)
from tests.test_counterparties import ACCOUNT_NUMBER, store
from tests.web_harness import documents_stub, make_app, post, signed_in, stub

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "contracts" / "openapi_production.json").read_text())

LIST = "/transactions"
WITHDRAWALS = f"{LIST}?type=withdrawal&customerId={CID}"
CONTACTS = f"/customers/{CID}/contacts"
PAYOUT_NEW = f"/customers/{CID}/payouts/new"


def routes(items=(PAYOUT,), details=(PAYOUT,), extra=None):
    return {
        ("GET", "/v2/transactions"): page(list(items)),
        ("GET", "/v2/customers"): page([{"id": CID, "legalName": "ZZZTEST Ltd"}]),
        **{("GET", f"/v2/transactions/{d['id']}"): httpx.Response(200, json=d) for d in details},
        **(extra or {}),
    }


async def used(session, contact_id, *, transaction: str, customer_id: str = CID, action=None):
    """One payout operation that named a contact, exactly as the payout route
    leaves it: the operation carries the transaction Conduit created, and the
    audit row against it carries the contact.
    """
    op, _ = await operations.start(
        session,
        type="payout_create",
        actor_id="usr_1",
        actor_email="ops@example.com",
        path="/v2/payouts",
        body={"nonce": str(uuid.uuid4())},
        customer_id=customer_id,
    )
    op.conduit_resource_id = transaction
    op.state = "confirmed"
    audit.record(
        session,
        action=action or counterparties.USED_ACTION,
        actor_id="usr_1",
        actor_email="ops@example.com",
        operation_id=op.id,
        detail={"counterparty": str(contact_id), "label": "Globex"},
    )
    await session.commit()
    return op


def tx(transaction_id: str, **overrides) -> dict:
    return {**PAYOUT, "id": transaction_id, "hasRfi": False, **overrides}


# --- the trail, and only what it proves ----------------------------------------------


async def test_a_used_row_is_a_match_and_a_modified_prefill_is_not(session):
    """The matrix's centre. Both rows sit against a real operation with a real
    transaction; the only difference is the action, and that difference is the
    decision that a payment sent on an edited prefill did not go to the
    saved contact.
    """
    contact = await store(session)
    await used(session, contact, transaction="txn_used")
    await used(
        session, contact, transaction="txn_edited", action="counterparty.modified_prefill"
    )

    found = await counterparties.linked_transactions(
        session, customer_id=CID, contact_id=str(contact), limit=25
    )
    assert found == ["txn_used"]


async def test_a_save_row_is_not_a_match_even_though_it_now_records_an_id(session):
    """The exclusion that needs proving rather than asserting — and it is the
    **action** that excludes it, not an accident of what the detail happens to
    hold.

    That distinction stopped being theoretical: `counterparty.save`
    now records the id it stored, so the operation panel can link to the contact
    a payout created instead of merely naming it. The trail's boundary is
    unmoved, because it never rested on the absent id: the payout that *first
    saved* a contact typed its destination, it did not pick it, and "sent to
    contact X" is a claim about picking. The query filters on `USED_ACTION`,
    which is why widening the detail could not widen the filter.
    """
    contact = await store(session)
    await used(session, contact, transaction="txn_saved", action="counterparty.save")
    assert (
        await counterparties.linked_transactions(
            session, customer_id=CID, contact_id=str(contact), limit=25
        )
        == []
    )
    # The id really is in the row that was just excluded — so this test would
    # fail the day the query started matching on the detail alone.
    saved = (
        await session.execute(
            select(AuditEvent.detail).where(AuditEvent.action == "counterparty.save")
        )
    ).scalar_one()
    assert saved["counterparty"] == str(contact)
    # That the saver writes the id is pinned where it is a *behaviour* rather
    # than a source string — `tests/test_counterparties.py` asserts the audit
    # detail and the rendered contact link on a real payout. A grep of
    # payouts.py used to stand here, pinning the exact dict literal that this
    # change edited; a test that breaks when a comment is added is pinning the
    # wrong thing.


async def test_an_operation_that_created_no_transaction_is_not_a_match(session):
    """A rejected payout — `DOCUMENTATION_REQUIRED`, say — leaves the audit row
    (it is written before the send, on purpose) and no `conduit_resource_id`.
    There is no ledger row to show, so there is none shown."""
    contact = await store(session)
    op = await used(session, contact, transaction="txn_gone")
    op.conduit_resource_id = None
    op.state = "rejected"
    await session.commit()

    assert (
        await counterparties.linked_transactions(
            session, customer_id=CID, contact_id=str(contact), limit=25
        )
        == []
    )


async def test_a_contact_never_reaches_another_customers_transactions(session):
    """Cross-customer isolation, at the query. The audit row names the contact
    and nothing else — so the customer predicate is what stops a contact id from
    dragging another customer's payment onto the page.
    """
    contact = await store(session)
    await used(session, contact, transaction="txn_ours")
    await used(session, contact, transaction="txn_theirs", customer_id=OTHER_CID)

    assert await counterparties.linked_transactions(
        session, customer_id=CID, contact_id=str(contact), limit=25
    ) == ["txn_ours"]
    assert (
        await counterparties.linked_transactions(
            session, customer_id=OTHER_CID, contact_id=str(contact), limit=25
        )
        == ["txn_theirs"]
    )


async def test_newest_first_by_the_trails_own_clock(session):
    contact = await store(session)
    for name in ("txn_1", "txn_2", "txn_3"):
        await used(session, contact, transaction=name)
    found = await counterparties.linked_transactions(
        session, customer_id=CID, contact_id=str(contact), limit=25
    )
    assert found == ["txn_3", "txn_2", "txn_1"]


async def test_the_query_refuses_an_empty_scope(session):
    assert (
        await counterparties.linked_transactions(
            session, customer_id="", contact_id="x", limit=25
        )
        == []
    )
    assert (
        await counterparties.linked_transactions(
            session, customer_id=CID, contact_id="", limit=25
        )
        == []
    )


# --- the ledger page -----------------------------------------------------------------


async def test_the_ledger_endpoint_still_has_no_recipient_filter():
    """Why this feature is built from the trail at all. A spec re-pin that adds a
    recipient filter to the transaction list should fail here rather than leave
    the console resolving locally what the endpoint could answer."""
    names = {
        p.get("name", "")
        for p in SPEC["paths"]["/transactions"]["get"].get("parameters", [])
    }
    assert not {
        n for n in names if re.search(r"recipient|counterpart|contact|payee|benefic", n, re.I)
    }


async def test_the_field_is_disabled_without_a_customer_and_says_why():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?type=withdrawal")
    assert '<select name="contact" disabled>' in response.text
    assert "pick a customer on the <em>Withdrawal</em> tab" in response.text


async def test_the_field_is_disabled_off_the_withdrawal_tab(session):
    """A contact's payments are payouts, and a payout lands on `withdrawal`. On
    any other tab the filter could only ever return nothing."""
    await store(session)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?type=deposit&customerId={CID}")
    assert '<select name="contact" disabled>' in response.text
    # And the options are not even read: there is nothing to offer.
    assert "Globex" not in response.text


async def test_the_picker_offers_this_customers_contacts(session):
    await store(session, label="Globex")
    await store(session, customer_id=OTHER_CID, label="Someone Else")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(WITHDRAWALS)
    assert '<select name="contact" >' in response.text or 'name="contact"' in response.text
    assert "Globex" in response.text and "Someone Else" not in response.text


async def test_the_filtered_view_reads_the_trails_ids_and_makes_no_list_request(session):
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    calls: list = []
    app = make_app(stub(routes(details=(tx("txn_a"),)), calls))
    async with signed_in(app) as web:
        response = await web.get(f"{WITHDRAWALS}&contact={contact}")

    paths = [c[1] for c in calls]
    assert "/v2/transactions" not in paths, "a contact-filtered view is not a list query"
    assert "/v2/transactions/txn_a" in paths
    assert "txn_a" in response.text


async def test_conduits_own_filters_are_dropped_rather_than_ignored(session):
    """The honesty rule for a filter that cannot be applied: it is not carried.
    `list_query` drops them, the tray renders them disabled, and the pager's URLs
    do not smuggle them back in."""
    from app.web import transactions as module

    query = QueryParams(
        f"type=withdrawal&customerId={CID}&contact=abc&status=pending"
        "&createdAfter=2026-01-01&externalReference=EXT-1&clientReferenceId=REF-1"
    )
    wire = module.list_query(query)
    assert wire["customerId"] == CID and wire["type"] == "withdrawal"
    assert wire["status"] is None
    assert wire["createdAfter"] is None and wire["externalReference"] is None
    assert wire["clientReferenceId"] is None
    # …and with no contact, nothing changes about the existing view.
    kept = module.list_query(QueryParams(f"type=withdrawal&customerId={CID}&status=pending"))
    assert kept["status"] == ["pending"]

    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    app = make_app(stub(routes(details=(tx("txn_a"),))))
    async with signed_in(app) as web:
        response = await web.get(
            f"{WITHDRAWALS}&contact={contact}&status=pending&externalReference=EXT-1"
        )
    assert 'name="status" disabled' in response.text
    # The tray shows them empty and switched off, so a re-submit cannot carry
    # them either — the value the URL still holds is applied by nothing.
    assert 'name="externalReference" value="" disabled' in response.text
    assert 'name="createdAfter" value="" disabled' in response.text
    assert 'name="clientReferenceId" value=""' in response.text
    assert "<option value=\"pending\" selected>" not in response.text
    # And the pager's own URLs carry the contact and the scope, nothing else.
    assert f"/transactions?type=withdrawal&amp;customerId={CID}&amp;contact={contact}" in response.text
    # The Export CSV href is the page's raw query string, so a stale
    # `status=` an operator hand-typed still rides in it. Harmless and pinned as
    # such: `list_query` drops it again inside the export, so the file, its
    # name and its audit row never mention it — asserted in
    # `test_the_export_drops_what_the_page_dropped`.


async def test_the_boundary_is_stated_while_the_filter_is_on(session):
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    app = make_app(stub(routes(details=(tx("txn_a"),))))
    async with signed_in(app) as web:
        on = await web.get(f"{WITHDRAWALS}&contact={contact}")
        off = await web.get(WITHDRAWALS)

    for sentence in (
        "made outside this console",
        "before this contact existed",
        "edited</em> prefill",
        "typed rather than picked",
    ):
        assert sentence in on.text, sentence
    assert "Showing 1 console-linked payout" in on.text
    assert "by when this console sent them" in on.text
    # Off: the ledger says exactly what it said before this slice.
    assert "Showing 1 transaction" in off.text and "console-linked" not in off.text


async def test_an_unreadable_linked_transaction_is_a_row_not_a_silence(session):
    """The one thing this filter must never do is make a contact's payment
    history quietly shorter than it is."""
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    await used(session, contact, transaction="txn_missing")
    app = make_app(
        stub(
            routes(
                details=(tx("txn_a"),),
                extra={
                    ("GET", "/v2/transactions/txn_missing"): httpx.Response(
                        503, json={"type": "SERVER_ERROR", "title": "down"}
                    )
                },
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"{WITHDRAWALS}&contact={contact}")
    assert "txn_missing" in response.text and "could not be read" in response.text
    assert "Showing 2 console-linked payouts" in response.text


async def test_a_contact_of_another_customer_is_refused_not_quietly_dropped(session):
    """The `/contacts` rule: a filter that cannot be answered is refused, never
    answered with the unfiltered list dressed as an answer."""
    theirs = await store(session, customer_id=OTHER_CID, label="Theirs")
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(f"{WITHDRAWALS}&contact={theirs}")
    assert "No such contact" in response.text
    assert "/v2/transactions" not in [c[1] for c in calls]
    assert "txn_payout_1" not in response.text


async def test_the_filtered_view_pages_by_offset(session):
    contact = await store(session)
    for name in ("txn_1", "txn_2", "txn_3"):
        await used(session, contact, transaction=name)
    app = make_app(
        stub(routes(details=(tx("txn_1"), tx("txn_2"), tx("txn_3"))))
    )
    async with signed_in(app) as web:
        first = await web.get(f"{WITHDRAWALS}&contact={contact}&limit=25")
        # The rows-per-page control is real here too; two pages of one.
        one = await web.get(f"{WITHDRAWALS}&contact={contact}&limit=25&offset=2")

    assert "One page — this is all of it." in first.text
    assert "txn_3" in first.text and "txn_1" in first.text
    # An offset past the first two ids shows the third and offers a way back —
    # the local list's equivalent of a cursor page turn.
    assert "txn_1" in one.text and "txn_3" not in one.text
    assert "Previous" in one.text and f"offset=0" not in one.text


async def test_the_pager_offers_a_next_only_when_there_is_one(session):
    contact = await store(session)
    for name in ("txn_1", "txn_2", "txn_3"):
        await used(session, contact, transaction=name)
    app = make_app(stub(routes(details=(tx("txn_1"), tx("txn_2"), tx("txn_3")))))
    async with signed_in(app) as web:
        response = await web.get(f"{WITHDRAWALS}&contact={contact}&limit=25")
        # `limit` is a whitelist, so the small page is forced through the URL the
        # size control itself builds.
        assert "Next" not in response.text
    # Three rows, two per page is not a size the control offers — so the +1
    # idiom is exercised at the query instead, which is where it lives.
    ids = await counterparties.linked_transactions(
        session, customer_id=CID, contact_id=str(contact), limit=3, offset=0
    )
    assert len(ids) == 3
    assert (
        await counterparties.linked_transactions(
            session, customer_id=CID, contact_id=str(contact), limit=3, offset=2
        )
        == ["txn_1"]
    )


async def test_a_viewer_may_filter_by_contact(session):
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    app = make_app(stub(routes(details=(tx("txn_a"),))))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(f"{WITHDRAWALS}&contact={contact}")
    assert response.status_code == 200 and "txn_a" in response.text


# --- the whitelisted arm (moved off the transfers screen) --------------------
#
# These four used to POST the transfers screen: that screen sent
# the `intercompany` payout a registered whitelist entry gates, and it was the
# only route that wrote `counterparty.used` with `via: "whitelist"`. Transfers
# now move money between Conduit accounts on the virtual-account arm, where there
# is no whitelist entry to link to a contact — so the CAPABILITY moved to the
# payout page's `intercompany` route, which is where an intercompany payout to a
# bank destination lives, and these tests moved with it. Nothing about the rule
# changed: the match is `counterparties.merge`, the row is written on a new
# operation only, and no saved twin means no row.


def whitelisted_routes(recipients=None, extra=None):
    return {
        ("GET", "/v2/payouts/requirements"): httpx.Response(200, json=FEDWIRE_INTERCOMPANY),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
        ("GET", WHITELIST_PATH): page(
            recipients if recipients is not None else [REGISTERED, PENDING, REVOKED]
        ),
        ("POST", "/v2/documents"): documents_stub,
        ("POST", "/v2/payouts"): httpx.Response(202, json={"id": "txn_transfer"}),
        ("GET", "/v2/transactions/txn_transfer"): httpx.Response(
            200, json=tx("txn_transfer", hasRfi=False)
        ),
        **(extra or {}),
    }


def whitelisted_form(**overrides):
    """The intercompany route's submission: the whitelist gate strips the
    recipient's identity fields, so what is sent names the entry and nothing
    else about the destination."""
    body = payout_form(
        purpose="intercompany",
        whitelistRecipientId=REGISTERED["id"],
        **{
            "f.destination.recipient.accountNumber": "",
            "f.destination.recipient.routingNumber": "",
            "f.destination.recipient.legalName": "",
        },
    )
    body.update(overrides)
    return encoded({k: v for k, v in body.items() if v != ""})


async def test_a_payout_to_a_saved_contacts_registered_twin_is_linked(session):
    """The whitelisted half of the requirement. Nothing on this path records a
    contact otherwise: the picked entry's id never reaches the body, and
    `request_body` is purged on the §6 retention schedule — so the audit row is
    the durable link, and the match is `counterparties.merge` itself.
    """
    contact = await store(session)  # same coordinates as REGISTERED
    app = make_app(stub(whitelisted_routes()))
    async with signed_in(app) as web:
        response = await post(web, PAYOUT_NEW, whitelisted_form())
    assert response.status_code in (204, 302, 303), response.text

    row = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == counterparties.USED_ACTION)
        )
    ).scalar_one()
    assert row.detail["counterparty"] == str(contact)
    assert row.detail["via"] == "whitelist"
    # And it is a match on the ledger, through the same query the free-form arm uses.
    assert await counterparties.linked_transactions(
        session, customer_id=CID, contact_id=str(contact), limit=25
    ) == ["txn_transfer"]


async def test_a_payout_to_a_registration_nothing_is_saved_for_links_nothing(session):
    """No saved twin, no row: a whitelist entry this console holds no contact for
    is a destination the ledger can say nothing about."""
    app = make_app(stub(whitelisted_routes()))
    async with signed_in(app) as web:
        response = await post(web, PAYOUT_NEW, whitelisted_form())
    assert response.status_code in (204, 302, 303), response.text
    assert (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == counterparties.USED_ACTION)
        )
    ).first() is None


async def test_a_near_miss_contact_is_not_linked_by_a_whitelisted_payout(session):
    """`merge`'s own rule, arriving here unchanged: one digit different is
    another account, whatever the legal name says."""
    await store(
        session,
        label="Nearly",
        recipient={
            "accountNumber": ACCOUNT_NUMBER[:-1] + "0",
            "routingNumber": REGISTERED["routingNumber"],
            "legalName": REGISTERED["legalName"],
        },
    )
    app = make_app(stub(whitelisted_routes()))
    async with signed_in(app) as web:
        await post(web, PAYOUT_NEW, whitelisted_form())
    assert (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == counterparties.USED_ACTION)
        )
    ).first() is None


async def test_a_failed_twin_lookup_never_strands_the_payment(session, monkeypatch):
    """The contact link is bookkeeping; the payout is money.

    The twin lookup used to sit **between** `operations.start` — which has
    already committed a `created` row — and `execute_operation`. A transient
    failure in that window 500s the request with the operation stranded
    `created`: the operator's retry resolves by intent to that dead row,
    `is_new` is False, execution is skipped, and the payment quietly does not
    happen until the TTL abandons it (OPERATIONS_SPEC §2). Money-safe, and a
    lost payment.

    So the read happens **before** any operation exists, where a failure is just
    a failed request and the retry is clean. Both directions are asserted: the
    failing attempt creates no operation and sends nothing, and the retry that
    follows it pays exactly once.
    """
    await store(session)  # same coordinates as REGISTERED — a twin exists to find
    calls: list = []
    app = make_app(stub(whitelisted_routes(), calls))

    async def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("the contacts read failed")

    monkeypatch.setattr(counterparties, "rows", unavailable)
    async with signed_in(app) as web:
        with pytest.raises(SQLAlchemyError):
            await post(web, PAYOUT_NEW, whitelisted_form())

        # Nothing was started, so nothing is stranded and nothing is on the wire.
        assert (
            await session.execute(
                select(Operation).where(Operation.type == "payout_create")
            )
        ).first() is None
        assert [c for c in calls if c[1] == "/v2/payouts"] == []

        monkeypatch.undo()
        retry = await post(web, PAYOUT_NEW, whitelisted_form())

    assert retry.status_code in (204, 302, 303), retry.text
    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 1
    operation = (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).scalar_one()
    assert operation.state == "confirmed"


async def test_the_whitelisted_contact_row_survives_onto_the_transaction_page(session):
    contact = await store(session)
    app = make_app(stub(whitelisted_routes()))
    async with signed_in(app) as web:
        await post(web, PAYOUT_NEW, whitelisted_form())
        detail = await web.get("/transactions/txn_transfer")
    assert "<th>Contact</th>" in detail.text
    assert f"/customers/{CID}/contacts#contact-{contact}" in detail.text


# --- both directions -----------------------------------------------------------------


async def test_the_contacts_page_links_to_the_filtered_ledger(session):
    contact = await store(session)
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): page([]),
                ("GET", "/v2/customers"): page([{"id": CID, "legalName": "ZZZTEST Ltd"}]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(CONTACTS)
    assert (
        f'href="/transactions?type=withdrawal&customerId={CID}&contact={contact}"'
        in response.text
    )
    assert "Payments to this contact" in response.text
    # The anchor the operation panel's Contact row points at.
    assert f'id="contact-{contact}"' in response.text


# --- the export ----------------------------------------------------------------------


async def test_the_export_applies_the_filter_and_names_it(session):
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    app = make_app(
        stub(
            {
                ("GET", "/v2/transactions"): page([tx("txn_a"), tx("txn_b")]),
                ("GET", "/v2/customers"): page([]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={CID}&contact={contact}"
        )
    body = response.text
    assert "txn_a" in body and "txn_b" not in body
    assert f"contact-{contact}" in response.headers["content-disposition"]
    # A filename token names the filter; it does not say what the filter leaves
    # out. The boundary the page states goes in the file too (review addendum).
    assert "payouts this console sent and recorded this contact on" in body
    assert "edited prefill" in body and "first saved the contact" in body

    row = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "export.csv")
        )
    ).scalar_one()
    assert row.detail["filters"]["contact"] == str(contact)
    assert row.detail["rows"] == 1


async def test_the_export_drops_what_the_page_dropped(session):
    """One parser, so the file cannot apply a filter the page did not — even
    when the URL still carries one."""
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    app = make_app(
        stub(
            {
                ("GET", "/v2/transactions"): page([tx("txn_a")]),
                ("GET", "/v2/customers"): page([]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={CID}"
            f"&contact={contact}&status=pending&externalReference=EXT-1"
        )
    assert "txn_a" in response.text
    assert "status" not in response.headers["content-disposition"]
    row = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "export.csv"))
    ).scalar_one()
    assert set(row.detail["filters"]) == {"type", "customerId", "contact"}


async def test_the_export_refuses_a_contact_it_cannot_scope(session):
    theirs = await store(session, customer_id=OTHER_CID, label="Theirs")
    app = make_app(stub({("GET", "/v2/transactions"): page([]), ("GET", "/v2/customers"): page([])}))
    async with signed_in(app) as web:
        response = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={CID}&contact={theirs}"
        )
    assert response.status_code == 400
    assert "silently be the unfiltered view" in response.text


# --- the slice-1 design-gate findings -------------------------------------------------


async def test_a_terminal_registration_drops_the_gerund(session):
    """"Whitelisting" describes a registration that is still happening. On a
    `revoked` or `rejected` entry nothing is in progress, so the status pill
    speaks alone."""
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): page([REVOKED, PENDING]),
                ("GET", "/v2/customers"): page([]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(CONTACTS)
    # One in-flight entry keeps the word; the terminal one does not add a second.
    assert response.text.count(">Whitelisting<") == 1
    assert "revoked" in response.text


async def test_the_archive_consequence_states_the_dual_capability_branch(session):
    await store(session)
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): page([REGISTERED]),
                ("GET", "/v2/customers"): page([]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(CONTACTS)
    assert "unless it is also whitelisted" in response.text
    assert "This row stays: its Conduit registration is not touched." in response.text
    # The old, false sentence is gone.
    assert "Archiving removes a contact from every payout picker and from this list" not in (
        response.text
    )


# --- the follow-up pass -------------------------------------------------------


async def test_the_export_is_driven_by_the_trail_not_by_intersecting_a_walk(session):
    """Intersecting the trail's ids with a walk of Conduit's list can
    only ever return what the walk happened to contain — so a linked transaction
    the list does not answer for was silently absent from the file while the HTML
    showed it as an unreadable row. The file is driven by the same ids the page
    is, and says so when one of them cannot be read.
    """
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    await used(session, contact, transaction="txn_missing")
    calls: list = []
    app = make_app(
        stub(
            {
                # A list read would answer with neither of them — the file must
                # not be built from it.
                ("GET", "/v2/transactions"): page([tx("txn_unrelated")]),
                ("GET", "/v2/customers"): page([]),
                ("GET", "/v2/transactions/txn_a"): httpx.Response(200, json=tx("txn_a")),
                ("GET", "/v2/transactions/txn_missing"): httpx.Response(
                    503, json={"type": "SERVER_ERROR", "title": "down"}
                ),
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={CID}&contact={contact}"
        )
    body = response.text
    assert "txn_a" in body and "txn_unrelated" not in body
    assert "txn_missing" in body, "a linked transaction is never silently absent"
    assert "# EXPORT INCOMPLETE" in body
    assert "/v2/transactions" not in [c[1] for c in calls], "no list walk at all"

    row = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "export.csv"))
    ).scalar_one()
    assert row.detail["rows"] == 2


async def test_an_archived_contact_still_answers_a_bookmarked_filter(session):
    """Archiving retires the picker entry; the payments that named the
    contact are history and do not stop existing."""
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    assert await counterparties.archive(session, CID, str(contact))
    await session.commit()

    app = make_app(stub(routes(details=(tx("txn_a"),))))
    async with signed_in(app) as web:
        html = (await web.get(f"{WITHDRAWALS}&contact={contact}")).text

    assert "No such contact" not in html
    assert "txn_a" in html
    assert "archived" in html
    # The picker itself stays live-only: an archived contact is not offered.
    assert f'<option value="{contact}"' not in html


async def test_the_archived_contacts_export_still_works(session):
    contact = await store(session)
    await used(session, contact, transaction="txn_a")
    assert await counterparties.archive(session, CID, str(contact))
    await session.commit()
    app = make_app(
        stub(
            {
                ("GET", "/v2/customers"): page([]),
                ("GET", "/v2/transactions/txn_a"): httpx.Response(200, json=tx("txn_a")),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(
            f"/export/transactions.csv?type=withdrawal&customerId={CID}&contact={contact}"
        )
    assert response.status_code == 200 and "txn_a" in response.text
