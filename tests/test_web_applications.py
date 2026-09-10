"""Applications dashboard, RFIs, IDV, the operation panel and the sandbox panel.

These cover the branches the live e2e cannot reach on staging: approval,
rejection-with-scenario, IDV links, the stalled/unknown operation states, and
the sandbox decision simulator (staging has no `/v2/sandbox/*` routes at all).
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import unquote_plus
import httpx
import pytest
from sqlalchemy import select, update

from app import audit, documents, operations
from app.db import sessionmaker
from app.main import ALREADY_MOVED
from app.web import applications
from app.models import AuditEvent, Draft, Operation
from app.onboarding import drafts
from tests.conftest import settings_override
from tests.test_web_onboarding import read_the_review
from tests.web_harness import (
    PDF,
    PNG,
    forbidden_affordances,
    form,
    make_app,
    post,
    signed_in,
    signed_in_as,
    stub,
    upload,
)

ACTOR = {"actor_id": "ops@example.com", "actor_email": "ops@example.com"}

APP = {
    "id": "app_1",
    "type": "customer_onboarding",
    "status": "processing",
    "createdAt": "2026-08-28T05:00:00.000Z",
    "submittedAt": "2026-08-28T05:00:01.000Z",
    "updatedAt": "2026-08-28T05:00:02.000Z",
    "clientReferenceId": "zzztest-1",
    "persons": [{"name": "Ada Lovelace", "referenceId": "app_1:o1M"}],
}
APPROVED = {**APP, "status": "approved", "customerId": "cus_42"}
REJECTED = {
    **APP,
    "status": "rejected",
    "failureCode": "rejected_by_ops",
    "failureMessage": "Beneficial ownership could not be verified.",
    "resubmittable": True,
}
FINAL = {**REJECTED, "resubmittable": False}

RFI = {
    "id": "rfi_1",
    "title": "Proof of address",
    "status": "open",
    "summary": "Send a utility bill.",
    "subjects": [{"subjectType": "application", "subjectId": "app_1"}],
    "createdAt": "2026-08-28T05:00:00.000Z",
    "updatedAt": "2026-08-28T05:00:00.000Z",
    "publishedAt": "2026-08-28T05:00:00.000Z",
}

SNAPSHOT = {
    "schemaVersion": "3",
    "context": "onboarding",
    "country": "BGR",
    "fields": [
        {
            "pointer": "/businessInfo/legalName",
            "label": "Legal name",
            "type": "string",
            "required": True,
            "group": "businessInfo",
        }
    ],
}


def page(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "meta": {"total": len(items)}})


CUSTOMER = {"id": "cus_1", "legalName": "ZZZTEST Ltd", "type": "business"}


def routes(
    application: dict = APP,
    rfis: list[dict] | None = None,
    customers=(CUSTOMER,),
    extra: dict | None = None,
) -> dict:
    return {
        ("GET", "/v2/applications"): page([application]),
        ("GET", f"/v2/applications/{application['id']}"): httpx.Response(200, json=application),
        ("GET", "/v2/rfis"): page(rfis if rfis is not None else []),
        # The list's second, convenience read: names for the customer filter.
        ("GET", "/v2/customers"): (
            customers if isinstance(customers, httpx.Response) else page(list(customers or []))
        ),
        **(extra or {}),
    }


# --- list -----------------------------------------------------------------------------


async def test_list_renders_filters_and_a_row():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get("/applications?status=processing&search=zzztest")

    assert response.status_code == 200
    assert "app_1" in response.text and "customer_onboarding" in response.text
    # One page, filters passed straight through — never an eager cursor walk.
    assert len([c for c in calls if c[1] == "/v2/applications"]) == 1
    assert "Processing" in response.text


async def test_search_describes_what_it_actually_matches():
    """The placeholder read "name, id…", which is the spec's
    "Free-text search across application fields" taken at its word. Probed
    against the live sandbox (2026-08-29) the parameter matched only a whole,
    case-exact `clientReferenceId`: a person's name, a customer's legal name, an
    `app_…` and a `cus_…` all returned zero rows. A search box that promises
    names and silently answers none is the console lying about what it knows.

    Scoped 2026-08-30: the probe was **sandbox-only**
    and the pinned contract still promises free text, so the copy states where
    the observation came from instead of asserting it of every environment. The
    advice it ends on holds either way.

    Renamed 2026-08-31 (QA F-005): the *label* still read
    "Search" while only the placeholder and this paragraph said what it matched,
    and the report found exactly that — a field titled Search that answers an
    application id with nothing. The label now names the one thing it matches;
    `name="search"` on the wire is unchanged."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/applications")).text

    assert 'placeholder="name, id…"' not in html
    assert "<label>Search " not in html
    assert '<label>Client reference <input type="text" name="search"' in html
    assert 'placeholder="exact, whole string"' in html
    # The claim is attributed to the environment it was observed in, and the
    # documented contract is named rather than contradicted.
    assert "On the sandbox, this box matched only a client reference" in html
    assert "Conduit documents it as free text, so it may do more elsewhere" in html
    # …and the sentence still points at the box that *can* answer the name question.
    assert "the Customer box is the sure way" in html


async def test_the_customer_filter_takes_a_name_and_sends_an_id():
    """`search` cannot find a person and
    `GET /v2/customers` has no name parameter either, so `customerId` is the
    only way to reach an application by who it is for — and the datalist is what
    turns the name the operator knows into the id the endpoint wants. Same
    macro, same context, same helper as the transactions filter."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/applications")).text

    assert '<datalist id="known-customers">' in html
    assert '<option value="cus_1">ZZZTEST Ltd</option>' in html
    assert 'name="customerId"' in html and 'list="known-customers"' in html
    assert 'placeholder="name or cus_…"' in html


async def test_the_customer_filter_round_trips_into_the_query_and_the_box():
    """It reaches Conduit as `customerId`, and it is still in the input when the
    filtered page comes back — a filter that forgets itself reads as a filter
    that was ignored."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/applications":
            seen.append(request.url.query.decode())
        return page([APP])

    app = make_app(handler)
    async with signed_in(app) as web:
        html = (await web.get("/applications?customerId=cus_1&status=processing")).text

    assert "customerId=cus_1" in seen[0] and "status=processing" in seen[0]
    assert 'name="customerId" value="cus_1"' in html


async def test_paging_keeps_every_filter_and_escapes_the_cursor():
    """Same fix transactions got, arriving late here: the links were
    string-concatenated from a raw cursor and carried nothing else, so page two
    silently dropped the operator's filters and showed them a different query's
    results. An opaque cursor with an `&` in it also built a broken URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v2/applications":
            return page([])
        return httpx.Response(
            200,
            json={
                "data": [APP],
                "meta": {
                    "mode": "cursor",
                    "nextCursor": "cur/sor+with&specials",
                    "previousCursor": "back+cur",
                },
            },
        )

    app = make_app(handler)
    async with signed_in(app) as web:
        html = (
            await web.get(
                "/applications?search=zzztest-1&status=processing&status=approved"
                "&type=customer_onboarding&customerId=cus_1"
            )
        ).text

    assert "cur%2Fsor%2Bwith%26specials" in html  # encoded, not concatenated
    for kept in (
        "search=zzztest-1",
        "status=processing",
        "status=approved",
        "type=customer_onboarding",
        "customerId=cus_1",
    ):
        assert kept in html, kept
    assert "direction=backward" in html


async def test_paging_backward_actually_asks_conduit_to_go_backward():
    """`direction` was never read off the query here, so Previous re-read the
    same page forwards from the cursor. The parameter is in the pin; pass it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/applications":
            seen.append(request.url.query.decode())
        return page([APP])

    app = make_app(handler)
    async with signed_in(app) as web:
        await web.get("/applications?cursor=back%2Bcur&direction=backward")

    assert "direction=backward" in seen[0] and "cursor=back%2Bcur" in seen[0]


async def test_the_page_head_leaves_onboarding_to_the_chrome():
    """Design pass 2026-09-02, reversing the earlier call for this surface. It put
    a "Start an onboarding" primary here, and its own justification was that it
    was "the ONLY thing an operator starts from here" — an argument from the
    absence of a better candidate, not from this page's job. What an operator
    does about a row on this list is open it, answer its RFI, or fix and
    resubmit; starting another onboarding is the Onboard page's verb, and the
    chrome's quick-actions cluster now offers it on every page.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as operator:
        operator_html = (await operator.get("/applications")).text
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as viewer:
        viewer_html = (await viewer.get("/applications")).text

    assert '<a class="btn primary" href="/onboarding">' not in operator_html
    assert '<div class="action">' not in operator_html  # the head has no slot at all now
    # One door, in the chrome, and it is the same words the Onboard page uses.
    assert '<a class="btn" href="/onboarding">Onboard</a>' in operator_html
    assert operator_html.count('href="/onboarding"') == 1
    assert 'href="/onboarding"' not in viewer_html


async def test_a_viewer_gets_the_customer_suggestions_too():
    """A viewer reads this list and filters it; the datalist is a read-only
    convenience over data they can already open at /customers."""
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        html = (await web.get("/applications")).text
    assert '<option value="cus_1">ZZZTEST Ltd</option>' in html


async def test_the_list_renders_when_the_customer_read_fails():
    """Two independent reads, gathered. Losing the names costs the suggestions
    and nothing else: the applications list renders, the filter still filters,
    and no banner claims an outage the main read did not hit."""
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
        response = await web.get("/applications")

    assert response.status_code == 200
    # "of 1" because this endpoint reports a `meta.total` and the table renders one
    # wherever it is real (never fabricated for the endpoints that send none).
    assert "app_1" in response.text and "Showing 1 of 1 application" in response.text
    assert (
        "<option" not in response.text.split('id="known-customers"')[1].split("</datalist>")[0]
    )
    assert "Customers down" not in response.text
    assert "Couldn't read this list" not in response.text
    assert 'list="known-customers"' in response.text  # the input is as it was


async def test_the_suggestions_say_when_there_are_more_than_they_show():
    more = httpx.Response(
        200, json={"data": [CUSTOMER], "meta": {"mode": "cursor", "nextCursor": "c2"}}
    )
    app = make_app(stub(routes(customers=more)))
    async with signed_in(app) as web:
        assert "Customer suggestions are the first 25" in (await web.get("/applications")).text

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        assert "Customer suggestions are the first 25" not in (await web.get("/applications")).text


async def test_a_failed_list_read_is_a_problem_not_an_empty_page():
    problem = {
        "type": "RATE_LIMITED",
        "title": "Too many requests",
        "detail": "Slow down.",
        "resolution": "Retry shortly.",
        "correlationId": "corr-1",
    }
    app = make_app(stub({("GET", "/v2/applications"): httpx.Response(429, json=problem)}))
    async with signed_in(app) as web:
        response = await web.get("/applications")
    assert "Conduit is asking this console to slow down" in response.text  # A3
    assert "corr-1" in response.text
    # Same phase-gate fix as the orders one: this test's *name* was right and its
    # last assertion was the contradiction — a page cannot say "Too many
    # requests" and "there are none" at once. The handler substitutes an empty
    # `Page` on failure, so the empty row has to branch on the problem.
    assert "The list could not be read — see above." in response.text
    assert "No applications on this page." not in response.text
    assert "Couldn't read this list" in response.text


async def test_unknown_status_renders_neutrally_and_never_terminal():
    """plan v2 §7: never guessed at, never a state-dependent action."""
    app = make_app(stub(routes({**APP, "status": "quarantined"})))
    async with signed_in(app) as web:
        listing = await web.get("/applications")
        detail = await web.get("/applications/app_1")

    assert "Unknown: quarantined" in listing.text
    assert 'class="pill unknown"' in listing.text
    # Not terminal, so the page keeps refreshing rather than declaring an end.
    assert "refreshing every 15s" in detail.text
    assert "Fix &amp; resubmit" not in detail.text


# --- detail + auto refresh ------------------------------------------------------------


async def test_detail_shows_timestamps_and_polls_while_non_terminal():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
        fragment = await web.get("/applications/app_1/status")

    # An earlier round put every timestamp through the `ts` filter: the compact UTC form
    # is what is on screen, the full ISO Conduit sent is on the element's title.
    # Still the same guard — that `submittedAt` reaches the page, exactly.
    assert 'title="2026-08-28T05:00:01+00:00"' in detail.text
    # Day and time, not the whole compact string: `ts` prints the year once the
    # fixture's 2026 is no longer the current one, and this test is not about
    # that rule.
    assert "Aug 28" in detail.text and "05:00 UTC" in detail.text
    assert 'hx-get="/applications/app_1/status"' in fragment.text
    assert 'hx-trigger="every 15s"' in fragment.text


async def test_polling_stops_on_a_terminal_application():
    app = make_app(stub(routes(APPROVED)))
    async with signed_in(app) as web:
        fragment = await web.get("/applications/app_1/status")

    assert "hx-trigger" not in fragment.text
    assert "settled — live refresh stopped" in fragment.text
    assert "cus_42" in fragment.text  # the customer link on approval


@pytest.mark.parametrize(
    "application, pill, tone",
    [
        (REJECTED, "Rejected · resubmittable", "warn"),
        (FINAL, "Rejected · final", "bad"),
        # No boolean at all: today's plain rejection. Which of the two above it
        # is has not been established, and the console does not pick one.
        ({k: v for k, v in REJECTED.items() if k != "resubmittable"}, "Rejected", "bad"),
    ],
)
async def test_a_rejection_pill_says_whether_it_can_be_corrected(application, pill, tone):
    """Human directive (2026-08-31), verified against the pin first:
    `resubmittable` is a boolean the four Application DTOs all carry and all
    describe as "Omitted on non-rejected applications" — so its absence on a
    *rejected* one is a fact this build cannot fill in.

    `true` is the warn family because it is actionable — correct it and send a
    fresh application, which is what the button under it does. `false` is the
    exception family with the other terminal noes. Absent is neither claim.

    Both the list and the detail render it, so both are asserted: a status that
    means different things on two screens is worse than one that means less.
    """
    app = make_app(stub(routes(application)))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
        listing = await web.get("/applications")

    for html in (detail.text, listing.text):
        assert f'<span class="pill {tone}">{pill}</span>' in html
    # The list row's exception flag follows the pill: an amber pill beside a red
    # bar would say two things about one row.
    assert ('class="exception"' in listing.text) is (tone == "bad")


async def test_no_other_status_grew_a_suffix():
    """The wrapper reads `PILL_TONES` and only ever overrides the one word that
    means two things — a `processing` application still renders exactly as it
    did, and so does every other kind (which never reaches the wrapper at all).
    """
    app = make_app(stub(routes()))  # APP is `processing`
    async with signed_in(app) as web:
        html = (await web.get("/applications")).text
    assert '<span class="pill wait">Processing</span>' in html


async def test_a_rejection_shows_the_code_in_english_and_the_reviewers_own_words():
    """The `failureCode` enum is two values in the pin (`rejected_by_ops`,
    `compliance_denied`); a third would be a contract change, so it is rendered
    raw rather than guessed at. `failureMessage` is the reviewer's own prose —
    quoted, never paraphrased, and never summarised into a category."""
    unknown = {**REJECTED, "failureCode": "sanctions_screening_hit"}
    app = make_app(stub(routes(REJECTED)))
    other = make_app(stub(routes(unknown)))
    async with signed_in(app) as web:
        known_html = (await web.get("/applications/app_1")).text
    async with signed_in(other) as web:
        unknown_html = (await web.get("/applications/app_1")).text

    assert "Rejected — Rejected by operations review" in known_html
    assert '<code class="raw">rejected_by_ops</code>' in known_html
    assert (
        '<blockquote class="reason">Beneficial ownership could not be verified.</blockquote>'
        in known_html
    )
    # An unknown code is shown, not translated and not dropped.
    assert "Rejected — sanctions_screening_hit" in unknown_html
    assert '<code class="raw">sanctions_screening_hit</code>' in unknown_html


async def test_a_final_rejection_offers_no_resubmit():
    app = make_app(stub(routes(FINAL)))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
    assert "This decision is final — do not resubmit." in detail.text
    assert "/resubmit" not in detail.text


# --- quick-view drawer ---------------------------------------------


async def test_the_list_carries_the_affordance_and_the_drawer_shell():
    """The mechanism, on the page that hosts it: one shell, one button per row,
    and a target that is the shell's inner div (an `innerHTML` swap of the shell
    itself would take the loading indicator with it)."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/applications")).text

    # Non-modal semantics, stated in the markup: a labelled complementary
    # region, never a dialog — nothing on this page is inert while it is open.
    assert '<aside id="drawer" role="complementary" aria-label="Application quick view"' in html
    assert '<div id="drawer-body"></div>' in html
    # The one dismiss and the in-flight word both live in the shell, so they
    # survive the `innerHTML` swap that replaces the body.
    assert html.count("data-drawer-close") == 1
    assert '<span class="htmx-indicator">Loading…</span>' in html
    assert 'hx-get="/applications/app_1/quick"' in html
    assert 'hx-target="#drawer-body" hx-swap="innerHTML" hx-indicator="#drawer"' in html
    # The row's own link is untouched: quick view is an addition, not a
    # replacement for the way to the record.
    assert '<a href="/applications/app_1"><code>app_1</code></a>' in html
    # Named per row, or a screen reader hears "Quick view" twenty-five times.
    assert 'aria-label="Quick view — app_1"' in html
    # In the row's FIRST cell, beside the id. A right-fixed drawer covers the
    # far end of a full-width table, so an action column there would be under
    # the panel the moment one was open — the browser suite caught exactly that.
    first_cell = html.split('<a href="/applications/app_1">')[1].split("</td>")[0]
    assert "data-quick" in first_cell


async def test_the_quick_view_renders_the_facts_and_both_ways_on():
    calls: list = []
    app = make_app(stub(routes(APPROVED), calls))
    async with signed_in(app) as web:
        response = await web.get("/applications/app_1/quick")

    assert response.status_code == 200
    html = response.text
    assert "Approved" in html
    assert "Customer onboarding" in html  # the label layer, same as the list's
    assert "<code>zzztest-1</code>" in html  # clientReferenceId
    assert 'title="2026-08-28T05:00:00+00:00"' in html  # createdAt, through `ts`
    # The canvas's pair, plus the dismiss.
    assert '<a class="btn primary" href="/applications/app_1">Open application</a>' in html
    assert '<a class="btn" href="/customers/cus_42">Open customer</a>' in html
    # No dismiss in the body at all: Close belongs to the shell, so there is one
    # of it in every state the body can be in (loaded, failed, still loading).
    assert "data-drawer-close" not in html

    # The budget: two reads, and the second is the console's one bounded name
    # resolver — never a read per row, never a second application read.
    assert sorted(c[1] for c in calls) == ["/v2/applications/app_1", "/v2/customers"]


async def test_the_quick_view_names_the_customer_from_the_one_bounded_read():
    """The name comes from `with_customer_names` — the same 25-row map the list
    itself resolves names from. A miss renders the bare id, never a guess."""
    named = {**APP, "status": "approved", "customerId": CUSTOMER["id"]}
    app = make_app(stub(routes(named)))
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1/quick")).text
    assert "ZZZTEST Ltd" in html and "<code>cus_1</code>" in html

    # Beyond the bounded page: the id alone, and the link still works.
    app = make_app(stub(routes(APPROVED)))
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1/quick")).text
    assert "<code>cus_42</code>" in html
    assert "ZZZTEST" not in html


async def test_a_viewer_reads_the_quick_view_exactly_as_an_operator_does():
    """Quick view is a read, and the detail page it previews is `viewer` too —
    so the two gates match. There is no action in here to gate on."""
    app = make_app(stub(routes(APPROVED)))
    async with signed_in(app) as operator:
        operator_html = (await operator.get("/applications/app_1/quick")).text
    app = make_app(stub(routes(APPROVED)))
    async with signed_in(app, groups="readers") as viewer:
        viewer_response = await viewer.get("/applications/app_1/quick")

    assert viewer_response.status_code == 200
    assert viewer_response.text == operator_html


async def test_the_quick_view_splits_a_rejection_but_offers_no_correction():
    """The same two voices the detail page uses (one macro, `m.rejection`) —
    because "resubmittable" and "final" are different instructions. The
    correcting action stays on the full page: a preview must not start a draft.
    """
    app = make_app(stub(routes(REJECTED)))
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1/quick")).text
    assert "Rejected · resubmittable" in html
    assert "Rejected — Rejected by operations review" in html
    assert "Beneficial ownership could not be verified." in html
    assert "A corrected application will be considered." in html
    assert "/resubmit" not in html

    app = make_app(stub(routes(FINAL)))
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1/quick")).text
    assert "Rejected · final" in html
    assert "This decision is final — do not resubmit." in html


async def test_a_failed_quick_view_read_renders_the_problem_in_the_drawer():
    """Never a dead click. The route answers 200 with the house error card where
    the facts would have been, so htmx swaps it in like any other body."""
    app = make_app(
        stub(
            {
                **routes(),
                ("GET", "/v2/applications/app_1"): httpx.Response(
                    503,
                    json={
                        "type": "UNAVAILABLE",
                        "title": "Conduit is unavailable",
                        "detail": "Try again shortly.",
                        "correlationId": "cor_quick_1",
                    },
                ),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get("/applications/app_1/quick")

    assert response.status_code == 200
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    assert "cor_quick_1" in response.text
    # Still a way out and a way on; nothing pretends to be a fact.
    assert '<a class="btn primary" href="/applications/app_1">Open application</a>' in response.text
    assert "Open customer" not in response.text


# --- rejection correction -------------------------------------------------------------


async def submitted_draft(session) -> tuple[Draft, Operation]:
    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=SNAPSHOT,
        payload={"root": {"businessInfo": {"legalName": "ZZZTEST Acme"}}, "persons": []},
        country="BGR",
        client_reference_id="zzztest-1",
    )
    op, _ = await operations.start(
        session,
        type="onboarding_submit",
        **ACTOR,
        path="/v2/onboarding",
        body={"businessInfo": {"legalName": "ZZZTEST Acme"}},
        draft_id=draft.id,
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)
    return draft, op


async def test_resubmit_reopens_the_draft_with_its_answers(session):
    draft, _ = await submitted_draft(session)
    app = make_app(stub(routes(REJECTED)))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
        assert "Fix &amp; resubmit" in detail.text
        assert "Beneficial ownership could not be verified." in detail.text
        reopened = await post(web, "/applications/app_1/resubmit", b"")
        assert reopened.headers["HX-Redirect"].startswith(f"/onboarding/{draft.id}")
        form_page = await web.get(f"/onboarding/{draft.id}")

    # The answers are still there — this is what gets corrected (drafts §purge).
    assert 'value="ZZZTEST Acme"' in form_page.text
    fresh = await drafts.load(session, draft.id)
    assert fresh.client_reference_id == "zzztest-1"


async def test_a_corrected_resubmit_is_a_new_operation_on_the_same_draft(session):
    draft, first = await submitted_draft(session)
    app = make_app(
        stub(
            routes(
                REJECTED,
                extra={
                    ("POST", "/v2/onboarding"): httpx.Response(
                        202, json={**APP, "id": "app_2", "status": "pending"}
                    )
                },
            )
        )
    )
    async with signed_in(app) as web:
        await post(web, "/applications/app_1/resubmit", b"")
        await post(
            web,
            f"/onboarding/{draft.id}/review",
            form(**{"f.businessInfo.legalName": "ZZZTEST Acme Corrected"}),
        )
        done = await post(
            web,
            f"/onboarding/{draft.id}/submit",
            # Submit carries the seal the review render minted, so this
            # has to go through the review page the way a reviewer does.
            await read_the_review(web, f"/onboarding/{draft.id}"),
        )

    assert done.headers["HX-Redirect"] == "/applications/app_2"
    ops = (
        await session.execute(
            select(Operation).where(Operation.type == "onboarding_submit").order_by(Operation.created_at)
        )
    ).scalars().all()
    assert len(ops) == 2
    assert ops[0].id != ops[1].id
    assert ops[0].idempotency_key != ops[1].idempotency_key  # never a reused key
    assert {op.draft_id for op in ops} == {draft.id}
    assert ops[0].state == "confirmed" and ops[0].conduit_resource_id == "app_1"


async def test_resubmit_without_a_local_draft_says_so(session):
    app = make_app(stub(routes(REJECTED)))
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/resubmit", b"")
    assert "No+local+draft" in response.headers["HX-Redirect"]


async def test_resubmit_after_the_answers_were_purged_opens_a_pinned_replacement(session):
    draft, _ = await submitted_draft(session)
    await session.execute(update(Draft).where(Draft.id == draft.id).values(payload=None))
    await session.commit()

    app = make_app(stub(routes(REJECTED)))
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/resubmit", b"")
        target = response.headers["HX-Redirect"]
        page = await web.get(target.split("?")[0])

    assert "purged+by+retention" in target
    assert page.status_code == 200
    replacement = await drafts.load(session, uuid.UUID(target.split("?")[0].rsplit("/", 1)[-1]))
    # Same pinned questionnaire, same operator reference — a new blank draft.
    assert replacement.requirements_snapshot == SNAPSHOT
    assert replacement.client_reference_id == "zzztest-1"


# --- RFIs -----------------------------------------------------------------------------


async def test_a_get_never_acknowledges_but_marks_the_rfi(session):
    """A GET is read-only. Acknowledging from the render meant a prefetch, a
    refresh or a link-follower sent a Conduit mutation with no CSRF token."""
    calls: list = []
    app = make_app(stub(routes(rfis=[RFI]), calls))
    async with signed_in(app) as web:
        page = await web.get("/applications/app_1")

    assert page.status_code == 200
    assert [c for c in calls if "acknowledge" in c[1]] == []
    assert (await session.execute(select(AuditEvent))).scalars().all() == []
    # …but the panel carries the explicit POST that does it.
    assert 'hx-post="/rfis/rfi_1/acknowledge"' in page.text
    assert 'hx-trigger="load"' in page.text


async def test_the_acknowledge_post_acknowledges_exactly_once(session):
    calls: list = []
    app = make_app(
        stub(
            routes(rfis=[RFI], extra={("POST", "/v2/rfis/rfi_1/acknowledge"): httpx.Response(204)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        first = await post(web, "/rfis/rfi_1/acknowledge", b"")
        second = await post(web, "/rfis/rfi_1/acknowledge", b"")
        page = await web.get("/applications/app_1")

    assert "acknowledged" in first.text
    assert len([c for c in calls if c[1] == "/v2/rfis/rfi_1/acknowledge"]) == 1
    assert second.status_code == 200  # idempotent, and it does not send again
    trail = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "rfi.acknowledged"))
    ).scalars().all()
    assert len(trail) == 1 and trail[0].detail["rfi"] == "rfi_1"
    # Once acknowledged, the panel stops asking.
    assert "hx-post=\"/rfis/rfi_1/acknowledge\"" not in page.text


async def test_a_failed_acknowledge_is_retried_next_time(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                rfis=[RFI],
                extra={("POST", "/v2/rfis/rfi_1/acknowledge"): httpx.Response(503, json={})},
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        await post(web, "/rfis/rfi_1/acknowledge", b"")
        await post(web, "/rfis/rfi_1/acknowledge", b"")
        page = await web.get("/applications/app_1")

    assert len([c for c in calls if c[1] == "/v2/rfis/rfi_1/acknowledge"]) == 2
    assert (await session.execute(select(AuditEvent).where(AuditEvent.action == "rfi.acknowledged"))).scalars().all() == []
    assert 'hx-post="/rfis/rfi_1/acknowledge"' in page.text  # still asking


async def test_acknowledge_needs_rfi_respond_and_the_csrf_header(session):
    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in(app, groups="readers") as viewer:
        refused = await post(viewer, "/rfis/rfi_1/acknowledge", b"")
    assert refused.status_code == 403
    async with signed_in(app) as web:
        naked = await web.post("/rfis/rfi_1/acknowledge", content=b"")
    assert naked.status_code == 403 and "CSRF" in naked.text


async def test_a_role_holding_only_rfi_respond_answers_but_cannot_touch_the_application():
    """An application page gates six things separately (idv link, resubmit,
    simulate, RFI answer, retry, abandon). Holding the RFI one buys the RFI one."""
    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in_as(app, "rfi.respond") as web:
        detail = await web.get("/applications/app_1")

    assert detail.status_code == 200
    assert 'hx-post="/rfis/rfi_1/respond"' in detail.text
    assert "Get verification link" not in detail.text
    assert "Simulate" not in detail.text.split("<main", 1)[1]
    # The people table still lists everyone — it loses a button, not its rows.
    assert "app_1:o1M" in detail.text
    assert forbidden_affordances(app, detail.text, {"console.view", "rfi.respond"}) == []


async def test_a_role_holding_only_the_idv_link_gets_no_rfi_controls():
    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in_as(app, "onboarding.idv_link") as web:
        detail = await web.get("/applications/app_1")

    assert "Get verification link" in detail.text
    assert 'hx-post="/rfis/rfi_1/respond"' not in detail.text
    assert 'hx-post="/rfis/rfi_1/acknowledge"' not in detail.text
    # The RFI is still readable — it is Conduit asking, and reading is the read.
    assert "rfi_1" in detail.text
    assert forbidden_affordances(app, detail.text, {"console.view", "onboarding.idv_link"}) == []


async def test_a_viewer_is_never_offered_the_acknowledge(session):
    calls: list = []
    app = make_app(stub(routes(rfis=[RFI]), calls))
    async with signed_in(app, groups="readers") as web:
        response = await web.get("/applications/app_1")
    assert response.status_code == 200
    assert [c for c in calls if "acknowledge" in c[1]] == []
    assert "acknowledge" not in response.text


async def test_responding_to_an_rfi_goes_through_the_ledger(session):
    calls: list = []
    minted = iter(("doc_1", "doc_2"))
    app = make_app(
        stub(
            routes(
                rfis=[RFI],
                extra={
                    ("POST", "/v2/rfis/rfi_1/acknowledge"): httpx.Response(204),
                    ("POST", "/v2/documents"): lambda r: httpx.Response(
                        201, json={"id": next(minted)}
                    ),
                    ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                        201, json={"id": "rfi_1", "status": "responded"}
                    ),
                },
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
        assert 'value="ops@example.com"' in detail.text  # prefilled from the Actor
        # Two real uploads through the real route — an invented `doc_` id is
        # refused now, and rightly (see the attachment tests below).
        first = await upload(web, purpose="rfi_response", content=PNG)
        second = await upload(web, purpose="rfi_response", filename="b.pdf", content=PDF)
        assert "doc_1" in first.text and "doc_2" in second.text
        response = await post(
            web,
            "/rfis/rfi_1/respond",
            form(
                subjectType="application",
                subject="app_1",
                message="Utility bill attached.",
                email="ops@example.com",
                documentIds=["doc_1", "doc_2"],
            ),
        )

    # `msg` rides with a `msgsig` companion, so the exact string is
    # no longer stable — the plaintext prefix still is.
    assert response.headers["HX-Redirect"].startswith("/applications/app_1?msg=Response+sent.&msgsig=")
    op = (await session.execute(select(Operation).where(Operation.type == "rfi_respond"))).scalar_one()
    assert op.state == "confirmed"
    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/rfis/rfi_1/responses"))
    assert sent["message"] == "Utility bill attached."
    assert sent["submittedBy"]["email"] == "ops@example.com"
    assert sent["documentIds"] == ["doc_1", "doc_2"]


async def test_an_empty_rfi_response_is_refused_before_the_ledger(session):
    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in(app) as web:
        response = await post(web, "/rfis/rfi_1/respond", form(subjectType="application", subject="app_1", message="  "))
    assert "needs+a+message" in response.headers["HX-Redirect"]
    assert (await session.execute(select(Operation))).scalars().all() == []


# --- IDV ------------------------------------------------------------------------------

IDV_PATH = "/v2/applications/app_1/persons/app_1:o1M/idv-link"


async def test_idv_link_is_shown_with_a_copy_control():
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", IDV_PATH): httpx.Response(
                        200,
                        json={
                            "url": "https://verify.example/inquiry/abc",
                            "shortUrl": "https://s/x",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/persons/app_1:o1M/idv-link", b"")

    assert "https://verify.example/inquiry/abc" in response.text
    assert "data-copy=" in response.text
    # The deprecated one-time link is never rendered.
    assert "https://s/x" not in response.text


@pytest.mark.parametrize(
    "status,expected",
    [
        (404, "Not ready yet"),
        (409, "already settled"),
    ],
)
async def test_idv_link_branches(status, expected):
    app = make_app(
        stub(routes(extra={("POST", IDV_PATH): httpx.Response(status, json={"type": "XYZ_REFUSED", "title": "x"})}))
    )
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/persons/app_1:o1M/idv-link", b"")
    assert expected in response.text
    assert "http" not in response.text  # no link to show, so none is invented


async def test_idv_link_needs_onboarding_idv_link():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await post(web, "/applications/app_1/persons/app_1:o1M/idv-link", b"")
    assert response.status_code == 403


# --- the sandbox panel ----------------------------------------------------------------

SIMULATE = "/v2/sandbox/applications/app_1/simulate/decision"


async def test_sandbox_panel_is_hidden_and_refused_on_staging():
    """The live host is staging, which has no `/v2/sandbox/*` routes at all."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            detail = await web.get("/applications/app_1")
            blocked = await post(web, "/applications/app_1/simulate", form(outcome="approved"))

    assert "Simulate approval" not in detail.text
    # The badge says the WORD and nothing else; the host moved into its `title`
    # when A2 dropped it from the text (Arca §4.1).
    assert ">Staging</span>" in detail.text
    assert "api.staging.conduit.financial" in detail.text.split('id="env-badge"')[1][:300]
    assert 'class="badge warn"' in detail.text  # amber: not a throwaway environment
    assert "sandbox+host+only" in blocked.headers["HX-Redirect"]
    assert calls == [c for c in calls if "sandbox" not in c[1]]  # nothing was sent


async def test_sandbox_panel_works_on_sandbox(session):
    calls: list = []
    app = make_app(stub(routes(extra={("POST", SIMULATE): httpx.Response(200, json=APPROVED)}), calls))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
        approve = await post(web, "/applications/app_1/simulate", form(outcome="approved"))
        reject = await post(
            web,
            "/applications/app_1/simulate",
            # Field names are lowercase in the contract; the operator's casing is
            # normalised rather than bounced.
            form(outcome="rejected", category="document_mismatch", field="Tax_ID", reason="  "),
        )
        plain = await post(
            web,
            "/applications/app_1/simulate",
            form(outcome="rejected", category="generic", reason="ZZZTEST free text"),
        )

    assert "Simulate approval" in detail.text
    assert 'class="badge ok"' in detail.text  # muted: sandbox is not a state
    assert approve.headers["HX-Redirect"].startswith(
        "/applications/app_1?msg=Simulated+approved.&msgsig="
    )
    assert reject.headers["HX-Redirect"].startswith(
        "/applications/app_1?msg=Simulated+rejected.&msgsig="
    )
    assert plain.headers["HX-Redirect"].startswith(
        "/applications/app_1?msg=Simulated+rejected.&msgsig="
    )
    bodies = [json.loads(c[2]) for c in calls if c[1].endswith("/simulate/decision")]
    # `reason` never goes on the wire — the live sandbox refuses the key
    # (400 `Unrecognized key`, verified 2026-08-28); it is audit-only.
    assert bodies == [
        {"outcome": "approved"},
        {"outcome": "rejected", "category": "document_mismatch", "field": "tax_id"},
        {"outcome": "rejected", "category": "generic"},
    ]
    trail = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "sandbox.simulate_decision")
        )
    ).scalars().all()
    assert len(trail) == 3 and trail[0].actor_email == "ops@example.com"
    assert trail[-1].detail["reason"] == "ZZZTEST free text"  # kept for the trail


async def test_the_simulate_panel_explains_itself_and_confirms_the_destructive_half():
    """QA F-003. Three asks, all on a *non-terminal* application where the
    controls are live:

      * what a simulation actually does, before the buttons rather than in the
        word "simulate" — and only what this console can prove: the decision is
        Conduit's real one, and the status card stops polling because that
        fragment carries `every 15s` only while non-terminal;
      * the rejection wears the danger affordance and the consequence sentence
        the rest of the console uses for destructive-shaped actions — not a JS
        `confirm()`, which this console does not use anywhere;
      * the category values read as English while still submitting their keys.
    """
    app = make_app(stub(routes()))  # APP is `processing`
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1")).text

    assert "A simulated decision is Conduit's real decision on this application" in html
    assert 'class="danger"' in html and "Rejecting settles this application against the customer" in html
    assert "confirm(" not in html
    assert '<option value="document_mismatch">Document mismatch — document_mismatch</option>' in html
    # Live: nothing is disabled while the application is still being decided.
    assert "disabled" not in html.split("<h2>Sandbox</h2>", 1)[1]


@pytest.mark.parametrize("application", [APPROVED, REJECTED])
async def test_a_settled_application_cannot_be_decided_again_and_says_why(application):
    """QA F-003's core: an approved application went on offering "Simulate
    approval". The controls stay on screen and go inert — this console states
    why rather than hiding the panel (DESIGN.md) — and the reason is verified,
    not assumed: `app/web/sandbox_actions.py` records the live 409
    `APPLICATION_ALREADY_DECIDED` behind this sentence."""
    app = make_app(stub(routes(application)))
    async with signed_in(app) as web:
        html = (await web.get("/applications/app_1")).text

    panel = html.split("<h2>Sandbox</h2>", 1)[1]
    assert f"Already decided — {application['status']}" in panel
    assert "409 APPLICATION_ALREADY_DECIDED" in panel
    # Both buttons carry the reason as a title; the three inputs that would feed
    # a rejection are inert too, so nothing can be typed into a dead form.
    assert panel.count('disabled title="This application is already') == 2
    assert panel.count("disabled>") == 3
    assert "Conduit will not decide it twice." in panel


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"outcome": "explode"}, "Unknown simulated outcome"),
        ({"outcome": "rejected", "category": "made_up"}, "Unknown rejection category"),
        # `field` without a `category` is not a body the sandbox accepts.
        ({"outcome": "rejected", "field": "tax_id"}, "field needs a rejection category"),
        ({"outcome": "approved", "field": "tax_id"}, "field needs a rejection category"),
    ],
)
async def test_sandbox_refuses_bodies_the_contract_does_not_allow(fields, expected, session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/simulate", form(**fields))
    assert expected in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if "sandbox" in c[1]] == []


async def test_an_approval_never_carries_a_category(session):
    calls: list = []
    app = make_app(stub(routes(extra={("POST", SIMULATE): httpx.Response(200, json=APPROVED)}), calls))
    async with signed_in(app) as web:
        await post(
            web, "/applications/app_1/simulate", form(outcome="approved", category="compliance")
        )
    assert json.loads(calls[-1][2]) == {"outcome": "approved"}


async def test_a_replayed_decision_surfaces_the_409(session):
    """The live sandbox refuses a decision on a decided application with
    409 APPLICATION_ALREADY_DECIDED (verified 2026-08-28) — the console
    surfaces Conduit's title as the banner rather than crashing or retrying."""
    responses = iter(
        [
            httpx.Response(200, json=APPROVED),
            httpx.Response(
                409,
                json={
                    "type": "APPLICATION_ALREADY_DECIDED",
                    "title": "Application Already Decided",
                    "status": 409,
                },
            ),
        ]
    )
    app = make_app(stub(routes(APPROVED, extra={("POST", SIMULATE): lambda _: next(responses)})))
    async with signed_in(app) as web:
        first = await post(web, "/applications/app_1/simulate", form(outcome="approved"))
        again = await post(web, "/applications/app_1/simulate", form(outcome="approved"))
    assert first.headers["HX-Redirect"].startswith(
        "/applications/app_1?msg=Simulated+approved.&msgsig="
    )
    assert again.headers["HX-Redirect"].startswith(
        "/applications/app_1?err=Conduit+refused+this%3A+APPLICATION_ALREADY_DECIDED&errsig="  # A3
    )


# --- the operation panel (OPERATIONS_SPEC §5) -----------------------------------------


async def stalled_operation(session) -> Operation:
    op, _ = await operations.start(
        session, type="onboarding_submit", **ACTOR, path="/v2/onboarding", body={"a": 1}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    return await operations.transition(
        session,
        op.id,
        "stalled",
        **ACTOR,
        error={
            "type": "SERVER_ERROR",
            "title": "Upstream failure",
            "detail": "No answer.",
            "correlationId": "corr-9",
        },
    )


async def test_outcome_unknown_is_shown_without_a_retry_button(session):
    op, _ = await operations.start(
        session, type="onboarding_submit", **ACTOR, path="/v2/onboarding", body={"a": 2}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        page = await web.get(f"/operations/{op.id}")

    assert "Result being confirmed" in page.text
    assert "do not resubmit" in page.text
    assert "/retry" not in page.text


async def test_a_stalled_operation_retries_with_the_same_key(session):
    op = await stalled_operation(session)
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/onboarding"): httpx.Response(202, json=APP)}), calls)
    )
    async with signed_in(app) as web:
        page = await web.get(f"/operations/{op.id}")
        assert "Couldn&#39;t confirm" in page.text
        assert "corr-9" in page.text
        assert "Retry (same idempotency key)" in page.text
        assert "Mark abandoned" not in page.text  # operator is not an admin
        retried = await post(web, f"/operations/{op.id}/retry", b"")

    assert retried.headers["HX-Redirect"].startswith(f"/operations/{op.id}?msg=Retried.&msgsig=")
    sent = next(c for c in calls if c[1] == "/v2/onboarding")
    assert sent[0] == "POST"
    refreshed = await session.get(Operation, op.id)
    await session.refresh(refreshed)
    assert refreshed.state == "confirmed"
    assert refreshed.idempotency_key == op.idempotency_key  # same row, same key


async def test_a_retry_that_lost_to_a_confirming_webhook_landed_on_the_operation(
    session, monkeypatch
):
    """This route reads `state != "stalled"` in its own
    session and nothing holds that read: a webhook applying Conduit's own
    account of the row can land in the gap between it and the locked
    `stalled -> in_flight` inside `execute_operation`, and the retry then
    arrives at a row that is already `confirmed`.

    The webhook is applied here through `operations.transition` — the same call
    `app.projections` makes when one is received — from a second session, at the
    one instant that is the bug. Gathering a real webhook POST against a real
    retry POST would reproduce this only when the scheduler felt like it; the
    behaviour under test is what the loser is told, not whether Python
    interleaves.

    It used to be told nothing: `IllegalTransition confirmed -> in_flight`, no
    handler, a bare 500 on the one page whose whole job is telling an operator
    whether a submission landed — and a 500 there is what makes somebody submit
    it again. Zero wire calls either way; this is about the answer.
    """
    op = await stalled_operation(session)
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", "/v2/onboarding"): httpx.Response(202, json=APP)}), calls)
    )
    real = applications.execute_operation

    async def webhook_lands_first(inner_session, operation, **kwargs):
        async with sessionmaker()() as webhook:
            await operations.transition(
                webhook,
                operation.id,
                "confirmed",
                actor_id=audit.SYSTEM_ACTOR_ID,
                actor_email=audit.SYSTEM_ACTOR_EMAIL,
                conduit_resource_id="app_1",
            )
        return await real(inner_session, operation, **kwargs)

    monkeypatch.setattr(applications, "execute_operation", webhook_lands_first)
    async with signed_in(app) as web:
        response = await post(web, f"/operations/{op.id}/retry", b"")

    assert [c for c in calls if c[1] == "/v2/onboarding"] == [], "nothing may go on the wire"
    assert response.status_code == 204
    landed = response.headers["HX-Redirect"]
    assert landed.startswith(f"/operations/{op.id}?msg="), landed
    assert unquote_plus(landed.split("msg=", 1)[1].split("&", 1)[0]) == ALREADY_MOVED
    # Not "Retried.", which this route would have said had it got that far, and
    # which would be a false claim about a submission this click did not resend.
    assert "Retried." not in unquote_plus(landed)
    refreshed = await session.get(Operation, op.id)
    await session.refresh(refreshed)
    assert refreshed.state == "confirmed"
    assert refreshed.attempt_count == 1, "the webhook's answer stands, unattempted again"


async def test_abandon_is_admin_only(session):
    op = await stalled_operation(session)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        refused = await post(web, f"/operations/{op.id}/abandon", b"")
    assert refused.status_code == 403

    async with signed_in(app, groups="admins") as boss:
        page = await boss.get(f"/operations/{op.id}")
        assert "Mark abandoned" in page.text
        # An earlier round: the one thing the button does not say. This handler makes no
        # Conduit call — the send may well have landed — and its entire effect
        # is releasing the §1 request-hash guard, which is why the resubmit that
        # follows carries a *new* idempotency key rather than this row's.
        assert "It tells Conduit nothing, and the original request" in page.text
        # "an identical resubmit" was the wrong promise.
        # `operations.start` checks the intent nonce first and in every state,
        # so a mechanically replayed identical body resolves back to *this*
        # abandoned row. A fresh render is what mints a fresh intent.
        assert (
            "re-submitting from a freshly loaded form becomes a new operation "
            "with a new idempotency key." in page.text
        )
        done = await post(boss, f"/operations/{op.id}/abandon", b"")

    assert done.headers["HX-Redirect"].startswith(f"/operations/{op.id}?msg=Marked+abandoned.&msgsig=")
    refreshed = await session.get(Operation, op.id)
    await session.refresh(refreshed)
    assert refreshed.state == "abandoned"
    trail = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "operation.abandoned")
        )
    ).scalars().all()
    assert len(trail) == 1


async def test_retry_is_refused_on_anything_but_a_stalled_operation(session):
    op, _ = await operations.start(
        session, type="onboarding_submit", **ACTOR, path="/v2/onboarding", body={"a": 3}
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(web, f"/operations/{op.id}/retry", b"")
    assert "Only+a+stalled+operation" in response.headers["HX-Redirect"]


async def test_the_application_page_carries_its_operation_panel(session):
    _, op = await submitted_draft(session)
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        detail = await web.get("/applications/app_1")
    assert str(op.id) in detail.text
    assert "onboarding_submit" in detail.text


async def test_a_definitive_conflict_surfaces_the_resource_it_named(session):
    """OPERATIONS_SPEC §4: the reconciler's rejection carries the *pre-existing*
    resource's id, and the panel shows it. Generic by `details.*Id`, so a conflict
    type nobody wrote a template for still points somewhere useful."""
    op, _ = await operations.start(
        session, type="onboarding_submit", **ACTOR, path="/v2/onboarding", body={"taxId": "X"}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "outcome_unknown", **ACTOR)
    await operations.transition(
        session,
        op.id,
        "rejected",
        **ACTOR,
        error={
            "type": "CUSTOMER_ALREADY_ONBOARDED",
            "title": "Customer already onboarded",
            "status": 409,
            "detail": "A customer with this tax ID has already completed onboarding.",
            "correlationId": "cor_dup",
            "details": {
                "customerId": "cus_existing",
                # Id-shaped, because A3 renders a named resource only when it
                # is one (gate m5): `app_9` is a fixture, not a Conduit id.
                "applicationId": "app_9000000000000000000",
                "count": 2,
            },
        },
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        page_text = (await web.get(f"/operations/{op.id}")).text

    assert "existing customer: <code>cus_existing</code>" in page_text
    assert "existing application: <code>app_9000000000000000000</code>" in page_text
    # Only `*Id` keys, and only string values: `count` never becomes a row.
    # Asserted as the row it would be, not as the bare word — "Accounts" in the
    # nav strip contains it, and a substring of the chrome is
    # not this panel saying anything.
    assert "existing count" not in page_text


async def test_a_locally_minted_rejection_is_labelled_as_such(session):
    """OPERATIONS_SPEC §5: no correlation id, because Conduit never issued one."""
    op, _ = await operations.start(
        session, type="payout_cancel", **ACTOR, path="/v2/payouts/txn_1/cancel", body={}
    )
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(
        session,
        op.id,
        "rejected",
        **ACTOR,
        conduit_resource_id="txn_1",
        error={
            "type": "RESOURCE_NOT_ACTIONABLE",
            "title": "Too late to apply",
            "detail": "The resource is already completed.",
            "resolution": "No action needed.",
        },
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        page = await web.get(f"/operations/{op.id}")

    assert "Determined from Conduit's records" in page.text
    assert "txn_1" in page.text


# --- CSRF, everywhere -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/applications/app_1/resubmit",
        "/applications/app_1/persons/app_1:o1M/idv-link",
        "/applications/app_1/simulate",
        "/rfis/rfi_1/respond",
    ],
)
async def test_mutations_without_the_csrf_header_are_refused(url):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            url, content=b"", headers={"content-type": "application/x-www-form-urlencoded"}
        )
    assert response.status_code == 403
    assert "CSRF" in response.text


# --- resubmission fails closed --------------------------------------------------------


@pytest.mark.parametrize(
    "application,expected",
    [
        (APPROVED, "Only a rejected application"),
        (APP, "Only a rejected application"),  # still processing
        ({**REJECTED, "resubmittable": None}, "has not said"),
        (FINAL, "has not said"),
        ({**APP, "status": "quarantined"}, "Only a rejected application"),
    ],
)
async def test_resubmit_refuses_anything_but_a_correctable_rejection(
    application, expected, session
):
    """Fail closed: only `resubmittable is False` was blocked, so an approved,
    pending or unknown-status application re-opened a draft whose answers
    Conduit had already accepted."""
    draft, _ = await submitted_draft(session)
    app = make_app(stub(routes(application)))
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/resubmit", b"")
    assert expected in unquote_plus(response.headers["HX-Redirect"])
    assert "/onboarding/" not in response.headers["HX-Redirect"]


@pytest.mark.parametrize(
    "response_",
    [
        httpx.Response(404, json={"type": "NOT_FOUND", "title": "No such application"}),
        httpx.Response(500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}),
        httpx.Response(200, json={"nonsense": True}),  # 2xx with no status field
    ],
)
async def test_resubmit_refuses_when_the_application_cannot_be_read(response_, session):
    """An unreadable application is not evidence that it was rejected."""
    await submitted_draft(session)
    app = make_app(stub({("GET", "/v2/applications/app_1"): response_}))
    async with signed_in(app) as web:
        result = await post(web, "/applications/app_1/resubmit", b"")
    assert "/onboarding/" not in result.headers["HX-Redirect"]
    assert "err=" in result.headers["HX-Redirect"]


async def test_resubmit_still_works_on_a_correctable_rejection(session):
    draft, _ = await submitted_draft(session)
    app = make_app(stub(routes(REJECTED)))
    async with signed_in(app) as web:
        response = await post(web, "/applications/app_1/resubmit", b"")
    assert response.headers["HX-Redirect"].startswith(f"/onboarding/{draft.id}")


async def test_another_operators_draft_is_not_reopened_through_an_application(session):
    """The same ownership rule as every other draft surface, one redirect later."""
    await submitted_draft(session)  # owned by ops@example.com
    app = make_app(stub(routes(REJECTED)))
    from tests.web_harness import PROXY_SECRET

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://console.test",
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "other@example.com",
            "X-Auth-Request-Email": "other@example.com",
            "X-Auth-Request-Groups": "ops",
        },
    ) as intruder:
        await intruder.get("/drafts")
        response = await post(intruder, "/applications/app_1/resubmit", b"")
    assert "No+local+draft" in response.headers["HX-Redirect"]


# --- regressions on the application view ------------------------------------------


async def test_an_rfi_response_can_only_attach_documents_this_actor_uploaded(session):
    """P1. `documentIds[]` went from the browser into `POST /v2/rfis/{id}/responses`
    unchecked, so any `doc_` id an operator could guess or had seen elsewhere —
    another customer's passport scan, a colleague's upload — could be published
    to Conduit as part of this organisation's compliance answer. Nothing
    downstream re-checked it: Conduit validates org-wide, which is the boundary
    this crosses.
    """
    calls: list = []
    app = make_app(
        stub(
            routes(
                rfis=[RFI],
                extra={
                    ("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_mine"}),
                    ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                        201, json={"id": "rfi_1", "status": "responded"}
                    ),
                },
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="rfi_response")
        refused = await post(
            web,
            "/rfis/rfi_1/respond",
            form(
                subjectType="application",
                subject="app_1",
                message="Bill attached.",
                documentIds=["doc_mine", "doc_someone_elses"],
            ),
        )

    assert "Attach+only+documents+you+uploaded" in refused.headers["HX-Redirect"]
    # Refused *before* the ledger: no operation, and nothing on the wire.
    assert (
        await session.execute(select(Operation).where(Operation.type == "rfi_respond"))
    ).scalars().all() == []
    assert [c for c in calls if c[1].endswith("/responses")] == []


async def test_a_document_uploaded_for_something_else_is_not_an_rfi_attachment(session):
    """Same guard, the other half of the rule: purpose as well as uploader. An
    onboarding document is not evidence for a compliance round, and attaching it
    would publish a file whose subject nobody chose for this."""
    app = make_app(
        stub(
            routes(
                rfis=[RFI],
                extra={
                    ("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_kyc"}),
                    ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                        201, json={"id": "rfi_1", "status": "responded"}
                    ),
                },
            )
        )
    )
    async with signed_in(app) as web:
        await upload(web, purpose="organization_onboarding")
        refused = await post(
            web,
            "/rfis/rfi_1/respond",
            form(subjectType="application", subject="app_1", message="x", documentIds=["doc_kyc"]),
        )
    assert "could+not+be+matched" in refused.headers["HX-Redirect"]


async def test_another_actors_upload_is_not_this_actors_to_attach(session):
    """Uploader-actor is the rule because it is the only scope the row carries:
    an `rfi_response` upload has no customer on it (`web/onboarding.upload`
    passes the actor and, for onboarding only, a draft).
    """
    op, _ = await documents.intake(
        session,
        data=PNG,
        filename="theirs.png",
        purpose="rfi_response",
        actor_id="usr_someone_else",
        actor_email="other@example.com",
    )
    await operations.transition(
        session,
        op.id,
        "in_flight",
        actor_id="usr_someone_else",
        actor_email="other@example.com",
    )
    await operations.transition(
        session,
        op.id,
        "confirmed",
        actor_id="usr_someone_else",
        actor_email="other@example.com",
        conduit_resource_id="doc_theirs",
    )

    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in(app) as web:
        refused = await post(
            web,
            "/rfis/rfi_1/respond",
            form(
                subjectType="application", subject="app_1", message="x", documentIds=["doc_theirs"]
            ),
        )
    assert "could+not+be+matched" in refused.headers["HX-Redirect"]
    assert await documents.attachable(
        session, ["doc_theirs"], purpose="rfi_response", actor_id="usr_someone_else"
    ) == {"doc_theirs"}  # …and it is still perfectly attachable by its own uploader


async def test_an_answered_rfi_offers_no_second_answer(session):
    """P1. `rfi.response_submitted` sets `responded`, and only
    `rfi.more_info_requested` — which sets the status back to `open` — asks for
    anything more. A form here invited a second response nobody asked for, under
    labelling that said the operator still owed one."""
    answered = {**RFI, "status": "responded"}
    app = make_app(stub(routes(rfis=[answered])))
    async with signed_in(app) as web:
        page_html = (await web.get("/applications/app_1")).text

    assert "Answered — waiting on Conduit" in page_html
    assert 'hx-post="/rfis/rfi_1/respond"' not in page_html
    assert "rfi_1" in page_html  # still listed: live, just not ours to move
    # …while an open one still gets the form.
    app = make_app(stub(routes(rfis=[RFI])))
    async with signed_in(app) as web:
        open_html = (await web.get("/applications/app_1")).text
    assert 'hx-post="/rfis/rfi_1/respond"' in open_html


async def test_an_unreadable_rfi_list_is_not_reported_as_no_rfis():
    """P2. `for_subject` flattened a 503 to `[]`, so the page stated an absence it
    had never established — the one thing every other read here is careful not
    to do."""
    app = make_app(
        stub(routes(extra={("GET", "/v2/rfis"): httpx.Response(503, json={})}))
    )
    async with signed_in(app) as web:
        page_html = (await web.get("/applications/app_1")).text

    assert "could not be read just now" in page_html
    assert "Conduit has not asked for anything more" not in page_html
