"""`app/accounts` — pure logic, no HTTP, no DB (plan v2 §7 Accounts).

The deposit-instruction fixtures are built from the pinned OpenAPI's three
`depositInstructions[]` variants: a real one can only be captured from a funded,
activated account, and `tests/e2e/03_accounts.py` saves that alongside these.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import accounts, forms

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


USD = fixture("virtual_account_usd.json")
EUR = fixture("virtual_account_eur.json")
REQUIREMENTS = fixture("feature_requirements_virtual_account_usd.json")


# --- discovery -------------------------------------------------------------------------


def test_allowed_assets_is_discovery_s_own_list_in_discovery_s_own_order():
    """Verbatim, order included — the fixture says `["EUR", "USD"]` and that is
    what the picker renders. Nothing here re-sorts Conduit's answer into a
    preference this console no longer has."""
    assert accounts.allowed_assets(REQUIREMENTS) == ["EUR", "USD"]


@pytest.mark.parametrize(
    "allowed,expected",
    [
        (["USD"], ["USD"]),
        (["EUR"], ["EUR"]),
        # Every currency named is offered — the console holds no list to narrow by.
        (["USD", "EUR", "GBP", "CHF"], ["USD", "EUR", "GBP", "CHF"]),
        # The point of deriving from discovery: a currency this console has never
        # had a constant for is offered the moment discovery names it.
        (["GBP"], ["GBP"]),
        # Only junk is dropped, and only because it cannot name a currency.
        (["USD", "", "  ", None, 7], ["USD"]),
    ],
)
def test_allowed_assets_offers_every_currency_discovery_names(allowed, expected):
    snapshot = {"fields": [{"pointer": "/asset/code", "allowedValues": allowed}]}
    assert accounts.allowed_assets(snapshot) == expected


def test_a_snapshot_that_names_no_currency_offers_none():
    """Inverted deliberately. "Unconstrained" used to mean "the hardcoded USD/EUR
    pair stands"; with no such pair there is nothing left for it to mean, and a
    picker built out of nothing would offer a currency Conduit never said it
    would accept."""
    assert accounts.allowed_assets({"fields": []}) == []
    assert accounts.allowed_assets({"fields": [{"pointer": "/asset/code"}]}) == []
    assert accounts.allowed_assets(
        {"fields": [{"pointer": "/asset/code", "allowedValues": []}]}
    ) == []
    assert accounts.allowed_assets(None) == []


def test_states_allowed_assets_separates_the_list_conduit_sent_from_the_one_it_did_not():
    """`allowed_assets` answers `[]` to both, and the request page must not
    render "this customer may hold none" for a catalog it failed to read."""
    assert accounts.states_allowed_assets(REQUIREMENTS) is True
    assert (
        accounts.states_allowed_assets(
            {"fields": [{"pointer": "/asset/code", "allowedValues": []}]}
        )
        is True
    )
    assert accounts.states_allowed_assets({"fields": [{"pointer": "/asset/code"}]}) is False
    assert accounts.states_allowed_assets({"fields": []}) is False
    assert accounts.states_allowed_assets(None) is False


# --- the request body ------------------------------------------------------------------


def submission(asset: str = "USD") -> list[tuple[str, str]]:
    return [
        ("f.asset.code", asset),
        ("f.regulatoryHistory.hasUSBankAccount", "true"),
        ("f.regulatoryHistory.deniedBankAccount", "false"),
        ("f.regulatoryHistory.hasPoliticallyExposedPersons", "false"),
        ("f.certification.termsAndConditions", "true"),
    ]


def test_request_body_is_a_feature_request_dto():
    model = forms.parse(REQUIREMENTS)
    values = forms.parse_submission(model, submission())
    assert forms.validate(model, values).ok

    body = accounts.request_body(model, values)
    assert body == {
        "type": "virtual_account",
        "asset": {"code": "USD"},
        "fields": {
            "regulatoryHistory": {
                "hasUSBankAccount": True,
                "deniedBankAccount": False,
                "hasPoliticallyExposedPersons": False,
            },
            "certification": {"termsAndConditions": True},
        },
    }


def test_request_body_carries_documents_at_the_top_level():
    model = forms.parse(REQUIREMENTS)
    values = forms.parse_submission(model, submission() + [("documentIds", "doc_1")])
    body = accounts.request_body(model, values)
    assert body["documentIds"] == ["doc_1"]
    assert "documentIds" not in body["fields"]


def test_the_submitted_asset_is_shape_checked_never_membership_checked():
    """Read before a model exists, and judged only on shape. Nothing local knows
    which currencies Conduit accepts any more, so a well-shaped code it has
    never heard of goes to Conduit and gets Conduit's own answer; only something
    that cannot be a currency code at all is dropped here."""
    assert accounts.submitted_asset(submission("EUR")) == "EUR"
    assert accounts.submitted_asset(submission("eur")) == "EUR"
    assert accounts.submitted_asset(submission(" sgd ")) == "SGD"
    assert accounts.submitted_asset(submission("GBP")) == "GBP"
    for malformed in ("US", "USDD", "12X", "US$", ""):
        assert accounts.submitted_asset(submission(malformed)) == ""
    assert accounts.submitted_asset([]) == ""


# --- features --------------------------------------------------------------------------


def test_feature_rows_mark_everything_but_virtual_accounts_inert():
    rows = accounts.feature_rows(
        {
            "features": [
                {"feature": "virtual_account", "isActive": False},
                {"feature": "crypto_wallet", "isActive": True},
                {"feature": "time_travel", "isActive": True},  # a future feature
            ]
        }
    )
    assert [r["supported"] for r in rows] == [True, False, False]
    assert [r["is_active"] for r in rows] == [False, True, True]
    assert rows[1]["note"] == rows[2]["note"] == "not available in this console"
    assert rows[2]["label"] == "Time travel"


def test_has_active_only_when_conduit_says_so():
    assert not accounts.has_active({"features": [{"feature": "virtual_account", "isActive": False}]})
    assert accounts.has_active({"features": [{"feature": "virtual_account", "isActive": True}]})
    assert not accounts.has_active({"features": [{"feature": "crypto_wallet", "isActive": True}]})
    assert not accounts.has_active(None)


# --- deposit instructions --------------------------------------------------------------


def rows_of(card: dict) -> dict[str, str]:
    return {row["label"]: row["value"] for row in card["rows"]}


def test_us_domestic_renders_rails_beneficiary_and_reference():
    card = accounts.deposit_cards(USD)[0]
    assert card["type"] == "us_domestic" and card["known"] and card["currency"] == "USD"
    rows = rows_of(card)
    assert rows["Account number"] == "9876543210"
    assert rows["Beneficiary name"] == "ZZZTEST Console E2E EOOD"
    assert rows["Payment reference"] == "VAC2XKJF9MQB7VN4HL1PR3W8T"
    assert rows["Bank name"] == "Lead Bank"
    # One row per network, each with its own routing number.
    assert rows["ACH routing number"] == "101019644"
    assert rows["FEDWIRE routing number"] == "101019644"
    assert rows["RTP routing number"] == "101019644"
    notes = {row["label"]: row["note"] for row in card["rows"]}
    assert "same-day eligible" in notes["ACH routing number"]
    assert "fednow" in notes["RTP routing number"] and "1000000.00" in notes["RTP routing number"]


def test_swift_renders_the_correspondent_hop():
    card = accounts.deposit_cards(USD)[1]
    assert card["type"] == "swift" and card["known"]
    rows = rows_of(card)
    assert rows["Bank BIC"] == "LEADUS44XXX"
    assert rows["Correspondent bank"] == "Citibank N.A."
    assert rows["Correspondent BIC"] == "CITIUS33XXX"
    assert rows["Correspondent account number"] == "36123456"
    assert rows["Rail"] == "SWIFT"


def test_sepa_renders_iban_and_bic():
    cards = accounts.deposit_cards(EUR)
    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "sepa" and card["currency"] == "EUR"
    rows = rows_of(card)
    assert rows["IBAN"] == "DE89370400440532013000"
    assert rows["BIC"] == "COBADEFFXXX"
    assert rows["Rail"] == "SEPA"


def test_an_unknown_instruction_variant_still_renders_its_coordinates():
    """Forward-compat: a block type this build has never seen is labelled as
    unknown, and every scalar it carries is still shown — a coordinate the payer
    needs is not dropped because the console is a version behind."""
    card = accounts.deposit_cards(
        {
            "depositInstructions": [
                {
                    "type": "faster_payments",
                    "currency": "GBP",
                    "beneficiaryName": "ZZZTEST",
                    "sortCode": "20-00-00",
                    "accountNumber": "12345678",
                }
            ]
        }
    )[0]
    assert card["known"] is False and card["title"] == "Faster payments"
    rows = rows_of(card)
    assert rows["Account number"] == "12345678"
    assert rows["Sort code"] == "20-00-00"  # unlabelled, humanized, not dropped


def test_the_live_capture_renders_every_coordinate_it_carries():
    """`virtual_account_live_usd.json` — the SHAPE of a real active account read
    from staging by `tests/e2e/03_accounts.py`, with the bank's own coordinates
    replaced by the mock values its EUR sibling already carries (
    a real routing number and account number are not test data). It publishes no
    `paymentReference` and no bank BIC (both optional in the spec), which is
    exactly the variance the card has to survive without leaving a blank labelled
    row behind."""
    live = fixture("virtual_account_live_usd.json")
    card = accounts.deposit_cards(live)[0]
    rows = rows_of(card)
    assert card["type"] == "us_domestic" and card["known"]
    assert rows["Account number"] == "0000000000000000"
    assert rows["Bank name"] == "Sandbox Mock Bank"
    assert {"ACH routing number", "FEDWIRE routing number", "RTP routing number"} <= set(rows)
    assert "Payment reference" not in rows and "Bank BIC" not in rows
    assert all(row["value"] for row in card["rows"])  # no empty copy rows
    # The live beneficiary address is three lines: an `<input>` would silently
    # strip the newlines out of what the operator copies.
    multiline = {row["label"] for row in card["rows"] if row["multiline"]}
    assert multiline == {"Beneficiary address"}
    # Conduit *stated* three zeros here, so three zeros is what renders.
    assert accounts.balance_rows(live) == [
        {"code": "USD", "available": "0.00", "pending": "0.00", "frozen": "0.00"}
    ]


def test_structured_addresses_are_flattened_into_rows_not_dropped():
    """The three nested address objects are coordinates, and a scalars-only pass
    over the top level dropped all of them — on a swift or sepa block, where the
    free-text `beneficiaryAddress` is optional, that can mean a card with no
    payee address on it at all."""
    card = accounts.deposit_cards(
        {
            "depositInstructions": [
                {
                    "type": "swift",
                    "currency": "USD",
                    "beneficiaryName": "ZZZTEST",
                    "beneficiaryPostalAddress": {
                        "addressLine1": "1 Test Street",
                        "city": "Sofia",
                        "country": "BG",
                    },
                    "bank": {
                        "legalName": "Lead Bank",
                        "address": "1801 Main St",
                        "postalAddress": {"addressLine1": "1801 Main St", "city": "Kansas City"},
                    },
                    "correspondent": {
                        "name": "Citibank N.A.",
                        "address": "388 Greenwich St",
                        "bic": "CITIUS33XXX",
                        "postalAddress": {"addressLine1": "388 Greenwich St", "country": "US"},
                    },
                    "rails": [{"rail": "swift"}],
                }
            ]
        }
    )[0]
    rows = rows_of(card)
    assert rows["Beneficiary postal address · Address line1"] == "1 Test Street"
    assert rows["Beneficiary postal address · City"] == "Sofia"
    assert rows["Bank · Postal address · City"] == "Kansas City"
    assert rows["Correspondent · Postal address · Country"] == "US"
    # The labelled paths are still rendered once, under their own labels.
    assert rows["Bank name"] == "Lead Bank" and rows["Correspondent BIC"] == "CITIUS33XXX"
    assert "Bank · Legal name" not in rows
    # The card header's own keys never become rows.
    assert not any(label in rows for label in ("Type", "Currency"))


def test_no_instructions_is_an_empty_list_not_a_crash():
    assert accounts.deposit_cards({"depositInstructions": []}) == []
    assert accounts.deposit_cards(None) == []
    assert accounts.deposit_cards({"depositInstructions": ["nonsense"]}) == []


# --- balances and deposits --------------------------------------------------------------


def test_balance_rows_keep_amounts_as_decimal_strings():
    row = accounts.balance_rows(USD)[0]
    assert row == {"code": "USD", "available": "125000.00", "pending": "2500.50", "frozen": "0.00"}


def test_an_unstated_bucket_is_none_not_zero_and_keeps_the_account_asset():
    """Corrected 2026-08-30: this asserted `"0"` for a
    bucket Conduit never mentioned — the console inventing the reassuring
    number, and on `frozen` the reassuring one is the dangerous one. The house
    rule one level up (an account with no `balances` block renders an em-dash)
    is the same rule, and it does not stop at the block boundary."""
    rows = accounts.balance_rows({"asset": {"code": "EUR"}, "balances": [{}]})
    assert rows == [{"code": "EUR", "available": None, "pending": None, "frozen": None}]


def test_a_partial_balance_states_what_it_states_and_no_more():
    """The shape that made this a defect rather than a curiosity: a real balance
    object *can* carry one bucket and omit the others."""
    rows = accounts.balance_rows(
        {"balances": [{"available": {"code": "USD", "amount": "10.00"}}]}
    )
    assert rows == [{"code": "USD", "available": "10.00", "pending": None, "frozen": None}]


def test_deposits_are_narrowed_to_the_account_locally():
    items = [
        {"id": "txn_1", "destination": {"virtualAccountId": "vac_1"}},
        {"id": "txn_2", "destination": {"virtualAccountId": "vac_2"}},
        {"id": "txn_3", "destination": {"type": "wallet", "address": "0x0"}},
    ]
    assert [d["id"] for d in accounts.deposits_for(items, "vac_1")] == ["txn_1"]


def test_amount_reads_whichever_side_states_it():
    assert accounts.amount_of({"destination": {"assetAmount": {"amount": "10.00", "code": "USD"}}}) == "10.00 USD"
    assert accounts.amount_of({"source": {"assetAmount": {"amount": "9.00", "code": "EUR"}}}) == "9.00 EUR"
    assert accounts.amount_of({}) == "—"
