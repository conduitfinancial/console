"""Schema-driven form engine (FORM_ENGINE_SPEC.md).

Two server dialects in, one `FormModel` out; from there: coercion of submitted
HTML names, condition filtering, assembly of the request body, validation, and a
render-model for the templates. Pure logic — no HTTP, no DB, no I/O.

The condition evaluator here is authoritative; `static/conditions.js` is the
browser twin and `tests/condition_vectors.json` is the shared conformance suite
both must pass (CI runs the vectors through node).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

# The engine's one import from the rest of the app, and deliberately a single
# pure function: a refused value that is quoted back to the operator is masked
# with the *same* masking the contact list and the DTO walker use, so there is
# one answer in this codebase to "how much of a coordinate may be shown" rather
# than one per surface. `mask` touches no DB and no config; the "no I/O" claim
# above is about what this module runs, and it still runs none.
from app.counterparties import mask

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3"

# The 12 documented types (§3). Anything else renders as a string + a warning.
WIDGETS = {
    "string": "text",
    "number": "number",
    "integer": "number",
    "boolean": "checkbox",  # overridden to "radio" — see Field.widget
    "date": "date",
    "email": "email",
    "url": "url",
    "phone": "tel",
    "country": "select",
    "enum": "select",
    "stringArray": "textarea",
    "enumArray": "checkboxes",
}
LIST_TYPES = ("stringArray", "enumArray")
# Number.MAX_SAFE_INTEGER. Beyond it the browser's evaluator and Python stop
# agreeing about the same submitted digits, so the engine refuses the value
# rather than letting the two sides diverge silently.
JS_SAFE_INTEGER = 2**53 - 1
NUMERIC_TYPES = ("number", "integer")


class SchemaVersionMismatch(Exception):
    """Requirements payload is not schemaVersion 3. Never render best-effort."""


class _Absent:
    """Sentinel: this field was not answered. Never reaches an assembled body."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ABSENT"


ABSENT = _Absent()


@dataclass(frozen=True)
class Invalid:
    """A submitted value that is not even the right shape (e.g. "abc" for a
    number). Carried through so validation can report it instead of crashing."""

    raw: str
    detail: str


# --- model ------------------------------------------------------------------------


@dataclass(frozen=True)
class Constraints:
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    min_date: str | None = None
    max_date: str | None = None
    min_age_years: int | None = None
    format: str | None = None
    example: str | None = None


NO_CONSTRAINTS = Constraints()


@dataclass(frozen=True)
class OptionMeta:
    label: str
    description: str | None = None
    abbr: str | None = None


@dataclass(frozen=True)
class Condition:
    path: tuple[str, ...]
    operator: str  # eq | in | not_in | exists | is_true | is_false
    value: object | None = None
    values: list | None = None
    scope: str = "root"  # "root" | "person"

    def as_json(self) -> dict:
        """Wire form for `data-conditions` and the shared vector file: dotted
        paths, exactly what `static/conditions.js` consumes."""
        out: dict[str, Any] = {"path": ".".join(self.path), "operator": self.operator}
        if self.value is not None:
            out["value"] = self.value
        if self.values is not None:
            out["values"] = self.values
        out["scope"] = self.scope
        return out


@dataclass(frozen=True)
class Field:
    path: tuple[str, ...]
    label: str
    help: str | None = None
    type: str = "string"
    required: bool = False
    group: str | None = None
    must_equal: object | None = None
    constraints: Constraints = NO_CONSTRAINTS
    allowed_values: list[str] | None = None
    options: dict[str, OptionMeta] = dc_field(default_factory=dict)
    conditions: list[Condition] = dc_field(default_factory=list)
    validator: str | None = None
    # Dialect B only. Unlike `conditions` this never hides the field — it only
    # flips `required` on (spec §4: "optional-but-visible").
    required_when: Condition | None = None
    # Learned person fields: the card indices a 422 actually named (spec §7).
    required_indices: tuple[int, ...] = ()
    # Set by `FormModel.__post_init__`, never by a parser: some other field in
    # this model has a condition that reads *this* field's path. It changes the
    # widget, and only for booleans (spec §3).
    conditioned: bool = False

    @property
    def known_type(self) -> bool:
        return self.type in WIDGETS

    @property
    def effective_type(self) -> str:
        return self.type if self.known_type else "string"

    @property
    def widget(self) -> str:
        if self.effective_type == "boolean":
            # A `mustEqual: true` field has one acceptable answer, so a single
            # checkbox says everything there is to say.
            if self.must_equal is True:
                return "checkbox"
            # Otherwise the question needs three states — yes, no, unanswered —
            # whenever the difference between "no" and "unanswered" is visible
            # to someone. It is visible in two cases: the field is required (a
            # blank answer must be refusable), or some other field's condition
            # reads this one (spec §3). A checkbox can only ever submit `true`
            # or nothing, so an `is_false` gate on an optional checkbox could
            # never fire and its dependants were unreachable in both evaluators.
            return "radio" if (self.required or self.conditioned) else "checkbox"
        return WIDGETS[self.effective_type]

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class DocumentRequirement:
    canonical_type: str
    title: str
    source: str | None = None
    guidance: str | None = None
    alternatives: list[str] = dc_field(default_factory=list)
    group_id: str | None = None
    address_target: str | None = None
    min_count: int | None = None


@dataclass(frozen=True)
class PersonRequirement:
    role: str
    min_count: int = 0
    max_count: int | None = None
    ownership_threshold: int | None = None
    fields: list[Field] = dc_field(default_factory=list)
    documents: list[DocumentRequirement] = dc_field(default_factory=list)


@dataclass(frozen=True)
class FormModel:
    fields: list[Field] = dc_field(default_factory=list)
    persons: list[PersonRequirement] = dc_field(default_factory=list)
    documents: list[DocumentRequirement] = dc_field(default_factory=list)
    min_documents: int = 0
    dialect: str = "requirements"
    # Dialect B route metadata, surfaced verbatim (spec §1) — the route obeys
    # these, the engine never interprets them.
    whitelist: dict = dc_field(default_factory=dict)
    documentation: dict = dc_field(default_factory=dict)
    blocked_jurisdictions: list[str] = dc_field(default_factory=list)
    rail: str | None = None
    schema_version: str | None = None
    context: str | None = None
    country: str | None = None
    warnings: list[str] = dc_field(default_factory=list)

    def __post_init__(self) -> None:
        """Mark the fields some condition in this model actually reads.

        Done here rather than in the two parsers because it is a property of the
        *model*, not of either wire dialect — and because every construction
        site routes through here, including `payments.recipient_model`, which
        builds a new model from a filtered subset of the fields (a gate whose
        dependants were all removed stops being a gate).
        """
        gates: dict[str, set] = {"root": set(), "person": set()}
        for source in [self.fields] + [row.fields for row in self.persons]:
            for field in source:
                for condition in list(field.conditions) + (
                    [field.required_when] if field.required_when else []
                ):
                    gates.setdefault(condition.scope, set()).add(condition.path)
        _mark_conditioned(self.fields, gates["root"])
        for row in self.persons:
            _mark_conditioned(row.fields, gates["person"])

    def field_by_path(self, path: tuple[str, ...]) -> Field | None:
        return next((f for f in self.fields if f.path == path), None)


def _mark_conditioned(fields: list[Field], gate_paths: set) -> None:
    """Rewrite `Field.conditioned` in place. The list is mutable even though the
    dataclasses are frozen, and the same `Field` object may be shared with
    another model — so a new object is substituted rather than the old one
    edited."""
    for index, field in enumerate(fields):
        conditioned = field.path in gate_paths
        if field.conditioned != conditioned:
            fields[index] = dataclasses.replace(field, conditioned=conditioned)


@dataclass
class PersonValues:
    values: dict = dc_field(default_factory=dict)
    role: str = ""
    document_ids: list[str] = dc_field(default_factory=list)


@dataclass
class FormValues:
    root: dict = dc_field(default_factory=dict)
    persons: list[PersonValues] = dc_field(default_factory=list)
    document_ids: list[str] = dc_field(default_factory=list)
    client_reference_id: str | None = None


# --- parsing ----------------------------------------------------------------------


def _pointer_path(pointer: str) -> tuple[str, ...]:
    """RFC 6901: `/a/b` -> ("a","b"), with ~1 / ~0 unescaped."""
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")]
    return tuple(p for p in parts if p != "")


def _constraints_a(raw: Mapping | None) -> Constraints:
    raw = raw or {}
    return Constraints(
        pattern=raw.get("pattern"),
        min_length=raw.get("minLength"),
        max_length=raw.get("maxLength"),
        min=raw.get("min"),
        max=raw.get("max"),
        min_date=raw.get("minDate"),
        max_date=raw.get("maxDate"),
        min_age_years=raw.get("minAgeYears"),
        format=raw.get("format"),
        example=raw.get("example"),
    )


def _options(raw: Sequence[Mapping] | None) -> dict[str, OptionMeta]:
    return {
        str(o["value"]): OptionMeta(
            label=str(o.get("label") or o["value"]),
            description=o.get("description"),
            abbr=o.get("abbr"),
        )
        for o in (raw or [])
        if isinstance(o, Mapping) and "value" in o
    }


def _conditions(raw: Sequence[Mapping] | None, person_paths: set) -> list[Condition]:
    out = []
    for c in raw or []:
        path = _pointer_path(str(c.get("pointer") or c.get("field") or ""))
        if not path:
            continue
        # No fixture carries `scope` yet. A pointer that names a sibling field
        # inside the same person row can only mean person scope; everything else
        # resolves against the root document.
        scope = c.get("scope") or ("person" if path in person_paths else "root")
        out.append(
            Condition(
                path=path,
                operator=str(c.get("operator") or "exists"),
                value=c.get("value"),
                values=list(c["values"]) if isinstance(c.get("values"), list) else None,
                scope=scope,
            )
        )
    return out


def _documents(raw: Sequence[Mapping] | None) -> list[DocumentRequirement]:
    return [
        DocumentRequirement(
            canonical_type=str(d.get("canonicalType") or ""),
            title=str(d.get("title") or d.get("canonicalType") or ""),
            source=d.get("from"),
            guidance=d.get("guidance") or d.get("helpText"),
            alternatives=list(d.get("alternatives") or []),
            group_id=d.get("groupId"),
            address_target=d.get("addressTarget"),
            min_count=d.get("minCount"),
        )
        for d in raw or []
    ]


def _field_a(raw: Mapping, warnings: list[str], person_paths: set) -> Field:
    ftype = str(raw.get("type") or "string")
    if ftype not in WIDGETS:
        message = f"unknown field type {ftype!r} at {raw.get('pointer')!r}: rendering as string"
        warnings.append(message)
        log.warning("forms: %s", message)
    return Field(
        path=_pointer_path(str(raw.get("pointer") or "")),
        label=str(raw.get("label") or ""),
        help=raw.get("helpText"),
        type=ftype,
        required=bool(raw.get("required")),
        group=raw.get("group"),
        must_equal=raw.get("mustEqual"),
        constraints=_constraints_a(raw.get("constraints")),
        allowed_values=list(raw["allowedValues"]) if raw.get("allowedValues") else None,
        options=_options(raw.get("options")),
        conditions=_conditions(raw.get("conditions"), person_paths),
    )


def _required_when(raw: Mapping | None) -> Condition | None:
    """Dialect B `requiredWhen`. Fixtures only ever use `{field, notIn}`; the
    other one-key forms map to the same operator vocabulary."""
    if not isinstance(raw, Mapping) or not raw.get("field"):
        return None
    path = tuple(str(raw["field"]).split("."))
    for key, operator in (("notIn", "not_in"), ("in", "in"), ("oneOf", "in")):
        if isinstance(raw.get(key), list):
            return Condition(path=path, operator=operator, values=list(raw[key]))
    for key in ("equals", "eq", "value"):
        if key in raw:
            return Condition(path=path, operator="eq", value=raw[key])
    return Condition(path=path, operator="exists")


def _field_b(raw: Mapping, warnings: list[str]) -> Field:
    ftype = str(raw.get("type") or "string")
    if ftype not in WIDGETS:
        message = f"unknown field type {ftype!r} at {raw.get('name')!r}: rendering as string"
        warnings.append(message)
        log.warning("forms: %s", message)
    name = str(raw.get("name") or "")
    return Field(
        path=tuple(p for p in name.split(".") if p),
        label=str(raw.get("label") or humanize(name.rsplit(".", 1)[-1])),
        help=raw.get("helpText"),
        type=ftype,
        required=bool(raw.get("required")),
        constraints=Constraints(
            pattern=raw.get("pattern"),
            min_length=raw.get("minLength"),
            max_length=raw.get("maxLength"),
        ),
        allowed_values=list(raw["enum"]) if raw.get("enum") else None,
        validator=raw.get("validator"),
        required_when=_required_when(raw.get("requiredWhen")),
    )


PERSON_ADDRESS_SOURCE = "registeredAddress"


def _person_address(root: Sequence[Field], person: Sequence[Field]) -> list[Field]:
    """The person address block discovery does not declare but submission requires.

    **Verified defect, 2026-08-28.** `POST /v2/onboarding`
    answers `422 ONBOARDING_NOT_READY` with `/ownership/persons/{i}/address/`
    `{country,addressLine1,city,state,postalCode}` — "Country is required",
    "Street 1 is required", … — while `individualRequirements[].fields` carries
    no `address` pointer at all (BGR/ITA/BRA/USA fixtures alike), and
    `OnboardingSubmitDto` documents only "roles[], scalar identity fields, and
    documentIds[]". Without this the console cannot produce a submittable body
    from discovery for any country.

    Same spirit as §6.7's hardcoded pre-checks: one clearly-marked place, no
    invented data. The block is *copied from the payload's own*
    `registeredAddress` fields — labels, types, the 248-value country list and
    which parts are optional all come from the server, only re-pointed under the
    person. Delete this function the day discovery declares the block; the guard
    below already makes it a no-op the moment it does.
    """
    if any(f.path[:1] == ("address",) for f in person):
        return []
    return [
        dataclasses.replace(f, path=("address", *f.path[1:]), group=None, conditions=[])
        for f in root
        if f.path[:1] == (PERSON_ADDRESS_SOURCE,) and len(f.path) > 1
    ]


def parse(payload: Mapping) -> FormModel:
    """Normalize either server dialect into a FormModel.

    Dialect A (`ExternalRequirementsResponseDto`) is recognised by
    `schemaVersion`; anything else is Dialect B (`PayoutRequirementsResponseDto`).
    """
    warnings: list[str] = []
    if "schemaVersion" not in payload:
        return _parse_payout(payload, warnings)

    version = str(payload.get("schemaVersion"))
    if version != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"requirements schemaVersion {version!r}, this build understands "
            f"{SCHEMA_VERSION!r} only"
        )

    root_fields = [_field_a(f, warnings, set()) for f in payload.get("fields") or []]
    persons = []
    for row in payload.get("individualRequirements") or []:
        person_paths = {_pointer_path(str(f.get("pointer") or "")) for f in row.get("fields") or []}
        person_fields = [_field_a(f, warnings, person_paths) for f in row.get("fields") or []]
        persons.append(
            PersonRequirement(
                role=str(row.get("role") or "any"),
                min_count=int(row.get("minCount") or 0),
                max_count=row.get("maxCount"),
                ownership_threshold=row.get("ownershipThreshold"),
                fields=person_fields + _person_address(root_fields, person_fields),
                documents=_documents(row.get("documents")),
            )
        )
    return FormModel(
        fields=root_fields,
        persons=persons,
        documents=_documents(payload.get("documents")),
        min_documents=int(payload.get("minDocuments") or 0),
        dialect="requirements",
        schema_version=version,
        context=payload.get("context"),
        country=payload.get("country"),
        warnings=warnings,
    )


def _parse_payout(payload: Mapping, warnings: list[str]) -> FormModel:
    return FormModel(
        fields=[_field_b(f, warnings) for f in payload.get("fields") or []],
        dialect="payout",
        whitelist=dict(payload.get("whitelist") or {}),
        documentation=dict(payload.get("documentation") or {}),
        blocked_jurisdictions=list(payload.get("blockedJurisdictions") or []),
        rail=payload.get("rail"),
        warnings=warnings,
    )


# --- HTML names -------------------------------------------------------------------

# `f.businessInfo.taxId` / `p.2.f.firstName` — the one regex (spec §2).
NAME_RE = re.compile(r"^(?:p\.(\d+)\.)?f\.(.+)$")


def field_name(path: tuple[str, ...], person_index: int | None = None) -> str:
    prefix = "" if person_index is None else f"p.{person_index}."
    return f"{prefix}f." + ".".join(path)


def parse_name(name: str) -> tuple[int | None, tuple[str, ...]] | None:
    match = NAME_RE.match(name)
    if not match:
        return None
    index, dotted = match.groups()
    return (int(index) if index is not None else None, tuple(dotted.split(".")))


# --- coercion ---------------------------------------------------------------------


def coerce(field: Field, raw: Sequence[str] | str | None) -> Any:
    """Submitted strings -> the typed value the server body carries.

    Empty means absent: the omission rule (§5.3) is enforced here so no later
    stage has to remember to strip `""` and `[]`.
    """
    raws = [raw] if isinstance(raw, str) else list(raw or [])
    ftype = field.effective_type

    if ftype in LIST_TYPES:
        if ftype == "stringArray" and len(raws) == 1:
            raws = raws[0].splitlines()
        items = [v.strip() for v in raws]
        items = [v for v in items if v]
        return items or ABSENT

    text = raws[0].strip() if raws else ""
    if ftype == "boolean":
        if text in ("true", "on", "yes", "1"):
            return True
        if text in ("false", "no", "0"):
            return False
        return ABSENT
    if text == "":
        return ABSENT
    if ftype == "number":
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return Invalid(text, "must be a number")
        # `Decimal` happily parses "NaN", "Infinity" and "1e10000". None of them
        # survive `json.dumps` as valid JSON (Python emits bare `NaN`/`Infinity`
        # literals), and a comparison against NaN is false in both directions —
        # so a constraint check on one silently passes. Refuse at the boundary.
        if not number.is_finite():
            return Invalid(text, "must be a finite number")
        # `Decimal` calls "1e10000" finite too — it is arbitrary precision —
        # but a `float` cannot hold it; that overflow is the same "must be a
        # finite number" refusal as NaN/Infinity, checked before anything else.
        approx = float(number)
        if not math.isfinite(approx):
            return Invalid(text, "must be a finite number")
        # No blanket `float()` beyond that: a discovery-declared `number` is
        # never money in this app (verified against every
        # `amount`-shaped field in contracts/openapi_production.json, all
        # `type: string`), but `float` is still lossy past its ~15-17
        # significant digits. An integral value survives exactly as `int`; a
        # fractional value survives only if the float round-trips back to the
        # same Decimal, otherwise the submission is refused rather than
        # silently truncated.
        if number == number.to_integral_value():
            return int(number)
        if Decimal(repr(approx)) != number:
            return Invalid(text, "carries more precision than this field accepts")
        return approx
    if ftype == "integer":
        try:
            number = int(text)
        except ValueError:
            return Invalid(text, "must be a whole number")
        if abs(number) > JS_SAFE_INTEGER:
            # Python integers are unbounded; the browser's evaluator holds the
            # same value as a double and rounds past 2^53−1, so the two sides
            # would disagree about a condition on it (and about what was
            # submitted). Neither is wrong — the value is simply out of range.
            return Invalid(text, f"must be between -{JS_SAFE_INTEGER} and {JS_SAFE_INTEGER}")
        return number
    # string / date / email / url / phone / country / enum: verbatim, stripped.
    return text


def _set_path(target: dict, path: tuple[str, ...], value: Any) -> None:
    node = target
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def parse_submission(model: FormModel, items: Iterable[tuple[str, str]]) -> FormValues:
    """Form-encoded (name, value) pairs -> FormValues. Names the model does not
    know are dropped (the top-level DTO is `additionalProperties: false`)."""
    raw: dict[str, list[str]] = {}
    for name, value in items:
        raw.setdefault(name, []).append(value)

    values = FormValues(
        document_ids=[d for d in raw.get("documentIds", []) if d.strip()],
        client_reference_id=(raw.get("clientReferenceId") or [None])[0],
    )
    for field in model.fields:
        coerced = coerce(field, raw.get(field_name(field.path)))
        if coerced is not ABSENT:
            _set_path(values.root, field.path, coerced)

    person_prefix = re.compile(r"^p\.(\d+)\.")
    indexes = sorted({int(m.group(1)) for m in map(person_prefix.match, raw) if m})
    for index in indexes:
        role = (raw.get(f"p.{index}.role") or [""])[0]
        row = next((p for p in model.persons if p.role == role), None) or (
            model.persons[0] if model.persons else PersonRequirement(role=role)
        )
        person = PersonValues(
            role=role or row.role,
            document_ids=[d for d in raw.get(f"p.{index}.documentIds", []) if d.strip()],
        )
        for field in row.fields:
            coerced = coerce(field, raw.get(field_name(field.path, index)))
            if coerced is not ABSENT:
                _set_path(person.values, field.path, coerced)
        values.persons.append(person)
    return values


# --- conditions (authoritative; mirrored by static/conditions.js) ------------------


def lookup(path: tuple[str, ...], values: Mapping | None) -> Any:
    node: Any = values or {}
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return ABSENT
        node = node[key]
    return node


def present(value: Any) -> bool:
    """Missing ≡ absent key, "", [] or None (spec §4). `false` and `0` are present."""
    return not (value is ABSENT or value is None or value == "" or value == [])


def same(a: Any, b: Any) -> bool:
    """Equality the browser agrees with.

    Python says `True == 1` and `False == 0`; JavaScript's `===` does not, so a
    condition `eq: 1` against a checked boolean would activate a field on the
    server and leave it hidden in the browser. A bool only ever equals a bool
    here. (`1 == 1.0` needs no guard — JS holds both as the same double.)
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def evaluate_condition(
    condition: Condition, root: Mapping | None, person: Mapping | None = None
) -> bool:
    scope = person if condition.scope == "person" else root
    value = lookup(condition.path, scope)
    has = present(value)
    operator = condition.operator

    if operator == "exists":
        return has
    if operator == "is_true":
        return value is True
    if operator == "is_false":
        return value is False
    if operator == "eq":
        return has and same(value, condition.value)
    if operator in ("in", "not_in"):
        candidates = condition.values or []
        # An enumArray gate holds a list: membership is intersection, not
        # equality (fixture: regulatedOrRestrictedActivities `in` [...]).
        hit = (
            any(any(same(v, c) for c in candidates) for v in value)
            if isinstance(value, list)
            else any(same(value, c) for c in candidates)
        )
        # `not_in` on an absent value is false: an unanswered gate never
        # activates its dependents.
        return has and (hit if operator == "in" else not hit)
    log.warning("forms: unknown condition operator %r -> inactive", operator)
    return False


def evaluate_field(
    conditions: Sequence[Condition],
    required_when: Condition | None,
    required: bool,
    root: Mapping | None,
    person: Mapping | None = None,
) -> dict:
    """The shared parity contract: `{active, required}` for one field."""
    return {
        "active": all(evaluate_condition(c, root, person) for c in conditions),
        "required": bool(required)
        or (required_when is not None and evaluate_condition(required_when, root, person)),
    }


def base_required(field: Field, person_index: int | None = None) -> bool:
    """`field.required`, read for one card (spec §7). `None not in ()`, so a root
    field — which is never read for a card — is unaffected."""
    return field.required or person_index in field.required_indices


def field_state(
    field: Field,
    root: Mapping | None,
    person: Mapping | None = None,
    person_index: int | None = None,
) -> dict:
    return evaluate_field(
        field.conditions,
        field.required_when,
        base_required(field, person_index),
        root,
        person,
    )


def active_fields(model: FormModel, values: FormValues) -> list[Field]:
    """Root fields whose conditions all hold. Server-side is authoritative:
    anything filtered out here is neither validated nor assembled."""
    return [f for f in model.fields if field_state(f, values.root)["active"]]


def active_person_fields(
    row: PersonRequirement, person: PersonValues, values: FormValues
) -> list[Field]:
    return [f for f in row.fields if field_state(f, values.root, person.values)["active"]]


def _row_for(model: FormModel, person: PersonValues) -> PersonRequirement:
    return next(
        (p for p in model.persons if p.role == person.role),
        model.persons[0] if model.persons else PersonRequirement(role=person.role),
    )


# --- assembly ---------------------------------------------------------------------


def _assemble_fields(fields: Sequence[Field], values: Mapping) -> dict:
    body: dict = {}
    for field in fields:
        value = lookup(field.path, values)
        if value is ABSENT or isinstance(value, Invalid) or not present(value):
            continue  # omission rule: never emit null or ""
        _set_path(body, field.path, value)
    return body


# `individualRequirements[].role` is a *requirement selector*, not always a role:
# the catch-all row ships `role: "any"`, and Conduit rejects `"any"` as a value
# in `ownership.persons[].roles[]` (verified live 2026-08-28 — 400
# VALIDATION_ERROR, "Invalid role \"any\". Allowed: BENEFICIAL_OWNER, …").
ROLE_WILDCARD = "any"


def satisfies(row: PersonRequirement, person: PersonValues, model: FormModel) -> bool:
    """Whether one person counts towards one `individualRequirements` row.

    The answer is the person's own coerced `/roles`, not which card they were
    typed into: the catch-all `any` row is satisfied by anybody, and a
    BENEFICIAL_OWNER entered under that row still is one. Counting card
    membership instead made a correctly-filled form fail its own minCount —
    and, worse, let a form with nobody in a required role pass.
    """
    if row.role == ROLE_WILDCARD:
        return True
    answered = lookup(("roles",), person.values)
    if isinstance(answered, list) and answered:
        return row.role in answered
    return (person.role or _row_for(model, person).role) == row.role


def person_roles(row: PersonRequirement, person: PersonValues, entry: Mapping) -> list[str]:
    """What goes in `ownership.persons[i].roles[]`.

    The person's own answer wins — discovery ships a required `/roles` enumArray
    whose `allowedValues` are the real vocabulary. The requirement row's `role`
    is only a fallback, and only when it names an actual role.
    """
    answered = entry.get("roles")
    if isinstance(answered, list) and answered:
        return list(answered)
    declared = person.role or row.role
    return [declared] if declared and declared != ROLE_WILDCARD else []


def assemble(model: FormModel, values: FormValues) -> dict:
    """The request body: active fields only, nested by path, empties omitted."""
    body = _assemble_fields(active_fields(model, values), values.root)

    if model.persons and values.persons:
        people = []
        for person in values.persons:
            row = _row_for(model, person)
            entry = _assemble_fields(active_person_fields(row, person, values), person.values)
            roles = person_roles(row, person, entry)
            if roles:
                entry["roles"] = roles
            if person.document_ids:
                entry["documentIds"] = person.document_ids
            people.append(entry)
        body.setdefault("ownership", {})["persons"] = people

    if values.document_ids:
        body["documentIds"] = values.document_ids[:100]
    if values.client_reference_id:
        body["clientReferenceId"] = values.client_reference_id
    return body


def _document_ids(node: Any) -> list[str]:
    """Every `documentIds` string under `node`, in walk order."""
    if isinstance(node, Mapping):
        out: list[str] = []
        for key, value in node.items():
            if key == "documentIds" and isinstance(value, (list, tuple)):
                out += [d for d in value if isinstance(d, str)]
            else:
                out += _document_ids(value)
        return out
    if isinstance(node, (list, tuple)):
        return [d for value in node for d in _document_ids(value)]
    return []


def attached_document_ids(body: Mapping) -> list[str]:
    """Every `doc_` id an assembled body carries — the root list and each
    person's — in order, without repeats.

    Read back off the **assembled body** rather than off `FormValues`, because
    what an attachment check has to cover is exactly what goes on the wire and
    nothing else. `assemble` truncates the root list at the DTO's `maxItems` and
    drops a person the model no longer has a row for; a check that walked the
    values instead would refuse ids that were never going to be sent, and — the
    direction that actually matters — would silently stop covering any id a
    future change to `assemble` starts sending from somewhere new.

    Order is submission order so the count in a refusal message matches what the
    operator can see on the form, and repeats collapse so one id pasted into two
    person cards is one refusal rather than two.

    The root list is read first and the rest of the body is then walked for the
    key wherever else it appears, so the sentence above is true of a third
    location as well as of the two today's DTO has (`OPERATIONS_SPEC` §3). Naming
    `ownership.persons` here instead would have made it a promise about the shape
    of the body rather than about the key.
    """
    found: list[str] = [d for d in body.get("documentIds") or [] if isinstance(d, str)]
    found += _document_ids({k: v for k, v in body.items() if k != "documentIds"})
    return list(dict.fromkeys(found))


# --- validation -------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    detail: str
    allowed_values: list[str] = dc_field(default_factory=list)


@dataclass
class FormErrors:
    """Errors keyed by HTML field name, so root and person-scoped errors land in
    the same place the template already names its inputs."""

    fields: dict[str, list[Message]] = dc_field(default_factory=dict)
    documents: list[Message] = dc_field(default_factory=list)
    form: list[Message] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.fields or self.documents or self.form)

    def add(self, name: str, detail: str, allowed_values: Sequence[str] | None = None) -> None:
        self.fields.setdefault(name, []).append(Message(detail, list(allowed_values or [])))

    def for_field(self, path: tuple[str, ...], person_index: int | None = None) -> list[Message]:
        return self.fields.get(field_name(path, person_index), [])


CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
US_STATE = re.compile(r"^US-[A-Z]{2}$")
US_ZIP = re.compile(r"^\d{5}(-\d{4})?$")
US_COUNTRIES = ("USA", "US")


def aba_valid(value: str) -> bool:
    """9 digits + the ABA/routing checksum."""
    digits = value.strip()
    if not re.fullmatch(r"\d{9}", digits):
        return False
    d = [int(c) for c in digits]
    total = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return total % 10 == 0


def iban_valid(value: str) -> bool:
    """ISO 13616 mod-97."""
    iban = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return False
    rotated = iban[4:] + iban[:4]
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rotated)
    return int(digits) % 97 == 1


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _bound(value: Any, ftype: str) -> float | None:
    """What `min`/`max` compare: item count for arrays, the number itself for
    numerics, string length otherwise."""
    if isinstance(value, list):
        return len(value)
    if ftype in NUMERIC_TYPES:
        return value if isinstance(value, (int, float)) else None
    return len(value) if isinstance(value, str) else None


def _check_field(
    field: Field, value: Any, required: bool, name: str, errors: FormErrors, today: date
) -> None:
    if value is ABSENT or not present(value):
        if required:
            errors.add(name, "This field is required.")
        return
    if isinstance(value, Invalid):
        errors.add(name, value.detail.capitalize() + ".")
        return

    c = field.constraints
    ftype = field.effective_type
    text = value if isinstance(value, str) else None

    # 2. constraints
    if text is not None:
        # JSON Schema `pattern` semantics: unanchored, partial match. The spec
        # says "full match", but the BGR fixture ships `\S` for
        # legalStructureOther, which full-matching would reject for every
        # multi-character answer. Fixture wins; anchored patterns say `^…$`.
        if c.pattern and not re.search(c.pattern, text):
            hint = f" Example: {c.example}" if c.example else ""
            errors.add(name, f"Does not match the required format.{hint}")
        if c.min_length is not None and len(text) < c.min_length:
            errors.add(name, f"Must be at least {c.min_length} characters.")
        if c.max_length is not None and len(text) > c.max_length:
            errors.add(name, f"Must be at most {c.max_length} characters.")
        if c.format == "safeString" and CONTROL_CHARS.search(text):
            errors.add(name, "Must not contain control characters.")
    elif isinstance(value, list) and c.max_length is not None:
        for item in value:
            if isinstance(item, str) and len(item) > c.max_length:
                errors.add(name, f"Each entry must be at most {c.max_length} characters.")
                break

    measured = _bound(value, ftype)
    if measured is not None:
        unit = "selection(s)" if isinstance(value, list) else ""
        if c.min is not None and measured < c.min:
            errors.add(name, f"Must be at least {c.min}{' ' + unit if unit else ''}.".strip())
        if c.max is not None and measured > c.max:
            errors.add(name, f"Must be at most {c.max}{' ' + unit if unit else ''}.".strip())

    if ftype == "date" and text is not None:
        parsed = _parse_date(text)
        if parsed is None:
            errors.add(name, "Must be a date in YYYY-MM-DD form.")
        else:
            for limit, label, bad in (
                (c.min_date, "on or after", lambda p, b: p < b),
                (c.max_date, "on or before", lambda p, b: p > b),
            ):
                if not limit:
                    continue
                bound = today if limit == "today" else _parse_date(limit)
                if bound and bad(parsed, bound):
                    errors.add(name, f"Must be {label} {bound.isoformat()}.")
            if c.min_age_years is not None:
                age = today.year - parsed.year - (
                    (today.month, today.day) < (parsed.month, parsed.day)
                )
                if age < c.min_age_years:
                    errors.add(name, f"Must be at least {c.min_age_years} years ago.")

    # 3. mustEqual
    if field.must_equal is not None and value != field.must_equal:
        errors.add(name, f"Must be {json.dumps(field.must_equal)}.")

    # 4. allowedValues membership
    #
    # The refused value is **masked**, because it is the one thing in this
    # message the operator typed and the message outlives the screen: a batch
    # row keeps its validation sentences in `payout_batch_rows.errors`, which is
    # plaintext JSONB with no retention job, and the results CSV prints the
    # first of them. The commonest way a value gets refused here is a paste one
    # column over — so the value that reaches this line is, precisely when the
    # complaint is loudest, someone's account number.
    #
    # Masked rather than dropped: the tail is what tells the operator *which*
    # fault this is. `••••4567` in an enum column says "a long number landed in
    # a two-value cell — the columns are shifted"; a bare "not an accepted
    # value" leaves them re-reading a 500-row file. The accepted values travel
    # with the message and the field names itself, so nothing that says how to
    # fix the row was in the echo to begin with.
    if field.allowed_values is not None:
        submitted = value if isinstance(value, list) else [value]
        bad = [v for v in submitted if v not in field.allowed_values]
        if bad:
            errors.add(
                name, f"{mask(bad[0])!r} is not an accepted value.", field.allowed_values[:20]
            )

    # 5. dialect-B validators
    if field.validator == "aba" and text is not None and not aba_valid(text):
        errors.add(name, "Not a valid ABA routing number (checksum failed).")
    if field.validator == "iban" and text is not None and not iban_valid(text):
        errors.add(name, "Not a valid IBAN (mod-97 check failed).")


def _us_address(
    fields: Sequence[Field],
    values: Mapping,
    prefix: str,
    errors: FormErrors,
    person_index: int | None = None,
) -> None:
    """One US address block, wherever it lives — a top-level one or a person's."""
    if lookup((prefix, "country"), values) not in US_COUNTRIES:
        return
    state_field = next((f for f in fields if f.path == (prefix, "state")), None)
    state = lookup((prefix, "state"), values)
    if (
        state_field is not None
        and not state_field.allowed_values
        and present(state)
        and isinstance(state, str)
        and not US_STATE.match(state)
    ):
        errors.add(
            field_name((prefix, "state"), person_index),
            "US addresses need an ISO 3166-2 state code, e.g. US-CA.",
        )
    postal = lookup((prefix, "postalCode"), values)
    if present(postal) and isinstance(postal, str) and not US_ZIP.match(postal):
        errors.add(
            field_name((prefix, "postalCode"), person_index),
            "US addresses need a ZIP, e.g. 94105.",
        )


def hardcoded_checks(model: FormModel, values: FormValues, errors: FormErrors) -> None:
    """§6.7 — verified traps the schema does not advertise. Everything
    country-specific in this engine lives in this one function.

    Person addresses get the same treatment as the business ones: a US-resident
    beneficial owner's ZIP is exactly as undeclared-but-required, and the check
    that only ran on the top-level blocks let every person's address through.

    String matching on well-known pointer tails, not a country table.
    If Conduit ever publishes these as constraints, delete the function.
    """
    for prefix in ("registeredAddress", "operatingAddress"):
        _us_address(model.fields, values.root, prefix, errors)
    for index, person in enumerate(values.persons):
        _us_address(_row_for(model, person).fields, person.values, "address", errors, index)


def validate(model: FormModel, values: FormValues, today: date | None = None) -> FormErrors:
    """All seven stages, collect-all (spec §6). Never fails fast."""
    today = today or date.today()
    errors = FormErrors()

    for field in model.fields:
        state = field_state(field, values.root)
        if not state["active"]:
            continue
        _check_field(
            field,
            lookup(field.path, values.root),
            state["required"],
            field_name(field.path),
            errors,
            today,
        )

    for index, person in enumerate(values.persons):
        row = _row_for(model, person)
        for field in row.fields:
            state = field_state(field, values.root, person.values, index)
            if not state["active"]:
                continue
            _check_field(
                field,
                lookup(field.path, person.values),
                state["required"],
                field_name(field.path, index),
                errors,
                today,
            )

    # 6. structural
    for row in model.persons:
        count = sum(1 for p in values.persons if satisfies(row, p, model))
        if count < row.min_count:
            errors.form.append(
                Message(f"At least {row.min_count} {row.role} person(s) required; {count} given.")
            )
        if row.max_count is not None and count > row.max_count:
            errors.form.append(
                Message(f"At most {row.max_count} {row.role} person(s) allowed; {count} given.")
            )
    if len(values.document_ids) < model.min_documents:
        errors.documents.append(
            Message(f"At least {model.min_documents} document(s) required.")
        )
    for doc in model.documents:
        if doc.min_count and len(values.document_ids) < doc.min_count:
            errors.documents.append(
                Message(f"{doc.title}: at least {doc.min_count} document(s) required.")
            )

    hardcoded_checks(model, values, errors)
    return errors


# --- server 422 mapping -----------------------------------------------------------

PERSONS_PREFIX = ("ownership", "persons")


def map_validation_errors(model: FormModel, errors: Sequence[Any]) -> FormErrors:
    """`ValidationError.errors[]` -> display data. Nothing is ever dropped."""
    mapped = FormErrors()
    for e in errors:
        pointer = getattr(e, "pointer", "") or ""
        detail = getattr(e, "detail", "") or ""
        category = getattr(e, "category", None)
        allowed = list(getattr(e, "allowed_values", None) or [])
        path = _pointer_path(pointer)

        if category == "document":
            mapped.documents.append(Message(detail, allowed))
            continue
        if len(path) > 3 and path[:2] == PERSONS_PREFIX and path[2].isdigit():
            index, rest = int(path[2]), path[3:]
            row = model.persons[0] if model.persons else None
            if row and any(f.path == rest for f in row.fields):
                mapped.add(field_name(rest, index), detail, allowed)
                continue
        if model.field_by_path(path) is not None:
            mapped.add(field_name(path), detail, allowed)
            continue
        mapped.form.append(Message(f"{pointer}: {detail}" if pointer else detail, allowed))
    return mapped


# --- learned fields (spec §7) -----------------------------------------------------

LEARNED_KEY = "x-learnedFields"
LEARNED_GROUP = "conduitRequested"
LEARNABLE_SEGMENT = re.compile(r"^\w+$")  # digits too: `persons/0` is a real path
# Console-only: neither key ever goes on the wire.
LEARNED_SCOPE = "x-scope"
LEARNED_INDICES = "x-requiredIndices"


def _learned(
    pointer: str,
    path: tuple[str, ...],
    detail: str,
    allowed: list,
    scope: str,
    indices: Sequence[int] = (),
) -> dict:
    tail = path[-1] if path else ""
    return {
        "pointer": pointer,
        "label": humanize(tail),
        "helpText": detail or None,
        "type": "date" if tail.endswith("Date") else "string",
        "required": not indices,
        "allowedValues": list(allowed) if allowed else None,
        "group": LEARNED_GROUP,
        LEARNED_SCOPE: scope,
        **({LEARNED_INDICES: list(indices)} if indices else {}),
    }


def _row_of_card(
    model: FormModel, persons: Sequence[PersonValues], index: int
) -> PersonRequirement | None:
    """The requirement row whose fields the card at `index` renders, or None when
    no card sits there (spec §7).

    `_row_for`, because that is what `render_model` and `assemble` both use, so
    the question `learn` asks — does this card already show an input for the
    field? — is answered against the inputs the operator can actually see.

    None rather than a fallback row: a pointer naming a card that was not
    submitted has no inputs to have already shown it, and guessing a row there is
    what produced the wrong answer in the first place.
    """
    if not model.persons or not 0 <= index < len(persons):
        return None
    return _row_for(model, persons[index])


def learn(
    model: FormModel, errors: Sequence[Any], persons: Sequence[PersonValues]
) -> list[dict]:
    """Discovery-shaped descriptors for the 422 pointers `model` has no field for:
    exactly the complement of `map_validation_errors` (spec §7).

    `persons` are the submitted cards, in the order `assemble` emitted them, which
    is the order a `/ownership/persons/{i}` pointer counts in. Required, with no
    default: an omitted one resolved every card to `model.persons[0]`, which is
    the row-by-position guess this exists to replace.
    """
    out: dict[tuple[str, str], dict] = {}
    for e in errors:
        pointer = getattr(e, "pointer", "") or ""
        if not pointer or getattr(e, "category", None) == "document":
            continue
        detail = getattr(e, "detail", "") or ""
        allowed = list(getattr(e, "allowed_values", None) or [])
        path = _pointer_path(pointer)
        if not path or not all(LEARNABLE_SEGMENT.match(p) for p in path):
            continue
        if len(path) > 3 and path[:2] == PERSONS_PREFIX and path[2].isdigit():
            index, rest = int(path[2]), path[3:]
            row = _row_of_card(model, persons, index)
            # Already answered for *this* card, natively or by indices (spec §7).
            if row and any(
                f.path == rest and (not f.required_indices or index in f.required_indices)
                for f in row.fields
            ):
                continue
            known = out.get(("person", "/".join(rest)))
            if known is not None:
                known[LEARNED_INDICES] = sorted(set(known[LEARNED_INDICES]) | {index})
                continue
            out[("person", "/".join(rest))] = _learned(
                "/" + "/".join(rest), rest, detail, allowed, "person", (index,)
            )
            continue
        if model.field_by_path(path) is not None:
            continue
        out.setdefault(("root", "/".join(path)), _learned(pointer, path, detail, allowed, "root"))
    return list(out.values())


def forget_person_index(descriptors: Sequence[Mapping], removed: int) -> list[dict]:
    """Rewrite learned descriptors for a person card deleted at `removed`.

    `x-requiredIndices` holds `ownership.persons[i]` ordinals, and deleting a card
    shifts every card above it down one. A descriptor that owed the demand to the
    deleted card owes it to nobody; one that owed it to a card above now owes it
    one position lower. Root-scoped descriptors carry no indices and pass through.

    A descriptor left with no indices is dropped rather than kept: `_learned` reads
    an empty list as "required of everyone", which is the opposite of what an empty
    list means here.
    """
    out: list[dict] = []
    for descriptor in descriptors:
        indices = descriptor.get(LEARNED_INDICES)
        if not indices:
            out.append(dict(descriptor))
            continue
        moved = sorted({i - 1 if i > removed else i for i in indices if i != removed})
        if moved:
            out.append({**descriptor, LEARNED_INDICES: moved})
    return out


def merge_learned(existing: Sequence[Mapping], fresh: Sequence[Mapping]) -> list[dict]:
    """What a draft should store under `LEARNED_KEY` after another rejection:
    `existing` keyed by scope + pointer, indices unioned (spec §7)."""
    out = [dict(d) for d in existing]
    by_key = {(d.get(LEARNED_SCOPE), d.get("pointer")): d for d in out}
    for descriptor in fresh:
        key = (descriptor.get(LEARNED_SCOPE), descriptor.get("pointer"))
        known = by_key.get(key)
        if known is None:
            out.append(dict(descriptor))
            by_key[key] = out[-1]
            continue
        widened = sorted(
            set(known.get(LEARNED_INDICES) or ()) | set(descriptor.get(LEARNED_INDICES) or ())
        )
        if widened:
            known[LEARNED_INDICES] = widened
    return out


def with_learned(model: FormModel, descriptors: Sequence[Mapping]) -> FormModel:
    """`model` plus its learned fields — the whole of what `drafts.model` returns.

    Precondition: `model` must be freshly parsed. Re-merging an already-merged
    model suppresses the re-fan and freezes the indices (spec §7).
    """
    if not descriptors:
        return model
    warnings: list[str] = []
    root = [_field_a(d, warnings, set()) for d in descriptors if d.get(LEARNED_SCOPE) != "person"]
    person = [
        dataclasses.replace(
            _field_a(d, warnings, set()),
            required_indices=tuple(d.get(LEARNED_INDICES) or ()),
        )
        for d in descriptors
        if d.get(LEARNED_SCOPE) == "person"
    ]
    return dataclasses.replace(
        model,
        fields=model.fields + root,
        persons=[
            dataclasses.replace(
                row,
                fields=row.fields
                + [f for f in person if not any(g.path == f.path for g in row.fields)],
            )
            for row in model.persons
        ],
    )


# --- render model (spec §8) -------------------------------------------------------

GROUP_TITLES = {
    "businessInfo": "Business information",
    "registeredAddress": "Registered address",
    "operatingAddress": "Operating address",
    "companyClassification": "Company classification",
    "businessActivity": "Business activity",
    "regulatoryHistory": "Regulatory history",
    "certification": "Certification",
    "conduitRequested": "Also required by Conduit",
    "ownership": "Ownership",
    "asset": "Account",
    # Dialect B presentation grouping by path prefix (spec §1).
    "recipient": "Recipient",
    "bank": "Bank details",
    "remittance": "Remittance",
    "payout": "Payout",
}


def humanize(token: str) -> str:
    """`legalName` -> `Legal name`. Public because the submitted-body view reads it
    the same way, as the `humanize` Jinja filter."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", token).replace("_", " ").capitalize()


def group_title(token: str | None) -> str:
    if not token:
        return "Details"
    return GROUP_TITLES.get(token, humanize(token))


def payout_group(field: Field) -> str:
    """Dialect B has no groups; the route groups by path prefix (spec §1).

    `remittance` is matched on the *segment*, not on the leaf name: discovery
    ships `destination.remittance.{reference,description}` and matching leaves
    put `reference` under Remittance while `description` fell through to the
    catch-all — two halves of one bank field, in two different sections.
    """
    path = field.path
    if "bankAddress" in path:
        return "bank"
    if "remittance" in path or (path and path[-1] in ("reference", "remittanceInformation")):
        return "remittance"
    if "recipient" in path:
        return "recipient"
    return "payout"


def html_attrs(field: Field, required: bool) -> dict:
    """Client-side sugar generated from the same Constraints as the server run."""
    c = field.constraints
    attrs: dict[str, Any] = {}
    if required:
        attrs["required"] = True
    # The HTML attribute is implicitly anchored; an unanchored server pattern
    # would reject in the browser what the server accepts, so it is not emitted
    # (§6: client attrs are sugar, the server run is the gate).
    if c.pattern and c.pattern.startswith("^") and c.pattern.endswith("$"):
        attrs["pattern"] = c.pattern
    if c.max_length is not None and field.effective_type not in LIST_TYPES:
        attrs["maxlength"] = c.max_length
    if c.min_length is not None:
        attrs["minlength"] = c.min_length
    if field.effective_type in NUMERIC_TYPES:
        attrs["step"] = "any" if field.effective_type == "number" else "1"
        if c.min is not None:
            attrs["min"] = c.min
        if c.max is not None:
            attrs["max"] = c.max
    if field.effective_type == "date":
        today = date.today().isoformat()
        if c.min_date:
            attrs["min"] = today if c.min_date == "today" else c.min_date
        if c.max_date:
            attrs["max"] = today if c.max_date == "today" else c.max_date
    if field.effective_type == "phone":
        attrs["placeholder"] = "+1 555 0100"
    return attrs


@dataclass
class RenderField:
    field: Field
    name: str
    widget: str
    required: bool  # as of this render — conditions.js recomputes it on change
    attrs: dict
    value: Any
    errors: list[Message]
    choices: list[tuple[str, str]]
    conditions_json: str
    # Dialect B's `requiredWhen`, for the browser's half of `evaluate_field`.
    # Without these two the client could decide `active` but never `required`,
    # so an optional-but-visible field stayed optional-looking after the answer
    # that made it mandatory (§4). `null` when the field has no requiredWhen.
    required_when_json: str = "null"
    base_required: bool = False


@dataclass
class RenderGroup:
    key: str | None
    title: str
    fields: list[RenderField]


@dataclass
class RenderPersonCard:
    index: int
    role: str
    roles: list[str]
    ownership_threshold: int | None
    min_count: int
    max_count: int | None
    fields: list[RenderField]
    documents: list[DocumentRequirement]
    document_ids: list[str]


@dataclass
class RenderModel:
    groups: list[RenderGroup]
    persons: list[RenderPersonCard]
    documents: list[DocumentRequirement]
    min_documents: int
    document_ids: list[str]
    document_errors: list[Message]
    form_errors: list[Message]
    model: FormModel


def _render_field(
    field: Field,
    values: Mapping,
    root: Mapping,
    errors: FormErrors,
    person_index: int | None,
    person: Mapping | None,
) -> RenderField:
    name = field_name(field.path, person_index)
    required = field_state(field, root, person, person_index)["required"]
    value = lookup(field.path, values)
    choices = [
        (v, field.options[v].label if v in field.options else v)
        for v in (field.allowed_values or [])
    ]
    return RenderField(
        field=field,
        name=name,
        widget=field.widget,
        required=required,
        attrs=html_attrs(field, required),
        value=None if value is ABSENT else value,
        errors=errors.fields.get(name, []),
        choices=choices,
        conditions_json=json.dumps([c.as_json() for c in field.conditions]),
        required_when_json=json.dumps(
            field.required_when.as_json() if field.required_when else None
        ),
        base_required=base_required(field, person_index),
    )


def render_model(
    model: FormModel, values: FormValues | None = None, errors: FormErrors | None = None
) -> RenderModel:
    """Data structures the templates consume. Groups appear in the order their
    first field does; inactive fields are still rendered (hidden by JS) so the
    browser can reveal them without a round trip."""
    values = values or FormValues()
    errors = errors or FormErrors()

    groups: dict[str | None, RenderGroup] = {}
    for field in model.fields:
        key = field.group if model.dialect == "requirements" else payout_group(field)
        if key not in groups:
            groups[key] = RenderGroup(key=key, title=group_title(key), fields=[])
        groups[key].fields.append(
            _render_field(field, values.root, values.root, errors, None, None)
        )

    cards = []
    for index, person in enumerate(values.persons):
        row = _row_for(model, person)
        cards.append(
            RenderPersonCard(
                index=index,
                role=person.role or row.role,
                roles=[p.role for p in model.persons],
                ownership_threshold=row.ownership_threshold,
                min_count=row.min_count,
                max_count=row.max_count,
                fields=[
                    _render_field(
                        f, person.values, values.root, errors, index, person.values
                    )
                    for f in row.fields
                ],
                documents=row.documents,
                document_ids=person.document_ids,
            )
        )

    return RenderModel(
        groups=list(groups.values()),
        persons=cards,
        documents=model.documents,
        min_documents=model.min_documents,
        document_ids=values.document_ids,
        document_errors=errors.documents,
        form_errors=errors.form,
        model=model,
    )
