"""Batch payouts: the template's columns, the upload's parser, the row
validator, the batch's own persistence, and the dispatch
engine that turns each valid row into one payout.

Route-free like `app/payments/` and `app/counterparties.py`: `app/web/batches.py`
renders what this module shapes and `tests/e2e/08_batch_payouts.py` drives the
same functions, so what the live run proves is what an operator sees.

**The template is discovery's, exactly as the form is.** A payout's columns are
`GET /v2/payouts/requirements` for the chosen route, in the order it sent them,
plus the three things the route owns rather than declares (`purpose`, `amount`,
and a `contact`). Nothing is hardcoded per rail, and a route this build has never
seen produces a template all the same — which is the whole point of generating it
from the live response instead of shipping a file.

**A batch is keyed by rail + recipient type, and `purpose` is a
COLUMN** (the payout-flow restructure round). One file may carry a goods row and
an intercompany row, because that is what a payment run out of an accounting
system looks like — and splitting it into one file per purpose was the console's
convenience, never the operator's. So the template's field columns are the
**union** of the per-purpose requirements for the route (all of
`payments.PURPOSE_VALUES`, read at template time — seven bounded reads), and
every row is validated against **its own** purpose's model. A row that fills a
column its purpose does not declare is refused for that row, with the same
honesty the file-level unknown-column refusal has: a value under a column that
would not be sent is never quietly dropped.

**Destination country is not part of the corridor.** Discovery's field schema
is chosen by rail and recipient type, never by country (domestic rails always
target a US account; `swift` and `sepa` are country-agnostic) — so one file's
rows may name recipients in any number of countries, each judged on its own
`*.country` field by `payments.blocked_country`.

**The fingerprint is the drift sentinel**, and it covers the whole per-purpose
set. The header block carries a sha256 over the canonical JSON of
`{purpose: requirements}` — so a change to *any* purpose's requirements makes the
template stale, which is exactly right when one file may use any of them. On
upload the console reads discovery **again**, unconditionally, for every purpose,
and validates the rows against *that*: the fingerprint decides only whether the
report says the template was stale. There is deliberately no branch here —
validating against a snapshot carried in a file would be validating against
whatever the file said, which is the one thing an uploaded file must never get to
decide.

**A purpose this route cannot be read for is named, not guessed.** If discovery
refuses one of the seven for a route, the template is still produced from the
ones that answered and the header block says which are missing; a row naming a
missing purpose is refused at upload. The alternative — no template at all
because `prefunding` 4xx'd on a corridor nobody was going to use it for — makes
one unreadable purpose cost the operator all seven.

**Unknown columns are refused, never ignored.** A column this route does not
declare is either a typo (so a value the operator believes is being sent is not)
or a field from another route (so the file is for a different payout than the
one being made). Both are worth a refusal and neither is worth a silent drop.

**A correction is a new batch.** There is no row editor: the operator fixes the
CSV and uploads it again, which mints a new batch id, and the stale one is
abandoned. The simplest honest model — a batch stays the permanent record of one
file, which is what the dispatch ledger points at — at the cost of a second row
in the list. That extends to a row Conduit *refused*: it keeps the operation
carrying the refusal, and the correction path is the same one — export the
results, fix those rows, upload them as a new batch.

**Dispatch is exactly-once by arithmetic, not by care.** Each row's operation
nonce is `uuid5(namespace, "{batch}:{row}")` — a pure function of two values
that never change — so a second dispatch of the same row resolves to the
operation the first one made, in whatever state it is in, from any process, and
nothing goes on the wire. See `intent_for` and `dispatch` at the end of this
module.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dc_field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import LargeBinary, func, or_, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import accounts, counterparties, forms, operations, payments
from app.conduit.client import Page, Problem
from app.conduit.execute import execute_operation
from app.config import get_settings
from app.crypto import read_json
# The one translation of a stored `operations.error`. Imported here rather than
# read raw, because this module's rows carry that snapshot to a page and to a
# results file (A3 gate, M1).
from app.web import problem_of
from app.models import Operation, PayoutBatch, PayoutBatchRow

log = logging.getLogger(__name__)

# The three columns the route owns rather than discovery declaring them, plus the
# comment marker the header block and the parser share. `purpose` joined them
# when a batch stopped being single-purpose: it is not a field of any
# requirements response, it is what *selects* one.
PURPOSE = "purpose"
AMOUNT = "amount"
CONTACT = "contact"
COMMENT = "#"

# The upload's two ceilings. The byte cap is the streaming one (`read_capped`);
# the row cap bounds what a single operator confirmation can put in flight,
# and both are stated in the header block so a file is never refused for
# a reason the template did not warn about.
MAX_BYTES = 1_048_576
MAX_ROWS = 500

# The one header-block key the parser reads back by name; the route's five are
# read by the caller's own expectation (`check_route`), so there is no second
# list of them here to fall out of step with the route object.
FINGERPRINT_KEY = "fingerprint"

# --- refusal sentences ---------------------------------------------------------------
#
# Every one of these ends an upload. They are constants because the flash banner,
# the tests and the re-upload prompt all quote them.

NOT_UTF8 = "That file is not UTF-8 text. Export it again as CSV UTF-8 and re-upload."
MALFORMED_CSV = "That file is not valid CSV. Re-save it as CSV UTF-8 and re-upload."
NO_HEADER = "That file has no header row — upload the template with its columns intact."
TOO_LARGE = f"That file is larger than {MAX_BYTES // 1024} KB. Split it and upload the parts."
TOO_MANY_ROWS = (
    f"That file has more than {MAX_ROWS} rows. Split it: a batch is confirmed in one "
    "click, and {MAX_ROWS} is as much as one click may carry."
).replace("{MAX_ROWS}", str(MAX_ROWS))
NO_ROWS = "That file has a header and no rows — there is nothing to validate."
UNKNOWN_COLUMNS = (
    "This route does not have {columns}. Nothing was validated: a column this route "
    "never declared is either a typo or a template for another route, and either way "
    "the values under it would not have been sent."
)
DUPLICATE_COLUMNS = "The header names {columns} more than once, so a value under it is ambiguous."
MISSING_COLUMNS = (
    "The header is missing {columns}, which this route requires. Download the template "
    "again and refill it."
)
WRONG_ROUTE = (
    "This file was built for a different route ({differences}). Download the template for "
    "the route you are on."
)

# --- per-row sentences ----------------------------------------------------------------

GATED_CONTACT = (
    "This route requires a registered whitelist recipient; put its id in the contact column."
)
EXCLUSIVE = (
    "Give a contact or the recipient columns, not both — this row names a contact and also "
    "fills {columns}."
)
# The typed cell is masked into this sentence (`counterparties.mask`, applied at
# the call site), for the reason the whole of `app/forms.py`'s allowedValues
# message is: a row's errors are kept in `payout_batch_rows.errors`, which is
# plaintext JSONB with no retention job behind it, and the first of them is
# printed in the results CSV's `problem` column. A contact column filled from a
# spreadsheet one column over holds an account number, and this refusal is what
# would then be the only cleartext, permanent copy of it on the console.
#
# The tail is enough to act on: the error names the column and the report and
# the CSV both name the row, so "which cell" was never carried by the echo —
# what the echo adds is which of several bad contacts this row had.
UNKNOWN_CONTACT = (
    "No live contact called {name!r} for this customer on this route. Use the name exactly "
    "as the Contacts page shows it, or fill the recipient columns instead."
)
UNREADABLE_CONTACT = (
    "That contact's stored coordinates cannot be read on this installation, so this row has "
    "no destination. Fill the recipient columns instead."
)
RAGGED_ROW = "This row has more cells than the header, so which value belongs to which column is not knowable."
# The purpose column's own two refusals. A row's purpose decides which model
# judges it, so neither of these can be softened into a warning: without a
# resolvable purpose there is no schema, and therefore nothing else about the
# row has been judged at all.
NO_PURPOSE = (
    "Every row needs a purpose. Put one of this template's raw purpose keys in the purpose "
    "column — they are listed in the # header block, and the column takes the key verbatim, "
    "not the label. Nothing else about this row was judged: the purpose is what says which "
    "requirements it is judged by."
)
# `{name}` is whatever `_quotable_purpose` allows to be quoted — the key itself
# when it is one of Conduit's, its masked tail when it is arbitrary typed text.
UNKNOWN_PURPOSE = (
    "{name!r} is not a purpose this route can be read for. Allowed here: {allowed}. Nothing "
    "else about this row was judged: the purpose is what says which requirements it is "
    "judged by."
)
# The per-row half of the unknown-column rule. The file-level refusal catches a
# column no purpose declares; this catches a column *another* purpose declares
# that this row filled in — same fault from the row's point of view, and the same
# answer: a value that would not be sent is never quietly dropped.
FOREIGN_COLUMNS = (
    "A {purpose} row does not have {columns}. That column belongs to another purpose's "
    "requirements, so the values under it would not have been sent — clear them, or change "
    "this row's purpose."
)


def fingerprint(snapshot: Mapping) -> str:
    """sha256 over the canonical requirements JSON — the drift sentinel.

    Canonical means sorted keys and no incidental whitespace, so the same
    response fingerprints identically across processes and days. It is a hash of
    *everything* discovery said, not just the field names: a changed `required`
    flag, a new blocked jurisdiction or a flipped `documentation.required` all
    move it, because all three change what a filled template means.
    """
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- the template ---------------------------------------------------------------------


def is_gated(model: forms.FormModel) -> bool:
    """Discovery's whitelist flag, read rather than remembered — the one thing
    that decides whether a purpose's rows may type a destination at all."""
    return bool(model.whitelist.get("required"))


def rendered_model(model: forms.FormModel) -> forms.FormModel:
    """The model the template's columns and the row validator both use: under a
    whitelist gate the recipient's coordinate fields come out, exactly as they do
    on the single-payout form (`payments.recipient_model`), because the
    destination is Conduit's registered record and not something a CSV may
    type."""
    return payments.recipient_model(model) if is_gated(model) else model


def purpose_columns(model: forms.FormModel) -> list[str]:
    """The columns **one** purpose's rows may fill: its own rendered field names,
    plus the three the route owns.

    `destination.remittance.*` is in there because discovery declares it, not
    because this module adds it — a route that ships no remittance fields gets no
    remittance columns, and inventing a pair for it would be inventing fields.
    `virtualAccountId` is absent for the same reason it is absent from the form:
    the funding account is the route's, picked once for the whole batch
    (`payments.ROUTE_OWNED_FIELDS`).
    """
    return [f.dotted for f in rendered_model(model).fields] + [PURPOSE, AMOUNT, CONTACT]


def columns(models: Mapping[str, forms.FormModel]) -> list[str]:
    """The template's columns: `purpose`, then the **union** of every purpose's
    field names in first-seen order, then `amount` and `contact`.

    Union rather than intersection because one file may carry any of them, and a
    column that only `intercompany` rows leave blank is still a column the goods
    rows need. Which purpose may fill which column is a per-row judgement
    (`Validator.row`), not a shape the header can express.
    """
    seen: dict[str, None] = {}
    for model in models.values():
        for field in rendered_model(model).fields:
            seen.setdefault(field.dotted, None)
    return [PURPOSE, *seen, AMOUNT, CONTACT]


def required_columns(models: Mapping[str, forms.FormModel]) -> list[str]:
    """The columns a filled file must carry a header for: `purpose`, plus the
    columns **every** purpose in this template requires.

    The intersection, deliberately, not the union. A header missing a column only
    `payroll` requires is a fine file if it holds no payroll rows — and if it
    holds one, that row fails with the engine's own required-field sentence
    against the column's own name, which is the more precise answer anyway. What
    cannot be per-row is a column no row could do without, and that is what stays
    a whole-file refusal.

    Base-required fields only: a `requiredWhen` field is required by an answer
    this file has not given yet, and a route that declares one still has files
    that legitimately omit its column.
    """
    per_purpose = [set(_required_one(model)) for model in models.values()]
    common = set.intersection(*per_purpose) if per_purpose else set()
    return [PURPOSE] + [name for name in columns(models) if name in common]


def _required_one(model: forms.FormModel) -> list[str]:
    required = [f.dotted for f in rendered_model(model).fields if f.required]
    return required + [AMOUNT] + ([CONTACT] if is_gated(model) else [])


def labels(models: Mapping[str, forms.FormModel]) -> dict[str, str]:
    """`{column: what discovery calls it}` — for the report's error lines, so a
    complaint about `destination.recipient.routingNumber` reads as *ABA routing
    number* exactly as it does on the form. Two purposes that label the same
    column differently: first one wins, and the label is display-only either way.
    """
    named: dict[str, str] = {}
    for model in models.values():
        for field in rendered_model(model).fields:
            named.setdefault(field.dotted, field.label or field.dotted)
    return named | {PURPOSE: "Purpose", AMOUNT: "Amount", CONTACT: "Contact"}


def documentation_of(
    models: Mapping[str, forms.FormModel], purposes: Sequence[str]
) -> tuple[bool, list[str]]:
    """`(does any of these purposes require a document, what Conduit accepts for
    those that do)`.

    A batch-level answer for a per-row fact: the shared documents are attached
    once, to the batch, and ride only with the rows whose purpose asks for one
    (`dispatch`). So the mark-ready gate asks "does *anything* in this file need
    one", and the widget lists the accepted types of the purposes that do.
    """
    accepted: dict[str, None] = {}
    required = False
    for purpose in dict.fromkeys(purposes):
        model = models.get(purpose)
        if model is None or not model.documentation.get("required"):
            continue
        required = True
        for kind in model.documentation.get("acceptedDocumentTypes") or []:
            accepted.setdefault(str(kind), None)
    return required, list(accepted)


def header_block(
    *,
    customer_id: str,
    rail: str,
    recipient_type: str,
    digest: str,
    models: Mapping[str, forms.FormModel],
    missing: Sequence[str] = (),
) -> list[str]:
    """The `#`-prefixed rows above the header: what route this template is for,
    which discovery snapshots it was built from, what the purpose column accepts,
    and the per-purpose gating an operator cannot see from the columns alone.

    The parser reads the route and the fingerprint back out of these lines, so
    they are data as well as prose — which is why each is one `key: value` and
    the sentences live on their own lines.
    """
    lines = [
        "Conduit Console — batch payout template. Keep these # lines: the upload reads them.",
        f"customer: {customer_id}",
        f"rail: {rail}",
        f"recipientType: {recipient_type}",
        f"{FINGERPRINT_KEY}: {digest}",
        f"generated: {datetime.now(UTC).isoformat()}",
        f"One payout per row, at most {MAX_ROWS} rows and {MAX_BYTES // 1024} KB per file.",
        "Every column below comes from this route's live requirements, for every purpose "
        "below. Unknown columns are refused, never ignored.",
        # The verbatim rule, stated where the operator meets it. The label is
        # there to read; the key is what the column takes, and saying so is the
        # difference between a filled template and seven refused rows.
        "The purpose column is REQUIRED on every row and takes the RAW KEY exactly as printed "
        "below — the label after it is for reading only and is not accepted.",
    ]
    lines += [
        f"purpose value — {payments.purpose_label(purpose)}: {purpose}" for purpose in models
    ]
    if missing:
        lines.append(
            "NOT available on this route (Conduit would not answer for them, so a row naming "
            f"one is refused): {', '.join(missing)}."
        )
    lines.append(
        "A row may only fill the columns its OWN purpose declares. A column another purpose "
        "declares is refused for that row rather than dropped."
    )
    gated = [purpose for purpose, model in models.items() if is_gated(model)]
    if gated:
        lines.append(
            f"WHITELISTED recipient needed for: {', '.join(gated)}. Those rows have no recipient "
            "columns to fill: put the registered recipient's id (wlr_...) in the contact column, "
            "and the bank coordinates come from Conduit's own record."
        )
    free = [purpose for purpose in models if purpose not in gated]
    if free:
        lines.append(
            f"contact: on {', '.join(free)} it is optional — the name of a saved contact (or its "
            "id), exactly as the Contacts page shows it. A row that names one must leave every "
            "destination.recipient.* column empty; a row that fills them must leave contact empty."
        )
    required, accepted = documentation_of(models, list(models))
    if required:
        needs = [p for p, m in models.items() if m.documentation.get("required")]
        lines.append(
            f"Supporting document needed for: {', '.join(needs)}. It is attached ONCE to the "
            "batch, on the batch's own page, and rides only with the rows whose purpose needs it"
            + (f" — accepted: {', '.join(accepted)}." if accepted else ".")
        )
    return lines


# --- the upload -----------------------------------------------------------------------


@dataclass(frozen=True)
class Upload:
    """One parsed file: what its header block claimed, its columns, and its data
    rows as `{column: cell}` in file order."""

    meta: dict[str, str] = dc_field(default_factory=dict)
    columns: list[str] = dc_field(default_factory=list)
    rows: list[dict[str, str]] = dc_field(default_factory=list)
    # Rows whose cell count exceeded the header's. Kept per row rather than
    # refused for the file: one ragged line is a broken row, not a broken file.
    ragged: set[int] = dc_field(default_factory=set)

    @property
    def template_fingerprint(self) -> str:
        return self.meta.get(FINGERPRINT_KEY, "")


def parse(data: bytes) -> tuple[Upload | None, str]:
    """`(Upload, "")` or `(None, the sentence that refuses this file)`.

    UTF-8 with a BOM is expected rather than tolerated: Excel writes one on every
    "CSV UTF-8" save, so `utf-8-sig` is the decoding, not a fallback.

    Nothing here evaluates a cell. `csv.reader` returns text, the text is
    validated as data, and anything this console later writes back out goes
    through the export module's injection-safe cell writer — a spreadsheet
    formula in a legal name is a string all the way through.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, NOT_UTF8

    meta: dict[str, str] = {}
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    ragged: set[int] = set()
    try:
        # A bare CR as the only line terminator (a classic-Mac or terminal-only
        # export) never splits into lines here — `io.StringIO`'s default
        # `newline='\n'` only breaks on LF, so the whole file arrives as one
        # "line" with its CRs still embedded — and `csv.reader` raises on the
        # embedded newline it was not told to expect. That is a malformed file,
        # not a server fault, so it is caught here rather than left to crash
        # the request.
        for cells in csv.reader(io.StringIO(text)):
            if header is None:
                # Comments are only comments *above* the header. After it, a cell
                # that starts with `#` is somebody's data and is treated as such.
                if not cells or not (cells[0] or "").strip():
                    continue
                first = cells[0].strip()
                if first.startswith(COMMENT):
                    key, _, value = first.lstrip(COMMENT).strip().partition(":")
                    key = key.strip().lower()
                    value = value.strip()
                    # The fingerprint is stored in a `varchar(64)` column
                    # (`app/models.py`); it is a drift sentinel compared for
                    # equality, never parsed, so truncating a hand-edited or
                    # corrupted one to fit is harmless — it just stops matching.
                    if key == FINGERPRINT_KEY:
                        value = value[:64]
                    if value:
                        meta[key] = value
                    continue
                header = [c.strip() for c in cells]
                continue
            if not any((c or "").strip() for c in cells):
                continue  # a blank line between records, or the file's last newline
            if len(rows) >= MAX_ROWS:
                return None, TOO_MANY_ROWS
            if len(cells) > len(header) and any((c or "").strip() for c in cells[len(header) :]):
                ragged.add(len(rows) + 1)
            rows.append(
                {name: (cells[i].strip() if i < len(cells) else "") for i, name in enumerate(header)}
            )
    except csv.Error:
        return None, MALFORMED_CSV

    if header is None:
        return None, NO_HEADER
    # A file with no rows is *not* refused here: its header may also be wrong,
    # and "this route does not have a column called swiftCode" is the more
    # useful of the two answers. The caller refuses emptiness after the column
    # checks (`app/web/batches.py`).
    return Upload(meta=meta, columns=header, rows=rows, ragged=ragged), ""


def _listed(names: Sequence[str]) -> str:
    shown = [f"'{n}'" for n in names[:5]]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")


def _quotable_purpose(purpose: str) -> str:
    """The purpose cell as `UNKNOWN_PURPOSE` may quote it: verbatim when it is
    one of Conduit's own purpose keys, masked when it is anything else.

    The same test the DTO walker makes and the same one `blocked_message`
    makes — decided on membership of a published, bounded vocabulary, never on
    the shape of the value. `prefunding` on a corridor that refuses it is a
    purpose key this console prints in every template header block, so quoting
    it back discloses nothing about the operator and keeps the message the one
    it has always been. Anything else in that cell is arbitrary typed text —
    the label instead of the key, a typo, or a column-shifted paste that put an
    account number there — and only its tail is quoted.

    A masked tail rather than no echo at all, for the reason `UNKNOWN_CONTACT`
    keeps one: `'••••4567' is not a purpose` and `'••••bels' is not a purpose`
    are different faults with different fixes, and an operator reading 40
    refused rows out of a results CSV is grouping them.
    """
    return purpose if purpose in payments.PURPOSE_VALUES else counterparties.mask(purpose)


def check_columns(upload: Upload, allowed: Sequence[str], required: Sequence[str]) -> str:
    """`""`, or the refusal. Unknown first, because a file with a stray column is
    a file whose author believed something was being sent."""
    seen = [c or "(blank)" for c in upload.columns]
    unknown = [c for c in seen if c not in allowed]
    if unknown:
        return UNKNOWN_COLUMNS.format(columns=_listed(unknown))
    duplicates = sorted({c for c in seen if seen.count(c) > 1})
    if duplicates:
        return DUPLICATE_COLUMNS.format(columns=_listed(duplicates))
    missing = [c for c in required if c not in seen]
    if missing:
        return MISSING_COLUMNS.format(columns=_listed(missing))
    return ""


def check_route(upload: Upload, expected: Mapping[str, str]) -> str:
    """`""`, or the refusal naming every part of the route the file disagrees
    with. A file built for another customer, rail or recipient type is not
    this batch, whatever its columns happen to be."""
    differences = [
        f"{key}: {upload.meta.get(key.lower()) or '(none)'}"
        for key, want in expected.items()
        if (upload.meta.get(key.lower()) or "").strip().lower() != str(want).strip().lower()
    ]
    return WRONG_ROUTE.format(differences="; ".join(differences)) if differences else ""


# --- row validation --------------------------------------------------------------------


@dataclass
class Validator:
    """One route's judgement of a row, made of the same parts the single-payout
    form is made of — with the row's **own purpose** choosing which model does
    the judging.

    Every sentence a row can carry is `forms.validate`'s, `payments`' or one of
    the batch-only ones above — the design owner's requirement is that a batch
    error reads exactly like the single-payout error for the same fault, and the
    way to guarantee that is to call the same validator rather than to match its
    wording.

    `contacts` and `entries` are read **once for the batch**: a 200-row file
    resolves every contact against one local query and one whitelist read, and
    the whitelist read happens only when some purpose on this route is gated.
    """

    models: dict[str, forms.FormModel]
    rail: str = ""
    contacts: dict[str, dict] = dc_field(default_factory=dict)
    entries: list[dict] = dc_field(default_factory=list)

    def __post_init__(self) -> None:
        self.labels = labels(self.models)
        self.rendered = {p: rendered_model(m) for p, m in self.models.items()}
        self.gated = {p: is_gated(m) for p, m in self.models.items()}
        self.allowed = {p: set(purpose_columns(m)) for p, m in self.models.items()}
        self.recipient_columns = {
            p: [f.dotted for f in m.fields if "recipient" in f.path]
            for p, m in self.rendered.items()
        }

    def _at(self, column: str, detail: str) -> dict:
        return {"column": column, "label": self.labels.get(column, column), "detail": detail}

    def row(self, cells: Mapping[str, str], *, ragged: bool = False) -> dict:
        """One validated row, ready to persist: `{purpose, payload, amount,
        contact_id, contact_label, errors}`."""
        errors: list[dict] = [self._at("", RAGGED_ROW)] if ragged else []
        purpose = (cells.get(PURPOSE) or "").strip()
        model = self.models.get(purpose)
        if model is None:
            # No model, no judgement — and saying "and also your ABA is wrong"
            # here would be judging the row against a schema it never claimed.
            errors.append(
                self._at(
                    PURPOSE,
                    NO_PURPOSE
                    if not purpose
                    else UNKNOWN_PURPOSE.format(
                        name=_quotable_purpose(purpose),
                        allowed=", ".join(self.models) or "none",
                    ),
                )
            )
            return {
                "purpose": purpose,
                "payload": None,
                "amount": None,
                "contact_id": "",
                "contact_label": "",
                "errors": errors,
            }

        rendered = self.rendered[purpose]
        gated = self.gated[purpose]
        chosen = (cells.get(CONTACT) or "").strip()
        # The per-row half of the unknown-column rule: a column another purpose
        # declares, filled in on a row whose purpose does not have it.
        foreign = [
            column
            for column in cells
            if column not in self.allowed[purpose] and (cells.get(column) or "").strip()
        ]
        if foreign:
            errors.append(
                self._at("", FOREIGN_COLUMNS.format(purpose=purpose, columns=_listed(foreign)))
            )
        values = forms.parse_submission(
            rendered,
            [
                (f"f.{column}", value)
                for column, value in cells.items()
                if column not in (PURPOSE, AMOUNT, CONTACT) and column in self.allowed[purpose]
            ],
        )
        contact_id = contact_label = ""

        if gated:
            if not chosen:
                errors.append(self._at(CONTACT, GATED_CONTACT))
            else:
                # The whitelist was read once for the batch; the refusals are
                # `payments`' own, so a bad row says here what it says on the
                # payout form.
                entry, refusal = payments.pick_recipient(self.entries, chosen, self.rail)
                if refusal is not None:
                    errors.append(self._at(CONTACT, refusal.detail))
                else:
                    payments.apply_recipient(model, values, entry or {})
                    contact_id = chosen
                    contact_label = str(
                        (entry or {}).get("label") or (entry or {}).get("legalName") or ""
                    )
        elif chosen:
            typed = [
                c for c in self.recipient_columns[purpose] if (cells.get(c) or "").strip()
            ]
            saved = self.contacts.get(chosen.lower())
            if typed:
                errors.append(self._at(CONTACT, EXCLUSIVE.format(columns=_listed(typed))))
            elif saved is None:
                errors.append(
                    self._at(CONTACT, UNKNOWN_CONTACT.format(name=counterparties.mask(chosen)))
                )
            elif not saved.get("recipient"):
                errors.append(self._at(CONTACT, UNREADABLE_CONTACT))
            else:
                # Prefill, exactly as `?counterparty=` does on the form — and
                # then validated like anything else, so a saved destination with
                # a coordinate this route refuses fails here rather than at
                # Conduit.
                values.root.setdefault("destination", {})["recipient"] = dict(saved["recipient"])
                contact_id, contact_label = str(saved["id"]), str(saved["label"] or "")

        found = forms.validate(rendered, values)
        for name, messages in found.fields.items():
            column = name[2:] if name.startswith("f.") else name
            errors += [self._at(column, message.detail) for message in messages]
        errors += [self._at("", message.detail) for message in found.form + found.documents]

        amount = payments.amount(cells.get(AMOUNT) or "")
        if amount is None:
            errors.append(self._at(AMOUNT, payments.AMOUNT_MESSAGE))
        if blocked := payments.blocked_country(model, values):
            errors.append(self._at("", payments.blocked_message(blocked)))

        return {
            "purpose": purpose,
            # The assembled body fragment — `{"destination": {...}}` — which is
            # what `payout_body` needs and all this row is.
            "payload": forms.assemble(model, values),
            "amount": amount,
            "contact_id": contact_id,
            "contact_label": contact_label[:128],
            "errors": errors,
        }


# --- totals ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    """What an operator is about to approve, and what is deliberately not in it.

    `by_currency` sums the **valid rows only** — a row with any error is not
    going to be dispatched, so counting its amount in a total presented for
    confirmation would be describing a payment that will not happen. `invalid` is
    rendered beside the sum, named rather than merely absent: a number an
    operator approves has to say what it left out.
    """

    rows: int = 0
    valid: int = 0
    invalid: int = 0
    by_currency: dict[str, str] = dc_field(default_factory=dict)


def totals(rows: Sequence[Mapping], asset: str) -> Totals:
    """Decimal throughout: money is a decimal string end to end in this console
    (`payments.amount`), and a float sum of two-decimal strings is the classic
    way to present a total that is not the sum.

    Two things about the arithmetic, both found by a review. Decimal's
    DEFAULT context is 28 significant digits, so a wide enough sum silently
    ROUNDS — the operator approves a number that is not the sum after all, which
    is the exact failure this function exists to prevent. And `str(Decimal)`
    emits scientific notation past that width (`3.70E+29`), which would reach
    both the confirm screen and `payments.over_ceiling`. `prec=60` is past any
    plausible batch (a 200-row file of 15-digit amounts needs ~18), and
    `format(…, "f")` is fixed-point always.
    """
    valid = 0
    with localcontext() as ctx:
        ctx.prec = 60
        total = Decimal("0")
        for row in rows:
            if row.get("errors"):
                continue
            try:
                total += Decimal(str(row.get("amount") or ""))
            except (InvalidOperation, ValueError):  # pragma: no cover - a valid row has an amount
                continue
            valid += 1
        rendered = format(total, "f")
    return Totals(
        rows=len(rows),
        valid=valid,
        invalid=len(rows) - valid,
        by_currency={asset: rendered} if valid else {},
    )


# --- persistence ---------------------------------------------------------------------------


async def create(
    session: AsyncSession,
    *,
    customer_id: str,
    rail: str,
    recipient_type: str,
    virtual_account_id: str,
    asset: str,
    digest: str,
    template_digest: str,
    filename: str,
    documents_required: bool,
    accepted_document_types: Sequence[str] = (),
    actor_id: str,
    actor_email: str,
    rows: Sequence[Mapping],
) -> PayoutBatch:
    """The batch and every one of its rows, in the caller's transaction.

    Committing is the caller's, like every other write in this console: the batch,
    its rows and its audit row land together or not at all.
    """
    batch = PayoutBatch(
        id=uuid.uuid4(),
        customer_id=customer_id,
        rail=rail,
        recipient_type=recipient_type,
        # Country is not part of a batch's corridor (module docstring) — the
        # column stays for the batches that still carry a real one from before
        # this was true, and every new batch writes the empty string.
        destination_country="",
        virtual_account_id=virtual_account_id,
        asset=asset,
        fingerprint=digest,
        template_fingerprint=template_digest,
        filename=filename[:255],
        documents_required=documents_required,
        accepted_document_types=[str(kind) for kind in accepted_document_types or []],
        document_ids=[],
        status="validating",
        actor_id=actor_id,
        actor_email=actor_email,
    )
    session.add(batch)
    for number, row in enumerate(rows, start=1):
        session.add(
            PayoutBatchRow(
                batch_id=batch.id,
                row_number=number,
                # The row's own purpose, verbatim as the cell held it — including
                # a purpose this route has none for, because the row's errors say
                # so and the report must show what the file actually said.
                purpose=(row.get("purpose") or "")[:64],
                payload=row.get("payload") or None,
                amount=row.get("amount"),
                contact_id=(row.get("contact_id") or None),
                contact_label=(row.get("contact_label") or None),
                errors=list(row.get("errors") or []),
            )
        )
    return batch


async def get(session: AsyncSession, customer_id: str, batch_id: str) -> PayoutBatch | None:
    """One batch **of this customer**, or None — the same route-level isolation
    guard `counterparties.get` makes: a URL naming customer B and a batch of
    customer A resolves to nothing, exactly as a made-up id does."""
    try:
        key = uuid.UUID(str(batch_id))
    except (ValueError, AttributeError):
        return None
    return (
        await session.execute(
            select(PayoutBatch).where(
                PayoutBatch.id == key, PayoutBatch.customer_id == customer_id
            )
        )
    ).scalar_one_or_none()


async def rows_of(session: AsyncSession, batch_id: uuid.UUID) -> list[dict]:
    """This batch's rows in file order, payloads decrypted **tolerantly**.

    Raw blob + `crypto.read_json`, the address book's idiom: letting
    `EncryptedJSON` decrypt inside the query means one corrupt or key-rotated row
    raises out of `session.execute` and takes the whole report down — on a page
    that has to decrypt to render at all.
    """
    found = await session.execute(
        select(
            PayoutBatchRow.id,
            PayoutBatchRow.row_number,
            PayoutBatchRow.purpose,
            PayoutBatchRow.amount,
            PayoutBatchRow.contact_id,
            PayoutBatchRow.contact_label,
            PayoutBatchRow.errors,
            PayoutBatchRow.operation_id,
            PayoutBatchRow.dispatch_error,
            # The row's dispatch outcome is the *operation's* — one outer join
            # rather than a state column this console would have to keep in step
            # with the ledger. A row whose operation a webhook resolved while
            # this page was open therefore reads correctly with no batch write
            # at all.
            Operation.state.label("operation_state"),
            Operation.conduit_resource_id.label("resource_id"),
            Operation.error.label("problem"),
            # Whether this row's request ever left the process. One more column
            # off the join that is already here, read by exactly one caller
            # (`problem_title`'s abandoned arm), and it has to be read: an
            # operation abandoned out of `created` was never sent, one an admin
            # abandoned out of `stalled` was, and those are opposite sentences
            # to put in a results file about somebody's payroll.
            Operation.in_flight_at,
            type_coerce(PayoutBatchRow.payload, LargeBinary).label("blob"),
            PayoutBatch.status.label("batch_status"),
        )
        .select_from(PayoutBatchRow)
        .outerjoin(Operation, Operation.id == PayoutBatchRow.operation_id)
        .join(PayoutBatch, PayoutBatch.id == PayoutBatchRow.batch_id)
        .where(PayoutBatchRow.batch_id == batch_id)
        .order_by(PayoutBatchRow.row_number)
    )
    rows = []
    for row in found.all():
        payload = read_json(row.blob) if row.blob is not None else None
        rows.append(
            {
                "id": row.id,
                "row_number": row.row_number,
                "purpose": row.purpose,
                "amount": row.amount,
                "contact_id": row.contact_id,
                "contact_label": row.contact_label,
                "errors": list(row.errors or []),
                "operation_id": row.operation_id,
                "dispatch_error": row.dispatch_error,
                "operation_state": row.operation_state,
                "attempted": row.in_flight_at is not None,
                "transaction_id": row.resource_id,
                # The translated view, never the stored vendor body: the detail
                # cell and `problem_title` (and through it the results CSV) both
                # print this.
                "problem": problem_of(row.problem, row.resource_id),
                "state": state_of(row.errors, row.dispatch_error, row.operation_state),
                "payload": payload,
                # None means the ciphertext could not be read, never "no
                # destination": the column is written on every row.
                "unreadable": row.blob is not None and payload is None,
                # **The third state.** `purge_row_payloads`
                # NULLs the column on a finished batch, and a NULL blob rendered
                # as a blank name and blank coordinates — identical to a row that
                # never had a destination, on the surface an operator goes to in
                # order to find out where the money went. A finished batch is the
                # only thing that empties it, so the pair says which of the two
                # this is.
                # A batch holding an abandoned row may never reach a
                # terminal status, and `purge_row_payloads` now empties such a
                # row on its operation's own clock — so batch terminality alone
                # would render it as "never had a destination", which is the
                # third state this pair exists to prevent.
                "purged": row.blob is None
                and (
                    row.batch_status in TERMINAL_BATCH_STATUSES
                    or row.operation_state == "abandoned"
                ),
                "recipient": ((payload or {}).get("destination") or {}).get("recipient") or {},
            }
        )
    return rows


# --- the batch state machine ----------------------------------------------------------
#
# Small, explicit, and enforced in one place, because this is a *safety* rule
# rather than a UI preference: **abandoning is legal only before dispatch
# starts.** Once a row has an operation, "abandoned" would be a word this
# console cannot make true — the money is Conduit's business now, and the exits
# are the rows' own terminal states plus the batch's completion.

LEGAL_STATUS: dict[str, frozenset[str]] = {
    "validating": frozenset({"ready", "abandoned"}),
    "ready": frozenset({"partially_dispatched", "abandoned"}),
    # Self-transition: a re-dispatch of a half-finished batch re-enters the same
    # state, and the loop is idempotent by intent (see `dispatch`).
    "partially_dispatched": frozenset({"partially_dispatched", "dispatched"}),
    "dispatched": frozenset(),
    "abandoned": frozenset(),
}


class IllegalBatchTransition(Exception):
    def __init__(self, batch_id, from_status: str, to_status: str) -> None:
        super().__init__(f"batch {batch_id}: {from_status} -> {to_status} is not legal")
        self.from_status, self.to_status = from_status, to_status


async def set_status(session: AsyncSession, batch: PayoutBatch, status: str) -> bool:
    """Move a batch's status, **and only from the status the caller decided on**.
    True if this call moved it; False if somebody else moved it first.

    This used to be a plain attribute write, flushed by the caller's commit. That
    is `UPDATE payout_batches SET status = ... WHERE id = ...`, which does not
    care what the row said when the decision to write it was made — and the two
    privileged buttons that reach here, dispatch and abandon, each decide on a
    status they read into their own session and then write it minutes of wall
    clock later. Every crossing of those two requests ended with the loser
    winning: an `abandoned` batch whose rows were all confirmed at Conduit, an
    `abandoned` batch a running loop then flushed `dispatched` over, and an
    operator told "Nothing was sent." about two payments that had been.

    So the expected status goes into the WHERE clause and the database arbitrates.
    One statement, no lock held across a status check — and specifically not
    across a dispatch run, which is minutes long and would be the wrong thing to
    hold a row lock for. A concurrent writer either has not committed (our UPDATE
    waits on its row lock, then re-reads and finds a status that is no longer the
    one we asked for) or has (we never match), and both come out as zero rows.

    **Zero rows is a refusal, not an error.** It means a legal transition arrived
    a moment too late, which is a thing two operators can honestly do to each
    other, and the caller answers it with the sentence it already has for a batch
    in the status it turned out to be in. That is a different failure from an
    ILLEGAL transition, which still raises: nothing may ask this console to move
    a batch somewhere `LEGAL_STATUS` refuses, so anything doing so is a bug, and
    a batch silently changing state around a dispatch is the class of bug this
    feature cannot have. Collapsing the two would hide the second inside the
    first.

    The caller's in-memory copy is re-read either way, because it is what the
    caller goes on to state: `_run` audits `batch.status` as the run's outcome,
    and both routes phrase their refusal out of it. A stale attribute here would
    put the losing race's answer into the record in exactly the words the
    guarded UPDATE exists to stop.
    """
    if status not in LEGAL_STATUS.get(batch.status, frozenset()):
        raise IllegalBatchTransition(batch.id, batch.status, status)
    moved = await session.execute(
        update(PayoutBatch)
        .where(PayoutBatch.id == batch.id, PayoutBatch.status == batch.status)
        .values(status=status)
        # The ORM copy is refreshed below from the table rather than from this
        # statement's criteria: `synchronize_session` would evaluate the WHERE in
        # Python and set `status` on an object whose row this UPDATE did not
        # touch, which is the stale write again wearing the fix's clothes.
        .execution_options(synchronize_session=False)
    )
    await session.refresh(batch, ["status"])
    return moved.rowcount == 1


def set_documents(batch: PayoutBatch, ids: Sequence[str]) -> None:
    batch.document_ids = list(ids)


async def summaries(
    session: AsyncSession, customer_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict]:
    """This customer's batches, newest first, with their row counts.

    The counts are **derived**, not stored: rows are the truth about a batch, and
    a cached count is a number that can quietly stop agreeing with them. One
    grouped query for the whole page, never one per batch.

    `offset` exists because the old `limit` was a **silent** ceiling: batch 51 was not
    on the page and the page did not say so, which is the one truncation this console
    does not allow anywhere else. `created_at` ties are broken by `id` so a page turn
    cannot drop or repeat a batch.
    """
    batches = (
        (
            await session.execute(
                select(PayoutBatch)
                .where(PayoutBatch.customer_id == customer_id)
                .order_by(PayoutBatch.created_at.desc(), PayoutBatch.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if not batches:
        return []
    counted = await session.execute(
        select(
            PayoutBatchRow.batch_id,
            func.count(),
            # `jsonb_array_length` over the errors column: an invalid row is one
            # with at least one error, and the database can answer that without
            # this process reading a single row.
            func.count().filter(func.jsonb_array_length(PayoutBatchRow.errors) > 0),
        )
        .where(PayoutBatchRow.batch_id.in_([b.id for b in batches]))
        .group_by(PayoutBatchRow.batch_id)
    )
    tally = {batch_id: (total, bad) for batch_id, total, bad in counted}
    return [
        {
            "batch": batch,
            "rows": tally.get(batch.id, (0, 0))[0],
            "invalid": tally.get(batch.id, (0, 0))[1],
        }
        for batch in batches
    ]


# --- dispatch ---------------------------------------------------------------------------
#
# One `payout_create` per valid row, sent sequentially, **exactly once, forever**.
#
# The whole guarantee rides on one line: the operation's intent nonce is a
# function of `(batch id, row number)` rather than of a form render
# (OPERATIONS_SPEC §1). A form's nonce is minted per render, which is what makes
# a *fresh render* an intentional new attempt; a batch row has no render, and
# there must never be an intentional second attempt at it — so its nonce is
# derived, deterministic across processes and days, and `operations.start`
# resolves to the existing operation in every state. Re-clicking dispatch,
# reloading mid-run, a worker dying between two rows, or dispatching again after
# a partial failure therefore all reach the same place: `is_new=False`, nothing
# on the wire, the row keeps the operation it already had.

# What that reasoning does not buy is secrecy: the nonce is derived from
# published inputs (a batch id and a row number both render on `console.view`
# pages) over a namespace committed in this repo, so it is computable by anyone
# who can read a batch. Making it secret would cost the durability that is the
# whole reason it is derived — see `intent_for`.
#
# So the invariant is checked rather than assumed. Every `is_new=False` is
# compared against the request hash of what this row would have sent
# (`NONCE_SPENT_ELSEWHERE`): resolving to an existing operation and that
# operation being this row's payment are two different facts.
#
# Forms now refuse a nonce this console did not mint (`web.intent_of`), so a
# freshly computed one has no submit channel — but the check is not redundant.
# `operation_intents` rows written before sealed tokens shipped can already map
# a computed nonce to a foreign operation, and dispatch resolves those rows the
# same way it resolves any other.

# A fixed, never-regenerated namespace. Written out as a literal because that is
# the point: the same batch and row must fingerprint identically in this process,
# in the worker, and in next year's build. Not a secret, and never was: see
# above.
BATCH_INTENT_NAMESPACE = uuid.UUID("9c1a4f6e-3b52-4d18-9f7a-2c0e5b8d41a3")

# The gap between rows. Operator-triggered work is still traffic, and a 500-row
# batch fired flat out is this console picking a fight with the rate limiter on
# behalf of a page nobody is watching. The client's own retry/backoff handles a
# 429 either way; this is politeness, not the mechanism.
# A constant, not a setting — one number, one caller, and the tests set
# it to 0. Make it configurable the day an operator asks for a different pace.
PACE_SECONDS = 0.25

# Row states. This console's own vocabulary (the pill kind `payout_batch_rows`),
# derived rather than stored: the operation is the truth about a dispatched row,
# and a state column here would be a second copy of it free to fall behind.
ROW_STATES = ("invalid", "pending", "sending", "sent", "rejected", "unconfirmed", "abandoned", "refused")

# operation state -> row state.
_BY_OPERATION = {
    "created": "sending",
    "in_flight": "sending",
    "confirmed": "sent",
    "rejected": "rejected",
    "outcome_unknown": "unconfirmed",
    "stalled": "unconfirmed",
    "abandoned": "abandoned",
}
# The row states that are an ANSWER: a terminal outcome, or an unknown the
# reconciler already owns (OPERATIONS_SPEC §3's `payout_create` recipe). "Done
# with" is not the same as "settled" — settlement is the transaction's, on the
# transaction's own page.
#
# Disjoint from `SENDABLE_ROW_STATES` by construction, and that is the whole
# point of the pair: a row is either owed or answered, never both. `refused` was
# in both sets once, which made a batch that still owed a payment go
# `dispatched` — green, terminal, and no longer in the routes' `DISPATCHABLE` —
# while the row's own copy told the operator to dispatch it again. `sending` is
# in neither: a row on the wire is not owed and not answered yet, so it must not
# make a batch look finished either.
#
# **`abandoned` is in neither set, for `sending`'s reason.** It was in
# here, and it is not an answer about a payment — it is this console recording
# that it stopped keeping the question open. Both roads to it say so. A
# `created` operation that expired (`abandon_expired`, OP_CREATED_TTL) was never
# on the wire at all, so the row is a payroll payment nobody made and nobody
# will; a `stalled` one an admin abandoned went out and never came back, so the
# row is a payment this console cannot say either way about. Counting either as
# settled let `complete` flip the batch to a green `dispatched` over it — the
# exact false terminal pill `refused` was taken out of this set for.
#
# Removed from the member list rather than subtracted at the one use site:
# `complete` is this set's only reader, so the two are the same change today,
# and this set is named for a property of a row state rather than for one
# function's rule. A second reader asking "is this row answered?" must inherit
# the true answer, not re-derive the exclusion.
#
# The cost is stated where it lands: a batch holding an abandoned row can no
# longer reach `dispatched`, and no dispatch of it will clear the row (the
# operation exists, so the loop resolves to it and does not send). It stays
# `partially_dispatched`, which is true and is what `refused` already does.
SETTLED_ROW_STATES = frozenset({"invalid", "sent", "rejected", "unconfirmed"})
# The rows a dispatch would actually put on the wire. `refused` is in here and
# `rejected` is not, and that pair is the whole ruling: a row this console
# declined to send never reached Conduit, so re-resolving its contact and trying
# again risks nothing; a row Conduit *refused* is final in this batch, because
# the only way to re-send it would be under an identity that already exists.
SENDABLE_ROW_STATES = frozenset({"pending", "refused"})

# --- the sentences a dispatched row can carry ---------------------------------------------

CONTACT_GONE = (
    "This row was addressed to a saved contact that is no longer available ({label}), so it was "
    "not sent. Nothing about it reached Conduit. Restore or re-save the contact and dispatch "
    "again, or correct the file and upload a new batch."
)
GATED_CONTACT_GONE = (
    "Conduit no longer offers the registered recipient this row names: {reason} It was not sent, "
    "and nothing about it reached Conduit."
)
CONTACT_CHANGED = (
    "{label} changed since this batch was validated — re-upload to pay the new coordinates. "
    "This row was not sent and nothing about it reached Conduit: the total you confirmed was "
    "for the destination this file was validated against, and that is no longer what this "
    "contact says."
)
NO_DESTINATION = (
    "This row's stored destination could not be read on this installation, so there was nothing "
    "to send. Upload the file again to rebuild it."
)
REJECTED_IS_FINAL = (
    "Conduit refused this row. A refused row is final in this batch: it keeps the operation that "
    "carries the refusal, and re-sending it under the same identity is exactly what this batch "
    "cannot do. Export the results, correct those rows, and upload them as a new batch."
)
UNREADABLE_WHITELIST_AT_DISPATCH = (
    "The whitelist could not be read, so no row of this batch was sent — every row on this route "
    "is addressed to a registered recipient, and dispatching without checking them would be "
    "guessing. Nothing has changed; try again."
)
NO_REQUIREMENTS_AT_DISPATCH = (
    "This route's requirements could not be read, so nothing was sent. The destination on a "
    "gated row comes from Conduit's own record at send time, and this console will not send a "
    "payout it could not resolve one for."
)
# The funding account, re-read at dispatch. It is picked at
# *upload* — the totals an operator approves are in a currency, and the account is
# the only thing that states one — and a batch can sit ready for days. Every other
# fact this loop needs is read live at dispatch precisely because the stored copy
# may have gone stale; the one account that every row debits was the exception,
# taken on trust from a row written before any of this happened.
UNREADABLE_ACCOUNT_AT_DISPATCH = (
    "The funding account could not be read, so no row of this batch was sent — every payment "
    "here debits that one account, and dispatching without confirming it would be guessing. "
    "Nothing has changed; try again."
)
NO_SUCH_ACCOUNT_AT_DISPATCH = (
    "The funding account this batch was built against no longer exists, so nothing was sent. "
    "Upload the file again against an account this customer still holds."
)
INACTIVE_ACCOUNT_AT_DISPATCH = (
    "The funding account is {status}, not active, so nothing was sent. A batch is approved "
    "against the account that funds it, and that account can be closed between approval and "
    "dispatch. Upload the file again against an active account."
)
ACCOUNT_ASSET_AT_DISPATCH = (
    "The funding account holds {actual}, but this batch's amounts were validated and totalled "
    "as {expected} — so nothing was sent. A CSV amount column carries digits, never a currency: "
    "the account is what states one, and it no longer states the one this file was approved in."
)
# This row's dispatch nonce had already been spent by a different request.
#
# A row's nonce is `intent_for` — `uuid5` over a namespace committed in this
# repo, of "{batch id}:{row number}" — and both halves of that string render on
# pages a `console.view` role can read. So the nonce is computable by anyone who
# can see the batch, and the forms that mint a `payout_create` or a
# `document_upload` take a nonce verbatim out of a hidden field. Spending a row's
# nonce first is therefore reachable, and the two shapes it arrives in — an
# operation of the same type about different money, or an operation of another
# type entirely — both land on this one sentence.
#
# It is a `dispatch_error` because that is exactly what it is: this console
# declined to send, which is neither a vendor refusal nor a validation
# complaint. That also means it needs no new plumbing to be *seen* — `state_of`
# reads it as `refused`, the detail page renders it in the `refused` arm, and
# `problem_title` puts it in the results file's `problem` column.
#
# It names the foreign operation because that is the only actionable fact in it.
# The nonce is a pure function of the batch and the row, so it is the same value
# on every future dispatch of this batch and no amount of re-clicking can clear
# it; an operator who cannot see what claimed it has nothing to do but press a
# button that will refuse again. Deliberately not `web.ALREADY_SPENT_ELSEWHERE`,
# which is a form's sentence — it tells the operator to reload and press again,
# and here there is nothing a reload would change.
#
# Written tight on purpose: `PayoutBatchRow.dispatch_error` is `String(500)` and
# a uuid plus an operation type eats 60 of them, so this is the one sentence here
# whose length is a constraint and not a preference.
NONCE_SPENT_ELSEWHERE = (
    "This row was not sent and nothing about it reached Conduit. The submission token that "
    "identifies it for dispatch had already been used for a different request — operation "
    "{op_id}, a {op_type} — so sending it would have meant reporting that request's outcome as "
    "this payment's. The token is derived from the batch id and the row number, so every future "
    "dispatch of this batch will refuse this row too. Upload it as a new batch."
)

# This row's submission was abandoned, in the two ways that can happen.
#
# `abandoned` is the operations ledger's word for "this console stopped holding
# the question open", and a row wearing it is the one row state that carries no
# sentence of its own: it has no `dispatch_error` (the link write clears it), no
# Conduit problem (nothing was refused), and no validation complaint (it was
# valid). It rendered a bare pill and an empty `problem` column — on the two
# surfaces an operator reads to find out which payments were made.
#
# Two sentences and not one, because the two roads are opposite facts about
# money and the row cannot say which it is. `created -> abandoned` is the TTL
# job on an operation that was never sent (`attempted` is False: no
# `in_flight_at`, and this is the strand the dispatch loop now prevents from
# being created in the first place — these are the ones a pre-fix build left
# behind). `stalled -> abandoned` is an admin declaring a submission dead that
# DID go out and never resolved. "Nothing reached Conduit" is true of the first
# and a lie about the second.
#
# Neither promises a retry, because this batch has none to offer: the nonce is
# spent on the abandoned operation, so every future dispatch of this batch
# resolves to it and sends nothing (and `abandoned -> in_flight` is not a legal
# transition anyway). Both fit `String(500)`.
ABANDONED_NEVER_SENT = (
    "This row was never sent — nothing about it reached Conduit and no payment was made. Its "
    "submission was recorded and then expired unsent past this console's send window, which "
    "happens when a dispatch run is interrupted between recording a row and putting it on the "
    "wire. This batch cannot send it again: its submission token is spent. Pay this row by "
    "uploading it as a new batch."
)
ABANDONED_AFTER_SENDING = (
    "This row was sent and its outcome was never established: Conduit did not answer, "
    "reconciliation gave up, and the submission was then abandoned. This console cannot say "
    "whether the payment was made, and this batch will not send it again. Open the submission "
    "record and check Conduit's own transactions for this row before paying it anywhere else."
)


def intent_for(batch_id, row_number: int) -> uuid.UUID:
    """The operation nonce for one row of one batch — `uuid5(namespace,
    "{batch}:{row}")`.

    Deterministic on purpose and forever: this is the value that makes a second
    dispatch of the same row resolve to the operation the first one made, in
    whatever state it is in, from any process. It is also why a row number is
    never renumbered and why a correction is a new batch (OPERATIONS_SPEC §1) —
    rewriting row 3 in place would change what an already-issued nonce means.
    """
    return uuid.uuid5(BATCH_INTENT_NAMESPACE, f"{batch_id}:{row_number}")


def state_of(errors, dispatch_error: str | None, operation_state: str | None) -> str:
    """One row's state, in this console's own words. Order matters: a row that
    never validated was never dispatchable, and a row refused before sending has
    no operation to ask."""
    if errors:
        return "invalid"
    if operation_state:
        return _BY_OPERATION.get(operation_state, "unconfirmed")
    # Checked *after* the operation: a refusal is cleared when a later dispatch
    # succeeds, but a stale one must never outrank a real send.
    if dispatch_error:
        return "refused"
    return "pending"


def tally(rows: Sequence[Mapping]) -> dict[str, int]:
    """`{state: count}` over every state, zeros included — a summary line that
    omitted the zeros would change shape as a batch progressed."""
    counts = dict.fromkeys(ROW_STATES, 0)
    for row in rows:
        counts[row.get("state") or "pending"] = counts.get(row.get("state") or "pending", 0) + 1
    return counts


def purpose_tally(rows: Sequence[Mapping]) -> list[tuple[str, int]]:
    """`[(purpose, count)]` in the order the file first named each — what this
    batch actually holds, which is the honest replacement for the single purpose
    a batch used to carry. A row whose purpose cell was empty counts under `""`
    and renders as such: the report shows what the file said."""
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get("purpose") or ""
        counts[key] = counts.get(key, 0) + 1
    return list(counts.items())


def complete(rows: Sequence[Mapping]) -> bool:
    """Whether every row has an answer — nothing owed, nothing still running.

    This is what the terminal `dispatched` status is allowed to mean, so it is
    deliberately stricter than "dispatch has stopped": a `refused` row is a
    payment this console declined to send and will retry
    (`SENDABLE_ROW_STATES`), a `sending` row is one still in flight, and an
    `abandoned` row is a submission this console gave up on rather than a
    payment it made. None of the three is an answer, and a batch holding
    any of them has not been dispatched.
    """
    return all(row.get("state") in SETTLED_ROW_STATES for row in rows)


async def _destination(
    session: AsyncSession,
    batch: PayoutBatch,
    row: Mapping,
    *,
    model: forms.FormModel,
    entries: Sequence[Mapping],
) -> tuple[dict | None, str]:
    """`(the assembled body fragment to send, "")`, or `(None, the refusal)`.

    **Contacts are resolved at dispatch, not trusted from upload.** A batch can
    sit ready for a day, and a contact can be archived or a whitelist
    registration revoked in that time. The row names its contact by id, so the
    answer is a fresh read of that id — never the label (which a rename moves)
    and never a guess.
    """
    payload = row.get("payload")
    if payload is None:
        return None, NO_DESTINATION
    values = forms.FormValues(root=dict(payload))
    chosen = (row.get("contact_id") or "").strip()
    if not chosen:
        return values.root, ""
    if is_gated(model):
        entry, refusal = payments.pick_recipient(entries, chosen, batch.rail)
        if refusal is not None:
            return None, GATED_CONTACT_GONE.format(reason=refusal.detail)
        # Conduit's own record of the destination, written over whatever the
        # batch stored — the same unconditional server-side rule the single
        # payout form applies (`payments.apply_recipient`).
        payments.apply_recipient(model, values, entry or {})
        return values.root, ""
    saved = await counterparties.get(session, batch.customer_id, chosen)
    if saved is None or not saved.get("recipient"):
        return None, CONTACT_GONE.format(label=row.get("contact_label") or chosen)
    # **Drift invalidates.** A contact can be *edited* now, not only renamed or
    # archived — so the coordinates this row was validated against can move
    # while the batch sits ready, and neither payload may quietly win. Paying
    # the stored row sends money to an account the address book has since
    # corrected; paying the new one sends money somewhere this operator never
    # confirmed, and what they confirmed is a total against a stated
    # destination. So the row is refused, in the retryable class — nothing
    # reached Conduit — and the fix is a re-upload against today's record.
    #
    # `same_destination` is the transfer gate's own identity test
    # (`IDENTITY_KEYS`), which is what makes a **rename** pass: a label says
    # nothing about where the money goes.
    if not counterparties.same_destination(
        counterparties.recipient_of(values.root), saved["recipient"]
    ):
        return None, CONTACT_CHANGED.format(label=row.get("contact_label") or chosen)
    return values.root, ""


async def _pace() -> None:
    """The gap between two rows.

    Its own function so it is a *named boundary*: this is the exact point a
    killed process stops between rows — one row fully recorded, the next not yet
    started — which is what the crash drill in the tests reaches for.
    """
    if PACE_SECONDS:
        await asyncio.sleep(PACE_SECONDS)


async def _refuse(session: AsyncSession, row_id, reason: str, *, unlink: bool = False) -> None:
    """One row's "not sent, and here is why", committed on its own.

    `unlink` drops the row's operation link as well, and exists for exactly the
    spent-nonce refusals: `state_of` asks the *operation* before it reads
    `dispatch_error`, so a row still pointing at a foreign operation would keep
    rendering that operation's state — `sent`, in the worst case — with the
    refusal sitting unread underneath it. Off by default because every other
    refusal here happens before the row has an operation at all, and a row that
    holds its own operation must keep it (`REJECTED_IS_FINAL`).
    """
    values: dict = {"dispatch_error": reason}
    if unlink:
        values["operation_id"] = None
    await session.execute(
        update(PayoutBatchRow).where(PayoutBatchRow.id == row_id).values(**values)
    )
    await session.commit()


async def _claimed_by(session: AsyncSession, nonce: uuid.UUID) -> str:
    """`NONCE_SPENT_ELSEWHERE` naming whatever operation holds `nonce` now.

    One lookup serving both spent-nonce arms. The hash-mismatch arm already has the
    operation in hand and could format the sentence itself, but the type-mismatch
    arm only has `IntentTypeMismatch` — which carries the foreign operation's
    *type* and not its id, and the id is the thing an operator has to be able to
    open. `by_intent` with no `type` argument cannot raise, which is the whole
    reason it takes the type as an argument rather than as a filter.
    """
    op = await operations.by_intent(session, nonce)
    if op is None:  # pragma: no cover - the claim would have to vanish between two reads
        return NONCE_SPENT_ELSEWHERE.format(op_id=nonce, op_type="request no longer on record")
    return NONCE_SPENT_ELSEWHERE.format(op_id=op.id, op_type=op.type)


async def _abort(session: AsyncSession, batch: PayoutBatch, result: dict, reason: str) -> dict:
    """A run that could not start, written onto the rows it did not send.

    The alternative — returning the sentence and leaving the rows untouched —
    left an operator watching a page that polled forever for work nobody was
    doing. Every row that has no operation gets the reason instead, which is
    both true (nothing reached Conduit) and retryable (a refusal is cleared by
    the next dispatch that succeeds). The batch stays `partially_dispatched`:
    it has not been dispatched, and saying so is the point.
    """
    log.warning("batch %s: dispatch aborted — %s", batch.id, reason)
    result["aborted"] = reason
    # `SENDABLE_ROW_STATES`, not `pending`: a row this dispatch would have put on
    # the wire is a row this abort refused. Skipping the already-`refused` ones
    # left the page showing the *previous* abort's reason for a run stopped by a
    # different cause, and under-counted `refused` to boot.
    for row in await rows_of(session, batch.id):
        if row["state"] in SENDABLE_ROW_STATES:
            result["refused"] += 1
            await _refuse(session, row["id"], reason)
    return result


async def dispatch(
    session: AsyncSession,
    client,
    batch: PayoutBatch,
    *,
    actor_id: str,
    actor_email: str,
) -> dict:
    """Every valid row of one batch, sequentially, exactly once.

    Returns `{"sent", "rejected", "unconfirmed", "refused", "skipped",
    "aborted"}` — `skipped` counts the rows that already had an operation (the
    idempotence path), `aborted` is a sentence when the batch could not be
    dispatched at all and nothing was attempted.

    Not a transaction: each row commits its own outcome, because a batch that
    crashed at row 40 must leave 39 dispatched rows on the record and one
    unknown, which is exactly what the operations ledger is for.
    """
    result = {"sent": 0, "rejected": 0, "unconfirmed": 0, "refused": 0, "skipped": 0, "aborted": ""}

    # **The funding account, live, before anything else**. It
    # is the one fact this loop used to take from the upload row unchecked, and
    # it is the fact every single payment here depends on: `virtual_account_id`
    # and `asset` go into every row's body. A batch can be approved on Monday and
    # dispatched on Friday, and an account can be closed in between — Conduit
    # would then refuse each row one at a time, N refusals for one cause, each
    # one a ledger row. First, so a batch that cannot be funded costs no
    # discovery reads either. `accounts.fetch_account` rather than the picker's
    # list read: the list is pre-filtered to active accounts, which collapses
    # "gone" and "closed" into one absence, and those are different sentences.
    #
    # **It is a read before the loop, not a lock on it.** Nothing here holds the
    # account, so it can be closed while row 40 of 200 is on the wire; what this
    # check buys is that a batch dispatched against an *already* dead account
    # costs one read and one sentence instead of N ledger rows. A closure that
    # happens mid-run surfaces the way it should — as Conduit rejecting the rows
    # after it, each one recorded against its own operation — because only
    # Conduit knows which side of the closure a payment landed on.
    account = await accounts.fetch_account(client, batch.customer_id, batch.virtual_account_id)
    if isinstance(account, Problem) and account.status == 404:
        return await _abort(session, batch, result, NO_SUCH_ACCOUNT_AT_DISPATCH)
    if not isinstance(account, dict):
        # Unreadable is never rendered as absent: "Conduit did not answer" and
        # "this account is gone" are different facts about a client's money, and
        # only one of them is retryable by trying again.
        return await _abort(session, batch, result, UNREADABLE_ACCOUNT_AT_DISPATCH)
    if account.get("status") != "active":
        return await _abort(
            session,
            batch,
            result,
            INACTIVE_ACCOUNT_AT_DISPATCH.format(
                status=account.get("status") or "in an unknown state"
            ),
        )
    if (held := str(((account.get("asset") or {}).get("code")) or "")) != batch.asset:
        return await _abort(
            session,
            batch,
            result,
            ACCOUNT_ASSET_AT_DISPATCH.format(
                actual=held or "an unstated currency", expected=batch.asset
            ),
        )

    rows = await rows_of(session, batch.id)
    # Fresh discovery, once per **purpose actually present in this batch** — at
    # most seven reads, usually one or two. The model is what turns a registered
    # recipient into coordinates (`apply_recipient`), it is the live answer to
    # whether that purpose is gated, and it is what says whether the batch's
    # shared documents ride with that row. All three are read rather than
    # remembered: nothing about this loop trusts what the upload stored.
    models: dict[str, forms.FormModel] = {}
    for purpose in dict.fromkeys(row["purpose"] for row in rows if not row["errors"]):
        snapshot = await payments.fetch_requirements(
            client,
            purpose=purpose,
            rail=batch.rail,
            recipient_type=batch.recipient_type,
            destination_country=batch.destination_country,
        )
        if not isinstance(snapshot, dict):
            return await _abort(session, batch, result, NO_REQUIREMENTS_AT_DISPATCH)
        models[purpose] = payments.payout_model(snapshot)

    entries: list[dict] = []
    if any(is_gated(model) for model in models.values()):
        page = await payments.fetch_recipients(client, batch.customer_id)
        if not isinstance(page, Page):
            return await _abort(session, batch, result, UNREADABLE_WHITELIST_AT_DISPATCH)
        entries = list(page.items)

    attachable = list(batch.document_ids or [])
    for row in rows:
        if row["errors"]:
            continue  # never dispatchable; the report has said so since upload
        model = models[row["purpose"]]

        # The idempotence gate, and it is deliberately *not* an early `continue`
        # on `operation_id`: a crash between `execute_operation` and the link
        # write leaves a row with no link and an operation that exists. Asking
        # `operations.start` — a local insert-or-resolve on the intent nonce — is
        # what makes "exactly once" a property of the database rather than of
        # this loop's bookkeeping.
        assembled, refusal = await _destination(
            session, batch, row, model=model, entries=entries
        )
        if refusal:
            result["refused"] += 1
            await _refuse(session, row["id"], refusal)
            continue

        # `MONEY_CEILING`, per row. Here rather than at
        # upload: the ceiling is deployment config, so a batch validated before
        # it was set must still be refused when it is dispatched. Onto the row's
        # own `dispatch_error`, which is the existing shape for "this row was not
        # sent and here is why", and before `operations.start` — the refused row
        # never becomes a ledger row.
        if ceiling := payments.over_ceiling(row["amount"]):
            result["refused"] += 1
            await _refuse(session, row["id"], ceiling)
            continue

        body = payments.assembled_payout_body(
            assembled or {},
            customer_id=batch.customer_id,
            virtual_account_id=batch.virtual_account_id,
            asset=batch.asset,
            amount_text=row["amount"] or "",
            # The ROW's purpose, not the batch's — a batch no longer has one.
            purpose=row["purpose"],
            # The shared documents ride only with the rows whose purpose asks
            # for one. A payroll register attached to a goods row is evidence for
            # a payment it is not about, and Conduit is entitled to read it that
            # way.
            document_ids=attachable if model.documentation.get("required") else [],
        )
        # The row's two derived identities, named once because the nonce guards
        # below need both of them and a second copy of either literal would be
        # free to drift from this one.
        nonce = intent_for(batch.id, row["row_number"])
        # Two rows of one file may be byte-identical and are still two payments
        # too. The §1 duplicate guard hashes the body, and the
        # body cannot tell them apart — `clientReferenceId` is injected at send
        # time — so without this scope row 2 resolved to row 1's still-active
        # operation and was never sent. Same identity the intent is derived
        # from, and console-local: nothing on the wire changes.
        scope = f"{batch.id}:{row['row_number']}"
        try:
            op, is_new = await operations.start(
                session,
                type="payout_create",
                actor_id=actor_id,
                actor_email=actor_email,
                path=payments.PAYOUT_PATH,
                body=body,
                customer_id=batch.customer_id,
                intent=nonce,
                hash_scope=scope,
            )
        except operations.IntentTypeMismatch:
            # **The cheap half of the attack.** A row's nonce spent on some
            # *other kind* of operation — a document upload will do, and
            # `document.upload` is a far weaker permission than `batch.dispatch`
            # — makes `by_intent` refuse rather than resolve, correctly: a nonce
            # belongs to one render and one form. But that refusal used to
            # travel straight out of this function and into `_run`'s blanket
            # `except`, which is a log line and a rollback. The run died at the
            # claimed row, every row after it was left `pending` and unsent, and
            # because the nonce is a pure function of the batch and the row,
            # every re-dispatch died in exactly the same place: the batch was
            # stuck `partially_dispatched` forever, `abandon` refuses that
            # status, and the only recovery was a hand-written DELETE in
            # `operation_intents`.
            #
            # Per row, therefore, and onto the row: one row of a batch being
            # unsendable is not a reason to stop paying the other 199.
            result["refused"] += 1
            await _refuse(session, row["id"], await _claimed_by(session, nonce), unlink=True)
            continue
        if not is_new and operations.resolved_elsewhere(op, payments.PAYOUT_PATH, body, scope):
            # **The silent half.** `start`'s guard 1 resolves on the nonce
            # alone, in every state, and `by_intent` narrows only by operation
            # *type* — both load-bearing exactly as written, and neither this
            # loop's to change. The consequence here is that a `payout_create`
            # which already spent this row's nonce comes back as `is_new=False`
            # with a real, confirmed operation about somebody else's payment,
            # and falling through to the link-and-skip below reported it as this
            # row's: the report and the results file both said `sent`, carrying
            # that operation's transaction id and an empty problem column, for a
            # payment this console never made. Zero wire calls, no error
            # anywhere, and every surface asserting the money went out.
            #
            # `resolved_elsewhere` recomputes the request hash — path plus
            # canonical body — and asks whether the resolved operation agrees
            # with what this row would have sent. It must be passed the same
            # `scope` given to `start`, or every row after the first looks like a
            # replay of the first.
            #
            # Before the link write below, and `unlink` on top of that for a row
            # a previous build already linked: `state_of` reads the operation
            # before it reads `dispatch_error`, so a row that keeps the foreign
            # operation keeps rendering `sent` however loudly the refusal
            # underneath it disagrees.
            result["refused"] += 1
            await _refuse(session, row["id"], await _claimed_by(session, nonce), unlink=True)
            continue
        if op.id != row["operation_id"]:
            # Link first, then send: a row whose operation is unrecorded is a row
            # the operator cannot follow, and the link is cheap to write twice.
            await session.execute(
                update(PayoutBatchRow)
                .where(PayoutBatchRow.id == row["id"])
                .values(operation_id=op.id, dispatch_error=None)
            )
            await session.commit()
        # **The strand.** `operations.start` commits a `created` row and
        # returns; `execute_operation` is what puts it on the wire. A process
        # that dies in that window leaves an operation nothing will ever finish:
        # the reconciler sweeps `in_flight` and `outcome_unknown` only, the
        # retry and abandon routes take `stalled` only, and the skip below —
        # written for "already dispatched" — read `is_new=False` as "already
        # sent" and stepped over it. After OP_CREATED_TTL the operation goes
        # `abandoned` and the row's payment simply never happens, on a batch
        # that has said `dispatched` since the re-dispatch.
        #
        # `created` is the one `is_new=False` state where nothing has been
        # sent — `in_flight_at` is null, `attempt_count` is 0, no idempotency
        # key has ever left this process — so executing is the *first* attempt,
        # not a second one. Exactly-once is unweakened by construction rather
        # than by this loop's care: the send goes through `created -> in_flight`
        # under `transition`'s `SELECT ... FOR UPDATE`, so of two runs that both
        # read `created` exactly one moves the row and the other finds a state
        # that makes its own transition illegal. The single-payout form fixed
        # this class for itself by moving its reads ahead of `operations.start`;
        # a batch row cannot, because the crash is a process death and not a
        # slow read.
        #
        # **Ordering, and it is the whole of the danger here.** Both nonce guards
        # are above and both `continue`: an operation of the wrong type, and an
        # operation whose request hash is not what this row would send, are
        # refused before this line is reached. A foreign `created` operation —
        # somebody who spent this row's nonce and has not sent it yet — is
        # therefore already gone by now, and must be: executing it would put
        # this batch's `batch.dispatch` permission behind a stranger's request
        # body, which is worse than the strand this branch exists to fix.
        stranded = not is_new and op.state == "created"
        if not is_new and not stranded:
            # This row has already been dispatched — by an earlier click, an
            # earlier process, or the request that raced this one. Nothing goes
            # on the wire.
            result["skipped"] += 1
            continue

        try:
            op = await execute_operation(
                session, op, client=client, actor_id=actor_id, actor_email=actor_email
            )
        except operations.OperationAdvanced:
            # **The strand's carry-forward, and this closes it.** The branch above
            # executes an operation this run did not create, so two runs can
            # hold the same `created` row and both arrive here. Exactly one
            # moves it — that is `transition`'s `SELECT ... FOR UPDATE` and it
            # was never in doubt — and before this the other raised
            # `IllegalTransition created -> in_flight` straight out of this
            # function into `_run`'s blanket `except`. The money was safe and
            # the run was not: it ended at the contested row, every row after it
            # went unattempted, and the rollback took that run's own
            # `payout_batch.dispatched` audit row with it, so nothing recorded
            # that the dispatch had happened at all.
            #
            # A convergence, counted where a convergence is counted. The run
            # that won this race is sending the row right now under the same
            # idempotency key, which is precisely what `skipped` already means
            # a few lines above — "this row has been dispatched, by an earlier
            # click, an earlier process, or the request that raced this one" —
            # and it is the same reading `_Result._record` gives a webhook that
            # resolves a row out from under an open HTTP call.
            #
            # Narrow on purpose: `OperationAdvanced` is raised for `-> in_flight`
            # and nothing else, so a genuinely illegal move still leaves this
            # loop as an `IllegalTransition` and still ends the run loudly.
            result["skipped"] += 1
            continue
        result[
            {"confirmed": "sent", "rejected": "rejected"}.get(op.state, "unconfirmed")
        ] += 1
        await _pace()
    return result


# --- the results file ----------------------------------------------------------------------


def list_filters(query) -> dict:
    """The two things a results export is about, parsed **once** — the export
    route calls this and nothing re-derives it (`app/web/exports.py`'s rule).

    A batch is not a filtered view, so there is no third parameter: the file is
    the batch, whole.
    """
    return {
        "customerId": (query.get("customerId") or "").strip(),
        "batchId": (query.get("batchId") or "").strip(),
    }


def problem_title(row: Mapping) -> str:
    """One column's worth of "why this row is not a payment".

    Four different authors, one cell: the console's own sentence for an
    abandoned submission, the console's own sentence for Conduit's refusal
    (`web.problem_of`, applied in `rows_of` — never Conduit's own prose), this
    console's own sentence where it declined to send, and the first validation
    complaint where the row never qualified in the first place. A valid, sent
    row has nothing to say here and says nothing.

    The abandoned arm is first, and for `state_of`'s reason: where the operation
    is the answer, its own sentence outranks anything written before it. A row
    abandoned after an earlier dispatch refused it still holds that refusal in
    `dispatch_error`, and printing it here would tell an operator this row was
    never sent on the strength of a sentence the operation has since overruled.
    """
    if row.get("state") == "abandoned":
        # Nothing else in this function can speak for an abandoned row —
        # there is no problem, the `dispatch_error` was cleared when the row was
        # linked, and the row was valid — so before this it printed an empty
        # cell next to a batch that said `dispatched`.
        return ABANDONED_AFTER_SENDING if row.get("attempted") else ABANDONED_NEVER_SENT
    if row.get("problem"):
        return str((row["problem"] or {}).get("title") or "Rejected")
    if row.get("dispatch_error"):
        return str(row["dispatch_error"])
    first = (row.get("errors") or [None])[0]
    return str((first or {}).get("detail") or "") if first else ""


async def results(session: AsyncSession, customer_id: str, batch_id: str) -> list[dict] | None:
    """One batch's rows with everything the results file states, or `None` when
    that batch is not this customer's.

    Zero Conduit calls: the destinations are this console's own encrypted rows
    and the outcomes are the operations ledger's, so a results export is
    readable when Conduit is not.
    """
    batch = await get(session, customer_id, batch_id)
    if batch is None:
        return None
    return [
        {**row, "asset": batch.asset, "problem_title": problem_title(row)}
        for row in await rows_of(session, batch.id)
    ]


# --- retention -------------------------------------------------------------------------

# A batch whose dispatch is over, mirroring `operations.TERMINAL_STATES`.
# `partially_dispatched` is deliberately out, for the reason `stalled` is out of
# the operations set: it is re-dispatchable, and the undispatched rows still need
# the destinations they would be sent with.
TERMINAL_BATCH_STATUSES = ("dispatched", "abandoned")


async def purge_row_payloads(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Worker retention job — `operations.purge_request_bodies`' rules, applied to
    the assembled `destination` subtree on each row of a finished batch.

    Same window (`OP_BODY_RETENTION`) as the operation bodies these rows became:
    a dispatched row's payload was copied into its `payout_create` operation's
    `request_body`, and purging one while keeping the other would delete nothing.
    An abandoned batch was never sent at all, and ages out on the same clock.

    The age is the **batch's** `updated_at`, because that is when its dispatch
    finished — a row carries only its creation time, which is upload time, and
    aging a batch from upload would purge one that dispatched yesterday.

    What survives is everything the results export and the ledger read:
    `amount`, `purpose`, `contact_label`, `errors`, `dispatch_error` and the
    operation each row minted. Only the encrypted coordinates go.

    `errors` survives in plaintext and forever, which is safe only because no
    validator quotes back more than a masked tail of a cell an operator typed
    (`UNKNOWN_CONTACT` and `_quotable_purpose` here, the allowedValues
    message in `app/forms.py`). That is a property of those sentences, not of
    this job — a value never stored whole cannot leak from a backup taken
    before the window closes — so a new message that interpolates a cell puts
    the exposure back and no retention change here would answer it.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=get_settings().op_body_retention_days)
    purged = await session.execute(
        update(PayoutBatchRow)
        .where(
            PayoutBatchRow.payload.is_not(None),
            or_(
                PayoutBatchRow.batch_id.in_(
                    select(PayoutBatch.id).where(
                        PayoutBatch.status.in_(TERMINAL_BATCH_STATUSES),
                        PayoutBatch.updated_at < cutoff,
                    )
                ),
                # **A row that can never be sent again, on a batch that may
                # never finish.** Dropping `abandoned` from `SETTLED_ROW_STATES`
                # means `complete()` can no longer return True for a
                # batch holding one, so it never reaches a terminal status and
                # the arm above never reaches it — while `abandon_expired` mints
                # these with no operator involved, so the console produces them
                # on its own. That left the row's encrypted recipient held
                # indefinitely while `operations.purge_request_bodies` deleted
                # the ledger's copy of the same data on schedule.
                #
                # `abandoned` has no outgoing legal transition, so this payload
                # cannot be needed for a send. The clock is the operation's own
                # `resolved_at`, mirroring the ledger purge exactly rather than
                # the batch's `updated_at`, which a live batch keeps moving.
                PayoutBatchRow.operation_id.in_(
                    select(Operation.id).where(
                        Operation.state == "abandoned",
                        Operation.resolved_at < cutoff,
                    )
                ),
            ),
        )
        .values(payload=None)
    )
    await session.commit()
    return purged.rowcount
