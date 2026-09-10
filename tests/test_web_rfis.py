"""The global RFI index: filters, typed subject links, overdue, pills, paging.

`GET /v2/rfis` is the only list endpoint in the console that answers across
subject kinds, and the console has a page for three of the four kinds it can
name. So the assertions here are mostly about honesty at the edges: the filter
never asks for a status Conduit refuses to serve, a subject kind with no page
never becomes a link, and a settled RFI is never called late.

Nothing on this page mutates — the respond form stays on the subject page
(tests/test_web_applications.py owns it), and the last test here is that a
viewer can read the whole thing.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote_plus

import httpx
from sqlalchemy import func, select

from app import projections
from app.models import Operation
from app.web import rfis as rfis_web
from tests.web_harness import form, make_app, minted_intent, post, signed_in, stub

LIST = "/rfis"
APP_ID = "app_2xPqN8RmK4vL9wT3sYbC7d"
TXN_ID = "txn_2xPqN8RmK4vL9wT3sYbC7d"
CUS_ID = "cus_2xPqN8RmK4vL9wT3sYbC7d"

PAST = (datetime.now(UTC) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")


def rfi(**overrides) -> dict:
    """One row of `ClientRfiPaginatedResponseDto.data`, spec-shaped: `subjects`
    is a **list**, and `summary` is not in the list DTO's required set."""
    return {
        "id": "rfi_2xPqN8RmK4vL9wT3sYbC7d",
        "title": "Proof of address for the beneficial owner",
        "status": "open",
        "subjects": [{"subjectType": "application", "subjectId": APP_ID}],
        "dueAt": FUTURE,
        "publishedAt": "2026-08-20T09:30:00.000Z",
        "createdAt": "2026-08-20T09:30:00.000Z",
        "updatedAt": "2026-08-20T09:30:00.000Z",
        **overrides,
    }


def page(items: list[dict], *, next_cursor=None, prev_cursor=None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": items,
            "meta": {
                "mode": "cursor",
                "nextCursor": next_cursor,
                "previousCursor": prev_cursor,
                "total": len(items),
            },
        },
    )


def routes(items=None, **kwargs):
    return {("GET", "/v2/rfis"): page(items if items is not None else [rfi()], **kwargs)}


def recording() -> tuple[list[str], object]:
    """Handler + the raw query strings it was sent, for the wiring assertions."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/rfis":
            seen.append(request.url.query.decode())
            return page([rfi()])
        return httpx.Response(404, json={"type": "NOT_FOUND", "title": "no"})

    return seen, handler


# --- filter wiring -------------------------------------------------------------------------


async def test_the_default_asks_only_for_what_still_owes_conduit_an_answer():
    """`open` + `responded`, and never `draft`: the endpoint's own description
    says draft RFIs are not visible on this surface, so requesting them would be
    requesting rows Conduit will not send."""
    seen, handler = recording()
    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST)

    assert response.status_code == 200
    query = parse_qs(seen[0])
    assert query["status"] == ["open", "responded"]
    assert "draft" not in seen[0]
    assert query["limit"] == ["25"]
    assert "subjectType" not in query  # not asked for, so not sent


async def test_the_toggle_adds_the_settled_statuses_and_nothing_else():
    seen, handler = recording()
    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST + "?include=settled")

    assert parse_qs(seen[0])["status"] == ["open", "responded", "resolved", "cancelled"]
    # …and the checkbox comes back ticked, so the view describes itself.
    assert 'name="include" value="settled" checked' in response.text


async def test_the_subject_type_filter_reaches_the_wire():
    seen, handler = recording()
    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST + "?subjectType=transaction")

    assert parse_qs(seen[0])["subjectType"] == ["transaction"]
    assert '<option value="transaction" selected>' in response.text


async def test_a_subject_type_outside_the_enum_is_dropped_rather_than_sent():
    """Conduit answers a value outside the enum with a 400. A typo in a URL
    should show the unfiltered page, not an error page."""
    seen, handler = recording()
    app = make_app(handler)
    async with signed_in(app) as web:
        response = await web.get(LIST + "?subjectType=teleport")

    assert "subjectType" not in parse_qs(seen[0])
    assert response.status_code == 200
    # The select falls back to "any" rather than growing an option for a kind
    # nobody can filter on. (The Refresh link still echoes the URL verbatim —
    # that is the operator's own address bar, not a claim by this page.)
    assert '<option value="teleport"' not in response.text
    assert " selected>" not in response.text  # nothing in the select is chosen


# --- typed subject links -------------------------------------------------------------------


async def test_each_subject_kind_this_console_has_a_page_for_is_linked():
    app = make_app(
        stub(
            routes(
                [
                    rfi(id="rfi_a", subjects=[{"subjectType": "application", "subjectId": APP_ID}]),
                    rfi(id="rfi_t", subjects=[{"subjectType": "transaction", "subjectId": TXN_ID}]),
                    rfi(id="rfi_c", subjects=[{"subjectType": "customer", "subjectId": CUS_ID}]),
                ]
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert f'href="/applications/{APP_ID}"' in html
    assert f'href="/transactions/{TXN_ID}"' in html
    assert f'href="/customers/{CUS_ID}"' in html


async def test_a_customer_subject_is_named_and_the_other_kinds_are_not_guessed_at():
    """`customer` is one of the four subject kinds, and
    a cell that says only `cus_…` makes the operator open the row to find out who
    Conduit is asking about — so one bounded customers read (gathered with the
    RFI read, silent on failure) names it. The other three kinds have no name to
    resolve and are left exactly as they were: nothing is invented for them, and
    a customer beyond the bounded page keeps its bare id."""
    known = {"id": CUS_ID, "legalName": "ZZZTEST Ltd", "type": "business"}
    calls: list = []
    app = make_app(
        stub(
            {
                **routes(
                    [
                        rfi(id="rfi_c", subjects=[{"subjectType": "customer", "subjectId": CUS_ID}]),
                        rfi(
                            id="rfi_x",
                            subjects=[{"subjectType": "customer", "subjectId": "cus_elsewhere"}],
                        ),
                        rfi(id="rfi_t", subjects=[{"subjectType": "transaction", "subjectId": TXN_ID}]),
                    ]
                ),
                ("GET", "/v2/customers"): httpx.Response(
                    200, json={"data": [known], "meta": {"total": 1}}
                ),
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "ZZZTEST Ltd" in html
    assert f'href="/customers/{CUS_ID}"' in html and f"<code>{CUS_ID}</code>" in html
    assert "<code>cus_elsewhere</code>" in html  # a miss is the id, never a guess
    assert f'href="/transactions/{TXN_ID}"' in html
    # One bounded read per render, never one per row.
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1


async def test_the_names_read_failing_costs_the_names_and_nothing_else():
    """The resolver is silent on failure by construction: a second banner over a
    list that read fine would be the console reporting an outage it does not
    have. The rows, the filters and the count are untouched."""
    app = make_app(
        stub(
            {
                **routes([rfi(id="rfi_c", subjects=[{"subjectType": "customer", "subjectId": CUS_ID}])]),
                ("GET", "/v2/customers"): httpx.Response(
                    503, json={"type": "UNAVAILABLE", "title": "Customers down"}
                ),
            }
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Showing 1 RFI" in html and f"<code>{CUS_ID}</code>" in html
    assert "Customers down" not in html and "Couldn't read this list" not in html


async def test_a_subject_kind_with_no_page_renders_unlinked_and_does_not_crash():
    """`organization` is in Conduit's `subjectType` enum and has no page in this
    console; `spacecraft` is the kind that gets added next year. Both render as
    text — a link to a 404 is worse than no link, and neither may take the page
    down. The row's title is unlinked for the same reason."""
    app = make_app(
        stub(
            routes(
                [
                    rfi(id="rfi_o", subjects=[{"subjectType": "organization", "subjectId": "org_1"}]),
                    rfi(id="rfi_x", subjects=[{"subjectType": "spacecraft", "subjectId": "spc_1"}]),
                    rfi(id="rfi_n", subjects=[]),
                ]
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(LIST)

    assert response.status_code == 200
    html = response.text
    assert "<code>org_1</code>" in html and "<code>spc_1</code>" in html
    for stray in ('href="/organizations/', 'href="/spacecrafts/', 'href="/organization/'):
        assert stray not in html
    # The unlinked rows' titles are text too: with no subject page to send the
    # operator to, there is nowhere for the title to go.
    assert html.count("Proof of address for the beneficial owner") == 3


async def test_an_rfi_with_several_subjects_shows_all_of_them():
    """`subjects` is a list in the DTO, not a scalar — a console that read only
    the first would silently hide the other half of what an RFI is about."""
    app = make_app(
        stub(
            routes(
                [
                    rfi(
                        subjects=[
                            {"subjectType": "customer", "subjectId": CUS_ID},
                            {"subjectType": "application", "subjectId": APP_ID},
                        ]
                    )
                ]
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert f'href="/customers/{CUS_ID}"' in html and f'href="/applications/{APP_ID}"' in html


# --- due dates -----------------------------------------------------------------------------


async def test_an_overdue_rfi_is_marked_and_a_finished_one_is_never_late():
    """Three rows, one mark. A resolved RFI whose date has passed is not late,
    it is finished — and a date this console cannot parse is not asserted to be
    either (the `ts` filter's rule, applied to the comparison as well as to the
    rendering)."""
    app = make_app(
        stub(
            routes(
                [
                    rfi(id="rfi_late", dueAt=PAST),
                    rfi(id="rfi_done", status="resolved", dueAt=PAST),
                    rfi(id="rfi_weird", dueAt="whenever"),
                    rfi(id="rfi_soon", dueAt=FUTURE),
                ]
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert html.count('<span class="pill warn">Overdue</span>') == 1
    # The unparseable date still reaches the screen rather than blanking a cell.
    assert "whenever" in html


# --- the pill vocabulary -------------------------------------------------------------------


async def test_every_rfi_status_renders_as_itself_not_as_unknown():
    """The `orders` defect, pre-empted: a `PILL_TONES` kind that is missing or
    incomplete renders real statuses as "Unknown: open" and nobody notices,
    because the Unknown assertions elsewhere all use fabricated words.

    `draft` is here even though this page never asks for one: if Conduit ever
    serves it, it must render as itself.
    """
    app = make_app(
        stub(
            routes(
                [rfi(id=f"rfi_{s}", status=s) for s in
                 ("draft", "open", "responded", "resolved", "cancelled")]
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST + "?include=settled")).text

    for status, tone in (
        ("Draft", "muted"),
        ("Open", "warn"),
        ("Responded", "wait"),
        ("Resolved", "ok"),
        ("Cancelled", "muted"),
    ):
        assert f'<span class="pill {tone}">{status}</span>' in html
    assert "Unknown:" not in html


def test_the_rfi_pills_and_the_pinned_spec_cannot_diverge():
    """The tone table is written by hand; the vocabulary is Conduit's. A status
    added to the API and not to the table is the defect above, coming back — so
    the guard reads the enum straight out of the pinned snapshot rather than
    restating it here.

    The tones are the house ladders: `open` is the only one that needs a human,
    `resolved` is the only good ending, and nothing finished is toned `wait`.
    """
    from app.web import PILL_TONES
    from app.web.rfis import OPEN_STATUSES, SETTLED_STATUSES, SUBJECT_TYPES
    from scripts.check_openapi_drift import PINNED

    spec = json.loads(PINNED.read_text())["components"]["schemas"]
    listed = spec["ClientRfiPaginatedResponseDto"]["properties"]["data"]["items"]["properties"]
    assert set(PILL_TONES["rfis"]) == set(listed["status"]["enum"])
    assert set(PILL_TONES["rfis"]) == set(spec["ClientRfiDetailDto"]["properties"]["status"]["enum"])

    tones = PILL_TONES["rfis"]
    assert tones["open"] == "warn" and tones["resolved"] == "ok"
    assert tones["responded"] == "wait"
    for finished in ("resolved", "cancelled"):
        assert tones[finished] != "warn"

    # The filter tuples partition the vocabulary minus `draft`, which Conduit
    # does not serve on this surface — so the toggle can never ask for a status
    # the pills cannot tone, and can never silently drop a new one.
    assert set(OPEN_STATUSES) | set(SETTLED_STATUSES) == set(tones) - {"draft"}
    assert set(SUBJECT_TYPES) == set(listed["subjects"]["items"]["properties"]["subjectType"]["enum"])


# --- pagination ----------------------------------------------------------------------------


async def test_paging_carries_the_filters_and_the_cursor():
    """The pager: a cursor is an opaque token and every filter has to
    survive the page turn, or page two is a different query's page two."""
    app = make_app(stub(routes(next_cursor="eyJpZCI6ICJyZmkifQ==", prev_cursor="cHJldg==")))
    async with signed_in(app) as web:
        html = (await web.get(LIST + "?include=settled&subjectType=customer")).text

    for link in (
        "include=settled",
        "subjectType=customer",
        "cursor=eyJpZCI6ICJyZmkifQ%3D%3D",
        "direction=backward",
    ):
        assert link in html
    assert ">Next</a>" in html and ">Previous</a>" in html
    # The design finding, from missing them entirely on the live
    # console: both are pills inside the table's own banded footer, not two bare
    # links loose under the table, and the rows-per-page control is beside them.
    foot = html.split('<div class="table-foot">')[1].split("</div>\n</div>")[0]
    assert '<a class="btn" href' in foot and ">Previous</a>" in foot and ">Next</a>" in foot
    assert '<span class="micro-label">Rows</span>' in foot

    # And the cursor is actually sent back on the next hop.
    seen, handler = recording()
    app = make_app(handler)
    async with signed_in(app) as web:
        await web.get(LIST + "?include=settled&cursor=eyJpZCI6ICJyZmkifQ%3D%3D")
    query = parse_qs(seen[0])
    assert query["cursor"] == ['eyJpZCI6ICJyZmkifQ==']
    assert query["status"] == ["open", "responded", "resolved", "cancelled"]


# --- chrome, failure, roles ----------------------------------------------------------------


async def test_the_ribbon_carries_the_rfis_entry_and_this_page_is_the_active_one():
    """This entry moved OUT of the Customers group and became
    a top-level one, between the browse groups and the Actions rule: an RFI is
    Conduit's demand about a customer *or* about a transaction, so filing it
    under either noun hid half of what the page lists, and it is nobody's verb.

    What replaces the old group assertion is the position: after Accounts (the
    last Customers sub) and after Orders (the last Transactions one), and before
    the seam."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        here = (await web.get(LIST)).text
        elsewhere = (await web.get("/drafts")).text

    assert '<a id="ribbon-rfis" class="item on" href="/rfis" aria-current="page">RFIs</a>' in here
    assert '<a id="ribbon-rfis" class="item" href="/rfis">RFIs</a>' in elsewhere
    # Standalone: the group that used to hold it is lit by its own subs only, and
    # this page lights neither of the two browse groups.
    assert '<div class="group on">' not in here
    assert '<p class="group-head" id="grp-customers">Customers</p>' in here  # the group itself is untouched
    assert here.index('href="/accounts"') < here.index('href="/rfis"')
    assert here.index('href="/orders"') < here.index('href="/rfis"')
    assert here.index('href="/rfis"') < here.index('<p class="divider">Actions</p>')


BADGE = '<span class="count" title="Open requests this console has observed">'


async def observe(session, resource_id: str, status: str) -> None:
    await projections.apply_observation(
        session, resource_kind="rfis", resource_id=resource_id, observed={"status": status}
    )


async def test_the_ribbon_badge_counts_the_open_pair_and_nothing_else(session):
    """The count is `open` + `responded` — the ladder's one non-terminal rung
    above `draft` — read from this installation's own projections. `resolved`
    and `cancelled` owe nobody an answer; a `draft` is never even served on the
    RFI surface; and an unknown status is not guessed into the count, for the
    same reason it is never guessed into a terminal state.

    Asserted on `/drafts`, not on `/rfis`: the badge is chrome, so its whole
    claim is that it renders on a page that has nothing to do with RFIs."""
    for index, status in enumerate(("open", "responded", "resolved", "cancelled", "draft")):
        await observe(session, f"rfi_{index}", status)
    await observe(session, "rfi_weird", "escalated_to_mars")

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/drafts")).text

    assert f'href="/rfis">RFIs{BADGE}2</span></a>' in html


async def test_an_empty_inbox_wears_no_badge(session):
    """Zero renders nothing at all — by design, not by accident. An empty inbox
    needs no number on it, and the absence is also what a *failed* read renders,
    so neither can be mistaken for a confident count."""
    await observe(session, "rfi_done", "resolved")

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/drafts")).text

    assert '<a id="ribbon-rfis" class="item" href="/rfis">RFIs</a>' in html
    assert BADGE not in html


async def test_a_failed_count_renders_no_badge_and_no_fake_zero(session, monkeypatch):
    """A count that could not be taken must not paint a "0" the console never
    counted. The page still renders, which is the point of catching at all.

    There IS an open RFI here, so without the failure this page would carry a
    badge — otherwise the assertion would pass on an empty database and prove
    nothing."""
    from app import web as web_module

    await observe(session, "rfi_open", "open")

    def explode(*args, **kwargs):
        raise RuntimeError("no database today")

    monkeypatch.setattr(web_module, "select", explode)

    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get("/drafts")

    assert response.status_code == 200
    assert '<a id="ribbon-rfis" class="item" href="/rfis">RFIs</a>' in response.text
    assert BADGE not in response.text


async def test_this_page_head_carries_no_action_pill_by_design():
    """Every other list surface gained a primary-action pill; this
    one deliberately did not, and the emptiness is the design. Only Conduit
    creates an RFI, so there is no verb to offer — and answering one belongs to
    the subject page each row already links to. Asserted for an OPERATOR, who
    would see a pill if one existed."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert '<h1>Requests for information</h1>' in html
    assert '<div class="action">' not in html


def test_the_wire_filter_and_the_badge_read_the_same_pair():
    """`OPEN_STATUSES` is what this page asks Conduit for; `OPEN_RFI_STATES` is
    what the badge counts locally. They are two expressions of one idea and a
    drift between them would put a number in the ribbon that the page it links
    to does not show."""
    assert set(rfis_web.OPEN_STATUSES) == projections.OPEN_RFI_STATES == {"open", "responded"}


async def test_an_unreadable_list_is_not_rendered_as_an_empty_one():
    """The house honest-failure rule: the handler substitutes an
    empty `Page` on failure, so both the meta line and the table's empty row have
    to branch on the problem — "there are none" and "we could not ask" are
    different facts."""
    app = make_app(
        stub({("GET", "/v2/rfis"): httpx.Response(503, json={"type": "XYZ_REFUSED", "title": "Conduit is down"})})
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Conduit refused this: XYZ_REFUSED" in html  # A3
    assert "Couldn't read this list" in html
    assert "Showing 0 RFI" not in html
    assert "The list could not be read — see above." in html
    assert "Nothing is being asked of you" not in html


async def test_the_empty_page_names_the_toggle_that_would_widen_it():
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Showing 0 RFIs" in html
    # "Switch to", not "Tick": the filter is a two-segment toggle now, and an
    # empty state that names a control the page does not have is a dead end.
    assert 'Switch to "Include resolved and cancelled"' in html


async def test_a_viewer_may_read_the_page_and_finds_nothing_to_press():
    """Roles: the page is a read. Everything that mutates an RFI — acknowledge
    and respond — lives on the subject page, so a viewer's render is the same
    render as an operator's, and neither contains a respond form."""
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as viewer:
        response = await viewer.get(LIST)
    async with signed_in(app) as operator:
        operator_html = (await operator.get(LIST)).text

    assert response.status_code == 200
    assert "Proof of address" in response.text
    assert "hx-post" not in response.text
    assert "/respond" not in response.text and "/acknowledge" not in response.text
    # The one form on either render is the GET filter bar: this page has no
    # operator-only half, so the two roles see the same page (the CSRF token in
    # `hx-headers` is per session and is the only byte that differs).
    for html in (response.text, operator_html):
        assert html.count("<form") == 1
        assert 'method="get"' in html
    # The chrome's quick-actions cluster lives inside `<main>` and
    # DOES differ by role — that is its whole job. It is dropped from both sides
    # here because the claim being pinned is about this PAGE: an RFI list has no
    # operator-only half of its own, and adding chrome that does must not be
    # able to hide one appearing.
    without = [
        re.sub(r'<nav id="quick-actions".*?</nav>', "", html, flags=re.S).split("<main")[1]
        for html in (response.text, operator_html)
    ]
    assert without[0] == without[1]

# --- the other two attention badges (2026-09-02) --------------------


async def stalled_operation(session, n):
    """One operation driven to `stalled` through the real machine."""
    from app import operations
    op, _ = await operations.start(
        session, type="payout_create", actor_id="usr_b", actor_email="b@x",
        path="/v2/payouts", body={"amount": f"{n}.00"},
    )
    for state in ("in_flight", "outcome_unknown", "stalled"):
        await operations.transition(session, op.id, state, actor_id="usr_b", actor_email="b@x")
    return op


async def test_the_stalled_operations_badge_counts_stalled_and_nothing_else(session):
    """The Overview badge is the chrome's sharpest claim — money only a human
    can settle — so it counts `stalled` EXACTLY: the other active states
    resolve themselves (worker, reconciler), and a badge over self-resolving
    work teaches operators to ignore badges."""
    from app import operations
    await stalled_operation(session, 1)
    await stalled_operation(session, 2)
    op, _ = await operations.start(  # in_flight: active, NOT badged
        session, type="payout_create", actor_id="usr_b", actor_email="b@x",
        path="/v2/payouts", body={"amount": "3.00"},
    )
    await operations.transition(session, op.id, "in_flight", actor_id="usr_b", actor_email="b@x")

    app = make_app(stub({("GET", "/v2/rfis"): httpx.Response(200, json={"data": []})}))
    async with signed_in(app) as web:
        html = (await web.get("/rfis")).text
    ribbon = html.split('id="ribbon"', 1)[1].split("</nav>", 1)[0]
    overview = ribbon.split(">Overview", 1)[0].rsplit("<a", 1)[1] + ribbon.split(">Overview", 1)[1].split("</a>", 1)[0]
    assert "settling them is yours" in overview
    assert ">2<" in overview


async def test_the_resubmittable_badge_counts_the_rejected_resubmittable_pair(session):
    """Rejected AND resubmittable — the applications list's amber pair. A final
    rejection has no operator action and must not light the badge."""
    for rid, status, flag in (("app_r1", "rejected", True), ("app_r2", "rejected", False),
                              ("app_r3", "pending", True)):
        await projections.apply_observation(
            session, resource_kind="applications", resource_id=rid,
            observed={"status": status, "resubmittable": flag},
        )
    app = make_app(stub({("GET", "/v2/rfis"): httpx.Response(200, json={"data": []})}))
    async with signed_in(app) as web:
        html = (await web.get("/rfis")).text
    ribbon = html.split('id="ribbon"', 1)[1].split("</nav>", 1)[0]
    apps = ribbon.split('href="/applications"', 1)[1].split("</a>", 1)[0]
    assert "corrected and resubmitted" in apps and ">1<" in apps


# --- respond attribution ------------------------------------------------------


async def test_a_typed_email_cannot_attribute_an_rfi_answer_to_another_operator(session):
    """The form's email field is pre-filled but not trusted: `values.get("email")`
    used to win over the signed-in actor whenever it was non-empty, so a crafted
    POST (or a careless edit of the field) could attribute a compliance answer to
    someone who never touched it. The wire body must carry the actor Conduit and
    this console's own audit trail agree on, never the typed string."""
    calls: list = []
    app = make_app(
        stub(
            {
                ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                    201, json={"id": "rfi_1", "status": "responded"}
                )
            },
            calls,
        )
    )
    async with signed_in(app) as web:  # signed in as ops@example.com
        response = await post(
            web,
            "/rfis/rfi_1/respond",
            form(
                subjectType="application",
                subject=APP_ID,
                message="Utility bill attached.",
                email="attacker@evil.example",
            ),
        )

    assert "HX-Redirect" in response.headers
    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/rfis/rfi_1/responses"))
    assert sent["submittedBy"]["email"] == "ops@example.com"
    assert sent["submittedBy"]["email"] != "attacker@evil.example"


async def test_the_actors_own_typed_address_still_sends_as_normal(session):
    """The ordinary case, unchanged: the pre-filled form submits the actor's own
    address, and the response still reaches Conduit and confirms."""
    calls: list = []
    app = make_app(
        stub(
            {
                ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                    201, json={"id": "rfi_1", "status": "responded"}
                )
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            "/rfis/rfi_1/respond",
            form(
                subjectType="application",
                subject=APP_ID,
                message="Utility bill attached.",
                email="ops@example.com",
            ),
        )

    # The flash content itself (the sealed token) is not this test's
    # concern; what matters is that the ordinary case still redirects and sends.
    assert response.headers["HX-Redirect"].startswith(f"/applications/{APP_ID}?msg=")
    sent = json.loads(next(c[2] for c in calls if c[1] == "/v2/rfis/rfi_1/responses"))
    assert sent["submittedBy"]["email"] == "ops@example.com"


async def test_a_spent_respond_nonce_replayed_at_another_rfi_is_refused(session):
    """`by_intent` is scoped to the operation type, so the nonce spent
    answering rfi_1 answered a submit against rfi_2 — and the route flashed
    "Response sent." for an RFI whose deadline is still running out.

    The message is deliberately identical in both submits: what is being
    replayed here is the resource, and the path is inside `request_hash`.
    """
    calls: list = []
    app = make_app(
        stub(
            {
                # Both stubbed to succeed: the only thing that may differ
                # between the two submits is whether the call is made at all.
                ("POST", "/v2/rfis/rfi_1/responses"): httpx.Response(
                    201, json={"id": "rfi_1", "status": "responded"}
                ),
                ("POST", "/v2/rfis/rfi_2/responses"): httpx.Response(
                    201, json={"id": "rfi_2", "status": "responded"}
                ),
            },
            calls,
        )
    )
    nonce = minted_intent()
    answer = dict(
        subjectType="application",
        subject=APP_ID,
        message="Utility bill attached.",
        intent=nonce,
    )
    async with signed_in(app) as web:
        first = await post(web, "/rfis/rfi_1/respond", form(**answer))
        replay = await post(web, "/rfis/rfi_2/respond", form(**answer))

    assert "msg=Response+sent" in first.headers["HX-Redirect"]
    # rfi_2 was never answered and never got an operation of its own — both true
    # before the guard existed too. Only the sentence was wrong.
    assert [c for c in calls if c[1] == "/v2/rfis/rfi_2/responses"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(
                Operation.request_path == "/v2/rfis/rfi_2/responses"
            )
        )
    ) == 0
    landing = unquote_plus(replay.headers.get("HX-Redirect") or replay.headers["location"])
    assert "Response sent." not in landing
    assert "had already been used" in landing


async def test_all_three_badges_fail_closed_together(session, monkeypatch):
    """One failed count clears all three — two rendered badges beside a
    silently vanished third would read as "nothing stalled", a confident claim
    the failed read cannot back."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS
    async def broken(self, *a, **k):
        raise RuntimeError("db momentarily unavailable")
    monkeypatch.setattr(_AS, "scalar", broken)
    app = make_app(stub({("GET", "/v2/rfis"): httpx.Response(200, json={"data": []})}))
    async with signed_in(app) as web:
        html = (await web.get("/rfis")).text
    ribbon = html.split('id="ribbon"', 1)[1].split("</nav>", 1)[0]
    assert '<span class="count"' not in ribbon
