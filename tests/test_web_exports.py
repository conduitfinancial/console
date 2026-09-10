"""CSV export of every list surface.

An export is data leaving the console, so the assertions here are the egress
ones: it carries **the page's own filters** (wire-asserted, cursor by cursor), it
never states more than it read (the caps and the failure note are in the file),
it cannot become a formula on someone's laptop, it masks coordinates exactly as
the screen does, and every single one of them leaves an audit row behind.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import select, text

from app import counterparties
from app.models import AuditEvent
from app.web import display_name, exports
from tests.payments_fixtures import CID, DEPOSIT, PAYOUT, REGISTERED, page
from tests.web_harness import make_app, signed_in, stub

CUSTOMER = {
    "id": CID,
    "legalName": "ZZZTEST Ltd",
    "type": "business",
    "createdAt": "2026-08-01T09:00:00.000Z",
}
OTHER_CID = "cus_034Abbx1XrOVaY6sXUBtGU"
ACCOUNT_NUMBER = "000123456789"


def rows_of(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body)))


def records_of(body: str) -> list[list[str]]:
    """`rows_of` minus the header and the notes — just the records.

    A note is written into its own row whose first cell begins with `#`
    (`exports._stream`) exactly so a reader can tell it from a record.
    """
    return [row for row in rows_of(body)[1:] if not (row and row[0].startswith("#"))]


def routes(extra: dict | None = None) -> dict:
    return {
        ("GET", "/v2/transactions"): page([PAYOUT]),
        ("GET", "/v2/customers"): page([CUSTOMER]),
        ("GET", "/v2/applications"): page([]),
        ("GET", "/v2/rfis"): page([]),
        ("GET", "/v2/orders"): page([]),
        ("GET", f"/v2/customers/{CID}/whitelist-recipients"): page([REGISTERED]),
        **(extra or {}),
    }


# --- the filters are the page's, and the walk follows the cursor ----------------------------


async def test_the_walk_carries_the_pages_filters_and_follows_every_cursor():
    """The load-bearing property of the whole slice: what the export asks
    Conduit for is what the page asked, page after page.

    The cursor sequence is asserted on the wire — a walk that dropped the token
    would re-read page one forever, and a walk that dropped a filter would hand
    an operator a file about a different question than the screen they exported
    it from.
    """
    seen: list[httpx.QueryParams] = []
    pages = {None: "cur_1", "cur_1": "cur_2", "cur_2": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/transactions"
        seen.append(request.url.params)
        cursor = request.url.params.get("cursor")
        nxt = pages[cursor]
        return page([{**PAYOUT, "id": f"txn_{cursor or 'first'}"}], next_cursor=nxt)

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(
            "/export/transactions.csv"
            "?type=withdrawal&status=pending&customerId=" + CID + "&createdAfter=2026-08-01"
        )

    assert response.status_code == 200
    assert [q.get("cursor") for q in seen] == [None, "cur_1", "cur_2"]
    for query in seen:
        # Every filter the page parses, on every page of the walk.
        assert query["type"] == "withdrawal"
        assert query["status"] == "pending"
        assert query["customerId"] == CID
        assert query["createdAfter"] == "2026-08-01"
        # The page's sort, so the file is in the order the screen was.
        assert query["sortBy"] == "createdAt" and query["sortOrder"] == "desc"
        # Conduit's own maximum, never the operator's rows-per-page.
        assert query["limit"] == "100"
        assert "direction" not in query

    body = rows_of(response.text)
    assert body[0][0] == "transaction_id"
    assert [row[0] for row in body[1:]] == ["txn_first", "txn_cur_1", "txn_cur_2"]


def _types(request: httpx.Request) -> list[str]:
    """The `type` values one request carried, in order. `params.get` returns only
    the first of a repeated parameter, which is exactly what must not be
    asserted here."""
    return [value for key, value in parse_qsl(request.url.query.decode()) if key == "type"]


async def test_an_unknown_tab_exports_the_same_default_the_page_shows():
    """`list_query` is one function, so a junk `type` falls back identically on
    both sides — the export can never be a view the page cannot render. The
    fallback is now **All**, so both surfaces ask for the same four kinds."""
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # The page also reads /v2/customers for its name suggestions; this test
        # is about the ledger read the two share.
        if request.url.path == "/v2/transactions":
            seen.append(_types(request))
        return page([])

    app = make_app(handler)
    async with signed_in(app) as web:
        await web.get("/export/transactions.csv?type=obliterate")
        await web.get("/transactions?type=obliterate")
    four = ["deposit", "withdrawal", "deposit_return", "fiat_conversion"]
    assert seen == [four, four]


async def test_the_all_view_exports_every_kind_and_the_file_says_it_is_mixed():
    """**The export follows the parser** (the shared-parser contract): the All
    page carries no `type` at all, so `transactions.list_query` hands the walk
    the same four repeated values it hands the screen — one parser, two
    surfaces, no second definition of what "all" means.

    A CSV cannot wear a tab strip, so the file states what it is: several kinds
    in one feed, with the `type` column saying which each row is. The column
    itself predates this — it has been in the header since the surface shipped —
    which is why the All export needed a sentence rather than a schema change.
    """
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(_types(request))
            return page([PAYOUT, DEPOSIT])
        return page([CUSTOMER])

    app = make_app(handler)
    async with signed_in(app) as web:
        page_html = await web.get("/transactions")
        response = await web.get(_export_href(page_html.text))

    # The link the All page offers carries no `type`, and the two reads agree.
    assert "type=" not in _export_href(page_html.text)
    assert seen == [
        ["deposit", "withdrawal", "deposit_return", "fiat_conversion"],
        ["deposit", "withdrawal", "deposit_return", "fiat_conversion"],
    ]
    body = rows_of(response.text)
    assert body[0][1] == "type"
    assert [row[1] for row in records_of(response.text)] == ["withdrawal", "deposit"]
    assert exports.MULTI_KIND_NOTE in [row[0] for row in body]
    # A single-kind export is not a mixed one and says nothing of the sort.
    async with signed_in(app) as web:
        one = await web.get("/export/transactions.csv?type=withdrawal")
    assert "SCOPE — this is the ledger's All view" not in one.text


async def test_the_rows_per_page_control_never_reaches_the_export():
    """`limit` is a property of the screen. The export is the whole filtered
    set, so the link drops it and the walk uses Conduit's maximum."""
    seen: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(request.url.params.get("limit"))
        return page([])

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/transactions?type=withdrawal&limit=100")
        await web.get("/export/transactions.csv?type=withdrawal")
    assert seen == ["100", "100"]
    assert "limit=100" not in _export_href(response.text)


# --- the caps, stated in the file -----------------------------------------------------------


async def test_the_row_cap_stops_the_walk_and_the_file_says_so():
    """10,000 rows, and the file states the cap rather than ending quietly."""

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("cursor") or 0)
        return page(
            [{**PAYOUT, "id": f"txn_{start + n}"} for n in range(100)],
            next_cursor=str(start + 100),
        )

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")

    body = rows_of(response.text)
    assert len(body) == 1 + exports.CAP_ROWS + 1  # header + rows + the note
    assert body[-1][0] == exports.TRUNCATED_NOTE
    assert str(exports.CAP_ROWS) in exports.TRUNCATED_NOTE
    assert body[exports.CAP_ROWS][0] == f"txn_{exports.CAP_ROWS - 1}"


async def test_the_page_cap_stops_a_walk_of_tiny_pages():
    """A list that answers one row per page cannot spend an unbounded number of
    requests either — 200 pages is the other half of the cap."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("cursor") or 0)
        calls.append(start)
        return page([{**PAYOUT, "id": f"txn_{start}"}], next_cursor=str(start + 1))

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")

    assert len(calls) == exports.CAP_PAGES
    body = rows_of(response.text)
    assert len(body) == 1 + exports.CAP_PAGES + 1
    assert body[-1][0] == exports.TRUNCATED_NOTE


async def test_a_walk_that_reaches_the_end_carries_no_truncation_note():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")
    assert "TRUNCATED" not in response.text
    assert len(rows_of(response.text)) == 2


async def test_a_mid_walk_failure_produces_an_honest_partial_file():
    """Never a silently short file: the rows that were read, and a final row
    saying the rest were not."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(
                503, json={"type": "UPSTREAM", "title": "Conduit is having a moment"}
            )
        return page([PAYOUT], next_cursor="cur_1")

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")

    body = rows_of(response.text)
    assert body[1][0] == PAYOUT["id"]
    note = body[-1][0]
    assert note.startswith("# EXPORT INCOMPLETE")
    # A3: the note names the code, which is what support can act on — never the
    # vendor's sentence, which would land in a file a client may forward.
    assert "1 rows" in note and "Conduit refused this: UPSTREAM" in note
    # A failure is not a truncation: the file says which one it was, once.
    assert "TRUNCATED" not in response.text


async def test_a_failure_on_the_first_page_is_a_header_and_a_note_not_an_empty_file():
    app = make_app(lambda r: httpx.Response(500, json={"type": "XYZ_REFUSED", "title": "no"}))
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")
    body = rows_of(response.text)
    assert body[0][0] == "transaction_id"
    assert body[1][0].startswith("# EXPORT INCOMPLETE")


# --- CSV injection ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["=cmd|'/c calc'!A1", "+1+1", "-5.00", "@SUM(A1)", "   =1+1", "\t@x", "\r\n=1+1"],
)
def test_a_formula_shaped_cell_is_neutralised(value: str):
    """A spreadsheet evaluates a cell that starts with `=`, `+`, `-` or `@` —
    including after leading whitespace, which is why the rule looks past it."""
    assert exports._cell(value) == "'" + value


@pytest.mark.parametrize("value", ["ZZZTEST Ltd", "", "0.00", "a=b", None, True])
def test_an_ordinary_cell_is_left_alone(value):
    assert not exports._cell(value).startswith("'")


def test_a_boolean_column_says_true_and_false_not_pythons_spelling():
    assert (exports._cell(True), exports._cell(False), exports._cell(None)) == (
        "true",
        "false",
        "",
    )


async def test_the_quoting_reaches_a_real_exported_row():
    """The rule is worth nothing if it lives in a helper nothing calls: a
    formula-shaped customer name comes out of the route neutralised."""
    hostile = "=cmd|'/c calc'!A1"
    app = make_app(
        stub(routes({("GET", "/v2/customers"): page([{**CUSTOMER, "legalName": hostile}])}))
    )
    async with signed_in(app) as web:
        response = await web.get("/export/customers.csv")
    assert rows_of(response.text)[1][1] == "'" + hostile


async def test_an_ordinary_cell_is_left_alone():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get("/export/customers.csv")
    assert rows_of(response.text)[1][1] == "ZZZTEST Ltd"


async def test_a_cell_with_commas_and_quotes_survives_the_round_trip():
    """`csv.writer` owns quoting; this is the assertion that nothing hand-rolls
    it later."""
    tricky = 'Globex, "the" one\nwith a newline'
    app = make_app(stub(routes({("GET", "/v2/customers"): page([{**CUSTOMER, "legalName": tricky}])})))
    async with signed_in(app) as web:
        response = await web.get("/export/customers.csv")
    assert rows_of(response.text)[1][1] == tricky


# --- masking ----------------------------------------------------------------------------------


async def test_a_counterparty_export_never_contains_a_full_coordinate(session):
    """The address book is the easiest thing here to mail somewhere. It exports
    the last four and nothing more — the same policy as the screen."""
    await counterparties.save(
        session,
        customer_id=CID,
        label="Globex",
        recipient={
            "accountNumber": ACCOUNT_NUMBER,
            "iban": "DE89370400440532013000",
            "routingNumber": "021000021",
            "legalName": "ZZZTEST Globex Supplies LLC",
        },
        rail_family="us",
        recipient_type="business",
        destination_country="USA",
        actor_id="usr_1",
        actor_email="ops@example.com",
    )
    await session.commit()

    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"/export/counterparties.csv?customerId={CID}")

    assert ACCOUNT_NUMBER not in response.text
    assert "DE89370400440532013000" not in response.text
    assert "••••6789" in response.text and "••••3000" in response.text
    # Names are not coordinates and go out plain.
    assert "ZZZTEST Globex Supplies LLC" in response.text


async def test_a_recipient_export_masks_the_account_and_keeps_the_public_routing():
    """Exactly what the whitelist table renders: masked account coordinates, and
    the ABA/BIC whole because they identify a bank, not an account."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"/export/recipients.csv?customerId={CID}")

    assert REGISTERED["accountNumber"] not in response.text
    assert counterparties.mask(REGISTERED["accountNumber"]) in response.text
    assert REGISTERED["routingNumber"] in response.text


async def test_a_counterparty_row_that_cannot_be_decrypted_says_so(session):
    from app.models import Counterparty

    session.add(
        Counterparty(
            id=uuid.uuid4(),
            customer_id=CID,
            label="Broken",
            recipient={"accountNumber": ACCOUNT_NUMBER},
            rail_family="us",
            recipient_type="business",
            destination_country="USA",
            created_by_actor_id="usr_1",
            created_by_actor_email="ops@example.com",
        )
    )
    await session.commit()
    await session.execute(
        text("update counterparties set recipient = :junk where label = 'Broken'"),
        {"junk": b"not-a-fernet-token"},
    )
    await session.commit()

    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"/export/counterparties.csv?customerId={CID}")
    assert "unreadable" in response.text
    assert ACCOUNT_NUMBER not in response.text


# --- the local surfaces --------------------------------------------------------------------------


async def observe(session, virtual_account_id: str, customer_id: str = CID, code: str = "USD"):
    from app import projections

    await projections.apply_observation(
        session,
        resource_kind="virtual_accounts",
        resource_id=virtual_account_id,
        observed={
            "virtualAccountId": virtual_account_id,
            "customerId": customer_id,
            "asset": {"code": code},
            "status": "active",
        },
    )


async def test_the_accounts_export_applies_the_pages_filters_and_spends_one_read(session):
    """The same three filters the screen validates, and the same ONE bounded
    customers read the screen makes — never one per row.

    This test used to assert `calls == []`. The page's contract moved with the
    the decision (see `test_the_index_renders_fully_with_zero_conduit_
    answers`): what both surfaces owe is completeness without Conduit's answer,
    not abstention from asking. The file is still a local SELECT; the wire
    traffic is one page of customers, whatever the row count.
    """
    await observe(session, "vac_1")
    await observe(session, "vac_2", OTHER_CID, "EUR")
    calls: list = []
    app = make_app(stub({("GET", "/v2/customers"): page([CUSTOMER])}, calls))
    async with signed_in(app) as web:
        response = await web.get("/export/accounts.csv?asset=eur")

    assert [(method, path) for method, path, _ in calls] == [("GET", "/v2/customers")]
    body = rows_of(response.text)
    assert body[0] == [
        "virtual_account_id",
        "customer_id",
        "asset",
        "status",
        "observed_at",
        # Appended, never inserted: a saved spreadsheet formula must survive it.
        "customer_name",
    ]
    assert [row[0] for row in records_of(response.text)] == ["vac_2"]
    assert body[1][1:4] == [OTHER_CID, "EUR", "active"]


async def test_the_accounts_export_names_customers_and_says_what_a_blank_means(session):
    """The export's own honesty rules for the new column. A name it resolved is
    in the cell; a name it did not is an EMPTY cell beside the id — never a
    guess, never the id repeated as if it were a name — and the file states
    which of the two kinds of blank it is carrying."""
    await observe(session, "vac_named", CID)
    await observe(session, "vac_unnamed", OTHER_CID)

    app = make_app(stub({("GET", "/v2/customers"): page([CUSTOMER])}))
    async with signed_in(app) as web:
        resolved = (await web.get("/export/accounts.csv")).text
    # Nothing stubbed: the directory read is refused outright.
    app = make_app(stub({}))
    async with signed_in(app) as web:
        unread = (await web.get("/export/accounts.csv")).text

    named = {row[0]: row[5] for row in records_of(resolved)}
    assert named == {"vac_named": display_name(CUSTOMER), "vac_unnamed": ""}
    assert "# SCOPE — `customer_name` is resolved from the first 25 customers" in resolved

    # The read failed: every name blank, both rows still in the file, and the
    # note says the accounts are complete and only the names are missing.
    blank = {row[0]: row[5] for row in records_of(unread)}
    assert blank == {"vac_named": "", "vac_unnamed": ""}
    assert "# EXPORT INCOMPLETE — the customer directory could not be read" in unread
    assert "# SCOPE — `customer_name`" not in unread


async def test_the_accounts_export_is_the_whole_filtered_set_not_the_screens_page(session):
    """The page is paged now; the file is not. An export that stopped where the
    screen stops would be the screen, and the operator already has that one."""
    for n in range(30):
        await observe(session, f"vac_{n:02d}")

    app = make_app(stub({("GET", "/v2/customers"): page([CUSTOMER])}))
    async with signed_in(app) as web:
        # Paging parameters an operator's URL could carry are not filters.
        response = await web.get("/export/accounts.csv?limit=25&offset=25")

    assert len(records_of(response.text)) == 30


async def test_a_local_timestamp_exports_as_iso_utc(session):
    """A machine file, so never the `ts` filter's reading form."""
    await observe(session, "vac_1")
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get("/export/accounts.csv")
    observed = rows_of(response.text)[1][4]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00", observed), observed


async def test_a_conduit_timestamp_exports_exactly_as_conduit_stated_it():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get("/export/customers.csv")
    assert rows_of(response.text)[1][3] == CUSTOMER["createdAt"]


async def test_the_counterparty_cap_is_spent_in_sql_not_after_it(session, monkeypatch):
    """Every counterparty row costs a Fernet decrypt, so the
    export's row cap has to be a `LIMIT` — fetching a whole address book and
    slicing it is the budget escape the cap exists to prevent."""
    passed: dict = {}
    real = counterparties.rows

    async def spy(*args, **kwargs):
        passed.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(exports.cp, "rows", spy)
    app = make_app(stub({}))
    async with signed_in(app) as web:
        await web.get(f"/export/counterparties.csv?customerId={CID}")
    assert passed["limit"] == exports.CAP_ROWS + 1


async def test_that_limit_is_a_real_sql_limit(session):
    """The bound has to bite in the query. Two rows stored, one asked for, one
    returned — a helper that fetched both and let the caller slice would fail
    this only by returning two."""
    for label in ("Alpha", "Beta"):
        await counterparties.save(
            session,
            customer_id=CID,
            label=label,
            recipient={"accountNumber": ACCOUNT_NUMBER, "legalName": label},
            rail_family="us",
            recipient_type="business",
            destination_country="USA",
            actor_id="usr_1",
            actor_email="ops@example.com",
        )
    await session.commit()
    assert len(await counterparties.rows(session, CID)) == 2
    assert len(await counterparties.rows(session, CID, limit=1)) == 1


# --- a row the builders cannot read -------------------------------------------------------------

# `sourceAsset` is a string where every DTO says it is an object.
# Accepted by `client.page` (it is a dict in a list envelope) and fatal to
# `conversions.order_view`, which reaches into it.
MALFORMED_ORDER = {"id": "ord_bad", "status": "pending", "sourceAsset": "USD"}
GOOD_ORDER = {
    "id": "ord_ok",
    "status": "pending",
    "customerId": CID,
    "sourceAsset": {"code": "USD", "amount": "10.00"},
    "destinationAsset": {"code": "EUR", "amount": "8.60"},
    "createdAt": "2026-08-30T10:00:00.000Z",
}


async def test_a_row_no_builder_can_read_ends_the_file_honestly(session):
    """The file and the audit row must agree in every ending.

    Building the rows lazily inside the response put `build` *after* the audit
    row committed and after the headers went out: the download stopped mid-file
    while the ledger recorded a complete export. The rows are rendered before
    either happens now, so a malformed item produces the same honest partial
    ending as an unreachable Conduit.
    """
    app = make_app(stub(routes({("GET", "/v2/orders"): page([GOOD_ORDER, MALFORMED_ORDER])})))
    async with signed_in(app) as web:
        response = await web.get("/export/orders.csv")

    assert response.status_code == 200
    body = rows_of(response.text)
    assert [row[0] for row in body[1:-1]] == ["ord_ok"]  # the readable row still ships
    note = body[-1][0]
    assert note.startswith("# EXPORT INCOMPLETE") and "after 1 rows" in note

    event = (await session.execute(select(AuditEvent))).scalar_one()
    # The two halves of the same fact: one row left, and it was not the whole set.
    assert event.detail["rows"] == 1
    assert "AttributeError" in event.detail["failed"]
    assert event.detail["truncated"] is False


async def test_a_malformed_first_row_is_a_header_and_a_note(session):
    app = make_app(stub(routes({("GET", "/v2/orders"): page([MALFORMED_ORDER])})))
    async with signed_in(app) as web:
        response = await web.get("/export/orders.csv")
    body = rows_of(response.text)
    assert body[0][0] == "order_id"
    assert body[1][0].startswith("# EXPORT INCOMPLETE")
    assert (await session.execute(select(AuditEvent))).scalar_one().detail["rows"] == 0


async def test_a_malformed_row_after_the_cap_reports_incomplete_not_truncated(session):
    """Two endings cannot both be the headline. The rows past the bad one were
    never rendered, so what this file *is* is incomplete — and the audit row
    says the same thing the file does."""

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("cursor") or 0)
        items = [GOOD_ORDER] * 100 if start == 0 else [GOOD_ORDER, MALFORMED_ORDER] + [GOOD_ORDER] * 98
        return page(items, next_cursor=str(start + 100))

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get("/export/orders.csv")

    assert "TRUNCATED" not in response.text
    assert rows_of(response.text)[-1][0].startswith("# EXPORT INCOMPLETE")
    event = (await session.execute(select(AuditEvent))).scalar_one()
    assert event.detail == {
        "surface": "orders",
        "filters": {},
        "rows": 101,
        "truncated": False,
        "failed": "AttributeError on row 102",
    }


async def test_a_readable_list_still_carries_no_failure(session):
    """The guard must not label a healthy export as a partial one."""
    app = make_app(stub(routes({("GET", "/v2/orders"): page([GOOD_ORDER])})))
    async with signed_in(app) as web:
        response = await web.get("/export/orders.csv")
    assert "INCOMPLETE" not in response.text
    assert "failed" not in (await session.execute(select(AuditEvent))).scalar_one().detail


# --- the audit row -----------------------------------------------------------------------------


async def test_every_export_writes_one_audit_row(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        await web.get(f"/export/transactions.csv?type=withdrawal&status=pending&customerId={CID}")

    event = (await session.execute(select(AuditEvent))).scalar_one()
    assert event.action == "export.csv"
    assert event.actor_email == "ops@example.com"
    assert event.detail == {
        "surface": "transactions",
        "filters": {"type": "withdrawal", "status": ["pending"], "customerId": CID},
        "rows": 1,
        "truncated": False,
    }
    # A sort is not a filter and has no business in the egress record.
    assert "sortBy" not in event.detail["filters"]


async def test_the_audit_row_records_a_truncated_export_as_truncated(session):
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("cursor") or 0)
        return page([{**PAYOUT, "id": f"txn_{start}"}], next_cursor=str(start + 1))

    app = make_app(handler)
    async with signed_in(app) as web:
        await web.get("/export/transactions.csv?type=withdrawal")
    event = (await session.execute(select(AuditEvent))).scalar_one()
    assert event.detail["truncated"] is True
    assert event.detail["rows"] == exports.CAP_PAGES


async def test_the_audit_row_records_a_partial_export_as_failed(session):
    app = make_app(lambda r: httpx.Response(500, json={"type": "XYZ_REFUSED", "title": "no"}))
    async with signed_in(app) as web:
        await web.get("/export/transactions.csv?type=withdrawal")
    event = (await session.execute(select(AuditEvent))).scalar_one()
    assert event.detail["rows"] == 0
    assert event.detail["failed"] == "500 Conduit refused this: XYZ_REFUSED"  # A3


async def test_a_local_export_is_audited_with_its_own_scope(session):
    app = make_app(stub({}))
    async with signed_in(app) as web:
        await web.get(f"/export/counterparties.csv?customerId={CID}")
    event = (await session.execute(select(AuditEvent))).scalar_one()
    assert event.action == "export.csv"
    assert event.detail == {
        "surface": "counterparties",
        "filters": {"customerId": CID},
        "rows": 0,
        "truncated": False,
    }


# --- roles, empties, refusals --------------------------------------------------------------------


async def test_a_viewer_may_export(session):
    """A read-only operator sees the list; the export is the same read, so it is
    the same permission. Masked stays masked for everyone."""
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")
    assert response.status_code == 200
    assert rows_of(response.text)[1][0] == PAYOUT["id"]
    assert (await session.execute(select(AuditEvent))).scalar_one().actor_email == "ops@example.com"


async def test_a_zero_row_view_exports_a_header_only_file(session):
    app = make_app(stub(routes({("GET", "/v2/transactions"): page([])})))
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal")
    assert response.status_code == 200
    assert rows_of(response.text) == [list(exports.CONDUIT_SURFACES["transactions"][2])]
    # Still an egress event: someone asked for this customer's transactions.
    assert (await session.execute(select(AuditEvent))).scalar_one().detail["rows"] == 0


async def test_an_unknown_surface_is_a_404_and_writes_nothing(session):
    app = make_app(stub({}))
    async with signed_in(app) as web:
        assert (await web.get("/export/everything.csv")).status_code == 404
    assert (await session.execute(select(AuditEvent))).scalars().all() == []


async def test_a_customer_scoped_export_refuses_to_guess_the_customer(session):
    """An empty file would read as "this customer has none", which is a
    different fact from "no customer was named"."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        assert (await web.get("/export/recipients.csv")).status_code == 400
        # Contacts is a real cross-customer view, so its export no longer needs
        # a customer — but the capability filter still does, because Conduit
        # lists a whitelist per customer only.
        assert (
            await web.get("/export/counterparties.csv?capability=whitelisted")
        ).status_code == 400
    # A refused export is not an egress and leaves no row.
    assert (await session.execute(select(AuditEvent))).scalars().all() == []


async def test_one_customers_address_book_is_never_anothers(session):
    await counterparties.save(
        session,
        customer_id=CID,
        label="Globex",
        recipient={"accountNumber": ACCOUNT_NUMBER, "legalName": "ZZZTEST Globex"},
        rail_family="us",
        recipient_type="business",
        destination_country="USA",
        actor_id="usr_1",
        actor_email="ops@example.com",
    )
    await session.commit()
    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get(f"/export/counterparties.csv?customerId={OTHER_CID}")
    assert records_of(response.text) == []
    assert "ZZZTEST Globex" not in response.text


# --- the response itself --------------------------------------------------------------------------


async def test_the_response_is_a_streamed_utf8_csv_attachment_named_after_the_view():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get("/export/transactions.csv?type=withdrawal&status=pending")

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    # Streamed: no length is known when the first byte goes out.
    assert "content-length" not in response.headers
    name = re.search(r'filename="([^"]+)"', response.headers["content-disposition"]).group(1)
    assert name.startswith("transactions_")
    assert "type-withdrawal" in name and "status-pending" in name
    assert re.search(r"_\d{8}T\d{6}Z\.csv$", name)


def test_a_filename_cannot_smuggle_a_path_or_a_second_header():
    name = exports.filename("transactions", {"customerId": 'cus_/../"; x=1', "status": ["a b"]})
    assert '"' not in name and "/" not in name and ";" not in name
    assert name.startswith("transactions_")


def test_every_surface_is_reachable_and_has_stable_snake_case_headers():
    for surface in exports.SURFACES:
        table = exports.CONDUIT_SURFACES.get(surface) or exports.LOCAL_SURFACES[surface]
        headers = table[-2]
        assert headers, surface
        for column in headers:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", column), (surface, column)


# --- the eight links ---------------------------------------------------------------------------------


def _export_href(html: str) -> str:
    match = re.search(r'href="(/export/[^"]+)"', html)
    return match.group(1) if match else ""


@pytest.mark.parametrize(
    "url, surface",
    [
        ("/transactions?type=withdrawal", "transactions"),
        ("/customers", "customers"),
        ("/applications", "applications"),
        ("/rfis", "rfis"),
        ("/orders", "orders"),
        ("/accounts", "accounts"),
        ("/contacts", "counterparties"),
        (f"/customers/{CID}/contacts", "counterparties"),
    ],
)
async def test_every_list_surface_offers_an_export_link(url: str, surface: str):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(url)
    assert response.status_code == 200, response.text
    href = _export_href(response.text)
    assert href.startswith(f"/export/{surface}.csv"), (surface, href)
    assert "Export CSV" in response.text


async def test_the_link_carries_the_filters_on_screen_and_not_the_cursor():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(
            f"/transactions?type=withdrawal&status=pending&customerId={CID}"
            "&cursor=cur_9&direction=backward"
        )
    href = _export_href(response.text)
    assert "type=withdrawal" in href and "status=pending" in href and CID in href
    assert "cursor" not in href and "direction" not in href


async def test_the_two_customer_scoped_links_carry_their_customer():
    """The customer-scoped pages carry their customer in the path and their
    export carries it in the query. `/customers/{id}/contacts` is one of them
    even though the surface is no longer customer-*only*: the page is about one
    customer, so its file must be too."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (f"/customers/{CID}/contacts",):
            href = _export_href((await web.get(url)).text)
            assert f"customerId={CID}" in href


# --- a file may never claim a filter it did not run -------------


SECOND_CUSTOMER = {
    "id": OTHER_CID,
    "legalName": "ZZZTEST Ltd (Holdings)",
    "type": "business",
    "createdAt": "2026-08-02T09:00:00.000Z",
}


async def test_a_name_that_matches_two_customers_refuses_a_capability_export(session):
    """The capability filter is answerable for **one** customer —
    Conduit lists a whitelist per customer — and the page says so on screen
    (`unanswerable`). The export resolved a *name* to three customers, silently
    dropped the filter, and still put `capability-whitelisted` in the filename
    and in the PII-egress audit row. A file whose name claims a filter that did
    not run is worse than no file: it is evidence of the wrong thing.
    """
    app = make_app(stub(routes({("GET", "/v2/customers"): page([CUSTOMER, SECOND_CUSTOMER])})))
    async with signed_in(app) as web:
        refused = await web.get(
            "/export/counterparties.csv?customerId=ZZZTEST&capability=whitelisted"
        )
    assert refused.status_code == 400
    assert "one customer" in refused.text or "single customer" in refused.text


async def test_a_customers_export_applies_the_pages_own_name_walk(session):
    """`?name=` is not a Conduit parameter — the page walks the
    directory and matches locally. The export shared the parser that deliberately
    omits `name`, so it walked the same list and wrote **every** customer out
    while the operator believed they were exporting a search."""
    app = make_app(stub(routes({("GET", "/v2/customers"): page([CUSTOMER, SECOND_CUSTOMER])})))
    async with signed_in(app) as web:
        exported = await web.get("/export/customers.csv?name=Holdings")

    assert exported.status_code == 200
    records = records_of(exported.text)
    assert [row[0] for row in records] == [OTHER_CID], records
    # The filter that shaped the file is named in the file's name and its audit
    # row, like every other filter (the standing rule).
    assert "name-Holdings" in exported.headers["content-disposition"]
    logged = (
        (await session.execute(select(AuditEvent).where(AuditEvent.action == "export.csv")))
        .scalars()
        .all()
    )
    assert any(row.detail.get("filters", {}).get("name") == "Holdings" for row in logged)


async def test_a_capped_name_walk_is_stated_in_the_contacts_file(session):
    """`resolve_customers` returns `capped` and this branch dropped
    it on the floor. A walk that stopped short can hand back exactly one match
    that is not the only match — so the file must neither claim the scoped view
    nor stay silent about the ceiling."""
    from app.web import customers as customers_page

    many = [dict(CUSTOMER, id=f"cus_{n:022d}", legalName=f"ZZZTEST {n}") for n in range(100)]

    # Always another page, so the walk reaches its own ceiling — which is
    # exactly the state whose silence this test exists to catch.
    app = make_app(stub(routes({("GET", "/v2/customers"): page(many, next_cursor="more")})))
    async with signed_in(app) as web:
        exported = await web.get("/export/counterparties.csv?customerId=ZZZTEST")

    assert exported.status_code == 200
    assert "SEARCH CAPPED" in exported.text or "stopped at" in exported.text
    assert str(customers_page.NAME_WALK_PAGES * customers_page.NAME_WALK_LIMIT) in exported.text


async def test_a_customer_directory_outage_is_not_no_such_customer(session):
    """`resolve_customers` flattened a failed walk into "no ids",
    which reads as "that customer does not exist" — on the page and, worse, in
    the export's own refusal. An outage and an absence are different facts."""
    outage = httpx.Response(
        503,
        json={"type": "SERVICE_UNAVAILABLE", "title": "The customer directory is unavailable"},
    )
    app = make_app(stub(routes({("GET", "/v2/customers"): outage})))
    async with signed_in(app) as web:
        refused = await web.get("/export/counterparties.csv?customerId=ZZZTEST")
        listed = await web.get("/contacts?customerId=ZZZTEST")

    # The file is refused with the reason that is actually true — an operator
    # told "no customer matches" edits the name, and the name was never wrong.
    assert refused.status_code == 502
    assert "could not be read" in refused.text
    # The page carries Conduit's own problem card, states no absence it cannot
    # establish, and lists nobody's contacts under a filter it could not resolve.
    assert "Conduit is temporarily unavailable" in listed.text  # A3
    assert "No customer matches" not in listed.text
    assert "ZZZTEST Globex" not in listed.text
