"""Form engine (FORM_ENGINE_SPEC §9) — pure logic, no HTTP, no DB.

The condition parity gate lives here too: `condition_vectors.json` is executed
by the Python evaluator and, in a node subprocess, by `static/conditions.js`.
Any disagreement fails this suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app import forms
from app.conduit.client import FieldError
from app.forms import (
    ABSENT,
    Condition,
    Field,
    FormValues,
    PersonValues,
    SchemaVersionMismatch,
    aba_valid,
    active_fields,
    assemble,
    coerce,
    evaluate_field,
    field_name,
    iban_valid,
    map_validation_errors,
    parse,
    parse_name,
    parse_submission,
    render_model,
    validate,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
VECTORS = ROOT / "tests" / "condition_vectors.json"
CONDITIONS_JS = ROOT / "static" / "conditions.js"
NODE = shutil.which("node") or "/opt/homebrew/bin/node"

ONBOARDING = sorted(p.name for p in FIXTURES.glob("onboarding_requirements_*.json"))
FEATURE = ["feature_requirements_virtual_account_usd.json"]
PAYOUT = sorted(p.name for p in FIXTURES.glob("payout_requirements_*.json"))
REQUIREMENTS = ONBOARDING + FEATURE
ALL_FIXTURES = REQUIREMENTS + PAYOUT

EXPECTED_FIELD_COUNT = {
    "onboarding_requirements_BGR.json": 61,
    "onboarding_requirements_BRA.json": 61,
    "onboarding_requirements_ITA.json": 61,
    "onboarding_requirements_USA.json": 61,
    "feature_requirements_virtual_account_usd.json": 5,
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def forms_error(pointer: str, detail: str, allowed=None):
    """The shape `map_validation_errors`/`learn` consume (spec §7)."""
    return SimpleNamespace(
        pointer=pointer, detail=detail, category=None, allowed_values=allowed or []
    )


def model(name: str):
    return parse(load(name))


# --- §9.1 parsing -----------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses_with_no_unknown_type_warnings(name):
    m = model(name)
    assert m.warnings == []
    assert m.fields, "no fields parsed"
    assert all(f.known_type for f in m.fields)


@pytest.mark.parametrize("name", REQUIREMENTS)
def test_requirements_field_counts_and_dialect(name):
    m = model(name)
    assert m.dialect == "requirements"
    assert len(m.fields) == EXPECTED_FIELD_COUNT[name]


@pytest.mark.parametrize("name", PAYOUT)
def test_payout_field_counts_and_route_metadata(name):
    m = model(name)
    assert m.dialect == "payout"
    assert 24 <= len(m.fields) <= 28
    # Surfaced verbatim, never interpreted (spec §1).
    raw = load(name)
    assert m.whitelist == raw["whitelist"]
    assert m.documentation == raw["documentation"]
    assert m.blocked_jurisdictions == raw["blockedJurisdictions"]


def test_payout_gating_both_polarities_come_from_discovery():
    goods = model("payout_requirements_fedwire_business.json")
    intercompany = model("payout_requirements_fedwire_intercompany.json")
    assert goods.documentation["required"] is True
    assert goods.whitelist["required"] is False
    assert intercompany.whitelist["required"] is True
    assert intercompany.documentation["required"] is False


@pytest.mark.parametrize("name", REQUIREMENTS)
def test_schema_version_drift_canary(name):
    """Recapture + review when this fails (spec §9.7)."""
    assert load(name)["schemaVersion"] == "3"
    assert model(name).schema_version == "3"


def test_schema_version_mismatch_is_a_hard_error():
    payload = load("onboarding_requirements_BGR.json") | {"schemaVersion": "4"}
    with pytest.raises(SchemaVersionMismatch):
        parse(payload)


def test_unknown_type_falls_back_to_string_with_a_warning():
    payload = {
        "schemaVersion": "3",
        "fields": [{"pointer": "/a/b", "label": "A", "type": "quaternion", "required": False}],
    }
    m = parse(payload)
    assert len(m.warnings) == 1 and "quaternion" in m.warnings[0]
    assert m.fields[0].effective_type == "string" and m.fields[0].widget == "text"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_html_name_path_round_trip(name):
    m = model(name)
    fields = list(m.fields) + [f for p in m.persons for f in p.fields]
    assert fields
    for f in fields:
        assert parse_name(field_name(f.path)) == (None, f.path)
        assert parse_name(field_name(f.path, 3)) == (3, f.path)
    assert parse_name("documentIds") is None


def test_dialect_b_dotted_names_and_extras():
    m = model("payout_requirements_fedwire_business.json")
    routing = m.field_by_path(("destination", "recipient", "routingNumber"))
    assert routing.validator == "aba"
    assert routing.constraints.pattern == "^[0-9]{9}$"
    assert routing.constraints.min_length == 9
    postal = m.field_by_path(("destination", "recipient", "postalAddress", "postalCode"))
    assert postal.required is False
    assert postal.required_when.operator == "not_in"
    assert postal.required_when.path == ("destination", "recipient", "postalAddress", "country")
    assert postal.conditions == []  # requiredWhen never hides (spec §4)
    accounts = m.field_by_path(("destination", "recipient", "accountType"))
    assert accounts.allowed_values == ["CHECKING", "SAVINGS"]


def test_persons_and_documents_parsed():
    m = model("onboarding_requirements_BGR.json")
    assert [p.role for p in m.persons] == ["any", "BENEFICIAL_OWNER"]
    assert all(p.min_count == 1 for p in m.persons)
    assert all(p.ownership_threshold is None for p in m.persons)  # render conditionally
    # 15 declared + the 6-field `address` block the submit endpoint requires but
    # discovery never declares (forms._person_address, verified live 2026-08-28).
    assert len(m.persons[0].fields) == 21
    assert [f.path for f in m.persons[0].fields if f.path[0] == "address"] == [
        ("address", "country"),
        ("address", "addressLine1"),
        ("address", "addressLine2"),
        ("address", "city"),
        ("address", "state"),
        ("address", "postalCode"),
    ]
    # Copied from the payload's own registeredAddress block — same labels, same
    # 248-value country list, same optionality; nothing invented.
    person_country = next(f for f in m.persons[0].fields if f.path == ("address", "country"))
    root_country = m.field_by_path(("registeredAddress", "country"))
    assert person_country.allowed_values == root_country.allowed_values
    assert person_country.label == root_country.label and person_country.required
    assert [d.canonical_type for d in m.persons[1].documents] == ["IDENTITY_VERIFICATION"]
    assert m.min_documents == 1
    assert [d.canonical_type for d in m.documents] == [
        "CERTIFICATE_OF_GOOD_STANDING",
        "BUSINESS_REGISTRATION",
        "OPERATING_LICENSE",
    ]
    assert m.documents[0].source == "Registry Agency (Агенция по вписванията)"


# --- §9.2 coercion ----------------------------------------------------------------


def f(type_, **kw) -> Field:
    return Field(path=("x",), label="X", type=type_, **kw)


@pytest.mark.parametrize(
    "type_,raw,expected",
    [
        ("string", ["  hi  "], "hi"),
        ("string", ["   "], ABSENT),
        ("string", [], ABSENT),
        ("number", ["12.5"], 12.5),
        ("number", ["0"], 0.0),
        ("integer", ["7"], 7),
        ("date", ["2020-01-02"], "2020-01-02"),
        ("email", [" a@b.co "], "a@b.co"),
        ("url", ["https://x.co"], "https://x.co"),
        ("phone", [" +359 2 1 "], "+359 2 1"),
        ("country", ["BGR"], "BGR"),
        ("enum", ["C-Corporation"], "C-Corporation"),  # verbatim key, never normalized
        ("stringArray", ["a\n\nb\n"], ["a", "b"]),
        ("stringArray", ["\n \n"], ABSENT),
        ("enumArray", ["Payroll", "Other"], ["Payroll", "Other"]),
        ("enumArray", [], ABSENT),
        ("boolean", ["true"], True),
        ("boolean", ["false"], False),
        ("boolean", ["on"], True),
        ("boolean", [], ABSENT),  # unchecked optional -> absent
        ("quaternion", [" fallback "], "fallback"),  # unknown -> string
    ],
)
def test_coercion_table(type_, raw, expected):
    assert coerce(f(type_), raw) == expected


def test_number_and_integer_junk_is_reported_not_raised():
    assert coerce(f("number"), ["abc"]).detail == "must be a number"
    assert coerce(f("integer"), ["1.5"]).detail == "must be a whole number"


def test_number_coercion_never_widens_an_integral_value_to_float():
    """An integral `number` submission is exact `int`, never
    a lossy `float`."""
    value = coerce(f("number"), ["12"])
    assert value == 12 and isinstance(value, int)


def test_number_coercion_accepts_a_round_tripping_fraction():
    value = coerce(f("number"), ["0.1"])
    assert value == 0.1 and isinstance(value, float)


def test_number_coercion_refuses_precision_float_cannot_hold():
    """A discovery-declared `number` is never money in this app (verified
    against contracts/openapi_production.json — every `amount`-shaped field
    is `type: string`), but float is still lossy past ~15-17 significant
    digits; refuse rather than silently truncate."""
    result = coerce(f("number"), ["0.1000000000000000000001"])
    assert isinstance(result, forms.Invalid)
    assert "precision" in result.detail


def test_boolean_widget_rule():
    assert f("boolean", required=True).widget == "radio"
    assert f("boolean", required=True, must_equal=True).widget == "checkbox"
    assert f("boolean", required=False).widget == "checkbox"


@pytest.mark.parametrize(
    "type_,widget",
    [
        ("string", "text"),
        ("number", "number"),
        ("integer", "number"),
        ("date", "date"),
        ("email", "email"),
        ("url", "url"),
        ("phone", "tel"),
        ("country", "select"),
        ("enum", "select"),
        ("stringArray", "textarea"),
        ("enumArray", "checkboxes"),
    ],
)
def test_widget_mapping_covers_all_twelve_types(type_, widget):
    assert f(type_).widget == widget


# --- §9.3 conditions + the CI parity gate ------------------------------------------


def vector_cases():
    return json.loads(VECTORS.read_text())["cases"]


def as_condition(raw: dict) -> Condition:
    return Condition(
        path=tuple(raw["path"].split(".")),
        operator=raw["operator"],
        value=raw.get("value"),
        values=raw.get("values"),
        scope=raw.get("scope", "root"),
    )


@pytest.mark.parametrize("case", vector_cases(), ids=lambda c: c["name"])
def test_condition_vectors_python(case):
    result = evaluate_field(
        [as_condition(c) for c in case.get("conditions", [])],
        as_condition(case["requiredWhen"]) if case.get("requiredWhen") else None,
        case.get("required", False),
        case.get("root"),
        case.get("person"),
    )
    assert result == case["expect"]


def test_condition_vectors_javascript_agree_with_python():
    """The parity gate: same vectors, `static/conditions.js`, under node."""
    program = f"""
      const C = require({str(CONDITIONS_JS)!r});
      const cases = require({str(VECTORS)!r}).cases;
      console.log(JSON.stringify(cases.map(c => C.evaluateField(
        {{conditions: c.conditions || [], requiredWhen: c.requiredWhen || null,
          required: c.required || false}}, c.root, c.person))));
    """
    proc = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    js = json.loads(proc.stdout)
    cases = vector_cases()
    assert len(js) == len(cases)
    disagreements = [
        (case["name"], case["expect"], got)
        for case, got in zip(cases, js)
        if got != case["expect"]
    ]
    assert not disagreements, f"JS/Python condition parity broken: {disagreements}"


def test_active_fields_uses_the_real_fixture_gates():
    m = model("onboarding_requirements_BGR.json")
    hidden = ("operatingAddress", "addressLine1")

    same = FormValues(root={"operatingAddress": {"sameAsRegistered": True}})
    assert hidden not in [x.path for x in active_fields(m, same)]

    different = FormValues(root={"operatingAddress": {"sameAsRegistered": False}})
    assert hidden in [x.path for x in active_fields(m, different)]

    # Unanswered gate: is_false is false for an absent value.
    assert hidden not in [x.path for x in active_fields(m, FormValues())]


def test_required_when_field_stays_visible_when_its_gate_is_false():
    m = model("payout_requirements_fedwire_business.json")
    postal = ("destination", "recipient", "postalAddress", "postalCode")
    values = FormValues(
        root={"destination": {"recipient": {"postalAddress": {"country": "HKG"}}}}
    )
    assert postal in [x.path for x in active_fields(m, values)]  # visible…
    assert not validate(m, values).for_field(postal)  # …but not required


# --- §9.4 assembly ------------------------------------------------------------------


def sample_value(field) -> list[str]:
    """A plausible submitted answer for one field, from its own schema."""
    t = field.effective_type
    if t == "boolean":
        return ["true"]
    if field.allowed_values:
        return field.allowed_values[:1]
    if t == "number":
        return ["50"]
    if t == "integer":
        return ["100"]
    if t == "date":
        return ["1990-01-01"] if field.constraints.min_age_years else ["2020-01-01"]
    if t == "email":
        return ["person@example.com"]
    if t == "url":
        return ["https://example.com"]
    if t == "phone":
        return ["+359 2 123 4567"]
    if field.constraints.example:
        return [field.constraints.example]
    return ["Sample value"]


def full_submission(m, persons=1, doc_ids=("doc_1",)):
    """Every field answered — including conditionally hidden ones, so the tests
    prove the server drops what the browser should not have sent."""
    items: list[tuple[str, str]] = [("documentIds", d) for d in doc_ids]
    items.append(("clientReferenceId", "ref-123"))
    for field in m.fields:
        items += [(field_name(field.path), v) for v in sample_value(field)]
    for index, row in enumerate(m.persons[:persons]):
        items.append((f"p.{index}.role", row.role))
        items.append((f"p.{index}.documentIds", f"doc_p{index}"))
        for field in row.fields:
            items += [(field_name(field.path, index), v) for v in sample_value(field)]
    return items


def test_bgr_valid_submission_assembles_and_validates():
    m = model("onboarding_requirements_BGR.json")
    values = parse_submission(m, full_submission(m, persons=2))
    errors = validate(m, values)
    assert errors.ok, (errors.fields, errors.form, errors.documents)

    body = assemble(m, values)
    # nesting by path, no list indices in dialect-A pointers
    assert body["businessInfo"]["legalName"] == "Sample value"
    assert body["companyClassification"]["primaryIndustry"] == "accounting_and_auditing"
    # inactive fields never reach the body even though they were submitted
    # only the gate itself survives; its six dependents were submitted and dropped
    assert body["operatingAddress"] == {"sameAsRegistered": True}
    assert "primaryIndustryOther" not in body["companyClassification"]
    # omission rule: no nulls, no empty strings anywhere
    assert not _has_empties(body)
    # persons: roles + own documentIds. `roles[]` is the person's *answer* — the
    # requirement row's `role` is a selector, and its catch-all value `any` is
    # not a role Conduit accepts (§5.4, verified live 2026-08-28).
    people = body["ownership"]["persons"]
    assert [p["roles"] for p in people] == [["BENEFICIAL_OWNER"], ["BENEFICIAL_OWNER"]]
    assert all("any" not in p["roles"] for p in people)
    assert people[0]["documentIds"] == ["doc_p0"]
    assert people[0]["firstName"] == "Sample value"
    assert people[0]["ownershipPercent"] == 50.0
    assert people[0]["sharesAllocated"] == 100
    assert body["documentIds"] == ["doc_1"]
    assert body["clientReferenceId"] == "ref-123"


def test_person_roles_never_emit_the_requirement_rows_wildcard():
    """§5.4. `role: "any"` selects the catch-all requirement row; it is not a
    value `ownership.persons[].roles[]` accepts, and the live API says so."""
    m = model("onboarding_requirements_BGR.json")
    wildcard, named = m.persons[0], m.persons[1]
    assert (wildcard.role, named.role) == ("any", "BENEFICIAL_OWNER")

    blank = forms.PersonValues(role="any")
    assert forms.person_roles(wildcard, blank, {}) == []  # never `["any"]`
    assert forms.person_roles(named, forms.PersonValues(role="BENEFICIAL_OWNER"), {}) == [
        "BENEFICIAL_OWNER"
    ]
    # The person's own answer always wins over the row's selector.
    answered = {"roles": ["CONTROLLING_PERSON"]}
    assert forms.person_roles(wildcard, blank, answered) == ["CONTROLLING_PERSON"]
    assert forms.person_roles(named, blank, answered) == ["CONTROLLING_PERSON"]


def _has_empties(node) -> bool:
    if isinstance(node, dict):
        return any(_has_empties(v) for v in node.values())
    if isinstance(node, list):
        return not node or any(_has_empties(v) for v in node)
    return node is None or node == ""


def test_optional_and_empty_keys_are_omitted_entirely():
    m = model("onboarding_requirements_BGR.json")
    items = [(field_name(("businessInfo", "legalName")), "Acme OOD")]
    body = assemble(m, parse_submission(m, items))
    assert body == {"businessInfo": {"legalName": "Acme OOD"}}


def test_fedwire_business_recipient_subtree():
    m = model("payout_requirements_fedwire_business.json")
    items = [
        ("f.virtualAccountId", "va_1"),
        ("f.destination.type", "fiat"),
        ("f.destination.rail", "fedwire"),
        ("f.destination.recipient.accountNumber", "12345678"),
        ("f.destination.recipient.routingNumber", "021000021"),
        ("f.destination.recipient.accountType", "CHECKING"),
        ("f.destination.recipient.type", "BUSINESS"),
        ("f.destination.recipient.legalName", "Acme Inc"),
        ("f.destination.recipient.bankAddress.addressLine1", "1 Bank St"),
        ("f.destination.recipient.bankAddress.city", "New York"),
        ("f.destination.recipient.bankAddress.country", "USA"),
        ("f.destination.recipient.postalAddress.addressLine1", "2 Main St"),
        ("f.destination.recipient.postalAddress.city", "Austin"),
        ("f.destination.recipient.postalAddress.country", "USA"),
        ("f.destination.recipient.postalAddress.postalCode", "78701"),
        # Discovery says this fedwire route requires it — the engine obeys
        # discovery and never second-guesses which rail a field belongs to.
        ("f.destination.ach.authorizationType", "written"),
        ("f.destination.recipient.bankName", ""),  # optional + empty -> omitted
    ]
    values = parse_submission(m, items)
    assert validate(m, values).ok
    body = assemble(m, values)
    recipient = body["destination"]["recipient"]
    assert recipient["routingNumber"] == "021000021"
    assert recipient["accountType"] == "CHECKING"
    assert recipient["bankAddress"] == {
        "addressLine1": "1 Bank St",
        "city": "New York",
        "country": "USA",
    }
    assert "bankName" not in recipient
    assert body["virtualAccountId"] == "va_1"


# --- §9.5 validation -----------------------------------------------------------------


def one_field_model(**kw):
    from app.forms import FormModel

    return FormModel(fields=[Field(path=("x",), label="X", **kw)])


def check(field_kwargs, raw):
    m = one_field_model(**field_kwargs)
    values = parse_submission(m, [("f.x", v) for v in raw])
    return validate(m, values).for_field(("x",))


def test_required_after_condition_filtering():
    assert check({"type": "string", "required": True}, [""])
    assert not check({"type": "string", "required": True}, ["ok"])
    from app.forms import Constraints, FormModel

    gated = FormModel(
        fields=[
            Field(path=("gate",), label="G", type="boolean"),
            Field(
                path=("x",),
                label="X",
                required=True,
                conditions=[Condition(path=("gate",), operator="is_true")],
                constraints=Constraints(),
            ),
        ]
    )
    assert validate(gated, parse_submission(gated, [("f.gate", "false")])).ok


@pytest.mark.parametrize(
    "kwargs,raw,expect_error",
    [
        ({"constraints": {"pattern": r"^\d{9}$"}}, "12345678", True),
        ({"constraints": {"pattern": r"^\d{9}$"}}, "123456789", False),
        # Unanchored, JSON-Schema style (fixture: legalStructureOther `\S`).
        ({"constraints": {"pattern": r"\S"}}, "Sample value", False),
        ({"constraints": {"pattern": r"\d"}}, "no digits here", True),
        ({"constraints": {"max_length": 3}}, "abcd", True),
        ({"constraints": {"min_length": 4}}, "abc", True),
        ({"constraints": {"format": "safeString"}}, "ok\x07bell", True),
        ({"constraints": {"format": "safeString"}}, "ok", False),
        ({"constraints": {"format": "prose"}}, "line—dash", False),
        ({"type": "number", "constraints": {"min": 0, "max": 100}}, "101", True),
        ({"type": "number", "constraints": {"min": 0, "max": 100}}, "100", False),
        ({"type": "integer", "constraints": {"min": 0}}, "-1", True),
        ({"type": "date", "constraints": {"min_date": "1900-01-01"}}, "1899-12-31", True),
        ({"type": "date", "constraints": {"max_date": "today"}}, "2999-01-01", True),
        ({"type": "date", "constraints": {"max_date": "today"}}, "2020-01-01", False),
        ({"type": "date", "constraints": {"min_age_years": 18}}, "2020-01-01", True),
        ({"type": "date", "constraints": {"min_age_years": 18}}, "1990-01-01", False),
        ({"type": "date"}, "not-a-date", True),
        ({"type": "boolean", "must_equal": True}, "false", True),
        ({"type": "boolean", "must_equal": True}, "true", False),
        ({"type": "enum", "allowed_values": ["A", "B"]}, "C", True),
        ({"type": "enum", "allowed_values": ["A", "B"]}, "A", False),
        ({"type": "country", "allowed_values": ["BGR"]}, "XXX", True),
    ],
)
def test_constraint_kinds(kwargs, raw, expect_error):
    from app.forms import Constraints

    kwargs = dict(kwargs)
    if "constraints" in kwargs:
        kwargs["constraints"] = Constraints(**kwargs["constraints"])
    assert bool(check(kwargs, [raw])) is expect_error


def test_a_refused_value_is_quoted_back_masked_and_never_whole():
    """This message is the one place the engine quotes the operator's own
    typing back, and a batch keeps it: the row's sentences are written to
    `payout_batch_rows.errors`, which is plaintext JSONB with no retention job,
    and the results CSV prints the first of them. The value that lands in an
    enum cell when a spreadsheet is pasted one column over is an account number.

    `counterparties.mask` is what masks it — the same four-character tail the
    contact list and the DTO walker show — so there is one answer in this
    codebase to how much of a coordinate may be quoted, not one per surface.
    """
    (message,) = check({"type": "enum", "allowed_values": ["CHECKING"]}, ["12345678901234567"])
    assert message.detail == "'••••4567' is not an accepted value."
    # The list of what *would* have been accepted is untouched: it is the half of
    # this message that says how to fix the row, and it is the schema's, not the
    # operator's.
    assert message.allowed_values == ["CHECKING"]


@pytest.mark.parametrize(
    "typed",
    ["x", "abcd", "José García", "СБЕРБАНК", "GB29 NWBK 6016 1331 9268 19"],
)
def test_a_refused_value_too_short_or_too_odd_to_mask_is_still_never_shown_whole(typed):
    """The cells this message judges hold arbitrary operator text — one
    character, a name, another script, a spaced IBAN. `mask` masks a value of
    four characters or fewer *whole* rather than showing it, so there is no
    length at which the engine falls back to the raw string and nothing to
    special-case at the call site. (An empty cell never reaches this branch:
    absence is `required`'s question, not membership's.)
    """
    (message,) = check({"type": "enum", "allowed_values": ["CHECKING"]}, [typed])
    assert typed not in message.detail
    assert message.detail.startswith("'••••") and message.detail.count("•") == 4


def test_enum_array_membership_and_min_selections():
    from app.forms import Constraints

    kwargs = {
        "type": "enumArray",
        "allowed_values": ["A", "B"],
        "constraints": Constraints(min=1),
    }
    assert check(kwargs, ["A", "B"]) == []
    assert check(kwargs, ["A", "C"])  # membership
    assert check({**kwargs, "required": True}, [])  # min selections via required


def test_all_stages_collect_rather_than_fail_fast():
    from app.forms import Constraints

    messages = check(
        {
            "type": "enum",
            "allowed_values": ["A"],
            "must_equal": "A",
            "constraints": Constraints(max_length=1, pattern="^Z$"),
        },
        ["BB"],
    )
    assert len(messages) == 4  # pattern + maxLength + mustEqual + allowedValues


@pytest.mark.parametrize(
    "value,ok",
    [
        ("021000021", True),
        ("011401533", True),
        ("122105155", True),
        ("021000022", False),
        ("12345678", False),
        ("abcdefghi", False),
        ("0210000211", False),
    ],
)
def test_aba_checksum_vectors(value, ok):
    assert aba_valid(value) is ok


@pytest.mark.parametrize(
    "value,ok",
    [
        ("DE89370400440532013000", True),
        ("GB82 WEST 1234 5698 7654 32", True),
        ("BE68539007547034", True),
        ("DE89370400440532013001", False),
        ("GB82WEST12345698765431", False),
        ("XX00", False),
        ("", False),
    ],
)
def test_iban_mod97_vectors(value, ok):
    assert iban_valid(value) is ok


def test_dialect_b_validators_run_on_the_real_fixtures():
    aba = model("payout_requirements_ach_individual.json")
    bad = parse_submission(aba, [("f.destination.recipient.routingNumber", "021000022")])
    assert validate(aba, bad).for_field(("destination", "recipient", "routingNumber"))

    sepa = model("payout_requirements_sepa_business.json")
    bad_iban = parse_submission(sepa, [("f.destination.recipient.iban", "DE00")])
    assert validate(sepa, bad_iban).for_field(("destination", "recipient", "iban"))


def test_person_min_count_and_min_documents_floor():
    m = model("onboarding_requirements_BGR.json")
    errors = validate(m, FormValues())
    forms = [e.detail for e in errors.form]
    assert any("any" in d for d in forms) and any("BENEFICIAL_OWNER" in d for d in forms)
    assert errors.documents and "1 document" in errors.documents[0].detail

    values = parse_submission(m, full_submission(m, persons=2, doc_ids=()))
    assert validate(m, values).documents  # floor still unmet


def test_person_max_count():
    from app.forms import FormModel, PersonRequirement

    m = FormModel(persons=[PersonRequirement(role="any", min_count=1, max_count=1)])
    values = FormValues(persons=[PersonValues(role="any"), PersonValues(role="any")])
    assert any("At most 1" in e.detail for e in validate(m, values).form)


def test_hardcoded_us_prechecks():
    """§6.7 — only when the country's own schema supplies no enum."""
    m = model("onboarding_requirements_BGR.json")  # state is a free string here
    items = [
        ("f.registeredAddress.country", "USA"),
        ("f.registeredAddress.state", "California"),
        ("f.registeredAddress.postalCode", "9410"),
    ]
    errors = validate(m, parse_submission(m, items))
    assert errors.for_field(("registeredAddress", "state"))
    assert errors.for_field(("registeredAddress", "postalCode"))

    good = [
        ("f.registeredAddress.country", "USA"),
        ("f.registeredAddress.state", "US-CA"),
        ("f.registeredAddress.postalCode", "94105-1234"),
    ]
    ok = validate(m, parse_submission(m, good))
    assert not ok.for_field(("registeredAddress", "state"))
    assert not ok.for_field(("registeredAddress", "postalCode"))

    # USA fixture ships a 56-value enum, so the ISO check must stand down.
    usa = model("onboarding_requirements_USA.json")
    usa_items = [
        ("f.registeredAddress.country", "USA"),
        ("f.registeredAddress.state", "US-CA"),
        ("f.registeredAddress.postalCode", "94105"),
    ]
    assert not validate(usa, parse_submission(usa, usa_items)).for_field(
        ("registeredAddress", "state")
    )


def test_non_us_address_is_left_alone():
    m = model("onboarding_requirements_BGR.json")
    items = [
        ("f.registeredAddress.country", "BGR"),
        ("f.registeredAddress.state", "Sofia-grad"),
        ("f.registeredAddress.postalCode", "1000"),
    ]
    errors = validate(m, parse_submission(m, items))
    assert not errors.for_field(("registeredAddress", "state"))
    assert not errors.for_field(("registeredAddress", "postalCode"))


def test_today_is_injectable_so_the_suite_does_not_rot():
    from app.forms import Constraints

    m = one_field_model(type="date", constraints=Constraints(max_date="today"))
    values = parse_submission(m, [("f.x", "2026-06-01")])
    assert validate(m, values, today=date(2026, 1, 1)).for_field(("x",))
    assert not validate(m, values, today=date(2027, 1, 1)).for_field(("x",))


# --- §9.6 server 422 mapping ---------------------------------------------------------


def error(pointer, detail="bad", category=None, allowed=()):
    return FieldError(
        pointer=pointer, detail=detail, category=category, allowed_values=list(allowed)
    )


def test_422_field_hit():
    m = model("onboarding_requirements_BGR.json")
    mapped = map_validation_errors(
        m, [error("/businessInfo/taxId", "Unknown tax id", allowed=["A", "B"])]
    )
    messages = mapped.for_field(("businessInfo", "taxId"))
    assert messages[0].detail == "Unknown tax id"
    assert messages[0].allowed_values == ["A", "B"]
    assert mapped.form == []


def test_422_person_scoped_hit():
    m = model("onboarding_requirements_BGR.json")
    mapped = map_validation_errors(m, [error("/ownership/persons/1/firstName", "Too short")])
    assert mapped.for_field(("firstName",), person_index=1)[0].detail == "Too short"
    assert mapped.form == []


def test_422_document_category_and_unmatched_pointer():
    m = model("onboarding_requirements_BGR.json")
    mapped = map_validation_errors(
        m,
        [
            error("/documents", "Missing proof of address", category="document"),
            error("/somethingNew", "Server knows a field we do not"),
            error("/ownership/persons/0/unknownField", "Nested unknown"),
        ],
    )
    assert mapped.documents[0].detail == "Missing proof of address"
    details = [e.detail for e in mapped.form]
    assert "/somethingNew: Server knows a field we do not" in details
    assert any("unknownField" in d for d in details)  # never dropped
    assert mapped.fields == {}


def test_422_mapping_against_the_real_captured_response():
    """FORM_ENGINE_SPEC §7's promised test, now that the fixture exists.

    Captured live 2026-08-28 by `tests/e2e/02_onboarding.py` (submit with the
    `ownership` section omitted). Two things about the real body that no
    hand-built dict predicted: the envelope's `type` is `ONBOARDING_NOT_READY`
    rather than a generic validation error, and the pointer is
    `/individual:any` — **not** RFC 6901, and not the pointer of any field. The
    mapper's contract is that such an error is surfaced at form level rather
    than dropped, and that is exactly what has to happen here.
    """
    from app.conduit.client import parse_problem
    import httpx

    raw = json.loads((FIXTURES / "validation_error_422.json").read_text())
    problem = parse_problem(httpx.Response(422, json=raw))
    assert problem.status == 422
    assert problem.type == "ONBOARDING_NOT_READY"
    assert problem.resolution and problem.correlation_id  # both are shown to the operator

    m = model("onboarding_requirements_BGR.json")
    mapped = map_validation_errors(m, problem.errors)
    assert mapped.fields == {} and mapped.documents == []
    assert [e.detail for e in mapped.form] == [
        "/individual:any: At least 1 any(s) required, got 0"
    ]
    assert not mapped.ok  # the form re-renders with it, never silently succeeds


def test_422_pointer_escapes_are_decoded():
    from app.forms import FormModel

    m = FormModel(fields=[Field(path=("a/b",), label="A")])
    mapped = map_validation_errors(m, [error("/a~1b", "escaped")])
    assert mapped.for_field(("a/b",))[0].detail == "escaped"


# --- §8 render contract ---------------------------------------------------------------


def test_render_model_groups_follow_first_appearance():
    m = model("onboarding_requirements_BGR.json")
    r = render_model(m)
    assert [g.key for g in r.groups] == [
        "businessInfo",
        "registeredAddress",
        "operatingAddress",
        "companyClassification",
        "businessActivity",
        "regulatoryHistory",
        "certification",
    ]
    assert r.groups[0].title == "Business information"
    assert sum(len(g.fields) for g in r.groups) == len(m.fields)


def test_render_model_unknown_group_is_title_cased():
    from app.forms import FormModel

    m = FormModel(fields=[Field(path=("x",), label="X", group="wildNewSection")])
    assert render_model(m).groups[0].title == "Wild new section"


def test_render_field_carries_widget_attrs_conditions_and_errors():
    m = model("onboarding_requirements_BGR.json")
    values = parse_submission(m, [("f.businessInfo.legalName", "")])
    errors = validate(m, values)
    r = render_model(m, values, errors)
    by_name = {rf.name: rf for g in r.groups for rf in g.fields}

    legal = by_name["f.businessInfo.legalName"]
    assert legal.widget == "text" and legal.required
    assert legal.attrs == {"required": True, "maxlength": 255}
    assert legal.errors[0].detail == "This field is required."

    operating = by_name["f.operatingAddress.addressLine1"]
    conditions = json.loads(operating.conditions_json)
    assert conditions == [
        {
            "path": "operatingAddress.sameAsRegistered",
            "operator": "is_false",
            "scope": "root",
        }
    ]

    industry = by_name["f.companyClassification.primaryIndustry"]
    assert industry.widget == "select"
    # verbatim allowedValues key, options[] label for display
    assert industry.choices[0] == ("accounting_and_auditing", "Accounting and Auditing")

    entity_id = by_name["f.businessInfo.businessEntityId"]
    assert entity_id.attrs["pattern"] == r"^(?:\d{9,13})$"  # anchored -> emitted
    other = by_name["f.companyClassification.legalStructureOther"]
    assert "pattern" not in other.attrs  # unanchored `\S` -> server-side only


def test_render_person_cards_and_document_rows():
    m = model("onboarding_requirements_BGR.json")
    values = parse_submission(m, full_submission(m, persons=2))
    r = render_model(m, values)
    assert [c.role for c in r.persons] == ["any", "BENEFICIAL_OWNER"]
    assert r.persons[1].roles == ["any", "BENEFICIAL_OWNER"]  # role picker choices
    assert r.persons[1].ownership_threshold is None  # rendered conditionally
    assert r.persons[0].min_count == 1 and r.persons[0].document_ids == ["doc_p0"]
    assert len(r.persons[0].fields) == 21  # incl. the undeclared address block
    assert [d.title for d in r.documents][2] == "Operating Permit"
    assert r.min_documents == 1


def test_render_document_row_branches():
    """No fixture exercises these yet — the branches exist for when one does."""
    m = parse(
        {
            "schemaVersion": "3",
            "fields": [],
            "minDocuments": 1,
            "documents": [
                {
                    "canonicalType": "PROOF_OF_ADDRESS",
                    "title": "Proof of address",
                    "alternatives": ["UTILITY_BILL", "BANK_STATEMENT"],
                    "groupId": "address",
                    "addressTarget": "operatingAddress",
                    "minCount": 2,
                }
            ],
        }
    )
    row = render_model(m).documents[0]
    assert row.alternatives == ["UTILITY_BILL", "BANK_STATEMENT"]
    assert row.group_id == "address" and row.address_target == "operatingAddress"
    assert validate(m, FormValues(document_ids=["doc_1"])).documents


def test_payout_render_groups_by_path_prefix():
    m = model("payout_requirements_fedwire_business.json")
    keys = [g.key for g in render_model(m).groups]
    assert keys[0] == "payout" and "recipient" in keys and "bank" in keys


def test_html_attrs_for_numeric_and_date_fields():
    m = model("onboarding_requirements_BGR.json")
    r = render_model(m, FormValues(persons=[PersonValues(role="any")]))
    by_name = {rf.name: rf for rf in r.persons[0].fields}
    assert by_name["p.0.f.ownershipPercent"].attrs["step"] == "any"
    assert by_name["p.0.f.ownershipPercent"].attrs["min"] == 0
    assert by_name["p.0.f.sharesAllocated"].attrs["step"] == "1"
    assert by_name["p.0.f.birthDate"].attrs["min"] == "1900-01-01"


# --- form-engine findings ------------------------------------------------------------


def test_person_mincount_counts_the_roles_answered_not_the_card_used():
    """A beneficial owner entered under the catch-all `any` row is still one.

    Counting requirement-row membership instead of the coerced `/roles` answer
    failed a correctly-filled form — and, worse, passed one where nobody held a
    required role.
    """
    m = model("onboarding_requirements_BGR.json")
    wildcard, owner = m.persons[0], m.persons[1]
    assert (wildcard.role, owner.role, owner.min_count) == ("any", "BENEFICIAL_OWNER", 1)

    # One person, typed into the `any` card, answering BENEFICIAL_OWNER.
    values = FormValues(
        persons=[PersonValues(role="any", values={"roles": ["BENEFICIAL_OWNER"]})]
    )
    assert forms.satisfies(wildcard, values.persons[0], m)
    assert forms.satisfies(owner, values.persons[0], m)
    assert not any("BENEFICIAL_OWNER person(s) required" in e.detail for e in validate(m, values).form)

    # …and the converse: a card labelled BENEFICIAL_OWNER whose occupant said
    # otherwise does not satisfy the row.
    other = FormValues(
        persons=[PersonValues(role="BENEFICIAL_OWNER", values={"roles": ["LEGAL_REPRESENTATIVE"]})]
    )
    assert not forms.satisfies(owner, other.persons[0], m)
    assert any("BENEFICIAL_OWNER person(s) required" in e.detail for e in validate(m, other).form)

    # No answer at all: fall back to the card, which is all we know.
    blank = FormValues(persons=[PersonValues(role="BENEFICIAL_OWNER")])
    assert forms.satisfies(owner, blank.persons[0], m)


@pytest.mark.parametrize(
    "raw,detail",
    [
        ("NaN", "finite"),
        ("Infinity", "finite"),
        ("-Infinity", "finite"),
        ("1e10000", "finite"),
    ],
)
def test_non_finite_numbers_are_invalid_not_values(raw, detail):
    """`Decimal` parses all four; none of them is JSON, and every comparison
    against NaN is false, so a constraint check on one silently passes."""
    field = Field(path=("n",), label="N", type="number")
    coerced = coerce(field, raw)
    assert isinstance(coerced, forms.Invalid) and detail in coerced.detail

    errors = forms.FormErrors()
    forms._check_field(field, coerced, True, "f.n", errors, date(2026, 1, 1))
    assert errors.for_field(("n",))  # reported, never silently dropped


@pytest.mark.parametrize(
    "raw,ok",
    [("9007199254740991", True), ("-9007199254740991", True),
     ("9007199254740992", False), ("9" * 40, False)],
)
def test_integers_beyond_the_browsers_range_are_refused(raw, ok):
    """Python integers are unbounded; the browser holds the same digits as a
    double and rounds. Rather than let the two evaluators disagree about the
    same submission, the value is out of range."""
    coerced = coerce(Field(path=("i",), label="I", type="integer"), raw)
    assert (not isinstance(coerced, forms.Invalid)) is ok
    if not ok:
        assert "between" in coerced.detail


def test_equality_is_type_strict_so_the_browser_agrees():
    """Python says True == 1; `===` does not. A field gated on `eq: 1` must not
    activate server-side while staying hidden in the browser."""
    assert forms.same(True, 1) is False and forms.same(False, 0) is False
    assert forms.same(1, 1.0) is True  # JS holds both as the same double
    gate = Condition(path=("flag",), operator="eq", value=1)
    assert forms.evaluate_condition(gate, {"flag": True}) is False
    member = Condition(path=("flag",), operator="in", values=[1, "yes"])
    assert forms.evaluate_condition(member, {"flag": True}) is False


def test_person_addresses_get_the_us_pre_checks_too():
    """§6.7 ran over the business addresses only, so every person's US address
    went unchecked — the same trap, one nesting level down."""
    m = model("onboarding_requirements_BGR.json")
    values = FormValues(
        persons=[
            PersonValues(
                role="any",
                values={"address": {"country": "USA", "state": "California", "postalCode": "9410"}},
            )
        ]
    )
    errors = validate(m, values)
    assert "ISO 3166-2" in errors.for_field(("address", "state"), person_index=0)[0].detail
    assert "ZIP" in errors.for_field(("address", "postalCode"), person_index=0)[0].detail

    good = FormValues(
        persons=[
            PersonValues(
                role="any",
                values={"address": {"country": "USA", "state": "US-CA", "postalCode": "94105-1234"}},
            )
        ]
    )
    assert not validate(m, good).for_field(("address", "state"), person_index=0)


def test_required_when_reaches_the_browser():
    """Item 5: the client could decide `active` but never `required`, so an
    optional-but-visible dialect-B field stayed optional-looking after the
    answer that made it mandatory."""
    m = parse(json.loads((FIXTURES / "payout_requirements_ach_individual.json").read_text()))
    gated = [f for f in m.fields if f.required_when is not None]
    assert gated, "the ACH fixture is the one that ships requiredWhen"

    rendered = forms.render_model(m)
    by_name = {rf.name: rf for group in rendered.groups for rf in group.fields}
    for field in gated:
        rf = by_name[field_name(field.path)]
        spec = json.loads(rf.required_when_json)
        assert spec["operator"] == field.required_when.operator
        assert spec["path"] == ".".join(field.required_when.path)
        assert rf.base_required == field.required
    # Fields without one say so explicitly rather than omitting the key.
    plain = next(rf for rf in by_name.values() if rf.field.required_when is None)
    assert plain.required_when_json == "null"


# --- an optional boolean that something conditions on (spec §3) -----------------------

SAME_AS_REGISTERED = ("operatingAddress", "sameAsRegistered")
OPERATING_ADDRESS_FIELDS = {
    "operatingAddress.addressLine1",
    "operatingAddress.addressLine2",
    "operatingAddress.city",
    "operatingAddress.country",
    "operatingAddress.postalCode",
    "operatingAddress.state",
}


def operating_address(values) -> set[str]:
    m = model("onboarding_requirements_BGR.json")
    return {f.dotted for f in active_fields(m, values)} & OPERATING_ADDRESS_FIELDS


def test_an_optional_boolean_a_condition_reads_is_a_radio_not_a_checkbox():
    """A checkbox submits `true` or nothing, so `is_false` on one could never
    fire and its six dependants were unreachable — in both evaluators."""
    m = model("onboarding_requirements_BGR.json")
    gate = m.field_by_path(SAME_AS_REGISTERED)
    assert gate.required is False and gate.conditioned is True
    assert gate.widget == "radio"


def test_optional_booleans_nobody_conditions_on_stay_checkboxes():
    """The rule is narrow on purpose: it is about the gate, not about booleans."""
    m = model("onboarding_requirements_BGR.json")
    plain = [
        f
        for f in m.fields
        if f.type == "boolean" and not f.required and f.path != SAME_AS_REGISTERED
    ]
    assert plain, "fixture no longer has an unconditioned optional boolean"
    assert {f.widget for f in plain} == {"checkbox"}
    assert not any(f.conditioned for f in plain)


def test_answering_no_reveals_the_operating_address_fields():
    assert operating_address(
        FormValues(root={"operatingAddress": {"sameAsRegistered": False}})
    ) == OPERATING_ADDRESS_FIELDS


def test_unanswered_keeps_them_hidden():
    # Absent is not false, for `is_true` and `is_false` alike (§4) — which is
    # exactly why the widget has to be able to say "no".
    assert operating_address(FormValues(root={})) == set()
    assert operating_address(
        FormValues(root={"operatingAddress": {"sameAsRegistered": True}})
    ) == set()


def test_answering_no_is_carried_into_the_assembled_body():
    m = model("onboarding_requirements_BGR.json")
    values = parse_submission(m, [("f.operatingAddress.sameAsRegistered", "false")])
    assert values.root["operatingAddress"]["sameAsRegistered"] is False
    body = assemble(m, values)
    assert body["operatingAddress"]["sameAsRegistered"] is False


def test_a_gate_whose_dependants_were_filtered_out_stops_being_one():
    """`payments.recipient_model` builds a model from a subset of the fields.
    `conditioned` is a property of the model, so it is recomputed there."""
    from app.payments import payout_model, recipient_model

    full = payout_model(load("payout_requirements_fedwire_intercompany.json"))
    country = ("destination", "recipient", "postalAddress", "country")
    assert any(f.path == country and f.conditioned for f in full.fields)

    trimmed = recipient_model(full)
    kept = [f for f in trimmed.fields if f.path == country]
    # Still conditioned here — the postalCode dependant survives the whitelist
    # filter. The assertion that matters is that it was *recomputed* from the
    # trimmed field list rather than inherited from `full`.
    assert kept and kept[0].conditioned is any(
        f.required_when and f.required_when.path == country for f in trimmed.fields
    )


def test_person_row_affordances_count_the_same_way_validation_does():
    """The Add/remove counter and the validator must not disagree.

    `_person_rows` counted the card a person was typed into while
    `validate` counted their answered `/roles`, so one person holding several
    roles satisfied the submission but still read "0 of 1" on every row but the
    card's — with an Add button asking for people the form did not need.
    """
    from app.web.onboarding import _person_rows

    # Three role rows, the way CAN discovery ships them — the fixtures carry
    # `any` + BENEFICIAL_OWNER only — and one person who answers all three.
    payload = load("onboarding_requirements_BGR.json")
    owner = next(r for r in payload["individualRequirements"] if r["role"] == "BENEFICIAL_OWNER")
    payload["individualRequirements"] += [
        {**owner, "role": role} for role in ("CONTROLLING_PERSON", "LEGAL_REPRESENTATIVE")
    ]
    m = parse(payload)
    roles = ["BENEFICIAL_OWNER", "CONTROLLING_PERSON", "LEGAL_REPRESENTATIVE"]
    values = FormValues(persons=[PersonValues(role="any", values={"roles": roles})])

    rows = {r["role"]: r for r in _person_rows(m, values)}
    assert [rows[role]["count"] for role in roles] == [1, 1, 1], "one person, three rows at once"
    assert not any(r["count"] < r["min_count"] for r in rows.values()), "no Add is owed"
    assert not any(r["can_remove"] for r in rows.values()), "and nobody is spare"
    # The validator agrees, which is the whole point.
    assert not validate(m, values).form


def test_learn_turns_an_unadvertised_422_pointer_into_a_field():
    """Reliance: Conduit refuses a person field discovery never advertised.

    Verified live 2026-09-07 — CAN discovery lists no `/birthDate`,
    `/nationality` or `/taxResidencyCountry` on any `individualRequirements`
    row, and the submission was refused for all three. Before this the pointers
    reached the operator as form-level prose with no input to answer them in.
    """
    m = model("onboarding_requirements_BGR.json")
    errs = [
        forms_error("/ownership/persons/0/residencyPermitExpiryDate", "Residency permit expiry is required"),
        forms_error("/ownership/persons/0/firstName", "First Name is required"),  # advertised
        forms_error("/businessInfo/relianceAttestationDate", "Attestation date is required"),
    ]

    cards = [PersonValues(role="any")]
    learned = forms.learn(m, errs, cards)
    by_pointer = {d["pointer"]: d for d in learned}
    assert set(by_pointer) == {"/residencyPermitExpiryDate", "/businessInfo/relianceAttestationDate"}
    assert by_pointer["/residencyPermitExpiryDate"]["label"] == "Residency permit expiry date"
    assert by_pointer["/residencyPermitExpiryDate"][forms.LEARNED_SCOPE] == "person"
    # No type in a 422; the name is the only signal.
    assert by_pointer["/businessInfo/relianceAttestationDate"]["type"] == "date"

    # …and merged in, they are ordinary fields: rendered, and required.
    merged = forms.with_learned(m, learned)
    assert merged.field_by_path(("businessInfo", "relianceAttestationDate")) is not None
    assert all(
        any(f.path == ("residencyPermitExpiryDate",) for f in row.fields)
        for row in merged.persons
    )
    values = FormValues(persons=[PersonValues(role="any", values={"roles": ["BENEFICIAL_OWNER"]})])
    assert validate(merged, values).for_field(("residencyPermitExpiryDate",), 0)

    # Learning is idempotent: a second identical rejection adds nothing.
    assert forms.learn(merged, errs, cards) == []


def test_a_learned_person_field_was_required_only_of_the_person_the_422_named():
    """One 422 pointer names one person; every other card is offered the input
    but owes no answer.

    Requiring it of all of them was a form the operator could not submit without
    inventing a residency-permit expiry for someone Conduit never asked about.
    """
    m = model("onboarding_requirements_BGR.json")
    path = ("residencyPermitExpiryDate",)
    values = FormValues(
        persons=[
            PersonValues(role="any", values={"roles": ["LEGAL_REPRESENTATIVE"]}),
            PersonValues(role="BENEFICIAL_OWNER", values={"roles": ["BENEFICIAL_OWNER"]}),
        ]
    )
    learned = forms.learn(
        m,
        [
            forms_error(
                "/ownership/persons/0/residencyPermitExpiryDate",
                "Residency permit expiry is required",
            )
        ],
        values.persons,
    )
    merged = forms.with_learned(m, learned)

    # Offered on every card: the operator may hold that person on whichever one.
    assert all(any(f.path == path for f in row.fields) for row in merged.persons)
    rendered = render_model(merged, values)
    assert [
        rf.required for card in rendered.persons for rf in card.fields if rf.field.path == path
    ] == [True, False]

    errors = validate(merged, values)
    assert errors.for_field(path, 0), "the person the 422 named still owes it"
    assert not errors.for_field(path, 1), "nobody else does"

    # …and answering for that one person is enough to clear it.
    values.persons[0].values["residencyPermitExpiryDate"] = "2030-01-01"
    assert not validate(merged, values).for_field(path, 0)


def test_a_field_only_another_row_advertised_is_learned_for_the_card_that_lacks_it():
    """The skip test is the row the pointer indexes, not "any row has it".

    Discovery may advertise a field on the beneficial-owner row alone. Demanded
    of a card that is not on that row, it was neither mapped to an input nor
    learned — the operator read the refusal and had nowhere to answer it.
    """
    payload = load("onboarding_requirements_BGR.json")
    rows = payload["individualRequirements"]
    assert [r["role"] for r in rows] == ["any", "BENEFICIAL_OWNER"]
    rows[1]["fields"] = rows[1]["fields"] + [
        {"pointer": "/residencyPermitExpiryDate", "label": "Residency permit", "type": "date"}
    ]
    m = parse(payload)
    detail = "Residency permit expiry is required"

    # Card 0 sits on `any`, card 1 on BENEFICIAL_OWNER — the cards, not the rows.
    cards = [PersonValues(role="any"), PersonValues(role="BENEFICIAL_OWNER")]

    named_0 = forms_error("/ownership/persons/0/residencyPermitExpiryDate", detail)
    learned = forms.learn(m, [named_0], cards)
    assert [d["pointer"] for d in learned] == ["/residencyPermitExpiryDate"]
    assert learned[0][forms.LEARNED_INDICES] == [0]
    # The card whose own row advertises it needs nothing learned.
    named_1 = forms_error("/ownership/persons/1/residencyPermitExpiryDate", detail)
    assert forms.learn(m, [named_1], cards) == []
    # …and merging never gives that row the field twice.
    merged = forms.with_learned(m, learned)
    assert [f.path for f in merged.persons[1].fields].count(("residencyPermitExpiryDate",)) == 1


def test_a_card_is_matched_to_its_own_row_not_to_the_row_in_its_position():
    """Card order and `individualRequirements` order are two different sequences.

    `learn` read `model.persons[index]` with a card ordinal, so which row it
    checked was decided by discovery's row order. On the BGR snapshot, rows are
    `[any, BENEFICIAL_OWNER]`: a 422 against card 1 was answered against the
    beneficial-owner row whoever card 1 actually was, and when that row already
    advertised the field nothing was learned — the dead end this feature closes.
    """
    payload = load("onboarding_requirements_BGR.json")
    rows = payload["individualRequirements"]
    assert [r["role"] for r in rows] == ["any", "BENEFICIAL_OWNER"]
    rows[1]["fields"] = rows[1]["fields"] + [
        {"pointer": "/residencyPermitExpiryDate", "label": "Residency permit", "type": "date"}
    ]
    m = parse(payload)
    named_1 = forms_error("/ownership/persons/1/residencyPermitExpiryDate", "required")

    # Card 1 is a legal representative, so it renders the `any` row's fields and
    # has no input for this pointer.
    on_any = [PersonValues(role="any"), PersonValues(role="any")]
    learned = forms.learn(m, [named_1], on_any)
    assert [d["pointer"] for d in learned] == ["/residencyPermitExpiryDate"]
    assert learned[0][forms.LEARNED_INDICES] == [1]

    # Same 422, same position, a card that really is on the row that advertises
    # it: nothing to learn. The answer follows the card, not the ordinal.
    on_owner = [PersonValues(role="any"), PersonValues(role="BENEFICIAL_OWNER")]
    assert forms.learn(m, [named_1], on_owner) == []


def test_a_pointer_naming_a_card_that_was_not_submitted_resolves_to_no_row():
    """`persons` has no default, and this is why.

    While it defaulted to `()`, every index fell outside the card list and every
    pointer resolved to `model.persons[0]` — the row-by-position guess `_row_at`
    was removed for, reinstated as a fallback. A pointer naming a card nobody
    submitted has no inputs to have already shown the field, so the honest answer
    is no row rather than the first one.
    """
    payload = load("onboarding_requirements_BGR.json")
    rows = payload["individualRequirements"]
    assert [r["role"] for r in rows] == ["any", "BENEFICIAL_OWNER"]
    rows[1]["fields"] = rows[1]["fields"] + [
        {"pointer": "/residencyPermitExpiryDate", "label": "Residency permit", "type": "date"}
    ]
    m = parse(payload)

    assert forms._row_of_card(m, [], 0) is None
    assert forms._row_of_card(m, [PersonValues(role="any")], 4) is None
    # And it is not silently substitutable: `learn` cannot be called without them.
    with pytest.raises(TypeError):
        forms.learn(m, [])


def test_removing_a_card_moves_the_demands_that_were_above_it():
    """`x-requiredIndices` holds `ownership.persons[i]` ordinals, and deleting a
    card slides every card above it down one while the stored indices stay put.

    Cards [A, B, C] with a demand on B (index 1). Remove A: C is now index 1 and
    would be asked for a field Conduit never demanded of them, while B — who owes
    it — is optional and resubmits into the identical 422.
    """
    owed = {"pointer": "/f/x", forms.LEARNED_SCOPE: "person", forms.LEARNED_INDICES: [1, 2]}
    root = {"pointer": "/businessInfo/y", forms.LEARNED_SCOPE: "root"}

    # Removing the card below them shifts both demands down one.
    assert forms.forget_person_index([owed, root], 0) == [
        {**owed, forms.LEARNED_INDICES: [0, 1]},
        root,
    ]
    # Removing a card above them moves nothing.
    assert forms.forget_person_index([owed, root], 3) == [owed, root]
    # Removing one of the two that owed it drops just that one.
    assert forms.forget_person_index([owed], 1) == [{**owed, forms.LEARNED_INDICES: [1]}]
    # The last card that owed it leaves nothing owing, so the descriptor goes —
    # an empty index list would read as "required of everyone" (`_learned`).
    assert forms.forget_person_index([{**owed, forms.LEARNED_INDICES: [1]}], 1) == []


def test_only_a_camelcase_date_suffix_makes_a_date_input():
    """The inference lowercased the segment and matched the letters `date`, so any
    name merely ending in them became `<input type="date">` — and the operator
    could not type the string the 422 demanded, making the field unanswerable.
    """
    def kind(tail):
        return forms._learned(f"/businessInfo/{tail}", ("businessInfo", tail), "", [], "root")["type"]

    assert kind("birthDate") == "date"
    assert kind("relianceAttestationDate") == "date"
    for not_a_date in ("lastUpdate", "regulatoryMandate", "candidate", "mandate"):
        assert kind(not_a_date) == "string", not_a_date


def test_a_second_rejection_widened_the_learned_field_to_the_card_it_named():
    """The same dead end, reached one rejection later.

    Keyed by pointer alone, the second 422 was dropped as a duplicate: the card
    it named kept an optional copy, the operator answered nothing there and
    resubmitted into the identical refusal.
    """
    m = model("onboarding_requirements_BGR.json")
    path = ("residencyPermitExpiryDate",)
    detail = "Residency permit expiry is required"
    named_0 = forms_error("/ownership/persons/0/residencyPermitExpiryDate", detail)
    named_1 = forms_error("/ownership/persons/1/residencyPermitExpiryDate", detail)

    cards = [PersonValues(role="any"), PersonValues(role="any")]
    stored = forms.merge_learned([], forms.learn(m, [named_0], cards))
    assert [d[forms.LEARNED_INDICES] for d in stored] == [[0]]

    # Replaying that rejection against the model it taught stores nothing new.
    taught = forms.with_learned(m, stored)
    assert forms.learn(taught, [named_0], cards) == []
    assert forms.merge_learned(stored, forms.learn(taught, [named_0], cards)) == stored

    # A rejection naming the other person widens the descriptor it finds.
    fresh = forms.learn(taught, [named_1], cards)
    assert [d[forms.LEARNED_INDICES] for d in fresh] == [[1]]
    stored = forms.merge_learned(stored, fresh)
    assert [d[forms.LEARNED_INDICES] for d in stored] == [[0, 1]]
    # Sorted and unique, so the reverse order stores the identical JSON.
    assert forms.merge_learned([], forms.learn(m, [named_1, named_0], cards)) == stored

    merged = forms.with_learned(m, stored)
    values = FormValues(
        persons=[
            PersonValues(role="any", values={"roles": ["LEGAL_REPRESENTATIVE"]}),
            PersonValues(role="BENEFICIAL_OWNER", values={"roles": ["BENEFICIAL_OWNER"]}),
        ]
    )
    rendered = render_model(merged, values)
    assert [
        rf.required for card in rendered.persons for rf in card.fields if rf.field.path == path
    ] == [True, True]

    # Both were named, so both owe an answer — and answering one is not enough.
    values.persons[0].values["residencyPermitExpiryDate"] = "2030-01-01"
    assert validate(merged, values).for_field(path, 1)
    values.persons[1].values["residencyPermitExpiryDate"] = "2031-02-02"
    errors = validate(merged, values)
    assert not errors.for_field(path, 0) and not errors.for_field(path, 1)
