"""The Convert workflow and the orders ledger, end to end against a stub.

The load-bearing assertions here are the ones about *time*: an option is chosen
and stored before anything is sent, a confirmation after its expiry is refused by
the server (not merely by a disabled button), and the refused operation is
abandoned so the operator can re-quote without the double-submit guard standing
in the way.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import func, select

from app import conversions
from app.auth.tokens import sign
from app.config import get_settings
from app.models import AuditEvent, Operation
from app.main import ALREADY_MOVED
from app.web import convert
from app.web.convert import OPTION_SELECTED, SELECTION_TTL
from tests.payments_fixtures import (
    CID,
    CONVERSION_QUOTE,
    EUR_ACTIVE,
    EUR_VID,
    EXPIRED_CONVERSION_QUOTE,
    ORDER,
    USD_ACCOUNT,
    VID,
    encoded,
    page,
)
from tests.conftest import settings_override
from tests.web_harness import (
    forbidden_affordances,
    make_app,
    minted_intent,
    post,
    signed_in,
    signed_in_as,
    stub,
)

ROOT = Path(__file__).resolve().parent.parent
CONVERT = f"/customers/{CID}/convert"
ACCOUNTS = f"/v2/customers/{CID}/virtual-accounts"
ORDER_ID = ORDER["id"]


def routes(accounts=None, quote=CONVERSION_QUOTE, extra=None):
    return {
        ("GET", "/v2/customers"): page([{"id": CID, "legalName": "ZZZTEST Console EOOD"}]),
        ("GET", ACCOUNTS): page(accounts if accounts is not None else [USD_ACCOUNT, EUR_ACTIVE]),
        ("POST", "/v2/quotes"): (
            quote if isinstance(quote, httpx.Response) else httpx.Response(201, json=quote)
        ),
        ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(200, json=ORDER),
        ("POST", "/v2/orders"): httpx.Response(202, json=ORDER),
        **(extra or {}),
    }


def quote_form(**overrides):
    return encoded(
        {
            "source": VID,
            "destination": EUR_VID,
            "amount": "1000.00",
            "lockSide": "source",
            **overrides,
        }
    )


def selection_of(html: str) -> str:
    """The hidden selection blob the options table posts back."""
    marker = 'name="selection" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)].replace("&#34;", '"').replace("&amp;", "&")


async def selected(web, app_selection: str | None = None, **overrides):
    """Quote, then pick the first option — the state every confirm test starts in."""
    quoted = await post(web, CONVERT + "/quote", quote_form(**overrides))
    blob = app_selection if app_selection is not None else selection_of(quoted.text)
    return await post(
        web,
        CONVERT + "/select",
        encoded({"source": VID, "destination": EUR_VID, "selection": blob}),
    )


# --- the form ---------------------------------------------------------------------------


async def test_the_destination_picker_offers_only_the_other_currency():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert response.status_code == 200
    assert EUR_VID in response.text and "EUR" in response.text
    assert "a same-asset move is a transfer" in response.text


async def test_the_source_param_preselects_that_account():
    """`?source=` is read, not decorative — the transfers screen's Convert
    pointer hands over the account the money is stuck in, and it arrives picked.
    The default is the first account (USD), so EUR selected can only come from
    the parameter."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat((await web.get(CONVERT + f"?source={EUR_VID}")).text)
    out_of = html.split('id="source"', 1)[1].split("</select>", 1)[0]
    assert f'<option value="{EUR_VID}" selected>' in out_of
    assert f'<option value="{VID}" selected>' not in out_of


# --- the way back to the payout that sent us here --------------------------

PAYOUT_ROUTE = {
    "purpose": "payment_for_goods_or_services",
    "rail": "fedwire",
    "recipientType": "business",
    "destinationCountry": "USA",
}
FROM_PAYOUT = (
    f"?source={EUR_VID}&destination={VID}&purpose=payment_for_goods_or_services"
    "&rail=fedwire&recipientType=business&destinationCountry=USA"
)


def flat(html: str) -> str:
    return " ".join(html.replace("&amp;", "&").split())


async def test_the_way_back_carries_the_route_and_the_account_the_conversion_lands_in():
    """ROUTE parameters only, plus the converted account as the payout's new
    funding pick. Never the amount typed before the conversion — what landed is
    what there is to send — and never an intent: the payout page mints one per
    render, so the return trip has to be an ordinary GET (OPERATIONS_SPEC §1)."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat((await web.get(CONVERT + FROM_PAYOUT)).text)

    back = html.split(f'href="/customers/{CID}/payouts/new?', 1)[1].split('"', 1)[0]
    assert dict(pair.split("=", 1) for pair in back.split("&")) == {
        **PAYOUT_ROUTE,
        "virtualAccountId": VID,
    }
    assert "amount" not in back and "intent" not in back and "lockSide" not in back
    # The sequencing, said where the operator is standing.
    assert "after this conversion settles, which is when its output can be spent" in html
    # Names over ids: the asset leads, the id trails it as copyable secondary.
    assert f'funded from the USD account it lands in (<code class="raw">{VID}</code>)' in html


async def test_the_way_back_survives_the_quote_round_trip():
    """The link is on the form, and quoting re-renders the form. A hand-off that
    disappeared the moment the operator priced the conversion would be a link
    only someone who never used it could see."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        quoted = await post(
            web,
            CONVERT + "/quote",
            quote_form(source=EUR_VID, destination=VID, **PAYOUT_ROUTE),
        )
    html = flat(quoted.text)
    assert f"/customers/{CID}/payouts/new?" in html
    assert f"virtualAccountId={VID}" in html


async def test_convert_reached_on_its_own_offers_no_way_back():
    """The pair. Convert is its own flow between a customer's own accounts; a
    "back to that payout" link on a page nobody arrived at from a payout points
    at a route row this console invented."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(CONVERT)).text
    assert "payouts/new?" not in html
    assert "Back to that payout" not in html


async def test_one_currency_only_explains_and_links_to_request_an_account():
    """The empty state that matters: nothing to convert *into*."""
    app = make_app(stub(routes(accounts=[USD_ACCOUNT])))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "Nothing to convert into" in response.text
    assert f"/customers/{CID}/request-account" in response.text
    assert "EUR account" in response.text


SGD_ACCOUNT = {**USD_ACCOUNT, "id": "vac_sgd", "asset": {"code": "SGD"}}
UNREADABLE_ASSET = {k: v for k, v in USD_ACCOUNT.items() if k != "asset"}


async def test_a_currency_conduit_will_not_quote_is_not_told_to_request_an_account():
    """The picker now names any currency discovery allows, so an SGD-only
    customer is ordinary. Conduit quotes USD↔EUR only, so no account this
    operator requests makes this balance convertible — offering the link is the
    same false promise as the old "Request USD or EUR account"."""
    app = make_app(stub(routes(accounts=[SGD_ACCOUNT])))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "Nothing to convert into" in response.text
    assert "Conduit quotes conversions between USD and EUR only" in response.text
    assert "SGD cannot be converted here at all" in response.text
    assert f"/customers/{CID}/request-account" not in response.text


async def test_an_unreadable_source_currency_is_not_told_it_cannot_be_converted():
    """The third arm, and the reason `wanted == []` is not one message: an
    account whose currency could not be read may not be told it is unquotable,
    because this console does not know what it holds."""
    app = make_app(stub(routes(accounts=[UNREADABLE_ASSET])))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "Nothing to convert into" in response.text
    assert "This account's currency could not be read" in response.text
    assert "cannot be converted here at all" not in response.text


async def test_holding_both_convertible_currencies_never_reaches_that_card():
    """Why the empty `wanted` needs no fourth arm: a customer holding both has a
    candidate to land in, so the card is not rendered at all."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "Nothing to convert into" not in response.text


async def test_no_active_account_at_all_says_so():
    """Half of a pair. This one is the *successful* empty read, and it asserts the
    absence copy — see the sibling below for the read that failed. Neither
    sentence may stand in for the other, which is why the negative assertion is
    here as well as there."""
    app = make_app(stub(routes(accounts=[])))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "No active virtual account" in response.text
    assert "could not be read" not in response.text


async def test_an_unreadable_account_list_is_not_no_active_account():
    """The other half. Conduit refuses the list, so the console knows nothing
    about this customer's accounts — and telling the operator there is no active
    account sends them to request a duplicate one a human then reviews."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", ACCOUNTS): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Accounts down"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert "No active virtual account" not in response.text
    assert "virtual accounts could not be read" in response.text
    # The failure is on the page as a problem card, not merely as a softer
    # sentence: the verified defect rendered zero of them.
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3


async def test_the_lock_side_is_a_segmented_control_of_two_real_radios():
    """Presentation only. The control is a `.segmented` box, but
    what is inside it is still two ordinary radios named `lockSide` carrying the
    two values `app.conversions.LOCK_SIDE_VALUES` defines — which is what the
    quote route validates against and what the live sweep posts."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(CONVERT)
    assert '<div class="segmented"' in response.text
    for value in conversions.LOCK_SIDE_VALUES:
        assert f'<input type="radio" name="lockSide" value="{value}"' in response.text
    # Exactly the two the enum has: a segmented control must not invent a state.
    assert response.text.count('name="lockSide"') == len(conversions.LOCK_SIDE_VALUES)
    # Still focusable, so still tabbable. A segmented control is only a
    # keyboard control while its inputs are *rendered* — `display: none` or
    # `visibility: hidden` on them would take the whole toggle out of the tab
    # order, so the rule that hides them is pinned here rather than left to a
    # later stylesheet sweep.
    rule = (ROOT / "static" / "styles.css").read_text().split(".segmented input {")[1]
    rule = rule[: rule.index("}")]
    assert "opacity: 0" in rule
    assert "display: none" not in rule and "visibility: hidden" not in rule


async def test_both_segments_still_submit_their_own_value_to_conduit():
    """Both halves of the toggle, through the real form post to the real route."""
    for value in conversions.LOCK_SIDE_VALUES:
        calls: list = []
        app = make_app(stub(routes(), calls))
        async with signed_in(app) as web:
            response = await post(web, CONVERT + "/quote", quote_form(lockSide=value))
        assert response.status_code == 200
        body = json.loads(next(c[2] for c in calls if c[1] == "/v2/quotes"))
        assert body["lockSide"] == value
        # And the choice survives the re-render, still checked on its segment.
        assert f'value="{value}" checked' in response.text


# --- the quote --------------------------------------------------------------------------


async def test_the_quote_is_a_conversion_quote_and_carries_no_idempotency_key():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, CONVERT + "/quote", quote_form())
    assert response.status_code == 200
    body = json.loads(next(c[2] for c in calls if c[1] == "/v2/quotes"))
    assert body == {
        "source": {"code": "USD"},
        "destination": {"code": "EUR"},
        "lockSide": "source",
        "amount": "1000.00",
    }
    assert "0.9123" in response.text and "912.30 EUR" in response.text


async def test_the_quote_endpoint_is_called_without_an_idempotency_key():
    """Verified live: `POST /v2/quotes` answers 400 when given one."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/quotes":
            seen["idempotency"] = request.headers.get("Idempotency-Key")
            return httpx.Response(201, json=CONVERSION_QUOTE)
        return page([USD_ACCOUNT, EUR_ACTIVE])

    app = make_app(handler)
    async with signed_in(app) as web:
        await post(web, CONVERT + "/quote", quote_form())
    assert seen == {"idempotency": None}


async def test_a_same_currency_pair_is_pointed_at_transfers():
    app = make_app(stub(routes(accounts=[USD_ACCOUNT, {**USD_ACCOUNT, "id": "vac_usd_2"}])))
    async with signed_in(app) as web:
        response = await post(web, CONVERT + "/quote", quote_form(destination="vac_usd_2"))
    assert response.status_code == 422
    assert "Same currency on both sides" in response.text
    assert "transfers screen" in response.text


async def test_a_bad_amount_never_reaches_conduit():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, CONVERT + "/quote", quote_form(amount="-1"))
    assert response.status_code == 422 and "positive decimal" in response.text
    assert [c for c in calls if c[1] == "/v2/quotes"] == []


async def test_conduits_own_refusal_to_price_is_shown():
    app = make_app(
        stub(
            routes(
                quote=httpx.Response(
                    422,
                    json={
                        "type": "NO_ELIGIBLE_PROVIDER",
                        "title": "No provider",
                        "detail": "EUR is not available for this organization.",
                        "resolution": "Contact support.",
                        "correlationId": "corr_quote",
                    },
                )
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, CONVERT + "/quote", quote_form())
    assert response.status_code == 422
    # A3: a catalogued code gets this console's own two sentences, and Conduit's
    # resolution ("Contact support.") is not one of them.
    assert "No banking provider can hold this currency" in response.text
    assert "corr_quote" in response.text
    assert "ask Conduit before trying another currency" in response.text
    assert "Contact support." not in response.text


# --- selecting an option ----------------------------------------------------------------


async def test_choosing_an_option_stores_it_before_anything_is_sent(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await selected(web)

    op = (await session.execute(select(Operation))).scalars().one()
    assert op.type == "order_create" and op.state == "created"
    # The option id is on the row itself — it is the order DTO.
    assert op.request_body["quoteOptionId"] == "qop_conv_a"
    assert op.request_body["source"] == {"type": "virtual_account", "id": VID}

    # …and the expiry and price are on the operation's own audit event.
    chosen = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == OPTION_SELECTED)
        )
    ).scalars().one()
    assert chosen.operation_id == op.id
    assert chosen.detail["expiresAt"] == CONVERSION_QUOTE["expiresAt"]
    assert chosen.detail["endUserRate"] == "0.9123"
    assert chosen.detail["amount"] == "1000.00"

    # Nothing was sent.
    assert [c for c in calls if c[1] == "/v2/orders"] == []
    assert response.headers["HX-Redirect"] == f"/convert/{op.id}"


async def test_an_expired_quote_offers_no_option_to_choose():
    app = make_app(stub(routes(quote=EXPIRED_CONVERSION_QUOTE)))
    async with signed_in(app) as web:
        response = await post(web, CONVERT + "/quote", quote_form())
    assert "already expired" in response.text
    assert 'name="selection"' not in response.text


def sealed(**payload) -> str:
    """A selection blob signed exactly as the render would sign it."""
    return sign(payload, secret=SECRET, ttl=SELECTION_TTL)


SECRET = get_settings().session_secret.get_secret_value()


async def test_an_already_expired_option_is_refused_at_select(session):
    """The button is gone (above), so this is the server saying no to a replay of
    a properly signed but lapsed selection."""
    app = make_app(stub(routes(quote=EXPIRED_CONVERSION_QUOTE)))
    async with signed_in(app) as web:
        response = await selected(
            web,
            app_selection=sealed(
                quoteOptionId="qop_conv_old",
                expiresAt=EXPIRED_CONVERSION_QUOTE["expiresAt"],
                source=VID,
                destination=EUR_VID,
            ),
        )
    assert response.status_code == 422
    assert "already expired" in response.text
    assert (await session.execute(select(Operation))).scalars().all() == []


@pytest.mark.parametrize(
    ("ceiling", "creates"),
    [
        ("", True),          # unset: today's behaviour
        ("1000.00", True),   # the option's sourceAmount is exactly 1000.00 — AT it
        ("999.99", False),   # …and one cent under the source amount is over the ceiling
    ],
)
async def test_the_money_ceiling_refuses_an_order_before_the_ledger(session, ceiling, creates):
    """The money ceiling on the conversion arm.

    Read off the option's own `sourceAmount` — Conduit's computed figure for what
    leaves the source account — rather than the typed amount, because on
    `lockSide=destination` the typed number is what LANDS, not what goes. It
    rides in the HMAC-signed selection, so the browser is not the author of the
    number this guard reads, and the check is before `operations.start`: a
    refused order is not an operation."""
    app = make_app(stub(routes()))
    with settings_override(money_ceiling=ceiling):
        async with signed_in(app) as web:
            response = await selected(web)
    rows = (
        await session.execute(select(Operation).where(Operation.type == "order_create"))
    ).scalars().all()
    if creates:
        assert rows, response.text[:400]
    else:
        assert response.status_code == 422
        assert "refuses any single amount over 999.99" in " ".join(response.text.split())
        assert rows == []


async def test_an_option_with_no_source_amount_is_refused_not_waved_through(session):
    """Fail CLOSED (minor 4).

    `payments._money` renders an absent or malformed amount block as `""`, and
    `over_ceiling("")` deliberately returns None — an empty amount is the calling
    site's own AMOUNT_MESSAGE, not this guard's. That interaction meant an option
    with no `sourceAmount` skipped the ceiling entirely. With a ceiling
    configured, "the number is not there" must never resolve to "therefore it is
    under the limit"."""
    app = make_app(stub(routes()))
    with settings_override(money_ceiling="1.00"):
        async with signed_in(app) as web:
            response = await selected(
                web,
                app_selection=sealed(
                    quoteOptionId="qop_conv_a",
                    expiresAt="2099-01-01T00:00:00Z",
                    source=VID,
                    destination=EUR_VID,
                    # …and no sourceAmount at all.
                ),
            )
    assert response.status_code == 422
    assert "ceiling" in " ".join(response.text.split()).lower()
    assert (
        await session.execute(select(Operation).where(Operation.type == "order_create"))
    ).scalars().all() == []


def test_the_selection_carries_a_source_amount_this_guard_can_read():
    """`quote_view` renders money as `"1000.00 USD"`, so the ceiling reads the
    first token. If that shape ever changes — a different separator, a dict, a
    renamed key — `over_ceiling` refuses everything rather than passing it, but
    THIS is the test that says so out loud instead of leaving a silent
    behaviour change to be discovered on a production validation run."""
    from decimal import Decimal

    from app import conversions, payments

    view = payments.quote_view(CONVERSION_QUOTE)
    chosen = conversions.selection(view, view["options"][0])
    token = str(chosen["sourceAmount"]).split(" ")[0]
    assert Decimal(token) == Decimal("1000.00")


async def test_a_forged_selection_without_an_option_is_refused(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await selected(web, app_selection='{"expiresAt": "2099-01-01T00:00:00Z"}')
    assert response.status_code == 422 and "could not be redeemed" in response.text
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_an_unsigned_selection_is_refused(session):
    """Raw JSON is what the browser used to be trusted to send. It is not a
    selection this console issued, so nothing is created from it."""
    app = make_app(stub(routes()))
    forged = json.dumps(
        {
            "quoteOptionId": "qop_conv_a",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "source": VID,
            "destination": EUR_VID,
        }
    )
    async with signed_in(app) as web:
        response = await selected(web, app_selection=forged)
    assert response.status_code == 422 and "could not be verified" in response.text
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_tampered_price_or_account_is_refused(session):
    """One flipped character anywhere in the payload — the rate the audit trail
    will record, or the account the money lands in — invalidates the signature."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        quoted = await post(web, CONVERT + "/quote", quote_form())
        good = selection_of(quoted.text)
        body, signature = good.split(".")
        tampered = f"{body[:-2]}XY.{signature}"
        response = await post(
            web, CONVERT + "/select", encoded({"selection": tampered})
        )
    assert response.status_code == 422 and "could not be verified" in response.text
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_the_accounts_the_order_names_come_out_of_the_signed_blob(session):
    """The form fields no longer decide where the money goes: only what was
    signed at render does."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        quoted = await post(web, CONVERT + "/quote", quote_form())
        await post(
            web,
            CONVERT + "/select",
            # A hostile client swaps the accounts in the plain fields.
            encoded(
                {
                    "source": EUR_VID,
                    "destination": VID,
                    "selection": selection_of(quoted.text),
                }
            ),
        )
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.request_body["source"] == {"type": "virtual_account", "id": VID}
    assert op.request_body["destination"] == {"type": "virtual_account", "id": EUR_VID}


async def test_the_audit_record_is_only_what_the_console_signed(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
    chosen = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == OPTION_SELECTED))
    ).scalars().one()
    assert chosen.detail["quoteOptionId"] == "qop_conv_a"
    assert chosen.detail["source"] == VID and chosen.detail["destination"] == EUR_VID
    assert "exp" not in chosen.detail  # the token's own field, not part of the choice


async def test_choosing_the_same_option_twice_is_one_operation(session):
    """The §1 double-submit guard: same body, same active row, same key."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        first = await selected(web)
        second = await selected(web)
    ops = (await session.execute(select(Operation))).scalars().all()
    assert len(ops) == 1
    assert first.headers["HX-Redirect"] == second.headers["HX-Redirect"]


# --- the confirm screen -----------------------------------------------------------------


async def test_the_confirm_screen_counts_down_against_the_stored_expiry(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        response = await web.get(f"/convert/{op.id}")
    assert response.status_code == 200
    assert f'data-expires-at="{CONVERSION_QUOTE["expiresAt"]}"' in response.text
    assert "qop_conv_a" in response.text and "0.9123" in response.text
    assert 'id="confirm-submit"' in response.text


async def test_confirming_creates_the_order_and_lands_on_it(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        response = await post(web, f"/convert/{op.id}")

    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/orders"))
    assert sent["quoteOptionId"] == "qop_conv_a"
    assert sent["clientReferenceId"] == str(op.id)  # injected at call time
    assert response.headers["HX-Redirect"] == f"/orders/{ORDER_ID}"
    await session.refresh(op)
    assert op.state == "confirmed" and op.conduit_resource_id == ORDER_ID


async def test_the_order_create_carries_the_operations_idempotency_key(session):
    keys: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/orders" and request.method == "POST":
            keys.append(request.headers.get("Idempotency-Key"))
            return httpx.Response(202, json=ORDER)
        if request.url.path == "/v2/quotes":
            return httpx.Response(201, json=CONVERSION_QUOTE)
        return page([USD_ACCOUNT, EUR_ACTIVE])

    app = make_app(handler)
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        await post(web, f"/convert/{op.id}")
    assert keys == [str(op.idempotency_key)]


def raced_at_the_wire(arrived: list, gate: asyncio.Event):
    """An `execute_operation` that holds the **first** confirm to reach it until
    a second one reaches it too, and then lets both through together.

    Gathering two POSTs and hoping reproduces this only sometimes: whichever
    request gets ahead resolves the operation before the other's `state !=
    "created"` read, and the other then takes the ordinary already-sent redirect
    and never races at all — which is the same two-non-500 answer arrived at by
    a path that was never broken. This rendezvous pins the one ordering that is
    the bug: both requests past that unlocked check, both holding a `created`
    row, both about to attempt `created -> in_flight`.
    """
    real = convert.execute_operation

    async def execute(session, op, **kwargs):
        arrived.append(op.id)
        if len(arrived) < 2:
            await gate.wait()
        else:
            gate.set()
        return await real(session, op, **kwargs)

    return execute


async def test_two_concurrent_confirms_sent_one_order_and_neither_operator_got_a_500(
    session, monkeypatch
):
    """The `state != "created"` guard above this route's
    send is an unlocked read, so two clicks that arrive together both pass it —
    a double-click that outran `hx-sync`, or the same operator on two tabs.

    On the wire, where this repository settles such things: **one** `POST
    /v2/orders`, because `created -> in_flight` goes through `transition`'s
    `SELECT ... FOR UPDATE` and only one of the two can move the row.

    What was broken is the other half. The loser's `IllegalTransition
    in_flight -> in_flight` had no handler anywhere, so it came back a bare 500
    — and a 500 on a submit is exactly what provokes the resubmit that pays
    twice (`_Result._record` says so in as many words). Both clicks now land on
    a page that tells the operator what happened to their conversion.
    """
    calls: list = []
    app = make_app(stub(routes(), calls))
    arrived: list = []
    monkeypatch.setattr(convert, "execute_operation", raced_at_the_wire(arrived, asyncio.Event()))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        first, second = await asyncio.gather(
            post(web, f"/convert/{op.id}"), post(web, f"/convert/{op.id}")
        )

    assert arrived == [op.id, op.id], "the drill never put two confirms on one operation"
    assert len([c for c in calls if c[1] == "/v2/orders"]) == 1, calls
    assert [first.status_code, second.status_code] == [204, 204], (
        first.status_code,
        second.status_code,
    )
    # The winner lands on the order it created; the loser lands on the
    # operation, which is the console's own account of what happened to it.
    landed = {r.headers["HX-Redirect"].split("?", 1)[0] for r in (first, second)}
    assert landed == {f"/orders/{ORDER_ID}", f"/operations/{op.id}"}, landed
    loser = next(r for r in (first, second) if f"/operations/{op.id}" in r.headers["HX-Redirect"])
    assert unquote_plus(loser.headers["HX-Redirect"].split("msg=", 1)[1].split("&", 1)[0]) == (
        ALREADY_MOVED
    )
    await session.refresh(op)
    assert op.state == "confirmed" and op.attempt_count == 1, "one attempt, one order"


async def test_a_stale_confirm_is_refused_by_the_server_and_re_quoted(session):
    """The disabled button is a display decision; this is the real guard."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        # The option lapses between the choice and the click.
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == OPTION_SELECTED)
            )
        ).scalars().one()
        event.detail = {**event.detail, "expiresAt": "2020-01-01T00:00:00.000Z"}
        await session.commit()

        response = await post(web, f"/convert/{op.id}")

    assert [c for c in calls if c[1] == "/v2/orders"] == []
    redirect = unquote_plus(response.headers["HX-Redirect"])
    assert redirect.startswith(CONVERT)
    assert "expired before it was confirmed" in redirect
    assert f"source={VID}" in redirect and "amount=1000.00" in redirect
    await session.refresh(op)
    # Abandoned, so the guard is released and a fresh quote can be redeemed.
    assert op.state == "abandoned"


async def test_after_a_stale_confirm_the_next_option_is_a_new_operation(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == OPTION_SELECTED)
            )
        ).scalars().one()
        event.detail = {**event.detail, "expiresAt": "2020-01-01T00:00:00.000Z"}
        await session.commit()
        await post(web, f"/convert/{op.id}")
        await selected(web)
    session.expire_all()  # the routes moved these rows in their own sessions
    ops = (await session.execute(select(Operation).order_by(Operation.created_at))).scalars().all()
    assert [o.state for o in ops] == ["abandoned", "created"]
    assert ops[0].idempotency_key != ops[1].idempotency_key


async def test_conduits_refusal_to_redeem_shows_the_re_quote_link(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", "/v2/orders"): httpx.Response(
                        422,
                        json={
                            "type": "QUOTE_EXPIRED",
                            "title": "Quote option expired",
                            "detail": "That option can no longer be redeemed.",
                            "correlationId": "corr_order",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        response = await post(web, f"/convert/{op.id}")
    assert response.status_code == 422
    assert "This price has expired" in response.text  # A3
    assert "corr_order" in response.text
    assert "Re-quote instead" in response.text


async def test_an_ambiguous_order_create_goes_to_the_operation_page(session):
    app = make_app(
        stub(routes(extra={("POST", "/v2/orders"): httpx.Response(500, json={"type": "OOPS"})}))
    )
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        response = await post(web, f"/convert/{op.id}")
    assert response.headers["HX-Redirect"] == f"/operations/{op.id}"
    await session.refresh(op)
    assert op.state == "outcome_unknown"


async def test_a_confirmed_conversion_reopened_lands_on_its_order(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        await post(web, f"/convert/{op.id}")
        again = await web.get(f"/convert/{op.id}")
    assert again.headers["location"] == f"/orders/{ORDER_ID}"


async def test_an_unknown_conversion_is_a_404():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        assert (await web.get("/convert/not-a-uuid")).status_code == 404
        assert (
            await web.get("/convert/11111111-1111-1111-1111-111111111111")
        ).status_code == 404


# --- the Transact page (the orders ledger plus the move-money launcher) -----------------

CUSTOMER = {"id": CID, "legalName": "ZZZTEST Ltd", "type": "business"}


def transact(customers=CUSTOMER, orders=(ORDER,)):
    """`/orders` with both of its reads answered."""
    return {
        ("GET", "/v2/orders"): page(list(orders)),
        ("GET", "/v2/customers"): (
            customers if isinstance(customers, httpx.Response) else page(list(customers or []))
        ),
    }


async def test_the_launcher_offers_the_three_verbs_against_a_picked_customer():
    """The IA change's whole point: a payout, a transfer and a conversion all
    start on the page the nav calls Transact. The paths are relative because
    app.js prefixes the customer the operator picks — assert the suffixes the
    handler concatenates, and the ids it concatenates them onto."""
    app = make_app(stub(transact(customers=[CUSTOMER])))
    async with signed_in(app) as web:
        html = (await web.get("/orders")).text

    # An earlier round retitled the page: the H1 names the ledger it is, and the launcher
    # sits under its own section heading. The ribbon's Transact split is unchanged.
    assert "<h1>Orders</h1>" in html and "<h2>Move money</h2>" in html
    assert "<h2>Conversion orders</h2>" in html
    assert f'<option value="{CID}">ZZZTEST Ltd · {CID}</option>' in html
    # `/payouts` (not `/payouts/new`) since the restructure: the launcher's
    # payout verb lands on the Transact fork — one payment or a batch — because
    # that question comes before any purpose or any requirements.
    for path in ("/payouts", "/transfers/new", "/convert"):
        assert f'data-launch="{path}"' in html
    # Enabled, because there is something to pick.
    assert "disabled" not in html.split('id="move-money"')[1].split("</div>")[0]
    # And the ledger the launcher sits above is still the orders list.
    assert ORDER_ID in html and "1000.00 USD" in html


async def test_the_orders_list_lights_the_browse_half_of_its_section_key():
    """The other side of the payout test. `section="orders"`
    is passed by this list *and* by all three money-movement flows; the ribbon
    splits them by path, so the list lights Transactions › Orders and none of
    the Transact verbs."""
    app = make_app(stub(transact(customers=[CUSTOMER])))
    async with signed_in(app) as web:
        html = (await web.get("/orders")).text

    assert (
        '<div class="group on"\n       role="group" aria-labelledby="grp-transactions">'
        '\n    <p class="group-head" id="grp-transactions">Transactions</p>'
    ) in html
    assert '<a class="item on" href="/orders" aria-current="page">Orders</a>' in html
    assert (
        '<div class="group on" role="group" aria-labelledby="grp-transact">'
        '\n    <p class="group-head" id="grp-transact">Transact</p>'
    ) not in html
    assert '<a class="item on" href="/orders#move-money"' not in html


async def test_a_viewer_gets_no_launcher_but_still_gets_named_rows():
    """Money movement needs operator: a viewer sees the list and nothing that
    starts a payment.

    The customers read is no longer operator-only, and that is an earlier round's
    deliberate change (logged): an order row carries `customerId` and no name, so
    the same one bounded read that fills the operator's picker is what names the
    rows for *everyone*. Skipping it for a viewer would mean the same list naming
    its customers or not depending on who is signed in. Exactly one such read,
    gathered with the orders read."""
    calls: list = []
    app = make_app(stub(transact(customers=[CUSTOMER]), calls))
    async with signed_in(app, groups="readers") as web:
        html = (await web.get("/orders")).text

    assert "<h1>Orders</h1>" in html and ORDER_ID in html
    assert 'id="move-money"' not in html and "data-launch" not in html
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1
    assert "ZZZTEST Ltd" in html


async def test_the_transact_page_renders_when_the_customer_fetch_fails():
    """One page, two independent reads. The launcher degrades to an inert tray
    with the problem on it; the orders list underneath is untouched."""
    app = make_app(
        stub(
            transact(
                customers=httpx.Response(503, json={"type": "UNAVAILABLE", "title": "Customers down"})
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get("/orders")

    assert response.status_code == 200
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    assert "No customer to move money for" in response.text
    tray = response.text.split('id="move-money"')[1].split("</div>")[0]
    assert tray.count("disabled") == 4  # the select and all three verbs
    assert ORDER_ID in response.text and "1000.00 USD" in response.text


async def test_the_launcher_says_when_there_are_more_than_it_shows():
    """It is the first cursor page, not the customer list — say so rather than
    let an operator conclude a customer does not exist."""
    more = httpx.Response(
        200, json={"data": [CUSTOMER], "meta": {"mode": "cursor", "nextCursor": "c2"}}
    )
    app = make_app(stub(transact(customers=more)))
    async with signed_in(app) as web:
        html = (await web.get("/orders")).text
    assert "The first 25 customers" in html and 'href="/customers"' in html


async def test_every_order_status_renders_as_itself_not_as_unknown():
    """`PILL_TONES` had no `orders` entry at all, so every real order status on
    this page rendered "Unknown: succeeded" in the neutral tone and warn-logged
    a row at a time. The four statuses are Conduit's own
    (`OrderExternalResponseDto`) and `projections._LADDERS["orders"]`'s."""
    rows = [
        {**ORDER, "id": f"ord_{state}", "status": state}
        for state in ("pending", "succeeded", "failed", "cancelled")
    ]
    app = make_app(stub(transact(orders=rows)))
    async with signed_in(app) as web:
        html = (await web.get("/orders")).text

    for state, tone in (
        ("Pending", "wait"),
        ("Succeeded", "ok"),
        ("Failed", "bad"),
        ("Cancelled", "muted"),
    ):
        assert f'<span class="pill {tone}">{state}</span>' in html
    assert "Unknown:" not in html


def test_the_order_pills_and_the_order_ladder_cannot_diverge():
    """The two lists are written by hand in two modules; a status added to the
    ladder (which drives the filter select and terminality) and not to the tones
    is the defect above, coming back. Same guard as the dashboard's NEXT_STEP.
    """
    from app import projections
    from app.web import PILL_TONES

    assert set(PILL_TONES["orders"]) == set(projections.STATE_RANKS["orders"])
    # And terminality still agrees: nothing terminal is toned as in-flight.
    for state in projections.TERMINAL["orders"]:
        assert PILL_TONES["orders"][state] != "wait"


async def test_the_orders_list_renders_both_legs_and_the_status():
    """One Amount column, both legs in it. Each leg names its own asset, so the
    old Pair column was printing USD and EUR a second time each."""
    app = make_app(
        stub({("GET", "/v2/orders"): page([ORDER]), ("GET", f"/v2/orders/{ORDER_ID}"): page([])})
    )
    async with signed_in(app) as web:
        response = await web.get("/orders")
    assert ORDER_ID in response.text
    assert "1000.00 USD" in response.text and "912.30 EUR" in response.text


async def test_a_real_order_payload_lists_its_amounts_rather_than_em_dashes():
    """The regression this slice was sent to find. Against a **live-captured**
    order — not the hand-written stub, which is what let the bug live — the list
    renders both legs. Before the fix every real row read `— —`, because
    `order_view` was reading `sourceAmount`/`destinationAmount`: keys that belong
    to a quote option and appear on no order Conduit has ever sent."""
    live = json.loads(
        (ROOT / "tests" / "fixtures" / "order_live_conversion_usd_eur.json").read_text()
    )
    app = make_app(stub({("GET", "/v2/orders"): page([live])}))
    async with signed_in(app) as web:
        response = await web.get("/orders")
    assert "19.00 USD" in response.text and "16.35 EUR" in response.text
    # The em-dash fallback only ever stands in for something Conduit did not
    # send; this row has both, so neither cell may show one.
    assert "— →" not in response.text and "→ —" not in response.text


async def test_an_unreadable_order_list_is_not_rendered_as_an_empty_one():
    """The test's own name, finally asserted. It used to require *both* the
    problem banner and "No orders on this page." — enshrining the contradiction
    it was named after: the page said "Conduit is down" and "there are none" at
    the same time, and only one of those can be true. The handler substitutes an
    empty `Page` on failure, so every downstream "no rows" sentence has to branch
    on the problem or it is reading a stub as a fact."""
    app = make_app(
        stub({("GET", "/v2/orders"): httpx.Response(503, json={"type": "UNAVAILABLE", "title": "Down"})})
    )
    async with signed_in(app) as web:
        response = await web.get("/orders")
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    assert "The list could not be read — see above." in response.text
    assert "No orders on this page." not in response.text
    assert "Showing 0 order" not in response.text


async def test_the_orders_pager_keeps_its_filters():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [ORDER],
                "meta": {
                    "mode": "cursor",
                    "nextCursor": "next&cursor",
                    "previousCursor": None,
                    "total": 1,
                },
            },
        )

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/orders?status=pending&status=failed&clientReferenceId=zzz")
    assert "status=pending" in response.text and "status=failed" in response.text
    assert "clientReferenceId=zzz" in response.text
    assert "next%26cursor" in response.text


async def test_the_orders_status_filter_is_checkbox_pills_that_round_trip():
    """Presentation only. The cramped `<select multiple size="4">` is now a row
    of `.pill-group` chips, and what is inside them is still ordinary checkboxes
    named `status` carrying `conversions.ORDER_STATUSES` — the same vocabulary
    the route validates against, from the same source. Two ticks therefore leave
    as the repeated `?status=a&status=b` the route has always parsed, and come
    back on screen re-checked. Nothing in `app/web/convert.py` moved."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/orders":
            seen.append(request.url.query.decode())
            return page([ORDER])
        return page([CUSTOMER])

    app = make_app(handler)
    async with signed_in(app) as web:
        html = (await web.get("/orders?status=pending&status=failed")).text

    filters = html.split('<form method="get" action="/orders"')[1].split("</form>")[0]
    assert '<div class="pill-group"' in filters and "<select" not in filters
    # One real checkbox per status, and not one more: the pills are the enum.
    for value in conversions.ORDER_STATUSES:
        assert f'<input type="checkbox" name="status" value="{value}"' in filters
    assert filters.count('name="status"') == len(conversions.ORDER_STATUSES)
    # The wire, which is the load-bearing half: two repeated `status=`, exactly
    # what the multi-select posted.
    assert seen[0].count("status=") == 2
    assert "status=pending" in seen[0] and "status=failed" in seen[0]
    # And the round trip: the two that were asked for come back checked, the
    # two that were not stay off.
    for value in ("pending", "failed"):
        assert f'<input type="checkbox" name="status" value="{value}" checked>' in filters
    for value in ("succeeded", "cancelled"):
        assert f'<input type="checkbox" name="status" value="{value}" checked>' not in filters
    # Hidden, never *removed* — same rule and same reason as `.segmented input`
    # (see the lock-side test): `display: none` would take every pill out of the
    # tab order, so it is pinned here rather than left to a stylesheet sweep.
    rule = (ROOT / "static" / "styles.css").read_text().split(".pill-group input {")[1]
    rule = rule[: rule.index("}")]
    assert "opacity: 0" in rule
    assert "display: none" not in rule and "visibility: hidden" not in rule


async def test_the_order_detail_shows_the_rate_the_lock_and_the_transactions():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"/orders/{ORDER_ID}")
    assert "0.9123" in response.text and "1002.00 USD" in response.text
    assert "Execution must start by" in response.text
    assert f'href="/transactions/txn_conv_1"' in response.text
    assert "Execute" in response.text and "Cancel" in response.text
    # Non-terminal: it keeps looking.
    assert 'hx-trigger="every 15s"' in response.text


async def test_the_order_head_names_the_customer_and_keeps_the_id():
    """An order DTO carries `customerId` and no name,
    so the head takes it from the one bounded customers read — which does not
    depend on the order and is therefore gathered with it, costing the page no
    latency. The id is demoted to the mono line under the name, not dropped: it
    is still what an operator copies.

    A read that answers with no such customer leaves the noun heading the page,
    exactly as before — never a blank heading and never a guess.
    """
    app = make_app(
        stub(routes(extra={("GET", "/v2/customers"): page([CUSTOMER])}))
    )
    async with signed_in(app) as web:
        named = (await web.get(f"/orders/{ORDER_ID}")).text
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): page([])})))
    async with signed_in(app) as web:
        anonymous = (await web.get(f"/orders/{ORDER_ID}")).text

    assert "<h1>ZZZTEST Ltd</h1>" in named
    assert f'<p class="head-id num">{ORDER_ID}</p>' in named
    assert "<h1>Order</h1>" in anonymous and f'<p class="head-id num">{ORDER_ID}</p>' in anonymous


def row(label: str, html: str) -> str:
    """The value cell of the raw panel's row carrying this label, or `""` when
    the page has no such row. A substring assertion cannot tell "the address is
    withheld" from "the address is gone", and the mask turns on the difference."""
    marker = f'>{label}</th><td class="num">'
    if marker not in html:
        return ""
    start = html.index(marker) + len(marker)
    return html[start : html.index("</td>", start)]


# An org crypto offramp: the console never creates one, but the orders list is
# org-wide and its detail page walks whatever arrived. The recipient block is
# the pinned `autoPayout.recipient` — coordinates from the live capture in
# `tests/e2e/sandbox_evidence/composite_order_autopayout_refused.json`, the
# natural-person fields (`dateOfBirth`, `phone`) from the us/individual branch
# of the same schema in `contracts/openapi_production.json`. One recipient
# carrying the whole set, because the page has to be safe for all of it.
AUTO_PAYOUT_ORDER = {
    **ORDER,
    "type": "OFFRAMP",
    "autoPayout": {
        "rail": "swift",
        "purpose": "payment_for_goods_or_services",
        "recipient": {
            "type": "individual",
            "accountNumber": "000123456789",
            "iban": "DE89370400440532013000",
            "bic": "MOCKDEFFXXX",
            "bankName": "Sandbox Mock Bank",
            "firstName": "ZZZTEST Ada",
            "lastName": "Lovelace",
            "dateOfBirth": "1985-04-12",
            "phone": "+49 30 5550199",
            "postalAddress": {
                "addressLine1": "ZZZTEST Strasse 1",
                "city": "Berlin",
                "country": "DEU",
                "postalCode": "10115",
            },
            "bankAddress": {
                "addressLine1": "123 Sandbox Way",
                "city": "Testville",
                "country": "DEU",
                "postalCode": "00000",
            },
        },
    },
}


async def test_an_orders_raw_panel_masks_the_auto_payout_coordinates():
    """`order_rows` is `payments.generic_rows`, which walked every scalar
    of the order — so an offramp order's chained payout published the payee's
    full IBAN and account number to anyone holding `console.view`.
    """
    app = make_app(stub(routes(extra={
        ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(200, json=AUTO_PAYOUT_ORDER),
    })))
    async with signed_in(app) as web:
        html = (await web.get(f"/orders/{ORDER_ID}")).text

    assert "000123456789" not in html and "DE89370400440532013000" not in html
    assert row("Auto payout · Recipient · Account number", html) == "••••6789"
    assert row("Auto payout · Recipient · Iban", html) == "••••3000"
    # Public routing data, whole: masking a BIC or a bank name helps nobody.
    assert row("Auto payout · Recipient · Bic", html) == "MOCKDEFFXXX"
    assert row("Auto payout · Recipient · Bank name", html) == "Sandbox Mock Bank"
    assert "123 Sandbox Way" in html  # the bank's own address


async def test_an_orders_raw_panel_publishes_no_date_of_birth_phone_or_address():
    """The natural-person data on the same block. Each row
    keeps its label and loses its value — an operator can see that Conduit holds
    a date of birth for this payee without the console printing it."""
    app = make_app(stub(routes(extra={
        ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(200, json=AUTO_PAYOUT_ORDER),
    })))
    async with signed_in(app) as web:
        html = (await web.get(f"/orders/{ORDER_ID}")).text

    assert "1985-04-12" not in html
    assert "5550199" not in html
    assert "ZZZTEST Strasse 1" not in html and "10115" not in html
    assert row("Auto payout · Recipient · Date of birth", html) == "withheld"
    assert row("Auto payout · Recipient · Phone", html) == "withheld"
    assert row("Auto payout · Recipient · Postal address", html) == "withheld"
    # The name stays: it is what an operator reconciles a payment against.
    assert "Lovelace" in html


async def test_a_settled_order_stops_refreshing_and_offers_no_actions():
    app = make_app(
        stub(
            {
                ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(
                    200, json={**ORDER, "status": "succeeded", "executedAt": "2026-08-28T11:00:00Z"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/orders/{ORDER_ID}")
    assert "settled — live refresh stopped" in response.text
    assert 'hx-trigger="every 15s"' not in response.text
    assert "terminal state" in response.text


async def test_an_unknown_order_status_renders_neutrally_and_enables_nothing():
    app = make_app(
        stub(
            {
                ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(
                    200, json={**ORDER, "status": "reticulating"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/orders/{ORDER_ID}")
    assert "Unknown: reticulating" in response.text
    assert "No action available" in response.text
    # Never inferred terminal: it keeps watching.
    assert 'hx-trigger="every 15s"' in response.text


async def test_a_failed_order_shows_conduits_failure_code():
    app = make_app(
        stub(
            {
                ("GET", f"/v2/orders/{ORDER_ID}"): httpx.Response(
                    200,
                    json={
                        **ORDER,
                        "status": "failed",
                        "failureCode": "insufficient_funds",
                        "failureMessage": "The source did not cover the total debit.",
                    },
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/orders/{ORDER_ID}")
    assert "insufficient_funds" in response.text
    assert "did not cover the total debit" in response.text


# --- execute and cancel -----------------------------------------------------------------


async def test_execute_goes_through_the_ledger_with_an_idempotency_key(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/orders/{ORDER_ID}/execute"): httpx.Response(202, json=ORDER)
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/orders/{ORDER_ID}/execute")
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.type == "order_execute" and op.state == "confirmed"
    assert op.request_path == f"/v2/orders/{ORDER_ID}/execute"
    assert "msg=Order+execute+accepted" in response.headers["HX-Redirect"]


async def test_insufficient_funds_is_shown_as_conduit_said_it(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/orders/{ORDER_ID}/execute"): httpx.Response(
                        422,
                        json={
                            "type": "INSUFFICIENT_FUNDS",
                            "title": "Insufficient funds",
                            "detail": "Fund the source and execute again before the lock expires.",
                            "resolution": "Fund the source and execute again.",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/orders/{ORDER_ID}/execute")
    message = unquote_plus(response.headers["HX-Redirect"])
    # A3: this console's own two sentences for the code — the same operational
    # fact, and Conduit's own wording nowhere in a URL.
    assert "The funding account does not hold enough" in message
    assert "fund the account or lower the amount" in message
    assert "Insufficient funds" not in message
    # The order stays pending, so a second attempt after funding is a new
    # operation rather than a blocked one.
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.state == "rejected"


async def test_an_unresolved_execute_hides_the_button_and_shows_the_panel(session):
    """An `order_execute` only earns a `conduit_resource_id`
    when it confirms, so an unresolved one was invisible to the order page and
    the operator was offered Execute again while one was still outstanding."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/orders/{ORDER_ID}/execute"): httpx.Response(
                        500, json={"type": "OOPS", "title": "Upstream"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        await post(web, f"/orders/{ORDER_ID}/execute")
        response = await web.get(f"/orders/{ORDER_ID}")

    op = (await session.execute(select(Operation))).scalars().one()
    assert op.state == "outcome_unknown" and op.conduit_resource_id is None
    assert "unresolved <code>order_execute</code>" in response.text
    assert "Result being confirmed" in response.text
    assert '<button type="submit" class="primary">Execute</button>' not in response.text


async def test_cancel_goes_through_the_ledger(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/orders/{ORDER_ID}/cancel"): httpx.Response(
                        200, json={**ORDER, "status": "cancelled"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/orders/{ORDER_ID}/cancel")
    op = (await session.execute(select(Operation))).scalars().one()
    assert op.type == "order_cancel" and op.state == "confirmed"
    assert "msg=Order+cancel+accepted" in response.headers["HX-Redirect"]


# --- sandbox ----------------------------------------------------------------------------


async def test_the_sandbox_order_simulators_send_the_paths_the_spec_declares():
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    (
                        "POST",
                        f"/v2/sandbox/orders/{ORDER_ID}/simulate/conversion-failed",
                    ): httpx.Response(200, json=ORDER),
                    (
                        "POST",
                        f"/v2/sandbox/orders/{ORDER_ID}/simulate/rate-lock-expired",
                    ): httpx.Response(200, json=ORDER),
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        for action in ("conversion-failed", "rate-lock-expired"):
            response = await post(
                web, f"/orders/{ORDER_ID}/simulate", encoded({"action": action})
            )
            assert f"Simulated+{action}" in response.headers["HX-Redirect"]
    sent = [(path, body) for _, path, body in calls if "sandbox" in path]
    assert [p for p, _ in sent] == [
        f"/v2/sandbox/orders/{ORDER_ID}/simulate/conversion-failed",
        f"/v2/sandbox/orders/{ORDER_ID}/simulate/rate-lock-expired",
    ]
    # Both simulators take an empty JSON object (the sandbox spec declares a
    # required body with no required properties).
    assert [json.loads(b) for _, b in sent] == [{}, {}]


async def test_an_unknown_simulated_action_is_refused_without_a_call():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, f"/orders/{ORDER_ID}/simulate", encoded({"action": "cosign"}))
    assert "Unknown+simulated+action" in response.headers["HX-Redirect"]
    assert [c for c in calls if "sandbox" in c[1]] == []


# --- roles, CSRF, double-click ----------------------------------------------------------


async def test_a_viewer_may_quote_but_not_choose_or_send():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        quoted = await post(web, CONVERT + "/quote", quote_form())
        chosen = await post(web, CONVERT + "/select", quote_form())
        executed = await post(web, f"/orders/{ORDER_ID}/execute")
    assert quoted.status_code == 200 and "0.9123" in quoted.text
    assert "Needs <code>order.create</code>" in quoted.text and "Choose" not in quoted.text
    assert chosen.status_code == 403 and executed.status_code == 403


async def test_a_viewer_reads_a_pending_confirmation_without_a_send_button(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
    op = (await session.execute(select(Operation))).scalars().one()
    async with signed_in(app, groups="readers") as viewer:
        response = await viewer.get(f"/convert/{op.id}")
        refused = await post(viewer, f"/convert/{op.id}")
    assert response.status_code == 200 and "needs the <code>order.create</code> permission" in response.text
    assert 'id="confirm-submit"' not in response.text
    assert refused.status_code == 403


async def test_a_role_holding_only_order_execute_gets_one_of_the_two_buttons():
    """A pending order offers execute and cancel, gated separately. Holding one
    must leave the other's control off the page — and must not fall through to
    the "no action available" sentence, which would be false."""
    app = make_app(stub(routes()))
    async with signed_in_as(app, "order.execute") as web:
        response = await web.get(f"/orders/{ORDER_ID}")

    assert response.status_code == 200
    assert '<button type="submit" class="primary">Execute</button>' in response.text
    assert "Cancel this order" not in response.text
    assert "No action available" not in response.text
    assert forbidden_affordances(app, response.text, {"console.view", "order.execute"}) == []


async def test_a_role_holding_neither_order_verb_is_shown_no_actions_block():
    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.create") as web:
        response = await web.get(f"/orders/{ORDER_ID}")

    assert "<h2>Actions</h2>" not in response.text
    assert "Execute</button>" not in response.text and "Cancel this order" not in response.text
    assert forbidden_affordances(app, response.text, {"console.view", "payout.create"}) == []


async def test_every_convert_mutation_needs_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (
            CONVERT + "/quote",
            CONVERT + "/select",
            f"/orders/{ORDER_ID}/execute",
            f"/orders/{ORDER_ID}/cancel",
            f"/orders/{ORDER_ID}/simulate",
        ):
            response = await web.post(
                url,
                content=quote_form(),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403 and "CSRF" in response.text, url


async def test_the_convert_forms_carry_hx_sync_against_a_double_click():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        form = await web.get(CONVERT)
        quoted = await post(web, CONVERT + "/quote", quote_form())
        order = await web.get(f"/orders/{ORDER_ID}")
    for html in (form.text, quoted.text, order.text):
        assert 'hx-sync="this:drop"' in html
    # The quote page's route picker (`#customer_id`) can
    # swap `main`, so both this page's POST forms freeze it and the route form's
    # button for the flight — the payout/transfer freeze, applied here.
    guards = re.findall(r'hx-disabled-elt="([^"]*)"', quoted.text)
    assert guards, "the quote page lost its disabled-elt guards"
    for guard in guards:
        assert "find button" in guard
        assert "#customer_id" in guard and "#convert-route button" in guard


async def test_every_form_that_can_receive_a_422_targets_main(session):
    """Htmx swaps a 422 (base.html's response config), and these three routes
    answer one with a whole re-rendered page — so each has to lift its <main>
    out rather than inject the document into itself."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        quoted = await post(web, CONVERT + "/quote", quote_form())
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        confirm = await web.get(f"/convert/{op.id}")
    for html in (quoted.text, confirm.text):
        assert 'hx-target="main" hx-select="main" hx-swap="outerHTML"' in html


async def test_the_confirm_form_cannot_be_double_clicked(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await selected(web)
        op = (await session.execute(select(Operation))).scalars().one()
        response = await web.get(f"/convert/{op.id}")
    assert 'hx-sync="this:drop"' in response.text
    assert 'hx-disabled-elt="find button"' in response.text


async def test_a_direction_flip_drops_the_promised_funding_pick_not_the_way_back():
    """An operator who arrived from a fedwire
    payout and then flipped the selects to convert INTO EUR was promised a
    funding account the payout page immediately disables ("EUR — not on
    fedwire"). The way-back survives — the route is still theirs — but it names
    no account: a promise the target page would break is not a hand-off."""
    flipped = (
        f"?source={VID}&destination={EUR_VID}&purpose=payment_for_goods_or_services"
        "&rail=fedwire&recipientType=business&destinationCountry=USA"
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat((await web.get(CONVERT + flipped)).text)

    back = html.split(f'href="/customers/{CID}/payouts/new?', 1)[1].split('"', 1)[0]
    params = dict(pair.split("=", 1) for pair in back.split("&"))
    assert params == PAYOUT_ROUTE, "the route survives the flip"
    assert "virtualAccountId" not in back, "promised a funding pick fedwire refuses"


async def test_a_lone_crafted_route_param_earns_no_payout_provenance():
    """`?purpose=x` alone must not make this page
    claim it was 'started from a payout on ``' with an empty rail — provenance
    is asserted only when the rail that pinned the hand-off is present."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat((await web.get(CONVERT + "?purpose=payment_for_goods_or_services")).text)
    assert "started from a payout" not in html
    assert "/payouts/new?" not in html


# --- the flow starts at the action --------------------------------------------------

GLOBAL = "/convert"


async def test_the_global_convert_route_and_the_scoped_one_are_the_same_form():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        scoped = await web.get(CONVERT)
        picked = await web.get(f"{GLOBAL}?customer_id={CID}")

    for response in (scoped, picked):
        assert response.status_code == 200
        row = response.text.split('id="convert-route"')[1].split("</form>")[0]
        assert f'hx-get="{GLOBAL}"' in row and f'action="{GLOBAL}"' in row
        assert "change from:#customer_id" in row
        assert f'value="{CID}"' in row
        assert VID in response.text  # this customer's own accounts


async def test_convert_with_no_customer_asks_for_one_and_asserts_nothing():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(GLOBAL)

    assert response.status_code == 200
    body = " ".join(response.text.split())
    assert "Pick the customer" in body
    # The three empty states are about a customer that WAS read. None of them
    # may be claimed about nobody.
    assert "No active virtual account" not in body
    assert "could not be read" not in body
    assert "Nothing to convert into" not in body
    assert [c for c in calls if "virtual-accounts" in c[1]] == []


async def test_the_convert_flow_keeps_the_ribbon_on_transact_at_the_new_route():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (GLOBAL, CONVERT):
            html = (await web.get(url)).text
            assert '<a class="item on" href="/convert" aria-current="page">Convert</a>' in html


async def test_the_reads_this_page_makes_are_the_two_it_names():
    """The read budget, pinned where it can drift. This customer's accounts, and
    ONE bounded customers read for the source-customer picker — gathered with it
    (`with_customer_names`), never serialised behind it, never one per row. With
    nobody picked the customer-scoped read is not made at all, because there is
    nobody to make it about."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await web.get(f"{GLOBAL}?customer_id={CID}")
    assert sorted(c[1] for c in calls) == sorted(["/v2/customers", ACCOUNTS])

    calls.clear()
    async with signed_in(app) as web:
        await web.get(GLOBAL)
    assert [c[1] for c in calls] == ["/v2/customers"]


async def test_route_params_with_no_customer_earn_no_provenance_and_no_dead_link():
    """The global /convert accepts route
    params with nobody picked; building the way-back anyway claimed payout
    provenance over a link to /customers//payouts/new — a 404. The real
    hand-off always carries its customer, so a customer-less route is a crafted
    URL and earns nothing."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = flat((await web.get(
            "/convert?purpose=payment_for_goods_or_services&rail=fedwire"
            "&recipientType=business&destinationCountry=USA"
        )).text)
    assert "started from a payout" not in html
    assert "/customers//payouts" not in html


@pytest.mark.parametrize("action", ["execute", "cancel"])
async def test_a_spent_order_nonce_replayed_at_another_order_is_refused(session, action):
    """`by_intent` is scoped to the operation type
    and nothing else, so the nonce spent executing (or cancelling) order A
    answered the same verb pressed on order B — and the route flashed
    "Order {action} accepted." for an order that was never touched."""
    other = "ord_2xReplayTarget9wT3sYbC7d"
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    # Both stubbed to succeed: the only thing that may differ
                    # between the two submits is whether the call is made at all.
                    ("POST", f"/v2/orders/{ORDER_ID}/{action}"): httpx.Response(
                        202, json=ORDER
                    ),
                    ("POST", f"/v2/orders/{other}/{action}"): httpx.Response(
                        202, json={**ORDER, "id": other}
                    ),
                }
            ),
            calls,
        )
    )
    nonce = minted_intent()
    async with signed_in(app) as web:
        first = await post(web, f"/orders/{ORDER_ID}/{action}", encoded({"intent": nonce}))
        replay = await post(web, f"/orders/{other}/{action}", encoded({"intent": nonce}))

    assert f"msg=Order+{action}+accepted" in first.headers["HX-Redirect"]
    # Order B never reached the wire and never got an operation of its own —
    # both true before the guard existed too. Only the sentence was wrong.
    assert [c for c in calls if c[1] == f"/v2/orders/{other}/{action}"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(
                Operation.request_path == f"/v2/orders/{other}/{action}"
            )
        )
    ) == 0
    landing = unquote_plus(replay.headers.get("HX-Redirect") or replay.headers["location"])
    assert f"Order {action} accepted." not in landing
    assert "had already been used" in landing
