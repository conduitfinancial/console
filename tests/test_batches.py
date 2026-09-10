"""Batch payouts: template out, filled file in, per-row report — now
**multi-purpose** (the payout-flow restructure round).

The shape of this file follows the feature's own two halves. The template and the
parser are judged as *artefacts* — what columns a route produces, what a file may
and may not contain — and the rows are judged against the same matrix the single
payout form is judged by, because they go through the same validator. Where a
sentence is asserted it is asserted verbatim: the design owner's requirement is
that a batch error reads exactly like the single-payout error for the same fault,
and the only way that stays true is if a test would fail when it stops being.

Nothing here dispatches: upload creates no operation and makes no Conduit
mutation, and one test asserts exactly that about the wire.
"""

from __future__ import annotations

import csv
import html
import io
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import httpx
import pytest
from sqlalchemy import select, text

from app import batches, counterparties, payments
from app.auth.tokens import CSRF_HEADER
from app.config import get_settings
from app.models import Operation, PayoutBatch, PayoutBatchRow
from tests.conftest import settings_override
from tests.payments_fixtures import (
    CHAPS_BUSINESS,
    CID,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    GBP_ACCOUNT,
    REGISTERED,
    SEPA_BUSINESS,
    USD_ACCOUNT,
    WHITELIST_PATH,
    page,
    requirements_handler,
)
from tests.web_harness import (
    CSRF_NAME,
    documents_stub,
    make_app,
    post,
    signed_in,
    stub,
    upload as upload_document,
)

BATCHES = f"/customers/{CID}/batches"
NEW = f"{BATCHES}/new"
TEMPLATE = f"{BATCHES}/template.csv"
# The corridor, and only the corridor: the purpose is a COLUMN now, so a batch's
# route has two parts and every row picks its own seventh.
ROUTE = "rail=fedwire&recipientType=business"
GOODS = "payment_for_goods_or_services"

# A complete fedwire/business row: every field the fixture declares required,
# plus the route's own purpose and amount columns.
ROW = {
    "purpose": GOODS,
    "destination.type": "fiat",
    "destination.rail": "fedwire",
    "destination.recipient.accountNumber": "000123456789",
    "destination.recipient.routingNumber": "021000021",
    "destination.recipient.accountType": "CHECKING",
    "destination.recipient.type": "BUSINESS",
    "destination.recipient.legalName": "ZZZTEST Globex Supplies LLC",
    "destination.recipient.bankAddress.addressLine1": "270 Park Ave",
    "destination.recipient.bankAddress.city": "New York",
    "destination.recipient.bankAddress.country": "US",
    "destination.recipient.postalAddress.addressLine1": "500 Market St",
    "destination.recipient.postalAddress.city": "New York",
    "destination.recipient.postalAddress.country": "US",
    "destination.recipient.postalAddress.postalCode": "10010",
    "destination.remittance.reference": "INV-4471",
    "amount": "10.00",
}
# The same row on the whitelist-gated purpose: `intercompany` declares no
# coordinate columns, so a row of it must leave the union's coordinate columns
# empty and name a registered recipient in `contact` instead.
GATED_ROW = {
    key: value
    for key, value in ROW.items()
    if key
    not in (
        "destination.recipient.accountNumber",
        "destination.recipient.routingNumber",
        "destination.recipient.legalName",
    )
} | {"purpose": "intercompany", "contact": REGISTERED["id"]}


def routes(requirements=FEDWIRE_BUSINESS, recipients=None, extra=None, refuse=(), per_purpose=None):
    """`requirements` answers every purpose except `intercompany`, which answers
    the gated fixture — the shape a real corridor has."""
    return {
        ("GET", "/v2/payouts/requirements"): requirements_handler(
            requirements, per_purpose=per_purpose, refuse=refuse
        ),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
        # Dispatch re-reads the funding account by id before it sends anything
        #, which is a different endpoint from the picker's list.
        ("GET", f"/v2/customers/{CID}/virtual-accounts/{USD_ACCOUNT['id']}"): httpx.Response(
            200, json=USD_ACCOUNT
        ),
        ("GET", WHITELIST_PATH): page(recipients if recipients is not None else [REGISTERED]),
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


# --- building a filled file ------------------------------------------------------------


def split(template: str) -> tuple[list[str], list[str]]:
    """`(the # lines, the header)` out of a downloaded template."""
    lines = list(csv.reader(io.StringIO(template)))
    comments = [row[0] for row in lines if row and row[0].startswith("#")]
    header = next(row for row in lines if row and not row[0].startswith("#"))
    return comments, header


def build(comments, header, rows, *, bom: bool = False) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for line in comments:
        writer.writerow([line])
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    text = out.getvalue()
    return ("﻿" + text).encode("utf-8") if bom else text.encode("utf-8")


async def template_for(web, query: str = ROUTE) -> tuple[list[str], list[str]]:
    response = await web.get(f"{TEMPLATE}?{query}")
    assert response.status_code == 200, response.text
    return split(response.text)


async def send(web, body: bytes, query: str = ROUTE, filename: str = "batch.csv"):
    """The upload exactly as `static/app.js` sends it: raw bytes as the body, the
    route in the query string, CSRF in a header."""
    return await web.post(
        f"{BATCHES}?{query}&filename={filename}",
        content=body,
        headers={
            "content-type": "text/csv",
            CSRF_HEADER: web.cookies.get(CSRF_NAME) or "",
            "HX-Request": "true",
        },
    )


def flash(response: httpx.Response) -> str:
    return httpx.URL(response.headers.get("hx-redirect", "")).params.get("err", "")


def shown(response: httpx.Response) -> str:
    """The page's text with HTML entities resolved — the sentences asserted here
    are the validator's own, and Jinja escapes the apostrophes in them."""
    return html.unescape(response.text)


async def uploaded(web, rows, query: str = ROUTE, *, filename: str = "batch.csv", **kwargs) -> str:
    """One batch from a real template, and the URL of its report."""
    comments, header = await template_for(web, query)
    response = await send(web, build(comments, header, rows, **kwargs), query, filename)
    assert response.status_code == 204, response.text
    location = response.headers["hx-redirect"]
    assert "err=" not in location, location
    return location


# --- the template ------------------------------------------------------------------------


async def test_the_template_columns_are_the_union_of_every_purposes_requirements():
    """Never a hardcoded template: the columns are the live requirements
    responses' field names, in their order, unioned across the seven purposes,
    plus the three the route owns rather than declares."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)

    declared = [f["name"] for f in FEDWIRE_BUSINESS["fields"]]
    # `virtualAccountId` is the route's (one funding account per batch) and the
    # off-rail `ach` subtree is the one thing this console overrides discovery on
    # (`payments._spurious_ach`) — the template obeys the same model the form does.
    assert "virtualAccountId" not in header
    assert not [name for name in header if name.startswith("destination.ach.")]
    assert [name for name in header if name in declared] == [
        name for name in declared if name in header
    ]
    assert header[0] == "purpose"
    assert header[-2:] == ["amount", "contact"]
    # Remittance is in there because discovery declares it, not because this
    # console appends a pair of columns of its own.
    assert "destination.remittance.reference" in header
    assert "destination.remittance.description" in header
    # The union's point: `intercompany` declares no coordinate columns and
    # `payment_for_goods_or_services` does, and one file may carry both — so the
    # coordinate columns are in the header and are a per-ROW judgement.
    assert "destination.recipient.routingNumber" in header


def test_a_gated_purpose_still_has_no_coordinate_columns_of_its_own():
    """The gate is per purpose now, not per file: under `whitelist.required` the
    destination is Conduit's registered record, so no `intercompany` row has a
    column to type one into — even though the union header has them for the
    purposes that do."""
    from app import payments

    gated = payments.payout_model(FEDWIRE_INTERCOMPANY)
    free = payments.payout_model(FEDWIRE_BUSINESS)
    own = batches.purpose_columns(gated)
    for coordinate in ("accountNumber", "routingNumber", "legalName"):
        assert f"destination.recipient.{coordinate}" not in own
        assert f"destination.recipient.{coordinate}" in batches.purpose_columns(free)
    assert "contact" in own
    # The non-identity recipient fields are still the row's, exactly as they are
    # on the gated form.
    assert "destination.recipient.postalAddress.city" in own


async def test_the_fingerprint_covers_the_whole_per_purpose_set():
    """The drift sentinel: the same responses fingerprint identically every time,
    and *any* change to what discovery said about *any* purpose moves it — a
    flipped `documentation.required` on one purpose as much as a new field."""
    both = {"payroll": FEDWIRE_BUSINESS, "intercompany": FEDWIRE_INTERCOMPANY}
    assert batches.fingerprint(both) == batches.fingerprint(dict(reversed(list(both.items()))))
    assert batches.fingerprint(both) != batches.fingerprint({"payroll": FEDWIRE_BUSINESS})
    # One purpose moving is the whole template moving, which is the point: a file
    # may use any of them.
    moved = {**both, "payroll": {**FEDWIRE_BUSINESS, "documentation": {"required": False}}}
    assert batches.fingerprint(both) != batches.fingerprint(moved)
    assert batches.fingerprint(both) != batches.fingerprint({**both, "payroll": SEPA_BUSINESS})


async def test_the_header_block_names_the_corridor_the_snapshot_and_the_purpose_column():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, _ = await template_for(web)

    block = "\n".join(comments)
    assert f"# customer: {CID}" in block
    assert "# purpose:" not in block  # the batch has none; each row names its own
    assert "# rail: fedwire" in block
    assert "# recipientType: business" in block
    assert "# destinationCountry" not in block
    # The verbatim rule, stated: the column takes the RAW KEY, and the label is
    # printed beside it so an operator can read the list.
    assert "RAW KEY" in block
    for purpose in ("payment_for_goods_or_services", "intercompany", "prefunding"):
        assert f"# purpose value — {batches.payments.purpose_label(purpose)}: {purpose}" in block
    # The per-purpose gating summary.
    assert "WHITELISTED recipient needed for: intercompany" in block
    assert "Supporting document needed for:" in block and "payroll_register" in block


async def test_the_templates_fingerprint_is_the_one_the_upload_recomputes():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, _ = await template_for(web)
        report = await web.get(await uploaded(web, [ROW]))
    assert "out of date" not in report.text
    assert any(line.startswith("# fingerprint: ") for line in comments)


async def test_a_purpose_this_corridor_refuses_is_named_not_guessed():
    """One purpose 4xx-ing must not cost the operator the other six: the template
    is built from what answered, the header block says which did not, and a row
    naming a missing purpose is refused at upload."""
    app = make_app(stub(routes(refuse=("prefunding",))))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        block = "\n".join(comments)
        assert "NOT available on this route" in block and "prefunding" in block
        report = await web.get(await uploaded(web, [{**ROW, "purpose": "prefunding"}]))
    assert "'prefunding' is not a purpose this route can be read for" in shown(report)
    assert "0 valid · 1 invalid" in report.text


async def test_no_template_is_produced_when_discovery_cannot_be_read():
    """A template with guessed columns is the one artefact that turns into a
    wrong payment later, so an unreadable route produces no file at all."""
    app = make_app(stub(routes(httpx.Response(503, json={"title": "upstream down"}))))
    async with signed_in(app) as web:
        response = await web.get(f"{TEMPLATE}?{ROUTE}", follow_redirects=False)
    assert response.status_code == 303
    assert "could not be read" in httpx.URL(response.headers["location"]).params.get("err", "")


async def test_a_viewer_may_download_a_template_but_not_upload_one():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        assert (await web.get(f"{TEMPLATE}?{ROUTE}")).status_code == 200
        assert (await send(web, b"anything")).status_code == 403


async def test_an_upload_without_the_csrf_header_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            f"{BATCHES}?{ROUTE}", content=b"x", headers={"content-type": "text/csv"}
        )
    assert response.status_code == 403


# --- the parser --------------------------------------------------------------------------


async def test_excels_byte_order_mark_is_expected_not_tolerated():
    """"CSV UTF-8" in Excel writes a BOM on every save, so the file an operator
    sends back always has one."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [ROW], bom=True))
    assert "1 row · 1 valid · 0 invalid" in report.text


async def test_a_file_that_is_not_utf8_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        body = build(comments, header, [ROW]).replace(b"Globex", b"Glob\xffex")
        response = await send(web, body)
    assert flash(response) == batches.NOT_UTF8


async def test_a_file_with_cr_only_line_endings_is_refused_not_crashed(session):
    """`io.StringIO` (the default `newline='\\n'`) only splits on LF, so a file
    whose only line terminator is a bare CR arrives as one giant "line" with the
    CRs still embedded in it — and `csv.reader` raises `_csv.Error: new-line
    character seen in unquoted field` reading it. That raise used to come from
    outside `parse`'s `try`, so it reached the route as a 500 instead of a
    refusal."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r")
        for line in comments:
            writer.writerow([line])
        writer.writerow(header)
        writer.writerow([ROW.get(name, "") for name in header])
        response = await send(web, out.getvalue().encode("utf-8"))
    assert flash(response) == batches.MALFORMED_CSV
    assert (await session.execute(select(PayoutBatch))).first() is None


async def test_an_overlong_fingerprint_header_is_truncated_not_stored_uncapped(session):
    """`template_fingerprint` is `varchar(64)` (`app/models.py`); the header's
    `# fingerprint:` value is whatever the file says and is never validated
    against the digest algorithm's own output length, so a hand-edited or
    corrupted file can carry one Postgres will not accept."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        comments = [
            f"# {batches.FINGERPRINT_KEY}: {'a' * 200}" if line.startswith("# fingerprint:") else line
            for line in comments
        ]
        response = await send(web, build(comments, header, [ROW]))
    assert response.status_code == 204, response.text
    assert "err=" not in response.headers.get("hx-redirect", "")
    batch = (await session.execute(select(PayoutBatch))).scalar_one()
    assert batch.template_fingerprint == "a" * 64


async def test_an_amount_of_65_digits_is_refused_as_an_invalid_row_not_a_crash():
    """`payments.amount()` used to be unbounded: `PLAIN_DECIMAL` matches any run
    of digits and `format(parsed, "f")` returns all of them, so a 65-digit
    amount produced a 65-character string for a row `amount` column that is
    `varchar(64)` — a DB error, not a refusal."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        report = await web.get(
            await uploaded(web, [{**ROW, "amount": "1" * 65}])
        )
    assert "0 valid · 1 invalid" in report.text
    assert payments.AMOUNT_MESSAGE in shown(report)


async def test_an_amount_whose_scale_overflows_the_column_is_refused_too():
    """The first cap counted *significant digits*, which is not
    what the `varchar(64)` row column bounds: `0.` followed by 64 zeroes and a
    `1` has ONE significant digit, passed that cap, and formatted to 67
    characters — so the overflow the cap existed to stop still reached the
    operator as a DB error rather than a refusal. Scale is the other half of
    the canonical length, so the bound has to be measured on the formatted
    string."""
    long_scale = "0." + "0" * 64 + "1"
    assert len(long_scale) > 64
    assert payments.amount(long_scale) is None  # the unit half, at the boundary

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        report = await web.get(await uploaded(web, [{**ROW, "amount": long_scale}]))
    assert "0 valid · 1 invalid" in report.text
    assert payments.AMOUNT_MESSAGE in shown(report)


async def test_an_amount_that_exactly_fills_the_column_is_still_accepted():
    """The non-vacuity half: the cap must refuse what does not fit, not round
    down to something conveniently small. 64 characters is the column, so 64
    characters must pass."""
    exact = "0." + "0" * 61 + "1"
    assert len(exact) == 64
    assert payments.amount(exact) == exact


async def test_an_unknown_column_is_refused_never_ignored():
    """A column this route never declared is a typo or
    another route's template, and either way the values under it would not have
    been sent."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header + ["swiftCode"], [ROW]))

    assert "'swiftCode'" in flash(response)
    assert "Nothing was validated" in flash(response)


async def test_a_duplicated_column_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header + ["amount"], [ROW]))
    assert "'amount'" in flash(response) and "more than once" in flash(response)


async def test_a_missing_column_every_purpose_requires_is_the_whole_files_refusal():
    """The file-level check is the INTERSECTION: `amount` is required whatever a
    row's purpose is, so a header without it cannot describe any payment."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        kept = [name for name in header if name != "amount"]
        response = await send(web, build(comments, kept, [ROW]))
    assert "'amount'" in flash(response) and "is missing" in flash(response)


async def test_a_missing_column_only_one_purpose_requires_is_that_rows_own_error():
    """`destination.recipient.accountNumber` is required by the goods purpose and
    does not exist at all on the gated one, so a header without it is a fine file
    for an intercompany run and a broken one for a goods row. Refusing the whole
    file would refuse the run that is correct; the goods row gets the engine's own
    required-field sentence against the column's own name instead."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        kept = [name for name in header if name != "destination.recipient.accountNumber"]
        response = await send(web, build(comments, kept, [GATED_ROW, ROW]))
        assert "err=" not in response.headers["hx-redirect"], flash(response)
        report = await web.get(response.headers["hx-redirect"])
    assert "2 rows · 1 valid · 1 invalid" in report.text
    assert "Account number: This field is required." in report.text


async def test_a_file_built_for_another_route_is_refused():
    """The corridor is in the file *and* in the request; a file that disagrees
    with the screen is not this batch, whatever its columns are. The purpose is
    NOT part of that check any more — it is a column, and the rows own it."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        forged = [line.replace("rail: fedwire", "rail: sepa") for line in comments]
        response = await send(web, build(forged, header, [ROW]))
    assert "different route" in flash(response) and "rail: sepa" in flash(response)


async def test_a_file_with_a_header_and_no_rows_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header, []))
    assert flash(response) == batches.NO_ROWS


async def test_a_file_over_the_row_cap_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header, [ROW] * (batches.MAX_ROWS + 1)))
    assert str(batches.MAX_ROWS) in flash(response) and "Split it" in flash(response)


async def test_a_body_over_the_byte_cap_is_refused_while_it_streams():
    """`read_capped`, the document uploader's idiom: a declared size is a promise,
    not a limit, so the body is read under a cap rather than buffered."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await send(web, b"a" * (batches.MAX_BYTES + 1))
    assert flash(response) == batches.TOO_LARGE


async def test_a_spreadsheet_formula_in_a_cell_is_text_all_the_way_through():
    """CSV injection, from the reading side: nothing here evaluates a cell, and
    what reaches the report is escaped by the template engine rather than
    rendered as markup."""
    app = make_app(stub(routes()))
    formula = '=cmd|\'/c calc\'!A1<script>alert(1)</script>'
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(web, [{**ROW, "destination.recipient.legalName": formula}])
        )
    assert "<script>alert(1)</script>" not in report.text
    assert "&lt;script&gt;" in report.text
    assert "=cmd|" in report.text  # stored and shown as the text it is


async def test_a_row_with_more_cells_than_the_header_is_invalid_but_the_file_is_not():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        body = build(comments, header, [ROW, ROW])
        lines = body.decode().split("\r\n")
        lines[-3] = lines[-3] + ",surprise"
        report = await web.get(
            httpx.URL(
                (await send(web, "\r\n".join(lines).encode())).headers["hx-redirect"]
            ).path
        )
    assert batches.RAGGED_ROW in report.text
    assert "2 rows · 1 valid · 1 invalid" in report.text


# --- the fingerprint, on upload ------------------------------------------------------------


async def test_a_stale_template_is_revalidated_against_fresh_discovery_and_warns():
    """A fingerprint mismatch never selects which schema
    validates — the rows are always judged by what Conduit says now — and the
    report says the template was stale."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        stale = [
            line if not line.startswith("# fingerprint:") else "# fingerprint: " + "0" * 64
            for line in comments
        ]
        # The row is missing a field the LIVE response requires. If the file's
        # own fingerprint had selected the schema, this would have to pass.
        response = await send(
            web, build(stale, header, [{**ROW, "destination.recipient.accountType": ""}])
        )
        report = await web.get(response.headers["hx-redirect"])

    assert "template this file was built from is out of date" in report.text
    assert "This field is required." in report.text
    assert "0 valid" in report.text


async def test_a_current_template_raises_no_staleness_warning():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [ROW]))
    assert "out of date" not in report.text


# --- row validation ------------------------------------------------------------------------


async def test_a_field_error_reads_exactly_as_it_does_on_the_single_payout_form():
    """The design owner's requirement, made structural: the sentence comes from
    `forms.validate`, so it is the same object the form prints."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(
                web, [{**ROW, "destination.recipient.routingNumber": "123456789"}]
            )
        )
    assert "Not a valid ABA routing number (checksum failed)." in report.text
    # Labelled by discovery's own label for the column, because a table row has
    # no labels above its cells.
    assert "Routing number:" in report.text


async def test_a_bad_amount_is_the_payout_forms_own_refusal():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(web, [{**ROW, "amount": "-5"}, {**ROW, "amount": "NaN"}])
        )
    assert report.text.count("The amount must be a positive decimal, e.g. 1000.00.") == 2
    assert "0 valid · 2 invalid" in report.text


async def test_a_blocked_jurisdiction_stops_a_row():
    app = make_app(stub(routes()))
    blocked = FEDWIRE_BUSINESS["blockedJurisdictions"][0]
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(
                web, [{**ROW, "destination.recipient.postalAddress.country": blocked}]
            )
        )
    assert f"{blocked} is a jurisdiction Conduit blocks for this route." in report.text


async def test_one_batch_may_carry_recipients_from_different_countries():
    """The corridor is rail + recipient type, not country: two rows on the same
    fedwire/business template, one naming a US recipient and the other a GB
    one, both validate — neither the route nor the upload asks the file to
    agree on a single destination country."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(
                web,
                [
                    ROW,
                    {
                        **ROW,
                        "destination.recipient.bankAddress.country": "GB",
                        "destination.recipient.postalAddress.country": "GB",
                    },
                ],
            )
        )
    assert "2 rows · 2 valid" in report.text


async def test_a_stray_destinationcountry_in_the_query_string_is_ignored():
    """Country left over in the query string — a bookmark from before this
    route dropped it, or a hand-edited link — must not narrow the batch to one
    country: it is not part of the corridor and the upload must not read it."""
    app = make_app(stub(routes()))
    blocked = FEDWIRE_BUSINESS["blockedJurisdictions"][0]
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(web, [ROW], query=f"{ROUTE}&destinationCountry={blocked}")
        )
    assert "1 row · 1 valid · 0 invalid" in report.text


async def test_a_newly_added_rail_needs_no_batch_specific_code():
    """The template and validator are entirely discovery-driven — adding a rail
    to `payments.RAILS` is the whole of what a batch needs to use it. Proven on
    `chaps` (UK domestic, sort code + account number), funded from a GBP
    account, with no change to this file's machinery beyond the fixtures."""
    app = make_app(
        stub(
            routes(
                requirements=CHAPS_BUSINESS,
                extra={
                    ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([GBP_ACCOUNT]),
                    (
                        "GET",
                        f"/v2/customers/{CID}/virtual-accounts/{GBP_ACCOUNT['id']}",
                    ): httpx.Response(200, json=GBP_ACCOUNT),
                },
            )
        )
    )
    chaps_query = "rail=chaps&recipientType=business"
    row = {
        "purpose": GOODS,
        "destination.type": "fiat",
        "destination.rail": "chaps",
        "destination.recipient.accountNumber": "12345678",
        "destination.recipient.sortCode": "040004",
        "destination.recipient.type": "BUSINESS",
        "destination.recipient.legalName": "ZZZTEST Example Bank Customer",
        "destination.recipient.bankAddress.addressLine1": "2 Bank Street",
        "destination.recipient.bankAddress.city": "London",
        "destination.recipient.bankAddress.country": "GB",
        "destination.recipient.postalAddress.addressLine1": "1 High Street",
        "destination.recipient.postalAddress.city": "London",
        "destination.recipient.postalAddress.country": "GB",
        "destination.recipient.postalAddress.postalCode": "EC1A 1AA",
        "amount": "10.00",
    }
    async with signed_in(app) as web:
        _comments, header = await template_for(web, chaps_query)
        assert "destination.recipient.sortCode" in header
        report = await web.get(await uploaded(web, [row], chaps_query))
    assert "1 row · 1 valid · 0 invalid" in report.text


async def saved_contact(session, label: str = "Globex", customer_id: str = CID, **overrides):
    return await counterparties.save(
        session,
        customer_id=customer_id,
        label=label,
        recipient={
            "accountNumber": "000123456789",
            "routingNumber": "021000021",
            "accountType": "CHECKING",
            "type": "BUSINESS",
            "legalName": "ZZZTEST Globex Supplies LLC",
            "bankAddress": {"addressLine1": "270 Park Ave", "city": "New York", "country": "US"},
            "postalAddress": {
                "addressLine1": "500 Market St",
                "city": "New York",
                "country": "US",
                "postalCode": "10010",
            },
            **overrides,
        },
        rail_family="us",
        recipient_type="business",
        destination_country="USA",
        actor_id="usr_1",
        actor_email="ops@example.com",
    )


CONTACT_ROW = {
    key: value
    for key, value in ROW.items()
    if not key.startswith("destination.recipient.")
} | {"contact": "Globex"}


async def test_a_row_may_name_a_saved_contact_instead_of_retyping_the_destination(session):
    """The optional `contact` column, resolved through the scoped contact read —
    prefill, then validated exactly like a typed destination."""
    saved = await saved_contact(session)
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [CONTACT_ROW]))
    assert "1 valid · 0 invalid" in report.text
    assert "Globex" in report.text
    # Masked, like every other surface: a batch report is the easiest place in
    # this console to read a hundred account numbers off one screen.
    assert "••••6789" in report.text and "000123456789" not in report.text

    row = (await session.execute(select(PayoutBatchRow))).scalar_one()
    assert row.contact_id == str(saved) and row.contact_label == "Globex"


async def test_an_unknown_contact_is_refused_by_its_masked_name(session):
    """Masked, not quoted: this sentence is stored in the row's plaintext
    `errors` column and printed in the results CSV, and the contact column is
    where a spreadsheet pasted one column over puts an account number. The
    error still names the column and the report still names the row, so what
    the mask costs is nothing an operator needed to find the cell."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**CONTACT_ROW, "contact": "Nobody"}]))
    assert "No live contact called '••••body'" in shown(report)
    assert "Nobody" not in shown(report)
    assert "0 valid · 1 invalid" in report.text


async def test_an_archived_contact_is_not_a_contact(session):
    await saved_contact(session)
    await session.commit()
    row = (await session.execute(select(counterparties.Counterparty))).scalar_one()
    await counterparties.archive(session, CID, str(row.id))
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [CONTACT_ROW]))
    assert "No live contact called '••••obex'" in shown(report)


async def test_another_customers_contact_id_resolves_to_nothing(session):
    """The isolation guard, from the batch side: the contact read is scoped to
    the customer in the path, so a foreign id is the same answer a made-up one
    gets."""
    other = await saved_contact(session, customer_id="cus_someone_else")
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**CONTACT_ROW, "contact": str(other)}]))
    assert f"No live contact called '{counterparties.mask(other)}'" in shown(report)
    assert str(other) not in shown(report)


async def test_a_contact_and_typed_coordinates_are_mutually_exclusive(session):
    await saved_contact(session)
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**ROW, "contact": "Globex"}]))
    assert "Give a contact or the recipient columns, not both" in report.text


async def test_a_contact_saved_for_another_route_is_not_offered_to_this_one(session):
    """The compatibility gate the payout picker already makes: a destination
    saved for a sepa route is not the one for a fedwire payout."""
    await saved_contact(session, label="Globex EU")
    await session.execute(
        text("update counterparties set rail_family = 'sepa' where label = 'Globex EU'")
    )
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**CONTACT_ROW, "contact": "Globex EU"}]))
    assert "No live contact called '••••x EU'" in shown(report)


# --- the whitelist gate ----------------------------------------------------------------------


async def test_a_gated_row_takes_its_coordinates_from_conduits_registered_record(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [GATED_ROW]))
    assert "1 valid · 0 invalid" in report.text
    assert REGISTERED["id"] in report.text

    row = (await session.execute(select(PayoutBatchRow))).scalar_one()
    recipient = row.payload["destination"]["recipient"]
    assert recipient["accountNumber"] == REGISTERED["accountNumber"]
    assert recipient["routingNumber"] == REGISTERED["routingNumber"]
    assert recipient["legalName"] == REGISTERED["legalName"]


async def test_a_gated_row_without_a_contact_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**GATED_ROW, "contact": ""}]))
    assert "requires a registered whitelist recipient" in report.text


async def test_a_gated_row_naming_an_unregistered_entry_is_refused():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**GATED_ROW, "contact": "wlr_made_up"}]))
    assert "pick one of the customer's registered entries" in shown(report)


async def test_a_gated_row_naming_an_entry_on_another_rail_is_refused():
    """`payments.pick_recipient`'s family check, which is the payout form's — an
    IBAN-addressed account cannot be reached over fedwire."""
    sepa = {**REGISTERED, "id": "wlr_sepa", "rail": "sepa"}
    app = make_app(stub(routes(recipients=[sepa])))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**GATED_ROW, "contact": "wlr_sepa"}]))
    assert "A sepa destination cannot be paid over fedwire." in report.text


async def test_an_unreadable_whitelist_refuses_the_whole_file():
    """A gated row names a registered recipient, so a whitelist that could not be
    read would produce a batch whose gated rows are invalid for a reason that is
    not the file's. The corridor has a gated purpose whether or not this file
    uses it, so the read happens and its failure is the file's refusal."""
    app = make_app(stub(routes(extra={("GET", WHITELIST_PATH): httpx.Response(503)})))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header, [GATED_ROW]))
    assert flash(response) == "The whitelist could not be read, so the recipient was not verified."


# --- totals ------------------------------------------------------------------------------------


def test_totals_are_decimal_and_count_only_the_validated_rows():
    """Money is a decimal string end to end; a float sum of two-decimal strings
    is the classic way to present a total that is not the sum."""
    rows = [
        {"amount": "0.10", "errors": []},
        {"amount": "0.20", "errors": []},
        {"amount": "1000000.05", "errors": []},
        {"amount": "999.99", "errors": [{"detail": "bad"}]},
    ]
    totals = batches.totals(rows, "USD")
    assert totals.by_currency == {"USD": "1000000.35"}
    assert Decimal(totals.by_currency["USD"]) == Decimal("0.10") + Decimal("0.20") + Decimal(
        "1000000.05"
    )
    assert (totals.rows, totals.valid, totals.invalid) == (4, 3, 1)


def test_a_wide_total_is_neither_rounded_nor_rendered_as_an_exponent():
    """Decimal's default context is 28 SIGNIFICANT digits
    — three 32-digit rows overflow it, and the sum silently loses its cents; and
    `str(Decimal)` past that width gives `3.70E+29`, which would reach both the
    confirm screen and `payments.over_ceiling`. Absurd amounts, deliberately: the
    property is that this function never quietly reports a number that is not the
    sum, and the only way to assert that is at the width where it used to."""
    row = "123456789012345678901234567890.01"
    totals = batches.totals([{"amount": row, "errors": []}] * 3, "USD")
    stated = totals.by_currency["USD"]
    assert stated == "370370367037037036703703703670.03"
    assert "E" not in stated and "e" not in stated
    # The comparison needs a wide context too — `Decimal(row) * 3` under the
    # default one rounds exactly the way the bug did, which is the point.
    with localcontext() as ctx:
        ctx.prec = 60
        assert Decimal(stated) == Decimal(row) * 3
    # …and the ceiling reads the same string the operator was shown.
    with settings_override(money_ceiling="1000.00"):
        assert stated in (payments.over_ceiling(stated) or "")


def test_a_batch_with_no_valid_rows_states_no_total_rather_than_zero():
    totals = batches.totals([{"amount": "5", "errors": [{"detail": "bad"}]}], "USD")
    assert totals.by_currency == {}


async def test_the_report_names_the_rows_its_totals_exclude():
    """The totals an operator approves say what they left out — not merely leave
    it out."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(web, [ROW, {**ROW, "amount": "2.50"}, {**ROW, "amount": "x"}])
        )
    assert "3 rows · 2 valid · 1 invalid" in report.text
    assert "12.50 USD across the valid rows" in report.text
    assert "1 row excluded by validation" in report.text
    assert "not included in these totals" in " ".join(report.text.split())


# --- lifecycle ------------------------------------------------------------------------------------


async def test_an_invalid_row_blocks_mark_ready_and_the_correction_is_a_new_batch(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW, {**ROW, "amount": "x"}])
        report = await web.get(url)
        assert "upload it again" in report.text
        response = await post(web, f"{url}/ready")
        assert "2 rows are invalid" in httpx.URL(response.headers["hx-redirect"]).params["err"]

        # The correction: a second file, a second batch, the first abandonable.
        again = await uploaded(web, [ROW, {**ROW, "amount": "2.50"}])
        assert again != url
        assert (
            "err=" not in (await post(web, f"{url}/abandon")).headers["hx-redirect"]
        )

    states = {
        str(batch.id): batch.status
        for batch in (await session.execute(select(PayoutBatch))).scalars()
    }
    assert states[url.rsplit("/", 1)[-1]] == "abandoned"
    assert states[again.rsplit("/", 1)[-1]] == "validating"


async def test_a_route_that_requires_a_document_will_not_go_ready_without_one(session):
    """The batch-level shared document, gated by the same discovery flag the
    single payout form obeys."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
        refused = await post(web, f"{url}/ready")
        assert (
            httpx.URL(refused.headers["hx-redirect"]).params["err"]
            == "Conduit requires a supporting document for this route; attach one before sending."
        )

        await upload_document(web, purpose="transaction_support", filename="doc_batch_1.png")
        accepted = await post(web, f"{url}/ready", b"documentIds=doc_batch_1")
        assert "msg=" in accepted.headers["hx-redirect"]
        report = await web.get(url)

    assert "Ready to dispatch — nothing has been sent" in report.text
    assert "Dispatching will send 1 payout totaling" in report.text
    assert "10.00" in report.text and "each is sent exactly once" in report.text
    batch = (await session.execute(select(PayoutBatch))).scalar_one()
    assert batch.status == "ready" and batch.document_ids == ["doc_batch_1"]


async def test_a_document_this_operator_did_not_upload_here_is_refused(session):
    """OPERATIONS_SPEC §3's attachment rule: a `doc_` id on a form is an
    assertion, not a fact — the batch page resolves it exactly as the payout form
    does."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
        response = await post(web, f"{url}/ready", b"documentIds=doc_somebody_elses")
    assert "could not be matched" in httpx.URL(response.headers["hx-redirect"]).params["err"]
    batch = (await session.execute(select(PayoutBatch))).scalar_one()
    assert batch.status == "validating" and batch.document_ids == []


async def test_a_gated_batch_needs_no_document_and_goes_ready(session):
    """`intercompany` asks for no document, so a file made only of intercompany
    rows needs none — the gate is the purposes the FILE holds, not the corridor."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [GATED_ROW, GATED_ROW])
        assert "msg=" in (await post(web, f"{url}/ready")).headers["hx-redirect"]
        report = await web.get(url)
    assert "Dispatching will send 2 payouts totaling" in report.text
    assert "20.00 USD" in report.text


async def test_an_abandoned_batch_accepts_nothing_further(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
        await post(web, f"{url}/abandon")
        again = await post(web, f"{url}/abandon")
        ready = await post(web, f"{url}/ready")
        report = await web.get(url)

    assert "already closed" in httpx.URL(again.headers["hx-redirect"]).params["err"]
    assert "cannot change" in httpx.URL(ready.headers["hx-redirect"]).params["err"]
    assert "This batch is abandoned." in report.text


async def test_a_viewer_can_read_a_report_but_cannot_change_it():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
    async with signed_in(app, groups="readers") as viewer:
        report = await viewer.get(url)
        assert report.status_code == 200
        assert "Read-only" in report.text
        assert (await post(viewer, f"{url}/ready")).status_code == 403
        assert (await post(viewer, f"{url}/abandon")).status_code == 403


async def test_another_customers_batch_is_not_found():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
        elsewhere = url.replace(CID, "cus_someone_else")
        assert (await web.get(elsewhere)).status_code == 404
        assert (await web.get(f"{BATCHES}/{uuid.uuid4()}")).status_code == 404


async def test_the_list_shows_this_customers_batches_and_their_counts():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await uploaded(web, [ROW, {**ROW, "amount": "x"}], filename="payroll-aug.csv")
        listing = await web.get(BATCHES)
    assert "payroll-aug.csv" in listing.text
    assert "1 invalid" in listing.text
    assert "Showing 1 batch" in listing.text


async def test_the_batch_list_pages_instead_of_stopping_at_fifty_in_silence(session):
    """`summaries` capped at 50 and neither the query nor the
    page said so, so a customer's 51st batch was on no page of this console —
    the one kind of truncation every other list here refuses to do quietly.

    The rows are built directly rather than uploaded: what is under test is the
    slice and its ordering, and 26 uploads would be 26 validation passes.
    """
    for n in range(26):
        session.add(
            PayoutBatch(
                customer_id=CID,
                rail="fedwire",
                recipient_type="business",
                destination_country="US",
                virtual_account_id="vac_1",
                asset="USD",
                fingerprint="f",
                template_fingerprint="f",
                filename=f"batch-{n:02d}.csv",
                status="validating",
                actor_id="ops@example.com",
                actor_email="ops@example.com",
            )
        )
    await session.commit()

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        first = (await web.get(BATCHES)).text
        second = (await web.get(f"{BATCHES}?limit=25&offset=25")).text

    assert first.count("batch-") == 25 and second.count("batch-") == 1
    assert f"{BATCHES}?limit=25&amp;offset=25" in first
    assert "Previous" in second
    # No batch is on both pages and none is missing: the tie-break is by id, so
    # rows written in the same instant cannot swap places between the two.
    on_screen = {
        name
        for text in (first, second)
        for name in re.findall(r"batch-\d\d\.csv", text)
    }
    assert len(on_screen) == 26


# --- what a batch is not -------------------------------------------------------------------


async def test_uploading_a_batch_creates_no_operation_and_sends_no_mutation(session):
    """An earlier round dispatches nothing: no `payout_create`, and the only Conduit
    traffic is the reads the page already makes."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        await uploaded(web, [ROW, ROW])

    assert (await session.execute(select(Operation))).scalars().all() == []
    assert [(method, path) for method, path, _ in calls if method != "GET"] == []
    rows = (await session.execute(select(PayoutBatchRow))).scalars().all()
    assert [row.operation_id for row in rows] == [None, None]


async def test_the_stored_rows_are_encrypted_at_rest(session):
    """The same posture as `counterparties.recipient` and
    `operations.request_body`: a batch row is a destination subtree, so the bytes
    on disk are ciphertext and the account number is not greppable."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await uploaded(web, [ROW])

    raw = (
        await session.execute(text("select payload from payout_batch_rows"))
    ).scalar_one()
    assert bytes(raw).startswith(b"gAAAAA")  # a Fernet token
    assert b"000123456789" not in bytes(raw)
    # And it reads back through the tolerant reader the address book uses.
    row = (await batches.rows_of(session, (await session.execute(select(PayoutBatch))).scalar_one().id))[0]
    assert row["recipient"]["accountNumber"] == "000123456789"


async def test_one_unreadable_row_does_not_take_the_report_down(session):
    """The lesson, on the second table that has to decrypt to render: a
    corrupt ciphertext renders as *unreadable* beside its neighbours rather than
    raising out of the query."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW, {**ROW, "amount": "20.00"}])
        await session.execute(
            text(
                "update payout_batch_rows set payload = 'not a fernet token'::bytea "
                "where row_number = 1"
            )
        )
        await session.commit()
        report = await web.get(url)

    assert report.status_code == 200
    assert "unreadable" in report.text
    assert "2 rows · 2 valid" in report.text  # validation happened at upload, not at render


@pytest.mark.parametrize("cells", [{}, {"amount": ""}])
def test_the_validator_is_pure_logic(cells):
    """No HTTP, no database: `Validator.row` is the same kind of object
    `forms.validate` is, which is what lets the e2e and the tests exercise the
    identical judgement."""
    from app import payments

    models = {GOODS: payments.payout_model(FEDWIRE_BUSINESS)}
    validator = batches.Validator(models=models, rail="fedwire")
    details = [error["detail"] for error in validator.row({**cells, "purpose": GOODS})["errors"]]
    assert "This field is required." in details
    assert payments.AMOUNT_MESSAGE in details
    assert validator.row({**cells, "purpose": GOODS})["amount"] is None
    # And with no purpose at all there is nothing to judge the row BY, which the
    # row says rather than pretending its fields were checked.
    bare = validator.row(cells)
    assert [error["detail"] for error in bare["errors"]] == [batches.NO_PURPOSE]


async def test_a_batch_funded_in_the_wrong_currency_is_refused_before_it_exists(session):
    """Item 11, batch level. The funding account is picked once and every row
    inherits it, so a doomed pair is not one failed payout — it is the whole
    file, each row accepted at create and each failing after review with
    `rail_unavailable` (live-proven, `tests/e2e/09_rail_asset_probe.py`).
    """
    from app.models import PayoutBatch
    from tests.payments_fixtures import EUR_ACTIVE, EUR_VID

    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", f"/v2/customers/{CID}/virtual-accounts"): page(
                        [USD_ACCOUNT, EUR_ACTIVE]
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        comments, header = await template_for(web, f"{ROUTE}&virtualAccountId={EUR_VID}")
        response = await send(
            web, build(comments, header, [ROW]), f"{ROUTE}&virtualAccountId={EUR_VID}"
        )

    assert "fedwire sends USD" in flash(response)
    assert "rail_unavailable" in flash(response)
    # Refused before the batch row exists — nothing to abandon, nothing to explain.
    assert (await session.execute(select(PayoutBatch))).first() is None


# --- multi-purpose batches ---------------------------------------------------------------
#
# The restructure round's own section: `purpose` is a column, one file may carry
# several, and every judgement a row gets is its own purpose's.


async def test_one_file_carries_two_purposes_and_each_row_is_judged_by_its_own(session):
    """The feature, end to end at upload: a goods row typed its destination, an
    intercompany row named a registered recipient, both valid, in one batch."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [ROW, GATED_ROW]))

    assert "2 rows · 2 valid · 0 invalid" in report.text
    assert "20.00 USD across the valid rows" in report.text
    # The report says which purpose each row was, because that is what decided
    # which requirements judged it.
    assert "Payment for goods or services" in report.text
    assert "Intercompany" in report.text

    rows = sorted(
        (await session.execute(select(PayoutBatchRow))).scalars().all(),
        key=lambda r: r.row_number,
    )
    assert [row.purpose for row in rows] == [GOODS, "intercompany"]
    # The gated row's coordinates are Conduit's registered record; the goods
    # row's are the ones the file typed.
    assert rows[0].payload["destination"]["recipient"]["legalName"] == ROW[
        "destination.recipient.legalName"
    ]
    assert rows[1].payload["destination"]["recipient"]["accountNumber"] == REGISTERED[
        "accountNumber"
    ]
    assert (rows[0].contact_id, rows[1].contact_id) == ("", REGISTERED["id"]) or (
        rows[0].contact_id is None and rows[1].contact_id == REGISTERED["id"]
    )


async def test_a_row_filling_another_purposes_column_is_refused_for_that_row():
    """The per-row half of the unknown-column rule, with the exact sentence: the
    union header has `routingNumber` because the goods purpose declares it, and
    an intercompany row filling it is a value that would not be sent."""
    app = make_app(stub(routes()))
    smuggled = {**GATED_ROW, "destination.recipient.routingNumber": "021000021"}
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [smuggled]))

    assert (
        "A intercompany row does not have 'destination.recipient.routingNumber'. That column "
        "belongs to another purpose's requirements, so the values under it would not have been "
        "sent — clear them, or change this row's purpose." in shown(report)
    )
    assert "0 valid · 1 invalid" in report.text


async def test_a_row_with_no_purpose_says_nothing_else_was_judged():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(await uploaded(web, [{**ROW, "purpose": ""}]))
    assert batches.NO_PURPOSE in shown(report)
    # And nothing else: judging the row against a schema it never claimed would
    # be inventing the complaint.
    assert "This field is required." not in report.text


async def test_the_purpose_column_takes_the_raw_key_not_the_label():
    """FORM_ENGINE_SPEC's verbatim rule, applied to the one column this console
    owns: the header block prints the label to read and the key to type, and the
    label is not accepted."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(web, [{**ROW, "purpose": "Payment for goods or services"}])
        )
    assert "is not a purpose this route can be read for" in shown(report)
    # And the *message* does not quote the label back: a purpose cell
    # holds whatever was typed, and only the message is kept in the row's
    # plaintext `errors` column. The page still prints the cell itself beside
    # the row — that is the purpose column, which this console stores verbatim
    # on purpose so the report can show what the file said.
    assert "'••••ices' is not a purpose" in shown(report)


def test_only_a_purpose_key_conduit_publishes_is_quoted_back_whole():
    """The rule behind that mask, stated on its own. The unknown-purpose
    refusal is stored forever in plaintext, so what it may quote is decided by
    membership of a bounded, published vocabulary — `payments.PURPOSE_VALUES`,
    the same seven keys every template header block prints — and never by the
    shape of the value. A key this corridor refuses is Conduit's word, not the
    operator's, and reads exactly as it always has; anything else is typed text
    and only its tail is shown, including text too short to have a tail.
    """
    assert batches._quotable_purpose("prefunding") == "prefunding"
    assert batches._quotable_purpose("Payment for goods or services") == "••••ices"
    assert batches._quotable_purpose("12345678901234567") == "••••4567"
    assert batches._quotable_purpose("pay") == "••••"


async def test_the_document_gate_is_the_purposes_the_file_actually_holds(session):
    """`intercompany` needs no document and the goods purpose does. A file of
    only intercompany rows is therefore ready with nothing attached; add one
    goods row and the same file is not."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        gated_only = await uploaded(web, [GATED_ROW])
        assert "msg=" in (await post(web, f"{gated_only}/ready")).headers["hx-redirect"]

        mixed = await uploaded(web, [GATED_ROW, ROW])
        refused = await post(web, f"{mixed}/ready")
        assert (
            httpx.URL(refused.headers["hx-redirect"]).params["err"]
            == "Conduit requires a supporting document for this route; attach one before sending."
        )


async def test_the_totals_are_unchanged_by_the_purpose_column(session):
    """Validated rows only, exclusions named beside them — the design owner's
    rule survives the row purposes differing."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        report = await web.get(
            await uploaded(
                web,
                [
                    ROW,
                    {**GATED_ROW, "amount": "2.50"},
                    {**ROW, "amount": "x"},
                    {**ROW, "purpose": "nonsense"},
                ],
            )
        )
    assert "4 rows · 2 valid · 2 invalid" in report.text
    assert "12.50 USD across the valid rows" in report.text
    assert "2 rows excluded by validation" in report.text


async def test_the_results_export_states_each_rows_purpose(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW, GATED_ROW])
        batch_id = url.rsplit("/", 1)[-1]
        exported = await web.get(
            f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}"
        )
    rows = [row for row in csv.reader(io.StringIO(exported.text)) if row]
    assert rows[0][:2] == ["row", "purpose"]
    assert [row[1] for row in rows[1:]] == [GOODS, "intercompany"]


# --- the batch screen (the fork's other arm) ----------------------------------------------


async def test_the_batch_screen_asks_for_a_corridor_and_not_a_purpose():
    """A batch's route is rail + recipient type. Asking for a purpose here
    would be asking the operator to choose something every row of the file
    then overrides — and there is no destination country to ask for either,
    since discovery's schema doesn't vary by it."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"{NEW}?{ROUTE}")

    row = response.text.split('id="route-row"')[1].split("</form>")[0]
    assert 'name="purpose"' not in row
    assert 'name="destinationCountry"' not in row
    for name in ("rail", "recipientType", "virtualAccountId"):
        assert f'name="{name}"' in row
    assert 'hx-trigger="change"' in row and 'hx-push-url="true"' in row
    # And the corridor's purposes are documented on the screen, with the raw key
    # the column takes — behind the house disclosure, because it is a reference
    # an operator needs once and the download is the verb.
    assert "<summary>What each purpose requires</summary>" in response.text
    assert "raw key" in shown(response)
    assert '<code class="raw">intercompany</code>' in response.text
    assert "Download template (CSV)" in response.text
    assert "data-batch-upload" in response.text
    # The verb comes first: the download bar is above the disclosure, not after
    # a table of seven purposes.
    assert response.text.index("Download template (CSV)") < response.text.index("<details>")
    # The union-on-screen gap is closed by pointing at where the whole answer
    # lives rather than by reprinting it (the accepted split).
    assert "the per-purpose detail is in the template" in shown(response)


async def test_the_batch_screen_offers_nothing_to_download_before_a_corridor():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(NEW)
    assert "Pick a rail and a recipient type" in response.text
    assert "template.csv" not in response.text


async def test_a_viewer_may_read_the_batch_screen_but_not_upload():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(f"{NEW}?{ROUTE}")
    assert response.status_code == 200
    assert "Download template (CSV)" in response.text
    assert 'type="file"' not in response.text
    assert "needs the <code>batch.upload</code> permission" in response.text


async def test_a_refused_upload_goes_back_to_the_batch_screen_not_the_payout_form():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        comments, header = await template_for(web)
        response = await send(web, build(comments, header + ["swiftCode"], [ROW]))
    assert httpx.URL(response.headers["hx-redirect"]).path == NEW


# --- retention --------------------------------------------------------------


async def seeded_batch(
    session, status: str, *, age_days: int, contact_label: str | None = "Globex"
) -> uuid.UUID:
    """One batch in `status`, last touched `age_days` ago, with one row carrying a
    destination. Built directly: what is under test is the purge's WHERE clause,
    and a real dispatch would prove nothing extra about it."""
    batch = PayoutBatch(
        customer_id=CID,
        rail="fedwire",
        recipient_type="business",
        destination_country="US",
        virtual_account_id="vac_1",
        asset="USD",
        fingerprint="f",
        template_fingerprint="f",
        filename=f"{status}.csv",
        status=status,
        actor_id="ops@example.com",
        actor_email="ops@example.com",
    )
    session.add(batch)
    await session.flush()
    session.add(
        PayoutBatchRow(
            batch_id=batch.id,
            row_number=1,
            purpose="goods_and_services",
            payload={"recipient": {"accountNumber": "1234567890"}},
            amount="10.00",
            contact_label=contact_label,
            errors=[],
        )
    )
    await session.commit()
    # `updated_at` carries `onupdate`, so it cannot be aged through the ORM.
    await session.execute(
        text("update payout_batches set updated_at = :when where id = :id"),
        {"when": datetime.now(UTC) - timedelta(days=age_days), "id": batch.id},
    )
    await session.commit()
    return batch.id


async def payloads(session) -> dict[uuid.UUID, dict | None]:
    rows = (
        await session.execute(
            select(PayoutBatchRow).execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.batch_id: row.payload for row in rows}


async def test_a_finished_batch_loses_its_destinations_after_the_window(session):
    """The rows of a dispatched batch hold the same `destination` subtree the
    operations they minted hold, and only the operation half was ever purged."""
    retention = get_settings().op_body_retention_days
    old = await seeded_batch(session, "dispatched", age_days=retention + 1)
    fresh = await seeded_batch(session, "dispatched", age_days=1)

    assert await batches.purge_row_payloads(session) == 1
    stored = await payloads(session)
    assert stored[old] is None
    assert stored[fresh] == {"recipient": {"accountNumber": "1234567890"}}
    # Everything the results export and the ledger read survives the purge.
    row = (
        await session.execute(select(PayoutBatchRow).where(PayoutBatchRow.batch_id == old))
    ).scalar_one()
    assert (row.amount, row.purpose, row.contact_label) == ("10.00", "goods_and_services", "Globex")
    assert await batches.purge_row_payloads(session) == 0  # not twice


async def test_an_abandoned_batch_ages_out_on_the_same_clock(session):
    """It was never sent at all, so there is even less reason to keep it."""
    old = await seeded_batch(
        session, "abandoned", age_days=get_settings().op_body_retention_days + 1
    )
    assert await batches.purge_row_payloads(session) == 1
    assert (await payloads(session))[old] is None


@pytest.mark.parametrize("status", ["validating", "ready", "partially_dispatched"])
async def test_a_batch_that_can_still_be_dispatched_keeps_its_destinations(session, status):
    """`partially_dispatched` is the `stalled` case: it is re-dispatchable, and
    the undispatched rows still need the destinations they would be sent with."""
    batch = await seeded_batch(session, status, age_days=3650)
    assert await batches.purge_row_payloads(session) == 0
    assert (await payloads(session))[batch] is not None


async def test_a_purged_row_says_so_on_the_page_and_in_the_export(session):
    """The third state. A purged row rendered a blank name over
    blank coordinates — byte for byte what a row with no destination renders —
    on the two surfaces an operator uses to find out where the money went. The
    batch was dispatched: it *had* a destination, and retention emptied it."""
    old = await seeded_batch(
        session,
        "dispatched",
        age_days=get_settings().op_body_retention_days + 1,
        contact_label=None,
    )
    fresh = await seeded_batch(session, "dispatched", age_days=1, contact_label=None)
    assert await batches.purge_row_payloads(session) == 1

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        purged_page = await web.get(f"/customers/{CID}/batches/{old}")
        purged_csv = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={old}")
        kept_page = await web.get(f"/customers/{CID}/batches/{fresh}")

    assert "destination purged after retention" in purged_page.text
    assert "destination purged after retention" in purged_csv.text
    # Non-vacuity: the row still holding its destination says nothing of the kind.
    assert "destination purged after retention" not in kept_page.text
