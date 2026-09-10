"""The operator's console-local internal reference (Design A).

One file rather than four, because the property under test is one property held
across four capture points: **the note is stored here and goes nowhere else.**
The briefed version was stopped the briefed version of this feature after proving Conduit's
`clientReferenceId` is ledger-owned — `execute.outbound_body` overwrites it with
`op.id`, and two independent recovery paths match on that. So the assertions
that matter are the negative ones: nothing named `internal_reference`, and no
operator text at all, reaches any Conduit body from any of the four forms.

The second load-bearing property is that OPERATIONS_SPEC §1 is untouched: the
note is not in `request_body`, therefore not in `request_hash`, therefore the
double-submit guard behaves exactly as it did before the column existed.
"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select

from app import forms, operations
from app.config import get_settings
from app.models import Draft, Operation
from tests.payments_fixtures import (
    CID,
    CONVERSION_QUOTE,
    FEDWIRE_INTERCOMPANY,
    EUR_ACTIVE,
    EUR_VID,
    OTHER_CID,
    ORDER,
    PAYOUT,
    USD_ACCOUNT,
    VID,
    WHITELIST_PATH,
    encoded,
    page,
    payout_form,
)
from tests.test_web_convert import selection_of
from tests.test_web_onboarding import (
    APPLICATION,
    REQUIREMENTS,
    complete_body,
    discovery,
    read_the_review,
    upload_to,
)
from tests.test_web_payouts import NEW as PAYOUT_NEW
from tests.test_web_payouts import routes as payout_routes
from tests.test_web_transfers import NEW as TRANSFER_NEW
from tests.test_web_transfers import routes as transfer_routes
from tests.test_web_transfers import transfer_form
from tests.web_harness import form, make_app, post, signed_in, stub, upload

NOTE = "Acme Q3 invoice 4471 — chased by Dana"
CONVERT = f"/customers/{CID}/convert"

# Every Conduit body this suite provokes, so "it never leaves" can be asserted
# over all of them at once rather than one endpoint at a time.
def wire_text(calls: list) -> str:
    return " ".join(body.decode("utf-8", "replace") for _, _, body in calls)


def field_on(html: str) -> bool:
    return 'name="internal_reference"' in html


# --- the column ------------------------------------------------------------------------


def test_the_migration_adds_a_nullable_reference_column(schema):
    """Additive only: nullable, no default, no index (nothing queries by it —
    the reference is *displayed* wherever the operation renders, and there is no
    operations index page to host a filter yet)."""
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        column = next(
            c for c in inspect(conn).get_columns("operations") if c["name"] == "reference"
        )
        indexed = [
            i for i in inspect(conn).get_indexes("operations") if "reference" in i["column_names"]
        ]
    engine.dispose()
    assert column["nullable"] is True and column["default"] is None
    assert indexed == []


# --- capture point 1: payouts -----------------------------------------------------------


def payout_app(calls: list):
    return make_app(
        stub(payout_routes(extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)}), calls)
    )


async def test_the_payout_form_offers_the_field_with_the_agreed_help_text():
    app = payout_app([])
    async with signed_in(app) as web:
        response = await web.get(
            PAYOUT_NEW
            + "?purpose=payment_for_goods_or_services&rail=fedwire"
            "&recipientType=business&destinationCountry=USA"
        )
    assert field_on(response.text)
    assert "Internal reference" in response.text
    assert (
        "Your note for this operation — shows wherever this console displays it. Optional."
        in response.text
    )


async def test_a_payout_stores_the_note_and_never_sends_it(session):
    calls: list = []
    async with signed_in(payout_app(calls)) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(
            web,
            PAYOUT_NEW,
            encoded(payout_form(documentIds="doc_support_1", internal_reference=NOTE)),
        )
    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"

    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.reference == NOTE
    # On the row and nowhere else: not in the stored body (which is what
    # `request_hash` is computed over), and not on the wire.
    assert "internal_reference" not in json.dumps(op.request_body)
    assert NOTE not in json.dumps(op.request_body)
    sent = json.loads(next(c for c in calls if c[1] == "/v2/payouts")[2])
    assert NOTE not in json.dumps(sent) and "internal_reference" not in json.dumps(sent)
    # The one reference Conduit does get is still the ledger's own matcher.
    assert sent["clientReferenceId"] == str(op.id)


async def test_an_empty_box_stores_null_not_an_empty_string(session):
    """A column of empty strings is a column that lies about how often the field
    is used — and `{% if op.reference %}` would then have to carry the
    distinction the database refused to."""
    calls: list = []
    async with signed_in(payout_app(calls)) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        await post(
            web,
            PAYOUT_NEW,
            encoded(payout_form(documentIds="doc_support_1", internal_reference="   ")),
        )
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.reference is None


async def test_a_note_longer_than_the_column_is_truncated_not_refused(session):
    """The note is a label. Refusing a payout over the length of a comment would
    be the console getting in the way of the money."""
    calls: list = []
    async with signed_in(payout_app(calls)) as web:
        await upload(web, purpose="transaction_support", filename="doc_support_1.png")
        response = await post(
            web,
            PAYOUT_NEW,
            encoded(payout_form(documentIds="doc_support_1", internal_reference="x" * 400)),
        )
    assert response.headers["HX-Redirect"] == f"/transactions/{PAYOUT['id']}"
    op = (await session.execute(select(Operation).where(Operation.type == "payout_create"))).scalar_one()
    assert op.reference == "x" * 128


async def test_an_error_path_render_keeps_the_note_the_operator_typed():
    """`_render` is one function for the picker, the form and every error
    re-render, and its error callers used to drop the reference. Proved on the
    unreadable-whitelist path, which is the error render that still puts the
    form on screen: the problem card and the note appear together.

    The requirements-read 502 is the other case and it is handled differently —
    see `test_a_502_says_the_note_was_lost_rather_than_dropping_it_silently`."""
    app = make_app(
        stub(
            payout_routes(
                requirements=FEDWIRE_INTERCOMPANY,
                extra={
                    ("GET", WHITELIST_PATH): httpx.Response(
                        500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                    )
                },
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            PAYOUT_NEW,
            encoded(
                payout_form(
                    purpose="intercompany",
                    whitelistRecipientId="wlr_1",
                    internal_reference=NOTE,
                )
            ),
        )
    assert response.status_code == 422
    assert "Conduit refused this: SERVER_ERROR" in response.text  # A3: the code, not the title
    assert NOTE in response.text


async def test_a_502_says_the_note_was_lost_rather_than_dropping_it_silently():
    """The no-snapshot case. Discovery failed, so `_render` falls back to the
    route picker: there is no submit form on the page, and therefore nowhere to
    put the note back. Carrying it anyway is not an option — the picker's next
    step is a **GET** form, so a carried note would end up in a URL, in the
    server's query log and in the operator's history, which is exactly what the
    no-personal-data-in-URLs rule forbids.

    So the page says it. The note is *displayed* (escaped, truncated) and the
    second assertion is the load-bearing one: it appears nowhere inside an
    attribute — no href, no action, no query string."""
    app = make_app(
        stub(
            payout_routes(
                extra={
                    ("GET", "/v2/payouts/requirements"): httpx.Response(
                        503, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web, PAYOUT_NEW, encoded(payout_form(internal_reference=NOTE))
        )

    assert response.status_code == 502
    html = response.text
    assert "Conduit refused this: SERVER_ERROR" in html  # A3
    assert "Your note was not saved with this attempt" in html
    assert "it will need re-entering when the form reloads" in html
    # The amount is not on this page either (its input lives in the missing
    # form), so it is named too.
    assert "The amount (1000.00) will need re-entering too." in html
    # Displayed, escaped, and never in a URL. Every href/action on the page is
    # checked whole, including the percent- and entity-encoded spellings a
    # carried value would take.
    assert "Acme Q3 invoice 4471" in html
    urls = re.findall(r'(?:href|action)="([^"]*)"', html)
    assert urls, "the picker page should still have links"
    for url in urls:
        assert "Acme" not in unquote_plus(url.replace("&amp;", "&"))
        assert "internal_reference" not in url


async def test_a_long_note_is_truncated_in_the_lost_note_line():
    """Display truncation only — nothing was stored, so there is nothing this
    could truncate away."""
    long_note = "Z" * 200
    app = make_app(
        stub(
            payout_routes(
                extra={
                    ("GET", "/v2/payouts/requirements"): httpx.Response(
                        503, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web, PAYOUT_NEW, encoded(payout_form(internal_reference=long_note))
        )
    assert response.status_code == 502
    assert "Z" * 79 + "…" in response.text
    assert "Z" * 100 not in response.text


async def test_a_422_re_render_keeps_the_note_the_operator_typed():
    """`DOCUMENTATION_REQUIRED` and friends re-render the form; a note that
    vanished on the way back would be retyped, or lost."""
    async with signed_in(payout_app([])) as web:
        refused = await post(
            web, PAYOUT_NEW, encoded(payout_form(amount="", internal_reference=NOTE))
        )
    assert refused.status_code == 422
    assert NOTE in refused.text


# --- capture point 2: transfers ----------------------------------------------------------


async def test_a_transfer_stores_the_note_and_never_sends_it(session):
    calls: list = []
    app = make_app(
        stub(
            transfer_routes(
                accounts=[USD_ACCOUNT],
                extra={("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT)},
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        # The virtual-account arm's own route: a source account and a
        # destination customer, no rail and no registered recipient.
        rendered = await web.get(
            TRANSFER_NEW + f"?virtualAccountId={VID}&destination={OTHER_CID}"
        )
        assert field_on(rendered.text)
        response = await post(web, TRANSFER_NEW, transfer_form(internal_reference=NOTE))

    assert response.status_code == 204, response.text
    op = (await session.execute(select(Operation))).scalar_one()
    assert op.reference == NOTE
    assert NOTE not in json.dumps(op.request_body)
    assert NOTE not in wire_text(calls)


# --- capture point 3: convert (the order-creation step) -----------------------------------


async def test_choosing_a_conversion_option_stores_the_note_and_never_sends_it(session):
    """The note rides in on the Choose form's `hx-include`, not in the signed
    selection: it is the operator's label, not part of the price they agreed to.
    Nothing about the order body changes."""
    calls: list = []
    app = make_app(
        stub(
            {
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT, EUR_ACTIVE]),
                ("POST", "/v2/quotes"): httpx.Response(201, json=CONVERSION_QUOTE),
                ("POST", "/v2/orders"): httpx.Response(202, json=ORDER),
                ("GET", f"/v2/orders/{ORDER['id']}"): httpx.Response(200, json=ORDER),
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        quoted = await post(
            web,
            CONVERT + "/quote",
            encoded(
                {
                    "source": VID,
                    "destination": EUR_VID,
                    "amount": "1000.00",
                    "lockSide": "source",
                    "internal_reference": NOTE,
                }
            ),
        )
        assert field_on(quoted.text)
        # It survived the quote round-trip, and the Choose forms read it live.
        assert NOTE in quoted.text
        assert 'hx-include="#internal-reference"' in quoted.text
        chosen = await post(
            web,
            CONVERT + "/select",
            encoded(
                {
                    "source": VID,
                    "destination": EUR_VID,
                    "selection": selection_of(quoted.text),
                    "internal_reference": NOTE,
                }
            ),
        )

    assert chosen.status_code == 204, chosen.text
    op = (await session.execute(select(Operation))).scalar_one()
    assert (op.type, op.reference) == ("order_create", NOTE)
    assert NOTE not in json.dumps(op.request_body)
    # Includes the quote call — a note must not leak into pricing either.
    assert NOTE not in wire_text(calls)


# --- capture point 4: onboarding (the dead `Draft.client_reference_id` path) ---------------


async def test_the_onboarding_start_page_collects_the_note_onto_the_draft(session):
    """`Draft.client_reference_id` has been rendering on /drafts and the
    dashboard with nothing ever writing to it. This is the write."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        start = await web.get("/onboarding")
        assert field_on(start.text)
        created = await post(web, "/onboarding", form(country="bgr", internal_reference=NOTE))
        listed = await web.get("/drafts")

    draft_id = uuid.UUID(created.headers["HX-Redirect"].rsplit("/", 1)[-1])
    draft = await session.get(Draft, draft_id)
    assert draft.client_reference_id == NOTE
    assert NOTE in listed.text  # the column the list was already rendering


async def test_the_drafts_note_lands_on_the_operation_and_not_in_the_body(session):
    """The other half of the dead path: it now reaches the operation's own
    column. It must NOT reach `body["clientReferenceId"]` — that key is
    `execute.outbound_body`'s, and it is `op.id`, the reconciler's matcher
    (OPERATIONS_SPEC §3)."""
    calls: list = []
    app = make_app(
        stub(discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}), calls)
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        created = await post(web, "/onboarding", form(country="bgr", internal_reference=NOTE))
        url = created.headers["HX-Redirect"]
        # `complete_body`'s three attachments, really uploaded against this draft
        # — the submit resolves them all before it sends.
        await upload_to(web, url, "doc_1")
        await upload_to(web, url, "doc_p0", person=0)
        await upload_to(web, url, "doc_p1", person=1)
        await post(web, f"{url}/save", complete_body(model))
        await post(web, f"{url}/review", complete_body(model))
        # The submit carries the review render's seal; a body without one
        # is refused before anything is sent.
        done = await post(web, f"{url}/submit", await read_the_review(web, url))

    assert done.status_code == 204, done.text
    op = (
        await session.execute(select(Operation).where(Operation.type == "onboarding_submit"))
    ).scalar_one()
    assert op.reference == NOTE
    assert NOTE not in json.dumps(op.request_body)
    sent = json.loads(next(c for c in calls if c[1] == "/v2/onboarding")[2])
    assert sent["clientReferenceId"] == str(op.id)
    assert NOTE not in json.dumps(sent)


# --- the surfaces ------------------------------------------------------------------------


async def _operation(session, reference: str | None) -> Operation:
    op, _ = await operations.start(
        session,
        type="payout_create",
        actor_id="usr_1",
        actor_email="ops@example.com",
        path="/v2/payouts",
        body={"amount": "1.00"},
        customer_id=CID,
        reference=reference,
    )
    return op


@pytest.mark.parametrize("reference", [NOTE, None])
async def test_the_operations_panel_shows_the_note_only_when_set(session, reference):
    op = await _operation(session, reference)
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"/operations/{op.id}")
    assert response.status_code == 200
    assert ("<th>Reference</th>" in response.text) is (reference is not None)
    assert (NOTE in response.text) is (reference is not None)


@pytest.mark.parametrize("reference", [NOTE, None])
async def test_the_dashboard_attention_rows_show_the_note_only_when_set(session, reference):
    await _operation(session, reference)
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get("/")
    assert response.status_code == 200
    assert (NOTE in response.text) is (reference is not None)


# --- OPERATIONS_SPEC §1: the guard is unchanged ------------------------------------------


async def test_the_note_is_outside_the_request_hash(session, actor):
    """Two identical bodies under two different notes are one payment, and one
    operation. If the reference had gone into `request_body` every hash would be
    unique and the double-submit guard would quietly stop existing."""
    first, new_first = await operations.start(
        session, type="payout_create", **actor, path="/v2/payouts", body={"a": 1}, reference="one"
    )
    second, new_second = await operations.start(
        session, type="payout_create", **actor, path="/v2/payouts", body={"a": 1}, reference="two"
    )
    assert (new_first, new_second) == (True, False)
    assert second.id == first.id
    # The first note stands: the second submit created nothing to label.
    assert second.reference == "one"
    assert first.request_hash == operations.request_hash("/v2/payouts", {"a": 1})
