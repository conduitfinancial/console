"""The landing dashboard: "what needs my attention", from the local database.

The load-bearing property is at the foot of the file — the page renders
completely with zero Conduit ANSWERS, and exactly one request leaves it (the
bounded customers read that names the ids). An operator whose console looks
wrong lands here first, and that is exactly the moment Conduit may be the thing
that is wrong.
"""

from __future__ import annotations

import re
from datetime import UTC

import pytest

from app import operations, projections
from app.models import ACTIVE_STATES, TERMINAL_STATES, Draft
from app.onboarding import drafts
from app.web import PILL_TONES
from app.web.dashboard import NEXT_STEP
from tests.conftest import ROOT, settings_override
from tests.web_harness import (
    cells_with_hero,
    forbidden_affordances,
    hero_numerals,
    make_app,
    signed_in,
    stub,
)

ACTOR = "ops@example.com"


def nothing():
    """A Conduit that 404s everything loudly — the dashboard must not touch it."""
    return stub({})


async def unresolved(session, type="payout_create", body=None, customer_id=None):
    op, _ = await operations.start(
        session,
        type=type,
        actor_id=ACTOR,
        actor_email=ACTOR,
        path="/v2/payouts",
        body=body or {"amount": "1.00"},
        customer_id=customer_id,
    )
    await operations.transition(session, op.id, "in_flight", actor_id=ACTOR, actor_email=ACTOR)
    return op


async def observe(session, kind, resource_id, status):
    await projections.apply_observation(
        session, resource_kind=kind, resource_id=resource_id, observed={"status": status}
    )


async def draft(session, country="US"):
    return await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country=country
    )


# --- empty ---------------------------------------------------------------------------


async def test_an_empty_console_says_what_would_fill_each_table():
    """DESIGN.md: an empty state is a sentence plus the one action that fills
    it, never a bare "No rows"."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        response = await web.get("/")

    assert response.status_code == 200
    assert "Every mutation this console sent has an answer" in response.text
    assert "No application is waiting on a decision." in response.text
    assert "Nothing half-finished." in response.text
    assert "Nothing observed yet" in response.text
    assert "No rows" not in response.text
    # Three doors, named one by one, and three is the ceiling by ruling (design
    # pass 2026-09-02: besides the chrome, at most ONE door per page):
    #   1. the chrome's quick-actions pill, on every page;
    #   2. "A new customer starts here" — the applications table's empty state;
    #   3. "Answers autosave from the first one" — the drafts table's.
    # Both empty-state links are sentences explaining an empty table, not
    # controls, which is why they survive the ruling. The page-head primary this
    # count used to include is gone: it was added because a landed
    # operator had no other way in, and the chrome is now that way.
    assert response.text.count('href="/onboarding"') == 3
    assert '<a class="btn primary" href="/onboarding">' not in response.text
    assert '<a class="btn" href="/onboarding">Onboard</a>' in response.text
    assert 'href="/transactions"' in response.text


async def test_a_viewer_gets_the_page_without_the_operator_actions():
    app = make_app(nothing())
    async with signed_in(app, groups="readers") as web:
        response = await web.get("/")
    assert response.status_code == 200
    assert "<h1>Overview</h1>" in response.text
    assert 'href="/onboarding"' not in response.text  # operators start onboardings


# --- populated -----------------------------------------------------------------------


async def test_every_section_shows_its_rows(session):
    await unresolved(session, customer_id="cus_7")
    await observe(session, "applications", "app_1", "pending")
    await observe(session, "transactions", "txn_1", "processing")
    row = await draft(session, country="DE")

    app = make_app(nothing())
    async with signed_in(app) as web:
        response = await web.get("/")
    html = response.text

    # a. unresolved operations — type, state pill, customer, age, next step, link
    assert "payout_create" in html and "cus_7" in html
    assert "Sending" in html  # the in_flight pill's operator-facing label
    assert "just now" in html
    assert "Sent — waiting for Conduit&#39;s answer." in html
    assert '/operations/' in html
    # b. pending applications
    assert 'href="/applications/app_1"' in html and "Pending" in html
    # c. open drafts
    assert f'href="/onboarding/{row.id}"' in html and "DE" in html
    # d. recent transactions
    assert 'href="/transactions/txn_1"' in html and "Processing" in html


async def test_the_attention_table_says_what_happens_next(session):
    """The pill answers "what state is this"; the operator's actual
    question is "is this mine to act on", and that is a different sentence per
    state — OPERATIONS_SPEC §2/§5, compressed to one line each."""
    waiting = await unresolved(session, body={"n": "waiting"})
    unknown = await unresolved(session, body={"n": "unknown"})
    await operations.transition(
        session, unknown.id, "outcome_unknown", actor_id=ACTOR, actor_email=ACTOR
    )

    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    assert "<th>What happens next</th>" in html
    assert "Sent — waiting for Conduit&#39;s answer." in html
    assert "The reconciler is asking Conduit what happened." in html
    # …and the column is *only* that: the row still carries its state pill, the
    # sentence never replaces it, and the two never say the same words twice.
    assert "Sending" in html and "Result being confirmed</span>" in html
    assert str(waiting.id) in html and str(unknown.id) in html


def test_every_unresolved_state_has_a_sentence_and_an_unknown_one_is_not_guessed():
    """The dashboard lists exactly `ACTIVE_STATES`, so the two lists must not be
    able to drift: a state added to one and not the other would render a blank
    cell on the page an operator lands on when something is wrong."""
    from app.models import ACTIVE_STATES
    from app.web.dashboard import NEXT_STEP

    assert set(NEXT_STEP) == set(ACTIVE_STATES)
    # Same rule as an unmapped status pill: an em-dash, never a guess.
    assert NEXT_STEP.get("some_future_state", "—") == "—"


async def test_settled_work_drops_off_the_attention_lists(session):
    """Terminality comes from `projections.TERMINAL`, and a resolved operation
    leaves `ACTIVE_STATES` — neither list re-states the vocabulary."""
    op = await unresolved(session)
    await operations.transition(
        session, op.id, "confirmed", actor_id=ACTOR, actor_email=ACTOR,
        conduit_resource_id="txn_9",
    )
    await observe(session, "applications", "app_done", "approved")
    await observe(session, "transactions", "txn_9", "completed")

    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    assert str(op.id) not in html            # resolved: not an attention item
    assert "app_done" not in html            # terminal application
    assert 'href="/transactions/txn_9"' in html  # the ledger still shows it


async def test_a_projection_with_no_status_is_pending_not_settled(session):
    """`apply_observation` stores a NULL state for an observation that carried no
    status; NULL is not terminal (`projections.is_terminal`), so the row is still
    something a human may need to look at."""
    await projections.apply_observation(
        session, resource_kind="applications", resource_id="app_quiet", observed={}
    )
    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text
    assert 'href="/applications/app_quiet"' in html


async def test_each_list_is_capped_and_points_at_its_full_section(session):
    for index in range(12):
        await unresolved(session, body={"n": index})
    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    assert html.count('<a href="/operations/') == 10
    assert "Showing the ten oldest unresolved operations." in html
    for link in ("/applications", "/drafts", "/transactions"):
        assert f'<a href="{link}">View all' in html


async def test_timestamps_are_compact_utc_with_the_full_iso_on_hover(session):
    """One filter, one format: an operator comparing a row against Conduit's
    records must not have to guess the timezone — and an earlier round's rule is that
    they must not have to *decode* it either. The zone is a word, never a `Z`."""
    row = await draft(session)
    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text
    moment = row.updated_at.astimezone(UTC)
    assert f'title="{moment.isoformat()}"' in html
    assert f">{moment:%b %d, %H:%M} UTC</time>" in html
    # The negative half of the rule: no cell on a rendered page ends in a bare
    # `Z`. Asserted on the markup the *server* sent, because that is the text a
    # browser with no `Intl` — or no JavaScript at all — is left holding.
    assert "Z</time>" not in html


def test_age_reads_in_the_biggest_unit_that_fits():
    from datetime import datetime, timedelta

    from app.web.dashboard import _age

    now = datetime(2026, 8, 28, 22, 19, tzinfo=UTC)
    ages = [
        _age(now - timedelta(seconds=5), now),
        _age(now - timedelta(minutes=12), now),
        _age(now - timedelta(hours=3), now),
        _age(now - timedelta(hours=25), now),
        _age(now - timedelta(days=9), now),
    ]
    assert ages == ["just now", "12m", "3h", "1d", "9d"]
    assert _age(None, now) == "—"
    # The database's clock is not this process's clock. A row stamped a moment
    # into the future is "just now", never "-1m".
    assert _age(now + timedelta(seconds=1), now) == "just now"
    assert _age(now + timedelta(hours=2), now) == "just now"


def test_ts_reads_conduits_iso_strings_as_well_as_our_own_datetimes():
    """An earlier round: half the stamps on screen are ours (datetimes off SQLAlchemy),
    half are Conduit's (ISO strings out of JSON). An operator must not be able
    to tell which row came from where."""
    from datetime import datetime

    from app.web import ts

    year = datetime.now(UTC).year
    ours = ts(datetime(year, 8, 28, 22, 19, tzinfo=UTC))
    theirs = ts(f"{year}-08-28T22:19:00.000Z")
    assert "Aug 28, 22:19 UTC" in ours and "Aug 28, 22:19 UTC" in theirs
    assert f'title="{year}-08-28T22:19:00+00:00"' in str(theirs)
    # A naive string is read as UTC, like a naive datetime is.
    assert "Aug 28, 22:19 UTC" in ts(f"{year}-08-28T22:19:00")


def test_ts_prints_the_year_when_it_is_not_this_one():
    """The console never lies about the state of money, and a date is part of
    that state: a lock expiring in 2099 must not read as one expiring today —
    nor may December's rows silently become this year's every January."""
    from datetime import datetime

    from app.web import ts

    year = datetime.now(UTC).year
    assert "Jan 01 2099, 00:10 UTC" in str(ts("2099-01-01T00:10:00.000Z"))
    assert "Dec 31 1999, 23:59 UTC" in str(ts(datetime(1999, 12, 31, 23, 59, tzinfo=UTC)))
    # …and the current year stays compact, so the common row loses nothing.
    assert f"Jan 01 {year}" not in str(ts(f"{year}-01-01T00:10:00.000Z"))


def test_ts_never_takes_a_page_down_over_a_payload_it_cannot_read():
    """Conduit's stamps are strings this console does not control. One in a
    format nobody anticipated is shown verbatim — not swallowed, not raised on,
    because a page of money state is worth more than a tidy column."""
    from app.web import ts

    assert "not-a-timestamp" in str(ts("not-a-timestamp"))
    # Parsing is not the only way this fails: this one parses, then overflows
    # on the shift to UTC. Same fallback, because the promise is about the
    # page staying up, not about where in the function it went wrong.
    assert "0001-01-01T00:00:00+14:00" in str(ts("0001-01-01T00:00:00+14:00"))
    assert "—" in str(ts(""))
    assert "—" in str(ts(None))
    # Escaped on the way out, like every other untrusted value.
    assert "<script>" not in str(ts("<script>alert(1)</script>"))


async def test_the_stat_band_counts_what_the_tables_below_list(session):
    """The tables show the ten newest rows; the band answers the
    question they cannot — how many. Same tables, same predicates."""
    await unresolved(session, body={"n": 1})
    await unresolved(session, body={"n": 2})
    await observe(session, "applications", "app_1", "pending")
    await observe(session, "applications", "app_done", "approved")  # terminal: not counted
    await observe(session, "transactions", "txn_1", "processing")
    await draft(session)

    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    # Unresolved operations is the one cell that may take a colour, and it has
    # rows, so it wears the `warn` amber — "this one is yours".
    assert '<p class="figure hero-num warn">2</p>' in html
    # The other three are plain ink: 1 pending application, 1 draft, 1 observed
    # transaction. The approved application is terminal and is not among them.
    assert html.count('<p class="figure hero-num">1</p>') == 3


async def test_each_stat_tile_lands_on_exactly_the_rows_it_counted(session):
    """A tile is a link only where the destination
    shows the *same population* the number came from — the honesty rule applied
    to navigation, because a tile that lands on a different set gives the
    operator two numbers and a reason to believe both.

    Three of the four have one; the fourth does not and therefore does not
    navigate. `app/web/dashboard.py` carries the check for each.
    """
    await unresolved(session)
    await observe(session, "applications", "app_1", "pending")
    await draft(session)

    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    # 1: no operations index exists, so the tile jumps to the section on this
    # page whose query IS `state IN ACTIVE_STATES`.
    assert '<a href="#needs-attention">' in html and 'id="needs-attention"' in html
    # 2: the local non-terminal projections — NOT `/applications?status=…`,
    # which reads Conduit live and cannot express a NULL or unknown state.
    assert '<a href="#pending-applications">' in html and 'id="pending-applications"' in html
    assert "/applications?status=pending" not in html
    # 3: a real destination, filtered to exactly the tile's own predicate.
    assert '<a href="/drafts?state=open">' in html
    # 4: still no link. The ledger has an all-kinds view now, but it reads
    # Conduit's list where this counts what this console has OBSERVED, and its
    # date filters are whole days — "observed, rolling 24 hours" has no honest
    # landing place.
    band = html.split('<div class="stat-band">')[1].split('<h2 id="needs-attention">')[0]
    assert "Transactions observed" in band
    assert band.split("Transactions observed")[0].count("<a href=") == 3
    assert "<a href" not in band.split("Transactions observed")[1]


async def test_the_editorial_numeral_is_on_the_band_and_in_no_cell(session):
    """Spec §2.3. Half a rule is not a rule.

    "Apply the big-numeral treatment to: the Overview stat band, account
    balance headers, batch totals. **Not** to: any table cell, anywhere." The
    positive half is easy to see and easy to keep; the negative half is the one
    that decays, because adding `hero-num` to an amount in a ledger row would
    look like emphasis to whoever did it and would ruin the page it is on. So
    both halves are asserted here, on the page that has four of these figures
    and four tables under them.
    """
    await unresolved(session)
    await observe(session, "applications", "app_1", "pending")
    await observe(session, "transactions", "txn_1", "processing")
    await draft(session)

    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    # Four figures, all four carrying the class.
    assert len(hero_numerals(html)) == 4
    assert all("figure" in tag for tag in hero_numerals(html))
    # And not one of the four tables' cells.
    assert cells_with_hero(html) == []


async def test_a_quiet_console_flags_nothing(session):
    """A zero is not a state to flag: the colour means a human is owed
    something, and nothing waiting is not that."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    assert html.count('<p class="figure hero-num">0</p>') == 4
    assert "figure warn" not in html


# --- chrome --------------------------------------------------------------------------


async def test_the_skip_link_is_the_first_thing_in_the_body():
    """Restored 2026-09-03 (spec §4.4), reversing the
    2026-09-02 removal — and the absence pin it reverses is replaced by a
    presence pin, not deleted, so the reversal is as deliberate as the removal.

    The removal's premise was "mouse-first payment operators, a handful of ours".
    The audience is now another organization's treasury staff, and the procurement
    argument is the strong one: B2B financial software answers accessibility
    questionnaires, and this was a documented gap sitting behind ~18 links on
    every page.

    FIRST in the body is the whole mechanism — a skip link that is the second tab
    stop skips nothing — and its target has to be focusable, which is what
    `tabindex="-1"` on `<main>` is for. The `.shell` opens *after* it: the link is
    pinned to the viewport, not to the card.
    """
    app = make_app(nothing())
    async with signed_in(app) as web:
        body = (await web.get("/")).text

    head, _, rest = body.partition('<a class="skip" href="#content">Skip to content</a>')
    assert rest, "the skip link is not in the page"
    # Nothing focusable before it: everything above is <head>, the <body> tag and
    # a Jinja comment's worth of nothing.
    assert "<a " not in head and "<button" not in head and "<input" not in head
    assert '<main id="content" tabindex="-1">' in body
    # It is outside the shell, which opens after it.
    assert rest.index('<div class="shell">') > 0


# The badge's text, per environment. Three pins in one because the point is the
# CONTRAST between them: the word changes, the hostname never appears, and the
# ceiling follows the setting rather than the environment.
@pytest.mark.parametrize(
    "env, ceiling, expected",
    [
        ("sandbox", "", "Sandbox"),
        ("sandbox", "5000", "Sandbox · ceiling 5000"),
        ("staging", "", "Staging"),
        ("staging", "5000", "Staging · ceiling 5000"),
        ("production", "", "Production"),
        # Arca §4.1 writes production's badge as `Production`, with no ceiling.
        # That is true of the DEFAULT — the ceiling is normally unset there — and
        # would be a lie about the console whenever it is set: a refusal the
        # operator did not know was coming reads as a broken console, in
        # production most of all. The environment decides the word; MONEY_CEILING
        # decides the ceiling.
        ("production", "5000", "Production · ceiling 5000"),
    ],
)
async def test_the_env_badge_names_the_environment_and_never_the_host(env, ceiling, expected):
    """`sandbox · api.sandbox.conduit.financial · ceiling 5000` loses its middle
    third (Arca §4.1). The environment and the ceiling are the money facts an
    operator must never be uncertain about; the hostname is this installation's
    infrastructure, and printing a vendor host on every page is one of the things
    that made this read as internal tooling."""
    app = make_app(nothing())
    with settings_override(conduit_env=env, money_ceiling=ceiling):
        async with signed_in(app) as web:
            body = (await web.get("/")).text

    badge = re.search(r'<span id="env-badge".*?</span>', body, re.S).group(0)
    assert badge.split(">")[-2].removesuffix("</span") == expected
    assert "conduit.financial" not in badge.split('title="')[1].split(">")[1]


async def test_the_host_and_the_auth_mode_move_into_the_badge_title():
    """Neither fact is deleted — both are re-homed to hover.

    `AUTH_MODE=disabled` is deployment debug output on a treasury
    client's screen; the hostname is deployment trivia. Whoever operates the
    install still needs both, and a `title` is where a fact that belongs to one
    reader in a hundred goes. No settings page: two facts do not earn a surface.
    """
    app = make_app(nothing())
    async with signed_in(app) as web:
        body = (await web.get("/")).text

    title = re.search(r'<span id="env-badge"[^>]*title="([^"]*)"', body).group(1)
    assert "api.sandbox.conduit.financial" in title
    assert "AUTH_MODE=disabled" in title


async def test_the_env_strip_keeps_the_actor_and_drops_the_auth_line():
    """Arca §4.2 splits the strip. The display name and roles stay: the
    rule is that a forbidden affordance is not drawn, which only works if you can
    see what you hold. The `auth …` line goes (to the badge's `title`)."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        body = (await web.get("/")).text

    strip = body.split('<div class="env-strip">')[1].split("</div>")[0]
    assert ACTOR in strip
    assert "auth" not in strip
    assert "AUTH_MODE" not in strip


async def test_only_the_theme_blocks_first_paint():
    """The theme has to be stamped on `<html>` before the first box is drawn, and
    CSP (`script-src 'self'`, no `unsafe-inline`) means that can only be a FILE
    in `<head>`. What must not follow from that is ~40 KB of wizard glue in the
    same position — so `theme.js` is its own ~1 KB file and `app.js` keeps the
    place it has always had, at the foot of the body (
    ruling).

    Pinned as an ORDER, because that is the whole mechanism: exactly one script
    before `</head>`, it is the theme, and the other three are after the
    content."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        body = (await web.get("/")).text

    head, _, rest = body.partition("</head>")
    assert re.findall(r'<script src="([^"]+)"', head) == ["/static/theme.js"]
    assert re.findall(r'<script src="([^"]+)"', rest) == [
        "/static/htmx.min.js",
        "/static/conditions.js",
        "/static/app.js",
    ]
    # Blocking, both of them: `defer`/`async`/`type=module` all run after
    # parsing, which the browser is free to paint before.
    assert 'src="/static/theme.js"></script>' in head
    # And the theme is not in app.js any more: one file owns it end to end.
    assert "data-theme" not in (ROOT / "static" / "app.js").read_text()


async def test_the_overview_offers_the_tour_once_and_hidden():
    """The server keeps NO tour state — whether this operator has seen
    it is a browser fact — so the offer is rendered on every load of this page
    and `hidden` on every one of them, and app.js is the only thing that unhides
    it. Rendered once: two offer cards would be two ways to start one tour.

    An offer, not an auto-opened overlay: an operator who landed here mid-
    incident must not have the page taken away from them.
    """
    app = make_app(nothing())
    async with signed_in(app) as web:
        overview = (await web.get("/")).text
        elsewhere = (await web.get("/drafts")).text

    assert overview.count('id="tour-offer"') == 1
    assert '<div class="card" id="tour-offer" hidden>' in overview
    assert overview.count('data-tour="start"') == 1
    assert overview.count('data-tour="dismiss"') == 1
    # The tour runs on its trigger page; no other page carries the offer.
    assert "tour-offer" not in elsewhere


async def test_every_tour_anchor_is_a_stable_id_on_the_page_that_owns_it():
    """The tour anchors to ids, never to copy text — so the ids are the
    contract, and they are asserted where they live: three in the chrome (on
    every page) and one on this page's Needs-attention table. The two steps
    whose subject lives elsewhere have no anchor here by design.
    """
    app = make_app(nothing())
    async with signed_in(app) as web:
        overview = (await web.get("/")).text
        elsewhere = (await web.get("/drafts")).text

    for chrome in ('id="ribbon"', 'id="env-badge"', 'id="ribbon-rfis"', 'id="tour-link"'):
        assert chrome in overview
        assert chrome in elsewhere  # the ribbon is the same on every page
    assert '<table id="attention-table">' in overview
    # The permanent re-trigger: a real link, so it is an affordance whether or
    # not app.js ran, and it points at the Overview because that is where the
    # tour's targets are.
    assert '<a class="item tour" id="tour-link" href="/?tour=1">Take the tour</a>' in elsewhere


def tour_step(anchor: str) -> str:
    """One product-tour step's copy, read out of the file that ships it.

    The tour is JS, so a pin that retyped the sentence in here would only pin
    the test to itself. This reads `static/app.js`: the string literals of the
    step whose `target` is `anchor`, concatenated the way the browser does.
    """
    source = (ROOT / "static" / "app.js").read_text()
    start = source.index("text:", source.index(f'target: "{anchor}"'))
    end = source.index("\n    }", start)
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', source[start:end]))


def test_the_tours_needs_attention_step_tracks_the_tables_real_vocabulary():
    """A drift guard on a tour step, because a tour step is evidence: it tells
    an operator what a colour on this table MEANS, which is a claim about the
    code below and gets the same guard as any other map in this app.

    It exists because the shipped copy got it exactly backwards once: it hung
    "a human — you — has to settle it" on AMBER, whose next-step column says the
    opposite ("Nothing to do, and do not resubmit"), and called RED "a refusal",
    which cannot render on this table at all — the query is `ACTIVE_STATES` and
    every refusal is terminal.

    So nothing here is hardcoded prose. The expected meaning is derived from
    `ACTIVE_STATES`, `PILL_TONES["operations"]` and `NEXT_STEP`, and a state
    joining the table, changing tone, or having its instruction reworded breaks
    this test rather than leaving the tour quietly lying.
    """
    tones = PILL_TONES["operations"]
    warn = {state for state in ACTIVE_STATES if tones[state] == "warn"}
    bad = {state for state in ACTIVE_STATES if tones[state] == "bad"}
    copy = tour_step("attention-table")
    sentences = [s.strip().lower() for s in copy.split(". ")]
    amber = next(s for s in sentences if s.startswith("amber"))
    red = next(s for s in sentences if s.startswith("red"))

    # The step speaks of exactly two colours, one sentence each. A second warn
    # or bad state with its own instruction cannot be covered by one sentence —
    # which is the moment to rewrite the step, not to let it generalise.
    assert len(warn) == 1 and len(bad) == 1, (warn, bad)
    # Every state that can reach this table has a next-step line, which is what
    # makes the step's last-column sentence true.
    assert set(ACTIVE_STATES) <= set(NEXT_STEP)
    assert "the last column says what happens next" in copy.lower()

    # No refusal can render here, so no word for one may appear in the copy.
    # The premise is asserted first: if a terminal state ever joins the table's
    # query, this fails before the vocabulary check does.
    assert not set(ACTIVE_STATES) & set(TERMINAL_STATES)
    for word in ("refus", "reject", "declin", "denied", "turned down"):
        assert word not in copy.lower(), word

    # Amber's instruction is to do nothing yet — asserted against the real
    # sentence, then required of the copy, and required NOT to invite the
    # intervention that belongs to red.
    assert all("do not resubmit" in NEXT_STEP[s].lower() for s in warn)
    assert all("nothing to do" in NEXT_STEP[s].lower() for s in warn)
    assert "nothing to do" in amber and "resubmit" in amber
    assert not any(word in amber for word in ("retry", "retrying", "yours", "settle"))

    # Red's is the opposite, and it carries the safety property that makes the
    # instruction actionable rather than frightening.
    assert all("retrying is safe" in NEXT_STEP[s].lower() for s in bad)
    assert all("idempotency key" in NEXT_STEP[s].lower() for s in bad)
    assert "yours" in red and "retrying is safe" in red
    assert "idempotency key" in red and "cannot pay twice" in red


async def test_the_footer_says_what_this_console_is_not_in_its_own_words():
    """The imported canvas footer carries Conduit Financial's own marketing
    tagline and Conduit's own regulatory disclaimer. Neither is this console's
    to say (DESIGN.md); the README's line is what ships."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        html = (await web.get("/")).text

    # The not-a-bank line was removed (2026-09-02); what this pin protects is
    # that NOBODY ELSE'S words fill the space — the marketing tagline and the
    # third-party disclaimer stay banned.
    assert "Not a bank, a ledger of record" not in html
    assert "Anydollar" not in html
    assert "financial technology company" not in html


async def test_the_ribbon_leads_here_and_marks_it_active():
    app = make_app(nothing())
    async with signed_in(app) as web:
        overview = await web.get("/")
        elsewhere = await web.get("/drafts")

    assert '<a class="brand" href="/">Conduit Console</a>' in overview.text
    assert '<a class="item on" href="/" aria-current="page">Overview</a>' in overview.text
    assert '<a class="item" href="/">Overview</a>' in elsewhere.text
    # The horizontal strip is a left ribbon and the labels
    # are grouped, but **no URL moved** — the Playwright suite and the live e2e
    # navigate these paths, and every route still passes the same `section`.
    for link in ("/applications", "/customers", "/rfis", "/accounts", "/transactions", "/orders", "/drafts"):
        assert f'href="{link}"' in overview.text
    assert '<a class="item on" href="/drafts" aria-current="page">Onboard</a>' in elsewhere.text
    # Group heads name their subs and never navigate: the group carries the
    # active class, the sub carries the link.
    assert '<p class="group-head" id="grp-customers">Customers</p>' in overview.text
    assert '<a class="item" href="/customers">Directory</a>' in overview.text
    # The three ribbon `.group` divs had no accessible
    # name — a screen reader announced "group" three times with nothing to
    # tell them apart. `role="group"` plus `aria-labelledby` pointing at the
    # `.group-head`'s own id (the same binding `err_attrs`/`{name}-error` uses
    # for a field, and `#rfi-scope` uses for its segmented toggle) fixes that.
    for key in ("customers", "transactions", "transact"):
        assert f'role="group" aria-labelledby="grp-{key}"' in overview.text
        assert f'id="grp-{key}"' in overview.text


async def test_the_read_pages_render_over_rows_they_cannot_decrypt(session):
    """The load-bearing one, and the other half of "renders when things are
    wrong": these pages must not need the encryption key.

    A draft's `payload` and an operation's `request_body` are `EncryptedJSON`,
    so loading either *entity* decrypts it — and rendering a country, a date
    and an operation type needs none of that. One corrupt ciphertext, or a key
    rotated away from under the rows, would otherwise 500 both the page an
    operator opens *because* something is broken and the list they check to see
    what survived. Both select columns, so the answer is: they render, and the
    row whose answers cannot be read is still there by country and reference.
    """
    from sqlalchemy import text

    row = await draft(session, country="BG")
    await unresolved(session, customer_id="cus_x")
    # Straight past the TypeDecorator: bytes that are not a Fernet token at all.
    await session.execute(
        text("update drafts set payload = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": row.id},
    )
    await session.execute(text("update operations set request_body = :junk"), {"junk": b"junk"})
    await session.commit()

    app = make_app(nothing())
    # The harness's own warm-up GET is `/drafts`, so signing in at all is
    # already half the assertion.
    async with signed_in(app) as web:
        pages = {path: await web.get(path) for path in ("/", "/drafts")}

    for path, response in pages.items():
        assert response.status_code == 200, path
        assert "BG" in response.text, path
        assert f'href="/onboarding/{row.id}"' in response.text, path
    assert "cus_x" in pages["/"].text

    # And the guard is real: the entity load these queries avoid does raise.
    with pytest.raises(Exception):
        await session.get(Draft, row.id, populate_existing=True)


async def test_the_dashboard_renders_fully_with_zero_conduit_answers(session):
    """**The contract, rewritten deliberately** — exactly the `/accounts`
    amendment, one file over (`tests/test_web_accounts.py`).

    It used to be "zero Conduit *calls*", asserted on the transport log. What
    that contract was protecting is **availability**, not asceticism: the page an
    operator lands on when Conduit looks broken must render when Conduit is the
    thing that is broken. The page now makes the console's one bounded, gathered,
    silent-on-failure customers read for the names three of its panels show —
    the rule that operators do not read ids — and availability
    survives it because the resolver's honest-miss clause IS the degraded mode:
    no answer, no names, no banner, every row still on screen with its id.

    So the claim is the one the page actually needs: **it renders completely
    with zero Conduit ANSWERS**, and the names are the bonus for when Conduit is
    up. The arithmetic is pinned with it — read budget 0 → 1: exactly one
    request leaves, `GET /v2/customers?limit=25`, whatever is in the four tables
    and however many rows they hold. Never one per row.

    The hang risk the old contract also guarded (a black-holed host consumes the
    client's whole retry budget before it fails) is answered the way every other
    list here answers it, and no worse: one bounded read whose failure this page
    ignores.
    """
    calls: list = []
    await unresolved(session, customer_id="cus_x")
    await observe(session, "applications", "app_1", "pending")
    # The ribbon's open-RFI badge renders on THIS page too, and it is
    # the newest thing that could have quietly put a Conduit read into the
    # chrome. An open RFI exists so the badge is actually on screen while the
    # transport log is asserted.
    await observe(session, "rfis", "rfi_1", "open")
    await draft(session)

    # Nothing is stubbed: every Conduit read this render makes is refused.
    app = make_app(stub({}, calls))
    async with signed_in(app) as web:
        response = await web.get("/")

    assert response.status_code == 200
    assert 'href="/rfis">RFIs<span class="count"' in response.text
    # Every panel is there, and the customer column carries the bare id — the
    # honest miss, not a placeholder and not a blank.
    assert "payout_create" in response.text and "<code>cus_x</code>" in response.text
    assert 'href="/applications/app_1"' in response.text
    # No banner about an outage this page survived (the resolver's silence rule).
    assert "could not be read" not in response.text
    # And exactly ONE request left: the bounded customers page. The local
    # SELECTs are not on this log.
    assert [(method, path) for method, path, _ in calls] == [("GET", "/v2/customers")]


async def test_the_dashboard_names_its_customers_or_shows_the_bare_id(session):
    """Names when the read answers, ids when it fails — the pair.

    The directive ("operators need names") applied to the Overview's
    three id-bearing panels, on the /accounts precedent: an operations row and
    an application row carry no name of their own and take the map; a
    transaction observed through a read carries `customerName` and that is the
    row's OWN answer, which beats the map (`m.customer_cell`'s contract). When
    the directory does not answer, all three fall back to the id alone.
    """
    from tests.payments_fixtures import page as customers_page

    await unresolved(session, customer_id="cus_x")
    await projections.apply_observation(
        session,
        resource_kind="applications",
        resource_id="app_1",
        # An application payload carries the id and never a name — the map's job.
        observed={"status": "pending", "customerId": "cus_x"},
    )
    await projections.apply_observation(
        session,
        resource_kind="transactions",
        resource_id="txn_1",
        observed={"status": "processing", "customerId": "cus_x", "customerName": "ZZZTEST Read"},
    )
    named = {"id": "cus_x", "legalName": "ZZZTEST Mapped SA", "type": "business"}

    app = make_app(stub({("GET", "/v2/customers"): customers_page([named])}))
    async with signed_in(app) as web:
        resolved = (await web.get("/")).text
    app = make_app(stub({}))  # the directory refuses
    async with signed_in(app) as web:
        missed = (await web.get("/")).text

    # The map names the operation and the application rows; the id stays under
    # the name in the mono face rather than being replaced by it.
    assert resolved.count('<a href="/customers/cus_x">ZZZTEST Mapped SA</a>') == 2
    assert '<div class="muted"><code>cus_x</code></div>' in resolved
    # The transaction row's own payload wins over the map for that row.
    assert '<a href="/customers/cus_x">ZZZTEST Read</a>' in resolved
    # Read failed: ids alone, no invented name, no banner.
    assert "ZZZTEST Mapped SA" not in missed
    assert missed.count('<a href="/customers/cus_x"><code>cus_x</code></a>') == 2
    assert "could not be read" not in missed


# --- the quick-actions cluster --------------------------------------------


QUICK = re.compile(r'<nav id="quick-actions".*?</nav>', re.S)

# The second row: "Pay a contact" is the same verb entered from
# the destination end, so it shares `payout.create` with "Send a payout" — the
# first pair in this cluster where one permission draws two pills.
PILLS = [
    ("onboarding.edit", '<a class="btn" href="/onboarding">Onboard</a>'),
    ("payout.create", '<a class="btn" href="/payouts/contact">Pay a contact</a>'),
    ("payout.create", '<a class="btn" href="/payouts">Send a payout</a>'),
    ("transfer.create", '<a class="btn" href="/transfers/new">Transfer</a>'),
]


def cluster(html: str) -> str:
    """The cluster's markup, or "" when it was not drawn at all."""
    found = QUICK.search(html)
    return found.group(0) if found else ""


async def test_a_viewer_gets_no_quick_actions_cluster_at_all():
    """Absent, not empty. A viewer holds none of the three, and an empty
    right-aligned row is a hole where an operator sees buttons — the
    rule is that a forbidden affordance is not drawn, and a container drawn
    around nothing is still the console saying "something belongs here"."""
    app = make_app(nothing())
    async with signed_in(app, groups="readers") as web:
        html = (await web.get("/")).text
    assert cluster(html) == ""
    assert 'id="quick-actions"' not in html


@pytest.mark.parametrize("permission", sorted({gate for gate, _pill in PILLS}))
async def test_one_permission_draws_exactly_the_pills_it_gates(permission):
    """Each row of the gating matrix, one held permission at a time — which only
    a deployment-defined role can produce, since the built-in bundles grant these
    together (`signed_in_as` builds one the way a client's ROLES_FILE would).

    Asserted per PILL rather than per permission: `payout.create`
    draws two, and every pill it does not gate is still absent."""
    from tests.web_harness import signed_in_as

    app = make_app(nothing())
    async with signed_in_as(app, permission) as web:
        drawn = cluster((await web.get("/")).text)
    for gate, pill in PILLS:
        assert (pill in drawn) is (gate == permission), pill


async def test_all_four_render_in_the_intended_order_on_every_page():
    """Onboard → pay a contact → payout → transfer, and the same on a page that
    is not the Overview: this is chrome, so "every page" is the claim.

    The order is the and it is not alphabetical or historical: "Pay a
    contact" sits between Onboard and Send a payout because it is the shortest
    of the two payout doors, and it was asked for there."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        overview = cluster((await web.get("/")).text)
        elsewhere = cluster((await web.get("/drafts")).text)

    for drawn in (overview, elsewhere):
        positions = [drawn.index(pill) for _permission, pill in PILLS]
        assert positions == sorted(positions), drawn
        assert len(positions) == 4
    # Convert is still deliberately absent — it keeps its ribbon entry, and the
    # cluster grew by the one pill named, not by every verb there is.
    assert "/convert" not in overview
    # No active state: the pill for the page you are on still renders, because
    # an operator who just sent a payout may want a second one.
    async with signed_in(app) as web:
        on_the_payout_page = cluster((await web.get("/payouts")).text)
    assert PILLS[2][1] in on_the_payout_page
    assert "aria-current" not in on_the_payout_page


async def test_the_onboard_pill_lands_where_the_drafts_page_sends_you():
    """Drift pin. Two affordances lead to onboarding — this cluster and the
    Onboard page's own header button — and they must never diverge onto two
    different ideas of where onboarding starts, nor onto two names for it.
    Neither is a copy of the `hx-post="/onboarding"` create-draft form: both are
    plain links to the GET that HOSTS that form, so there is one truth for
    "start onboarding" and the POST has exactly one author.

    /drafts is also the ONE page that keeps a page-head door beside the chrome's
    (design pass 2026-09-02) — this page is the verb, so the darkest thing on it
    is the thing you press. The other three heads that carried this pill
    (Overview, Customers, Applications) gave it up.
    """
    app = make_app(nothing())
    async with signed_in(app) as web:
        drafts_page = (await web.get("/drafts")).text
    assert '<a class="btn primary" href="/onboarding">Onboard</a>' in drafts_page
    # Same target, same words, different weight (the page's own action is the
    # primary; chrome is never primary — DESIGN.md's one-forward-action rule).
    assert '<a class="btn" href="/onboarding">Onboard</a>' in cluster(drafts_page)
    start = (ROOT / "app" / "web" / "templates" / "onboarding" / "start.html").read_text()
    assert 'hx-post="/onboarding"' in start
    assert 'hx-post' not in cluster(drafts_page)


@pytest.mark.parametrize(
    "held",
    [
        set(),                                             # viewer only
        {"onboarding.edit"},
        {"payout.create"},
        {"transfer.create"},
        {"onboarding.edit", "payout.create", "transfer.create"},
    ],
)
async def test_the_chrome_paints_nothing_the_actor_cannot_open(held):
    """`forbidden_affordances` against the app's real routing table.

    Stated plainly, because it is weaker than it looks: all four targets are
    `console.view` GETs (`/onboarding`, `/payouts/contact`, `/payouts`,
    `/transfers/new` — the forms ask "who" as their first field and the picker
    only picks; all four gate on the POST), so this returns [] for
    every row above whether or not the cluster is gated at all. It cannot pin
    the gating — the parametrized render tests above are what does that. What it
    DOES pin is the other half, and the half that would actually break: that
    nobody ever re-points one of these pills at a route the actor cannot open.
    """
    from tests.web_harness import signed_in_as

    app = make_app(nothing())
    async with signed_in_as(app, *held) as web:
        html = (await web.get("/")).text
    assert forbidden_affordances(app, html, {"console.view", *held}) == []


# --- one filter grammar for every list ------------------------------------------------
#
# 2026-09-02: "when I select the customer, I need to click Filter
# for the contacts to appear — auto-filter once I select the customer." A console
# where six lists apply on `change` and two wait for a click has no grammar at
# all, so the rule is pinned across every list that has a filter tray rather than
# on the page that prompted it. Source-level because the attributes are literals
# in the template with no branch above them: what can drift is a page being
# rewritten without them, and that is exactly what this reads for.
FILTERED_LISTS = {
    "contacts/index": "/contacts",
    "customers/list": "/customers",
    "transactions/list": "/transactions",
    "accounts/list": "/accounts",
    "applications/list": "/applications",
    "orders/list": "/orders",
    "rfis/list": "/rfis",
    "drafts": "/drafts",
}
# `/batches` has no filter tray of its own (it is reached per customer), and the
# Transact route rows are not filters — they already carry this exact triple for
# their own reason, which is where the grammar came from.
TRAY = re.compile(r'<form method="get" action="(?P<path>[^"]+)" class="bar row"(?P<attrs>[^>]*)>')


@pytest.mark.parametrize(("template", "path"), sorted(FILTERED_LISTS.items()))
def test_every_list_filter_applies_on_change(template, path):
    source = (ROOT / "app/web/templates" / f"{template}.html").read_text()
    trays = [m for m in TRAY.finditer(source) if m.group("path") == path]
    assert len(trays) == 1, f"{template}: expected one filter tray for {path}"
    attrs = trays[0].group("attrs")
    for required in (
        f'hx-get="{path}"',
        'hx-trigger="change"',
        'hx-target="main"',
        'hx-select="main"',
        'hx-swap="outerHTML"',
        # The filtered URL stays linkable — the pager, the CSV link and a
        # bookmark all name what is on screen.
        'hx-push-url="true"',
        # A fast second pick wins over a slow first one.
        'hx-sync="this:replace"',
    ):
        assert required in attrs, f"{template}: missing {required}"
    # The no-JS path is the same page: `hx-trigger` replaces the form's default
    # `submit` trigger, so the button has to stay an ordinary GET submit.
    assert 'method="get"' in trays[0].group(0)
    assert 'type="submit"' in source
