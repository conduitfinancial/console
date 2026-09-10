"""Whitelist recipients, external fiat payouts, and the transactions ledger
(plan v2 §7 Payments).

Route-free like `app/accounts/`: the pages in `app/web/{recipients,payouts,
transactions}.py` render what this module shapes, and `tests/e2e/04_payments.py`
calls the same functions, so what the live run proves is what an operator sees.

Three facts drive everything here.

* **The payout form is discovery's, not ours.** `GET /v2/payouts/requirements`
  answers with fields *and* route metadata — `whitelist.required`,
  `documentation.required` + `acceptedDocumentTypes`, `blockedJurisdictions` —
  and the route obeys all three rather than hardcoding "purpose X needs a
  document" (FORM_ENGINE_SPEC §1). The fixtures already show both polarities:
  fedwire/goods asks for a document, fedwire/intercompany asks for a whitelisted
  recipient instead.

* **The whitelist DTOs are static, so they are written down here** — as Dialect B
  payloads rather than as hand-rolled HTML. `POST /customers/{id}/whitelist-
  recipients` takes a `oneOf` discriminated by `rail` with no discovery endpoint
  in front of it, and re-describing three fixed shapes in the same dialect the
  engine already parses buys the ABA/IBAN validators, the required-field pass,
  the 422 mapper and the field macro for free.

* **Jurisdiction codes are Conduit's to judge.** `blockedJurisdictions` ships
  ISO-3; a form that answers ISO-2 slips past the local pre-check and is caught
  server-side. That is deliberate — there is no local country table here, and
  the server is the gate (plan v2 ground rules).

* **One payout endpoint, two destination arms.** `POST /v2/payouts` takes either
  a *bank* destination (rail + recipient, discovery-shaped, the payout page) or a
  *virtual-account* one (`{type:"virtual_account", virtualAccountId, remittance?}`
  — no rail, no recipient, the transfers screen). They have separate builders on
  purpose (`payout_body` / `virtual_account_body`); a merged one could emit both
  halves of a `oneOf` at once.

* **Money is a decimal string, end to end.** `assetAmount.amount` is a string in
  the spec and stays one here: `amount()` refuses anything that is not a plain
  positive decimal and hands back its **canonical** form, which is the string
  that is transmitted and the string the operator is shown. No float ever
  touches a payout body — `tests/test_payments.py` greps for it.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from app import counterparties, forms
# `humanize` (label an unlabelled key) and `amount_of` (what moved, from whichever
# side says so) already exist for the account pages and do the same job here.
# Imported, not re-derived; re-exported so the templates can reach them.
from app.accounts import amount_of as amount_of
from app.accounts import humanize as humanize
from app.conduit.client import Page, Result, Success
from app.config import get_settings

# --- payout vocabulary ----------------------------------------------------------------

# `FiatPayoutDto.purpose`, verbatim from the pinned spec, each with the sentence
# an operator needs to pick correctly. The purpose drives Conduit's own
# documentation and whitelist gating, so choosing it wrongly is not cosmetic.
PURPOSES: tuple[tuple[str, str], ...] = (
    ("payment_for_goods_or_services", "Paying a supplier or a vendor invoice."),
    ("payroll", "Paying salaries, contractors or benefits."),
    ("treasury_management", "Moving the customer's own money for cash management."),
    ("intercompany", "Between entities of the same group — needs a whitelisted recipient."),
    ("investments", "Funding an investment or a capital contribution."),
    (
        "prefunding",
        "Pre-funding a Conduit house account — house accounts only; Conduit will refuse "
        "otherwise. Rarely the right answer.",
    ),
    ("other", "Anything the six above do not describe. Expect a document request."),
)
PURPOSE_VALUES = tuple(value for value, _ in PURPOSES)
PURPOSE_HELP = dict(PURPOSES)


def purpose_label(purpose: str) -> str:
    """`payroll` → "Payroll" — the same words `web.countries.enum_label` puts on
    the dropdown, from the same transformation (`accounts.humanize`, re-exported
    above). Here rather than there because the batch template's header block is
    written by a route-free module and must not reach into `app/web/`; the label
    is display-only in both places and the raw key is what is ever submitted.
    """
    return humanize(purpose) if purpose else ""

# The purpose the transfers screen owns: an intercompany payout is how
# a same-currency A→B transfer is made, so it is started there, not here.
TRANSFER_PURPOSE = "intercompany"

# `PayoutRequirementsResponseDto.rail` minus `crypto` (out of v1 scope, plan v2
# §0). Lowercase — the *only* place this API spells rails in caps is the sandbox
# deposit simulator.
RAILS: tuple[str, ...] = (
    "fedwire",
    "rtp",
    "fednow",
    "ach",
    "swift",
    "sepa",
    "faster_payments",
    "chaps",
)
RECIPIENT_TYPES: tuple[str, ...] = ("business", "individual")

REQUIREMENTS_PATH = "/v2/payouts/requirements"
PAYOUT_PATH = "/v2/payouts"
QUOTE_PATH = "/v2/quotes"
TRANSACTIONS_PATH = "/v2/transactions"
DOCUMENT_PURPOSE = "transaction_support"  # DocumentUploadDto.purpose for payout evidence

# `virtualAccountId` is declared by discovery but owned by the route: which
# account funds the payout is a picker over the customer's own accounts, not a
# free-text box. Dropped from the model, added back in `payout_body`.
ROUTE_OWNED_FIELDS = {("virtualAccountId",)}

MAX_DOCUMENTS = 10  # FiatPayoutDto.documents: "Maximum 10."


# --- whitelist vocabulary -------------------------------------------------------------

WHITELIST_PATH = "/v2/customers/{customer_id}/whitelist-recipients"
WHITELIST_RAILS: tuple[str, ...] = ("us", "swift", "sepa", "uk_domestic")
RELATIONSHIPS: tuple[str, ...] = ("self", "group_entity")
EVIDENCE_PURPOSE = "feature_request"  # per the slice brief; DocumentUploadDto.purpose

_RELATIONSHIP = {
    "name": "relationship",
    "label": "Relationship",
    "type": "enum",
    "required": True,
    "enum": list(RELATIONSHIPS),
    "helpText": (
        "self — an account the customer owns elsewhere. "
        "group_entity — an account belonging to another entity in the same group."
    ),
}
_LEGAL_NAME = {
    "name": "legalName",
    "label": "Legal name of the account holder",
    "type": "string",
    "required": True,
    "maxLength": 140,
}
_LABEL = {
    "name": "label",
    "label": "Label",
    "type": "string",
    "required": False,
    "maxLength": 140,
    "helpText": "Optional, for your own reference.",
}

# `UsWhitelistRecipientDto` / `SwiftWhitelistRecipientDto` /
# `SepaWhitelistRecipientDto`, transcribed into Dialect B. `rail` itself is not a
# field: the variant picker *is* the rail, and `whitelist_body` writes it.
WHITELIST_VARIANTS: dict[str, dict] = {
    "us": {
        "rail": "us",
        "fields": [
            {
                "name": "routingNumber",
                "label": "ABA routing number",
                "type": "string",
                "required": True,
                "minLength": 9,
                "maxLength": 9,
                "pattern": "^[0-9]{9}$",
                "validator": "aba",
            },
            {
                "name": "accountNumber",
                "label": "Account number",
                "type": "string",
                "required": True,
                "minLength": 4,
                "maxLength": 64,
            },
            _RELATIONSHIP,
            _LEGAL_NAME,
            _LABEL,
        ],
    },
    "swift": {
        "rail": "swift",
        "fields": [
            {
                "name": "bic",
                "label": "BIC of the recipient bank",
                "type": "string",
                "required": True,
                "minLength": 8,
                "maxLength": 11,
            },
            {
                "name": "iban",
                "label": "IBAN",
                "type": "string",
                "required": False,
                "maxLength": 34,
                "validator": "iban",
                "helpText": "Required unless an account number is given.",
            },
            {
                "name": "accountNumber",
                "label": "Account number",
                "type": "string",
                "required": False,
                "maxLength": 64,
                "helpText": "For banks in countries without IBAN. Required unless an IBAN is given.",
            },
            _RELATIONSHIP,
            _LEGAL_NAME,
            _LABEL,
        ],
    },
    "sepa": {
        "rail": "sepa",
        "fields": [
            {
                "name": "iban",
                "label": "IBAN",
                "type": "string",
                "required": True,
                "maxLength": 34,
                "validator": "iban",
            },
            _RELATIONSHIP,
            _LEGAL_NAME,
            _LABEL,
        ],
    },
    "uk_domestic": {
        "rail": "uk_domestic",
        "fields": [
            {
                "name": "sortCode",
                "label": "UK sort code",
                "type": "string",
                "required": True,
                "minLength": 6,
                "maxLength": 8,
                # 6 digits, optionally dash-separated in pairs (`12-34-56`):
                # Conduit strips the separators server-side, so this shape is
                # what it accepts, not the canonical one.
                "pattern": "^[0-9]{2}-?[0-9]{2}-?[0-9]{2}$",
            },
            {
                "name": "accountNumber",
                "label": "UK bank account number",
                "type": "string",
                "required": True,
                "minLength": 8,
                "maxLength": 8,
                "pattern": "^[0-9]{8}$",
            },
            _RELATIONSHIP,
            _LEGAL_NAME,
            _LABEL,
        ],
    },
}

# The one shape rule the engine cannot express: SWIFT is registered by BIC plus
# *either* an IBAN or an account number, and `oneOf` is not a field constraint.
SWIFT_EITHER = ("iban", "accountNumber")

WHITELIST_STATUSES: tuple[str, ...] = (
    "pending_review",
    "registered",
    "suspended",
    "revoked",
    "rejected",
)
USABLE_STATUS = "registered"  # the only status a payout may name


def whitelist_model(rail: str) -> forms.FormModel:
    """The static DTO for one rail, as a FormModel. Raises KeyError on a rail
    this console does not register (the picker only offers three)."""
    return forms.parse(WHITELIST_VARIANTS[rail])


# **Verified live 2026-08-28**: `evidenceDocumentIds` is
# not merely `required` — it must be non-empty. A `self` registration with `[]`
# answers `400 VALIDATION_ERROR`, `/evidenceDocumentIds`, *"Too small: expected
# array to have >=1 items"*. The spec's own prose ("evidencing the intercompany
# relationship") reads as though `self` would be exempt; it is not.
EVIDENCE_MIN = 1


def whitelist_body(rail: str, values: forms.FormValues, model: forms.FormModel) -> dict:
    """`UsWhitelistRecipientDto` & co. — flat, `rail` from the picker."""
    body = forms.assemble(model, values)
    body.pop("documentIds", None)
    body.pop("clientReferenceId", None)
    return {"rail": rail, **body, "evidenceDocumentIds": list(values.document_ids)}


def whitelist_errors(rail: str, values: forms.FormValues, errors: forms.FormErrors) -> None:
    """The two shape rules the engine cannot express, in one clearly-marked
    place (FORM_ENGINE_SPEC §6.7's spirit): SWIFT's `oneOf` coordinates, and the
    evidence floor Conduit enforces but the DTO does not declare."""
    if len(values.document_ids) < EVIDENCE_MIN:
        errors.documents.append(
            forms.Message(
                f"Conduit requires at least {EVIDENCE_MIN} evidence document on every "
                "whitelist registration, including a `self` one."
            )
        )
    if rail != "swift":
        return
    if not any(forms.present(forms.lookup((key,), values.root)) for key in SWIFT_EITHER):
        errors.form.append(
            forms.Message("A SWIFT recipient needs an IBAN or an account number.")
        )


# Which payout rails can carry a destination registered on a given whitelist
# rail. Structural, not preference: an ABA-addressed account cannot be reached
# over SEPA, and an IBAN-only account cannot be reached over Fedwire. Lives here
# because both the payout form and the transfers screen have to obey it — one
# table, or the two screens disagree about what is payable.
RAILS_FOR: dict[str, tuple[str, ...]] = {
    "us": ("fedwire", "ach", "rtp", "fednow"),
    "sepa": ("sepa",),
    "swift": ("swift",),
    "uk_domestic": ("faster_payments", "chaps"),
}


# The same table read the other way: which family a payout rail belongs to. The
# console's own counterparties are stored by *family*, not by
# rail, because one us-family destination is payable over four of them and a
# picker keyed to `fedwire` would hide it from an `ach` payout of the same money.
FAMILY_OF: dict[str, str] = {
    rail: family for family, rails in RAILS_FOR.items() for rail in rails
}


def family_of(rail: str) -> str:
    """`fedwire` → `us`. A rail this build has never seen has no family, and a
    counterparty with no family is offered to nothing — the same refusal-to-guess
    `rails_for` makes in the other direction."""
    return FAMILY_OF.get((rail or "").lower(), "")


# The one currency inference in this console, and it is a definition rather than
# a table of prices: SEPA settles in euro, and a US ABA routing number addresses
# a US dollar account. A `swift` entry pins nothing — Conduit picks the corridor.
# Keyed by rail **family** (`family_of`), so `fedwire`, `ach`, `rtp` and `fednow`
# all inherit the one fact that makes them a US-dollar system.
#
# Lived in `web/transfers.py` until item 11; it is here because three surfaces
# ask the same question — the payout form, the transfer screen and a batch's
# funding account — and a second copy of this table is a second answer.
RAIL_ASSET: dict[str, str] = {"us": "USD", "sepa": "EUR", "uk_domestic": "GBP"}


def rail_asset(rail: str) -> str:
    """The currency this rail settles in, or `""` when it settles in any.

    `""` for a rail this build has never seen, for the same reason `family_of`
    returns `""`: the console does not guess a currency for a corridor it does
    not know, and a guess here would refuse a payment Conduit would have made.
    """
    return RAIL_ASSET.get(family_of(rail), "")


# The sentence, in one place, because three screens say it. Grounded in a live
# sandbox run (`tests/e2e/09_rail_asset_probe.py`), not in reasoning about what
# fedwire is: Conduit **accepts** a EUR-funded fedwire payout with a 202 and it
# then **fails after review** with `rail_unavailable` — "No viable payment rail
# was available for the requested corridor. No funds were moved."
RAIL_ASSET_MESSAGE = (
    "{rail} sends {pinned} — Conduit accepts a {asset}-funded {rail} but it fails after "
    "review (rail_unavailable); no funds move. Fund it from a {pinned} account, or convert "
    "first."
)


def doomed_rail(rail: str, asset: str) -> str:
    """The currency `rail` settles in, when funding it with `asset` is the
    combination Conduit accepts and then fails. `""` when there is nothing to
    refuse.

    Both sides have to be known: an unknown rail pins nothing, and an account
    whose asset this console could not read is not evidence of a mismatch.
    """
    pinned = rail_asset(rail)
    return pinned if pinned and asset and pinned != asset else ""


# --- the Convert hand-off -------------------------------------------------
#
# `doomed_rail` above refuses a funding pick; the refusal is also a path — convert
# that account into the currency the rail settles in, then fund the payout from
# the converted one. Two operations with two settlements, which is what the
# copy says: the composite is not buildable (`autoPayout` is refused on a
# fiat→fiat conversion), so nothing here may imply one call, one rate or one
# settlement.
#
# The round trip travels on the payout page's own four route parameters and
# NOTHING else. Not the typed amount — what the conversion delivers is what
# there is to send, so the amount is answered again against the account that
# received it — and above all nothing nonce-like: the payout page mints its
# intent per render (OPERATIONS_SPEC §1), so the way back has to be an ordinary
# GET whose render mints a fresh one. Whitelisting the four keys is what makes a
# stale token structurally unable to ride the link.
ROUTE_KEYS: tuple[str, ...] = ("purpose", "rail", "recipientType", "destinationCountry")


def route_query(values: Mapping, **extra: str) -> str:
    """The four route parameters found in `values`, plus whatever the caller
    names explicitly, as a query string. Empty answers are dropped — the payout
    page is one URL with a widening query string, so a partial route is a
    prefilled route row rather than an error — and a key outside the four is
    only ever here because a caller wrote it out.
    """
    pairs = [(key, str(values.get(key) or "").strip()) for key in ROUTE_KEYS]
    pairs += [(key, str(value or "").strip()) for key, value in extra.items()]
    return urlencode([(key, value) for key, value in pairs if value])


def rails_for(entry: Mapping | None) -> tuple[str, ...]:
    """The payout rails this registered destination can be paid over. An entry
    on a rail this build has never seen offers none rather than a guess."""
    return RAILS_FOR.get(str((entry or {}).get("rail") or ""), ())


def payable_over(entry: Mapping | None, rail: str) -> bool:
    return (rail or "").lower() in rails_for(entry)


def registered_only(items: Sequence[Mapping]) -> list[dict]:
    """The entries a payout may actually name. `pending_review` is not yet a
    recipient and `suspended`/`revoked`/`rejected` no longer are — offering any
    of them would be offering a payout Conduit will refuse."""
    return [dict(i) for i in items if i.get("status") == USABLE_STATUS]


# --- the "whitelist another customer's account" shortcut -------------------------------

# One deposit-instruction block type per whitelist rail. A block type this build
# has never seen yields no prefill rather than a guess.
_BLOCK_RAIL = {"us_domestic": "us", "swift": "swift", "sepa": "sepa", "uk_domestic": "uk_domestic"}


def _first_routing(block: Mapping) -> str:
    for entry in block.get("rails") or []:
        if isinstance(entry, Mapping) and entry.get("routingNumber"):
            return str(entry["routingNumber"])
    return ""


def prefill_from_account(account: Mapping | None) -> dict:
    """`{rail, values}` for the group-entity shortcut: the bank coordinates a
    payer would be handed, read off the target account's own deposit
    instructions.

    Only the coordinates — the operator still confirms the relationship and the
    legal name, and Conduit still reviews the registration. Returns `{}` when
    the account publishes nothing this console can register.
    """
    for block in (account or {}).get("depositInstructions") or []:
        if not isinstance(block, Mapping):
            continue
        rail = _BLOCK_RAIL.get(str(block.get("type") or ""))
        if rail is None:
            continue
        values: dict[str, str] = {}
        if name := block.get("beneficiaryName"):
            values["legalName"] = str(name)
        if rail == "us":
            routing = _first_routing(block)
            if not (block.get("accountNumber") and routing):
                continue
            values |= {"accountNumber": str(block["accountNumber"]), "routingNumber": routing}
        elif rail == "sepa":
            if not block.get("iban"):
                continue
            values["iban"] = str(block["iban"])
        elif rail == "uk_domestic":
            if not (block.get("accountNumber") and block.get("sortCode")):
                continue
            values |= {
                "accountNumber": str(block["accountNumber"]),
                "sortCode": str(block["sortCode"]),
            }
        else:  # swift
            bic = block.get("bic") or (block.get("bank") or {}).get("bic")
            if not bic:
                continue
            values["bic"] = str(bic)
            for key in ("iban", "accountNumber"):
                if block.get(key):
                    values[key] = str(block[key])
        return {"rail": rail, "values": values}
    return {}


# --- reads ------------------------------------------------------------------------------


async def fetch_requirements(
    client,
    *,
    purpose: str,
    rail: str,
    recipient_type: str | None = None,
    destination_country: str | None = None,
) -> dict | Result:
    """`GET /v2/payouts/requirements`, or the failing `Result` unchanged so the
    route renders Conduit's own problem-detail."""
    result = await client.get(
        REQUIREMENTS_PATH,
        purpose=purpose,
        rail=rail,
        recipientType=recipient_type or None,
        destinationCountry=destination_country or None,
    )
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    return result


async def fetch_recipients(
    client, customer_id: str, status: str | None = None, cursor: str | None = None
):
    """One cursor page of the customer's whitelist recipients (`Page`), or the
    failing `Result` — an unreadable list is never rendered as an empty one.

    `cursor` is how `web.contacts._whitelist` walks past the first page; the
    pickers still take page one, which is what they always did.
    """
    return await client.page(
        WHITELIST_PATH.format(customer_id=customer_id), limit=100, status=status, cursor=cursor
    )


async def fetch_transaction(client, transaction_id: str) -> dict | Result:
    result = await client.get(f"{TRANSACTIONS_PATH}/{transaction_id}")
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    return result


# --- the payout form --------------------------------------------------------------------


ACH_SUBTREE = ("destination", "ach")


def _spurious_ach(model: forms.FormModel) -> set[tuple[str, ...]]:
    """**The one place this console overrides discovery** — conduit-issues/06.

    Everywhere else the engine obeys the requirements response verbatim; that is
    the whole design (FORM_ENGINE_SPEC §1). This is the single documented
    exception, and it is narrow on purpose: `destination.ach.*` fields declared
    on a route whose rail is not `ach`.

    Both fedwire fixtures ship `destination.ach.authorizationType` with `required:
    true`; sepa and swift do not. Verified live 2026-08-28: a fedwire payout carrying
    that subtree was accepted (202) and the whole `ach` block was **silently dropped** —
    it is absent from the created resource. So the field is neither validated nor stored
    on that rail, and obeying discovery here means making an operator answer a mandatory
    question that changes nothing.

    Delete this function the day discovery stops declaring the subtree off-rail;
    the rail check below already makes it a no-op on a real ACH route.
    """
    if (model.rail or "").lower() == "ach":
        return set()
    return {f.path for f in model.fields if f.path[: len(ACH_SUBTREE)] == ACH_SUBTREE}


def payout_model(snapshot: Mapping) -> forms.FormModel:
    """Discovery's Dialect B payload minus the fields the route owns, and minus
    the one subtree discovery declares that Conduit itself discards."""
    model = forms.parse(snapshot)
    drop = ROUTE_OWNED_FIELDS | _spurious_ach(model)
    return dataclasses.replace(model, fields=[f for f in model.fields if f.path not in drop])


# The recipient identity a registered whitelist entry actually carries. When
# `whitelist.required` these come from the picked entry and nowhere else — the
# whole point of the gate is that the operator cannot type a destination.
COORDINATE_KEYS = ("accountNumber", "routingNumber", "sortCode", "iban", "bic", "legalName")


def coordinate_fields(model: forms.FormModel) -> list[forms.Field]:
    """Discovery's recipient-identity fields, whatever the rail spells them as —
    matched on the last path segment, so nothing is hardcoded per rail."""
    return [f for f in model.fields if f.path and f.path[-1] in COORDINATE_KEYS]


def recipient_model(model: forms.FormModel) -> forms.FormModel:
    """The form as rendered under a whitelist gate: the coordinate fields come
    out, the rest (addresses, account type, remittance) stay."""
    keep = {f.path for f in coordinate_fields(model)}
    return dataclasses.replace(model, fields=[f for f in model.fields if f.path not in keep])


def apply_recipient(model: forms.FormModel, values: forms.FormValues, entry: Mapping) -> None:
    """Write the picked entry's coordinates onto the submitted values.

    Server-side and unconditional: the browser sent whatever it sent, and under
    a whitelist gate the destination is Conduit's registered record of it, not
    the request's.
    """
    for field in coordinate_fields(model):
        value = entry.get(field.path[-1])
        if value not in (None, ""):
            forms._set_path(values.root, field.path, str(value))


# A plain decimal literal and nothing else: ASCII digits, at most one dot, no
# sign, no underscore, no exponent. `Decimal` is far more permissive than that —
# it accepts "1_000" (= 1000), "1e3" (= 1000), "+5", "NaN", "Infinity" and
# Unicode decimal digits from any script — and every one of those used to be
# handed to Conduit VERBATIM, because this function returned its input. What the
# operator read on the confirmation and what went on the wire were then two
# different numbers, or the same number spelled a way the far side may parse
# differently. `[0-9]` rather than `\d`, which is Unicode-aware and matches
# fullwidth digits.
PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# The row `amount` column this feeds is `varchar(64)` (`app/models.py`), and a
# canonical string longer than that is not a bigger payment — it is a malformed
# one, which used to reach the operator as a DB error rather than a refusal.
# The cap lives here, not at whichever caller persists it, because this return
# value is the wire form *and* what the operator is shown.
#
# It bounds the CANONICAL LENGTH, not the significant-digit count. Those are
# different: `0.` followed by 64 zeroes and a `1` has one significant digit and
# formats to 67 characters, so a significant-digit cap let exactly the overflow
# it was written to stop straight through. Scale is the other
# half of the length and `format(Decimal, "f")` is what decides it, so the only
# honest place to measure is the formatted string.
MAX_CANONICAL_CHARS = 64


def amount(text: str) -> str | None:
    """The operator's amount in canonical form, or None if it is not a positive
    plain decimal.

    Canonical means `format(Decimal, "f")`: fixed-point, never an exponent (which
    is what `str(Decimal("1e3"))` gives), and trailing zeros left exactly as
    typed — `1.50` is not rewritten to `1.5`, because scale is information on a
    money field. The only thing that changes is a run of leading zeros.

    **This is the value that is transmitted.** Every caller passes what comes
    back here into the wire body *and* into whatever it shows the operator, so
    the two cannot disagree (`app.batches.totals` sums this, and it is the sum
    the confirm screen states).

    No quantization to the asset's scale: that would need a scale-per-asset table
    in this repository, and this console deliberately holds no such tables (plan
    v2 ground rules — Conduit owns the money data). An over-precise amount is
    passed through and refused by Conduit, which is the side that knows.
    """
    text = (text or "").strip()
    if not PLAIN_DECIMAL.fullmatch(text):
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):  # pragma: no cover - the regex got there first
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    canonical = format(parsed, "f")
    if len(canonical) > MAX_CANONICAL_CHARS:
        return None
    return canonical


def blocked_country(model: forms.FormModel, values: forms.FormValues, *extra: str) -> str | None:
    """The first jurisdiction discovery told us to refuse, if the operator named
    one — the route's destination country, or any country field in the form.

    Comparison is a plain uppercase set membership against the list Conduit
    sent. `blockedJurisdictions` ships ISO-3 codes; a form that answers ISO-2
    slips past here and is caught server-side by Conduit, which is the gate that
    counts. Never a local country table (plan v2 ground rules).
    """
    blocked = {str(code).upper() for code in model.blocked_jurisdictions}
    if not blocked:
        return None
    candidates = [str(v) for v in extra if v]
    candidates += [
        str(forms.lookup(f.path, values.root))
        for f in model.fields
        if f.path and f.path[-1] == "country"
    ]
    return next((c for c in candidates if c.upper() in blocked), None)


def payout_body(
    model: forms.FormModel,
    values: forms.FormValues,
    *,
    customer_id: str,
    virtual_account_id: str,
    asset: str,
    amount_text: str,
    purpose: str,
    document_ids: Sequence[str] = (),
) -> dict:
    """`FiatPayoutDto`. The `destination` subtree is the engine's (Dialect B
    nesting of discovery's dotted names); everything else is the route's."""
    return assembled_payout_body(
        forms.assemble(model, values),
        customer_id=customer_id,
        virtual_account_id=virtual_account_id,
        asset=asset,
        amount_text=amount_text,
        purpose=purpose,
        document_ids=document_ids,
    )


def assembled_payout_body(
    assembled: Mapping,
    *,
    customer_id: str,
    virtual_account_id: str,
    asset: str,
    amount_text: str,
    purpose: str,
    document_ids: Sequence[str] = (),
) -> dict:
    """The same `FiatPayoutDto`, from a body the engine has **already**
    assembled.

    Split out for batch dispatch, which assembles each row at
    *upload* and stores that subtree — so at dispatch there is no form and no
    `FormValues` to re-derive one from. One builder rather than two: a batch
    payout that differed from a single payout by one key would be a difference
    nobody could see until Conduit answered, and the single-payout path is the
    one that has been proven live.
    """
    assembled = dict(assembled)
    assembled.pop("documentIds", None)
    assembled.pop("clientReferenceId", None)
    body: dict[str, Any] = {
        "customerId": customer_id,
        "virtualAccountId": virtual_account_id,
        "assetAmount": {"code": asset, "amount": amount_text},
        "purpose": purpose,
        "destination": assembled.get("destination") or {},
    }
    if document_ids:
        body["documents"] = list(document_ids)[:MAX_DOCUMENTS]
    return body


# --- the virtual-account destination arm --------------------------------------

# `FiatPayoutDto.destination.anyOf[1]` in the pinned spec: `{type, virtualAccountId,
# remittance?}` with `additionalProperties: false` and `required: [type,
# virtualAccountId]`. **No rail and no recipient** — you name the destination
# account and Conduit routes the movement. Same currency both sides, same
# organisation, any customer of it, and a destination that is not the source.
VIRTUAL_ACCOUNT = "virtual_account"

# The DTO's own caps on `destination.remittance`, stated once so the form's
# `maxlength` attributes and this builder cannot disagree about them.
REMITTANCE_LIMITS: tuple[tuple[str, int], ...] = (("reference", 140), ("description", 280))

# Conduit's refusal when the two sides of a virtual-account transfer are not the
# same currency. A cross-currency move between Conduit accounts is an *order*,
# not a payout, which is why this console points at Convert instead.
PAYOUT_DESTINATION_CURRENCY_MISMATCH = "PAYOUT_DESTINATION_CURRENCY_MISMATCH"

SAME_ACCOUNT_MESSAGE = (
    "A transfer needs a destination account that is not the source account. Pick the other "
    "side of the move."
)
TRANSFER_CURRENCY_MESSAGE = (
    "That account holds {destination} and this one holds {asset}. Transfers are "
    "same-currency — Conduit refuses the pair with "
    f"`{PAYOUT_DESTINATION_CURRENCY_MISMATCH}`."
)


def remittance(reference: str = "", description: str = "") -> dict:
    """`destination.remittance`, truncated to the DTO's own caps.

    Empty when neither half says anything: an empty object is not a remittance,
    and `additionalProperties: false` makes a speculative key a 400 rather than
    a no-op.
    """
    given = {"reference": reference or "", "description": description or ""}
    return {key: given[key].strip()[:cap] for key, cap in REMITTANCE_LIMITS if given[key].strip()}


def virtual_account_body(
    *,
    customer_id: str,
    virtual_account_id: str,
    destination_account_id: str,
    asset: str,
    amount_text: str,
    reference: str = "",
    description: str = "",
    document_ids: Sequence[str] = (),
) -> dict:
    """`FiatPayoutDto` on the virtual-account arm — the transfers screen's body.

    A **separate branch**, not a variant of `payout_body`: the destination
    subtree is constructed here from two arguments and nothing else, so no
    assembled form, no discovery snapshot and no recipient coordinate can reach
    it. Emitting a `rail` or a `recipient` beside a `virtualAccountId` is not
    something a caller can get wrong here — there is no code path that writes
    them.

    The envelope (customer, funding account, amount, purpose, documents) is
    `assembled_payout_body`'s, unchanged: it is the same `POST /v2/payouts`, so
    one builder owns the parts that are the same on every arm.

    `purpose` is not a parameter. A transfer is an `intercompany` payout by
    definition here (`TRANSFER_PURPOSE`, still required by the DTO), and a
    caller free to pass something else would be a second answer to a settled
    question.

    Raises `ValueError` when the destination is missing or is the source
    account. The route refuses that before it gets here, with a sentence an
    operator can read; this is the structural backstop that makes "a transfer
    into itself" unrepresentable rather than merely unreachable.
    """
    if not destination_account_id or destination_account_id == virtual_account_id:
        raise ValueError(SAME_ACCOUNT_MESSAGE)
    destination: dict[str, Any] = {
        "type": VIRTUAL_ACCOUNT,
        "virtualAccountId": destination_account_id,
    }
    if note := remittance(reference, description):
        destination["remittance"] = note
    return assembled_payout_body(
        {"destination": destination},
        customer_id=customer_id,
        virtual_account_id=virtual_account_id,
        asset=asset,
        amount_text=amount_text,
        purpose=TRANSFER_PURPOSE,
        document_ids=document_ids,
    )


DOCUMENTATION_REQUIRED = "DOCUMENTATION_REQUIRED"
# Conduit's refusal when an `intercompany` destination is not a registered
# whitelist entry. Rendered with a link to the whitelist page rather than as a
# bare problem card — the fix is one page away.
RECIPIENT_NOT_WHITELISTED = "RECIPIENT_NOT_WHITELISTED"


def documentation_gap(model: forms.FormModel, values: forms.FormValues) -> bool:
    """Discovery says a document is required and none is attached. Refusing here
    saves an operation row and a round trip for a 422 we can see coming."""
    return bool(model.documentation.get("required")) and not values.document_ids


def pick_recipient(
    entries: Sequence[Mapping], chosen: str, rail: str = ""
) -> tuple[dict | None, forms.Message | None]:
    """The registered entry named out of a whitelist **already read**, or the
    sentence that explains why there isn't one.

    Split out of `resolve_recipient` for the one caller that
    must not read per candidate: a batch of 200 rows on a gated route resolves
    every row against **one** whitelist read, and the refusals stay these exact
    sentences rather than a second set written for the batch report.
    """
    entry = next((e for e in registered_only(entries) if e.get("id") == chosen), None)
    if entry is None:
        return None, forms.Message(
            "This route requires a registered whitelist recipient; pick one of the "
            "customer's registered entries."
        )
    if rail and not payable_over(entry, rail):
        return None, forms.Message(
            f"A {entry.get('rail')} destination cannot be paid over {rail}."
        )
    return dict(entry), None


async def resolve_recipient(
    client, customer_id: str, chosen: str, rail: str = ""
) -> tuple[dict | None, forms.Message | None]:
    """The registered whitelist entry a gated payout named, or the sentence that
    explains why there isn't one.

    Shared by the payout form and the transfers screen because the gate
    is the same gate: under `whitelist.required` the destination is Conduit's own
    registered record, and an unreadable whitelist is *not* "no entries" — the
    payout is refused either way, but the operator is told which.

    `rail` enforces the family table on the way through: the
    transfers screen filtered its picker by it while the payout form did not, so
    a `sepa` entry could be named on a fedwire payout and the coordinates copied
    onto it were ones that rail cannot carry.
    """
    page = await fetch_recipients(client, customer_id)
    if not isinstance(page, Page):
        return None, forms.Message(UNREADABLE_WHITELIST)
    return pick_recipient(page.items, chosen, rail)


# The route-level refusals, as sentences. Constants because the batch screens
# make exactly the same three judgements about a CSV row and a batch
# as this function makes about a form — and a batch error that read differently
# from the single-payout error for the same fault is the thing the design owner
# ruled out.
AMOUNT_MESSAGE = "The amount must be a positive decimal, e.g. 1000.00."
CEILING_MESSAGE = (
    "This deployment refuses any single amount over {ceiling}. "
    "{amount} is above that ceiling, so nothing was sent."
)
DOCUMENTATION_MESSAGE = (
    "Conduit requires a supporting document for this route; attach one before sending."
)
UNREADABLE_WHITELIST = "The whitelist could not be read, so the recipient was not verified."


def blocked_message(code: str) -> str:
    return f"{code} is a jurisdiction Conduit blocks for this route."


def over_ceiling(amount_text: str | None) -> str | None:
    """The refusal sentence if `MONEY_CEILING` is set and this amount exceeds it.

    One function for every money mutation in the console (single payout,
    virtual-account transfer, each batch row, the batch total, a conversion
    order). Asset-agnostic: one number, and the environment strip states it, so
    an operator is never guessing which currency the ceiling is in.

    Called **before** `operations.start` at every site, so a refused submit never
    becomes a ledger row — a ceiling that appeared in the ledger as a rejected
    operation would be a record of an attempt this console had already decided
    not to make.

    An *absent* amount is not this guard's business — `amount()` has already
    refused it and the site reports that, so returning None keeps one fault to
    one sentence. A *present but unparseable* one is refused: with a ceiling
    configured, "this console could not read the number" must not resolve to
    "therefore it is under the limit".
    """
    ceiling = get_settings().ceiling
    if ceiling is None:
        return None
    text = (amount_text or "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
        under = parsed.is_finite() and parsed <= ceiling
    except (InvalidOperation, ValueError):
        under = False
    if under:
        return None
    return CEILING_MESSAGE.format(ceiling=format(ceiling, "f"), amount=text)


def payout_errors(
    model: forms.FormModel,
    values: forms.FormValues,
    *,
    account: Mapping | None,
    amount: str | None,
    rail: str | None,
    country: str = "",
) -> forms.FormErrors:
    """Discovery's own validation plus the route-level refusals every
    `FiatPayoutDto` submission shares. Callers add whatever else their screen
    knows (a stale quote, a cross-currency pick) to the returned errors.

    `rail` turns on the currency guard and is **required, with no
    default** — the opt-out is an explicit `rail=None`. It used to default to
    `""`, which meant a new caller that simply forgot it got a payout body
    validated with the doomed-rail guard silently switched off: the one refusal
    here that is not mirroring a Conduit 4xx (Conduit answers 202 and then fails
    after review, `rail_unavailable`). Forgetting must not be spellable, so the
    parameter has no default and every caller states its choice.

    There is no `rail=None` caller today. The transfers screen used to be the
    one deliberate opt-out; it sends on the virtual-account arm,
    which has no rail to guard and does not call this function at all. The
    parameter keeps its no-default shape anyway — that is what makes forgetting
    unspellable, and a caller that genuinely has no rail must still say so.
    """
    errors = forms.validate(model, values)
    if documentation_gap(model, values):
        errors.documents.append(forms.Message(DOCUMENTATION_MESSAGE))
    # Every route-level refusal below also lands in `errors.fields`, keyed to
    # the hand-templated control it is actually about:
    # `errors.form` keeps feeding the summary banner unchanged, and the field
    # entry is what lets the template give that control the same
    # `aria-invalid`/`aria-describedby` binding `m.err_attrs` gives a
    # discovery-driven field — `payouts/new.html` reads `rf.errors` off a
    # small ad-hoc mapping rather than a `RenderField`, since `amount` and
    # `virtualAccountId` are route-owned and never reach `forms.validate`.
    if account is None:
        message = "Pick an active virtual account to fund the payout."
        errors.form.append(forms.Message(message))
        errors.add("virtualAccountId", message)
    if amount is None:
        errors.form.append(forms.Message(AMOUNT_MESSAGE))
        errors.add("amount", AMOUNT_MESSAGE)
    if refusal := over_ceiling(amount):
        errors.form.append(forms.Message(refusal))
        errors.add("amount", refusal)
    if blocked := blocked_country(model, values, country):
        errors.form.append(forms.Message(blocked_message(blocked)))
    # Pre-ledger, deliberately: this is not mirroring a 4xx (Conduit answers
    # 202), it is declining an attempt that live evidence proves is doomed —
    # accepted at create, `rail_unavailable` after review, no funds moved.
    asset = str(((account or {}).get("asset") or {}).get("code") or "")
    if pinned := doomed_rail(rail, asset):
        message = RAIL_ASSET_MESSAGE.format(rail=rail, pinned=pinned, asset=asset)
        errors.form.append(forms.Message(message))
        errors.add("virtualAccountId", message)
    return errors


# --- indicative quotes -------------------------------------------------------------------


def quote_request(
    *, source: str, destination: str, destination_country: str, amount_text: str
) -> dict:
    """`CreateQuoteRequestDto` for a *withdrawal* quote: same asset both sides
    plus a destination country is the mode the spec says prices a payout.
    `lockSide: source` — the operator types what leaves the account."""
    return {
        "source": {"code": source},
        "destination": {"code": destination},
        "destinationCountry": destination_country,
        "lockSide": "source",
        "amount": amount_text,
    }


def _money(value: Any) -> str:
    if not isinstance(value, Mapping) or not value.get("amount"):
        return ""
    return f"{value['amount']} {value.get('code') or ''}".strip()


def quote_view(response: Mapping | None) -> dict | None:
    """`QuoteResponseDto` → the indicative panel. Amounts stay strings."""
    if not isinstance(response, Mapping) or not response.get("options"):
        return None
    options = []
    for option in response.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        rate = option.get("rate") if isinstance(option.get("rate"), Mapping) else {}
        options.append(
            {
                "id": option.get("id"),
                "rail": option.get("rail") or "—",
                "end_user_rate": rate.get("endUserRate") or "",
                "reference_rate": rate.get("referenceRate") or "",
                "spread_bps": rate.get("totalSpreadBps") or "",
                "fees": [
                    {
                        "charge": fee.get("charge") or "",
                        "owner": fee.get("owner") or "",
                        "amount": _money(fee.get("assetAmount")),
                    }
                    for fee in option.get("fees") or []
                    if isinstance(fee, Mapping)
                ],
                "source_amount": _money(option.get("sourceAmount")),
                "destination_amount": _money(option.get("destinationAmount")),
                "recipient_amount": _money(option.get("recipientAmount")),
                "total_debit": _money(option.get("totalDebit")),
            }
        )
    return {
        "id": response.get("id"),
        "expires_at": response.get("expiresAt") or "",
        "lock_side": response.get("lockSide") or "",
        "amount": _money(response.get("amount")),
        "options": options,
        "stale": expired(response.get("expiresAt")),
    }


def expired(expires_at: Any, now: datetime | None = None) -> bool:
    """Whether a quote option's `expiresAt` has passed.

    A missing or unparseable timestamp counts as expired: the panel says
    "indicative", and the only safe reading of "we cannot tell when this stops
    being true" is that it already has.
    """
    if not expires_at:
        return True
    try:
        moment = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= (now or datetime.now(UTC))


# --- the transactions ledger ---------------------------------------------------------------

# The kinds this console works in — the tab strip, and the value the ledger's
# All view sends as a repeated `type` parameter (one read, one cursor). The
# spec's enum has seven; onramp, offramp and conversion are crypto and out of
# product scope, so All means all of THESE and the template says so.
TRANSACTION_TYPES: tuple[str, ...] = (
    "deposit",
    "withdrawal",
    "deposit_return",
    "fiat_conversion",
)
TRANSACTION_STATUSES: tuple[str, ...] = (
    "pending",
    "processing",
    "completed",
    "failed",
    "cancelled",
)

# `stage` is informational (the spec is explicit that it replaces nothing), so it
# renders as a sub-label under the status pill and never as a status of its own.
STAGE_LABELS = {
    "awaiting_signature": "awaiting a signature",
    "awaiting_customer_action": "awaiting the customer",
    "under_review": "under review",
    "settling": "settling",
}

# **"What happens next", for the ledger** (spec §4.1: the
# column "survives and should be promoted ... widen its coverage"). The Overview
# carries one of these per *operation* state already (`app/web/dashboard.py`'s
# `NEXT_STEP`); this is the same idea over the states a **transaction** can be
# in, which is the other table an operator scans daily and the one that had no
# answer to "whose problem is this" at all.
#
# The keys are `PILL_TONES["transactions"]`/`TRANSACTION_STATUSES` and nothing
# else — no state is invented here, and `tests/test_vocabulary_drift.py` pins
# that. `completed` is deliberately absent: nothing happens next on a payment
# that landed, and a sentence saying so on every row of a healthy page is the
# per-visit cost §4.5 spends the rest of this slice removing. An unknown status
# gets nothing for the same reason the pill renders it neutrally — the console
# does not know what happens next and will not guess.
#
# Each sentence names *who owns the wait*, and none of them describes machinery
# this console cannot observe: `failed` points at the failure code on the
# detail page rather than claiming what caused it or promising a retry.
TRANSACTION_NEXT_STEP = {
    "pending": "Conduit has it and has not moved it yet — nothing to do here.",
    "processing": "Conduit is moving it — nothing to do until it settles or fails.",
    "failed": "Conduit stopped this one — open it for the failure Conduit gave.",
    "cancelled": "Cancelled — nothing further happens on this transaction.",
}

CANCELLABLE_STATUSES = ("pending",)


def stage_label(transaction: Mapping | None) -> str:
    stage = (transaction or {}).get("stage")
    if not stage:
        return ""
    return STAGE_LABELS.get(str(stage), f"stage: {stage}")


def can_cancel(transaction: Mapping | None) -> bool:
    """A payout may be cancelled while it is still pending. An unknown status is
    never cancellable (plan v2 §7: no state-dependent action on an unknown
    state)."""
    txn = transaction or {}
    return txn.get("type") == "withdrawal" and txn.get("status") in CANCELLABLE_STATUSES


def fee_rows(transaction: Mapping | None) -> list[dict]:
    rows = []
    for fee in (transaction or {}).get("fees") or []:
        if isinstance(fee, Mapping):
            rows.append(
                {
                    "type": fee.get("type") or "",
                    "owner": fee.get("owner") or "",
                    "amount": _money(fee.get("assetAmount")),
                }
            )
    return rows


def markup_row(transaction: Mapping | None) -> dict | None:
    markup = (transaction or {}).get("markup")
    if not isinstance(markup, Mapping):
        return None
    return {
        "bps": markup.get("bps"),
        "flat": _money(markup.get("flatAmount")),
        "charged": _money(markup.get("assetAmount")),
    }


_SKIP_IN_GENERIC = frozenset({"id", "type", "status", "customerId"})


# --- what the generic walk is not allowed to print ------------------------------------
#
# `generic_rows` walks *every* scalar of whatever arrived, which is what makes
# it an honest fallback for a shape this build has never seen — and what made
# it, on two pages, a payee's full bank coordinates on screen. The withdrawal
# detail page renders `destination.recipient` through `side_rows`, and the
# pinned contract makes `accountNumber` required on every us-rail recipient
# branch (`iban` on swift/sepa); the orders raw panel renders
# `autoPayout.recipient`, which additionally carries `dateOfBirth`, `phone` and
# `postalAddress`. Both are the artefact `app/web/exports.py` states does not
# exist — "a full account number or IBAN is in no column of any surface" — and
# both break DESIGN.md's 2026-08-30 rule, which puts full coordinates on exactly
# one screen: the payout form an operator is typing into. That form is built by
# `app/forms.py` from discovery and never comes through here.
#
# The rule is applied **at the walker** rather than at the two call sites: one
# boundary covers both pages and whatever renders a DTO this way next. By key
# name, never by the shape of the value — "twelve digits" is also an amount, a
# reference, a fedwire IMAD and an ACH trace number.
#
# Two treatments, because the two kinds of data are needed differently here:
#
# * **Masked** — the account coordinates. `counterparties.mask` and its key
#   list, imported rather than re-derived, so this walk and the contact list
#   cannot drift about what counts as a coordinate or what four digits look
#   like. Four digits is what tells two destinations apart, which is the only
#   question this page asks of them.
# * **Withheld** — the payee's own person and address. There is no useful
#   last-four of a date of birth: masked, it still states the year, and neither
#   it nor a phone tail answers anything an operator asks of a payment — the
#   legal name is what they reconcile against. So the value is dropped, and
#   `postalAddress` is dropped whole rather than walked into five rows of dots.
#
# The row itself stays, saying `withheld`, for the same reason the emptied
# Sender table has a sentence of its own (DESIGN.md 2026-09-01): the console
# does not report its own suppression as an absence at Conduit. "Conduit sent no
# date of birth" and "Conduit sent one and this page will not print it" are
# different facts, and an operator ringing support needs to know which.
#
# Bank identifiers are deliberately NOT here and print whole — `routingNumber`,
# `bic`, `bankName`, `bankAddress`. They identify a *bank*, they are public
# routing data everywhere else in this console (DESIGN.md, same row), and a
# masked routing number tells an operator chasing a wire nothing at all. The
# beneficiary address on a deposit-funded order's `depositInstructions` is left
# alone for the neighbouring reason: it is the customer's own account, printed
# in full on the accounts page precisely so it can be relayed to a payer.


def _fold(key: str) -> str:
    """`postalAddress`, `postal_address`, `POSTALADDRESS` → `postaladdress`.

    The walk labels keys exactly as Conduit spelled them, at any depth, and
    `humanize` — the labeller it has always used — already takes both dialects
    (`sortCode` / `sort_code`), which is this codebase's own record that both
    arrive. A masking set matching one spelling would be a masking set with a
    hole in it, and the hole would be a full account number.
    """
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_MASKED_IN_GENERIC = frozenset(_fold(key) for key in counterparties.MASKED_KEYS)
# Exactly what the pinned `autoPayout.recipient` and the payout recipient
# branches carry of a natural person, and nothing inferred: a suffix rule on
# `*Address` would take the bank's address and the customer's own with it.
_WITHHELD_IN_GENERIC = frozenset(_fold(key) for key in ("dateOfBirth", "phone", "postalAddress"))

WITHHELD = "withheld"


def _generic_row(path: tuple[str, ...], value: str) -> dict:
    return {"label": " · ".join(humanize(p) for p in path), "value": value}


def _sent_anything(node: Any) -> bool:
    """Whether Conduit put anything at all under this key — the walk's own
    emptiness test, run over a subtree the walk is about to refuse to enter. A
    `withheld` row over a key that arrived as `null` would be this console
    inventing the very fact it is withholding."""
    if isinstance(node, Mapping):
        return any(_sent_anything(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(_sent_anything(value) for value in node)
    return isinstance(node, (str, int, float, bool)) and bool(str(node).strip())


def generic_rows(node: Any, path: tuple[str, ...] = (), masked: bool = False) -> list[dict]:
    """Every scalar in a transaction this build has no typed view for, labelled
    by its own key path — coordinates masked and personal data withheld (see
    the note above).

    The fallback for an unknown `type` (plan v2 §7): a transaction kind this
    console has never heard of still shows what Conduit sent, rather than an
    empty page or an inferred meaning.

    `masked` is the decision carried down, and it has to be carried because the
    key that earns it is not always the key the scalar sits under. This is the
    fallback for a DTO nobody has seen, so its shape is unknown by construction:
    an `accountNumber` may arrive as a string, as `["1234…"]` or as
    `{"value": "1234…"}`. Deciding at the leaf, the last two recursed past the
    masked key, the leaf became `0` or `value`, and the coordinate printed whole
    — while `_WITHHELD_IN_GENERIC`, tested before the recursion, collapsed the
    same shapes correctly. The two rules now agree about container-valued keys.
    """
    rows: list[dict] = []
    leaf = _fold(path[-1]) if path else ""
    if leaf in _WITHHELD_IN_GENERIC:
        # Not walked at all: a scalar and a five-line address alike collapse to
        # one row that names the field and states nothing.
        return [_generic_row(path, WITHHELD)] if _sent_anything(node) else []
    masked = masked or leaf in _MASKED_IN_GENERIC
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not path and key in _SKIP_IN_GENERIC:
                continue
            rows += generic_rows(value, path + (str(key),), masked)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            rows += generic_rows(value, path + (str(index),), masked)
    elif isinstance(node, (str, int, float, bool)) and str(node).strip():
        rows.append(_generic_row(path, counterparties.mask(node) if masked else str(node)))
    return rows


def converted(transaction: Mapping | None) -> str:
    """`1000.00 USD → 912.30 EUR` for a transaction whose two sides are in
    different assets.

    A conversion leg's whole content is the pair of amounts, and `side_rows`
    deliberately drops `assetAmount` (the header carries one of them) — so
    without this the typed view would show a conversion with the money missing.
    Empty when the two sides do not both state an amount.

    **The test is the pair of asset codes, not the pair of formatted strings.**
    A payout debits 1000.00 USD and delivers 987.50 USD — two different strings,
    one currency, nothing converted — and comparing the strings labelled that
    "Converted" on every fee-bearing withdrawal's detail page. A same-asset
    movement is never a conversion, whatever the fee did to the number.
    """
    txn = transaction or {}
    sides = [
        side.get("assetAmount") if isinstance(side, Mapping) else None
        for side in (txn.get("source"), txn.get("destination"))
    ]
    left, right = (_money(side) for side in sides)
    out, into = (
        str(side.get("code") or "") if isinstance(side, Mapping) else "" for side in sides
    )
    # Both codes have to be *stated* and differ. A side that named no asset is
    # an unknown currency, not a second one.
    return f"{left} → {right}" if left and right and out and into and out != into else ""


def side_rows(side: Mapping | None) -> list[dict]:
    """One side of a transaction, whichever branch of the `oneOf` it is. The
    branches overlap almost entirely and Conduit adds new ones, so this walks
    what arrived instead of switching on `type`."""
    if not isinstance(side, Mapping):
        return []
    return generic_rows({k: v for k, v in side.items() if k != "assetAmount"})
