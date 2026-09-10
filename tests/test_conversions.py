"""`app.conversions` — the quote/order shapes, and what may be done to an order.

The two facts worth a test on their own: a conversion quote is discriminated by
what it *omits* (no `destinationCountry`), and `QuoteRedemptionOrderDto` must
carry neither `amount` nor `lockSide` — both are inherited from the option, and
sending them is a 400.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app import accounts, conversions, payments

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


# --- the quote ------------------------------------------------------------------------


def test_a_conversion_quote_names_no_destination_country():
    body = conversions.quote_request(
        source="USD", destination="EUR", amount_text="1000.00", lock_side="source"
    )
    assert body == {
        "source": {"code": "USD"},
        "destination": {"code": "EUR"},
        "lockSide": "source",
        "amount": "1000.00",
    }
    # The absence *is* the discriminator (CreateQuoteRequestDto: "different
    # assets + no country prices a conversion").
    assert "destinationCountry" not in body


def test_the_withdrawal_quote_mode_is_the_other_one():
    """Both modes live on `POST /v2/quotes`; only the payout one names a country."""
    withdrawal = payments.quote_request(
        source="USD", destination="USD", destination_country="USA", amount_text="1.00"
    )
    assert withdrawal["destinationCountry"] == "USA"
    assert withdrawal["source"]["code"] == withdrawal["destination"]["code"]


def test_the_lock_side_vocabulary_is_the_dtos():
    assert conversions.LOCK_SIDE_VALUES == ("source", "destination")


def test_opposite_assets_are_the_convert_pair_minus_the_source():
    assert conversions.opposite_assets("USD") == ["EUR"]
    assert conversions.opposite_assets("eur") == ["USD"]


def test_a_source_outside_the_convert_pair_can_land_nowhere():
    """The picker now names any currency Conduit allows, so an SGD account
    exists — and offering SGD→USD offers a pair `CONVERT_ASSETS` exists to rule
    out. The empty string is the same answer for the same reason: it names no
    currency Conduit will quote out of."""
    assert conversions.opposite_assets("GBP") == []
    assert conversions.opposite_assets("SGD") == []
    assert conversions.opposite_assets("") == []


def test_the_convert_pair_does_not_follow_the_discovered_account_currencies():
    """What an account may hold is discovered per customer; what Conduit will
    quote a conversion between is not, so a third discovered currency must not
    reach the Convert page as an unquotable pair.

    The conjunction is the test. `test_allowed_assets_offers_every_currency_discovery_names`
    owns one side and `test_opposite_assets_are_the_convert_pair_minus_the_source`
    the other; neither names the other module, so both keep passing if
    `CONVERT_ASSETS` is "fixed" to follow the discovered list. This is also the
    only assertion of that constant's value in the suite: the prose arguing the
    rule beside `CONVERT_ASSETS` was cut to one line (0708229) on the grounds
    that this test would carry the argument, so deleting it too leaves the rule
    asserted nowhere.
    """
    discovered = {"fields": [{"pointer": "/asset/code", "allowedValues": ["USD", "EUR", "GBP"]}]}
    assert accounts.allowed_assets(discovered) == ["USD", "EUR", "GBP"]
    assert conversions.CONVERT_ASSETS == ("USD", "EUR")
    assert conversions.opposite_assets("USD") == ["EUR"]


# --- the order body -------------------------------------------------------------------


def test_the_order_body_is_the_quote_redemption_dto():
    body = conversions.order_body(
        quote_option_id="qop_1", source_id="vac_src", destination_id="vac_dst"
    )
    assert body == {
        "quoteOptionId": "qop_1",
        "source": {"type": "virtual_account", "id": "vac_src"},
        "destination": {"type": "virtual_account", "id": "vac_dst"},
    }
    # Inherited from the option; `additionalProperties: false` refuses them.
    assert "amount" not in body and "lockSide" not in body
    # Injected at call time by `app.conduit.execute`, never stored — or every
    # request_hash would be unique (OPERATIONS_SPEC §5).
    assert "clientReferenceId" not in body
    # Not sent: the console renders an explicit Execute step.
    assert "autoExecute" not in body


def test_the_selection_carries_the_expiry_and_the_price():
    from tests.payments_fixtures import CONVERSION_QUOTE

    view = payments.quote_view(CONVERSION_QUOTE)
    chosen = conversions.selection(view, view["options"][0])
    assert chosen["quoteOptionId"] == "qop_conv_a"
    assert chosen["expiresAt"] == CONVERSION_QUOTE["expiresAt"]
    assert chosen["endUserRate"] == "0.9123"
    assert chosen["sourceAmount"] == "1000.00 USD"
    assert chosen["destinationAmount"] == "912.30 EUR"


def test_an_option_is_found_by_id_and_a_stranger_is_not():
    from tests.payments_fixtures import CONVERSION_QUOTE

    view = payments.quote_view(CONVERSION_QUOTE)
    # A conversion option carries no rail (live, 2026-08-28) — the panel shows
    # the em dash `quote_view` substitutes rather than an empty cell.
    assert conversions.option_of(view, "qop_conv_a")["rail"] == "—"
    assert conversions.option_of(view, "qop_nope") is None
    assert conversions.option_of(None, "qop_conv_a") is None


# --- the order view -------------------------------------------------------------------


def test_the_order_view_keeps_amounts_as_strings():
    from tests.payments_fixtures import ORDER

    view = conversions.order_view(ORDER)
    assert view["source_amount"] == "1000.00 USD"
    assert view["destination_amount"] == "912.30 EUR"
    assert view["total_debit"] == "1002.00 USD"
    assert view["transactions"] == ["txn_conv_1"]
    assert {row["label"] for row in view["rate"]} == {
        "End user rate",
        "Reference rate",
        "Total spread bps",
    }


def test_the_order_view_reads_the_amounts_a_real_order_actually_carries():
    """The shape pin. `order_view` used to read `sourceAmount`/`destinationAmount`
    — keys that exist on a **quote option** and on nothing else. No order in the
    pinned spec and no order on the live sandbox has ever had them, so every real
    row rendered its two amounts as em-dashes while `payments_fixtures.ORDER`,
    which had invented the same two keys, kept this file green.

    Captured 2026-08-29 from `GET /v2/orders/{id}` on the real sandbox.
    """
    live = json.loads((FIXTURES / "order_live_conversion_usd_eur.json").read_text())
    assert "sourceAmount" not in live and "destinationAmount" not in live

    view = conversions.order_view(live)
    assert view["source_amount"] == "19.00 USD"
    assert view["destination_amount"] == "16.35 EUR"
    assert view["source_asset"] == "USD" and view["destination_asset"] == "EUR"
    assert view["total_debit"] == "20.00 USD"

    # …and the fabricated fixture the rest of the suite runs on speaks the same
    # dialect, so this class of bug cannot come back through the stub.
    from tests.payments_fixtures import ORDER

    assert "sourceAmount" not in ORDER and "destinationAmount" not in ORDER
    assert set(ORDER) - set(live) == set()


def test_an_order_with_nothing_on_it_renders_empty_rather_than_raising():
    view = conversions.order_view(None)
    assert view["source_amount"] == "" and view["fees"] == [] and view["rate"] == []


@pytest.mark.parametrize(
    "status,trigger,execute,cancel",
    [
        ("pending", None, True, True),
        ("pending", "client", False, True),
        ("succeeded", None, False, False),
        ("failed", None, False, False),
        ("cancelled", None, False, False),
        # plan v2 §7: a status this build has never heard of enables nothing.
        ("settling_somehow", None, False, False),
        (None, None, False, False),
    ],
)
def test_only_a_pending_unclaimed_order_may_be_executed(status, trigger, execute, cancel):
    order = {"status": status, "executionTrigger": trigger}
    assert conversions.can_execute(order) is execute
    assert conversions.can_cancel(order) is cancel


def test_execute_is_offered_on_an_auto_execute_order_too():
    """The spec is explicit: `/execute` is callable regardless of `autoExecute`."""
    assert conversions.can_execute({"status": "pending", "autoExecute": True}) is True


def test_the_order_statuses_come_from_the_projection_ladder():
    from app import projections

    assert conversions.ORDER_STATUSES == tuple(projections.STATE_RANKS["orders"])
    # plan v2 §11: no `expired`, no `completed`.
    assert "expired" not in conversions.ORDER_STATUSES


def test_the_crypto_only_cosign_simulator_is_not_wired():
    """`simulate/cosign` is the non-custodial signing leg — out of v1 scope, so
    the console offers the two order simulators that apply to a fiat conversion
    and nothing else."""
    assert conversions.SIMULATE_VALUES == ("conversion-failed", "rate-lock-expired")
    assert "cosign" not in conversions.SIMULATE_PATH


# --- money ----------------------------------------------------------------------------


def test_no_float_ever_touches_a_conversion():
    """`float("0.1")` is not 0.1. A grep, because the failure mode is one
    careless `float(...)` in a future edit."""
    for module in (
        ROOT / "app" / "conversions" / "__init__.py",
        ROOT / "app" / "web" / "convert.py",
        ROOT / "app" / "web" / "transfers.py",
    ):
        source = module.read_text()
        assert "float(" not in source, module
        tree = ast.parse(source)
        assert [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ] == []
