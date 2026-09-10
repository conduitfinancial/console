# `forms.py` — form engine specification

The contract for Conduit Console's schema-driven form engine (Milestone 1a of `IMPLEMENTATION_PLAN.md`). Ground truth is the captured payloads in `tests/fixtures/` — where this prose and a fixture disagree, the fixture wins and this file gets corrected.

Consumers: onboarding wizard, virtual-account feature request, payout recipient form, customer-update form.

---

## 1. Two input dialects, one FormModel

The engine normalizes both server shapes into a single internal model.

**Dialect A — `ExternalRequirementsResponseDto`** (onboarding + feature requirements; fixtures `onboarding_requirements_*.json`, `feature_requirements_*.json`):
- `schemaVersion` — must be `"3"`. Anything else: raise `SchemaVersionMismatch`; the route renders a hard error banner. Never attempt best-effort rendering.
- `fields[]` with RFC 6901 `pointer` (`/businessInfo/taxId`), `label`, `helpText`, `type`, `required`, `group`, `mustEqual`, `constraints`, `allowedValues`, `options[]`, `conditions[]`.
- `individualRequirements[]`, `documents[]`, `minDocuments`.

**Dialect B — `PayoutRequirementsResponseDto`** (fixtures `payout_requirements_*.json`):
- Flat `fields[]` with dotted `name` (`destination.recipient.routingNumber`) — **not** RFC 6901. Normalize: split on `.` → same path-segment representation as Dialect A pointers.
- Extra attributes: `requiredWhen` (treated as a condition), `validator` (`aba | iban`), inline `minLength/maxLength/pattern/enum`.
- No groups, persons, or documents sections. The route supplies presentation grouping (recipient / bank / remittance) by path prefix.
- **Route metadata, surfaced verbatim on the FormModel (not fields):** `whitelist {required, reason?}`, `documentation {required, reason?, acceptedDocumentTypes[]}`, `blockedJurisdictions`. Payout gating is discovery-driven — routes obey these flags and never hardcode "purpose X needs a document / a whitelisted recipient". Fixtures confirm both polarities: fedwire/goods → `documentation.required: true`; fedwire/intercompany → `whitelist.required: true`, `documentation.required: false`.

## 2. Data model

```python
@dataclass
class Field:
    path: tuple[str, ...]          # ("businessInfo","taxId") — canonical id
    label: str; help: str | None
    type: str                      # one of the 12 types, or dialect-B primitive
    required: bool
    group: str | None
    must_equal: object | None
    constraints: Constraints       # pattern, min/max length, min/max, minDate, maxDate, minAgeYears, format
    allowed_values: list[str] | None
    options: dict[str, OptionMeta] # value -> label/description/abbr (display only)
    conditions: list[Condition]    # AND-ed
    validator: str | None          # dialect B: "aba" | "iban"

@dataclass
class Condition:
    path: tuple[str, ...]; operator: str  # eq|in|not_in|exists|is_true|is_false
    value: object | None; values: list | None
    scope: str                     # "root" | "person"

@dataclass
class PersonRequirement:           # one per individualRequirements[] row
    role: str                      # "BENEFICIAL_OWNER" | "any" | other free strings
    min_count: int; max_count: int | None
    ownership_threshold: int | None
    fields: list[Field]; documents: list[DocumentRequirement]

@dataclass
class FormModel:
    fields: list[Field]
    persons: list[PersonRequirement]
    documents: list[DocumentRequirement]
    min_documents: int             # 0 | 1
    dialect: str                   # "requirements" | "payout"
```

`FormValues` is a plain nested dict mirroring submission shape, plus `persons: list[dict]` and `document_ids: list[str]`. HTML field names encode the path: `f.businessInfo.taxId`, `p.2.f.firstName` (person index 2), parsed back with one regex.

## 3. Widget mapping

All 12 documented types must be handled; the ✓ column shows which the fixtures actually exercise (the rest get the mapping + a unit test, nothing more).

| type | widget | submit coercion | in fixtures |
|---|---|---|---|
| `string` | text input | strip; `""` → absent | ✓ (24×) |
| `number` | `<input type=number step=any>` | `float` via `Decimal`→JSON number; **NaN / Infinity / overflowing magnitudes are `Invalid`** (none is JSON, and every comparison against NaN is false, so a constraint check on one silently passes) | — |
| `integer` | `<input type=number step=1>` | `int`, **bounded to ±(2^53−1)** — beyond `Number.MAX_SAFE_INTEGER` the browser's evaluator rounds and the two sides stop agreeing about the same submitted digits | — |
| `boolean` | checkbox, or yes/no radio when the answer has to be three-state — see below | `true/false`; unchecked optional / unanswered radio → absent | ✓ (15×) |
| `date` | `<input type=date>` | `YYYY-MM-DD` string | ✓ |
| `email` | `<input type=email>` | strip | ✓ |
| `url` | `<input type=url>` | strip | ✓ |
| `phone` | text with `+` placeholder | strip | ✓ |
| `country` | select from `allowedValues` | verbatim key | ✓ (2×) |
| `enum` | select; `options[]` labels when present | **verbatim `allowedValues` key** (`"C-Corporation"`, never a normalized form) | ✓ (8×) |
| `stringArray` | textarea, one entry per line (v1) | list of stripped non-empty lines | — |
| `enumArray` | checkbox group | list of verbatim keys | ✓ (8×) |

Unknown `type` → render as `string`, log a warning, never crash (forward-compat).

### Which booleans get a radio

A checkbox has two submittable states, `true` and absent; a radio has three, `true`, `false` and unanswered. A boolean gets the radio whenever the difference between **"no"** and **"not answered"** is visible to something:

1. **`mustEqual == true` ⇒ always a checkbox.** There is one acceptable answer, so there is nothing a third state could express.
2. **Otherwise, `required` ⇒ radio.** A required question like `hasRegulatedOrRestrictedActivities` must be refusable when unanswered, which a bare checkbox cannot be.
3. **Otherwise, radio if any condition in the model reads this field's path** — its own `conditions` or another field's `requiredWhen`, matched within scope (`root` conditions against root fields, `person` against person fields). `FormModel.__post_init__` computes this and sets `Field.conditioned`; no parser sets it, and it is recomputed for every derived model (`payments.recipient_model` builds one from a filtered field list, where a gate whose dependants were all removed stops being a gate).
4. **Otherwise, checkbox.**

Rule 3 exists because §4 defines absent as false for `is_true` **and** `is_false`. An optional checkbox can therefore never satisfy an `is_false` gate — unchecked coerces to absent, not to `false` — so its dependants are unreachable in *both* evaluators, consistently and invisibly. `operatingAddress.sameAsRegistered` is the live instance: it gates six operating-address fields on `is_false`, and until the rule existed no operator could ever see them. The rule is deliberately narrow — an optional boolean nobody conditions on stays a checkbox, because for it the third state changes nothing.

**Fixture note:** no current field carries `mustEqual` (the terms checkbox is a plain required boolean today) — support the attribute, don't depend on it.

## 4. Condition semantics

Evaluated over **coerced** values (post §3 coercion), independently in two places that must agree:
- `static/app.js`: fields carry `data-conditions='[...]'` (JSON, paths as dotted names), and a field with a `requiredWhen` also carries `data-required-when` + `data-required` (its base flag). **Every form holding such a field is wired, found by those attributes** — the wizard, the payout screen, the transfer screen, the feature request — never by an element id; re-evaluate on any `change` within the form (or person card for `scope:"person"`); hide + `disabled` inactive fields so they never submit, and set `required`/`aria-required` from the recomputed `{active, required}` so an optional-but-visible field becomes mandatory the moment its gate is answered.
- `forms.py::active_fields(model, values)`: the same logic in Python. **Server-side is authoritative**; inactive fields are excluded from validation *and* from the assembled body even if the browser submitted them.

Operator definitions (missing value ≡ absent key, `""`, `[]`, or `None`):

| operator | true iff |
|---|---|
| `eq` | coerced value == `value` |
| `in` | scalar value: ∈ `values`; **array value (enumArray): any overlap** — the fixtures gate on enumArray fields |
| `not_in` | value present **and** (scalar: ∉ `values`; array: no overlap) — absent → false: an unanswered gate never activates dependents |
| `exists` | value present (non-empty) |
| `is_true` / `is_false` | value is exactly `true` / `false` (absent → false for both) |

Equality is **type-strict on booleans**: Python's `True == 1` is not JS's `===`, so `forms.same()` refuses to equate a bool with a number (`1 == 1.0` needs no guard — JS holds both as the same double). Path lookup uses own properties only (`hasOwnProperty`), so a path like `constructor` is absent rather than finding something on `Object.prototype`. Both are in the vector file.

Multiple conditions AND. `scope:"person"` resolves the path within the current person's values; `scope:"root"` against the top-level dict (also from inside a person card). Fixture example (`onboarding_requirements_BGR.json`): `/companyClassification/primaryIndustryOther` requires `primaryIndustry eq "other_industry"`.

Dialect B `requiredWhen` normalizes to a condition on the referenced field; a field whose `requiredWhen` is false is optional-but-visible (not hidden) — that is the semantic difference from Dialect A conditions.

## 5. Assembly

`assemble(model, values) -> dict`:
1. Filter to `active_fields`.
2. Nest by path: `("businessInfo","taxId")` → `{"businessInfo":{"taxId":…}}`. Pure dicts — no list indices appear in Dialect A pointers (persons are handled separately).
3. **Omission rule:** optional-and-empty → key absent entirely. Never emit `null` or `""` (top-level DTO is `additionalProperties:false`; empty strings fail pattern constraints).
4. Persons → `ownership.persons[]`: each person dict assembled the same way from its `PersonRequirement.fields`, plus `roles[]` and its own `documentIds[]`. **`roles[]` comes from the person's own answer** — discovery ships a required `/roles` enumArray whose `allowedValues` are the real vocabulary (`BENEFICIAL_OWNER | CONTROLLING_PERSON | LEGAL_REPRESENTATIVE`). `individualRequirements[].role` is a *requirement selector*, not always a role: the catch-all row ships `role: "any"`, and submitting `roles: ["any"]` is rejected — `400 VALIDATION_ERROR`, `/ownership/persons/0/roles/0`, *"Invalid role \"any\". Allowed: BENEFICIAL_OWNER, …"* (verified live 2026-08-28; corrected from the original "plus `roles: [row.role]`"). The row's role is used only as a fallback when the person answered nothing and the row names an actual role.
5. Attach top-level `documentIds[]` (≤100) and `clientReferenceId`.
6. Dialect B: same nesting from dotted names, producing the `FiatPayoutDto.destination.recipient` subtree; the route merges it into the payout body it owns (amount, purpose, ids are route-level fields, not engine fields).

## 6. Validation order (server-side, pre-submit)

Run all stages, collect everything (no fail-fast), key errors by path (+ person index):

1. **Required** (after condition filtering).
2. **Constraints**: `pattern` (**partial match**, `re.search` — JSON Schema semantics; BGR ships `\S`, and Conduit anchors explicitly with `^…$` where it means full-match. The HTML `pattern=` attribute is implicitly anchored, so emit it only for anchored patterns), length/numeric bounds, `minDate/maxDate` (literal `"today"` = server-local today), `minAgeYears` (date ≤ today − N years), `format: safeString` (reject control chars) / `prose` (length only).
3. **`mustEqual`** mismatch.
4. **`allowedValues` membership** (enum/enumArray/country) — guards against stale catalogs mid-session.
5. **Dialect-B validators**: `aba` = 9 digits + ABA checksum; `iban` = mod-97. Implement both inline (~10 lines each), no dependency.
6. **Structural**: person `minCount/maxCount` per row — counted from each person's **coerced `/roles` answer**, not the card they were typed into (`forms.satisfies`): the catch-all `any` row is satisfied by anybody, and a BENEFICIAL_OWNER entered under it still is one; `min_documents` floor; per-document-row `minCount` when present.
7. **Hardcoded pre-checks** the schema doesn't advertise (verified traps, keep in one clearly-marked function): US `registeredAddress.state` must be ISO 3166-2 `US-XX` *when the country fixture supplies no enum* (USA fixture has a 56-value enum — the check matters for US addresses inside non-US applications); US operating/person addresses require a US ZIP — **applied to `ownership.persons[i].address` as well as the top-level blocks**, errors keyed with the person index.

Client-side HTML attrs (`pattern`, `maxlength`, `min`, `max`, `required`) are generated from the same `Constraints` — UX sugar; the server run is the gate.

## 7. Server 422 mapping

Server errors arrive as **problem-detail** responses: `ValidationErrorDto` extends `ProblemDetailDto` (`type, title, status, detail, resolution, docs, instance, correlationId, timestamp`) with `errors[]`. The Conduit client layer (`app/conduit/`) parses the envelope centrally and surfaces `resolution` + `correlationId` on every error UI; the engine's mapper consumes only `errors[]`. Live fixture: `tests/fixtures/problem_detail_422_no_eligible_provider.json`.

`map_validation_errors(model, errors) -> FormErrors` for `errors[]: {pointer, detail, category, allowedValues}`:
- Exact pointer match → attach `detail` (+ `allowedValues` as hint chips) to that field.
- `/ownership/persons/<i>/…` → person card `i`, remainder matched within the card.
- Document-category errors (`category:"document"`) → the documents section header.
- Anything unmatched → ordered form-level error list at the top (never dropped). A *learnable* unmatched pointer does not stay there: it becomes a field (below), and the mapper then attaches it by the exact-pointer rule.
- The engine treats server errors as display data only — no retry logic, no interpretation.

### Learned fields

Discovery does not always advertise every field Conduit will demand: an organization with `kybRelianceEnabled` is validated against person fields `GET /v2/onboarding/requirements` never lists (verified live 2026-09-07 — CAN, refused for `/birthDate`, `/nationality` and `/taxResidencyCountry`, none of them in any `individualRequirements` row). A form-level message for those names what is wrong and gives the operator nowhere to fix it, so an unmatched pointer becomes an **input** instead.

`learn(model, errors)` returns discovery-shaped descriptors for exactly the pointers `map_validation_errors` would drop to the form-level list; `merge_learned(existing, fresh)` folds them into what the draft already stored; `with_learned(model, descriptors)` merges the result back into the model.
- **Learnable** = a pointer whose every segment matches `^\w+$`. `/individual:any` names a requirement row rather than a field; it, and any error carrying no pointer, stays form-level prose.
- `/ownership/persons/<i>/<rest>` learns `/<rest>` at person scope and is **offered on every person row** — role membership is decided from the answers, not from the card an answer was typed into (§6.6 `satisfies`). Any other pointer is root-scoped at its own pointer. A pointer is not learned when the row it addresses already answers for that card — the field is natively advertised there, or is a learned one whose indices already name the card — and a row that advertises it natively keeps its own, advertised input. That dedup is sound only because `with_learned` is always applied to a *freshly parsed* model (`drafts.model` = `with_learned(parse(snapshot), snapshot["x-learnedFields"])`), so a row's own fields are discovery's alone at that point. Applying it to an already-merged model would suppress the re-fan and freeze the indices; nothing does, and no caller should.
- **Offered everywhere, owed by the people the 422 named.** `x-requiredIndices` carries the `ownership.persons[i]` indices demanded so far, and the field is required on exactly those cards — present and optional on the rest. Card *i* is `ownership.persons[i]` in the submission that was refused and the redraw keeps the draft's card order, so the index still means that person. Requiring it of every card asked the operator to invent answers for people Conduit said nothing about. `Field.required_indices`; read for one card by `base_required`.
- **The demand only ever widens.** Indices accumulate within one rejection, and `merge_learned` unions them **across** rejections: two successive 422s naming persons 0 then 1 leave both cards owing an answer. Dropping the second as a duplicate of a pointer already stored left that card optional, so the operator answered nothing there and resubmitted into the identical refusal. Sorted and unique, so replaying a rejection stores nothing new and two equivalent rejection sequences persist byte-identical JSON.
- Descriptors are Dialect-A shaped, so they parse through the ordinary field reader and then render, validate and assemble down the same path as an advertised field. There is no second kind of field in the engine.
- Fixed by construction: `group: conduitRequested` (§8 title "Also required by Conduit"), `type: date` when the last segment ends in `date` and `string` otherwise, `allowedValues` only when the error carries them, and `required: true` for a root-scoped descriptor — Conduit named it in a refusal, so the console insists on it too. A person-scoped descriptor carries `required: false` and its indices instead. Nothing else is inferred — a 422 carries no type.
- Stored on the draft under `x-learnedFields` by `merge_learned`, additive and beside the pinned discovery keys, because it must outlive the rejection that taught it: the operator fixes the form and resubmits. Not a re-fetch, so §10's pinning rule is unaffected. `x-scope` and `x-requiredIndices` are console-only and never go on the wire.
- Learning happens before the form is re-rendered, so the error that taught a field arrives attached to that field. The form-level list keeps only what no field could answer.

The captured 422 is `tests/fixtures/validation_error_422.json` (2026-08-28): an `ONBOARDING_NOT_READY` refusal whose one `errors[]` row carries the non-RFC-6901 pointer `/individual:any`, which is what pins the unmatched-and-unlearnable path in `test_forms.py`. The mapper's other cases are hand-built error dicts of the shape above.

## 8. Rendering contract

- Sections ordered by first appearance of `group` in `fields[]` (fixture order: businessInfo → registeredAddress → operatingAddress → companyClassification → businessActivity → regulatoryHistory → certification — order by first appearance, never by this prose). Group tokens map to display titles in one template dict; unknown group → title-cased token.
- Field: label + required marker, widget, `helpText` as muted text, error slot beneath.
- Person cards: one bordered card per person, role picker limited to the `individualRequirements` rows, add/remove buttons respecting min/max (htmx swaps), `ownershipThreshold` shown in the BENEFICIAL_OWNER card header when non-null (fixtures: currently null — render conditionally).
- Documents: checklist rows (`canonicalType` + title + guidance); rows with `alternatives[]` render "any one of: …"; same `groupId` rows share a bracket "satisfy any of these". **Fixture note:** current countries have 3 flat rows, no alternatives/groupId/addressTarget — render support is a few template branches, no logic beyond the floor check.
- Upload widget (shared): file input → `POST /v2/documents` (multipart; `purpose` supplied by the route) via htmx → chip with filename + `doc_…` id + remove; hidden inputs feed `documentIds[]`. Client-side ≤10MB check; server errors shown on the chip slot.
- Idempotency key + `clientReferenceId` are hidden fields owned by the route (per IMPLEMENTATION_PLAN §0), not the engine.

## 9. Test plan (`tests/test_forms.py` — pure logic, no HTTP)

Parametrized over the four onboarding fixtures + two feature fixtures (USD; EUR-availability is runtime — see fixtures README on `NO_ELIGIBLE_PROVIDER`) + five payout fixtures (incl. `payout_requirements_sepa_business.json`):

1. Every fixture parses to a FormModel with zero unknown-type warnings; all 61/5/26–28 fields present; pointer↔name round-trip.
2. Coercion per type (table §3), including empty-optional omission and verbatim enum keys.
3. Condition operators — each of the 6, absent-value behavior, AND of multiple, person scope. **Parity gate in CI**: one language-neutral vector file (`tests/condition_vectors.json`) executed by both the Python evaluator (pytest) and the JS evaluator (node); any disagreement fails the build. Python stays authoritative at runtime.
4. Assembly: nested shape for a fully-valid BGR submission snapshot; persons array with roles + documentIds; Dialect B recipient subtree for fedwire-business.
5. Validation: each constraint kind, ABA checksum vectors (valid/invalid), IBAN mod-97 vectors, person minCount, minDocuments floor.
6. 422 mapping: field hit, person-scoped hit, unmatched unlearnable pointer → form-level; unmatched learnable pointer → a learned field (root and person scope) carrying the error that taught it, required of the cards the rejection named and no others.
7. Drift canary: `schemaVersion == "3"` asserted for every requirements fixture — recapture + review when this fails.

## 10. Non-goals (v1)

- No dynamic re-fetch of requirements mid-form (a form session pins its fetched payload; a learned field, §7, is added beside that payload and is not a re-fetch).
- No cross-field validation beyond conditions + §6.7 (server owns the rest).
- No file-type sniffing beyond extension/size (Conduit validates magic bytes).
- No localization of labels (server strings are already display-ready).
