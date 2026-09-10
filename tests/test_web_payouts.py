"""The external payout flow: route metadata obeyed, decimals kept, quotes stale.

The polarity matrix is the point of this file. The two fedwire fixtures differ in
exactly the two gates that matter — goods asks for a document, intercompany asks
for a whitelisted recipient — and the form has to change shape for each *from the
response alone*, with no purpose→gate table anywhere in the app.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.tokens import sign
from app.models import Operation
from app.web import payouts
from tests.conftest import settings_override
from tests.payments_fixtures import (
    ACH_INDIVIDUAL,
    CID,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    PENDING,
    PAYOUT,
    QUOTE,
    EXPIRED_QUOTE,
    REGISTERED,
    SEPA_BUSINESS,
    USD_ACCOUNT,
    VID,
    WHITELIST_PATH,
    encoded,
    page,
    payout_form,
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

NEW = f"/customers/{CID}/payouts/new"
ROUTE = "?purpose=payment_for_goods_or_services&rail=fedwire&recipientType=business&destinationCountry=USA"
INTERCOMPANY_ROUTE = "?purpose=intercompany&rail=fedwire&recipientType=business&destinationCountry=USA"


def routes(requirements=FEDWIRE_BUSINESS, recipients=None, extra=None):
    return {
        ("GET", "/v2/payouts/requirements"): (
            requirements
            if isinstance(requirements, httpx.Response)
            else httpx.Response(200, json=requirements)
        ),
        ("GET", "/v2/customers"): page(
            [{"id": CID, "legalName": "ZZZTEST Console EOOD"}]
        ),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
        ("GET", WHITELIST_PATH): page(
            recipients if recipients is not None else [REGISTERED, PENDING]
        ),
        # A `doc_` id is only attachable once it is in this console's own upload
        # ledger (`documents.attachable`), so these tests upload for real.
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


# --- the route row --------------------------------------------------------------------------
#
# The payout-flow restructure round replaced the two gating steps (a screen of
# seven purpose buttons, then a rail/type/country form) with one row of five
# controls at the top of the page, and the requirements form underneath it.


async def test_the_route_row_offers_all_seven_purposes_as_a_dropdown():
    """SUPERSEDED `test_the_purpose_picker_lists_all_seven_with_plain_language`:
    the seven purposes were a screen of `<a class="btn">` links that had to be
    pressed before anything else on the page existed. They are options now, so
    the assertion about hrefs is gone — what survives is that all seven are
    offered, in operator language, with the raw key still visible and still the
    only thing submitted (QA F-002 / FORM_ENGINE_SPEC's verbatim rule)."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    for value in (
        "payment_for_goods_or_services",
        "payroll",
        "treasury_management",
        "intercompany",
        "investments",
        "prefunding",
        "other",
    ):
        assert f'<option value="{value}" >' in response.text
    # The OPTION carries the label alone — this is the one enum whose labels are
    # sentence-length, and the design pass ruled the closed control reads as a
    # paragraph with the raw key appended. The key is still on the page (the
    # `.raw` mono under the row) and is still the only thing `value` submits.
    assert ">Payment for goods or services</option>" in response.text
    assert "Payment for goods or services — payment_for_goods_or_services" not in response.text
    # Nothing is loaded until the four route parts are answered.
    assert 'id="payout-form"' not in response.text


async def test_the_row_refetches_on_a_route_change_and_keeps_the_url_linkable():
    """The refetch machinery, asserted where it lives: one `hx-get` on the row,
    fired by `change` from the FOUR route controls — so moving the purpose, the
    rail, the recipient type or the country re-reads discovery and reshapes the
    form — and `hx-push-url` keeps the widening query string in the address bar.
    The funding account is deliberately outside that scope (see the test below).

    A later round moved the row's `hx-get` to the GLOBAL route and put the source
    customer on its own `change` clause: the customer is a control in this row
    now, not a prefix of the URL, and it re-reads the accounts and the whitelist
    rather than discovery."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    row = response.text.split('id="route-row"')[1].split("</form>")[0]
    fields = row.split('id="route-fields"')[1].split("</span>")[0]
    assert 'hx-get="/payouts/new"' in row
    assert 'hx-trigger="change from:#route-fields, change from:#customer_id"' in row
    assert 'name="customer_id"' in row and 'name="customer_id"' not in fields
    assert 'hx-push-url="true"' in row
    # The four that change what discovery answers are inside the trigger scope;
    # the account, which cannot, is not.
    for name in ("purpose", "rail", "recipientType", "destinationCountry"):
        assert f'name="{name}"' in fields
    assert 'name="virtualAccountId"' not in fields
    assert 'name="virtualAccountId"' in row
    # A concurrent change must replace the in-flight read, not be dropped by it:
    # dropping would leave the form showing the route that is no longer selected.
    assert 'hx-sync="this:replace"' in row
    # Progressive enhancement survives: with the trigger on `change`, the button
    # is still a plain GET submit.
    assert 'method="get"' in row and "Load requirements" in row
    for name in ("purpose", "rail", "recipientType", "destinationCountry", "virtualAccountId"):
        assert f'name="{name}"' in row


async def test_a_chosen_purpose_stays_selected_and_explains_itself():
    """SUPERSEDED `test_a_settled_purpose_collapses_to_a_summary_with_a_change_link`:
    there is no step to settle and no Change link, because the control never went
    away. The purpose's own sentence is the row's help text instead of a
    collapsed summary's, and the raw key is still printed beside the label."""
    app = make_app(stub(routes(ACH_INDIVIDUAL)))
    async with signed_in(app) as web:
        response = await web.get(NEW + "?purpose=payroll")

    assert '<option value="payroll" selected>' in response.text
    assert '<code class="raw">payroll</code>' in response.text
    assert "Paying salaries, contractors or benefits." in response.text
    # The other six are still offered — that is the point of a dropdown.
    assert '<option value="treasury_management" >' in response.text


async def test_a_deep_link_with_only_a_purpose_still_lands_on_the_right_page():
    """The pills' URLs are in bookmarks and in this suite. `?purpose=…` still
    means what it meant: the page opens with that purpose chosen and the rest of
    the route to answer."""
    app = make_app(stub(routes(ACH_INDIVIDUAL)))
    async with signed_in(app) as web:
        response = await web.get(NEW + "?purpose=payroll")
    assert response.status_code == 200
    assert '<option value="payroll" selected>' in response.text
    assert 'id="payout-form"' not in response.text  # the route is not complete yet


async def test_a_complete_route_loads_the_form_under_the_row():
    """SUPERSEDED `test_a_settled_route_collapses_and_change_keeps_the_purpose`:
    the route row does not collapse, so there is no summary chip and no Change
    URL to assert. What matters is unchanged — the four parts are what they were
    asked for, and discovery's form is below them."""
    app = make_app(stub(routes(FEDWIRE_BUSINESS)))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)

    row = response.text.split('id="route-row"')[1].split("</form>")[0]
    assert '<option value="fedwire" selected>fedwire</option>' in row
    # Sentence case on the recipient type (QA F-002) — the `value` is the wire's.
    assert '<option value="business" selected>Business</option>' in row
    assert 'name="destinationCountry" value="USA"' in row
    assert 'id="payout-form"' in response.text


async def test_the_route_row_survives_a_route_conduit_refuses():
    """SUPERSEDED `test_a_route_that_conduit_refuses_keeps_its_selects_on_screen`:
    the selects are always on screen now, so this asserts the thing that could
    still go wrong — the problem-detail is shown and the controls that pick
    another rail are usable."""
    app = make_app(
        stub(
            routes(
                httpx.Response(
                    422,
                    json={"type": "RAIL_NOT_ELIGIBLE", "title": "Rail not eligible"},
                )
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    assert 'name="rail"' in response.text
    assert "Load requirements" in response.text
    assert "Conduit refused this: RAIL_NOT_ELIGIBLE" in response.text  # A3


async def test_intercompany_reshapes_inline_and_does_not_bounce_to_transfers():
    """SUPERSEDED `test_intercompany_points_at_the_transfers_screen`: the bounce
    card is gone. `intercompany` is a purpose like the other six, and what makes
    it different is discovery's own `whitelist.required` flag — which reshapes
    this form in place, exactly as it does for any other gated route. The
    transfers screen is unchanged and is still its own flow."""
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)

    assert "transfers screen" not in response.text
    # The flag-driven reshape, in place: the picker replaced the typed
    # coordinates and the form is right there.
    assert 'id="whitelistRecipientId"' in response.text
    assert "This route requires a whitelisted recipient" in response.text
    assert 'name="f.destination.recipient.routingNumber"' not in response.text
    assert 'id="payout-form"' in response.text


async def test_an_unreadable_requirements_response_is_shown_not_swallowed():
    app = make_app(
        stub(
            routes(
                httpx.Response(
                    422,
                    json={
                        "type": "RAIL_NOT_ELIGIBLE",
                        "title": "Rail not eligible",
                        "detail": "fednow is not available for this corridor.",
                        "resolution": "Pick another rail.",
                        "correlationId": "corr_rail",
                    },
                )
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    # A3: an uncatalogued code renders as the code, and Conduit's own resolution
    # line rides along — it is the one remaining piece of guidance for a refusal
    # this console has no sentence for. Its `detail` never does.
    assert "Conduit refused this: RAIL_NOT_ELIGIBLE" in response.text
    assert "Pick another rail." in response.text and "corr_rail" in response.text
    assert "fednow is not available for this corridor." not in response.text
    assert 'id="payout-form"' not in response.text


async def test_the_payout_flow_keeps_the_ribbon_on_transact():
    """Re-stated for the ribbon: a payout
    is an ACTION, so it lights the Transact group and its own verb — never
    Customers (the page it is nested under by URL), and never the Orders list
    (the browse half of the same `section="orders"` key). Both steps of the flow
    are asserted, because a highlight that changes between the picker and the
    loaded route is worse than one that is simply wrong."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        picker = await web.get(NEW)
        loaded = await web.get(NEW + ROUTE)

    for response in (picker, loaded):
        assert '<div class="group on" role="group" aria-labelledby="grp-transact">\n    <p class="group-head" id="grp-transact">Transact</p>' in response.text
        assert (
            '<a class="item on" href="/payouts" aria-current="page">Send a payout</a>'
            in response.text
        )
        assert '<a class="item on" href="/customers"' not in response.text
        assert '<a class="item on" href="/orders">Orders</a>' not in response.text


# --- the route-metadata polarity matrix ---------------------------------------------------------


async def test_documentation_required_shows_the_upload_widget_and_accepted_types():
    app = make_app(stub(routes(FEDWIRE_BUSINESS)))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)

    assert "This route requires a supporting document" in response.text
    assert 'data-purpose="transaction_support"' in response.text
    for kind in ("bank_verification_letter", "invoice", "payroll_register"):
        assert kind in response.text
    # …and the recipient fields stay free-form, because this route has no
    # whitelist gate.
    assert 'name="f.destination.recipient.accountNumber"' in response.text
    assert 'name="whitelistRecipientId"' not in response.text


async def test_whitelist_required_replaces_the_recipient_fields_with_a_picker():
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)

    assert "This route requires a whitelisted recipient" in response.text
    assert 'name="whitelistRecipientId"' in response.text
    # The identity fields the registered entry carries are gone from the form…
    for gone in ("accountNumber", "routingNumber", "legalName"):
        assert f'name="f.destination.recipient.{gone}"' not in response.text
    # …while the ones it does not (addresses, account type) remain.
    assert 'name="f.destination.recipient.bankAddress.city"' in response.text
    assert 'name="f.destination.recipient.accountType"' in response.text
    # …and no document *gate*: this fixture says documentation.required is false,
    # so the widget is there, collapsed and optional, and the
    # route states no precondition.
    assert "This route requires a supporting document" not in response.text
    assert "Attach a supporting document (optional)" in response.text


async def test_the_picker_offers_registered_entries_only():
    app = make_app(
        stub(
            routes(
                FEDWIRE_INTERCOMPANY,
                recipients=[
                    REGISTERED,
                    PENDING,
                    {**REGISTERED, "id": "wlr_suspended", "status": "suspended"},
                    {**REGISTERED, "id": "wlr_revoked", "status": "revoked"},
                ],
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert 'value="wlr_registered"' in response.text
    for excluded in ("wlr_pending", "wlr_suspended", "wlr_revoked"):
        assert f'value="{excluded}"' not in response.text


async def test_a_whitelist_gate_with_no_registered_entry_says_so():
    """Half of a pair: the *successful* empty read, where "there are none" is a
    fact. The sibling below is the read that failed, and the negative assertion
    here is what stops the two collapsing back into one sentence."""
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY, recipients=[PENDING])))
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert "No <strong>registered</strong> recipient" in response.text
    assert f"/customers/{CID}/recipients" in response.text
    assert "whitelist could not be read" not in response.text


async def test_an_unreadable_whitelist_is_not_no_registered_recipient():
    """The other half. Registering is a compliance-reviewed write at Conduit, so
    the console asserting an absence it never established is how an operator ends
    up filing a duplicate."""
    app = make_app(
        stub(
            routes(
                FEDWIRE_INTERCOMPANY,
                extra={
                    ("GET", WHITELIST_PATH): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Conduit is unavailable"}
                    )
                },
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert "No <strong>registered</strong> recipient" not in response.text
    assert "whitelist could not be read" in response.text
    # And the banner names *which* read failed, rather than only Conduit's own
    # title — the page carries more than one read.
    assert "The whitelist could not be read" in response.text


async def test_an_unreadable_funding_list_is_not_no_active_virtual_account():
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
        response = await web.get(NEW + ROUTE)
    assert "no active virtual account" not in response.text
    assert "virtual accounts could not be read" in response.text
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3


async def test_a_funding_list_that_is_genuinely_empty_still_says_so():
    """The pair's other half for the accounts read."""
    app = make_app(
        stub(
            routes(
                extra={("GET", f"/v2/customers/{CID}/virtual-accounts"): page([])}
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    assert "no active virtual account" in response.text
    assert "virtual accounts could not be read" not in response.text


async def test_a_second_page_of_registrations_is_stated_not_swallowed():
    """The picker takes page one. That is allowed; being quiet about it is not —
    a registration on page two is not offered here, and the sentence above it
    must not assert that it does not exist."""
    app = make_app(
        stub(
            routes(
                FEDWIRE_INTERCOMPANY,
                extra={
                    ("GET", WHITELIST_PATH): page([REGISTERED], next_cursor="cur_page_two")
                },
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert "More registrations than this picker reads" in response.text


async def test_the_contact_step_shares_the_route_row_s_sync_queue():
    """Every `main`-swapping form on this page has to be on one queue, or two of
    them can be in flight at once and the loser paints a page built from the
    other's inputs. The route row is the master; the contact step (the Prefill
    form, before it moved to step 2) used to carry no scope at all."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    step = response.text.split('id="contact-first"')[1].split("</form>")[0]
    assert "hx-get" in step and 'name="counterparty"' in step
    assert 'hx-sync="#route-row:replace"' in step
    # It sends the customer and the contact, and NOT the answered route: a route
    # carried back would be filled in already and would then win over the
    # corridor the contact implies, which is the whole point of the step.
    assert 'name="customer_id"' in step
    for key in ("purpose", "rail", "recipientType", "destinationCountry"):
        assert f'name="{key}"' not in step, key


async def test_the_batch_funding_picker_inherits_this_module_s_verdict():
    """`app/web/batches.py` funds a batch through this module's `_accounts`, so
    the batch upload page inherits the same read — and must spend its verdict
    rather than print "no active virtual account" over a list nobody read. The
    test lives here because the read under test is this module's."""
    batch_new = (
        f"/customers/{CID}/batches/new"
        "?rail=fedwire&recipientType=business&destinationCountry=USA"
    )
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
        response = await web.get(batch_new)
    assert "no active virtual account" not in response.text
    assert "virtual accounts could not be read" in response.text
    # The refusal itself too, since this page mints no problem card of its own
    # for the accounts read — in this console's words, with Conduit's code in
    # them (A3).
    assert "Conduit refused this: UNAVAILABLE" in response.text

    readable = make_app(
        stub(routes(extra={("GET", f"/v2/customers/{CID}/virtual-accounts"): page([])}))
    )
    async with signed_in(readable) as web:
        response = await web.get(batch_new)
    assert "no active virtual account" in response.text
    assert "virtual accounts could not be read" not in response.text


async def test_one_page_of_registrations_states_no_cap():
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert "More registrations than this picker reads" not in response.text


async def test_the_sepa_fixture_drives_a_different_form_again():
    app = make_app(stub(routes(SEPA_BUSINESS)))
    async with signed_in(app) as web:
        response = await web.get(
            NEW + "?purpose=payment_for_goods_or_services&rail=sepa&recipientType=business"
            "&destinationCountry=DEU"
        )
    assert 'name="f.destination.recipient.iban"' in response.text
    assert "This route requires a supporting document" in response.text


# --- submitting -----------------------------------------------------------------------------------


async def test_both_worlds_the_documentation_gate_needs_no_flag(session):
    """OPERATIONS_SPEC §5. Today a missing required document is
    a synchronous `422 DOCUMENTATION_REQUIRED` (no payout created); Conduit has
    announced the transaction will instead be **accepted** with an RFI raised
    against it. Both already work, because the submit branches on `op.state` and
    on nothing else.

    STUBBED, necessarily: no RFI can be created in any environment this console
    can reach, so the announced world has never been observed anywhere.
    """
    refusal = {
        "type": "DOCUMENTATION_REQUIRED",
        "title": "Supporting documentation required",
        "detail": "This route requires an invoice.",
        "status": 422,
    }
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(422, json=refusal)}))
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_wrong_kind.png")
        today = await post(web, NEW, encoded(payout_form(documentIds="doc_wrong_kind")))

    # World 1: rejected, rendered as the problem it is, form still filled in.
    assert today.status_code == 422
    # A3: the console's sentence for DOCUMENTATION_REQUIRED, not Conduit's.
    assert "This payment needs a supporting document" in today.text
    assert "This route requires an invoice." not in today.text
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.state == "rejected"  # …so the §1 guard is released for the retry

    # World 2: the same submission accepted, and an RFI raised against the
    # transaction it created. The operator lands on the payment's own page and
    # is told it is held — no branch in payouts.py knows the difference.
    held = {**PAYOUT, "hasRfi": True, "rfiId": "rfi_9"}
    rfi = {
        "id": "rfi_9",
        "title": "Invoice for this payment",
        "status": "open",
        "subjects": [{"subjectType": "transaction", "subjectId": PAYOUT["id"]}],
        "rounds": [{"roundNumber": 1, "status": "open", "ask": "Attach the invoice."}],
        "createdAt": "2026-08-27T11:00:00.000Z",
        "updatedAt": "2026-08-27T11:00:00.000Z",
        "publishedAt": "2026-08-27T11:00:00.000Z",
    }
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", "/v2/payouts"): httpx.Response(202, json=held),
                    ("GET", f"/v2/transactions/{PAYOUT['id']}"): httpx.Response(200, json=held),
                    ("GET", "/v2/rfis"): page([rfi]),
                }
            )
        )
    )
    async with signed_in(app) as web:
        accepted = await post(
            web, NEW, encoded(payout_form(documentIds="doc_wrong_kind", amount="1001.00"))
        )
        landing = await web.get(accepted.headers["HX-Redirect"])

    assert accepted.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    assert "Held — information requested" in landing.text
    assert "Attach the invoice." in landing.text
    assert 'hx-post="/rfis/rfi_9/respond"' in landing.text


async def test_a_valid_payout_is_sent_as_the_fiat_payout_dto(session):
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(web, NEW, encoded(payout_form(documentIds="doc_support_1")))

    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert sent["customerId"] == CID and sent["virtualAccountId"] == VID
    assert sent["assetAmount"] == {"code": "USD", "amount": "1000.00"}
    assert sent["purpose"] == "payment_for_goods_or_services"
    assert sent["documents"] == ["doc_support_1"]
    assert sent["destination"]["rail"] == "fedwire" and sent["destination"]["type"] == "fiat"
    assert sent["destination"]["recipient"]["routingNumber"] == "021000021"
    assert sent["destination"]["recipient"]["bankAddress"]["city"] == "New York"
    assert sent["destination"]["remittance"] == {"reference": "INV-4471"}
    # `virtualAccountId` is the route's, not the engine's — it never appears
    # inside the destination subtree discovery declared it under.
    assert "virtualAccountId" not in sent["destination"]
    # The operations layer injected the durable matcher.
    assert sent["clientReferenceId"]

    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.type == "payout_create" and op.state == "confirmed"
    assert op.conduit_resource_id == PAYOUT["id"]
    assert str(op.id) == sent["clientReferenceId"]


async def test_the_off_rail_ach_subtree_is_neither_asked_for_nor_sent(session):
    """The app's only discovery override, end to end (conduit-issues/06).

    Both fedwire fixtures declare `destination.ach.authorizationType` required;
    a live fedwire payout carrying it was accepted and the subtree discarded. So
    the operator is never asked, and a browser that posts one anyway is ignored
    — the engine drops names the model does not know."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        page_html = await web.get(NEW + ROUTE)
        await post(web, NEW, encoded(payout_form(documentIds="doc_1")))

    assert 'name="f.destination.ach.authorizationType"' not in page_html.text
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert "ach" not in sent["destination"]


async def test_a_real_ach_route_still_asks_for_its_authorization_type():
    app = make_app(stub(routes(ACH_INDIVIDUAL)))
    async with signed_in(app) as web:
        response = await web.get(
            NEW + "?purpose=payroll&rail=ach&recipientType=individual&destinationCountry=USA"
        )
    assert 'name="f.destination.ach.authorizationType"' in response.text


async def test_a_missing_document_is_refused_before_conduit_sees_it(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, NEW, encoded(payout_form()))

    assert response.status_code == 422
    assert "requires a supporting document" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_whitelist_gated_payout_takes_its_destination_from_the_entry(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                FEDWIRE_INTERCOMPANY,
                extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)},
            ),
            calls,
        )
    )
    body = payout_form(
        purpose="intercompany",
        whitelistRecipientId=REGISTERED["id"],
        # The browser is free to send anything for the coordinate fields; the
        # server writes the registered entry's own values over them.
        **{
            "f.destination.recipient.accountNumber": "999999999",
            "f.destination.recipient.routingNumber": "011000015",
            "f.destination.recipient.legalName": "Somewhere Else Ltd",
        },
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, encoded(body))

    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    recipient = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])["destination"][
        "recipient"
    ]
    assert recipient["accountNumber"] == REGISTERED["accountNumber"]
    assert recipient["routingNumber"] == REGISTERED["routingNumber"]
    assert recipient["legalName"] == REGISTERED["legalName"]


async def test_a_whitelist_gated_payout_without_a_picked_entry_is_refused(session):
    calls: list = []
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY), calls))
    async with signed_in(app) as web:
        response = await post(web, NEW, encoded(payout_form(purpose="intercompany")))
    assert response.status_code == 422
    assert "requires a registered whitelist recipient" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_pending_entry_cannot_be_smuggled_past_the_picker(session):
    calls: list = []
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(payout_form(purpose="intercompany", whitelistRecipientId=PENDING["id"])),
        )
    assert response.status_code == 422
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_blocked_jurisdiction_stops_the_submit(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(payout_form(destinationCountry="RUS", documentIds="doc_1")),
        )
    assert response.status_code == 422
    assert "RUS is a jurisdiction Conduit blocks" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_blocked_country_answered_inside_the_form_is_caught_too(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    **{"f.destination.recipient.postalAddress.country": "IRN"},
                )
            ),
        )
    assert response.status_code == 422 and "IRN is a jurisdiction" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_bad_amount_never_becomes_a_float(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        for bad in ("", "abc", "-5", "0", "NaN", "Infinity"):
            response = await post(
                web, NEW, encoded(payout_form(amount=bad, documentIds="doc_1"))
            )
            assert response.status_code == 422, bad
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_422_amount_carries_the_aria_binding_a_clean_render_does_not(session):
    """`#amount` is hand-templated, outside the discovery
    model `m.err_attrs` reads `RenderField.errors` off — so `payments.
    payout_errors` also keys the amount refusal to `errors.fields['amount']`
    (`app/payments/__init__.py`) and the template binds it the same way a
    wizard field is bound: `aria-invalid`, `aria-describedby` pointing at an
    `#amount-error` that actually exists and carries the sentence."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        refused = await post(
            web, NEW, encoded(payout_form(amount="not-a-number", documentIds="doc_1"))
        )
        clean = await web.get(NEW + ROUTE)

    assert refused.status_code == 422
    assert (
        '<input type="text" id="amount" name="amount" inputmode="decimal" class="num"\n'
        '               value="not-a-number" placeholder="1000.00" required'
        ' aria-invalid="true" aria-describedby="amount-error">'
    ) in refused.text
    assert (
        '<div id="amount-error">\n'
        '  <div class="err">The amount must be a positive decimal, e.g. 1000.00.</div>\n'
        "</div>"
    ) in refused.text

    assert clean.status_code == 200
    assert "aria-invalid" not in clean.text
    assert "amount-error" not in clean.text


async def test_the_amount_reaches_conduit_as_the_operators_own_digits(session):
    """0.10 must not become 0.1, and 1000.000000 must not become 1000.0 — this is
    money, and `float` is not."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(web, NEW, encoded(payout_form(amount="0.10", documentIds="doc_1")))
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert sent["assetAmount"]["amount"] == "0.10"
    assert isinstance(sent["assetAmount"]["amount"], str)


@pytest.mark.parametrize(
    ("ceiling", "amount", "sent"),
    [
        ("", "1000.00", True),         # unset: today's behaviour
        ("1000.00", "1000.00", True),  # AT the ceiling
        ("1000.00", "1000.01", False),  # one minimum unit over
    ],
)
async def test_the_money_ceiling_refuses_a_payout_before_the_ledger(
    session, ceiling, amount, sent
):
    """The money ceiling on the single-payout arm, through `payout_errors` — so
    it is an ordinary form error beside the other route-level refusals, and it
    lands before `operations.start`: a refused submit is not an operation."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    with settings_override(money_ceiling=ceiling):
        async with signed_in(app) as web:
            await upload(web, purpose="transaction_support", filename="doc_1.png")
            response = await post(
                web, NEW, encoded(payout_form(amount=amount, documentIds="doc_1"))
            )
    payouts = [c for c in calls if c[1] == "/v2/payouts"]
    if sent:
        assert payouts, response.text[:400]
    else:
        assert response.status_code == 422
        assert "refuses any single amount over 1000.00" in " ".join(response.text.split())
        assert payouts == []
        # No `payout_create` row — the document upload above is its own operation.
        rows = (
            await session.execute(
                select(Operation).where(Operation.type == "payout_create")
            )
        ).scalars().all()
        assert rows == []


# --- the DOCUMENTATION_REQUIRED trap -------------------------------------------------------------


async def test_documentation_required_422_re_renders_and_the_next_submit_is_a_new_operation(session):
    """`422 DOCUMENTATION_REQUIRED` means *no payout was created*. The operation
    is `rejected` (terminal), which releases the §1 double-submit guard, so
    attaching the document and resubmitting opens a **new** operation with a new
    idempotency key rather than resolving to the old one."""
    calls: list = []
    responses = iter(
        [
            httpx.Response(
                422,
                json={
                    "type": "DOCUMENTATION_REQUIRED",
                    "title": "A supporting document is required",
                    "detail": "This payout purpose requires a supporting document.",
                    "resolution": "Upload one with purpose transaction_support.",
                    "correlationId": "corr_doc",
                },
            ),
            httpx.Response(202, json=PAYOUT),
        ]
    )
    app = make_app(
        stub(
            routes(extra={("POST", "/v2/payouts"): lambda request: next(responses)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_right_type.png")
        await upload(web, purpose="transaction_support", filename="doc_wrong_type.png")
        # A body that passes the *local* gate (a document is attached) but that
        # Conduit still refuses — the server's own answer, not ours.
        first = await post(web, NEW, encoded(payout_form(documentIds="doc_wrong_type")))
        assert first.status_code == 422
        assert "A supporting document is required" in first.text
        assert "corr_doc" in first.text
        # The operator's values are still on the re-rendered form.
        assert "INV-4471" in first.text

        second = await post(web, NEW, encoded(payout_form(documentIds="doc_right_type")))

    assert second.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    ops = (await session.execute(select(Operation).where(Operation.type == "payout_create").order_by(Operation.created_at))).scalars().all()
    assert [o.state for o in ops] == ["rejected", "confirmed"]
    assert ops[0].id != ops[1].id
    assert ops[0].idempotency_key != ops[1].idempotency_key
    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 2


async def test_an_identical_resubmit_after_a_rejection_is_still_a_new_operation(session):
    """Same trap, same body: the guard is released by terminality, not by the
    body changing (OPERATIONS_SPEC §1)."""
    calls: list = []
    responses = iter(
        [
            httpx.Response(
                422, json={"type": "DOCUMENTATION_REQUIRED", "title": "Document required"}
            ),
            httpx.Response(202, json=PAYOUT),
        ]
    )
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): lambda r: next(responses)}), calls)
    )
    body = encoded(payout_form(documentIds="doc_1"))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        await post(web, NEW, body)
        second = await post(web, NEW, body)
    assert second.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    assert len((await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalars().all()) == 2


# --- the indicative quote ----------------------------------------------------------------------


async def test_the_quote_panel_renders_per_rail_options_labelled_indicative():
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)}), calls)
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1000.00", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )

    assert response.status_code == 200
    assert "indicative only" in response.text
    assert "fedwire" in response.text and "rtp" in response.text
    assert "1020.00" in response.text and "1001.00" in response.text
    sent = json.loads(next(c for c in calls if c[1] == "/v2/quotes")[2])
    assert sent == {
        "source": {"code": "USD"},
        "destination": {"code": "USD"},
        "destinationCountry": "USA",
        "lockSide": "source",
        "amount": "1000.00",
    }


async def test_a_quote_creates_no_operation(session):
    app = make_app(stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)})))
    async with signed_in(app) as web:
        await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1000.00", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_an_already_expired_quote_says_so_on_arrival():
    app = make_app(
        stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=EXPIRED_QUOTE)}))
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1000.00", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )
    assert "already expired" in response.text


def quoted(expires_at: str) -> dict:
    """A quote claim as the panel renders it: the timestamp the operator was
    shown, plus this console's seal over the same one."""
    return {
        "quoteExpiresAt": expires_at,
        payouts.QUOTE_SEAL: payouts._sealed({"expires_at": expires_at}),
    }


async def test_a_stale_quote_blocks_the_send_server_side(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(documentIds="doc_1", **quoted("2020-01-01T00:00:00.000Z"))
            ),
        )
    assert response.status_code == 422 and "quote has expired" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_fresh_quote_does_not_block_the_send(session):
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}))
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        response = await post(
            web,
            NEW,
            encoded(payout_form(documentIds="doc_1", **quoted("2099-01-01T00:00:00.000Z"))),
        )
    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"


# --- the quote claim is sealed ---------------------------------------------
#
# The staleness check used to read a plain hidden field, so its input was under
# the control of the thing it was checking: rewriting `quoteExpiresAt` to a
# future timestamp walked past the one refusal the panel exists to produce.


async def test_the_panel_ships_the_seal_alongside_the_claim():
    """Both travel or neither: a claim with no seal is refused below, so a panel
    that rendered one without the other would be a dead form."""
    app = make_app(stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)})))
    async with signed_in(app) as web:
        panel = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1000.00", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )
    assert 'name="quoteExpiresAt"' in panel.text
    assert 'name="quoteSeal"' in panel.text
    sealed = re.search(r'name="quoteSeal" value="([^"]+)"', panel.text).group(1)
    shown = re.search(r'name="quoteExpiresAt" value="([^"]+)"', panel.text).group(1)
    # The seal is over the timestamp the operator was shown, not some other one.
    assert payouts._unsealed(sealed) == shown


async def test_a_forged_future_expiry_is_refused_and_sends_nothing(session):
    """The whole item, in one submission. A lapsed quote, rewritten by hand to a
    timestamp two years out — which before the seal sailed through, because the
    console checked the browser's own answer."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    **{
                        **quoted("2020-01-01T00:00:00.000Z"),
                        "quoteExpiresAt": "2099-01-01T00:00:00.000Z",
                    },
                )
            ),
        )
    assert response.status_code == 422 and "could not be verified" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 0


async def test_a_tampered_seal_is_refused_and_sends_nothing(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    fields = quoted("2099-01-01T00:00:00.000Z")
    body, mac = fields[payouts.QUOTE_SEAL].split(".")
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    **{**fields, payouts.QUOTE_SEAL: f"{body}.{mac[:-2]}xy"},
                )
            ),
        )
    assert response.status_code == 422 and "could not be verified" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 0


async def test_a_claim_with_no_seal_at_all_is_refused(session):
    """The drift pin. Without it, a template that rendered the panel and forgot
    the seal would make this console skip its own staleness check in silence —
    the failure would be invisible, which is the worse half of the defect."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(payout_form(documentIds="doc_1", quoteExpiresAt="2099-01-01T00:00:00.000Z")),
        )
    assert response.status_code == 422 and "could not be verified" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_submit_that_claims_no_quote_is_still_allowed(session):
    """Unchanged, and deliberately so: a quote reserves nothing and the payout is
    priced when it lands, so holding one was never a condition of sending."""
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}))
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_1.png")
        response = await post(web, NEW, encoded(payout_form(documentIds="doc_1")))
    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"


async def test_a_seal_from_another_deployments_secret_is_refused(session):
    """Signed, not merely encoded: the secret is what the refusal rests on."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    forged = sign(
        {"quoteExpiresAt": "2099-01-01T00:00:00.000Z"}, secret="not-this-console", ttl=3600
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(
                    documentIds="doc_1",
                    quoteExpiresAt="2099-01-01T00:00:00.000Z",
                    **{payouts.QUOTE_SEAL: forged},
                )
            ),
        )
    assert response.status_code == 422 and "could not be verified" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_the_browser_half_of_the_stale_guard_is_wired():
    """The countdown that disables the button lives in `static/app.js`; the panel
    is what tells it when. Both halves or neither."""
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    assert "quote-stale-note" in js and "data-expires-at" in js
    app = make_app(stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)})))
    async with signed_in(app) as web:
        panel = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )
        form_page = await web.get(NEW + ROUTE)
    assert f'data-expires-at="{QUOTE["expiresAt"]}"' in panel.text
    assert 'id="payout-submit"' in form_page.text


SEPA_ENTRY = {
    **REGISTERED,
    "id": "wlr_sepa",
    "rail": "sepa",
    "iban": "DE89370400440532013000",
    "accountNumber": None,
    "routingNumber": None,
    "legalName": "ZZZTEST Euro Group GmbH",
}


async def test_the_picker_offers_only_destinations_this_rail_can_reach():
    """The transfers screen filtered by rail family and the payout
    form did not, so a euro IBAN was offered on a fedwire payout."""
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY, recipients=[REGISTERED, SEPA_ENTRY])))
    async with signed_in(app) as web:
        response = await web.get(NEW + INTERCOMPANY_ROUTE)
    assert REGISTERED["id"] in response.text
    assert SEPA_ENTRY["id"] not in response.text


async def test_a_destination_this_rail_cannot_reach_is_refused_server_side():
    calls: list = []
    app = make_app(
        stub(routes(FEDWIRE_INTERCOMPANY, recipients=[REGISTERED, SEPA_ENTRY]), calls)
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(purpose="intercompany", whitelistRecipientId=SEPA_ENTRY["id"])
            ),
        )
    assert response.status_code == 422
    assert "cannot be paid over fedwire" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_the_quote_currency_is_the_accounts_own_not_the_browsers(session):
    """The panel used to price whatever `asset` the hidden field
    claimed, so a quote could be shown in one currency next to a payout that
    moves another. The account is now the only input, and its asset is read from
    Conduit."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)}), calls)
    )
    async with signed_in(app) as web:
        rendered = await web.get(NEW + ROUTE)
        response = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            # The USD account, plus a lie about the currency.
            encoded(
                {
                    "amount": "1000.00",
                    "virtualAccountId": VID,
                    "asset": "EUR",
                    "destinationCountry": "USA",
                }
            ),
        )
    assert response.status_code == 200
    sent = json.loads(next(c for c in calls if c[1] == "/v2/quotes")[2])
    assert sent["source"] == {"code": "USD"} and sent["destination"] == {"code": "USD"}
    # And the form no longer offers that field to lie with.
    assert 'name="asset"' not in rendered.text


async def test_a_quote_on_an_account_that_is_not_the_customers_prices_nothing():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded(
                {"amount": "1000.00", "virtualAccountId": "vac_someone_else", "destinationCountry": "USA"}
            ),
        )
    assert "Nothing to quote" in response.text
    assert [c for c in calls if c[1] == "/v2/quotes"] == []


async def test_a_quote_without_an_amount_asks_for_one_rather_than_calling():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web, f"/customers/{CID}/payouts/quote", encoded({"virtualAccountId": VID})
        )
    assert "Nothing to quote" in response.text
    assert [c for c in calls if c[1] == "/v2/quotes"] == []


# --- cancel -------------------------------------------------------------------------------------


async def test_cancel_goes_through_the_ledger(session):
    calls: list = []
    cancel_path = f"/v2/payouts/{PAYOUT['id']}/cancel"
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", cancel_path): httpx.Response(
                        200, json={**PAYOUT, "status": "cancelled"}
                    )
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/transactions/{PAYOUT['id']}/cancel")

    assert "msg=Payout+cancelled" in response.headers["HX-Redirect"]
    op = (await session.execute(select(Operation))).scalar_one()
    assert op.type == "payout_cancel" and op.state == "confirmed"
    # No body, and the idempotency key rides along.
    sent = next(c for c in calls if c[1] == cancel_path)
    assert sent[2] in (b"", b"null")


async def test_a_too_late_cancel_says_what_conduit_said(session):
    cancel_path = f"/v2/payouts/{PAYOUT['id']}/cancel"
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", cancel_path): httpx.Response(
                        422,
                        json={"type": "PAYOUT_NOT_CANCELLABLE", "title": "Already settling"},
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/transactions/{PAYOUT['id']}/cancel")
    # A3 gate M1: same for the cancel banner.
    banner = unquote_plus(response.headers["HX-Redirect"])
    assert "This payout can no longer be cancelled" in banner
    assert "Already settling" not in banner


# --- roles and CSRF --------------------------------------------------------------------------------


async def test_a_viewer_sees_the_form_read_only_and_cannot_send():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(NEW + ROUTE)
        refused = await post(web, NEW, encoded(payout_form()))
        cancel = await post(web, f"/transactions/{PAYOUT['id']}/cancel")
    assert response.status_code == 200 and "needs the <code>payout.create</code> permission" in response.text
    assert 'id="payout-form"' not in response.text
    assert refused.status_code == 403 and cancel.status_code == 403


async def test_a_viewer_may_still_ask_for_an_indicative_quote():
    """A quote reserves nothing and creates nothing — reading a price is a read."""
    app = make_app(stub(routes(extra={("POST", "/v2/quotes"): httpx.Response(201, json=QUOTE)})))
    async with signed_in(app, groups="readers") as web:
        response = await post(
            web,
            f"/customers/{CID}/payouts/quote",
            encoded({"amount": "1000.00", "virtualAccountId": VID, "destinationCountry": "USA"}),
        )
    assert response.status_code == 200 and "indicative only" in response.text


async def test_the_payout_mutations_need_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (NEW, f"/customers/{CID}/payouts/quote", f"/transactions/{PAYOUT['id']}/cancel"):
            response = await web.post(
                url,
                content=encoded(payout_form()),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403 and "CSRF" in response.text, url


async def test_the_payout_form_carries_hx_sync_against_a_double_click():
    """REWRITTEN (the transfers finding, applied to its
    twin): the old assertion pinned `hx-disabled-elt="find button[type=submit]"`
    exactly, which is the shape that carried the hazard.

    The route row (`hx-sync="this:replace"`) and this form (`this:drop`) are
    separate sync queues, so a route change *during* a submission fired a
    competing GET that swapped `main` — fresh nonce, re-enabled button — while
    the POST was still on the wire, and a second click was a second payout. The
    submit therefore reaches outside itself and freezes every control that can
    trigger that swap: the four route controls inside `#route-fields`, the row's
    own button, and the saved-contact form, which also swaps `main`
    (`#contact-first`, where it became step 2 — and the control an
    operator is most likely to touch while a payout is in flight).
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)
    assert 'hx-sync="this:drop"' in response.text
    guard = re.search(r'id="payout-form"[^>]*hx-disabled-elt="([^"]*)"', response.text)
    assert guard, "the submit form lost its disabled-elt guard"
    for control in (
        "find button[type=submit]",
        # The customer control: inside the route row but
        # OUTSIDE #route-fields, with its own change clause that swaps `main`.
        "#customer_id",
        "#route-fields select",
        "#route-fields input",
        "#route-row button",
        "#contact-first button",
        # The contact box itself, not just its button: its own `change` clause
        # swaps `main` — the customer control's lesson, one control
        # later.
        "#counterparty",
    ):
        assert control in guard.group(1), f"{control} is live during a money POST"


async def test_every_control_that_swaps_main_is_frozen_by_the_payout_submit():
    """The other direction of the same rule, and the one that survives a
    refactor: whatever the guard *names*, every affordance on this page that
    replaces `main` must be covered by it — because that swap is what re-arms
    the send button mid-flight. Read off the rendered page rather than from a
    list kept by hand here.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(NEW + ROUTE)).text
    guard = re.search(r'id="payout-form"[^>]*hx-disabled-elt="([^"]*)"', html).group(1)

    # Every form on the page that swaps `main`, other than the submit itself.
    swappers = [
        block
        for block in html.split("<form ")[1:]
        if 'hx-target="main"' in block.split(">")[0] and 'id="payout-form"' not in block.split(">")[0]
    ]
    assert len(swappers) == 2, "the page's main-swapping forms changed — re-read the guard"
    # The route row's controls and the contact step's are both named. A scope
    # selector counts: `#route-fields select` covers a fifth route control the
    # day one is added, which an id list would not.
    for scope in ("#customer_id", "#route-fields", "#route-row", "#contact-first"):
        assert scope in guard, f"{scope} can swap main while a payout is in flight"

    # Per-trigger coverage, not substring luck (the finding:
    # `#route-row` was satisfied by `#route-row button` while the customer INPUT
    # — outside `#route-fields`, its own `change` clause — stayed live). Every
    # source named by ANY of these forms' triggers must be frozen: directly by
    # id, or — for a wrapper span — by the scoped select/input pair.
    #
    # Read from every swapper rather than from the route row alone:
    # the contact step carries its own `change from:#counterparty`, and a sweep
    # that parsed one form's trigger would have missed it — the same shape of gap
    # it was found one level up.
    sources = re.findall(r"change from:#([\w-]+)", " ".join(swappers))
    assert len(sources) >= 3, "a main-swapping form lost its change trigger — re-read the guard"
    for source in sources:
        covered = f"#{source}" in guard or (
            f"#{source} select" in guard and f"#{source} input" in guard
        )
        assert covered, f"trigger #{source} can swap main while a payout is in flight"


async def test_an_incomplete_route_is_sent_back_to_the_picker():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, NEW, encoded({"purpose": "payroll"}))
    assert "Pick a purpose and a route first" in unquote_plus(response.headers["HX-Redirect"])


async def test_an_unreadable_whitelist_is_not_read_as_no_entries(session):
    """"Could not be read" and "there are none" are different facts, and only one
    of them means the operator should go and register something."""
    calls: list = []
    app = make_app(
        stub(
            {
                ("GET", "/v2/payouts/requirements"): httpx.Response(
                    200, json=FEDWIRE_INTERCOMPANY
                ),
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
                ("GET", WHITELIST_PATH): httpx.Response(
                    500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                ),
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            NEW,
            encoded(payout_form(purpose="intercompany", whitelistRecipientId=REGISTERED["id"])),
        )
    assert response.status_code == 422
    assert "Conduit refused this: SERVER_ERROR" in response.text  # A3
    assert "could not be read" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


# --- optional supporting documents --------------------------------------
#
# Attaching a document proactively must be easy even where
# discovery does not ask for one. Nothing about the contract moves — the chips
# are the same `documentIds`, and they ride the same `documents[]`.


async def test_an_optional_route_still_offers_the_widget_collapsed():
    optional = {
        **FEDWIRE_BUSINESS,
        "documentation": {**FEDWIRE_BUSINESS["documentation"], "required": False},
    }
    app = make_app(stub(routes(optional)))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)

    # No gate…
    assert "This route requires a supporting document" not in response.text
    # …and no dead end either: the widget is there, closed, with what Conduit
    # says it would accept.
    assert "<details class=\"attach\">" in response.text
    assert "Attach a supporting document (optional)" in response.text
    assert 'data-purpose="transaction_support"' in response.text
    assert "bank_verification_letter" in response.text
    # Closed by default: the attribute that would open it is absent.
    assert "<details class=\"attach\" open>" not in response.text


async def test_a_document_attached_to_an_optional_route_rides_the_same_documents_array():
    optional = {
        **FEDWIRE_BUSINESS,
        "documentation": {**FEDWIRE_BUSINESS["documentation"], "required": False},
    }
    calls: list = []
    app = make_app(
        stub(
            routes(optional, extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_voluntary_1.png")
        response = await post(web, NEW, encoded(payout_form(documentIds="doc_voluntary_1")))

    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert sent["documents"] == ["doc_voluntary_1"]


async def test_an_optional_route_sends_nothing_when_nothing_is_attached():
    """The other half of "optional": an empty widget is not a refusal, and no
    `documents` key is invented for a payout that carries none."""
    optional = {
        **FEDWIRE_BUSINESS,
        "documentation": {**FEDWIRE_BUSINESS["documentation"], "required": False},
    }
    calls: list = []
    app = make_app(
        stub(
            routes(optional, extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, NEW, encoded(payout_form()))

    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert "documents" not in sent


# --- attachments are ledger-checked ---------------------------


async def test_a_payout_cannot_attach_a_document_this_actor_did_not_upload(session):
    """The money-path half of the RFI rule. `documents[]` rides into
    `POST /v2/payouts`, so a `doc_` id on this form is an assertion until it is
    matched to a confirmed `document_upload` of this operator's, for this
    purpose — otherwise a guessed id attaches someone else's file to a payment.
    """
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_mine.png")
        refused = await post(
            web, NEW, encoded(payout_form(documentIds=["doc_mine", "doc_someone_elses"]))
        )

    assert refused.status_code == 422
    assert "could not be matched" in refused.text
    # Refused before the ledger: no payout operation, nothing on the wire.
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).scalars().all() == []
    # …and the operator's values survive the refusal, as with every other 422.
    assert "021000021" in refused.text


async def test_an_rfi_document_is_not_payout_evidence(session):
    """Purpose is half the rule on this path too: a file uploaded to answer a
    compliance question is not a payment's supporting document, and attaching it
    would send Conduit a file chosen for something else."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="rfi_response", filename="doc_answer.png")
        refused = await post(web, NEW, encoded(payout_form(documentIds="doc_answer")))

    assert refused.status_code == 422 and "could not be matched" in refused.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


# --- the rail/asset guard (live-proven) ---------------------------------------
#
# `tests/e2e/09_rail_asset_probe.py` establishes the behaviour these tests encode:
# Conduit **accepts** a EUR-funded fedwire payout at create (202) and it **fails
# after review** with `rail_unavailable` — no funds moved, one review cycle and
# an operator's day burnt. So the console refuses it before the ledger row
# exists: this is not mirroring a 4xx, it is declining a proven-doomed attempt.

from app.models import AuditEvent  # noqa: E402
from tests.payments_fixtures import EUR_ACTIVE, EUR_VID  # noqa: E402

EUR_ROUTES = {("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT, EUR_ACTIVE])}


def test_the_rail_asset_table_is_a_definition_not_a_guess():
    from app import payments

    # us settles USD, sepa settles EUR, uk_domestic settles GBP, swift pins
    # nothing — Conduit decides.
    assert payments.rail_asset("fedwire") == "USD"
    assert payments.rail_asset("ach") == "USD"
    assert payments.rail_asset("sepa") == "EUR"
    assert payments.rail_asset("chaps") == "GBP"
    assert payments.rail_asset("faster_payments") == "GBP"
    assert payments.rail_asset("swift") == ""
    # A rail this build has never heard of pins nothing: the console does not
    # guess a currency for a corridor it does not know.
    assert payments.rail_asset("something_new") == ""

    assert payments.doomed_rail("fedwire", "EUR") == "USD"
    assert payments.doomed_rail("fedwire", "USD") == ""
    assert payments.doomed_rail("sepa", "USD") == "EUR"
    assert payments.doomed_rail("chaps", "USD") == "GBP"
    assert payments.doomed_rail("swift", "EUR") == ""
    # Nothing known on either side is never a refusal.
    assert payments.doomed_rail("fedwire", "") == ""


async def test_a_eur_funded_fedwire_payout_is_refused_before_the_ledger(session):
    calls: list = []
    app = make_app(stub(routes(extra=EUR_ROUTES), calls))
    async with signed_in(app) as web:
        form = await web.get(NEW + ROUTE)
        await upload(web, purpose="transaction_support", filename="doc_rail_1.png")
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(
                    virtualAccountId=EUR_VID,
                    documentIds="doc_rail_1",
                    intent=intent(form.text),
                )
            ),
        )

    assert response.status_code == 422
    assert "fedwire sends USD" in response.text
    assert "rail_unavailable" in response.text
    assert "no funds move" in response.text
    # Pre-ledger: no payout operation row (the upload above has its own), and
    # nothing on the wire.
    assert (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).first() is None
    assert not [c for c in calls if c[0] == "POST" and c[1] == "/v2/payouts"]


async def test_the_matching_currency_is_untouched(session):
    app = make_app(
        stub(
            routes(extra={**EUR_ROUTES, ("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)})
        )
    )
    async with signed_in(app) as web:
        form = await web.get(NEW + ROUTE)
        await upload(web, purpose="transaction_support", filename="doc_rail_2.png")
        response = await post(
            web,
            NEW,
            encoded(
                payout_form(documentIds="doc_rail_2", intent=intent(form.text))
            ),  # USD account
        )
    assert response.status_code in (204, 302, 303)
    assert (await session.execute(select(Operation))).first() is not None


async def test_the_form_disables_the_doomed_account_and_says_why():
    app = make_app(stub(routes(extra=EUR_ROUTES)))
    async with signed_in(app) as web:
        html = (await web.get(NEW + ROUTE)).text

    assert f'value="{EUR_VID}" disabled' in html
    assert "fedwire sends USD" in html
    # The account that works is still selectable.
    assert f'value="{VID}" disabled' not in html


# --- the guided hand-off ---------------------------------------------------
#
# The composite the refusal seems to ask for is unbuildable: a fiat→fiat
# conversion redeemed with `autoPayout` is `422 INVALID_ORDER_COMBO`
# (`tests/e2e/11_composite_probe.py`, evidence in `sandbox_evidence/composite_*`).
# So the refusal becomes a *path* instead — convert, then pay — and these tests
# hold both halves of it: where the guard refuses AND a conversion can land, the
# link appears; where no conversion can land, the fact appears and the link does
# not, because a link into a flow that dead-ends is worse than no link.


def links_in(html: str) -> str:
    """The page with its entities decoded and its wrapping collapsed, so an
    assertion is about a sentence rather than about where the template happened
    to break the line."""
    return " ".join(html.replace("&amp;", "&").split())


async def test_a_refused_funding_pick_offers_the_conversion_that_would_serve_it():
    app = make_app(stub(routes(extra=EUR_ROUTES)))
    async with signed_in(app) as web:
        html = links_in((await web.get(NEW + ROUTE)).text)

    # Convert, prefilled with what the operator holds and where it has to land —
    # the two accounts, never an amount this console computed.
    assert (
        f"/customers/{CID}/convert?purpose=payment_for_goods_or_services&rail=fedwire"
        f"&recipientType=business&destinationCountry=USA&source={EUR_VID}&destination={VID}"
        in html
    )
    assert "Convert EUR into USD</a>" in html
    # Two operations, two settlements — stated, and never as one step.
    assert "Two operations, each with its own settlement" in html
    # An INSTRUCTION, not a settlement claim:
    # nothing enforces that Conduit holds an early payout for unsettled funds,
    # so the sentence tells the operator what to do rather than asserting what
    # the system does.
    assert "fund the payout only after it has" in html
    assert "waits for settled funds" not in html
    assert "in one step" not in html
    # The typed amount is destination-side money and the conversion's own rate
    # decides what lands: it does not ride the link, and the copy says so.
    assert "The amount does not travel" in html
    link = html.split(f"/customers/{CID}/convert?", 1)[1].split('"', 1)[0]
    assert "amount" not in link and "intent" not in link


async def test_no_conversion_is_offered_when_none_can_land():
    """The other half. The customer holds EUR only — Convert has nothing to
    convert *into*, so the sentence states that instead of pointing at a flow
    whose own empty state would be the operator's next surprise."""
    app = make_app(
        stub(
            routes(
                extra={("GET", f"/v2/customers/{CID}/virtual-accounts"): page([EUR_ACTIVE])}
            )
        )
    )
    async with signed_in(app) as web:
        html = links_in((await web.get(NEW + ROUTE)).text)

    assert "fedwire sends USD" in html  # the refusal is unchanged
    assert f"/customers/{CID}/convert?" not in html
    assert "holds no active USD account for a conversion to land in" in html
    assert f"/customers/{CID}/request-account" in html


async def test_a_route_that_refuses_nothing_offers_no_hand_off():
    """The third state, and the one that keeps the offer honest: a USD account on
    a fedwire route is fundable, so there is nothing to convert and nothing to
    say about converting."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = links_in((await web.get(NEW + ROUTE)).text)

    assert f"/customers/{CID}/convert?" not in html
    assert "Two operations" not in html


async def test_a_swift_route_disables_nothing():
    """`swift` pins no currency — Conduit decides the corridor, so the console
    has nothing to refuse."""
    app = make_app(
        stub(
            routes(
                requirements=SEPA_BUSINESS,
                extra={
                    **EUR_ROUTES,
                    ("GET", f"/v2/customers/{CID}/virtual-accounts"): page(
                        [USD_ACCOUNT, EUR_ACTIVE]
                    ),
                },
            )
        )
    )
    async with signed_in(app) as web:
        html = (
            await web.get(
                f"{NEW}?purpose=payment_for_goods_or_services&rail=swift"
                "&recipientType=business&destinationCountry=DEU"
            )
        ).text
    assert "disabled" not in html.split('name="virtualAccountId"', 1)[1].split("</select>", 1)[0]


# --- the Transact fork ----------------------------------------------------------------------


FORK = f"/customers/{CID}/payouts"


async def test_the_fork_asks_one_or_many_before_any_purpose():
    """The restructure's first question. It has to come before the purpose and
    before any requirements read, because its two answers are different flows —
    a form and a file — and a batch's route has no purpose in it at all."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(FORK)

    assert response.status_code == 200
    assert ">Single payment</a>" in response.text
    assert ">Batch payment</a>" in response.text
    assert f'href="{NEW}"' in response.text
    assert f'href="/customers/{CID}/batches/new"' in response.text
    # No purpose, no rail, no requirements: the fork decides nothing about the
    # payment, so it asks Conduit nothing ABOUT the payment. It makes exactly
    # one read, and it is the bounded customers one that fills its
    # own source-customer picker — this is where the ribbon lands, so the fork
    # has to be able to ask who.
    assert "payment_for_goods_or_services" not in response.text
    assert [c[1] for c in calls] == ["/v2/customers"]
    # The CTA grammar row (DESIGN.md, 2026-08-31): a default and its alternative,
    # so exactly one primary. Two equal buttons would ask the operator to decide
    # which one is normal every time they arrive.
    assert f'href="{NEW}">Single payment</a>' in response.text
    assert response.text.count('class="btn primary"') == 1


async def test_the_launcher_keeps_three_equal_verbs_and_no_primary():
    """The other half of the same grammar row: `/orders`' move-money launcher is
    three different intentions, not a default and its alternative, so promoting
    one would be this console guessing which kind of money movement an operator
    came for."""
    from tests.web_harness import make_app as _make_app

    app = _make_app(stub(routes(extra={("GET", "/v2/customers"): page([])})))
    async with signed_in(app) as web:
        launcher = (await web.get("/orders")).text.split('id="move-money"')[1].split("</div>")[0]
    assert launcher.count("data-launch=") == 3
    assert "primary" not in launcher


async def test_a_viewer_reaches_the_fork_and_is_told_which_arm_is_read_only():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(FORK)
    assert response.status_code == 200
    assert "needs the <code>batch.upload</code> permission" in response.text
    # A viewer may still read batch reports.
    assert f'href="/customers/{CID}/batches"' in response.text


async def test_the_single_payment_form_signposts_the_batch_arm_with_its_corridor():
    """The template/upload tray left this form (a batch has no purpose), and what
    replaces it carries the corridor forward so the batch screen opens on it."""
    app = make_app(stub(routes(FEDWIRE_BUSINESS)))
    async with signed_in(app) as web:
        response = await web.get(NEW + ROUTE)

    assert "Send them as a batch" in response.text
    assert f"/customers/{CID}/batches/new?rail=fedwire" in response.text
    # The tray itself is gone: no file input and no template link on this page.
    assert "data-batch-upload" not in response.text
    assert "template.csv" not in response.text


def test_payout_errors_requires_an_explicit_rail_decision():
    """The carried item. `rail` used to default to `""`, which meant a caller
    that simply forgot it got a payout validated with the doomed-rail guard
    silently off — the one refusal here that is not mirroring a Conduit 4xx
    (Conduit answers 202 and then fails after review). Forgetting must not be
    spellable, so the parameter has no default: `rail=None` is the opt-out and it
    has to be typed."""
    import inspect as _inspect

    import pytest

    from app import forms, payments

    signature = _inspect.signature(payments.payout_errors)
    rail = signature.parameters["rail"]
    assert rail.default is _inspect.Parameter.empty
    assert rail.kind is _inspect.Parameter.KEYWORD_ONLY

    model = payments.payout_model(FEDWIRE_BUSINESS)
    values = forms.FormValues()
    account = {"id": VID, "asset": {"code": "EUR"}}
    with pytest.raises(TypeError):
        payments.payout_errors(model, values, account=account, amount="1.00")

    # The opt-out is silent about the rail, and stating one is the guard.
    opted_out = payments.payout_errors(
        model, values, account=account, amount="1.00", rail=None
    )
    assert not [m for m in opted_out.form if "fedwire sends USD" in m.detail]
    guarded = payments.payout_errors(
        model, values, account=account, amount="1.00", rail="fedwire"
    )
    assert [m for m in guarded.form if "fedwire sends USD" in m.detail]


def test_forgetting_the_currency_guard_is_unspellable():
    """SUPERSEDED `test_the_transfers_screen_is_the_documented_opt_out`: that
    test named the transfers screen as the one deliberate `rail=None` caller.
    The transfers screen sends on the virtual-account arm — no
    rail exists there and it does not call `payout_errors` at all — so the pin
    would have asserted a call site that no longer exists.

    The rule it protected is the one kept here, and unweakened: `rail` has **no
    default**, so a new caller cannot get the doomed-rail guard silently switched
    off by simply not thinking about it. Every live caller is checked to state
    its choice.
    """
    import ast
    import inspect as _inspect

    from app import payments

    parameter = _inspect.signature(payments.payout_errors).parameters["rail"]
    assert parameter.default is _inspect.Parameter.empty
    assert parameter.kind is _inspect.Parameter.KEYWORD_ONLY

    callers = [
        node
        for path in (Path(__file__).resolve().parents[1] / "app").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "payout_errors"
    ]
    assert callers, "no caller found — this pin has stopped testing anything"
    for call in callers:
        assert "rail" in [kw.arg for kw in call.keywords]


async def test_a_registered_destination_deep_link_preselects_it_on_the_form():
    """The Contacts page hands a just-registered destination straight to this
    form (`?purpose=intercompany&whitelistRecipientId=…`). It used to hand it to
    the transfers screen, which pays no whitelist entry at all —
    so the deep link came here, and the picker has to honour it."""
    app = make_app(stub(routes(FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        response = await web.get(
            NEW + INTERCOMPANY_ROUTE + f"&whitelistRecipientId={REGISTERED['id']}"
        )
    assert f'value="{REGISTERED["id"]}" selected' in response.text


async def test_registrations_on_another_rail_are_not_reported_as_none():
    """Three states, not two. A customer with a `us` registration, asked for a
    `sepa` payout, has registered recipients — just none this rail can pay.
    Saying "No registered recipient for this customer" is false, and it leaves an
    operator who registered one last week unable to tell whether they are adding
    a rail or creating a counterparty from scratch (cross-cutting honesty sweep,
    residual of c5823eb).
    """
    sepa_entry = {
        **REGISTERED,
        "id": "wlr_sepa_only",
        "rail": "sepa",
        "accountNumber": None,
        "iban": "DE89370400440532013000",
        "bic": "COBADEFFXXX",
    }
    app = make_app(stub(routes(requirements=FEDWIRE_INTERCOMPANY, recipients=[sepa_entry])))
    async with signed_in(app) as web:
        page = await web.get(NEW + INTERCOMPANY_ROUTE)

    assert page.status_code == 200
    assert "No <strong>registered</strong> recipient" not in page.text, (
        "claimed the customer has none while holding a sepa registration"
    )
    assert "1 registered recipient" in page.text
    assert "payable over" in page.text


async def test_a_customer_with_genuinely_no_registrations_still_says_so():
    """The pair. Removing the false claim must not remove the true one — that is
    how the vacuous half of this family got written the first time."""
    app = make_app(stub(routes(requirements=FEDWIRE_INTERCOMPANY, recipients=[])))
    async with signed_in(app) as web:
        page = await web.get(NEW + INTERCOMPANY_ROUTE)

    assert page.status_code == 200
    assert "No <strong>registered</strong> recipient" in page.text
    # ...and NOT the rail sentence: there is no other-rail registration to name.
    assert "payable over" not in page.text


# --- the composite probe's guards (hermetic) -------------------------------------------
#
# `tests/e2e/11_composite_probe.py` is what keeps the hand-off copy above
# honest: it re-asks whether a fiat→fiat conversion has started accepting an
# `autoPayout`, which is the one API change that would make the composite
# buildable and this two-step copy wrong. CI cannot run it — it needs sandbox
# credentials — so what CI checks is that it refuses to run anywhere else.


def test_the_composite_probe_refuses_anything_that_is_not_the_sandbox():
    """`--check-config` runs the two refusals and exits before anything is sent,
    which is the only way to prove a guard fires without letting the script talk
    to Conduit. The idiom (and this test) are `10_va_transfer_probe.py`'s."""
    import os
    import subprocess
    import sys

    script = Path(__file__).parent / "e2e" / "11_composite_probe.py"

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


def test_the_composite_probe_never_executes_the_order_it_might_create():
    """The one thing this script must not do. An order create is free — it is the
    call that 422s on the baseline path — but `POST /v2/orders/{id}/execute` is
    the call that spends, and a probe that reached it would be paying a recipient
    to find out whether it could. The check is on the source, because the script
    exits on missing credentials by design and cannot be imported."""
    script = (Path(__file__).parent / "e2e" / "11_composite_probe.py").read_text()
    # Past the module docstring (which names the call in order to say it is never
    # made) and with the comments dropped, what is left is what runs.
    code = "\n".join(
        line
        for line in script.split('"""', 2)[-1].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/execute" not in code and "simulate" not in code
    # …and it cancels what it creates, on the one path where a create succeeds.
    assert "/cancel" in code
    assert "ZZZTEST" in code


# --- the flow starts at the action --------------------------------------------------

GLOBAL_FORK = "/payouts"
GLOBAL_NEW = "/payouts/new"


async def test_the_global_fork_and_the_scoped_fork_both_render():
    """The fork is the Transact ribbon's landing page now. With no customer it
    still asks its one question — and it says which link the answer unlocks
    rather than pointing at a customer-less batch URL."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        bare = await web.get(GLOBAL_FORK)
        scoped = await web.get(f"/customers/{CID}/payouts")
        picked = await web.get(f"{GLOBAL_FORK}?customer_id={CID}")

    assert bare.status_code == scoped.status_code == picked.status_code == 200
    assert 'id="customer_id"' in bare.text
    # Batches are customer-scoped at upload by design, so their entry appears
    # once a customer is picked and says so when one is not.
    assert f"/customers/{CID}/batches/new" in scoped.text
    assert f"/customers/{CID}/batches/new" in picked.text
    assert "/batches/new" not in bare.text
    assert "Pick the customer" in " ".join(bare.text.split())


async def test_the_global_payout_form_and_the_scoped_one_are_the_same_form():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        scoped = await web.get(NEW + ROUTE)
        picked = await web.get(f"{GLOBAL_NEW}?customer_id={CID}&" + ROUTE.lstrip("?"))

    for response in (scoped, picked):
        assert response.status_code == 200
        row = response.text.split('id="route-row"')[1].split("</form>")[0]
        assert f'hx-get="{GLOBAL_NEW}"' in row and f'action="{GLOBAL_NEW}"' in row
        assert "change from:#customer_id" in row
        assert f'value="{CID}"' in row
        assert VID in row  # this customer's funding accounts


async def test_the_payout_form_with_no_customer_asks_for_one_and_asserts_nothing():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(GLOBAL_NEW)

    assert response.status_code == 200
    body = " ".join(response.text.split())
    assert "Pick the customer" in body
    assert "no active virtual account" not in body
    assert "virtual accounts could not be read" not in body
    assert [c for c in calls if "virtual-accounts" in c[1]] == []
    assert [c for c in calls if "whitelist-recipients" in c[1]] == []


async def test_the_payout_flow_keeps_the_ribbon_on_transact_at_the_new_route():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (GLOBAL_FORK, GLOBAL_NEW, NEW + ROUTE):
            html = (await web.get(url)).text
            assert '<div class="group on" role="group" aria-labelledby="grp-transact">\n    <p class="group-head" id="grp-transact">Transact</p>' in html
            assert (
                '<a class="item on" href="/payouts" aria-current="page">Send a payout</a>'
                in html
            )


async def test_the_reads_this_page_makes_are_the_five_it_names(session):
    """The read budget, pinned where it can drift.

    Three were already here — the route's requirements, the customer's whitelist
    and their funding accounts — and a later round added the fourth: ONE bounded
    customers read for the source-customer picker at the head of the row. It is
    gathered with the accounts read (`with_customer_names`), never serialised
    behind it, and never one per row.

    **The worst case is FIVE, and only on the `?counterparty=` path:**
    one read of the transaction the console's own trail says was the last payment
    to that contact, which is what the amount, the purpose and the settled rail
    are prefilled from. It is serial by necessity — the purpose it answers is an
    input to the requirements read below it — and it happens at most once per
    render, never once per contact or once per row. Everything else still applies:
    the whitelist only on a gated route, the requirements only on a complete one,
    the two customer-scoped reads only once a customer is named (which is why the
    bare `/payouts/new` below makes exactly one), and the transaction read only
    when a contact is named AND the trail has a `counterparty.used` row for it
    carrying a transaction — `tests/test_pay_a_contact.py` pins that no other
    shape of trail row can produce it.
    """
    calls: list = []
    app = make_app(stub(routes(requirements=FEDWIRE_INTERCOMPANY), calls))
    async with signed_in(app) as web:
        await web.get(f"{GLOBAL_NEW}?customer_id={CID}&" + INTERCOMPANY_ROUTE.lstrip("?"))
    assert sorted(c[1] for c in calls) == sorted(
        [
            "/v2/payouts/requirements",
            WHITELIST_PATH,
            "/v2/customers",
            f"/v2/customers/{CID}/virtual-accounts",
        ]
    )

    # …and the fifth, on the resend path: a saved contact with a payment on the
    # trail. Same page, same route, one more read and no more.
    from tests.test_contact_filter import used
    from tests.test_counterparties import store

    cp_id = await store(session)
    await used(session, cp_id, transaction=PAYOUT["id"])
    calls.clear()
    resend = make_app(
        stub(
            routes(
                requirements=FEDWIRE_INTERCOMPANY,
                extra={
                    ("GET", f"/v2/transactions/{PAYOUT['id']}"): httpx.Response(
                        200, json=PAYOUT
                    )
                },
            ),
            calls,
        )
    )
    async with signed_in(resend) as web:
        await web.get(
            f"{GLOBAL_NEW}?customer_id={CID}&counterparty={cp_id}&"
            + INTERCOMPANY_ROUTE.lstrip("?")
        )
    assert sorted(c[1] for c in calls) == sorted(
        [
            f"/v2/transactions/{PAYOUT['id']}",
            "/v2/payouts/requirements",
            WHITELIST_PATH,
            "/v2/customers",
            f"/v2/customers/{CID}/virtual-accounts",
        ]
    )

    calls.clear()
    async with signed_in(app) as web:
        await web.get(GLOBAL_NEW)
    assert [c[1] for c in calls] == ["/v2/customers"]


# --- one render, one payment: what the nonce may and may not answer for -----------------------


async def test_a_resubmit_with_a_changed_amount_says_it_was_not_sent(session):
    """The silent case. A spent nonce resolves to the operation
    its render already made — right, and previously indistinguishable from
    "sent": the operator edited the amount, pressed the button, and landed on a
    confirmed payout for the *old* amount with nothing saying so."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_a2.png")
        form = await web.get(NEW + ROUTE)
        nonce = intent(form.text)
        first = await post(web, NEW, encoded(payout_form(documentIds="doc_a2", intent=nonce)))
        assert first.status_code in (204, 302, 303)
        again = await post(
            web,
            NEW,
            encoded(payout_form(documentIds="doc_a2", intent=nonce, amount="9000.00")),
        )
    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 1  # the wire, not the states
    landing = unquote_plus(again.headers.get("HX-Redirect") or again.headers["location"])
    assert "was NOT sent" in landing
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 1


async def test_the_identical_resubmit_keeps_its_quiet_redirect(session):
    """The non-vacuity guard on the flash above: a mechanical re-POST of the
    *same* body is the ordinary double-click, and it must stay silent."""
    app = make_app(stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)})))
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_a2b.png")
        form = await web.get(NEW + ROUTE)
        body = encoded(payout_form(documentIds="doc_a2b", intent=intent(form.text)))
        await post(web, NEW, body)
        again = await post(web, NEW, body)
    landing = unquote_plus(again.headers.get("HX-Redirect") or again.headers["location"])
    assert "was NOT sent" not in landing


async def test_a_nonce_that_reached_another_kind_of_operation_is_refused(session):
    """`by_intent` resolved on the nonce alone, so a nonce already spent on an
    order execution answered a payout submit with that order. Refused rather
    than filtered: filtering reads as "this nonce reached nothing", and the
    submit would then mint a second operation under a spent nonce."""
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
        await upload(web, purpose="transaction_support", filename="doc_a3.png")
        response = await post(
            web, NEW, encoded(payout_form(documentIds="doc_a3", intent=minted_intent(nonce)))
        )
    assert response.status_code == 422
    assert "belongs to something else" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 0


async def test_a_spent_cancel_nonce_replayed_at_another_payout_is_refused(session):
    """`by_intent` is scoped to the operation *type*, not the resource, so
    the nonce a cancel of payout A spent answered a cancel of payout B — and the
    route flashed "Payout cancelled." for a payout nobody cancelled, which is
    still pending and will settle."""
    other = "txn_payout_2"
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/payouts/{PAYOUT['id']}/cancel"): httpx.Response(
                        200, json={**PAYOUT, "status": "cancelled"}
                    ),
                    # Stubbed to succeed on purpose: the only thing that may
                    # differ between the two submits is whether the call is made.
                    ("POST", f"/v2/payouts/{other}/cancel"): httpx.Response(
                        200, json={**PAYOUT, "id": other, "status": "cancelled"}
                    ),
                }
            ),
            calls,
        )
    )
    nonce = minted_intent()
    async with signed_in(app) as web:
        first = await post(
            web, f"/transactions/{PAYOUT['id']}/cancel", encoded({"intent": nonce})
        )
        replay = await post(web, f"/transactions/{other}/cancel", encoded({"intent": nonce}))

    assert "msg=Payout+cancelled" in first.headers["HX-Redirect"]
    # Nothing reached the wire for B, and no operation names B either — asserted
    # before the banner because both of these held *before* the guard existed
    # too. B was never cancelled; only the sentence was wrong.
    assert [c for c in calls if c[1] == f"/v2/payouts/{other}/cancel"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(
                Operation.request_path == f"/v2/payouts/{other}/cancel"
            )
        )
    ) == 0
    landing = unquote_plus(replay.headers.get("HX-Redirect") or replay.headers["location"])
    assert "Payout cancelled." not in landing
    assert "had already been used" in landing


# --- a nonce this server never minted -----------------------------


async def test_a_self_minted_nonce_the_server_never_issued_is_refused(session):
    """`intent_of` parsed *any* uuid out of the field, so a submitter could
    pre-claim a nonce it had computed itself — a value no render of this console
    ever put on a page. That is the channel the batch-dispatch guard closes at
    the far end; this closes it at the door. Nothing may reach the wire."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_forged.png")
        response = await post(
            web,
            NEW,
            encoded(payout_form(documentIds="doc_forged", intent=str(uuid.uuid4()))),
        )

    assert response.status_code == 422, response.text
    assert "nothing was sent" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 0


async def test_a_tampered_submission_token_is_refused(session):
    """The same refusal for a real token whose payload was edited in the browser.
    A seal that no longer verifies is *present and invalid*, which is a refusal —
    not the absent case, which falls through to the request-hash guard."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_tampered.png")
        form = await web.get(NEW + ROUTE)
        minted = intent(form.text)
        body, signature = minted.split(".") if "." in minted else (minted, "")
        forged = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{signature}"
        assert forged != minted
        response = await post(
            web, NEW, encoded(payout_form(documentIds="doc_tampered", intent=forged))
        )

    assert response.status_code == 422, response.text
    assert "nothing was sent" in response.text
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_submission_with_no_token_at_all_still_goes_through(session):
    """The over-trigger guard. "Refuse a nonce we did not mint" must not become
    "refuse everything": an absent `intent` is a documented non-error that falls
    back to the request-hash guard, and hundreds of form posts in this suite —
    and every non-form caller — depend on it."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_bare.png")
        response = await post(web, NEW, encoded(payout_form(documentIds="doc_bare")))

    assert response.status_code in (204, 302, 303), response.text
    assert len([c for c in calls if c[1] == "/v2/payouts"]) == 1
