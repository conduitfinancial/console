"""Pay a contact: the picker, the hand-off, and the resend prefill.

The ask was "quick actions such as pay a contact … pick a contact and
just resend the funds", and the whole of it is GETs. **Nothing here pays
anybody**: the picker turns a name into the customer-scoped payout URL
already had, the form prefills more of itself than it used to, and the only thing
that sends money is the same `POST /customers/{id}/payouts/new` with the same
ceiling, nonce, seal and gates. The tests below are mostly about what the console
refuses to claim:

* an archived contact is not offered and is refused if named anyway;
* a contact of another customer resolves to nothing on the form, exactly as a
  made-up id does;
* a prefill comes only from a transaction the trail binds to *this* contact —
  `counterparty.used` carries the contact's id, and nothing else is a link;
* what could not be read is said in a sentence rather than filled in: no earlier
  payment, no rail the record can name, no account in that currency.
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest

from sqlalchemy import select

from app import counterparties, payments, projections
from app.models import AuditEvent
from app.web import payouts
from app.models import Counterparty
from tests.payments_fixtures import (
    CID,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    EUR_ACTIVE,
    OTHER_CID,
    PAYOUT,
    SEPA_BUSINESS,
    USD_ACCOUNT,
    VID,
    encoded,
    page,
    payout_form,
)
from tests.conftest import settings_override
from tests.test_contact_filter import used
from tests.test_counterparties import ACCOUNT_NUMBER, field_value, saving, store
from tests.web_harness import (
    documents_stub,
    forbidden_affordances,
    make_app,
    post,
    signed_in,
    signed_in_as,
    stub,
    upload,
)

PICKER = "/payouts/contact"
NEW = f"/customers/{CID}/payouts/new"

# The payment the trail points at: a real withdrawal shape, in a currency and on
# a rail the fixtures can prove — `fedwireImad` is Conduit's own record that this
# one went over Fedwire, which is the only thing on a read-back that can name a
# payout rail (`destination.recipient.rail` is the whitelist FAMILY).
SENT = {
    **PAYOUT,
    "id": "txn_sent_1",
    "hasRfi": False,
    "purpose": "payment_for_goods_or_services",
    "destination": {
        "type": "external_bank",
        "assetAmount": {"code": "USD", "amount": "987.50"},
        "fedwireImad": "20260827MMQFMP0100001",
        "recipient": {
            "rail": "us",
            "type": "business",
            "accountNumber": ACCOUNT_NUMBER,
            "routingNumber": "021000021",
            "legalName": "ZZZTEST Globex Supplies LLC",
        },
    },
}


def routes(requirements=None, accounts=(USD_ACCOUNT,), transactions=(SENT,), extra=None):
    return {
        ("GET", "/v2/payouts/requirements"): httpx.Response(
            200, json=requirements or FEDWIRE_BUSINESS
        ),
        ("GET", "/v2/customers"): page([{"id": CID, "legalName": "ZZZTEST Console EOOD"}]),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page(list(accounts)),
        ("GET", f"/v2/customers/{OTHER_CID}/virtual-accounts"): page(list(accounts)),
        ("GET", f"/v2/customers/{CID}/whitelist-recipients"): page([]),
        ("POST", "/v2/documents"): documents_stub,
        **{
            ("GET", f"/v2/transactions/{t['id']}"): httpx.Response(200, json=t)
            for t in transactions
        },
        **(extra or {}),
    }


def options(html: str) -> list[str]:
    """The picker's datalist, as the browser would offer it."""
    found = re.search(r'<datalist id="known-contacts">(.*?)</datalist>', html, re.S)
    return re.findall(r"<option [^>]*>([^<]*)</option>", found.group(1)) if found else []


def selected(html: str, name: str) -> str:
    """Which option of one `<select>` is selected — "" when none is."""
    # `[^>]*` rather than a fixed `name="{name}"` prefix: a later fix
    # gave `#purpose` an `id` ahead of its `name` for the aria-invalid/
    # aria-describedby binding (`m.err_attrs_named`), and this helper should
    # not care which order a `<select>`'s own attributes come in.
    block = re.search(rf'<select[^>]*\bname="{name}"[^>]*>.*?</select>', html, re.S)
    if block is None:
        return ""
    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected', block.group(0))
    return chosen.group(1) if chosen else ""


# --- the picker ------------------------------------------------------------------------


async def test_the_picker_offers_every_customers_live_contacts_and_asks_conduit_nothing(session):
    """Cross-customer by design (this is `everyones`' surface, like `/contacts`),
    archived rows excluded, and a read budget of zero — the rows are local and
    the customer names come from what this console has already observed, so the
    page renders whole with Conduit unreachable."""
    mine = await store(session, customer_id=CID, label="ZZZTEST Mine Ltd")
    theirs = await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs Ltd")
    retired = await store(session, customer_id=CID, label="ZZZTEST Retired Ltd")
    assert await counterparties.archive(session, CID, str(retired))
    await session.commit()

    calls: list = []
    app = make_app(stub({}, calls))
    async with signed_in(app) as web:
        response = await web.get(PICKER)

    assert response.status_code == 200
    offered = " ".join(options(response.text))
    assert "ZZZTEST Mine Ltd" in offered and "ZZZTEST Theirs Ltd" in offered
    assert "ZZZTEST Retired Ltd" not in offered
    assert str(mine) in response.text and str(theirs) in response.text
    # Said, not merely done: an operator who archived one has to be told why it
    # is not on the list rather than left to conclude it was lost.
    assert "Archived contacts are not offered" in " ".join(response.text.split())
    assert calls == []


async def test_the_picker_names_the_customer_from_what_this_console_observed(session):
    """"Contact name · customer name", with the name from the projections this
    console already holds — and the bare id where nothing ever named that
    customer (DESIGN.md's honest-miss clause). No `/v2/customers` read: this page
    has nothing to gather one with, and its budget is zero."""
    await store(session, customer_id=CID, label="ZZZTEST Named Ltd")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Unnamed Ltd")
    await projections.apply_observation(
        session,
        resource_kind="transactions",
        resource_id="txn_seen",
        observed={
            "id": "txn_seen",
            "status": "completed",
            "customerId": CID,
            "customerName": "ZZZTEST Console EOOD",
        },
    )

    app = make_app(stub({}))
    async with signed_in(app) as web:
        offered = options((await web.get(PICKER)).text)

    assert "ZZZTEST Named Ltd · ZZZTEST Console EOOD" in offered
    assert f"ZZZTEST Unnamed Ltd · {OTHER_CID}" in offered


async def test_the_contact_input_carries_no_size_attribute(session):
    """`size="52"` forced ~415px regardless of viewport
    (a ruling for the datalist popup's width) and overflowed 375px.
    The width now comes from CSS (`#contact { width: 100% }` in its tray),
    never from the HTML attribute."""
    await store(session, customer_id=CID, label="ZZZTEST Mine Ltd")

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get(PICKER)).text

    assert 'id="contact"' in html
    assert "size=" not in html


async def test_an_empty_picker_claims_nothing_and_says_where_contacts_come_from(session):
    app = make_app(stub({}))
    async with signed_in(app) as web:
        body = " ".join((await web.get(PICKER)).text.split())
    assert "No contact has been saved in this console yet" in body
    # The other half of the empty state, which a bare "none" would hide: archived
    # ones would not be here either, so "none" is not evidence that none exist.
    assert "an archived one would not be offered here even if there were" in body


async def test_the_hand_off_is_the_prefill_link_the_payout_form_itself_builds(session):
    """Drift pin. The picker hands off on **the** prefill URL — the one the payout
    form's own `Use saved contact` control has posted at — rather
    than on a second shape that could quietly diverge from it. Both are read out
    of the running app here: the form's `#prefill` action and its select's name,
    against the redirect's `Location`."""
    cp_id = await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs Ltd")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        hand_off = await web.get(f"{PICKER}?contact={cp_id}")
        form = (
            await web.get(
                f"/customers/{OTHER_CID}/payouts/new?purpose=payment_for_goods_or_services"
                "&rail=fedwire&recipientType=business&destinationCountry=USA"
            )
        ).text

    step = form.split('id="contact-first"')[1].split("</form>")[0]
    prefill = re.search(r'action="([^"]+)"', step).group(1)
    key = re.search(r'<input type="text" id="counterparty" name="([^"]+)"', step).group(1)
    assert hand_off.status_code == 303
    assert hand_off.headers["location"] == f"{prefill}?{key}={cp_id}"
    # …and that is the customer the contact belongs to, not the one who picked it.
    assert hand_off.headers["location"].startswith(f"/customers/{OTHER_CID}/payouts/new")


async def test_the_picker_refuses_an_archived_contact_in_the_contacts_pages_words(session):
    cp_id = await store(session, label="ZZZTEST Retired Ltd")
    assert await counterparties.archive(session, CID, str(cp_id))
    await session.commit()

    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"{PICKER}?contact={cp_id}")

    assert response.status_code == 200  # stayed on the picker; nothing handed off
    body = " ".join(response.text.split())
    assert "That contact cannot be paid from here" in body
    assert "left every payout picker" in body
    assert "save the destination again from a payout form" in body


@pytest.mark.parametrize("named", ["not-a-uuid", str(uuid.uuid4())])
async def test_an_id_no_contact_wears_refuses_without_inventing_one(named):
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"{PICKER}?contact={named}")
    assert response.status_code == 200
    assert "No saved contact of this console has that id" in response.text


@pytest.mark.parametrize("held", [set(), {"payout.create"}, {"transfer.create"}])
async def test_the_picker_paints_nothing_the_actor_cannot_open(held, session):
    """The same note applies verbatim: this returns [] whatever the gating,
    because the picker's own target is a `console.view` GET. What it pins is that
    nobody ever re-points it at a route the actor cannot open."""
    await store(session, label="ZZZTEST Mine Ltd")
    app = make_app(stub({}))
    async with signed_in_as(app, *held) as web:
        html = (await web.get(PICKER)).text
    assert forbidden_affordances(app, html, {"console.view", *held}) == []


# --- the resend prefill ----------------------------------------------------------------


async def test_the_last_payment_prefills_the_amount_the_purpose_and_the_route(session):
    """The feature, end to end on the form: the trail names the transaction, the
    read-back names the money and the purpose, its settlement reference names the
    rail, and the contact's own row names the rest of the corridor. The URL
    carries the contact and nothing else — this is the picker's hand-off."""
    cp_id = await store(session, label="ZZZTEST Globex Supplies")
    await used(session, cp_id, transaction=SENT["id"])

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    assert field_value(html, "amount") == "987.50"
    assert selected(html, "purpose") == "payment_for_goods_or_services"
    assert selected(html, "rail") == "fedwire"  # from `fedwireImad`, not from `us`
    assert selected(html, "recipientType") == "business"
    assert field_value(html, "destinationCountry") == "USA"
    assert selected(html, "virtualAccountId") == VID
    # The recipient, as it always was — and still editable, never locked.
    assert field_value(html, "f.destination.recipient.accountNumber") == ACCOUNT_NUMBER
    for match in re.finditer(r'<input [^>]*name="f\.destination\.recipient[^>]*>', html):
        assert "readonly" not in match.group(0) and "disabled" not in match.group(0)
    assert "The last payment this console recorded to" in " ".join(html.split())


@pytest.mark.parametrize(
    ("linked", "answer"),
    [
        (False, None),  # nothing on the trail: no transaction to read
        (True, httpx.Response(503, json={"type": "UNAVAILABLE", "title": "down"})),
    ],
)
async def test_with_no_readable_earlier_payment_the_form_says_so_and_prefills_no_amount(
    session, linked, answer
):
    """One sentence for both causes, because the page cannot tell them apart —
    and neither of them is allowed to look like an amount the operator chose."""
    # A `sepa` contact, so the route is complete from its own row and the amount
    # box is on the page to be provably empty: a `us` one would leave the rail
    # unanswered and there would be no form to check (the test below).
    cp_id = await store(session, rail_family="sepa", destination_country="DEU")
    if linked:
        await used(session, cp_id, transaction=SENT["id"])

    app = make_app(
        stub(
            routes(
                requirements=SEPA_BUSINESS,
                accounts=(EUR_ACTIVE,),
                extra={("GET", f"/v2/transactions/{SENT['id']}"): answer} if answer else None,
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    body = " ".join(html.split())
    # The contact is NAMED in this branch (design pass): the picker's wire value
    # is an id, so without the label a resend that could not be read back showed
    # the operator a UUID and nothing else.
    assert (
        "No earlier payment to <strong>Globex</strong> could be read back; "
        "amount not prefilled." in body
    )
    # The corridor is still filled in from the contact's own row — that is local
    # and needs no read at all. The PURPOSE is not: only the last payment could
    # have named it, so the route stays incomplete and there is no form yet,
    # which is what "the purpose and the amount are not" says on the page.
    assert selected(html, "rail") == "sepa"
    assert selected(html, "recipientType") == "business"
    assert field_value(html, "destinationCountry") == "DEU"
    assert selected(html, "purpose") == ""
    assert "the purpose and the amount are not" in body
    assert "Payout details" not in body


async def test_a_transaction_that_belongs_to_another_contact_never_prefills(session):
    """The link is the audit row's
    **counterparty id** — `counterparty.used` and nothing else — so a payment
    made to a different contact of the same customer cannot be read back onto
    this one, and neither can one whose prefill was edited
    (`counterparty.modified_prefill`, which did not go to the saved contact and
    is excluded by construction)."""
    theirs = await store(session, label="ZZZTEST Globex Supplies")
    mine = await store(session, label="ZZZTEST Someone Else Ltd")
    await used(session, theirs, transaction=SENT["id"])
    await used(
        session, mine, transaction=SENT["id"], action="counterparty.modified_prefill"
    )

    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        body = " ".join((await web.get(f"{NEW}?counterparty={mine}")).text.split())

    assert "987.50" not in body
    assert "No earlier payment to <strong>" in body
    assert "could be read back" in body
    # Not "read and discarded" — never read at all.
    assert [c for c in calls if SENT["id"] in c[1]] == []


async def test_a_contact_of_another_customer_resolves_to_nothing(session):
    """Route level, and now for the whole prefill rather than the recipient half:
    a URL naming this customer and someone else's contact fills in no
    destination, no amount and no route part, and says so — the same answer a
    made-up id gets. `counterparties.get` filters on `customer_id`; the picker's
    cross-customer read (`find`) is a different function with a different job."""
    theirs = await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs Ltd")
    await used(session, theirs, transaction=SENT["id"], customer_id=OTHER_CID)

    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={theirs}")).text

    assert "That contact could not be used" in html
    assert ACCOUNT_NUMBER not in html and "987.50" not in html
    assert selected(html, "recipientType") == "" and selected(html, "purpose") == ""
    assert [c for c in calls if SENT["id"] in c[1]] == []


async def test_the_operators_own_amount_beats_the_one_from_last_time(session):
    """The Prefill button and every route change carry the amount box's current
    contents, so the resend may only fill a box that is empty."""
    cp_id = await store(session)
    await used(session, cp_id, transaction=SENT["id"])

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}&amount=5.00")).text
    assert field_value(html, "amount") == "5.00"


async def test_a_prefilled_amount_is_refused_by_the_ceiling_like_a_typed_one(session):
    """The prefill is an ordinary value on an ordinary form: it is shown (hiding
    it would be this console deciding what the operator may see about their own
    last payment) and it is judged by `payout_errors` at submit exactly as a
    typed one is — before `operations.start`, so nothing was sent."""
    cp_id = await store(session)
    await used(session, cp_id, transaction=SENT["id"])

    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )
    with settings_override(money_ceiling="100.00"):
        async with signed_in(app) as web:
            html = (await web.get(f"{NEW}?counterparty={cp_id}")).text
            await upload(web, purpose="transaction_support", filename="doc_1.png")
            refused = await post(
                web,
                NEW,
                encoded(payout_form(amount="987.50", documentIds="doc_1",
                                    counterparty=str(cp_id))),
            )

    assert field_value(html, "amount") == "987.50"
    assert refused.status_code == 422
    assert "refuses any single amount over 100.00" in " ".join(refused.text.split())
    assert [c for c in calls if c[1] == "/v2/payouts"] == []


async def test_a_rail_no_record_names_is_the_familys_first_and_says_so(session):
    """AMENDED, deliberately. `destination.recipient.rail` is the
    whitelist family (`us`), which four payout rails share, and this payment has
    no settlement reference to name one.

    An earlier round left the rail unanswered in that case. The design rules
    otherwise and says why: `contacts.edit_route` — the contact edit page's own
    answer to "which rail describes this stored destination" —
    takes the family's FIRST rail, and two answers to one question would be two
    different forms for one contact. So the rail is filled and the note carries
    the honesty instead: which family, which rails share it, and that nothing on
    record says which one carried the payment. The row that asks for it carries
    the contact, so changing it does not drop the prefill."""
    cp_id = await store(session)
    unsettled = {**SENT, "id": "txn_unsettled", "destination": {
        **{k: v for k, v in SENT["destination"].items() if k != "fedwireImad"}}}
    await used(session, cp_id, transaction=unsettled["id"])

    app = make_app(stub(routes(transactions=(unsettled,))))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    assert selected(html, "rail") == "fedwire"  # `RAILS_FOR["us"][0]`, via edit_route
    body = " ".join(html.split())
    assert 'saved for the <code class="raw">us</code> rail family' in body
    assert "which fedwire, ach, rtp, fednow all share" in body
    assert "Nothing on record says which one carried the payment" in body
    # The route row carries the contact, so changing the rail there comes back
    # with it — this is the field that makes the hand-off survivable.
    row = html.split('id="route-row"')[1].split("</form>")[0]
    assert f'<input type="hidden" name="counterparty" value="{cp_id}">' in row
    # And the sentence is not printed for a contact whose payment DID name the
    # rail: a settlement reference is a fact, and a fact needs no caveat.
    settled_contact = await store(session, label="ZZZTEST Settled Ltd")
    await used(session, settled_contact, transaction=SENT["id"])
    async with signed_in(make_app(stub(routes()))) as web:
        settled = " ".join(
            (await web.get(f"{NEW}?counterparty={settled_contact}")).text.split()
        )
    assert "all share" not in settled
    assert 'name="rail"' in settled


async def test_a_family_that_names_one_rail_needs_no_settlement_reference(session):
    """`sepa` and `swift` map to exactly one payout rail each, so the contact's
    own row answers it and the read-back never has to."""
    cp_id = await store(session, rail_family="sepa", destination_country="DEU",
                        recipient={"iban": "DE89370400440532013000", "bic": "COBADEFFXXX",
                                   "legalName": "ZZZTEST Bauer GmbH"})
    app = make_app(stub(routes(requirements=SEPA_BUSINESS, accounts=(EUR_ACTIVE,))))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text
    assert selected(html, "rail") == "sepa"
    assert field_value(html, "destinationCountry") == "DEU"


async def test_the_funding_account_follows_the_currency_that_payment_was_in(session):
    """Matched by currency among the customer's active accounts — on a swift
    route, where the rail pins no currency and both accounts are therefore
    fundable, so the choice is the resend's rather than the page's default."""
    cp_id = await store(session, rail_family="swift", destination_country="CHE")
    in_euro = {**SENT, "id": "txn_euro", "destination": {
        **SENT["destination"], "assetAmount": {"code": "EUR", "amount": "400.00"}}}
    await used(session, cp_id, transaction=in_euro["id"])

    app = make_app(
        stub(routes(requirements=SEPA_BUSINESS, accounts=(USD_ACCOUNT, EUR_ACTIVE),
                    transactions=(in_euro,)))
    )
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    assert selected(html, "virtualAccountId") == EUR_ACTIVE["id"]
    # The rail is the contact's family, not the Fedwire reference this fixture
    # still carries: a reference naming a rail outside the family the contact was
    # saved for is two records disagreeing, and neither is believed over the
    # other.
    assert selected(html, "rail") == "swift"
    assert "not prefilled" not in " ".join(html.split())


async def test_a_currency_this_customer_holds_no_account_in_is_stated(session):
    """The other half: no account matches, so the page keeps its ordinary default
    and says the funding account is not the resend's — rather than selecting a
    currency the last payment cannot vouch for in silence."""
    cp_id = await store(session, rail_family="swift", destination_country="CHE")
    in_euro = {**SENT, "id": "txn_euro", "destination": {
        **SENT["destination"], "assetAmount": {"code": "EUR", "amount": "400.00"}}}
    await used(session, cp_id, transaction=in_euro["id"])

    app = make_app(
        stub(routes(requirements=SEPA_BUSINESS, accounts=(USD_ACCOUNT,),
                    transactions=(in_euro,)))
    )
    async with signed_in(app) as web:
        body = " ".join((await web.get(f"{NEW}?counterparty={cp_id}")).text.split())

    assert "no active EUR account of this customer is on the funding list above" in body


async def test_coordinates_edited_since_that_payment_are_stated_on_the_form(session):
    """The `modified_prefill` idiom, one step earlier: what is filled in is the
    contact's record as it stands now, which is not what that payment paid."""
    cp_id = await store(session)
    moved = {**SENT, "id": "txn_moved", "destination": {
        **SENT["destination"],
        "recipient": {**SENT["destination"]["recipient"], "accountNumber": "000999888777"}}}

    app = make_app(stub(routes(transactions=(moved,))))
    async with signed_in(app) as web:
        # The trail points at the moved payment instead.
        await used(session, cp_id, transaction=moved["id"])
        body = " ".join((await web.get(f"{NEW}?counterparty={cp_id}")).text.split())

    assert "coordinates were edited after that payment" in body
    assert "as it stands now" in body


async def test_a_read_back_with_no_recipient_block_claims_no_change(session):
    """The three-state rule again: Conduit omits the recipient on some views, and
    a comparison against nothing would call every such contact modified."""
    cp_id = await store(session)
    bare = {**SENT, "id": "txn_bare", "destination": {
        k: v for k, v in SENT["destination"].items() if k != "recipient"}}
    await used(session, cp_id, transaction=bare["id"])

    app = make_app(stub(routes(transactions=(bare,))))
    async with signed_in(app) as web:
        body = " ".join((await web.get(f"{NEW}?counterparty={cp_id}")).text.split())

    assert "coordinates were edited" not in body
    assert "The last payment this console recorded to" in body


async def test_a_whitelist_gated_resend_claims_nothing_about_the_contacts_coordinates(session):
    """The reachable gated case: an intercompany payout writes `counterparty.used`
    too (via the whitelist branch), so the trail can hand this page a contact on a
    route whose destination is Conduit's registered record. The amount is still
    the resend's; the destination sentences are not said at all, because under the
    gate the address book is not offered and nothing of the contact is filled in.
    """
    cp_id = await store(session)
    intercompany = {**SENT, "id": "txn_gated", "purpose": "intercompany"}
    await used(session, cp_id, transaction=intercompany["id"])

    app = make_app(
        stub(routes(requirements=FEDWIRE_INTERCOMPANY, transactions=(intercompany,)))
    )
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    body = " ".join(html.split())
    assert field_value(html, "amount") == "987.50"
    assert "This route is whitelist-gated" in body
    assert "filled in from the contact itself" not in body
    # The address book stays off a gated route, exactly as it always has.
    assert "Use saved contact" not in body


async def test_the_payout_that_saved_a_contact_is_the_fallback_read_back(session):
    """A contact's FIRST resend can have no
    `counterparty.used` row — the payout that saved it typed its destination, so
    the trail records `counterparty.save` — and that was the commonest resend of
    all prefilling nothing.

    `counterparty.save` is written on the **202**, so its operation is provably a
    payment to this destination; what the exclusion refuses is the claim
    "sent to contact X", which is about *picking* a saved record, and
    `linked_transactions` still refuses it. Reading a transaction back is a
    different question. The binding is `history`'s own — the audit row names this
    contact, and a save is otherwise matched by (customer, label, at or after this
    contact was created) — and this test goes through the real save path rather
    than seeding a row, so the detail shape it matches is the one the app writes.
    """
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT),
                    ("GET", f"/v2/transactions/{PAYOUT['id']}"): httpx.Response(
                        200, json=PAYOUT
                    ),
                }
            )
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        sent = await post(web, NEW, saving(counterparty_label="ZZZTEST Newly Saved"))
        assert sent.headers["HX-Redirect"].endswith(PAYOUT["id"]), sent.text[:300]

        cp_id = (
            await session.execute(select(Counterparty.id).where(Counterparty.label
                                                                == "ZZZTEST Newly Saved"))
        ).scalar_one()
        # No `used` row exists for it: nothing picked this contact, it was created.
        assert (
            await counterparties.linked_transactions(
                session, customer_id=CID, contact_id=str(cp_id), limit=5
            )
            == []
        )
        html = (await web.get(f"{NEW}?counterparty={cp_id}")).text

    body = " ".join(html.split())
    assert field_value(html, "amount") == PAYOUT["destination"]["assetAmount"]["amount"]
    assert selected(html, "purpose") == PAYOUT["purpose"]
    assert selected(html, "rail") == "fedwire"  # `fedwireImad`, as on the used path
    # …and the note names the payment it came from, which is a different one.
    assert "The payout that saved this contact" in body
    assert "The last payment this console recorded to" not in body


# --- contact-first ------------------------------------------------------------


async def test_the_contact_step_is_answerable_before_any_route(session):
    """The ask: "once the client chooses a customer, list available
    contacts so they can choose one before re-entering the requirements".

    So the step renders with a customer and NOTHING else answered, and picking a
    contact from it implies the route rather than requiring one.
    """
    cp_id = await store(session, label="ZZZTEST Globex Supplies")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        bare = await web.get(NEW)  # a customer, no route at all
        picked = await web.get(f"{NEW}?counterparty={cp_id}")

    step = bare.text.split('id="contact-first"')[1].split("</form>")[0]
    assert 'list="saved-contacts"' in step
    assert f'<option value="{cp_id}">ZZZTEST Globex Supplies · us · ' in bare.text
    # …and it is offered before the requirements exist: no form on that page yet.
    assert "Payout details" not in bare.text

    # Picking implies the corridor — and only the purpose is left to answer,
    # which is the one thing a contact cannot store.
    assert selected(picked.text, "rail") == "fedwire"
    assert selected(picked.text, "recipientType") == "business"
    assert field_value(picked.text, "destinationCountry") == "USA"
    assert selected(picked.text, "purpose") == ""
    assert "Payout details" not in picked.text

    # …and answering it brings the form, with the destination prefilled: the
    # purpose travels on the route row, which carries the contact with it.
    async with signed_in(app) as web:
        whole = await web.get(
            f"{NEW}?counterparty={cp_id}&purpose=payment_for_goods_or_services"
        )
    assert field_value(whole.text, "f.destination.recipient.accountNumber") == ACCOUNT_NUMBER
    assert selected(whole.text, "rail") == "fedwire"


async def test_the_contact_never_answers_the_purpose(session):
    """A contact stores a corridor, never a purpose (`app/counterparties.py`), so
    the purpose control renders empty and required and the operator picks it.

    **The one exception, and it is not the contact answering:** where the trail
    has a payment to this contact, the read-back prefills the purpose that
    payment carried — from Conduit's record of a payment, not from the record of
    an address. Both are pinned here so the difference stays visible.
    """
    no_history = await store(session, label="ZZZTEST Fresh Ltd")
    with_history = await store(session, label="ZZZTEST Globex Supplies")
    await used(session, with_history, transaction=SENT["id"])

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        fresh = await web.get(f"{NEW}?counterparty={no_history}")
        seen = await web.get(f"{NEW}?counterparty={with_history}")

    assert selected(fresh.text, "purpose") == ""
    assert 'name="purpose" required' in " ".join(fresh.text.split())
    # The corridor still came from the contact — only the purpose is open.
    assert selected(fresh.text, "rail") == "fedwire"
    assert selected(seen.text, "purpose") == "payment_for_goods_or_services"


async def test_the_implied_route_reads_discovery_exactly_once(session):
    """The requirements are never skipped, only pre-answered: the implied route
    is fetched exactly as a typed one is, and the contact list itself is local —
    no new Conduit read, and the page's budget is the five already pinned."""
    cp_id = await store(session)
    await used(session, cp_id, transaction=SENT["id"])
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await web.get(f"{NEW}?counterparty={cp_id}")

    assert [c[1] for c in calls].count("/v2/payouts/requirements") == 1
    assert sorted(c[1] for c in calls) == sorted(
        [
            f"/v2/transactions/{SENT['id']}",
            "/v2/payouts/requirements",
            "/v2/customers",
            f"/v2/customers/{CID}/virtual-accounts",
        ]
    )


async def test_a_gated_route_keeps_its_registered_picker_and_no_address_book(session):
    """Unchanged, and the reason is unchanged: under a whitelist gate the saved
    destinations ARE Conduit's registered entries, and a second unreviewed list
    beside them would offer a payout Conduit refuses."""
    await store(session, label="ZZZTEST Globex Supplies")
    app = make_app(stub(routes(requirements=FEDWIRE_INTERCOMPANY)))
    async with signed_in(app) as web:
        html = (
            await web.get(
                f"{NEW}?purpose=intercompany&rail=fedwire"
                "&recipientType=business&destinationCountry=USA"
            )
        ).text

    assert 'id="contact-first"' not in html
    assert "ZZZTEST Globex Supplies" not in html
    assert 'id="whitelistRecipientId"' in html


# --- gate fixes (2026-09-03) -------------------------------------------------------------


async def test_a_save_row_of_a_contact_renamed_away_is_never_this_contacts(session):
    """Gate finding M1, reproduced then fixed (probe P1).

    The save fallback used to match on `(customer, label, since)` — `history`'s
    own label arm, which exists so a *history panel* can show a pre-Phase-12
    save. A label is renameable and an archived contact does not hold its name,
    so: archive "Globex", rename "Acme" into "Globex", and Acme's payout form
    prefilled the amount and purpose of a payment made to Globex — and, because
    the coordinates of the two differ, told the operator Acme's had been edited
    since. Wrong contact, wrong payment, a false claim about a destination.

    Bound by `detail->>'counterparty'` now, which is whose save it actually was.
    A save with no id recorded on it prefills nothing, which is the honest end of
    it: this page has no way to prove that row is this contact's.
    """
    acme = await store(
        session,
        label="ZZZTEST Acme",
        recipient={
            "accountNumber": "999888777666",
            "routingNumber": "021000021",
            "legalName": "ZZZTEST Acme Inc",
        },
    )
    globex = await store(session, label="ZZZTEST Globex")  # SENT's own coordinates
    # A save row exactly as `_save_counterparty` writes it, for GLOBEX — the
    # `customer` and `label` keys included, because those are what `history`'s
    # label arm matches on and a helper that omitted them would make this test
    # pass against the bug.
    op = await used(session, globex, transaction=SENT["id"], action=counterparties.SAVE_ACTION)
    row = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == counterparties.SAVE_ACTION)
        )
    ).scalar_one()
    row.detail = {
        "customer": CID,
        "label": "ZZZTEST Globex",
        "rail_family": "us",
        "counterparty": str(globex),
    }
    await session.commit()
    assert op is not None
    assert await counterparties.archive(session, CID, str(globex))
    assert await counterparties.rename(session, CID, str(acme), "ZZZTEST Globex") == ""
    await session.commit()

    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        html = (await web.get(f"{NEW}?counterparty={acme}")).text

    body = " ".join(html.split())
    # Nothing of Globex's payment reached Acme's form — and it was never read.
    assert "987.50" not in body
    assert [c for c in calls if SENT["id"] in c[1]] == []
    assert selected(html, "purpose") == ""
    # …and no claim about Acme's coordinates having moved, which is what the
    # mismatched comparison used to produce.
    assert "coordinates were edited" not in body

    # The same save, bound to the contact it was actually written for, still
    # works: this is the intended fallback, not a retreat from it.
    async with signed_in(make_app(stub(routes()))) as web:
        own = await web.get(f"/customers/{CID}/contacts/{globex}/edit")
    assert own.status_code in (200, 303)  # archived: the point is Acme, above


async def test_two_settlement_references_name_no_rail_and_say_so(session):
    """Gate finding m2, at the branch rather than through the copy (probe P3).

    ONE reference names one rail. TWO name two, which names none — and that case
    fell through to the family's first rail with `implied` False, so the page
    showed a guess with the caveat unprinted. `rails` is what the caveat renders
    from, so it is what this pins.
    """
    cp_id = await store(session)
    picked = await counterparties.get(session, CID, str(cp_id))
    both = {
        **SENT,
        "id": "txn_two",
        "destination": {**SENT["destination"], "achTraceNumber": "0910000100"},
    }
    none_named = {
        **SENT,
        "id": "txn_none",
        "destination": {
            k: v for k, v in SENT["destination"].items() if k != "fedwireImad"
        },
    }
    app = make_app(stub(routes(transactions=(SENT, both, none_named))))

    async def resend_for(transaction):
        await used(session, cp_id, transaction=transaction["id"])
        return await payouts._resend(
            app.state.conduit, session, CID, picked, payouts.Route({})
        )

    # Two references: the family's first rail, and the caveat's own input set.
    two = await resend_for(both)
    assert two["rails"] == payments.RAILS_FOR["us"]
    # None at all: same answer, same caveat.
    nothing = await resend_for(none_named)
    assert nothing["rails"] == payments.RAILS_FOR["us"]
    # Exactly one: a fact, and no caveat. (`used` rows are newest-first, so this
    # one wins for the last call.)
    one = await resend_for(SENT)
    assert one["rails"] == ()
