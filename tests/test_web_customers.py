"""Customers list/detail and the bank-account request flow (plan v2 §7).

The EUR arm is the interesting one: discovery's `/asset/code` advertises EUR
while the provider check refuses it with `422 NO_ELIGIBLE_PROVIDER` — verified
live on this org, fixture `problem_detail_422_no_eligible_provider.json`. The
console has to show Conduit's own `resolution` text and keep the picker usable,
which is the gate's second arm.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote_plus
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import documents, operations
from app.models import AuditEvent, Operation
from tests.web_harness import (
    PNG,
    cells_with_hero,
    documents_stub,
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

FIXTURES = Path(__file__).parent / "fixtures"
REQUIREMENTS = json.loads((FIXTURES / "feature_requirements_virtual_account_usd.json").read_text())
NO_PROVIDER = json.loads((FIXTURES / "problem_detail_422_no_eligible_provider.json").read_text())
USD_ACCOUNT = json.loads((FIXTURES / "virtual_account_usd.json").read_text())

CID = "cus_034Abbx1XrOVaY6sXUBtGT"
REQUIREMENTS_PATH = f"/v2/customers/{CID}/features/requirements"
FEATURES_PATH = f"/v2/customers/{CID}/features"

CUSTOMER = {
    "id": CID,
    "type": "business",
    "applicationId": "app_1",
    "legalName": "ZZZTEST Console E2E EOOD",
    "taxId": "1234567890",
    "contactEmail": "zzztest@example.com",
    "registeredAddress": {"addressLine1": "1 Test Street", "city": "Sofia", "country": "BG"},
    "features": [
        {"feature": "virtual_account", "isActive": False},
        {"feature": "crypto_wallet", "isActive": False},
    ],
    "createdAt": "2026-08-19T09:30:00.000Z",
    "updatedAt": "2026-08-19T09:30:00.000Z",
}
FUNDED = {
    **CUSTOMER,
    "features": [
        {"feature": "virtual_account", "isActive": True},
        {"feature": "crypto_wallet", "isActive": False},
    ],
}
APPLICATION = {
    "id": "app_feature_1",
    "type": "virtual_account",
    "status": "pending",
    "customerId": CID,
    "asset": {"code": "USD"},
    "createdAt": "2026-08-28T09:00:00.000Z",
    "updatedAt": "2026-08-28T09:00:00.000Z",
}


def page(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "meta": {"total": len(items)}})


def catalog_snapshot(*codes: str) -> dict:
    """The unassetted call's answer: the same requirements shape, with
    `/asset/code` naming every currency this customer may hold."""
    return {
        **REQUIREMENTS,
        "fields": [
            {**f, "allowedValues": list(codes)} if f["pointer"] == "/asset/code" else f
            for f in REQUIREMENTS["fields"]
        ],
    }


def requirements_route(catalog=("USD", "EUR"), **per_asset):
    """`GET …/features/requirements`, both of its two questions.

    **No `?asset=`** is the catalog call: Conduit resolves over every eligible
    provider and its `/asset/code` `allowedValues` is the picker. Each test
    declares its own catalog — a tuple of codes, or a whole `httpx.Response`
    when the catalog call itself is what fails.

    **With `?asset=`** it answers per currency: USD succeeds and EUR 422s,
    exactly as this org behaves.
    """
    responses = {
        "": (
            catalog
            if isinstance(catalog, httpx.Response)
            else httpx.Response(200, json=catalog_snapshot(*catalog))
        ),
        "USD": httpx.Response(200, json=REQUIREMENTS),
        "EUR": httpx.Response(422, json=NO_PROVIDER),
        **per_asset,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.get(
            request.url.params.get("asset", ""),
            httpx.Response(400, json={"type": "VALIDATION_ERROR", "title": "unknown asset"}),
        )

    return handler


def routes(customer: dict = CUSTOMER, extra: dict | None = None) -> dict:
    return {
        ("GET", "/v2/customers"): page([customer]),
        ("GET", f"/v2/customers/{CID}"): httpx.Response(200, json=customer),
        ("GET", REQUIREMENTS_PATH): requirements_route(),
        ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


# What this form's document widget uploads under (`accounts.PURPOSE`, rendered as
# `data-purpose` and sent by `static/app.js`). The submit resolves
# every `documentIds` entry against this console's own upload ledger for exactly
# this purpose, so a test that attaches has to upload first.
DOCUMENT_PURPOSE = "feature_request"


async def attach(web, *doc_ids: str) -> None:
    """Really upload the documents a request is about to name. `documents_stub`
    answers with the filename's stem, so the id is the test's to choose."""
    for doc_id in doc_ids:
        response = await upload(web, purpose=DOCUMENT_PURPOSE, filename=f"{doc_id}.png")
        assert response.status_code == 200, response.text


def submission(asset: str = "USD", **extra) -> bytes:
    return form(
        **{
            "f__asset__code": asset,
            "f__regulatoryHistory__hasUSBankAccount": "true",
            "f__regulatoryHistory__deniedBankAccount": "false",
            "f__regulatoryHistory__hasPoliticallyExposedPersons": "false",
            "f__certification__termsAndConditions": "true",
            **extra,
        }
    )


# --- list ------------------------------------------------------------------------------


async def test_list_renders_a_row_and_its_filters():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get("/customers?type=business")
    assert response.status_code == 200
    assert CID in response.text and "ZZZTEST Console E2E EOOD" in response.text
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1  # one page, never a walk
    assert "clientReferenceId" in response.text


async def test_paging_keeps_every_filter_and_escapes_the_cursor():
    """Same fix transactions got, arriving late here: the links were
    string-concatenated from a raw cursor and carried neither the type nor the
    reference, so page two showed a different query's results. An opaque cursor
    with an `&` in it also built a broken URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [CUSTOMER],
                "meta": {
                    "mode": "cursor",
                    "nextCursor": "cur/sor+with&specials",
                    "previousCursor": "back+cur",
                },
            },
        )

    app = make_app(handler)
    async with signed_in(app) as web:
        html = (await web.get("/customers?type=business&clientReferenceId=zzztest-1")).text

    assert "cur%2Fsor%2Bwith%26specials" in html  # encoded, not concatenated
    assert "type=business" in html and "clientReferenceId=zzztest-1" in html
    assert "direction=backward" in html


async def test_the_missing_name_search_is_stated_rather_than_faked():
    """**Amended by the name search.** `GET /v2/customers` still offers
    only `clientReferenceId` and `type` — no `search`, no name — and the page
    still says so. What changed is what it does about it: the Name box is this
    console's own bounded walk, not a client-side filter over one cursor page
    pretending to be a search, and the copy names which half is whose.

    The original assertion ("read the names below and page through") is gone
    because that instruction is no longer true — there is a search now, and
    telling an operator to page by eye would be the stale half of the lie.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/customers")).text

    # **Re-aimed** (spec §4.5): the paragraph is unchanged
    # and still on the page, one disclosure away instead of above the rows on
    # every visit. Asserted INSIDE the `<details>`, so "moved" and "quietly
    # deleted" cannot both pass — and as exact strings, because "the copy
    # itself is an asset ... it moves, it does not get rewritten".
    disclosure = html.split("<details")[1].split("</details>")[0]
    assert "Conduit lists customers without a name search" in disclosure
    assert "this console's own" in disclosure
    assert "Reference and Type are Conduit's and are sent as filters" in disclosure
    assert 'name="search"' not in html


async def test_the_directory_lede_is_one_sentence_and_carries_no_hero_numeral():
    """Two rules on one page.

    §4.5: the lede is the page's ONE sentence — what the rows are — and the
    paragraph about whose filter is whose is behind the disclosure above.

    §2.3: a directory of customers has no summary figure. Its numbers are the
    ids and the dates in the cells, and those stay at body size in the mono
    face; the editorial numeral is for a summary, and there is none here to be
    the summary OF. The negative half is what is worth pinning — a balance
    column arriving on this page later must not quietly take 44px.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/customers")).text

    lede = html.split('class="muted lede">')[1].split("</p>")[0]
    assert re.sub(r"<[^>]+>", "", lede).count(".") == 1, lede
    assert hero_numerals(html) == []
    assert cells_with_hero(html) == []


async def test_a_failed_list_read_is_a_problem_not_an_empty_page():
    app = make_app(
        stub(
            {
                ("GET", "/v2/customers"): httpx.Response(
                    429,
                    json={
                        "type": "RATE_LIMITED",
                        "title": "Too many requests",
                        "resolution": "Retry shortly.",
                        "correlationId": "corr-c1",
                    },
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get("/customers")
    assert "Conduit is asking this console to slow down" in response.text  # A3
    assert "corr-c1" in response.text
    # Same phase-gate fix as the orders one: this test's *name* was right and its
    # last assertion was the contradiction — a page cannot say "Too many
    # requests" and "there are none" at once. The handler substitutes an empty
    # `Page` on failure, so the empty row has to branch on the problem.
    assert "The list could not be read — see above." in response.text
    assert "No customers on this page." not in response.text
    assert "Couldn't read this list" in response.text


# --- detail: the page head and the action row -------------------------------------------


async def test_the_head_is_the_name_and_the_id_is_demoted_under_it():
    """An operator knows the customer by
    name; `cus_…` is what they copy. So the h1 is the legal name and the id
    moves beneath it as mono — and when the read failed and there is no name to
    show, the h1 falls back to a generic 'Customer' (same idiom as
    accounts/orders detail) with the id still in the `.head-id.num` mono
    treatment, never as the h1's own bare-id display face (a later
    fixes: a `cus_…` id has no spaces to wrap on and overflowed 375px)."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"/customers/{CID}")).text
    async with signed_in(app) as web:
        unread = (await web.get("/customers/cus_missing")).text

    assert '<p class="eyebrow">customer</p>' in html
    assert "<h1>ZZZTEST Console E2E EOOD</h1>" in html
    assert f'<p class="head-id num">{CID}</p>' in html
    assert f"<h1>{CID}</h1>" not in html
    # No name to be had: a generic h1, and the id lives only in the mono line —
    # never inside the h1 itself.
    assert "<h1>Customer</h1>" in unread
    assert '<p class="head-id num">cus_missing</p>' in unread
    assert "<h1>cus_missing</h1>" not in unread


async def test_the_action_row_is_pills_and_every_href_is_unchanged():
    """Same seven destinations, at button weight instead of prose weight. None
    is `primary`: this page has no single forward action."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"/customers/{CID}")).text

    for href, label in (
        (f"/applications?customerId={CID}", "Applications"),
        # One Contacts page now absorbs the two that used to sit here: a saved
        # record and its whitelist twin are capabilities of one
        # contact, not two links.
        (f"/customers/{CID}/contacts", "Contacts"),
        (f"/transactions?type=withdrawal&amp;customerId={CID}", "Payouts"),
        # The fork, not the single-payment form: "Send a payout" from a customer
        # page may mean one or many, and the restructure asks that first.
        (f"/customers/{CID}/payouts", "Send a payout"),
        (f"/customers/{CID}/transfers/new", "Transfer"),
        (f"/customers/{CID}/convert", "Convert"),
    ):
        assert f'<a class="btn" href="{href}">{label}</a>' in html
    assert 'class="btn primary"' not in html


# --- detail: the feature buttons --------------------------------------------------------


async def test_an_inactive_virtual_account_offers_the_request_button():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")
    assert response.status_code == 200
    assert f"/customers/{CID}/request-account" in response.text
    assert "Request bank account" in response.text
    # Not active: the accounts list is not fetched at all.
    assert [c for c in calls if c[1].endswith("/virtual-accounts")] == []


async def test_a_customer_with_no_features_still_offers_the_request_button():
    """The first-account case: Conduit lists no features at all, which is when
    an operator most needs the button (found on production, 2026-09-07)."""
    app = make_app(stub(routes({**CUSTOMER, "features": []})))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")
    assert response.status_code == 200
    assert f"/customers/{CID}/request-account" in response.text
    assert "Request bank account" in response.text


async def test_an_active_virtual_account_shows_the_accounts_inline():
    app = make_app(stub(routes(FUNDED)))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")
    assert USD_ACCOUNT["id"] in response.text
    assert f"/customers/{CID}/accounts/{USD_ACCOUNT['id']}" in response.text
    assert "Active" in response.text


async def test_the_accounts_table_shows_balances_without_a_second_call(caplog):
    """`GET /v2/customers/{id}/virtual-accounts` returns
    `balances[]` on every item — the pinned `VirtualAccountListResponseClass`
    says so and the sandbox confirmed it live (2026-08-29) — so the three
    columns cost no request at all, and the numbers come from the same
    `accounts.balance_rows` the account's own page renders."""
    calls: list = []
    app = make_app(stub(routes(FUNDED), calls))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")

    assert response.status_code == 200
    for column in ("Available", "Pending", "Frozen"):
        assert f'<th class="num">{column}</th>' in response.text
    # The fixture's own numbers, as decimal strings, unrounded and unparsed.
    assert "125000.00" in response.text and "2500.50" in response.text
    # One customer read and one accounts read. Nothing per row.
    assert [c[1] for c in calls if c[1].startswith("/v2/customers")] == [
        f"/v2/customers/{CID}",
        f"/v2/customers/{CID}/virtual-accounts",
    ]


async def test_an_account_with_no_stated_balance_is_a_dash_never_a_zero():
    """The empty-vs-unreadable house rule, on money. An item Conduit sent with
    no `balances` block has not been reported as empty — it has not been
    reported — and printing 0.00 would be the console inventing the reassuring
    half of that."""
    bare = {k: v for k, v in USD_ACCOUNT.items() if k != "balances"}
    app = make_app(
        stub(routes(FUNDED, {("GET", f"/v2/customers/{CID}/virtual-accounts"): page([bare])}))
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")

    assert response.status_code == 200
    assert bare["id"] in response.text  # the row is still there, and still linked
    # Once per bucket: available, pending and frozen are each unstated.
    assert response.text.count('title="Conduit stated no balance for this account"') == 3
    assert ">0.00<" not in response.text  # no invented zero anywhere on the page


async def test_a_partly_stated_balance_dashes_only_the_buckets_it_omits():
    """The same rule one level further in: a balance
    object that states `available` and omits `pending`/`frozen` used to print
    `0` for the two it never mentioned. The stated number renders; the other two
    are em-dashes."""
    partial = {
        **USD_ACCOUNT,
        "balances": [{"available": {"code": "USD", "amount": "10.00"}}],
    }
    app = make_app(
        stub(routes(FUNDED, {("GET", f"/v2/customers/{CID}/virtual-accounts"): page([partial])}))
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")

    assert response.status_code == 200
    assert "10.00" in response.text
    assert response.text.count('title="Conduit stated no amount for this bucket"') == 2
    assert ">0<" not in response.text and ">0.00<" not in response.text


async def test_the_page_survives_an_accounts_read_that_failed_entirely():
    """The balances arrive with the accounts, so "every balance is unreadable"
    is the same event as "the account list is unreadable" — one problem card,
    and the rest of the customer still renders."""
    app = make_app(
        stub(
            routes(
                FUNDED,
                {
                    ("GET", f"/v2/customers/{CID}/virtual-accounts"): httpx.Response(
                        503, json={"type": "UNAVAILABLE", "title": "Conduit is unavailable"}
                    )
                },
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")

    assert response.status_code == 200
    assert "ZZZTEST Console E2E EOOD" in response.text  # identity still on screen
    assert "Conduit refused this: UNAVAILABLE" in response.text  # A3
    assert "Available" not in response.text  # no table of em-dashes pretending to be one


async def test_crypto_wallet_and_unknown_features_render_inert():
    customer = {
        **CUSTOMER,
        "features": [
            {"feature": "crypto_wallet", "isActive": True},
            {"feature": "time_travel", "isActive": True},
        ],
    }
    app = make_app(stub(routes(customer)))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")
    assert response.text.count("not available in this console") == 2
    assert "request-account" not in response.text  # no button for either


async def test_the_directory_head_carries_no_onboarding_pill_and_the_chrome_does():
    """Design pass 2026-09-02, reversing the earlier call for this surface. Its
    own rule was that a list's head pill answers "and what do I do about it",
    "but only where a truthful answer exists" — and about a directory of
    customers Conduit has already approved, starting a *different* onboarding is
    the nearest verb, not this page's own. The chrome's quick-actions cluster
    carries it now, on this page and every other, so nothing was lost.
    """
    app = make_app(stub(routes()))
    async with signed_in(app) as operator:
        operator_html = (await operator.get("/customers")).text
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as viewer:
        viewer_html = (await viewer.get("/customers")).text

    assert '<a class="btn primary" href="/onboarding">' not in operator_html
    assert '<div class="action">' not in operator_html  # the head has no slot at all now
    assert '<a class="btn" href="/onboarding">Onboard</a>' in operator_html
    # …and it is the only door on a populated list: the empty state's sentence
    # (copy, not a control) keeps its own link but is not rendered here.
    assert operator_html.count('href="/onboarding"') == 1
    # A viewer holds neither the chrome pill nor anything else that starts one.
    assert 'href="/onboarding"' not in viewer_html


async def test_a_viewer_is_not_offered_the_request_button():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(f"/customers/{CID}")
    assert "Request bank account" not in response.text
    assert "needs the <code>account.request</code> permission" in response.text


# --- partial roles --------------------------------------------------------------------------
#
# The customer page carries two independent gates: the money-movement launcher
# (`can_any` over the three verbs) and the account request. A deployment role may
# hold either without the other.


async def test_a_role_holding_only_account_request_gets_no_money_movement_row():
    app = make_app(stub(routes()))
    async with signed_in_as(app, "account.request") as web:
        response = await web.get(f"/customers/{CID}")

    assert response.status_code == 200
    # The page's own body: the ribbon deliberately lists every destination in
    # the console for everyone, and each page states its own gate on arrival.
    body = response.text.split("<main", 1)[1]
    assert "Request bank account" in body
    for label in (">Send a payout</a>", ">Transfer</a>", ">Convert</a>"):
        assert label not in body
    # The action row keeps its read-only links, so it is not an empty band.
    assert ">Applications</a>" in body and ">Contacts</a>" in body
    assert forbidden_affordances(app, response.text, {"console.view", "account.request"}) == []


async def test_a_role_holding_only_payout_create_gets_the_launcher_and_the_sentence():
    """One of the three MOVE_MONEY verbs is enough for the launcher — and the
    account-request cell then explains its own, different permission."""
    app = make_app(stub(routes()))
    async with signed_in_as(app, "payout.create") as web:
        response = await web.get(f"/customers/{CID}")

    body = response.text.split("<main", 1)[1]
    assert ">Send a payout</a>" in body
    assert "Request bank account" not in body
    assert "needs the <code>account.request</code> permission" in response.text
    assert forbidden_affordances(app, response.text, {"console.view", "payout.create"}) == []


async def test_an_unreadable_customer_renders_conduit_s_problem():
    app = make_app(
        stub(
            {
                ("GET", f"/v2/customers/{CID}"): httpx.Response(
                    404,
                    json={
                        "type": "RESOURCE_NOT_FOUND",
                        "title": "Customer not found",
                        "correlationId": "corr-c2",
                    },
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}")
    assert "Conduit refused this: RESOURCE_NOT_FOUND" in response.text  # A3
    assert "corr-c2" in response.text


# --- the request form -------------------------------------------------------------------


async def test_the_picker_offers_the_currencies_discovery_names():
    """The picker is one unassetted call's `allowedValues`, verbatim. EUR is on
    it even though EUR 422s when chosen: the catalog says what this customer may
    hold, and a provider refusal is a separate fact, discovered on the click and
    named there rather than quietly shortening the list."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account")

    assert "Account currency" in response.text
    assert "?asset=USD" in response.text and "?asset=EUR" in response.text
    assert "asset=GBP" not in response.text
    # No red card about a currency nobody asked for yet.
    assert "No banking provider can hold this currency" not in response.text
    assert [c[1] for c in calls].count(REQUIREMENTS_PATH) == 1


async def test_the_bare_picker_costs_exactly_one_requirements_call():
    """The efficiency claim of this whole change, guarded: the old flow spent
    one call per hardcoded currency, so it grew with the list. This one is flat,
    and the single call carries no `asset` at all — an empty `asset=` would ask
    a different question and get a 400."""
    queries: list[str] = []
    answer = requirements_route()

    def probe(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query.decode())
        return answer(request)

    app = make_app(stub(routes(extra={("GET", REQUIREMENTS_PATH): probe})))
    async with signed_in(app) as web:
        assert (await web.get(f"/customers/{CID}/request-account")).status_code == 200
    assert queries == ["type=virtual_account"]


async def test_a_currency_named_only_by_discovery_reaches_the_picker():
    """The point of deriving the picker: SGD is a currency this console has
    never had a constant for, and it is offered the moment Conduit names it."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        catalog=("USD", "SGD"),
                        SGD=httpx.Response(200, json=REQUIREMENTS),
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        picker = await web.get(f"/customers/{CID}/request-account")
        chosen = await web.get(f"/customers/{CID}/request-account?asset=SGD")

    assert "?asset=SGD" in picker.text
    assert chosen.status_code == 200 and "Request SGD account" in chosen.text


async def test_one_currency_s_refusal_never_hides_another():
    """What the per-asset probe loop was really protecting, kept: USD 422s here
    and EUR does not, and neither answer is allowed to speak for the other. The
    refused one is still named with Conduit's own title, one click from the full
    refusal, and the other is still reachable from that same page."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        USD=httpx.Response(422, json=NO_PROVIDER),
                        EUR=httpx.Response(200, json=REQUIREMENTS),
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        picker = await web.get(f"/customers/{CID}/request-account")
        refused = await web.get(f"/customers/{CID}/request-account?asset=USD")
        form_page = await web.get(f"/customers/{CID}/request-account?asset=EUR")

    assert "?asset=USD" in picker.text and "?asset=EUR" in picker.text
    assert (
        "USD</a>\n      — No banking provider can hold this currency for this customer"
        in refused.text
    )
    assert "?asset=EUR" in refused.text  # the other currency is still one click away
    assert form_page.status_code == 200
    assert "Request EUR account" in form_page.text


async def test_an_unreadable_catalog_is_that_problem_and_no_picker():
    """The catalog call is the only source of the picker, so when it fails there
    is no picker — and no fallback list, which would be this console inventing
    currencies out of a failure. Same rule `accounts.active()` keeps: an
    unreadable list is never rendered as an empty one."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        catalog=httpx.Response(
                            403,
                            json={
                                "type": "FEATURE_NOT_ENABLED",
                                "title": "Feature not enabled",
                                "correlationId": "corr-c3",
                            },
                        )
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account")
    assert "Discovery named no currency for this customer." in response.text
    assert "Conduit has not enabled this for your organization" in response.text  # A3
    assert "corr-c3" in response.text
    assert "?asset=USD" not in response.text  # never a fallback list


async def test_a_catalog_that_names_no_currency_field_is_a_problem_not_an_empty_picker():
    """A 200 this console failed to understand is not a fact about the customer.
    `allowed_assets` flattens "no `/asset/code` field" into the same `[]` as
    "Conduit named none", and only the second may be rendered as the customer's
    answer — the distinction `withheld` and the two abandoned reasons
    already draw.
    """
    unreadable = {
        **REQUIREMENTS,
        "fields": [f for f in REQUIREMENTS["fields"] if f["pointer"] != "/asset/code"],
    }
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        catalog=httpx.Response(200, json=unreadable)
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account")
    assert "The account currencies could not be read" in response.text
    assert "?asset=USD" not in response.text  # never a fallback list


async def test_a_currency_list_conduit_sent_empty_is_conduit_saying_none():
    """The other direction, and the only one that is silent: `allowedValues` is
    present and empty, so Conduit did answer the question."""
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(catalog=())
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account")
    assert "Discovery named no currency for this customer." in response.text
    assert "The account currencies could not be read" not in response.text
    assert "Conduit is unreachable" not in response.text


async def test_picking_usd_renders_discovery_s_form():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account?asset=USD")
    assert "Existing US Bank Account?" in response.text
    assert "Terms and Conditions" in response.text
    assert 'name="f.asset.code"' in response.text
    assert "Request USD account" in response.text


async def test_eur_renders_the_no_eligible_provider_problem_and_keeps_the_picker():
    """The EUR arm: schema says EUR, the provider check says no."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account?asset=EUR")

    assert response.status_code == 200
    assert "No banking provider can hold this currency for this customer" in response.text
    assert NO_PROVIDER["correlationId"] in response.text
    # A3 REVERSES the line that used to stand here — "Conduit's own resolution
    # text, verbatim enough to act on". That verbatim resolution is the Arca
    # audit's first blocking finding (spec §0.2): developer prose shown to a
    # client's treasury staff about their own currency. The console's own two
    # sentences say the same operational fact, and the vendor's do not appear.
    assert "ask Conduit before trying another currency" in response.text
    assert NO_PROVIDER["resolution"] not in response.text
    assert NO_PROVIDER["detail"] not in response.text
    # And the operator can still pick the other currency without going back.
    assert "asset=USD" in response.text
    assert "Existing US Bank Account?" not in response.text  # no form for a refused asset
    # Exactly two, and never more: the catalog call that built the picker, then
    # this currency's own. The count is the contract, not an incidental.
    assert [c[1] for c in calls].count(REQUIREMENTS_PATH) == 2


async def test_the_upload_widget_appears_when_this_customer_needs_evidence():
    """Requirements are per customer, not per organization: the same call
    answered `minDocuments: 1` for one customer and `0` for another on this org
    (verified live 2026-08-28). The page reads the number it was given."""
    demanding = {
        **REQUIREMENTS,
        "minDocuments": 1,
        "documents": [{"canonicalType": "bank_statement", "title": "Recent bank statement"}],
    }
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        USD=httpx.Response(200, json=demanding)
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account?asset=USD")
        submitted = await post(web, f"/customers/{CID}/request-account", submission())

    assert "Recent bank statement" in response.text
    assert 'data-purpose="feature_request"' in response.text
    # And the floor is enforced server-side, not just displayed.
    assert submitted.status_code == 422
    assert "At least 1 document(s) required." in submitted.text


async def test_documents_ride_along_at_the_top_level(session):
    """SUPERSEDED in its `doc_1`: this test used to hand the form a `doc_1`
    nobody had uploaded and assert it on the wire, which pinned the gap as
    though it were the contract. The id is uploaded for real now — same
    assertion about where it lands on the body, one real document behind it.
    """
    demanding = {**REQUIREMENTS, "minDocuments": 1}
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        USD=httpx.Response(200, json=demanding)
                    ),
                    ("POST", FEATURES_PATH): httpx.Response(202, json=APPLICATION),
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        await attach(web, "doc_1")
        response = await post(
            web, f"/customers/{CID}/request-account", submission(documentIds="doc_1")
        )
    assert response.headers["HX-Redirect"] == "/applications/app_feature_1"
    body = json.loads(next(c for c in calls if c[1] == FEATURES_PATH)[2])
    assert body["documentIds"] == ["doc_1"] and "documentIds" not in body["fields"]


async def test_a_document_the_operator_did_not_upload_here_never_reached_conduit(session):
    """`documentIds` on a feature application is the evidence a reviewer at
    Conduit reads about *this* customer, and it was whatever the browser sent —
    so another customer's bank statement, or a `doc_` id read off the operations
    panel and retyped, satisfied `minDocuments` and went out as proof.

    Another actor's real upload and an id that never existed are both here: they
    fail for the same reason and the operator is told the same thing about both.
    """
    other, _ = await documents.intake(
        session,
        data=PNG,
        filename="their-statement.png",
        purpose=DOCUMENT_PURPOSE,
        actor_id="usr_someone_else",
        actor_email="other@example.com",
    )
    for state, resource in (("in_flight", None), ("confirmed", "doc_theirs")):
        await operations.transition(
            session,
            other.id,
            state,
            actor_id="usr_someone_else",
            actor_email="other@example.com",
            **({"conduit_resource_id": resource} if resource else {}),
        )

    demanding = {**REQUIREMENTS, "minDocuments": 1}
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        USD=httpx.Response(200, json=demanding)
                    ),
                    ("POST", FEATURES_PATH): httpx.Response(202, json=APPLICATION),
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        refused = await post(
            web,
            f"/customers/{CID}/request-account",
            submission(documentIds=["doc_theirs", "doc_guessed_9999"]),
        )

    assert refused.status_code == 422
    assert "Attach only documents you uploaded here for this purpose" in refused.text
    assert "2 of the attachments could not be matched" in refused.text
    # Nothing sent, nothing ledgered — the refusal is in front of
    # `operations.start`, not a rollback after it.
    assert [c for c in calls if c[1] == FEATURES_PATH] == []
    assert (
        await session.execute(select(Operation).where(Operation.type == "feature_request"))
    ).scalars().all() == []
    # …and the other actor's document is still perfectly attachable by them.
    assert await documents.attachable(
        session, ["doc_theirs"], purpose=DOCUMENT_PURPOSE, actor_id="usr_someone_else"
    ) == {"doc_theirs"}


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_schema_version_this_build_cannot_render_is_refused_not_a_500(method):
    """FORM_ENGINE_SPEC §1: never best-effort. The POST guarded this and the GET
    did not, so opening the form against a bumped schema was a 500."""
    bumped = {**REQUIREMENTS, "schemaVersion": "4"}
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        USD=httpx.Response(200, json=bumped)
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = (
            await web.get(f"/customers/{CID}/request-account?asset=USD")
            if method == "get"
            else await post(web, f"/customers/{CID}/request-account", submission())
        )
    assert response.status_code == 502
    assert "Requirements schema not supported" in response.text
    assert "needs an update" in response.text
    assert "Existing US Bank Account?" not in response.text


async def test_the_request_form_survives_htmx_response_handling():
    """htmx 2 drops non-2xx responses unless configured otherwise, and swaps
    whatever it is given into the form itself. Both halves of the contract are
    template-level, so both are asserted here: the error status is on the config
    list, and the form pulls only `<main>` out of the re-rendered page."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/request-account?asset=USD")

    assert 'name="htmx-config"' in response.text
    for code in ("400", "422", "502"):
        assert f'{{"code":"{code}","swap":true}}' in response.text.replace(" ", "")
    assert 'hx-target="main" hx-select="main" hx-swap="outerHTML"' in response.text
    # A double-click is one request, and the button says so while it is in
    # flight (the queueing itself is browser-side; the attributes are the
    # testable contract).
    assert 'hx-sync="this:drop"' in response.text
    assert 'hx-disabled-elt="find button[type=submit]"' in response.text


async def test_a_viewer_sees_the_form_but_cannot_submit():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(f"/customers/{CID}/request-account?asset=USD")
        refused = await post(web, f"/customers/{CID}/request-account", submission())
    assert "needs the <code>account.request</code> permission" in response.text
    assert "<form" not in response.text.split("Account currency")[1]
    assert refused.status_code == 403


# --- submission -------------------------------------------------------------------------


async def test_a_valid_request_becomes_an_operation_and_redirects_to_the_application(session):
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", FEATURES_PATH): httpx.Response(202, json=APPLICATION)}), calls)
    )
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission())

    assert response.headers["HX-Redirect"] == "/applications/app_feature_1"
    sent = next(c for c in calls if c[1] == FEATURES_PATH)
    assert json.loads(sent[2]) == {
        "type": "virtual_account",
        "asset": {"code": "USD"},
        "fields": {
            "regulatoryHistory": {
                "hasUSBankAccount": True,
                "deniedBankAccount": False,
                "hasPoliticallyExposedPersons": False,
            },
            "certification": {"termsAndConditions": True},
        },
    }
    op = (await session.execute(select(Operation))).scalar_one()
    assert op.type == "feature_request" and op.state == "confirmed"
    assert op.customer_id == CID and op.conduit_resource_id == "app_feature_1"
    # Every mutation carries a key, whether or not the endpoint declares one.
    assert op.idempotency_key is not None


async def test_the_schema_is_re_read_for_the_submitted_asset(session):
    """The body is always validated against the schema Conduit will judge it by:
    submitting EUR re-reads discovery for EUR, which refuses it — and nothing is
    sent to `/features`."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission("EUR"))

    assert response.status_code == 502
    assert "No banking provider can hold this currency for this customer" in response.text
    assert [c for c in calls if c[1] == FEATURES_PATH] == []
    assert (await session.execute(select(Operation))).scalars().all() == []
    asked = [c for c in calls if c[1] == REQUIREMENTS_PATH]
    assert len(asked) == 2  # the catalog, then EUR's own schema


@pytest.mark.parametrize("code", ["US", "12X", "USDD", ""])
async def test_a_malformed_currency_is_refused_before_any_call(session, code):
    """Shape is the only thing this console can judge locally, so it is the only
    thing it judges: a code that cannot be a currency at all never costs a
    request."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission(code))
    assert "Pick an account currency" in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if c[1] == REQUIREMENTS_PATH] == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_well_shaped_currency_the_catalog_omits_is_still_probed(session):
    """The other half of the split: GBP is not on this customer's catalog, and
    it is asked anyway. Conduit's own refusal, with the `resolution` text that
    says what to do next, is a better answer than a console-invented "not
    available" — and nothing local could have known GBP was wrong."""
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", REQUIREMENTS_PATH): requirements_route(
                        GBP=httpx.Response(422, json=NO_PROVIDER)
                    )
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission("GBP"))

    assert response.status_code == 502
    assert "No banking provider can hold this currency for this customer" in response.text
    assert [c for c in calls if c[1] == FEATURES_PATH] == []
    assert len([c for c in calls if c[1] == REQUIREMENTS_PATH]) == 2
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_an_incomplete_request_comes_back_as_field_errors(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(
            web,
            f"/customers/{CID}/request-account",
            form(f__asset__code="USD", f__certification__termsAndConditions="true"),
        )
    assert response.status_code == 422
    assert "This field is required." in response.text
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_server_rejection_is_rendered_with_its_resolution(session):
    """`NO_ELIGIBLE_PROVIDER` can also arrive at request time, after discovery
    allowed the currency. It is the operator's answer, not a bug."""
    app = make_app(
        stub(
            routes(
                extra={("POST", FEATURES_PATH): httpx.Response(422, json=NO_PROVIDER)}
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission())

    assert response.status_code == 422
    assert "No banking provider can hold this currency for this customer" in response.text
    assert NO_PROVIDER["correlationId"] in response.text
    op = (await session.execute(select(Operation))).scalar_one()
    assert op.state == "rejected"


async def test_a_double_submit_is_one_operation_and_one_call(session):
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", FEATURES_PATH): httpx.Response(202, json=APPLICATION)}), calls)
    )
    async with signed_in(app) as web:
        first = await post(web, f"/customers/{CID}/request-account", submission())
        second = await post(web, f"/customers/{CID}/request-account", submission())

    assert first.headers["HX-Redirect"] == "/applications/app_feature_1"
    # The first one is terminal, so the guard has released: an intentional
    # identical resubmit is a second, separate application — which is exactly
    # what "request another account" means. What must never happen is two calls
    # from one submission.
    assert len([c for c in calls if c[1] == FEATURES_PATH]) == 2
    assert second.status_code in (200, 204)
    keys = {op.idempotency_key for op in (await session.execute(select(Operation))).scalars()}
    assert len(keys) == 2


async def test_an_ambiguous_answer_goes_to_the_operation_page(session):
    app = make_app(
        stub(routes(extra={("POST", FEATURES_PATH): httpx.Response(503, text="upstream")}))
    )
    async with signed_in(app) as web:
        response = await post(web, f"/customers/{CID}/request-account", submission())
    op = (await session.execute(select(Operation))).scalar_one()
    assert op.state == "outcome_unknown"
    assert response.headers["HX-Redirect"] == f"/operations/{op.id}"


# --- roles and CSRF ----------------------------------------------------------------------


async def test_the_request_needs_account_request():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await post(web, f"/customers/{CID}/request-account", submission())
    assert response.status_code == 403


async def test_the_request_needs_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            f"/customers/{CID}/request-account",
            content=submission(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 403 and "CSRF" in response.text


@pytest.mark.parametrize("groups", ["readers", "ops", "admins"])
async def test_reading_a_customer_needs_only_console_view(groups):
    app = make_app(stub(routes()))
    async with signed_in(app, groups=groups) as web:
        assert (await web.get("/customers")).status_code == 200
        assert (await web.get(f"/customers/{CID}")).status_code == 200


async def test_no_audit_or_operation_is_written_by_a_read(session):
    app = make_app(stub(routes(FUNDED)))
    async with signed_in(app) as web:
        await web.get(f"/customers/{CID}")
        await web.get(f"/customers/{CID}/request-account?asset=USD")
    assert (await session.execute(select(AuditEvent))).scalars().all() == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_the_accounts_list_pages_instead_of_dead_ending():
    """25 accounts was the end of the list: the page fetched one cursor page and
    rendered no way to ask for the next."""
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/virtual-accounts"):
            queries.append(request.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "data": [USD_ACCOUNT],
                    "meta": {
                        "nextCursor": "cur_next",
                        "previousCursor": "cur_prev",
                        "total": 60,
                    },
                },
            )
        return httpx.Response(200, json=FUNDED)

    app = make_app(handler)
    async with signed_in(app) as web:
        first = await web.get(f"/customers/{CID}")
        assert "accounts_cursor=cur_next" in first.text
        assert "accounts_cursor=cur_prev&amp;accounts_direction=backward" in first.text
        await web.get(f"/customers/{CID}?accounts_cursor=cur_next&accounts_direction=forward")

    assert len(queries) == 2  # one page per view, never a walk
    assert "cursor=" not in queries[0]
    assert "cursor=cur_next" in queries[1] and "direction=forward" in queries[1]


async def test_the_htmx_config_meta_is_parseable_json():
    """htmx `JSON.parse`s this attribute; a stray comma or quote silently
    disables the whole config and takes the error re-renders with it."""
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/customers")).text
    raw = html.split('name="htmx-config" content=\'', 1)[1].split("'>", 1)[0]
    handling = json.loads(raw)["responseHandling"]
    swaps = {entry["code"]: entry.get("swap") for entry in handling}
    assert swaps["400"] is swaps["422"] is swaps["502"] is True
    assert swaps["204"] is False
    # The catch-all still comes last, so nothing else swaps by accident.
    assert handling[-1] == {"code": "[45]..", "swap": False, "error": True}


# --- the name search ------------------------------------------


def named(*names: str, cursor: str | None = None) -> httpx.Response:
    body = {
        "data": [
            {**CUSTOMER, "id": f"cus_{n}", "legalName": name} for n, name in enumerate(names)
        ],
        "meta": {"total": len(names), **({"nextCursor": cursor} if cursor else {})},
    }
    return httpx.Response(200, json=body)


def walking(pages: list[httpx.Response]):
    """A cursored list: each request answers the next page."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.query.decode())
        return pages[min(len(seen) - 1, len(pages) - 1)]

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


async def test_a_name_search_matches_case_insensitively_on_either_name():
    walk = walking([named("ACME Holdings BV", "Globex SA", cursor=None)])
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): walk})))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=acme")).text

    assert "ACME Holdings BV" in html and "Globex SA" not in html
    assert "Showing 1 customer" in html


async def test_a_name_search_that_matches_nothing_says_so_rather_than_showing_everything():
    walk = walking([named("ACME Holdings BV")])
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): walk})))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=zzz-nobody")).text

    assert "ACME Holdings BV" not in html
    assert "No customer" in html


async def test_a_name_search_walks_the_cursor_and_composes_with_type():
    walk = walking([named("Alpha Ltd", cursor="C2"), named("Acme Two Ltd")])
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): walk})))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=acme&type=business")).text

    assert "Acme Two Ltd" in html and "Alpha Ltd" not in html
    # The type filter rides the walk — it is Conduit's own, and dropping it would
    # search a different population than the one the operator asked about.
    assert all("type=business" in q for q in walk.seen)
    assert len(walk.seen) == 2


async def test_a_capped_name_search_says_exactly_that():
    from app.web import customers as module

    pages = [named(f"Company {n}", cursor="MORE") for n in range(module.NAME_WALK_PAGES + 2)]
    walk = walking(pages)
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): walk})))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=nothing-here")).text

    assert len(walk.seen) == module.NAME_WALK_PAGES
    assert "searched the first" in html
    assert "refine" in html or "reference id" in html


async def test_an_empty_name_leaves_the_page_exactly_as_it_was():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=")).text

    # One request, cursor-paged as before — no walk.
    assert len([c for c in calls if c[1] == "/v2/customers"]) == 1
    assert "searched the first" not in html
    assert "ZZZTEST Console E2E EOOD" in html or "Showing 1 customer" in html


async def test_a_failed_walk_is_a_problem_not_an_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"type": "SERVER_ERROR", "title": "Upstream failure"})

    app = make_app(stub(routes(extra={("GET", "/v2/customers"): handler})))
    async with signed_in(app) as web:
        html = (await web.get("/customers?name=acme")).text
    assert "Conduit refused this: SERVER_ERROR" in html  # A3
    assert "No customer" not in html
