"""The transactions ledger: All, tabs, filters, typed detail, unknown-type fallback.

`type` is a repeatable array on the endpoint and a multi-type read comes back as
one feed with one cursor, so the first assertions here are that the default view
asks for the four fiat kinds in ONE request and that a single-kind deep link
still means exactly that kind. After that: each typed view shows
the things an operator would otherwise have to ask support for — the stage a
payout is stuck at, the failure code, the RFI, the SWIFT UETR — and a type this
build has never seen still renders what Conduit sent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus

import httpx
from sqlalchemy import select

from app.models import AuditEvent, Operation
from tests.conftest import settings_override
from tests.payments_fixtures import (
    CID,
    CONVERSION,
    DEPOSIT,
    FIXTURES,
    PAYOUT,
    encoded,
    page,
)
from tests.web_harness import (
    cells_with_hero,
    forbidden_affordances,
    form,
    make_app,
    post,
    hero_numerals,
    signed_in,
    signed_in_as,
    stub,
    upload,
)

LIST = "/transactions"
# An operation id, which is what `clientReferenceId` holds on everything this
# console sends (`app/conduit/execute.py:outbound_body`). Copied from the live
# sandbox payout in tests/e2e/sandbox_evidence/payout_completed.json.
LEDGER_REF = "9474ca3c-5542-4d28-b8e1-11a574a4ff00"
DETAIL = f"/transactions/{PAYOUT['id']}"
SIMULATE = f"{DETAIL}/simulate"


CUSTOMER = {"id": CID, "legalName": "ZZZTEST Ltd", "type": "business"}


def routes(items=None, detail=PAYOUT, customers=(CUSTOMER,), extra=None):
    return {
        ("GET", "/v2/transactions"): page(items if items is not None else [PAYOUT]),
        ("GET", f"/v2/transactions/{detail['id']}"): httpx.Response(200, json=detail),
        # The list's second, convenience read: names for the customer filter.
        ("GET", "/v2/customers"): (
            customers if isinstance(customers, httpx.Response) else page(list(customers or []))
        ),
        **(extra or {}),
    }


# --- the list ------------------------------------------------------------------------------


def types_sent(query: str) -> list[str]:
    """The `type` values one request carried, in order — the wire fact this
    page's whole shape rests on. Parsed rather than substring-matched: a
    repeated parameter is exactly the thing `in` cannot tell you about."""
    return [value for key, value in parse_qsl(query) if key == "type"]


async def test_all_is_the_default_and_asks_for_the_four_kinds_in_one_read():
    """**The ledger opens on All** (human: "the client needs to see ALL
    transactions in one view by default, then filter by type").

    `type` is a repeatable array on `GET /v2/transactions` and Conduit answers a
    multi-type request with ONE newest-first feed carrying ONE cursor (the
    live probe, 2026-09-02), so All is one read of the four fiat
    kinds this console works in — not a fan-out, not a merge. The exact repeated
    parameter set is pinned here because it IS the feature: a kind dropped from
    it is a kind of payment that silently stops being on the default screen.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(request.url.query.decode())
            return page([PAYOUT, DEPOSIT])
        return httpx.Response(404, json={"type": "NOT_FOUND", "title": "no"})

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST)

    # One request, four repeated `type` values, in the console's own order.
    assert len(seen) == 1
    assert types_sent(seen[0]) == ["deposit", "withdrawal", "deposit_return", "fiat_conversion"]
    # The crypto kinds the spec's enum also has are not asked for: this console
    # is fiat-only by design, and All must not claim a coverage it never read.
    assert not {"onramp", "offramp", "conversion"} & set(types_sent(seen[0]))
    # The mixed feed is on screen — both kinds, one list, one cursor pager.
    assert PAYOUT["id"] in response.text and DEPOSIT["id"] in response.text
    assert "Showing 2 transactions" in response.text


async def test_the_tabs_are_all_first_then_the_four_kinds():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        default = (await web.get(LIST)).text
        one = (await web.get(LIST + "?type=withdrawal")).text

    # All is the first tab, it carries no `type` at all, and it is the active
    # one on a bare /transactions.
    assert '<a class="on" href="/transactions" aria-current="page">All' in default
    for kind in ("deposit", "withdrawal", "deposit_return", "fiat_conversion"):
        assert f'href="/transactions?type={kind}"' in default
    # A single-kind tab takes the active state off All and puts it on itself.
    assert '<a class="on" href="/transactions" aria-current="page">All' not in one
    assert '<a class="on" href="/transactions?type=withdrawal"' in one
    assert 'aria-current="page"' in one


async def test_a_single_kind_deep_link_still_means_that_one_kind():
    """The existing tabs are the filter that was asked for, so `?type=deposit`
    means what it has always meant: one kind on the wire, one kind on screen.
    Every bookmark, pager link and export URL in the wild keeps working."""
    seen: list[str] = []
    app = make_app(watcher(seen, [DEPOSIT]))
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=deposit")

    assert types_sent(seen[0]) == ["deposit"]
    assert '<input type="hidden" name="type" value="deposit">' in response.text
    assert 'href="/transactions?type=deposit">Reset</a>' in response.text


async def test_the_result_meta_line_counts_this_page_and_refreshes_this_view():
    """Three things that are easy to get wrong and invisible when they
    are: the count is *this page's* rows (not a total nobody sent), the singular
    is not "1 transactions", and Refresh keeps the operator's filters.

    `, newest first` is here and on none of the other three lists, because only
    this request carries `sortBy=createdAt&sortOrder=desc`.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        one = await web.get(LIST + "?type=withdrawal&customerId=" + CID)

    # An earlier round split the qualifiers out of the count: the count is the 800-weight
    # uppercase half, "newest first" the muted breakdown beside it.
    assert "Showing 1 transaction" in one.text and ">newest first<" in one.text
    assert f'href="/transactions?type=withdrawal&amp;customerId={CID}"' in one.text
    assert ">Refresh</a>" in one.text

    app = make_app(stub(routes(items=[PAYOUT, DEPOSIT])))
    async with signed_in(app) as web:
        many = await web.get(LIST)
    assert "Showing 2 transactions" in many.text and ">newest first<" in many.text


async def test_the_page_size_control_is_a_whitelist_and_rides_every_link():
    """Three sizes, validated server-side, and carried
    by everything that navigates away from this view — the pager and the filter
    form — because a page turn that quietly reverted to 25 is the console
    forgetting what it was asked.

    A value outside the whitelist is the default, not a 400 and not a clamp:
    the same treatment an unknown `type` or an unknown account `status` gets.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(request.url.query.decode())
            return page([PAYOUT], next_cursor="cur_n")
        return page([CUSTOMER])

    app = make_app(handler)
    async with signed_in(app) as web:
        chosen = await web.get(LIST + "?type=withdrawal&limit=50")
        await web.get(LIST + "?limit=7")
        await web.get(LIST + "?limit=101")

    assert "limit=50" in seen[0]
    assert "limit=25" in seen[1] and "limit=25" in seen[2]  # 7 and 101 are not on offer
    # Next carries it, the size links carry the filters, the current one is on,
    # and the filter form re-submits it.
    assert "limit=50&amp;cursor=cur_n" in chosen.text
    assert 'href="/transactions?type=withdrawal&amp;limit=100"' in chosen.text
    assert '<a class="btn selected" href="/transactions?type=withdrawal&amp;limit=50">50</a>' in chosen.text
    assert '<input type="hidden" name="limit" value="50">' in chosen.text
    assert "50 per page" in chosen.text


async def test_a_terminal_failure_flags_its_row():
    """DESIGN_DIRECTION "Tables": a row whose status is the terminal-failure
    family carries the 3px left flag. The class is set from the same PILL_TONES
    table the row's own pill reads (`is_exception`), so the flag and the pill
    cannot disagree — and an unknown status, which is never inferred to be
    terminal, is never flagged either."""
    failed = {**PAYOUT, "id": "txn_failed", "status": "failed"}
    unknown = {**PAYOUT, "id": "txn_weird", "status": "teleporting"}
    app = make_app(stub(routes(items=[PAYOUT, failed, unknown])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    rows = [chunk for chunk in html.split("<tr ") if "txn_" in chunk]
    flagged = {"txn_failed"}
    for row in rows:
        expected = any(name in row for name in flagged)
        assert ('class="exception"' in row.split(">")[0]) is expected, row[:120]


async def test_the_all_view_carries_a_type_column_and_a_single_kind_tab_does_not():
    """The Type column, and the ruling on where it goes.

    On All the kind is the one thing a row cannot be read without — so it is a
    column, in the house pairing (`enum_label` for the words, the wire value
    demoted under it in mono, exactly as the batch rows' purpose column does it).
    On a single-kind tab every cell would repeat the tab above it, so the column
    is absent: the tab strip already says which kind the page is.
    """
    app = make_app(stub(routes(items=[PAYOUT, DEPOSIT, CONVERSION])))
    async with signed_in(app) as web:
        every = (await web.get(LIST)).text
        one = (await web.get(LIST + "?type=withdrawal")).text

    assert "<th>Type</th>" in every
    # Words for the operator, wire value for the URL, the endpoint and Conduit.
    assert 'Withdrawal<div class="muted"><code class="raw">withdrawal</code></div>' in every
    assert 'Deposit<div class="muted"><code class="raw">deposit</code></div>' in every
    assert (
        'Fiat conversion<div class="muted"><code class="raw">fiat_conversion</code></div>' in every
    )
    assert "<th>Type</th>" not in one and '<code class="raw">withdrawal</code>' not in one


async def test_every_row_in_the_mixed_feed_wears_its_own_status_word():
    """A mixed feed's pills, checked per row rather than per page.

    The vocabulary itself is one table — `PILL_TONES["transactions"]` is the
    UNION of all seven transaction DTOs' `status` enums, pinned in both
    directions by tests/test_vocabulary_drift.py — so the kind a row is does not
    select a vocabulary. What this pins is that each row renders its OWN status
    through it: a pending withdrawal and a completed deposit on one screen, each
    with its own word and its own tone, and neither borrowing the other's.
    """
    app = make_app(stub(routes(items=[PAYOUT, DEPOSIT])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    payout_row = [chunk for chunk in html.split("<tr ") if PAYOUT["id"] in chunk][0]
    deposit_row = [chunk for chunk in html.split("<tr ") if DEPOSIT["id"] in chunk][0]
    assert '<span class="pill wait">Pending</span>' in payout_row
    assert '<span class="pill ok">Completed</span>' in deposit_row
    # Neither row wears the other's status, and nothing rendered as Unknown.
    assert "Completed" not in payout_row and "Pending" not in deposit_row
    assert "Unknown:" not in html


async def test_every_state_with_a_next_step_renders_its_sentence():
    """**"What happens next", widened to the ledger** (spec
    §4.1: the column "survives and should be promoted ... widen its coverage").

    The Overview has answered "whose problem is this" per *operation* state. The ledger
    — the other table an operator scans daily, and the longer one — answered it for no
    state at all. Every state that gained a sentence is asserted to render it, on its
    own row, from the map rather than from a copy of the words: a sentence edited in
    `payments` and forgotten in the template is exactly the drift this pins.
    """
    from app import payments

    rows = [
        {**PAYOUT, "id": f"txn_{state}", "status": state, "stage": None, "hasRfi": False}
        for state in payments.TRANSACTION_NEXT_STEP
    ]
    # Plus the two states that must render NOTHING: `completed` (nothing happens
    # next) and a status this build has never heard of (the console does not
    # know, and will not guess).
    rows.append({**PAYOUT, "id": "txn_done", "status": "completed", "stage": None})
    rows.append({**PAYOUT, "id": "txn_alien", "status": "teleported", "stage": None})

    app = make_app(stub(routes(items=rows)))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    def row_of(transaction_id: str) -> str:
        return [chunk for chunk in html.split("<tr ") if transaction_id in chunk][0]

    for state, sentence in payments.TRANSACTION_NEXT_STEP.items():
        assert sentence in row_of(f"txn_{state}"), f"{state} lost its next step"
    assert "next-step" not in row_of("txn_done")
    assert "next-step" not in row_of("txn_alien")


async def test_a_stage_conduit_named_beats_the_generic_next_step():
    """`stage` is the more precise answer to the same question — "awaiting the
    customer" names who is holding this up, where `pending` can only say that
    nobody has moved it. Two muted lines saying the same thing is the per-visit
    cost §4.5 spends this slice removing, so the stage wins and the sentence
    stands down. PAYOUT is `pending` with `stage: under_review`.
    """
    from app import payments

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "under review" in html
    assert payments.TRANSACTION_NEXT_STEP["pending"] not in html


async def test_the_teaching_prose_is_one_disclosure_away_and_word_for_word():
    """**Spec §4.5: demote, do not delete.** The ~90 words that used to sit
    between the filter bar and the first row are still on the page, verbatim,
    inside a `<details>`; the lede above is one sentence.

    Asserted as exact strings on purpose. "The copy itself is an asset — it is
    more honest than most treasury software manages — so it moves, it does not
    get rewritten", and a paraphrase would pass any looser check.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    disclosure = html.split("<details")[1].split("</details>")[0]
    for moved in (
        "Ledger reference: the reference this console stamps on everything it sends",
        "Paste one to find its transaction.",
        "External reference is the other side's",
        "Filter by contact: pick a customer on the <em>Withdrawal</em> tab.",
        "The tabs narrow it to one kind.",
    ):
        assert moved in disclosure, f"not inside the disclosure: {moved}"

    # And out of the flow above it: the disclosure is where they live now, not
    # a second copy of them.
    above = html.split("<details")[0]
    assert "Ledger reference: the reference" not in above
    assert "The tabs narrow it to one kind" not in above

    # The lede is ONE sentence. Counted on the rendered text of the element
    # itself rather than the template, because that is what an operator reads.
    lede = re.sub(r"<[^>]+>", "", html.split('class="muted lede">')[1].split("</p>")[0])
    assert lede.count(".") == 1, f"the lede is not one sentence: {lede.strip()!r}"


async def test_the_editorial_numeral_never_lands_in_a_ledger_cell():
    """§2.3's negative half, on the page it exists to protect. Twenty-five
    amounts at 44px is not emphasis, it is a wall — and the ledger is what this
    console is for. The list has no summary figure of its own, so it has no
    hero numeral at all.
    """
    app = make_app(stub(routes(items=[PAYOUT, DEPOSIT])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert hero_numerals(html) == []
    assert cells_with_hero(html) == []


async def test_the_records_own_amount_is_the_page_s_one_hero_numeral():
    """§2.3's positive half on a detail page: the transaction's amount is the
    one number the page is about, so it is the page's summary figure. It is
    NOT in a cell — every amount inside the tables below it stays at body size
    and in the mono face.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(DETAIL)).text

    figures = hero_numerals(html)
    assert len(figures) == 1, figures
    assert "987.50 USD" in html.split(figures[0])[1].split("</p>")[0]
    assert cells_with_hero(html) == []


async def test_a_row_shows_the_name_conduit_sent_and_falls_back_to_the_map():
    """Names over ids. Three sources, in the order the row trusts them: the
    payload's own `customerName` (every transaction view DTO carries one), then
    the bounded customers read's map, then nothing — and "nothing" is the bare
    id, never a placeholder."""
    unnamed = {**PAYOUT, "id": "txn_2", "customerId": CID}
    unnamed.pop("customerName")
    stranger = {**unnamed, "id": "txn_3", "customerId": "cus_elsewhere"}
    app = make_app(stub(routes(items=[PAYOUT, unnamed, stranger])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "ZZZTEST Console E2E EOOD" in html  # the payload's own answer
    assert "ZZZTEST Ltd" in html  # …and the map, for the row that carried none
    # The stranger is beyond the bounded read: id only, and no invented name.
    stranger_row = [c for c in html.split("<tr ") if "txn_3" in c][0]
    assert "<code>cus_elsewhere</code>" in stranger_row
    # The id is still on screen under every name — it is what gets copied.
    assert f"<code>{CID}</code>" in html


async def test_an_unreadable_list_does_not_report_a_count_of_zero():
    """Every list handler substitutes an empty `Page` when
    the read fails, so the meta line was rendering "Showing 0 transactions" off
    an error — stating as fact the one thing the console does not know. That is
    the same empty-vs-unreadable conflation recipients/list.html refuses in its
    own empty row, and this is the four lists' version of it.

    The banner keeps the detail; this line only stops claiming a number.
    """
    app = make_app(
        stub(
            {
                ("GET", "/v2/transactions"): httpx.Response(
                    503, json={"type": "UNAVAILABLE", "title": "Conduit is down"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(LIST)

    assert "Couldn't read this list" in response.text
    assert "Showing 0 transaction" not in response.text
    # …and one level down: the table's own empty row said "No deposit
    # transactions on this page", which is the same claim in a different font.
    assert "The list could not be read — see above." in response.text
    assert "on this page." not in response.text
    # The problem banner still carries what actually happened…
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    # …and it comes first: the reason, then the line that has no number for it.
    assert response.text.index("Conduit refused this: UNAVAILABLE") < response.text.index(
        "Couldn't read this list"
    )
    # Refresh survives — a failed read is exactly when it is wanted.
    assert ">Refresh</a>" in response.text


def watcher(seen: list[str], items: list[dict]):
    """Record the *ledger* read's query. The list also fetches a page of
    customers for the filter's name suggestions, so a handler that recorded
    every request would be asserting against whichever of the two `gather`
    happened to start first."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(request.url.query.decode())
        return page(items)

    return handler


async def test_an_unknown_tab_falls_back_rather_than_400ing_upstream():
    seen: list[str] = []
    handler = watcher(seen, [])

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=teleportation")
    assert response.status_code == 200
    assert "type=deposit" in seen[0] and "teleportation" not in seen[0]


async def test_the_filters_are_passed_through():
    seen: list[str] = []
    app = make_app(watcher(seen, [PAYOUT]))
    async with signed_in(app) as web:
        await web.get(
            LIST
            + f"?type=withdrawal&customerId={CID}&status=pending"
            "&createdAfter=2026-08-01&createdBefore=2026-08-31&externalReference=INV-1"
            f"&clientReferenceId={LEDGER_REF}"
        )
    query = seen[0]
    for expected in (
        "type=withdrawal",
        f"customerId={CID}",
        "status=pending",
        "createdAfter=2026-08-01",
        "createdBefore=2026-08-31",
        "externalReference=INV-1",
        f"clientReferenceId={LEDGER_REF}",
    ):
        assert expected in query


async def test_the_ledger_reference_filter_round_trips():
    """`GET /v2/transactions` takes `clientReferenceId` as well as
    `externalReference` — two different references (ours vs the provider's), so
    two boxes. This one carries the operation id `app/conduit/execute.py` stamps
    on every payout and order, which is the only way to walk from an operation
    back to its transaction."""
    seen: list[str] = []
    app = make_app(watcher(seen, [PAYOUT]))
    async with signed_in(app) as web:
        response = await web.get(LIST + f"?type=withdrawal&clientReferenceId={LEDGER_REF}")
    assert f"clientReferenceId={LEDGER_REF}" in seen[0]
    # Back out onto the form, so a filtered page shows what it is filtered by.
    assert f'name="clientReferenceId" value="{LEDGER_REF}"' in response.text
    # Not folded into the provider-side box, whose value is untouched.
    assert 'name="externalReference" value=""' in response.text


async def test_an_empty_ledger_reference_is_not_sent():
    seen: list[str] = []
    app = make_app(watcher(seen, []))
    async with signed_in(app) as web:
        await web.get(LIST + "?type=withdrawal&clientReferenceId=")
    assert "clientReferenceId" not in seen[0]


async def test_a_status_the_api_does_not_know_is_dropped_from_the_query():
    seen: list[str] = []
    app = make_app(watcher(seen, []))
    async with signed_in(app) as web:
        await web.get(LIST + "?type=withdrawal&status=teleported")
    assert "status=" not in seen[0]


async def test_the_list_links_each_row_to_its_detail_page():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=withdrawal")
    assert f'href="/transactions/{PAYOUT["id"]}"' in response.text
    # Stage as a sub-label on the row, in the console's words rather than the
    # wire value — `payments.stage_label`, the same function the detail page
    # uses (re-aimed: the list rendered the raw enum while
    # the detail rendered the phrase, which is one fact with two words).
    assert "under review" in response.text


async def test_the_customer_filter_takes_a_name_and_sends_an_id():
    """`GET /v2/transactions` filters by `customerId` and by
    nothing name-shaped, and `GET /v2/customers` has no `search` either — so the
    only place a name can become an id is the browser. The option's *value* is
    what the form submits; the label is what the operator knows."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert '<datalist id="known-customers">' in html
    assert f'<option value="{CID}">ZZZTEST Ltd</option>' in html
    assert 'list="known-customers"' in html and 'placeholder="name or cus_…"' in html
    # Suggestion, not constraint: no `required`, no `pattern`, still a text input.
    assert 'name="customerId"' in html and 'type="text"' in html


async def test_a_customer_without_a_legal_name_falls_back_to_its_own_id():
    """An individual carries first/last, and a customer that carries neither is
    still listed — a blank option would be an id an operator cannot pick."""
    app = make_app(
        stub(
            routes(
                customers=[
                    {"id": "cus_person", "firstName": "Aiko", "lastName": "Tanaka"},
                    {"id": "cus_bare"},
                ]
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert '<option value="cus_person">Aiko Tanaka</option>' in html
    assert '<option value="cus_bare">cus_bare</option>' in html


async def test_the_ledger_head_offers_the_launcher_not_one_of_the_three_verbs():
    """The pill says "Move money" and lands on `/orders#move-money`:
    payout, transfer and convert are all per-customer routes, and this page has
    no customer in context, so a pill naming one verb would promise a page that
    first has to ask who. Viewers, who cannot move money at all, see no pill."""
    pill = '<a class="btn primary" href="/orders#move-money">Move money</a>'
    app = make_app(stub(routes()))
    async with signed_in(app) as operator:
        operator_html = (await operator.get(LIST)).text
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as viewer:
        viewer_html = (await viewer.get(LIST)).text

    assert pill in operator_html
    assert operator_html.index('<div class="action">') < operator_html.index(pill)
    assert pill not in viewer_html


async def test_a_viewer_gets_the_suggestions_too():
    """The Transact launcher is operator-gated because a viewer cannot move
    money and the tray it fills is absent for them. This filter is not: a viewer
    reads this list and filters it, and the datalist is a read-only convenience
    over data the viewer can already open at /customers."""
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        html = (await web.get(LIST)).text

    assert f'<option value="{CID}">ZZZTEST Ltd</option>' in html


async def test_the_list_renders_when_the_customer_read_fails():
    """Two independent reads on one page. The names are a convenience; losing
    them costs the suggestions and nothing else — the filter still filters, the
    ledger still renders, and no banner claims an outage the main read did not
    hit."""
    app = make_app(
        stub(
            routes(
                customers=httpx.Response(
                    503, json={"type": "UNAVAILABLE", "title": "Customers down"}
                )
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=withdrawal")

    assert response.status_code == 200
    assert PAYOUT["id"] in response.text  # the list itself is untouched
    assert "Showing 1 transaction" in response.text
    assert "<option" not in response.text.split('id="known-customers"')[1].split("</datalist>")[0]
    # Silent: the convenience read's failure is not the list's problem.
    assert "Customers down" not in response.text
    assert "Couldn't read this list" not in response.text
    assert 'list="known-customers"' in response.text  # the input is as it was


async def test_the_suggestions_say_when_there_are_more_than_they_show():
    """A name-completion list that quietly stops at 25 would let an operator
    conclude a customer does not exist."""
    more = httpx.Response(
        200, json={"data": [CUSTOMER], "meta": {"mode": "cursor", "nextCursor": "c2"}}
    )
    app = make_app(stub(routes(customers=more)))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text
    assert "Customer suggestions are the first 25" in html
    assert 'href="/customers"' in html

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        assert "Customer suggestions are the first 25" not in (await web.get(LIST)).text


async def test_an_unreadable_list_is_a_problem_not_an_empty_page():
    app = make_app(
        stub(
            {
                ("GET", "/v2/transactions"): httpx.Response(
                    500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=withdrawal")
    assert "Conduit refused this: SERVER_ERROR" in response.text  # A3


# --- the withdrawal (payout) detail ----------------------------------------------------------


async def test_the_payout_detail_shows_everything_support_would_ask_for():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)

    assert response.status_code == 200
    assert "Pending" in response.text          # status pill
    assert "under review" in response.text     # stage, as a sub-label
    assert PAYOUT["swiftUetr"] in response.text
    assert "12.50" in response.text            # fee
    assert "25 bps" in response.text           # declared markup
    assert "INV-4471" in response.text         # remittance reference
    assert "rfi_9" in response.text            # the RFI it is waiting on
    assert "20260827MMQFMP0100001" in response.text  # the fedwire IMAD
    assert "Cancel this payout" in response.text


def row(label: str, html: str) -> str:
    """The value cell of the generic-walk row carrying this label, or `""` when
    the page has no such row. A substring assertion cannot tell "the address is
    withheld" from "the address is gone", and the mask turns on the difference."""
    marker = f'>{label}</th><td class="num">'
    if marker not in html:
        return ""
    start = html.index(marker) + len(marker)
    return html[start : html.index("</td>", start)]


# The withdrawal captured from staging, whole — the one fixture in this repo
# that carries a real payee's coordinates, which is why the masking assertions
# below are pinned to it rather than to a payload this file wrote.
LIVE_WITHDRAWAL = json.loads((FIXTURES / "payout_live_withdrawal.json").read_text())
LIVE_DETAIL = f"/transactions/{LIVE_WITHDRAWAL['id']}"


async def test_the_payout_detail_masks_the_payees_account_number_to_its_last_four():
    """The generic walk under Source/Destination printed every scalar of
    the recipient block, so the detail page of any us-rail withdrawal was a full
    account number on screen — the artefact `exports.py` and the contact list
    both promise does not exist ("a full account number or IBAN is in no column
    of any surface").

    What an operator needs off this page is which of two destinations this went
    to, and four digits says that. What the bank is, and where it is, stays
    whole: `routingNumber` is public routing data.
    """
    app = make_app(stub(routes(detail=LIVE_WITHDRAWAL)))
    async with signed_in(app) as web:
        html = (await web.get(LIVE_DETAIL)).text

    assert "000123456789" not in html
    assert "••••6789" in html
    # The bank identifiers an operator chases a wire with are untouched.
    assert "021000021" in html
    # …and so is the name on the account: it is what tells two payees apart.
    assert "ZZZTEST Globex Supplies 940a80c3b6c7" in html


async def test_the_payout_detail_withholds_the_payees_own_address_and_keeps_the_banks():
    """`postalAddress` is the payee's home or office; `bankAddress` is the
    bank's, which is public. The row stays on the page saying `withheld` — the
    console does not report its own suppression as an absence at Conduit (the
    Received-via rule, applied to a value instead of a table)."""
    app = make_app(stub(routes(detail=LIVE_WITHDRAWAL)))
    async with signed_in(app) as web:
        html = (await web.get(LIVE_DETAIL)).text

    assert row("Recipient · Postal address", html) == "withheld"
    assert "Recipient · Postal address · Address line1" not in html
    assert "500 Market St" in html  # the bank's address line, untouched
    assert "Recipient · Bank address · Address line1" in html


async def test_the_uetr_is_copyable_rather_than_retyped():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert 'data-copy="#copy-uetr"' in response.text and 'id="copy-uetr"' in response.text


async def test_a_payout_without_a_uetr_does_not_render_an_empty_row():
    app = make_app(stub(routes(detail={**PAYOUT, "swiftUetr": None})))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "SWIFT UETR" not in response.text


async def test_a_failed_payout_shows_its_failure_code():
    failed = {
        **PAYOUT,
        "status": "failed",
        "stage": None,
        "failureCode": "insufficient_funds_at_settle",
        "failureMessage": "The account balance fell below the amount before settlement.",
    }
    app = make_app(stub(routes(detail=failed)))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "insufficient_funds_at_settle" in response.text
    assert "fell below the amount" in response.text
    assert "Cancel this payout" not in response.text


async def test_a_settled_payout_offers_no_cancel_button():
    app = make_app(stub(routes(detail={**PAYOUT, "status": "completed"})))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "Cancel this payout" not in response.text
    assert "terminal state" in response.text


async def test_an_unknown_status_is_neither_cancellable_nor_terminal():
    app = make_app(stub(routes(detail={**PAYOUT, "status": "levitating"})))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "Unknown: levitating" in response.text
    assert "Cancel this payout" not in response.text
    assert "terminal state" not in response.text


async def test_an_unknown_stage_is_shown_raw_rather_than_dropped():
    app = make_app(stub(routes(detail={**PAYOUT, "stage": "waiting_for_godot"})))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "stage: waiting_for_godot" in response.text


async def test_the_linked_operation_panel_appears_when_this_console_sent_it(session):
    """The payout's own row in the ledger, so "did we send this twice" is
    answerable from the page the operator is already on."""
    from app import operations

    op, _ = await operations.start(
        session,
        type="payout_create",
        actor_id="usr_1",
        actor_email="ops@example.com",
        path="/v2/payouts",
        body={"customerId": CID},
    )
    await operations.transition(
        session, op.id, "in_flight", actor_id="usr_1", actor_email="ops@example.com"
    )
    await operations.transition(
        session,
        op.id,
        "confirmed",
        actor_id="usr_1",
        actor_email="ops@example.com",
        conduit_resource_id=PAYOUT["id"],
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)
    assert "This console's record" in response.text
    assert str(op.id) in response.text and "payout_create" in response.text


# --- held on an RFI ---------------------------------------------------------
#
# **Every assertion below is against a stub, and that is the ceiling, not a
# shortcut.** An RFI cannot be created in any environment this console can reach
# — Conduit's compliance side publishes them — so no live RFI has ever been
# observed and no live e2e is possible for any of it (the same "tested by
# inspection" honesty as onboarding). What *is* live evidence: the six `rfi.*`
# event-type names and their id-only payloads (`GET /v2/webhooks/event-types`,
# sandbox, read-only, 2026-08-30; see `test_worker.py`), the RFI DTOs in the
# pinned spec, and `hasRfi`'s own description — "True when at least one
# published (non-draft, non-cancelled) RFI targets this transaction. Always
# present; derived at read time, no stored column" — which is why it, and not a
# local flag, is what gates the read below.

TXN_RFI = {
    "id": "rfi_9",  # the id PAYOUT itself names in `rfiId`
    "title": "Invoice for this payment",
    "status": "open",
    "summary": "Send the commercial invoice for this transfer.",
    "subjects": [{"subjectType": "transaction", "subjectId": PAYOUT["id"]}],
    "rounds": [{"roundNumber": 1, "status": "open", "ask": "Attach the invoice."}],
    "dueAt": "2026-09-03T00:00:00.000Z",
    "createdAt": "2026-08-27T11:00:00.000Z",
    "updatedAt": "2026-08-27T11:00:00.000Z",
    "publishedAt": "2026-08-27T11:00:00.000Z",
}
RFIS = ("GET", "/v2/rfis")


async def test_a_payout_held_on_an_rfi_says_so_and_offers_the_answer(session):
    calls: list = []
    app = make_app(stub(routes(extra={RFIS: page([TXN_RFI])}), calls))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)

    # The held state is in the status line, not below the fold.
    assert "Held — information requested" in response.text
    assert "Conduit has asked for more information" in response.text
    # …and the panel is the application page's panel: round, ask, respond form.
    assert "Attach the invoice." in response.text
    assert 'hx-post="/rfis/rfi_9/respond"' in response.text
    assert 'value="transaction"' in response.text and f'value="{PAYOUT["id"]}"' in response.text
    assert 'data-purpose="rfi_response"' in response.text
    # The read is subject-scoped, and a GET never acknowledges anything.
    rfi_reads = [c for c in calls if c[1] == "/v2/rfis"]
    assert len(rfi_reads) == 1
    assert [c for c in calls if "acknowledge" in c[1]] == []
    # The unacknowledged open round asks the browser to POST that separately.
    assert 'hx-post="/rfis/rfi_9/acknowledge"' in response.text


async def test_a_transaction_without_an_rfi_costs_no_extra_read():
    """`hasRfi` is Conduit's own derivation and it is always present, so the
    common transaction pays nothing for this feature."""
    calls: list = []
    app = make_app(stub(routes(detail=DEPOSIT), calls))
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{DEPOSIT['id']}")

    assert [c for c in calls if c[1] == "/v2/rfis"] == []
    assert "Requests for information" not in response.text
    assert "Held — information requested" not in response.text


async def test_an_unreadable_rfi_is_never_reported_as_no_rfi():
    """Conduit said there is one. An empty list is a read that did not land."""
    app = make_app(stub(routes()))  # `/v2/rfis` unstubbed → 404
    async with signed_in(app) as web:
        response = await web.get(DETAIL)

    assert "could not be read just now" in response.text
    assert "rfi_9" in response.text  # the id from the transaction, to go on
    assert 'hx-post="/rfis/rfi_9/respond"' not in response.text


async def test_a_settled_rfi_is_history_not_a_to_do():
    """`hasRfi` stays true after the RFI resolves, so the panel outlives the
    hold — but nothing is owed and nothing is badged."""
    app = make_app(
        stub(routes(extra={RFIS: page([{**TXN_RFI, "status": "resolved"}])}))
    )
    async with signed_in(app) as web:
        response = await web.get(DETAIL)

    assert "Held — information requested" not in response.text
    assert "Settled — nothing is owed" in response.text
    assert 'hx-post="/rfis/rfi_9/respond"' not in response.text  # no round to answer


async def test_an_answered_rfi_holds_nothing_and_offers_no_second_answer():
    """`responded` is Conduit's turn: the payment is not "held,
    act now", the operator is not owed a form, and it is not settled either —
    only `rfi.more_info_requested` (which sets `open` again) puts it back on this
    side."""
    app = make_app(stub(routes(extra={RFIS: page([{**TXN_RFI, "status": "responded"}])})))
    async with signed_in(app) as web:
        response = await web.get(DETAIL)

    assert "Held — information requested" not in response.text
    assert "Conduit has asked for more information" not in response.text
    assert 'hx-post="/rfis/rfi_9/respond"' not in response.text
    assert "Answered — waiting on Conduit" in response.text
    # …and it is not called settled either: nobody has decided anything yet.
    assert "Settled — nothing is owed" not in response.text
    assert "Attach the invoice." in response.text  # the round is still readable


async def test_responding_from_the_transaction_page_comes_back_to_it(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    RFIS: page([TXN_RFI]),
                    ("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_inv_1"}),
                    ("POST", "/v2/rfis/rfi_9/responses"): httpx.Response(
                        201, json={"id": "rfi_9", "status": "responded"}
                    ),
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        # The real upload route: a `doc_` id only becomes attachable by passing
        # through this console's own ledger (`documents.attachable`).
        assert "doc_inv_1" in (await upload(web, purpose="rfi_response")).text
        response = await post(
            web,
            "/rfis/rfi_9/respond",
            form(
                subjectType="transaction",
                subject=PAYOUT["id"],
                message="Invoice attached.",
                email="ops@example.com",
                documentIds=["doc_inv_1"],
            ),
        )

    # `msg` rides with a `msgsig` companion, so the exact string is
    # no longer stable — the plaintext prefix still is.
    assert response.headers["HX-Redirect"].startswith(f"{DETAIL}?msg=Response+sent.&msgsig=")
    op = (
        await session.execute(select(Operation).where(Operation.type == "rfi_respond"))
    ).scalar_one()
    assert op.state == "confirmed"
    sent = json.loads(next(c for c in calls if c[1] == "/v2/rfis/rfi_9/responses")[2])
    assert sent["message"] == "Invoice attached." and sent["documentIds"] == ["doc_inv_1"]


async def test_a_response_naming_a_subject_this_console_cannot_show_lands_on_the_index():
    """The form names a subject, never a URL: the path is rebuilt from the fixed
    map in `app/rfis.py`, so nothing here can choose its own redirect target."""
    app = make_app(stub(routes(extra={RFIS: page([TXN_RFI])})))
    async with signed_in(app) as web:
        response = await post(
            web,
            "/rfis/rfi_9/respond",
            form(subjectType="https://evil.test", subject="x", message="  "),
        )
    assert response.headers["HX-Redirect"].startswith("/rfis?err=")


async def test_a_viewer_sees_the_rfi_but_is_offered_no_answer_and_no_acknowledge():
    calls: list = []
    app = make_app(stub(routes(extra={RFIS: page([TXN_RFI])}), calls))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(DETAIL)
        refused = await post(web, "/rfis/rfi_9/respond", form(message="x"))

    assert "Attach the invoice." in response.text  # reading is fine
    assert "respond" not in response.text and "acknowledge" not in response.text
    assert refused.status_code == 403
    assert [c for c in calls if c[1].startswith("/v2/rfis/")] == []


async def test_the_rfi_response_needs_the_csrf_header():
    app = make_app(stub(routes(extra={RFIS: page([TXN_RFI])})))
    async with signed_in(app) as web:
        response = await web.post(
            "/rfis/rfi_9/respond",
            content=encoded({"subjectType": "transaction", "subject": PAYOUT["id"]}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 403 and "CSRF" in response.text


async def test_the_ledger_list_badges_a_held_payment_with_no_local_flag():
    """`hasRfi` rides every row of the list read, so the badge needs no
    projection column and no second call."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(LIST + "?type=withdrawal")

    assert "information requested" in response.text
    assert [c for c in calls if c[1] == "/v2/rfis"] == []


# --- the deposit detail ----------------------------------------------------------------------


async def test_the_deposit_detail_shows_the_sender_and_the_rail():
    app = make_app(stub(routes(detail=DEPOSIT)))
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{DEPOSIT['id']}")
    assert "ZZZTEST Payer Inc" in response.text
    assert "CITIUS33XXX" in response.text
    assert "091000010000001" in response.text  # the ACH trace number: which rail it came over
    assert "Sender" in response.text
    assert "Cancel this payout" not in response.text


# --- the receiving leg of an internal transfer -------------------------------------------------

EVIDENCE = Path(__file__).parent / "e2e" / "sandbox_evidence"


def sender_section(html: str) -> str:
    """Everything between the Sender heading and the Destination one — the
    generic walk, and nothing the typed rows above it already said."""
    return html.split("<h2>Sender</h2>", 1)[1].split("<h2>Destination</h2>", 1)[0]


async def test_a_received_internal_transfer_is_typed_and_links_the_sending_transaction():
    """Drift-pinned against the live capture, the `order_view` precedent: the
    transaction is the sandbox's own body rather than a fixture this repo wrote,
    so if Conduit's shape moves the test reads the new truth and fails here
    instead of on an operator's screen.

    Conduit's internal transfer arm went live 2026-09-01; this is the deposit it
    lands on the receiving customer.
    """
    received = {
        k: v
        for k, v in json.loads((EVIDENCE / "va_transfer_received_deposit.json").read_text()).items()
        if not k.startswith("_")  # `_note` / `_observed_at` are the capture's own margin notes
    }
    assert received["source"]["type"] == "internal_transfer"

    app = make_app(stub(routes(detail=received)))
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{received['id']}")
    html = response.text

    assert "Received via" in html
    assert "Internal transfer" in html  # the label
    assert '<code class="raw">internal_transfer</code>' in html  # and the wire word beside it
    assert '<a href="/transactions/txn_034HRmGsnk0798PRyU7zUE">' in html

    # No fact twice: the generic walk below must not repeat what the typed row
    # states in full.
    sender = sender_section(html)
    assert "internal_transfer" not in sender
    assert "txn_034HRmGsnk0798PRyU7zUE" not in sender
    # …and the emptied table does not report the suppression as an absence at
    # Conduit — the two are different facts.
    assert "Conduit published no detail" not in sender
    assert "is in Received via, above" in sender
    # The sender's own reference is untouched — it is neutral for both
    # directions and says something the provenance row does not.
    assert "ZZZTEST-VA-PROBE" in html


async def test_an_unknown_source_type_gets_no_typed_row_and_keeps_its_generic_walk():
    """`internal_transfer` is typed by name, not by the presence of an
    originating id. A source kind this build has never heard of is rendered as
    Conduit sent it and claims nothing."""
    unknown = {
        **json.loads((EVIDENCE / "va_transfer_received_deposit.json").read_text()),
        "source": {
            "type": "carrier_pigeon",
            "originatingTransactionId": "txn_pigeon_1",
            "loftName": "ZZZTEST Loft",
        },
    }
    app = make_app(stub(routes(detail=unknown)))
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{unknown['id']}")
    html = response.text

    assert "Received via" not in html
    assert "Carrier pigeon" not in html
    sender = sender_section(html)
    # `side_rows` drops a side's own `type` for EVERY kind (`_SKIP_IN_GENERIC`),
    # which is why the typed row exists at all — but everything else it carries,
    # the originating id included, still walks out under its own key path.
    assert "Loft name" in sender and "ZZZTEST Loft" in sender
    assert "Originating transaction id" in sender and "txn_pigeon_1" in sender


async def test_paging_keeps_every_filter_and_escapes_the_cursor():
    """The links were string-concatenated from the tab plus a raw
    cursor, so page two silently dropped the operator's filters — and an opaque
    cursor carrying an `&` built a broken URL."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [PAYOUT],
                "meta": {
                    "mode": "cursor",
                    "nextCursor": "cur/sor+with&specials",
                    "previousCursor": "back+cur",
                    "total": 1,
                },
            },
        )

    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(
            f"{LIST}?type=withdrawal&status=pending&status=processing"
            f"&customerId={CID}&createdAfter=2026-08-01"
        )
    html = response.text
    assert "cur%2Fsor%2Bwith%26specials" in html  # the cursor, encoded not concatenated
    for kept in ("type=withdrawal", "status=pending", "status=processing", f"customerId={CID}",
                 "createdAfter=2026-08-01"):
        assert kept in html, kept
    assert "direction=backward" in html


async def test_an_unresolved_cancel_hides_the_cancel_button(session):
    """A `payout_cancel` that never got a definitive answer has
    no `conduit_resource_id`, so the payout page could not see it — and offered
    Cancel again while one was still in flight."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", f"/v2/payouts/{PAYOUT['id']}/cancel"): httpx.Response(
                        500, json={"type": "OOPS", "title": "Upstream"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        await post(web, f"{DETAIL}/cancel")
        response = await web.get(DETAIL)

    op = (await session.execute(select(Operation))).scalars().one()
    assert op.state == "outcome_unknown" and op.conduit_resource_id is None
    assert "unresolved <code>payout_cancel</code>" in response.text
    assert "Cancel this payout" not in response.text


# --- the unknown-type fallback ----------------------------------------------------------------


async def test_a_conversion_leg_links_to_the_order_that_owns_it():
    """An earlier round folded `fiat_conversion` into the typed view rather than adding a
    fourth template: what it needs is the summary plus a link to its order, where
    the rate, the fees and the execution live."""
    app = make_app(stub(routes(detail=CONVERSION)))
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{CONVERSION['id']}")
    assert response.status_code == 200
    assert "No typed view for" not in response.text
    assert 'href="/orders/ord_conv_1"' in response.text
    assert "1000.00" in response.text and "912.30" in response.text


async def test_a_conversion_row_in_the_list_shows_both_legs():
    """`amount_of` answers with whichever side states an amount, which on a
    conversion is one arbitrary half of the fact — the row said "912.30 EUR"
    about a movement whose content is that 1000.00 USD *became* it. The list now
    uses the same `converted` the detail page's Converted row does, so the two
    surfaces cannot phrase the same transaction differently."""
    app = make_app(stub(routes(items=[CONVERSION])))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?type=fiat_conversion")
    assert "1000.00 USD → 912.30 EUR" in response.text


async def test_a_single_asset_row_still_shows_one_amount():
    """The other tabs are untouched: a payout has one asset on both sides, so
    `converted` returns nothing and `amount_of` is still what renders."""
    app = make_app(stub(routes(items=[PAYOUT])))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?type=withdrawal")
    assert "→" not in response.text.split("<table>")[-1]


async def test_a_conversion_leg_is_settled_by_the_transaction_level_simulator():
    """The payout simulators are withdrawal-shaped; `simulate/terminal` is the
    one that applies to a conversion's own leg."""
    calls: list = []
    app = make_app(
        stub(
            routes(
                detail={**CONVERSION, "status": "pending"},
                extra={
                    (
                        "POST",
                        f"/v2/sandbox/transactions/{CONVERSION['id']}/simulate/terminal",
                    ): httpx.Response(200, json=CONVERSION)
                },
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/transactions/{CONVERSION['id']}")
        assert "Force completed" in response.text and "Simulate review" not in response.text
        sent = await post(
            web,
            f"/transactions/{CONVERSION['id']}/simulate",
            encoded({"action": "terminal", "outcome": "completed"}),
        )
    assert "Simulated+terminal+completed" in sent.headers["HX-Redirect"]
    body = json.loads(next(c[2] for c in calls if "simulate/terminal" in c[1]))
    assert body == {"outcome": "completed"}


async def test_a_transaction_type_this_build_has_never_heard_of_still_renders():
    alien = {**CONVERSION, "id": "txn_alien", "type": "quantum_settlement", "spookyDistance": "42"}
    app = make_app(stub(routes(detail=alien)))
    async with signed_in(app) as web:
        response = await web.get("/transactions/txn_alien")
    assert response.status_code == 200
    assert "quantum_settlement" in response.text and "42" in response.text


async def test_an_unreadable_transaction_is_a_problem_page():
    app = make_app(
        stub(
            {
                ("GET", "/v2/transactions/txn_missing"): httpx.Response(
                    404, json={"type": "NOT_FOUND", "title": "No such transaction"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get("/transactions/txn_missing")
    assert response.status_code == 200
    assert "Conduit has no record of this" in response.text  # A3: NOT_FOUND, our words


# --- the sandbox payout simulators --------------------------------------------------------------


async def test_the_simulate_panel_is_hidden_off_sandbox():
    app = make_app(stub(routes()))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await web.get(DETAIL)
    assert "Simulate review" not in response.text


async def test_simulate_is_refused_off_sandbox_even_when_posted():
    calls: list = []
    app = make_app(stub(routes(), calls))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await post(
                web, SIMULATE, encoded({"action": "review", "outcome": "approve"})
            )
    assert "sandbox-only" in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if "sandbox" in c[1]] == []


async def test_review_approve_and_reject_send_an_empty_body(session):
    for outcome in ("approve", "reject"):
        path = f"/v2/sandbox/payouts/{PAYOUT['id']}/simulate-review-{outcome}"
        calls: list = []
        app = make_app(stub(routes(extra={("POST", path): httpx.Response(200, json=PAYOUT)}), calls))
        async with signed_in(app) as web:
            response = await post(
                web, SIMULATE, encoded({"action": "review", "outcome": outcome})
            )
        assert f"msg=Simulated+review+{outcome}" in response.headers["HX-Redirect"]
        assert json.loads(next(c for c in calls if c[1] == path)[2]) == {}


async def test_settle_sends_the_outcome(session):
    path = f"/v2/sandbox/payouts/{PAYOUT['id']}/simulate/settled"
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", path): httpx.Response(200, json={**PAYOUT, "status": "completed"})}),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, SIMULATE, encoded({"action": "settle", "outcome": "failed"}))
    assert "msg=Simulated+settle+failed" in response.headers["HX-Redirect"]
    assert json.loads(next(c for c in calls if c[1] == path)[2]) == {"outcome": "failed"}


async def test_a_premature_settle_passes_conduits_guard_message_through(session):
    """Conduit refuses a settle before the review is approved, and its sentence
    is the whole answer — rewriting it into "simulation failed" would throw away
    the only thing that says what to do next."""
    path = f"/v2/sandbox/payouts/{PAYOUT['id']}/simulate/settled"
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", path): httpx.Response(
                        409,
                        json={
                            "type": "PAYOUT_NOT_SETTLEABLE",
                            "title": "Payout has not passed review",
                            "detail": "GIRAFFE-CANARY-2210: inspect the 'field' member",
                            "resolution": "Approve the compliance review before settling it.",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, SIMULATE, encoded({"action": "settle", "outcome": "completed"}))
    banner = unquote_plus(response.headers["HX-Redirect"])
    # A3: the banner names the code, and carries Conduit's RESOLUTION — the one
    # actionable line left for a code this console has no sentence of its own
    # for (A3 gate, m3: the join used to take `detail`, which is always "" now,
    # so the banner was the bare code). Neither the vendor's title nor its
    # `detail` appears.
    assert "Conduit refused this: PAYOUT_NOT_SETTLEABLE" in banner
    assert "Approve the compliance review before settling it." in banner
    assert "Payout has not passed review" not in banner
    assert "GIRAFFE-CANARY-2210" not in banner


async def test_the_simulators_are_audited_and_stay_out_of_the_ledger(session):
    path = f"/v2/sandbox/payouts/{PAYOUT['id']}/simulate-review-approve"
    app = make_app(stub(routes(extra={("POST", path): httpx.Response(200, json=PAYOUT)})))
    async with signed_in(app) as web:
        await post(web, SIMULATE, encoded({"action": "review", "outcome": "approve"}))
    audit = (await session.execute(select(AuditEvent))).scalar_one()
    assert audit.action == "sandbox.simulate_payout"
    assert audit.detail == {
        "transaction": PAYOUT["id"],
        "action": "review",
        "outcome": "approve",
        "ok": True,
    }
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_an_unknown_simulated_action_is_refused_before_the_wire():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        for body in (
            {"action": "review", "outcome": "obliterate"},
            {"action": "detonate", "outcome": "completed"},
            {"action": "settle", "outcome": "approve"},
        ):
            response = await post(web, SIMULATE, encoded(body))
            assert "Unknown simulated action" in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if "sandbox" in c[1]] == []


# --- roles and CSRF --------------------------------------------------------------------------------


async def test_a_viewer_reads_the_ledger_but_gets_no_buttons():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        listing = await web.get(LIST + "?type=withdrawal")
        detail = await web.get(DETAIL)
        refused = await post(web, SIMULATE, encoded({"action": "review", "outcome": "approve"}))
    assert listing.status_code == 200 and detail.status_code == 200
    assert "Cancel this payout" not in detail.text
    assert "Simulate review" not in detail.text
    assert refused.status_code == 403


async def test_a_role_holding_only_payout_cancel_gets_the_cancel_and_nothing_else():
    """A payout detail page offers five separately-gated things (cancel, simulate,
    RFI response, retry, abandon). One permission buys exactly one of them."""
    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.cancel") as web:
        detail = await web.get(DETAIL)

    assert detail.status_code == 200 and "Cancel this payout" in detail.text
    assert "Simulate review" not in detail.text
    # The RFI this payout waits on is still listed — reading it is the read
    # permission — but answering it is not offered.
    assert "rfi_9" in detail.text and 'hx-post="/rfis/rfi_9/respond"' not in detail.text
    assert forbidden_affordances(app, detail.text, {"console.view", "payout.cancel"}) == []


async def test_a_role_that_cannot_cancel_is_told_so_rather_than_shown_a_dead_heading():
    app = make_app(stub(routes()))
    async with signed_in_as(app, "rfi.respond") as web:
        detail = await web.get(DETAIL)

    assert "Cancel this payout" not in detail.text
    # `can_act` is `payout.cancel` alone, so the Actions heading goes with it —
    # no heading over an empty block.
    assert "<h2>Actions</h2>" not in detail.text
    assert forbidden_affordances(app, detail.text, {"console.view", "rfi.respond"}) == []


async def test_the_simulator_needs_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            SIMULATE,
            content=encoded({"action": "review", "outcome": "approve"}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 403 and "CSRF" in response.text
