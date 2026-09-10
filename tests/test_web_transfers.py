"""The transfers screen: one payout on Conduit's **virtual-account** destination
arm.

`{"type": "virtual_account", "virtualAccountId": …, "remittance": …}` — no rail,
no recipient, no whitelist. Four things are load-bearing and each has a test
here: the body carries the VA branch and *nothing* of the bank branch; both
pickers are names-first; the destination list separates "none in this currency"
from "none at all" from "could not be read"; and the currency label and balances
follow the source selection rather than a render-time copy of it.

The whitelist mechanism this screen used to implement did not disappear — it
moved to the payout page's `intercompany` route, and its tests moved with it
(`tests/test_web_payouts.py`).
"""

from __future__ import annotations

import json
import re
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import select

from app import payments
from app.models import Operation
from tests.conftest import settings_override
from tests.payments_fixtures import (
    CID,
    EUR_ACTIVE,
    OTHER_CID,
    PAYOUT,
    USD_ACCOUNT,
    VID,
    encoded,
    page,
)
from tests.web_harness import (
    documents_stub,
    intent,
    make_app,
    minted_intent,
    post,
    signed_in,
    stub,
    upload,
)

NEW = f"/customers/{CID}/transfers/new"

# The destination customer's own accounts — distinct ids, so "source" and
# "destination" can never be the same row by accident.
DEST_USD = {**USD_ACCOUNT, "id": "vac_dest0000000000000usd"}
DEST_EUR = {**EUR_ACTIVE, "id": "vac_dest0000000000000eur"}
DEST_ACCOUNTS = f"/v2/customers/{OTHER_CID}/virtual-accounts"

NAMES = [
    {"id": CID, "legalName": "ZZZTEST Console EOOD"},
    {"id": OTHER_CID, "legalName": "ZZZTEST Globex Supplies LLC"},
]


def routes(accounts=None, destination=None, extra=None):
    return {
        ("GET", "/v2/customers"): page(NAMES),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page(
            accounts if accounts is not None else [USD_ACCOUNT, EUR_ACTIVE]
        ),
        ("GET", DEST_ACCOUNTS): page(
            destination if destination is not None else [DEST_USD, DEST_EUR]
        ),
        # An attachment has to be in this console's own upload ledger before it
        # can ride `documents[]` (`documents.attachable`).
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


PICKED = f"?virtualAccountId={VID}&destination={OTHER_CID}"


def flat(response) -> str:
    """The page with its wrapping collapsed, so an assertion is about the
    sentence rather than about where Jinja happened to break the line."""
    return " ".join(response.text.split())


def transfer_form(**overrides):
    """A complete transfer submission, as the browser sends it: the route form's
    three controls (`hx-include`) plus the amount and the optional remittance."""
    body = {
        "virtualAccountId": VID,
        "destination": OTHER_CID,
        "destinationVirtualAccountId": DEST_USD["id"],
        "amount": "250.00",
        "remittanceReference": "ZZZTEST-TRANSFER",
    }
    body.update(overrides)
    return encoded({k: v for k, v in body.items() if v != ""})


# --- the builder ---------------------------------------------------------------------------


def test_the_body_is_the_virtual_account_branch_and_nothing_of_the_other_one():
    """`FiatPayoutDto.destination.anyOf[1]`: `additionalProperties: false`,
    `required: [type, virtualAccountId]`. A `rail` or a `recipient` beside a
    `virtualAccountId` is two branches of one `oneOf` at once — so the assertion
    is on their ABSENCE, not merely on the presence of the right keys."""
    body = payments.virtual_account_body(
        customer_id=CID,
        virtual_account_id=VID,
        destination_account_id=DEST_USD["id"],
        asset="USD",
        amount_text="250.00",
        reference="INV-1",
        description="August supplies",
    )
    assert body["destination"] == {
        "type": "virtual_account",
        "virtualAccountId": DEST_USD["id"],
        "remittance": {"reference": "INV-1", "description": "August supplies"},
    }
    assert body["customerId"] == CID and body["virtualAccountId"] == VID
    assert body["assetAmount"] == {"code": "USD", "amount": "250.00"}
    # Still `intercompany`: the DTO requires a purpose and this arm has exactly one.
    assert body["purpose"] == payments.TRANSFER_PURPOSE
    # The bank branch's keys, nowhere at any depth.
    wire = json.dumps(body)
    for absent in ("rail", "recipient", "accountNumber", "routingNumber", "iban", "bic"):
        assert absent not in wire, absent


def test_a_transfer_into_the_source_account_is_unrepresentable():
    """Refused by the route with a sentence an operator reads; refused *here* so
    that "a transfer into itself" cannot be built at all."""
    for destination in (VID, ""):
        with pytest.raises(ValueError):
            payments.virtual_account_body(
                customer_id=CID,
                virtual_account_id=VID,
                destination_account_id=destination,
                asset="USD",
                amount_text="1.00",
            )


def test_remittance_is_capped_at_the_dtos_own_lengths_and_omitted_when_empty():
    assert payments.remittance("", "  ") == {}
    capped = payments.remittance("r" * 200, "d" * 400)
    assert len(capped["reference"]) == 140 and len(capped["description"]) == 280
    assert payments.remittance("only-a-ref") == {"reference": "only-a-ref"}
    # An empty remittance is not sent at all: `additionalProperties: false` makes
    # a speculative key a refusal rather than a no-op.
    body = payments.virtual_account_body(
        customer_id=CID,
        virtual_account_id=VID,
        destination_account_id=DEST_USD["id"],
        asset="USD",
        amount_text="1.00",
    )
    assert "remittance" not in body["destination"]


def test_the_maxlengths_on_the_form_are_the_dtos_own():
    """Stated as `maxlength` attributes, not only as a server-side truncation —
    the operator finds out at the keyboard, not after a submit."""
    assert dict(payments.REMITTANCE_LIMITS) == {"reference": 140, "description": 280}


# --- the pickers ----------------------------------------------------------------------------


async def test_both_pickers_are_names_first():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)

    # Source: the owning customer, named, beside the account it is — and the id
    # demoted, never replaced (names-over-ids).
    assert "ZZZTEST Console EOOD" in response.text and VID in response.text
    # Destination: the customer picker is a datalist of names over ids, and the
    # account options carry that customer's name too.
    assert 'list="known-customers"' in response.text
    assert response.text.count("ZZZTEST Globex Supplies LLC") >= 2
    assert DEST_USD["id"] in response.text


async def test_one_bounded_customers_read_per_render_never_one_per_row():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await web.get(NEW + PICKED)
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1


async def test_a_cross_currency_destination_is_offered_disabled_with_the_reason():
    """Disabled, not filtered: an account that vanished from a picker is an
    account the operator goes looking for (the payout page's rule for the same
    situation). The reason is on the option itself."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)

    assert DEST_EUR["id"] in response.text
    assert "holds EUR, not USD" in response.text
    # ...and the same-currency one is offered live.
    row = next(
        line for line in response.text.splitlines() if DEST_USD["id"] in line and "option" in line
    )
    assert "disabled" not in row


async def test_the_source_account_is_offered_disabled_as_its_own_destination():
    """Destination ≠ source, enforced where the operator can see it as well as at
    the builder."""
    app = make_app(
        stub(routes(extra={("GET", DEST_ACCOUNTS): page([USD_ACCOUNT, DEST_USD])}))
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    assert "this is the source account" in response.text


# --- the three empty states, paired both ways ------------------------------------------------


async def test_a_destination_with_no_account_in_this_currency_says_which_fact_it_is():
    """The sharpest of the three: they have accounts, just none in USD."""
    app = make_app(stub(routes(destination=[DEST_EUR])))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    assert "has no active <strong>USD</strong> account" in flat(response)
    assert "has no <em>active</em> virtual account" not in response.text
    assert "could not be read" not in response.text


async def test_a_destination_with_no_active_account_at_all_says_that_instead():
    app = make_app(stub(routes(destination=[])))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    assert "has no <em>active</em> virtual account" in response.text
    assert "has no active <strong>USD</strong> account" not in flat(response)


async def test_an_unreadable_destination_list_is_never_rendered_as_none():
    """The half of the pair that matters: "we could not ask" and "there are none"
    are different facts about a client's money."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", DEST_ACCOUNTS): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Accounts down"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    assert "could not be read" in response.text
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    assert "has no <em>active</em> virtual account" not in response.text
    assert "has no active <strong>USD</strong> account" not in flat(response)


async def test_the_convert_handoff_is_offered_only_between_one_customers_own_accounts():
    """A cross-customer cross-currency move has no console flow — converting THIS
    customer's money does not create an account over there — so that case states
    what is true and stops. Pointed at Convert only where the destination is this
    customer's own set of accounts, which is the case Convert actually serves."""
    theirs = make_app(stub(routes(destination=[DEST_EUR])))
    async with signed_in(theirs) as web:
        cross_customer = await web.get(NEW + PICKED)
    # `?destination=` naming this same customer: the only active USD account is
    # the source itself, and their other money is in EUR — that IS a conversion.
    own = make_app(stub(routes()))
    async with signed_in(own) as web:
        same_customer = await web.get(NEW + f"?virtualAccountId={VID}&destination={CID}")

    assert "has no active <strong>USD</strong> account" in flat(cross_customer)
    assert "/convert?" not in cross_customer.text

    assert "the one you are transferring" in same_customer.text
    # `source=` is not decoration: Convert's `_pick` reads it and preselects the
    # account the money is stuck in (test_web_convert.py pins the reading side).
    assert f"/customers/{CID}/convert?source={VID}" in same_customer.text


async def test_an_unreadable_source_account_list_is_not_no_active_account():
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", f"/v2/customers/{CID}/virtual-accounts"): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Accounts down"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW)
    assert "no active virtual account" not in response.text
    assert "virtual accounts could not be read" in response.text
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3


async def test_an_empty_source_account_list_still_says_there_are_none():
    app = make_app(stub(routes(accounts=[])))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    assert "no active virtual account" in response.text
    assert "virtual accounts could not be read" not in response.text


# --- the selection is never a render-time copy ------------------------------------------------


async def test_the_currency_label_and_the_balances_follow_the_source_selection():
    """The bug this form was rebuilt around: the select did not re-render, so a
    USD selection showed the EUR account's balances under an "Amount (EUR)"
    label. Fixed structurally — the select refetches — so this pins BOTH
    selections rather than only the one that was wrong."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        usd = await web.get(NEW + f"?virtualAccountId={VID}")
        eur = await web.get(NEW + f"?virtualAccountId={EUR_ACTIVE['id']}")

    assert "Amount (USD)" in usd.text and "125000.00" in usd.text
    assert "Amount (EUR)" not in usd.text
    assert "Amount (EUR)" in eur.text and "125000.00" not in eur.text
    assert "Amount (USD)" not in eur.text


async def test_the_source_select_refetches_the_page_rather_than_copying_it():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    # The source CUSTOMER is on the same trigger, for the same reason
    # the other two are on it: everything the server re-reads for this page hangs
    # off one of the three, and a render-time copy of any of it goes stale the
    # moment the control moves.
    assert (
        'hx-trigger="change from:#customer_id, change from:#virtualAccountId, '
        'change from:#destination"' in response.text
    )
    assert 'hx-sync="this:replace"' in response.text
    # JavaScript off: the same form is a plain GET with a button.
    assert 'method="get"' in response.text and ">Reload<" in response.text


# --- no discovery call ------------------------------------------------------------------------


async def test_the_screen_never_asks_for_payout_requirements():
    """The arm is outside `GET /v2/payouts/requirements` — `rail` is a required
    parameter there and has no `virtual_account` value — so the call is not made
    and no `acceptedDocumentTypes` is claimed."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
        await post(web, NEW, transfer_form(destinationVirtualAccountId=DEST_EUR["id"]))
    assert [c for c in calls if "requirements" in c[1]] == []
    assert "Attach a supporting document (optional)" in response.text
    assert "Accepted:" not in response.text


# --- the sandbox limitation, retired -----------------------------------------------------------


SANDBOX_NOTE = "does not settle yet"


async def test_the_retired_sandbox_note_stays_gone():
    """The note was true for the arm's first hours (accept-then-fail,
    `rail_unavailable`) and came off the evening sandbox began settling it
    (2026-09-01 ~20:00 UTC; evidence: sandbox_evidence/va_transfer_settled.json
    and the receiving deposit beside it). A resurrected note would be a warning
    about something that is not happening — the exact dishonesty the note
    existed to avoid, pointing the other way. `tests/e2e/10_va_transfer_probe.py`
    is the sentinel that says if reality changes back (exit 3)."""
    for env in ("sandbox", "production"):
        with settings_override(conduit_env=env):
            app = make_app(stub(routes()))
            async with signed_in(app) as web:
                response = await web.get(NEW + PICKED)
        assert SANDBOX_NOTE not in response.text, env
        assert "rail_unavailable" not in response.text, env
        # The transfer is offered, note or no note.
        assert 'id="transfer-submit"' in response.text, env


# --- what is sent ------------------------------------------------------------------------------


async def test_a_transfer_sends_the_virtual_account_body_and_lands_on_its_transaction(session):
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json={"id": "txn_t1"})}),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form())

    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/payouts"))
    assert sent["destination"] == {
        "type": "virtual_account",
        "virtualAccountId": DEST_USD["id"],
        "remittance": {"reference": "ZZZTEST-TRANSFER"},
    }
    assert sent["purpose"] == "intercompany"
    assert sent["assetAmount"] == {"code": "USD", "amount": "250.00"}
    assert sent["virtualAccountId"] == VID
    assert response.headers["HX-Redirect"] == "/transactions/txn_t1"

    op = (await session.execute(select(Operation))).scalars().one()
    # The wire call is `POST /v2/payouts`, so the reconciler's `payout_create`
    # recipe applies to this arm unchanged (OPERATIONS_SPEC §3).
    assert op.type == "payout_create" and op.state == "confirmed"
    assert op.request_path == "/v2/payouts"


async def test_the_destination_account_is_verified_against_conduit_not_the_browser():
    """The select is a convenience; a destination account is a place money goes."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web, NEW, transfer_form(destinationVirtualAccountId="vac_not_theirs00000000")
        )
    assert response.status_code == 422
    assert "not one of this customer&#39;s active virtual accounts" in flat(response)
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_cross_currency_transfer_is_refused_before_the_ledger_row(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web, NEW, transfer_form(destinationVirtualAccountId=DEST_EUR["id"])
        )
    assert response.status_code == 422
    assert "Transfers are same-currency" in response.text
    assert "PAYOUT_DESTINATION_CURRENCY_MISMATCH" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_transfer_into_the_source_account_is_refused():
    calls: list = []
    app = make_app(
        stub(routes(extra={("GET", DEST_ACCOUNTS): page([USD_ACCOUNT])}), calls)
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form(destinationVirtualAccountId=VID))
    assert response.status_code == 422
    assert "not the source account" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_an_unreadable_destination_list_refuses_rather_than_sending_unverified():
    """The same refusal `payments.resolve_recipient` makes about an unreadable
    whitelist: the destination was not verified, so nothing is sent."""
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", DEST_ACCOUNTS): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Accounts down"}
                    )
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form())
    assert response.status_code == 422
    assert "was not verified" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_an_amount_that_is_not_a_positive_decimal_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form(amount="-1"))
    assert response.status_code == 422 and "positive decimal" in response.text


@pytest.mark.parametrize(
    ("ceiling", "amount", "sent"),
    [
        ("", "250.00", True),        # unset: today's behaviour, nothing refuses
        ("250.00", "250.00", True),  # AT the ceiling is allowed
        ("250.00", "250.01", False),  # one minimum unit over is not
        ("250.00", "10000.00", False),
    ],
)
async def test_the_money_ceiling_refuses_a_transfer_before_the_ledger(ceiling, amount, sent):
    """The money ceiling, on the arm that does not go through `payout_errors`.
    The refusal is an ordinary form error and it lands BEFORE `operations.start`:
    a refused submit is not an operation, and nothing reaches Conduit."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    with settings_override(money_ceiling=ceiling):
        async with signed_in(app) as web:
            response = await post(web, NEW, transfer_form(amount=amount))
    payouts = [c for c in calls if c[1] == "/v2/payouts"]
    if sent:
        assert payouts, response.text[:400]
    else:
        assert response.status_code == 422
        assert "refuses any single amount over 250.00" in flat(response)
        assert payouts == []


async def test_a_refused_transfer_writes_no_operation_row(session):
    """The ceiling's whole promise: the operations ledger never sees it."""
    app = make_app(stub(routes()))
    with settings_override(money_ceiling="10.00"):
        async with signed_in(app) as web:
            response = await post(web, NEW, transfer_form(amount="500.00"))
    assert response.status_code == 422
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_remittance_over_the_cap_is_truncated_to_the_dtos_length():
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await post(
            web,
            NEW,
            transfer_form(remittanceReference="r" * 300, remittanceDescription="d" * 400),
        )
    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/payouts"))
    note = sent["destination"]["remittance"]
    assert len(note["reference"]) == 140 and len(note["description"]) == 280


# --- Conduit's own refusals, and the accept-then-fail path --------------------------------------


async def test_a_conduit_422_renders_on_the_form(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", "/v2/payouts"): httpx.Response(
                        422,
                        json={
                            "type": "PAYOUT_DESTINATION_CURRENCY_MISMATCH",
                            "title": "Destination currency mismatch",
                            "detail": "The destination account holds a different currency.",
                            "correlationId": "corr_vac",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form())
    assert response.status_code == 422
    assert "The destination account holds a different currency" in response.text  # A3
    assert "corr_vac" in response.text
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.state == "rejected"


async def test_an_ambiguous_transfer_goes_to_its_operation_page(session):
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(504, json={"type": "TIMEOUT"})}))
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form())
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.state == "outcome_unknown"
    assert response.headers["HX-Redirect"] == f"/operations/{op.id}"


async def test_an_accepted_then_failed_transfer_renders_conduits_own_outcome():
    """The accept-then-fail path — sandbox's behaviour for this arm's first
    hours, and still a real outcome the docs describe (two accounts that cannot
    be connected fail after acceptance, funds released). The transaction page
    already renders `failureCode`/`failureMessage`, so this arm needs no new
    code to be honest about it."""
    failed = {
        **PAYOUT,
        "id": "txn_t1",
        "status": "failed",
        "failureCode": "rail_unavailable",
        "failureMessage": "No viable payment rail was available. No funds were moved.",
    }
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", "/v2/payouts"): httpx.Response(202, json={"id": "txn_t1"}),
                    ("GET", "/v2/transactions/txn_t1"): httpx.Response(200, json=failed),
                }
            )
        )
    )
    async with signed_in(app) as web:
        sent = await post(web, NEW, transfer_form())
        detail = await web.get(sent.headers["HX-Redirect"])
    assert "rail_unavailable" in detail.text
    assert "No funds were moved" in detail.text


async def test_a_deposit_from_an_internal_transfer_renders_what_conduit_sent():
    """The receiving side, from this screen's end of the round trip: a transfer
    sent here lands as the other customer's deposit and the link back to the
    sending transaction is on that page.

    It used to be a raw generic row — this build had no typed view for
    `source.type = internal_transfer`. The receiving leg became observable
    2026-09-01 and the row is now typed (label, raw enum, link); the drift pin
    against the live capture lives in `tests/test_web_transactions.py`."""
    received = {
        "id": "txn_in_1",
        "type": "deposit",
        "status": "completed",
        "customerId": OTHER_CID,
        "hasRfi": False,
        "fees": [],
        "source": {
            "type": "internal_transfer",
            "originatingTransactionId": "txn_t1",
            "assetAmount": {"code": "USD", "amount": "250.00"},
        },
        "destination": {
            "type": "virtual_account",
            "virtualAccountId": DEST_USD["id"],
            "assetAmount": {"code": "USD", "amount": "250.00"},
        },
        "createdAt": "2026-09-01T10:00:00.000Z",
    }
    app = make_app(
        stub(routes(extra={("GET", "/v2/transactions/txn_in_1"): httpx.Response(200, json=received)}))
    )
    async with signed_in(app) as web:
        response = await web.get("/transactions/txn_in_1")
    # The provenance is stated once, typed, and the sending transaction is a
    # link — this page never claims that page is readable, only where it is.
    assert "Received via" in response.text
    assert '<code class="raw">internal_transfer</code>' in response.text
    assert '<a href="/transactions/txn_t1">' in response.text
    # The side's own `type` is dropped by `payments.side_rows` for EVERY kind
    # (`_SKIP_IN_GENERIC`), not specially for this one — no copy was invented for
    # a state this build cannot observe, and nothing claims the wrong kind.
    assert "external_bank" not in response.text


# --- roles, CSRF, double-click -------------------------------------------------------------------


async def test_a_viewer_sees_it_read_only():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(NEW + PICKED)
        refused = await post(web, NEW, transfer_form())
    assert response.status_code == 200
    assert "needs the <code>transfer.create</code> permission" in response.text
    assert 'id="transfer-form"' not in response.text
    assert refused.status_code == 403


async def test_the_transfer_mutation_needs_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            NEW,
            content=transfer_form(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 403 and "CSRF" in response.text


async def test_the_transfer_form_carries_hx_sync_against_a_double_click():
    """...and freezes the ROUTE controls for the flight.

    The route form and the submit form are separate sync queues, so with the
    selects live a route change mid-submission swapped `main` — fresh nonce,
    re-enabled button — while the POST was still on the wire, and a second click
    was a second payout. Disabling the triggers means no change event fires, so
    no competing GET exists to swap anything.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    assert 'hx-sync="this:drop"' in response.text
    guard = re.search(r'id="transfer-form"[^>]*hx-disabled-elt="([^"]*)"', response.text)
    assert guard, "the submit form lost its disabled-elt guard"
    # `#customer_id` joined at the design pass — the round that made the
    # customer a trigger of the same route form: the freeze covers every control
    # whose `change` swaps `main`, or it covers nothing.
    for control in ("button[type=submit]", "#customer_id", "#virtualAccountId",
                    "#destination", "#destinationVirtualAccountId"):
        assert control in guard.group(1), f"{control} is live during a money POST"

    # Per-trigger coverage (the finding on the payout twin):
    # every source the route form's own trigger names must appear in the freeze,
    # so a control added to the trigger without joining the guard fails here.
    triggers = re.search(
        r'id="transfer-route"[^>]*hx-trigger="([^"]*)"', response.text
    ).group(1)
    sources = re.findall(r"change from:#([\w-]+)", triggers)
    assert sources, "the route form lost its change triggers — re-read the guard"
    for source in sources:
        assert f"#{source}" in guard.group(1), (
            f"trigger #{source} is live during a money POST"
        )


async def test_a_replayed_transfer_form_never_pays_twice(session):
    """The lost-redirect drill (OPERATIONS_SPEC §1, intent nonce): the hash guard
    releases the moment the first operation confirms, so a browser re-sending the
    same body after a lost redirect would pay twice without it. One render is one
    operation, forever."""
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json={"id": "txn_t1"})}),
            calls,
        )
    )
    async with signed_in(app) as web:
        rendered = await web.get(NEW + PICKED)
        body = transfer_form(intent=intent(rendered.text))
        first = await post(web, NEW, body)
        replay = await post(web, NEW, body)  # the browser re-sends, unprompted

    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 1
    ops = (await session.execute(select(Operation))).scalars().all()
    assert len(ops) == 1 and ops[0].state == "confirmed"
    assert first.headers["HX-Redirect"] == replay.headers["HX-Redirect"] == "/transactions/txn_t1"


async def test_a_transfer_carrying_a_nonce_spent_on_another_payment_landed_on_no_receipt(session):
    """This form's nonce arrives in a hidden field — a real one,
    minted by a render of this console — and the form mints a `payout_create`,
    which is the one thing `by_intent` compares. So a nonce
    already spent on a *different* payment of the same type came back here as
    `is_new=False` holding a real, confirmed operation, and both arms after the
    send key off `op.state`: the operator was redirected to that payment's
    transaction page. A settled-looking receipt for a transfer nobody made.

    Nothing was ever sent on this path, before the guard or after it — the same
    shape as the four sibling routes. Only the sentence was wrong, and a sentence is
    what an operator acts on."""
    import uuid

    from app import operations

    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    nonce = uuid.uuid4()
    thief = {"actor_id": "ops-2", "actor_email": "other@example.com"}
    # Somebody else's payout: the same operation type, so no `IntentTypeMismatch`,
    # a different body, and already confirmed with a transaction id of its own.
    spent, _ = await operations.start(
        session,
        type="payout_create",
        **thief,
        path=payments.PAYOUT_PATH,
        body={"amount": "1.00", "asset": "USD"},
        intent=nonce,
    )
    await operations.transition(session, spent.id, "in_flight", **thief)
    await operations.transition(
        session, spent.id, "confirmed", **thief, conduit_resource_id="txn_someone_else"
    )

    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form(intent=minted_intent(nonce)))

    landing = unquote_plus(response.headers.get("HX-Redirect") or response.headers["location"])
    # The resolved operation, not that operation's transaction: the page an
    # operator lands on must not be readable as this transfer's receipt.
    assert f"/operations/{spent.id}" in landing
    assert "txn_someone_else" not in landing
    assert "had already been used" in landing
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    # No second operation either — the nonce resolved, as it is supposed to.
    payouts = (
        (await session.execute(select(Operation).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert [op.id for op in payouts] == [spent.id]


async def test_a_fresh_render_is_a_deliberate_second_transfer(session):
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json={"id": "txn_t1"})}),
            calls,
        )
    )
    async with signed_in(app) as web:
        for _ in range(2):
            rendered = await web.get(NEW + PICKED)
            await post(web, NEW, transfer_form(intent=intent(rendered.text)))
    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 2
    ops = (await session.execute(select(Operation))).scalars().all()
    assert len(ops) == 2
    assert len({op.idempotency_key for op in ops}) == 2
    assert len({op.intent for op in ops}) == 2


async def test_every_render_of_the_transfer_form_mints_a_new_nonce():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        first = await web.get(NEW + PICKED)
        second = await web.get(NEW + PICKED)
    assert intent(first.text) != intent(second.text)


# --- optional supporting documents ----------------------------------------------------------------


async def test_a_document_attached_to_a_transfer_rides_the_same_documents_array():
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_voluntary_1.png")
        await post(web, NEW, transfer_form(documentIds="doc_voluntary_1"))
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert sent["documents"] == ["doc_voluntary_1"]
    assert sent["purpose"] == "intercompany"


async def test_a_transfer_cannot_attach_a_document_this_actor_did_not_upload(session):
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        refused = await post(web, NEW, transfer_form(documentIds="doc_not_mine"))

    assert refused.status_code == 422
    assert "could not be matched" in refused.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).scalars().all() == []


async def test_a_transfer_with_no_document_sends_no_documents_key():
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await post(web, NEW, transfer_form())
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert "documents" not in sent


# --- what left this screen, and where it went ------------------------------------------------------


async def test_the_screen_offers_no_whitelist_rail_or_country_control():
    """And reads no whitelist at all — so there is no bank coordinate on this
    page to mask, which is why `test_the_registered_destination_pickers_never_
    print_a_whole_account` no longer names this surface."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(NEW + PICKED)
    for gone in ("whitelistRecipientId", 'name="rail"', "recipientType", "destinationCountry"):
        assert gone not in response.text, gone
    assert [c for c in calls if "whitelist" in c[1]] == []
    for coordinate in ("accountNumber", "routingNumber", "iban", "bic", "••••"):
        assert coordinate not in response.text, coordinate


async def test_the_lede_leaves_a_breadcrumb_to_the_flow_that_moved():
    """The registration walkthrough left this screen for the payout page's
    intercompany route and the Contacts page. An operator who used to reach it
    from here must still be able to find it."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    assert f"/customers/{CID}/payouts/new?purpose=intercompany" in response.text
    assert f"/customers/{CID}/contacts" in response.text


async def test_a_destination_deep_link_still_preselects_that_customer():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{NEW}?destination={OTHER_CID}")
    assert f'value="{OTHER_CID}"' in response.text
    assert DEST_USD["id"] in response.text


# --- the live probe's guards (hermetic) ------------------------------------------------------


def test_the_va_transfer_probe_refuses_anything_that_is_not_the_sandbox():
    """`--check-config` runs the two refusals and exits before anything is sent,
    which is the only way to prove a guard fires without running the script that
    would otherwise create a payout. The probe itself is run by hand against the
    sandbox; this is the part CI can see."""
    script = Path(__file__).parent / "e2e" / "10_va_transfer_probe.py"

    def run(**overrides):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CONDUIT_SANDBOX_")}
        return subprocess.run(
            [sys.executable, str(script), "--check-config"],
            env={
                **env,
                "CONDUIT_SANDBOX_API_KEY": "ck_sandbox_fake_for_check_config",
                "CONDUIT_SANDBOX_HOST": "https://api.sandbox.conduit.financial",
                **overrides,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

    ok = run()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "api.sandbox.conduit.financial" in ok.stdout

    live_key = run(CONDUIT_SANDBOX_API_KEY="ck_live_not_a_sandbox_key")
    assert live_key.returncode != 0
    assert "not a ck_sandbox_ key" in live_key.stdout + live_key.stderr

    other_host = run(CONDUIT_SANDBOX_HOST="https://api.conduit.financial")
    assert other_host.returncode != 0
    assert "is not https://api.sandbox.conduit.financial" in other_host.stdout + other_host.stderr


def test_the_probe_sends_exactly_the_body_the_console_builds():
    """A probe answering about a body nobody sends answers nothing. The script
    cannot be imported (it exits on missing credentials by design), so the check
    is on its source: the destination keys it hardcodes are the ones
    `virtual_account_body` emits, no more and no less."""
    script = (Path(__file__).parent / "e2e" / "10_va_transfer_probe.py").read_text()
    built = payments.virtual_account_body(
        customer_id=CID,
        virtual_account_id=VID,
        destination_account_id=DEST_USD["id"],
        asset="USD",
        amount_text="1.00",
        reference="ZZZTEST-VA-PROBE",
    )
    start = script.index('"destination": {')
    subtree = script[start : script.index("\n    }", start)]
    assert set(built["destination"]) == {"type", "virtualAccountId", "remittance"}
    for key in built["destination"]:
        assert f'"{key}"' in subtree, key
    for absent in ("rail", "recipient", "accountNumber", "routingNumber"):
        assert f'"{absent}"' not in subtree, absent


# --- the flow starts at the action --------------------------------------------------
#
# The ribbon lands on `/transfers/new` with no customer in the URL, and the
# source customer is the form's FIRST control rather than a prefix of it. The
# customer-scoped URL is unchanged and still renders the same form — every
# action pill, way-back link and bookmark in the console points at one.

GLOBAL = "/transfers/new"


async def observe_account(session, *, account_id: str, customer_id: str, code: str):
    """One `virtual_account.activated` observation, exactly as the webhook and
    the reconciler write it (`app/web/accounts.py` reads these four keys)."""
    from app import projections

    await projections.apply_observation(
        session,
        resource_kind="virtual_accounts",
        resource_id=account_id,
        observed={
            "virtualAccountId": account_id,
            "customerId": customer_id,
            "asset": {"code": code},
            "activatedAt": "2026-08-28T22:19:39.125Z",
            "status": "active",
        },
    )


async def test_the_global_route_renders_the_form_with_no_customer_picked():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(GLOBAL)

    assert response.status_code == 200
    # The source customer is a control on the form, not a segment of the URL.
    assert 'id="customer_id"' in response.text
    # No customer, so no customer-scoped read was made — and the ONE bounded
    # customers read still happened, because that is what fills the picker.
    assert [c for c in calls if "virtual-accounts" in c[1]] == []
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1


async def test_the_global_and_the_scoped_route_render_the_same_form():
    """Paired on purpose: the customer-scoped URL is what every existing pill and
    hand-off in this console points at, and it must keep serving the form with
    the customer already filled in."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        scoped = await web.get(NEW)
        picked = await web.get(f"{GLOBAL}?customer_id={CID}")

    for response in (scoped, picked):
        assert response.status_code == 200
        assert f'value="{CID}"' in response.text
        assert VID in response.text  # this customer's own accounts, listed
    # One form, one action: both renders point their route form at the global
    # route, so changing the customer is a re-render rather than a new URL shape.
    for response in (scoped, picked):
        row = response.text.split('id="transfer-route"')[1].split("</form>")[0]
        assert f'hx-get="{GLOBAL}"' in row and f'action="{GLOBAL}"' in row
        assert "change from:#customer_id" in row


async def test_changing_the_source_customer_re_renders_with_that_customers_accounts():
    """The established route-form idiom: `change` on the picker re-reads, so the
    balances, the currency label and the destination list belong to the customer
    that is actually selected."""
    app = make_app(
        stub(
            {
                ("GET", "/v2/customers"): page(NAMES),
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
                ("GET", DEST_ACCOUNTS): page([DEST_EUR]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"{GLOBAL}?customer_id={OTHER_CID}")

    assert response.status_code == 200
    assert DEST_EUR["id"] in response.text
    assert USD_ACCOUNT["id"] not in response.text


async def test_the_empty_form_never_claims_the_customer_has_no_accounts():
    """The honesty rule this console is built on, at the one new state: nothing
    has been asked about anybody, so nothing may be asserted about anybody."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(GLOBAL)

    body = flat(response)
    assert "no active virtual account" not in body
    assert "virtual accounts could not be read" not in body
    assert "has no" not in body
    # What it says instead.
    assert "Pick the customer" in body


async def test_the_destination_suggestions_are_the_observed_holders_of_the_source_currency(
    session,
):
    """One LOCAL query over the virtual-account projections —
    no Conduit call, no per-candidate read — and the sentence says `observed`,
    because an account this console never saw is absent from it."""
    await observe_account(session, account_id=VID, customer_id=CID, code="USD")
    await observe_account(
        session, account_id=DEST_USD["id"], customer_id=OTHER_CID, code="USD"
    )
    third = {"id": "cus_holds_nothing_here", "legalName": "ZZZTEST Quiet Holdings"}

    app = make_app(
        stub(
            {
                ("GET", "/v2/customers"): page([*NAMES, third]),
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
                ("GET", DEST_ACCOUNTS): page([DEST_USD]),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"{GLOBAL}?customer_id={CID}&virtualAccountId={VID}")

    suggestions = response.text.split('id="destination-customers"')[1].split("</datalist>")[0]
    assert OTHER_CID in suggestions
    assert third["id"] not in suggestions
    assert "observed" in flat(response)
    assert "USD" in flat(response).split("observed")[1][:120]


async def test_with_no_source_account_the_destination_suggestions_are_unfiltered(session):
    """No currency picked yet is no filter — the datalist is the bounded read as
    it has always been, and the help sentence is the one it has always carried.

    "No source account" and "no source customer" are the same state on this
    screen: the select falls back to the customer's first active account, so the
    only way to have no currency is to have no accounts to pick from."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(GLOBAL)

    suggestions = response.text.split('id="destination-customers"')[1].split("</datalist>")[0]
    assert CID in suggestions and OTHER_CID in suggestions
    assert "observed" not in flat(response)
    assert "Any customer of this organisation" in flat(response)


async def test_the_ceiling_holds_on_the_global_render(session):
    """Three Conduit reads per render, the number this screen has always made:
    the bounded customers read, this customer's accounts and the destination
    customer's. The holdings query is local and free."""
    await observe_account(session, account_id=VID, customer_id=CID, code="USD")
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await web.get(f"{GLOBAL}?customer_id={CID}&virtualAccountId={VID}&destination={OTHER_CID}")

    reads = [c for c in calls if c[0] == "GET"]
    assert len(reads) == 3, reads


async def test_the_ribbon_lands_on_the_form_itself():
    """The redesign's whole point: Transact's three entries are the three forms,
    not a launcher that asks for a customer first."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(GLOBAL)).text

    ribbon = html.split('<nav class="ribbon"')[1].split("</nav>")[0]
    for href, label in (
        ("/payouts", "Send a payout"),
        ("/transfers/new", "Transfer"),
        ("/convert", "Convert"),
    ):
        assert f'href="{href}"' in ribbon and f">{label}</a>" in ribbon
    assert "/orders#move-money" not in ribbon


async def test_a_console_that_has_observed_nothing_does_not_filter_on_ignorance(session):
    """A fresh deploy (no webhook yet) has zero virtual-account observations.
    Filtering the destination datalist against that empty knowledge emptied the
    picker for every currency — found live on exactly such an install
    (2026-09-02). No observations at all → the filter has no information: the
    suggestions stay unfiltered and the sentence claims no "observed" narrowing.
    The pair below is the informed case, unchanged."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat(await web.get(NEW + PICKED))
    # Both customers suggested — nothing filtered them.
    assert "ZZZTEST Globex Supplies LLC" in html
    assert "observed, not all" not in html


async def test_a_console_with_observations_still_narrows(session):
    from app import projections as proj
    await proj.apply_observation(
        session, resource_kind="virtual_accounts", resource_id="vac_seen_1",
        observed={"customerId": OTHER_CID, "asset": {"code": "USD"}, "status": "active"},
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat(await web.get(NEW + PICKED))
    assert "observed, not all" in html


async def test_a_nonce_from_another_kind_of_operation_is_refused_here_too(session):
    """The handler under every route that has no re-render of its own
    (`create_app`'s `IntentTypeMismatch` handler). `operations.start` refuses a
    nonce that already reached another kind of operation for all twelve of its
    callers; before the handler, eleven of them turned that honest refusal into
    a 500. 422, the house problem box, nothing on the wire, nothing in the
    ledger."""
    import uuid

    from app import operations

    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    nonce = uuid.uuid4()
    await operations.start(
        session,
        type="order_execute",
        actor_id="ops-1",
        actor_email="ops@example.com",
        path="/v2/orders/ord_1/execute",
        body=None,
        intent=nonce,
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, transfer_form(intent=minted_intent(nonce)))

    assert response.status_code == 422
    assert "belongs to something else" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).first() is None
