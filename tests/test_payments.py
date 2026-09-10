"""`app/payments/` — pure logic: DTO shaping, decimals, quotes, ledger rows.

No HTTP, no DB. The route tests in `test_web_{recipients,payouts,transactions}.py`
prove the pages; this file proves the shapes those pages send and render.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import forms, payments
from tests.payments_fixtures import (
    ACH_INDIVIDUAL,
    CHAPS_BUSINESS,
    CONVERSION,
    DEPOSIT,
    EUR_ACCOUNT,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    GBP_ACCOUNT,
    PAYOUT,
    QUOTE,
    REGISTERED,
    SEPA_BUSINESS,
    USD_ACCOUNT,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


# --- the whitelist DTOs ---------------------------------------------------------------------


@pytest.mark.parametrize("rail", payments.WHITELIST_RAILS)
def test_every_variant_parses_with_no_unknown_types(rail):
    model = payments.whitelist_model(rail)
    assert model.warnings == []
    assert model.dialect == "payout"
    names = {f.dotted for f in model.fields}
    assert {"relationship", "legalName", "label"} <= names
    # `rail` is the picker, not a field — nothing to mis-answer.
    assert "rail" not in names


def test_the_us_variant_matches_the_pinned_spec():
    spec = json.loads((ROOT / "contracts" / "openapi_production.json").read_text())
    required = set(spec["components"]["schemas"]["UsWhitelistRecipientDto"]["required"])
    model = payments.whitelist_model("us")
    ours = {f.dotted for f in model.fields if f.required} | {"rail", "evidenceDocumentIds"}
    assert required <= ours


def test_the_uk_domestic_variant_matches_the_pinned_spec():
    spec = json.loads((ROOT / "contracts" / "openapi_production.json").read_text())
    required = set(spec["components"]["schemas"]["UkDomesticWhitelistRecipientDto"]["required"])
    model = payments.whitelist_model("uk_domestic")
    ours = {f.dotted for f in model.fields if f.required} | {"rail", "evidenceDocumentIds"}
    assert required <= ours


def test_a_dash_separated_sort_code_is_accepted():
    """`UkDomesticWhitelistRecipientDto.sortCode` says separators are stripped
    server-side — the console must not refuse the formatted version before it
    ever reaches Conduit."""
    model = payments.whitelist_model("uk_domestic")
    values = forms.FormValues(root={"sortCode": "04-00-04"})
    errors = forms.validate(model, values)
    assert not errors.fields.get("f.sortCode")


def test_the_body_is_flat_with_the_rail_from_the_picker():
    model = payments.whitelist_model("sepa")
    values = forms.FormValues(
        document_ids=["doc_1"],
        root={"iban": "DE89370400440532013000", "relationship": "self", "legalName": "X Ltd"},
    )
    assert payments.whitelist_body("sepa", values, model) == {
        "rail": "sepa",
        "iban": "DE89370400440532013000",
        "relationship": "self",
        "legalName": "X Ltd",
        "evidenceDocumentIds": ["doc_1"],
    }


def test_evidence_is_required_on_every_registration_including_self():
    """**Verified live 2026-08-28**: an empty `evidenceDocumentIds` is refused —
    `400 VALIDATION_ERROR`, "Too small: expected array to have >=1 items" — on a
    `self` registration, which the DTO's prose implies would be exempt."""
    model = payments.whitelist_model("us")
    root = {
        "routingNumber": "021000021",
        "accountNumber": "1",
        "relationship": "self",
        "legalName": "X",
    }
    empty = forms.FormErrors()
    payments.whitelist_errors("us", forms.FormValues(root=root), empty)
    assert len(empty.documents) == 1

    attached = forms.FormErrors()
    payments.whitelist_errors(
        "us", forms.FormValues(root=root, document_ids=["doc_1"]), attached
    )
    assert attached.documents == []


def test_the_engine_validators_are_wired_to_the_right_fields():
    assert payments.whitelist_model("us").field_by_path(("routingNumber",)).validator == "aba"
    assert payments.whitelist_model("sepa").field_by_path(("iban",)).validator == "iban"
    assert payments.whitelist_model("swift").field_by_path(("iban",)).validator == "iban"


def test_swift_needs_one_of_iban_or_account_number():
    model = payments.whitelist_model("swift")
    bare = forms.FormValues(root={"bic": "CITIUS33XXX", "legalName": "X", "relationship": "self"})
    errors = forms.FormErrors()
    payments.whitelist_errors("swift", bare, errors)
    assert [m.detail for m in errors.form] == [
        "A SWIFT recipient needs an IBAN or an account number."
    ]

    with_iban = forms.FormValues(root={**bare.root, "iban": "DE89370400440532013000"})
    clean = forms.FormErrors()
    payments.whitelist_errors("swift", with_iban, clean)
    assert clean.form == []
    # …and the rule applies to `swift` only.
    other = forms.FormErrors()
    payments.whitelist_errors("us", bare, other)
    assert other.form == []


def test_only_registered_entries_are_offered_to_a_payout():
    entries = [
        REGISTERED,
        {**REGISTERED, "id": "b", "status": "pending_review"},
        {**REGISTERED, "id": "c", "status": "suspended"},
        {**REGISTERED, "id": "d", "status": "revoked"},
        {**REGISTERED, "id": "e", "status": "rejected"},
        {**REGISTERED, "id": "f", "status": "something_new"},
    ]
    assert [e["id"] for e in payments.registered_only(entries)] == [REGISTERED["id"]]


# --- the group-entity shortcut -----------------------------------------------------------------


def test_the_shortcut_reads_us_coordinates_off_a_us_domestic_block():
    prefill = payments.prefill_from_account(USD_ACCOUNT)
    assert prefill["rail"] == "us"
    assert prefill["values"] == {
        "legalName": "ZZZTEST Console E2E EOOD",
        "accountNumber": "9876543210",
        "routingNumber": "101019644",
    }


def test_the_shortcut_reads_sepa_coordinates_off_a_sepa_block():
    prefill = payments.prefill_from_account(EUR_ACCOUNT)
    assert prefill["rail"] == "sepa"
    assert prefill["values"]["iban"] == "DE89370400440532013000"


def test_the_shortcut_reads_uk_domestic_coordinates_off_a_uk_domestic_block():
    prefill = payments.prefill_from_account(GBP_ACCOUNT)
    assert prefill["rail"] == "uk_domestic"
    assert prefill["values"]["sortCode"] == "040004"
    assert prefill["values"]["accountNumber"] == "12345678"


def test_the_shortcut_reads_swift_coordinates_when_that_is_all_there_is():
    swift_only = {
        **USD_ACCOUNT,
        "depositInstructions": [USD_ACCOUNT["depositInstructions"][1]],
    }
    prefill = payments.prefill_from_account(swift_only)
    assert prefill["rail"] == "swift"
    assert prefill["values"]["bic"] == "LEADUS44XXX"
    assert prefill["values"]["accountNumber"] == "9876543210"


def test_the_shortcut_invents_nothing_when_there_is_nothing_to_read():
    assert payments.prefill_from_account(None) == {}
    assert payments.prefill_from_account({"depositInstructions": []}) == {}
    assert payments.prefill_from_account({"depositInstructions": [{"type": "carrier_pigeon"}]}) == {}
    # A block of a known type with no coordinates is skipped, not half-filled.
    assert payments.prefill_from_account(
        {"depositInstructions": [{"type": "us_domestic", "beneficiaryName": "X"}]}
    ) == {}


def test_the_prefilled_routing_numbers_pass_the_checksum_the_form_enforces():
    """The shortcut's whole point is a form the operator can submit — a prefill
    that fails our own ABA check would be worse than no prefill."""
    prefill = payments.prefill_from_account(USD_ACCOUNT)
    assert forms.aba_valid(prefill["values"]["routingNumber"])
    live = json.loads((FIXTURES / "virtual_account_live_usd.json").read_text())
    assert forms.aba_valid(payments.prefill_from_account(live)["values"]["routingNumber"])


# --- the payout form model ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot", [FEDWIRE_BUSINESS, FEDWIRE_INTERCOMPANY, SEPA_BUSINESS, CHAPS_BUSINESS]
)
def test_the_route_owns_the_virtual_account_not_the_engine(snapshot):
    model = payments.payout_model(snapshot)
    assert ("virtualAccountId",) not in {f.path for f in model.fields}
    # …and discovery declared it, so this is a removal rather than an absence.
    assert any(f.get("name") == "virtualAccountId" for f in snapshot["fields"])


def test_the_spurious_ach_subtree_is_dropped_off_an_ach_rail():
    """The app's **only** discovery override (conduit-issues/06).

    Both fedwire fixtures declare `destination.ach.authorizationType` required;
    a live fedwire payout carrying it was accepted and the whole subtree
    discarded (`payout_live_withdrawal.json` has no `destination.ach`). So the
    field is hidden off-rail — and kept, untouched, on a real ACH route.
    """
    fedwire = payments.payout_model(FEDWIRE_BUSINESS)
    assert not [f for f in fedwire.fields if f.path[:2] == payments.ACH_SUBTREE]
    # …and discovery really did declare it, so this is a removal, not an absence.
    assert any(
        f["name"] == "destination.ach.authorizationType" for f in FEDWIRE_BUSINESS["fields"]
    )
    # Every other field survives: the override is one subtree, not a rewrite.
    assert len(fedwire.fields) == len(FEDWIRE_BUSINESS["fields"]) - 2  # ach + virtualAccountId


def test_an_ach_route_keeps_its_own_ach_fields():
    ach = payments.payout_model(ACH_INDIVIDUAL)
    assert ach.rail == "ach"
    assert [f.dotted for f in ach.fields if f.path[:2] == payments.ACH_SUBTREE] == [
        "destination.ach.authorizationType"
    ]


def test_a_hidden_ach_subtree_never_reaches_the_body():
    """The override runs in `payout_model`, so the field is gone from the render
    *and* from `assemble` — a browser that posts one is ignored, because the
    engine drops names the model does not know."""
    model = payments.payout_model(FEDWIRE_BUSINESS)
    values = forms.parse_submission(
        model,
        [
            ("f.destination.rail", "fedwire"),
            ("f.destination.ach.authorizationType", "corporate_agreement"),
        ],
    )
    body = payments.payout_body(
        model,
        values,
        customer_id="c",
        virtual_account_id="v",
        asset="USD",
        amount_text="1",
        purpose="payroll",
    )
    assert "ach" not in body["destination"]
    assert forms.validate(model, values).for_field(("destination", "ach", "authorizationType")) == []


def test_the_metadata_polarity_is_read_off_the_response_and_nowhere_else():
    goods = payments.payout_model(FEDWIRE_BUSINESS)
    inter = payments.payout_model(FEDWIRE_INTERCOMPANY)
    assert goods.documentation["required"] is True and goods.whitelist["required"] is False
    assert inter.whitelist["required"] is True and inter.documentation["required"] is False
    # Same purpose vocabulary, opposite gates: nothing in the app maps one to the
    # other.
    assert "acceptedDocumentTypes" in goods.documentation


def test_the_whitelist_gate_removes_exactly_the_identity_fields():
    model = payments.payout_model(FEDWIRE_INTERCOMPANY)
    gated = payments.recipient_model(model)
    removed = {f.dotted for f in model.fields} - {f.dotted for f in gated.fields}
    assert removed == {
        "destination.recipient.accountNumber",
        "destination.recipient.routingNumber",
        "destination.recipient.legalName",
    }
    # The address blocks a whitelist entry does not carry are still asked for.
    assert any(f.dotted.endswith("bankAddress.city") for f in gated.fields)


def test_the_picked_entry_overwrites_whatever_the_browser_sent():
    model = payments.payout_model(FEDWIRE_INTERCOMPANY)
    values = forms.FormValues(
        root={"destination": {"recipient": {"accountNumber": "999", "legalName": "Wrong Ltd"}}}
    )
    payments.apply_recipient(model, values, REGISTERED)
    recipient = values.root["destination"]["recipient"]
    assert recipient["accountNumber"] == REGISTERED["accountNumber"]
    assert recipient["routingNumber"] == REGISTERED["routingNumber"]
    assert recipient["legalName"] == REGISTERED["legalName"]


def test_a_sepa_entry_maps_onto_a_sepa_route_by_field_name_not_by_rail_table():
    model = payments.payout_model(SEPA_BUSINESS)
    entry = {**REGISTERED, "rail": "sepa", "iban": "DE89370400440532013000", "routingNumber": None}
    values = forms.FormValues()
    payments.apply_recipient(model, values, entry)
    assert values.root["destination"]["recipient"]["iban"] == "DE89370400440532013000"
    # A null coordinate is not written as an empty string.
    assert "routingNumber" not in values.root["destination"]["recipient"]


def test_remittance_fields_are_grouped_together():
    """Both halves of `destination.remittance.*` belong in one section — matching
    on the leaf name put `reference` under Remittance and `description` in the
    catch-all."""
    model = payments.payout_model(FEDWIRE_BUSINESS)
    groups = {
        f.dotted: forms.payout_group(f) for f in model.fields if "remittance" in f.path
    }
    assert set(groups.values()) == {"remittance"}
    assert len(groups) == 2


# --- money ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["1000.00", "0.01", "1", " 250.5 "])
def test_a_good_amount_comes_back_as_the_operators_own_digits(text):
    """Scale included: `1000.00` stays `1000.00`. Trailing zeros on a money field
    are information, so nothing here normalises them away."""
    assert payments.amount(text) == text.strip()


@pytest.mark.parametrize("text", ["", "   ", "abc", "-1", "0", "0.00", "NaN", "Infinity", "-Inf"])
def test_a_bad_amount_is_refused(text):
    assert payments.amount(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1_000", None),          # Decimal reads this as 1000 and sent "1_000"
        ("1e3", None),            # …as 1000, and sent "1e3"
        ("+5", None),             # …as 5, and sent "+5"
        ("\uff11\uff10\uff10\uff10", None),  # fullwidth digits: Decimal reads 1000, sent verbatim
        ("\u0661\u0662\u0663", None),         # Arabic-Indic: same trick, different script
        ("007.50", "7.50"),       # leading zeros are the one allowed divergence
        ("00.50", "0.50"),
        ("1.50", "1.50"),         # …and trailing zeros are never touched
        ("1000", "1000"),
        ("1E+3", None),
        (".5", None),             # a digit before the point, or nothing
        ("1.", None),
        ("1 000", None),
        ("1,000", None),
    ],
)
def test_only_a_plain_decimal_is_accepted_and_it_is_returned_canonical(text, expected):
    """`amount()` used to return its **input**: every spelling
    `Decimal` tolerates — underscores, exponents, a leading `+`, digits from any
    Unicode script — validated as a number and then went to Conduit verbatim, so
    the amount the operator was shown and the amount on the wire were not
    necessarily the same string. What comes back now is the canonical form, and
    it is the value every caller transmits."""
    assert payments.amount(text) == expected


def test_the_canonical_amount_is_never_an_exponent():
    """`str(Decimal("1E+3"))` is `"1E+3"`; `format(..., "f")` is `"1000"`. The
    guard above refuses exponent input outright, so this pins the formatter
    rather than the parser — a future edit that widens the regex must not
    reintroduce an exponent on the wire."""
    from decimal import Decimal

    assert format(Decimal("1E+3"), "f") == "1000"
    assert all("e" not in (payments.amount(t) or "").lower() for t in ("1000", "0.01", "007.50"))


def test_no_float_ever_touches_a_payout_body():
    """`float("0.1")` is not 0.1. A grep, because the failure mode is a single
    careless `float(...)` in a future edit, not a bug in today's code."""
    source = (ROOT / "app" / "payments" / "__init__.py").read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "float"
    ]
    assert calls == []
    for module in ("payouts.py", "recipients.py", "transactions.py"):
        text = (ROOT / "app" / "web" / module).read_text()
        assert "float(" not in text, module


def test_the_payout_body_is_the_fiat_payout_dto():
    model = payments.payout_model(FEDWIRE_BUSINESS)
    values = forms.FormValues(
        root={
            "destination": {
                "type": "fiat",
                "rail": "fedwire",
                "recipient": {"accountNumber": "1", "legalName": "X"},
                "remittance": {"reference": "INV-1"},
            }
        }
    )
    body = payments.payout_body(
        model,
        values,
        customer_id="cus_1",
        virtual_account_id="vac_1",
        asset="USD",
        amount_text="1000.00",
        purpose="payroll",
        document_ids=["doc_1"],
    )
    assert body == {
        "customerId": "cus_1",
        "virtualAccountId": "vac_1",
        "assetAmount": {"code": "USD", "amount": "1000.00"},
        "purpose": "payroll",
        "destination": values.root["destination"],
        "documents": ["doc_1"],
    }
    assert isinstance(body["assetAmount"]["amount"], str)


def test_the_document_list_is_capped_at_the_dtos_maximum():
    model = payments.payout_model(FEDWIRE_BUSINESS)
    body = payments.payout_body(
        model,
        forms.FormValues(),
        customer_id="c",
        virtual_account_id="v",
        asset="USD",
        amount_text="1",
        purpose="other",
        document_ids=[f"doc_{i}" for i in range(25)],
    )
    assert len(body["documents"]) == payments.MAX_DOCUMENTS == 10


# --- gates -------------------------------------------------------------------------------------------


def test_the_documentation_gap_is_read_off_the_response():
    goods = payments.payout_model(FEDWIRE_BUSINESS)
    inter = payments.payout_model(FEDWIRE_INTERCOMPANY)
    empty, attached = forms.FormValues(), forms.FormValues(document_ids=["doc_1"])
    assert payments.documentation_gap(goods, empty) is True
    assert payments.documentation_gap(goods, attached) is False
    # The route that does not ask for one never demands one.
    assert payments.documentation_gap(inter, empty) is False


def test_blocked_jurisdictions_cover_the_route_and_the_form():
    model = payments.payout_model(FEDWIRE_BUSINESS)
    assert "RUS" in model.blocked_jurisdictions
    assert payments.blocked_country(model, forms.FormValues(), "RUS") == "RUS"
    assert payments.blocked_country(model, forms.FormValues(), "rus") == "rus"
    assert payments.blocked_country(model, forms.FormValues(), "USA") is None
    inside = forms.FormValues(
        root={"destination": {"recipient": {"postalAddress": {"country": "IRN"}}}}
    )
    assert payments.blocked_country(model, inside, "USA") == "IRN"


def test_a_response_with_no_blocked_list_blocks_nothing():
    model = payments.payout_model({**FEDWIRE_BUSINESS, "blockedJurisdictions": []})
    assert payments.blocked_country(model, forms.FormValues(), "RUS") is None


# --- quotes ---------------------------------------------------------------------------------------------


def test_the_quote_request_is_the_withdrawal_mode():
    """Same asset both sides + a destination country = "price a withdrawal",
    per the spec's own discrimination rule."""
    assert payments.quote_request(
        source="USD", destination="USD", destination_country="USA", amount_text="1000.00"
    ) == {
        "source": {"code": "USD"},
        "destination": {"code": "USD"},
        "destinationCountry": "USA",
        "lockSide": "source",
        "amount": "1000.00",
    }


def test_the_quote_view_keeps_amounts_as_strings():
    view = payments.quote_view(QUOTE)
    assert [o["rail"] for o in view["options"]] == ["fedwire", "rtp"]
    assert view["options"][0]["total_debit"] == "1020.00 USD"
    assert view["options"][0]["fees"][0] == {
        "charge": "payout",
        "owner": "conduit",
        "amount": "20.00 USD",
    }
    assert view["stale"] is False


def test_a_same_asset_withdrawal_quote_has_no_rate_and_says_so():
    """**Captured live 2026-08-28**: `rate` is `null` on a USD→USD withdrawal
    option — there is no exchange rate to quote. The panel must render the fees
    and the debit, not blow up on the missing block."""
    live = json.loads((FIXTURES / "quote_live_usd_withdrawal.json").read_text())
    view = payments.quote_view(live)
    assert view is not None and view["options"]
    for option in view["options"]:
        assert option["end_user_rate"] == "" and option["spread_bps"] == ""
        assert option["total_debit"].endswith("USD")
        assert option["fees"] and option["fees"][0]["charge"] == "payout"
    assert {o["rail"] for o in view["options"]} >= {"fedwire", "ach"}


def test_a_quote_with_no_options_renders_nothing_rather_than_an_empty_panel():
    assert payments.quote_view({"id": "q", "options": []}) is None
    assert payments.quote_view(None) is None


@pytest.mark.parametrize(
    "expires_at,stale",
    [
        ("2099-01-01T00:00:00.000Z", False),
        ("2020-01-01T00:00:00.000Z", True),
        ("", True),
        (None, True),
        ("not a timestamp", True),
        # Naive timestamps are read as UTC rather than crashing on the compare.
        ("2099-01-01T00:00:00", False),
    ],
)
def test_expiry_reads_unknowable_as_expired(expires_at, stale):
    assert payments.expired(expires_at) is stale


def test_expiry_is_evaluated_against_the_moment_given():
    moment = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    assert payments.expired("2026-08-27T12:00:01Z", moment) is False
    assert payments.expired("2026-08-27T11:59:59Z", moment) is True
    # Exactly at the boundary is expired: a rate that "expires at" 12:00 is not
    # still good at 12:00.
    assert payments.expired("2026-08-27T12:00:00Z", moment) is True


# --- the ledger ------------------------------------------------------------------------------------------


def test_stage_is_labelled_when_known_and_shown_raw_when_not():
    assert payments.stage_label(PAYOUT) == "under review"
    assert payments.stage_label({"stage": "moon_phase"}) == "stage: moon_phase"
    assert payments.stage_label({}) == ""
    assert payments.stage_label(None) == ""


def test_only_a_pending_withdrawal_is_cancellable():
    assert payments.can_cancel(PAYOUT) is True
    assert payments.can_cancel({**PAYOUT, "status": "processing"}) is False
    assert payments.can_cancel({**PAYOUT, "status": "completed"}) is False
    # Never on an unknown status (plan v2 §7), and never on a deposit.
    assert payments.can_cancel({**PAYOUT, "status": "levitating"}) is False
    assert payments.can_cancel(DEPOSIT) is False
    assert payments.can_cancel(None) is False


def test_fee_and_markup_rows_keep_their_decimal_strings():
    assert payments.fee_rows(PAYOUT) == [
        {"type": "fixed", "owner": "conduit", "amount": "12.50 USD"}
    ]
    assert payments.markup_row(PAYOUT) == {"bps": 25, "flat": "", "charged": "2.50 USD"}
    assert payments.markup_row(DEPOSIT) is None
    assert payments.fee_rows(None) == []


def test_the_generic_fallback_labels_every_scalar_it_finds():
    rows = {row["label"]: row["value"] for row in payments.generic_rows(CONVERSION)}
    assert rows["Conversion rate"] == "0.9123"
    assert rows["Source · Asset amount · Code"] == "USD"
    # The identity fields already in the page header are not repeated.
    assert "Id" not in rows and "Type" not in rows


def test_the_live_payout_renders_through_the_same_functions_the_page_uses():
    """The withdrawal captured from staging, through the detail page's own
    helpers — including the two things it echoed back that we never sent
    (`recipient.rail`) and the one we did that it dropped (`destination.ach`)."""
    live = json.loads((FIXTURES / "payout_live_withdrawal.json").read_text())
    assert payments.amount_of(live) == "11.00 USD"
    assert payments.stage_label(live) == "settling"
    assert payments.can_cancel(live) is True
    assert payments.fee_rows(live) == [
        {"type": "fixed", "owner": "conduit", "amount": "0.25 USD"}
    ]
    assert payments.markup_row(live) is None
    rows = {row["label"]: row["value"] for row in payments.side_rows(live["destination"])}
    assert rows["Recipient · Routing number"] == "021000021"
    # The coordinate this fixture really carries, and the exact digits
    # that used to render on the detail page.
    assert rows["Recipient · Account number"] == "••••6789"
    assert live["destination"]["recipient"]["accountNumber"] == "000123456789"
    assert not any("000123456789" in value for value in rows.values())
    # Conduit normalises what discovery spells in caps: we sent `CHECKING` /
    # `BUSINESS`, it stored `checking` / `business`.
    assert rows["Recipient · Account type"] == "checking"
    assert rows["Recipient · Type"] == "business"
    # …and it fills in a `recipient.rail` discovery never declared.
    assert rows["Recipient · Rail"] == "us"
    # The `destination.ach` subtree the fedwire fixture declares required was
    # accepted and dropped — it is not on the resource.
    assert "ach" not in live["destination"]


def test_the_generic_walk_masks_a_coordinate_wherever_the_key_turns_up():
    """The walk is the one place both leaking pages
    go through, so the rule is pinned here by key rather than by page — at every
    depth, in either dialect, and never on a bank identifier."""
    rows = {
        row["label"]: row["value"]
        for row in payments.generic_rows(
            {
                "accountNumber": "000123456789",  # a bare top-level coordinate
                "autoPayout": {"recipient": {"iban": "DE89370400440532013000"}},
                "deposit_instructions": {"account_number": "9876543210"},
                "routingNumber": "021000021",
                "bic": "CITIUS33XXX",
                "bankName": "ZZZTEST Bank",
                "fedwireImad": "20260827MMQFMP0100001",
            }
        )
    }
    assert rows["Account number"] == "••••6789"
    assert rows["Auto payout · Recipient · Iban"] == "••••3000"
    assert rows["Deposit instructions · Account number"] == "••••3210"
    # Public routing data, whole — and a long digit string that is neither a
    # coordinate nor PII is untouched, which is why this is a key set and not a
    # value-shape guess.
    assert rows["Routing number"] == "021000021"
    assert rows["Bic"] == "CITIUS33XXX"
    assert rows["Bank name"] == "ZZZTEST Bank"
    assert rows["Fedwire imad"] == "20260827MMQFMP0100001"


def test_the_generic_walk_masks_a_coordinate_that_arrives_inside_a_container():
    """This walk is the fallback for a DTO nobody has seen, so the shape under a key
    is unknown by construction. Masking was decided on the scalar's own leaf, so a
    masked key holding a list or an object was recursed straight past — the leaf
    became `0` or `value`, matched nothing, and the coordinate printed whole on
    the withdrawal detail page and the orders raw panel. `_WITHHELD_IN_GENERIC` is
    tested before the recursion and collapsed the same shapes correctly, so the
    two rules disagreed about exactly the keys that carry account numbers.
    """
    rows = {
        row["label"]: row["value"]
        for row in payments.generic_rows(
            {
                "recipient": {
                    "accountNumber": ["12345678901234"],
                    "iban": {"value": "DE89370400440532013000"},
                    "routingNumber": ["021000021"],
                }
            }
        )
    }
    assert rows["Recipient · Account number · 0"] == "••••1234"
    assert rows["Recipient · Iban · Value"] == "••••3000"
    # Still keyed, not shape-guessed: public routing data inside a container is
    # as untouched as it is outside one.
    assert rows["Recipient · Routing number · 0"] == "021000021"
    assert "12345678901234" not in str(rows)
    assert "DE89370400440532013000" not in str(rows)


def test_the_generic_walk_withholds_a_persons_data_without_claiming_it_was_absent():
    """The value goes; the row stays, so the page never reports its own
    suppression as an absence at Conduit. A key Conduit did not send gets no row
    at all — a `withheld` over nothing would be an invented fact."""
    rows = {
        row["label"]: row["value"]
        for row in payments.generic_rows(
            {
                "recipient": {
                    "legalName": "ZZZTEST Globex Supplies LLC",
                    "dateOfBirth": "1985-04-12",
                    "phone": "+49 30 5550199",
                    "postalAddress": {"addressLine1": "ZZZTEST Strasse 1", "city": "Berlin"},
                    "bankAddress": {"addressLine1": "123 Sandbox Way", "city": "Testville"},
                },
                "sender": {"dateOfBirth": None, "phone": "", "postalAddress": {}},
            }
        )
    }
    assert rows["Recipient · Date of birth"] == "withheld"
    assert rows["Recipient · Phone"] == "withheld"
    assert rows["Recipient · Postal address"] == "withheld"
    assert not any("Postal address · " in label for label in rows)
    # The name is how an operator tells two payees apart, and the bank's own
    # address is public — both are printed exactly as before.
    assert rows["Recipient · Legal name"] == "ZZZTEST Globex Supplies LLC"
    assert rows["Recipient · Bank address · Address line1"] == "123 Sandbox Way"
    # Nothing arrived under any of the sender's three, so the sender says
    # nothing about them.
    assert not any(label.startswith("Sender") for label in rows)


def test_side_rows_walk_whatever_branch_arrived():
    rows = {row["label"]: row["value"] for row in payments.side_rows(DEPOSIT["source"])}
    assert rows["Sender · Name"] == "ZZZTEST Payer Inc"
    assert rows["Ach trace number"] == "091000010000001"
    # The amount is the page's headline, not a row in the side table.
    assert not any("Asset amount" in label for label in rows)
    assert payments.side_rows(None) == []


def test_only_a_real_currency_change_is_called_a_conversion():
    """`converted` compared the two *formatted strings*, so a payout that debits
    1000.00 USD and delivers 987.50 USD — one currency, a fee, nothing converted
    — rendered a "Converted" row on its detail page. The comparison is the pair
    of asset codes."""
    assert payments.converted(CONVERSION) == "1000.00 USD → 912.30 EUR"
    # Same asset, different numbers because of a fee: not a conversion.
    assert payments.converted(PAYOUT) == ""
    # A side that named no asset is an unknown currency, not a second one.
    one_sided = {
        "source": {"assetAmount": {"amount": "10.00"}},
        "destination": {"assetAmount": {"code": "EUR", "amount": "9.00"}},
    }
    assert payments.converted(one_sided) == ""
    assert payments.converted(None) == "" and payments.converted({}) == ""
