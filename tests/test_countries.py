"""The display-only ISO table (`app/web/countries.py`).

A wrong code→name pair on a money console is worse than no name at all, so the
table gets a real sanity pass here: size, no duplicate names, and a spot-check
of codes that are famously easy to swap (SVN/SVK, NER/NGA, AUT/AUS, …). Nothing
in here asserts what Conduit *supports* — discovery owns that, and the table is
deliberately allowed to be a superset of any one country's `allowedValues`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.web.countries import (
    COUNTRIES,
    country_name,
    enum_label,
    enum_option,
    humanize_enum,
    option_label,
    resolve_country,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_the_table_covers_iso_3166_1_and_names_nothing_twice():
    assert len(COUNTRIES) >= 245
    assert all(len(code) == 3 and code.isupper() for code in COUNTRIES)
    assert len(set(COUNTRIES.values())) == len(COUNTRIES)


@pytest.mark.parametrize(
    "code,name",
    [
        ("CAN", "Canada"),
        ("BGR", "Bulgaria"),
        ("KOR", "South Korea"),
        ("PRK", "North Korea"),
        ("USA", "United States"),
        ("GBR", "United Kingdom"),
        ("DEU", "Germany"),
        ("CHE", "Switzerland"),
        ("AUT", "Austria"),
        ("AUS", "Australia"),
        ("SVN", "Slovenia"),
        ("SVK", "Slovakia"),
        ("NER", "Niger"),
        ("NGA", "Nigeria"),
        ("ZAF", "South Africa"),
        ("ARE", "United Arab Emirates"),
    ],
)
def test_well_known_codes_are_paired_correctly(code, name):
    assert country_name(code) == name


def test_lookup_is_case_insensitive_and_misses_quietly():
    assert country_name("can") == "Canada"
    assert country_name(" Can ") == "Canada"
    assert country_name("XXX") is None
    assert country_name("") is None
    assert country_name(None) is None
    assert country_name(123) is None  # a non-string value from a payload


def test_every_country_conduit_offers_has_a_name():
    """Not a contract with Conduit — a coverage check on the table. Discovery's
    248-value `allowedValues` is the widest country list the console renders."""
    requirements = json.loads((FIXTURES / "onboarding_requirements_BGR.json").read_text())
    allowed = next(
        f["allowedValues"] for f in requirements["fields"] if f["pointer"] == "/registeredAddress/country"
    )
    assert [code for code in allowed if code not in COUNTRIES] == []


def test_humanize_only_touches_machine_constants():
    assert humanize_enum("INTERCOMPANY_TRANSFER") == "Intercompany transfer"
    assert humanize_enum("LEGAL_REPRESENTATIVE") == "Legal representative"
    # No underscore, lower case, mixed case, or a human sentence: left alone.
    assert humanize_enum("SWIFT") is None
    assert humanize_enum("other_industry") is None
    assert humanize_enum("C-Corporation") is None
    assert humanize_enum("Sole Proprietorship") is None
    assert humanize_enum(None) is None


def test_option_label_defers_to_discovery_and_never_replaces_the_value():
    # A label discovery actually shipped wins outright.
    assert option_label("USA", "United States of America") == "United States of America"
    # No label (forms._options falls back to the value) → the value plus a name.
    assert option_label("CAN", "CAN") == "CAN — Canada"
    assert option_label("NATIONAL_ID", "NATIONAL_ID") == "NATIONAL_ID — National id"
    # Nothing recognised → unchanged, never guessed at.
    assert option_label("ZZZ", "ZZZ") == "ZZZ"


def test_enum_label_says_the_wire_constant_in_words():
    assert enum_label("payment_for_goods_or_services") == "Payment for goods or services"
    assert enum_label("deposit_return") == "Deposit return"
    assert enum_label("business") == "Business"
    assert enum_label("") == ""


def test_enum_option_keeps_the_raw_value_unless_it_only_repeats_the_label():
    assert enum_option("deposit_return") == "Deposit return — deposit_return"
    # Capitalisation is the only difference, so the second half would say nothing.
    assert enum_option("application") == "Application"


def test_resolve_country_takes_exact_names_only_and_never_guesses():
    """The directive (2026-08-31): "United States" should reach Conduit
    as `USA`. The boundary matters more than the feature — a resolver that
    guessed would send money to a country nobody typed."""
    assert resolve_country("United States") == "USA"
    assert resolve_country("  bulgaria ") == "BGR"  # case- and space-insensitive
    # A code is not a name: unchanged, and still what the route uppercases.
    assert resolve_country("USA") == "USA"
    assert resolve_country("bg") == "bg"
    # Partial, misspelt, or unknown: verbatim, so Conduit's own refusal is what
    # the operator reads. No fuzzy matching, ever.
    assert resolve_country("United") == "United"
    assert resolve_country("Untied States") == "Untied States"
    assert resolve_country("Narnia") == "Narnia"
    assert resolve_country(None) == ""


def test_every_country_name_resolves_back_to_its_own_code():
    """The reverse map is built from the forward table, so this is really a check
    that no two rows share a name — a duplicate would make one of them
    unreachable and silently resolve to the other's code."""
    for code, name in COUNTRIES.items():
        assert resolve_country(name) == code
