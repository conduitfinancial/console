"""Virtual-account detail: instructions, balances, deposits, simulate (plan v2 §7).

The deposit-instruction card is what an operator hands to whoever is sending the
money, so the assertions here are about coordinates being present, labelled, and
individually copyable — not about layout.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote_plus, urlsplit
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.models import AuditEvent, Operation
from app.web import accounts as accounts_web
from tests.conftest import settings_override
from tests.web_harness import (
    forbidden_affordances,
    form,
    make_app,
    post,
    signed_in,
    signed_in_as,
    stub,
)

FIXTURES = Path(__file__).parent / "fixtures"
USD = json.loads((FIXTURES / "virtual_account_usd.json").read_text())
EUR = json.loads((FIXTURES / "virtual_account_eur.json").read_text())

CID = "cus_034Abbx1XrOVaY6sXUBtGT"
VID = USD["id"]
ACCOUNT_PATH = f"/v2/customers/{CID}/virtual-accounts/{VID}"
SIMULATE_PATH = f"/v2/sandbox/customers/{CID}/virtual-accounts/{VID}/deposits/simulate"
URL = f"/customers/{CID}/accounts/{VID}"

DEPOSIT = {
    "id": "txn_deposit_1",
    "type": "deposit",
    "status": "completed",
    "customerId": CID,
    "source": {"type": "external_bank", "assetAmount": {"code": "USD", "amount": "1000.00"}},
    "destination": {
        "type": "virtual_account",
        "virtualAccountId": VID,
        "assetAmount": {"code": "USD", "amount": "995.00"},
    },
    "createdAt": "2026-08-27T10:00:00.000Z",
    "completedAt": "2026-08-27T10:05:00.000Z",
}
OTHER_DEPOSIT = {
    **DEPOSIT,
    "id": "txn_deposit_2",
    "destination": {"type": "virtual_account", "virtualAccountId": "vac_someone_else"},
}


def page(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "meta": {"total": len(items)}})


def routes(account: dict = USD, deposits: list[dict] | None = None, extra: dict | None = None):
    return {
        ("GET", f"/v2/customers/{CID}/virtual-accounts/{account['id']}"): httpx.Response(
            200, json=account
        ),
        ("GET", "/v2/transactions"): page(
            deposits if deposits is not None else [DEPOSIT, OTHER_DEPOSIT]
        ),
        **(extra or {}),
    }


async def test_the_account_head_names_the_customer_it_belongs_to():
    """An account DTO names no customer, so the head
    takes the name from the one bounded customers read — gathered with the
    account read, which it does not depend on — and demotes the `vac_…` to the
    mono line. A read that names nobody leaves the noun and the id, and the
    customer link below keeps its id either way: it is the copyable identity."""
    known = {"id": CID, "legalName": "ZZZTEST Console E2E EOOD", "type": "business"}
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): page([known])})))
    async with signed_in(app) as web:
        named = (await web.get(URL)).text
    app = make_app(stub(routes(extra={("GET", "/v2/customers"): page([])})))
    async with signed_in(app) as web:
        anonymous = (await web.get(URL)).text

    assert "<h1>ZZZTEST Console E2E EOOD</h1>" in named
    assert f'<p class="head-id num">{VID}</p>' in named
    assert f"<code>{CID}</code>" in named
    assert "<h1>Virtual account</h1>" in anonymous and f"<code>{CID}</code>" in anonymous


# --- instructions -------------------------------------------------------------------------


async def test_us_domestic_and_swift_render_as_labelled_copy_rows():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(URL)

    assert response.status_code == 200
    assert "US domestic (ACH · Fedwire · RTP)" in response.text
    assert "SWIFT (international)" in response.text
    for value in (
        "9876543210",  # account number
        "101019644",  # routing number, once per rail
        "VAC2XKJF9MQB7VN4HL1PR3W8T",  # payment reference
        "LEADUS44XXX",  # bank BIC
        "CITIUS33XXX",  # correspondent BIC
        "ZZZTEST Console E2E EOOD",  # beneficiary
    ):
        assert value in response.text
    # A multi-line postal address is copied out of a textarea, not an input
    # (whose value sanitizer would strip the newlines).
    live = json.loads((FIXTURES / "virtual_account_live_usd.json").read_text())
    async with signed_in(make_app(stub(routes(live)))) as web:
        page = await web.get(f"/customers/{CID}/accounts/{live['id']}")
    assert "<textarea" in page.text and "Testville" in page.text

    # One copy button per row, each pointing at its own field.
    assert response.text.count('data-copy="#copy-') == response.text.count("<button type=\"button\" data-copy")
    assert response.text.count('data-copy="#copy-') >= 12


async def test_copy_field_ids_are_unique_across_same_type_blocks():
    """Two blocks of one type is a shape the API permits, and ids built from the
    block's type alone collided — every Copy button in the second card would
    have copied the first card's field."""
    twin = {
        **USD,
        "depositInstructions": [
            USD["depositInstructions"][0],
            {**USD["depositInstructions"][0], "accountNumber": "1111111111"},
        ],
    }
    app = make_app(stub(routes(twin)))
    async with signed_in(app) as web:
        response = await web.get(URL)

    ids = re.findall(r'id="(copy-[^"]+)"', response.text)
    targets = re.findall(r'data-copy="#(copy-[^"]+)"', response.text)
    assert len(ids) == len(set(ids)) == len(targets)
    assert set(ids) == set(targets)
    assert "1111111111" in response.text and "9876543210" in response.text


async def test_sepa_renders_iban_bic_and_the_pending_status():
    app = make_app(stub(routes(EUR)))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/accounts/{EUR['id']}")
    assert "SEPA (euro area)" in response.text
    assert "DE89370400440532013000" in response.text and "COBADEFFXXX" in response.text
    assert "Pending activation" in response.text


async def test_an_unknown_account_status_renders_neutrally():
    app = make_app(stub(routes({**USD, "status": "quarantined"})))
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert "Unknown: quarantined" in response.text


async def test_an_account_without_instructions_says_so():
    app = make_app(stub(routes({**USD, "status": "pending_activation", "depositInstructions": []})))
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert "no deposit instructions" in response.text
    assert "they normally arrive when it activates" in response.text


async def test_an_unreadable_account_renders_conduit_s_problem():
    app = make_app(
        stub(
            {
                ("GET", ACCOUNT_PATH): httpx.Response(
                    404,
                    json={
                        "type": "RESOURCE_NOT_FOUND",
                        "title": "Virtual account not found",
                        "resolution": "Check the id.",
                        "correlationId": "corr-a1",
                    },
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(URL)
    # A3: the code, and the correlation id, which is the half support quotes.
    assert "Conduit refused this: RESOURCE_NOT_FOUND" in response.text
    assert "corr-a1" in response.text


# --- balances ------------------------------------------------------------------------------


async def test_balances_render_available_and_pending_in_tabular_figures():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert "125000.00" in response.text and "2500.50" in response.text
    # `.num` is the whole contract here: the mono/tabular-nums rule behind it
    # lives in static/styles.css, which this response no longer carries.
    assert 'class="num"' in response.text


# --- deposit history -------------------------------------------------------------------------


async def test_deposits_are_filtered_to_this_account():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await web.get(URL)

    assert "txn_deposit_1" in response.text
    assert "txn_deposit_2" not in response.text  # another account's deposit
    assert "995.00 USD" in response.text
    sent = next(c for c in calls if c[1] == "/v2/transactions")
    assert sent[0] == "GET"
    assert len([c for c in calls if c[1] == "/v2/transactions"]) == 1  # one page


async def test_the_deposit_query_asks_for_this_customer_s_deposits():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/transactions":
            seen.append(str(request.url.query.decode()))
            return page([DEPOSIT])
        return httpx.Response(200, json=USD)

    app = make_app(handler)
    async with signed_in(app) as web:
        await web.get(URL)
    assert "customerId=" + CID in seen[0] and "type=deposit" in seen[0]


async def test_an_unreadable_deposit_list_is_a_problem_not_an_empty_history():
    app = make_app(
        stub(
            {
                ("GET", ACCOUNT_PATH): httpx.Response(200, json=USD),
                ("GET", "/v2/transactions"): httpx.Response(
                    500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                ),
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert "Conduit refused this: SERVER_ERROR" in response.text  # A3
    assert "9876543210" in response.text  # the instructions still rendered


# --- the sandbox simulator ---------------------------------------------------------------------


async def test_the_simulate_panel_is_hidden_off_sandbox():
    app = make_app(stub(routes()))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await web.get(URL)
    assert "Simulate deposit" not in response.text


async def test_the_simulate_panel_states_the_accounts_own_currency_rather_than_offering_it():
    """A deposit lands in the account's own currency and the route reads that
    back from Conduit at submit time, so the currency is text. The select this
    replaces had one option whose posted value was discarded — a choice the
    operator did not have."""
    app = make_app(stub(routes(EUR, deposits=[])))
    async with signed_in(app) as web:
        response = await web.get(f"/customers/{CID}/accounts/{EUR['id']}")
    panel = response.text.split("Simulate deposit")[0]
    assert "Currency <strong>EUR</strong>" in panel
    assert 'name="code"' not in response.text


async def test_the_simulate_panel_refuses_when_the_accounts_currency_cannot_be_read():
    """The same fail-closed answer `simulate_code` gives the POST, said on the
    page: no currency to denominate the deposit in, so none is offered."""
    unassetted = {k: v for k, v in USD.items() if k != "asset"}
    app = make_app(stub(routes(unassetted, deposits=[])))
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert "This account's currency is unknown, so a deposit cannot be simulated." in response.text


async def test_simulate_is_refused_off_sandbox_even_when_posted(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await post(web, f"{URL}/simulate-deposit", form(amount="10.00", code="USD"))
    assert "sandbox-only" in response.headers["HX-Redirect"]
    assert [c for c in calls if "sandbox" in c[1]] == []


async def test_simulate_sends_the_sandbox_dto_and_audits_it(session):
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("POST", SIMULATE_PATH): httpx.Response(201, json=DEPOSIT)}),
            calls,
        )
    )
    async with signed_in(app) as web:  # tests run with CONDUIT_ENV=sandbox
        response = await post(
            web,
            f"{URL}/simulate-deposit",
            form(amount="1000.00", code="USD", rail="FEDWIRE", outcome="completed"),
        )

    assert "msg=Simulated" in response.headers["HX-Redirect"]
    sent = next(c for c in calls if c[1] == SIMULATE_PATH)
    assert json.loads(sent[2]) == {
        "assetAmount": {"code": "USD", "amount": "1000.00"},
        "outcome": "completed",
        "rail": "FEDWIRE",
    }
    audit = (await session.execute(select(AuditEvent))).scalar_one()
    assert audit.action == "sandbox.simulate_deposit"
    assert audit.detail["virtualAccount"] == VID and audit.detail["ok"] is True
    # Sandbox scaffolding is outside the ledger (see `app.web`).
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_simulate_omits_the_optional_fields_when_unset():
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", SIMULATE_PATH): httpx.Response(201, json=DEPOSIT)}), calls)
    )
    async with signed_in(app) as web:
        await post(web, f"{URL}/simulate-deposit", form(amount="5", code="USD", rail="", outcome=""))
    sent = next(c for c in calls if c[1] == SIMULATE_PATH)
    assert json.loads(sent[2]) == {"assetAmount": {"code": "USD", "amount": "5"}}


@pytest.mark.parametrize(
    "body,expected",
    [
        (form(amount="", code="USD"), "amount"),
        (form(amount="10", code="USD", outcome="exploded"), "outcome"),
        (form(amount="10", code="USD", rail="carrier_pigeon"), "rail"),
    ],
)
async def test_simulate_refuses_a_body_the_sandbox_would_reject(body, expected):
    """Refused locally, with no call to Conduit at all. The currency is not among
    these: it is derived, so there is no posted code left to reject."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/simulate-deposit", body)
    assert expected in unquote_plus(response.headers["HX-Redirect"]).lower()
    assert [c for c in calls if "sandbox" in c[1]] == []


async def test_a_posted_code_that_disagrees_with_the_account_never_reaches_the_wire():
    """The picker offers the account's own currency and nothing else, but a
    picker is not a control: a direct or stale POST names whatever it likes. The
    code on the wire is the account's, read back from Conduit at submit time and
    not from the form, so a posted code cannot be wrong — it is never read.

    One posted value is the whole claim: the route never reads the field, so a
    malformed `12X` takes the same path a well-formed `GBP` does. `GBP` is the
    row kept because a currency that is *valid and wrong* is what a stale form
    actually posts, and a failure here reads as "the account's USD was replaced
    by the form's GBP" rather than as a shape check that let junk through."""
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", SIMULATE_PATH): httpx.Response(201, json=DEPOSIT)}), calls)
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/simulate-deposit", form(amount="10", code="GBP"))
    sent = next(c for c in calls if c[1] == SIMULATE_PATH)
    assert json.loads(sent[2])["assetAmount"] == {"code": "USD", "amount": "10"}
    assert "msg=Simulated+a+10+USD+deposit" in response.headers["HX-Redirect"]


async def test_a_simulation_is_refused_when_the_accounts_own_currency_cannot_be_read():
    """Fail closed. The deposit is denominated in the account's currency; if
    that read fails there is nothing to derive from, and guessing from the form
    is the bypass this route just closed."""
    calls: list = []
    app = make_app(
        stub(
            {
                ("GET", ACCOUNT_PATH): httpx.Response(503, json={"type": "UPSTREAM"}),
                ("POST", SIMULATE_PATH): httpx.Response(201, json=DEPOSIT),
            },
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/simulate-deposit", form(amount="10", code="USD"))
    # `err` rides with an `errsig` companion, so the refusal is no
    # longer the tail of the URL — it is followed by its signature. The message
    # itself is what this test is about, so assert on that.
    redirect = unquote_plus(response.headers["HX-Redirect"])
    assert f"?err={accounts_web.UNKNOWN_CURRENCY}&errsig=" in redirect, redirect
    assert [c for c in calls if c[1] == SIMULATE_PATH] == []


async def test_a_flash_message_is_urlencoded_not_space_replaced():
    """A refusal rides in the query string. `replace(" ", "+")` is not encoding:
    an `&` in it invents a second query parameter, and everything after it
    vanishes from the banner.

    A3 moved WHICH text carries the hostile characters. Conduit's title no
    longer reaches a URL at all; its `resolution` does, on a code this console
    has no sentence for, so that is where the `& % # =` payload lives now — and
    the encoding path is exercised by the same bytes it always was.
    """
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", SIMULATE_PATH): httpx.Response(
                        422,
                        json={
                            "type": "XYZ_REFUSED",
                            "title": "Rejected & held: amount=0 100% refused #1",
                            "resolution": "Refund & retry: amount=0 100% #1",
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/simulate-deposit", form(amount="1", code="USD"))

    location = response.headers["HX-Redirect"]
    # A3: the banner carries this console's sentence for the refusal — for a
    # code it has none for, the code itself, plus Conduit's resolution line.
    # Conduit's own TITLE never reaches a query string (which is also a browser
    # history and a server log).
    assert parse_qs(urlsplit(location).query)["err"] == [
        "Conduit refused this: XYZ_REFUSED — Refund & retry: amount=0 100% #1"
    ]
    assert "Rejected & held" not in unquote_plus(location)
    # And the banner renders the whole sentence back, hostile characters intact.
    async with signed_in(app) as web:
        page = await web.get(location)
    assert "Conduit refused this: XYZ_REFUSED" in page.text
    assert "Refund &amp; retry: amount=0 100% #1" in page.text


async def test_a_failed_simulation_says_what_conduit_said():
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", SIMULATE_PATH): httpx.Response(
                        422, json={"type": "ACCOUNT_NOT_ACTIVE", "title": "Account not active"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/simulate-deposit", form(amount="1", code="USD"))
    assert (
        "err=Conduit+refused+this%3A+ACCOUNT_NOT_ACTIVE"
        in response.headers["HX-Redirect"]
    )


# --- roles and CSRF -------------------------------------------------------------------------


async def test_a_viewer_reads_the_account_but_is_offered_no_simulation():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(URL)
        refused = await post(web, f"{URL}/simulate-deposit", form(amount="1", code="USD"))
    assert response.status_code == 200 and "9876543210" in response.text
    assert "Simulate deposit" not in response.text
    assert refused.status_code == 403


async def test_a_role_holding_only_sandbox_simulate_gets_the_card_and_no_money_movement():
    """The account page's two gates — the money-movement row (`can_any` over the
    three verbs) and the sandbox card — come apart under a deployment role."""
    app = make_app(stub(routes()))
    async with signed_in_as(app, "sandbox.simulate") as web:
        response = await web.get(URL)

    body = response.text.split("<main", 1)[1]
    assert response.status_code == 200 and "Simulate deposit" in body
    for label in (">Send a payout</a>", ">Transfer</a>", ">Convert</a>"):
        assert label not in body
    # The row above still carries its two ledger links, so nothing is orphaned.
    assert ">Deposits ledger</a>" in body and ">Payouts ledger</a>" in body
    assert forbidden_affordances(app, response.text, {"console.view", "sandbox.simulate"}) == []


async def test_simulate_needs_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.post(
            f"{URL}/simulate-deposit",
            content=form(amount="1", code="USD"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 403 and "CSRF" in response.text


# --- the all-customers index (/accounts) -----------------------------------------------------
#
# The load-bearing property is `test_the_index_renders_when_conduit_is_down`:
# there is no global virtual-accounts endpoint to build this page from (the
# pinned spec has only `/customers/{id}/virtual-accounts`), so it is built from
# this installation's own projections and must render when Conduit does not.

OTHER_CID = "cus_034Abbx1XrOVaY6sXUBtGU"


def activated(virtual_account_id: str, customer_id: str = CID, code: str = "USD") -> dict:
    """A `virtual_account.activated` delivery's `data`, as the worker projects it.

    The keys are the ones a live delivery carries — `virtualAccountId`,
    `customerId`, `asset.code`, `activatedAt` — confirmed against
    `GET /v2/webhooks/event-types` on the sandbox (2026-08-29).
    """
    return {
        "virtualAccountId": virtual_account_id,
        "customerId": customer_id,
        "asset": {"code": code},
        "activatedAt": "2026-08-28T22:19:39.125Z",
        "status": "active",
    }


async def observe(session, observed: dict, status: str | None = None):
    from app import projections

    payload = dict(observed)
    if status is not None:
        payload["status"] = status
    await projections.apply_observation(
        session,
        resource_kind="virtual_accounts",
        resource_id=payload["virtualAccountId"],
        observed=payload,
    )


async def test_the_index_lists_every_observed_account(session):
    await observe(session, activated("vac_1"))
    await observe(session, activated("vac_2", OTHER_CID, "EUR"))

    app = make_app(stub({}))
    async with signed_in(app) as web:
        response = await web.get("/accounts")

    assert response.status_code == 200
    # Account id, linked to the customer-scoped detail page Conduit's own read
    # path requires.
    assert f'href="/customers/{CID}/accounts/vac_1"' in response.text
    assert f'href="/customers/{OTHER_CID}/accounts/vac_2"' in response.text
    assert f'href="/customers/{CID}"' in response.text
    assert "USD" in response.text and "EUR" in response.text
    assert '<span class="pill ok">Active</span>' in response.text
    assert "Showing 2 accounts" in response.text
    # The page says what it does not show, rather than leaving a blank column.
    assert "this installation's own records" in response.text


async def test_the_index_filters_round_trip(session):
    await observe(session, activated("vac_usd"))
    await observe(session, activated("vac_eur", OTHER_CID, "EUR"))
    await observe(session, activated("vac_pending"), status="pending_activation")

    app = make_app(stub({}))
    async with signed_in(app) as web:
        by_asset = await web.get("/accounts?asset=EUR")
        by_status = await web.get("/accounts?status=pending_activation")
        bogus = await web.get("/accounts?status=teleported")

    assert "vac_eur" in by_asset.text and "vac_usd" not in by_asset.text
    assert '<option value="EUR" selected>' in by_asset.text  # the choice survives the round trip
    assert "vac_pending" in by_status.text and "vac_usd" not in by_status.text
    # A status outside the ladder shows the unfiltered page rather than an
    # empty one — a typo in a URL is not a state of the world.
    assert "vac_usd" in bogus.text and "vac_eur" in bogus.text


async def test_the_index_customer_filter_reads_live_accounts_for_that_customer(session):
    # Observed under a different status, so a projection answer would be
    # visibly wrong rather than coincidentally right.
    await observe(session, activated("vac_usd"), status="pending_activation")

    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD, EUR])}))
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}")

    assert response.status_code == 200
    assert "read live from Conduit" in response.text
    assert "this installation's own records" not in response.text
    assert "125000.00" in response.text and "2500.50" in response.text  # USD's live balance
    assert f'href="/customers/{CID}/accounts/{USD["id"]}"' in response.text
    assert f'href="/customers/{CID}/accounts/{EUR["id"]}"' in response.text
    assert "Request another account" in response.text


async def test_the_index_live_read_that_fails_is_not_rendered_as_no_accounts(session):
    app = make_app(
        stub(
            {
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): httpx.Response(
                    503, json={"type": "SERVICE_UNAVAILABLE", "title": "upstream"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}")

    assert response.status_code == 200
    assert "Conduit is temporarily unavailable" in response.text
    assert "No virtual accounts for this customer yet" not in response.text


async def test_a_failed_live_read_falls_back_to_the_observed_rows(session):
    """The page's charter is availability: selecting a customer must not be the
    one path that stops rendering when Conduit is down."""
    await observe(session, activated("vac_usd"))

    app = make_app(
        stub(
            {
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): httpx.Response(
                    503, json={"type": "SERVICE_UNAVAILABLE", "title": "upstream"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}")

    # The row is there, said to be observed rather than live, under the banner.
    assert "vac_usd" in response.text
    assert "this installation's own records" in response.text
    assert "read live from Conduit" not in response.text
    assert "Conduit is temporarily unavailable" in response.text


async def test_the_live_read_takes_the_pages_own_size(session):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("limit") or "")
        return page([USD])

    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): handler}))
    async with signed_in(app) as web:
        await web.get(f"/accounts?customerId={CID}&limit=50")

    assert seen == ["50"], "the page-size control has to reach Conduit"


async def test_the_live_page_says_its_export_is_the_observed_records(session):
    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD])}))
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}")

    # The button is back, and named for what the file actually contains — the
    # export reads projections, the table above is Conduit's live answer.
    assert "Export observed records (CSV)" in response.text


async def test_the_index_sends_the_asset_filter_to_conduit_not_to_the_page(session):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("asset") or "")
        return page([EUR])

    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): handler}))
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}&asset=EUR")

    assert seen == ["EUR"]
    assert EUR["id"] in response.text
    # And the select can still render the value it was handed.
    assert '<option value="EUR" selected>' in response.text


async def test_the_index_asset_filter_with_no_match_is_not_no_accounts(session):
    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): page([])}))
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}&asset=EUR")

    assert "No virtual accounts for this customer yet" not in response.text
    assert "No account matches these filters" in response.text
    # Conduit applied this one, so the caveat about later pages would be a lie.
    assert "there may be a match on" not in response.text


async def test_the_index_status_filter_stays_on_the_page_and_says_so(session):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("status") or "")
        return page([USD])

    app = make_app(stub({("GET", f"/v2/customers/{CID}/virtual-accounts"): handler}))
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}&status=pending_activation")

    assert seen == [""], "status is not a Conduit parameter and must not be sent"
    assert "there may be a match on" in response.text
    assert "No virtual accounts for this customer yet" not in response.text


async def test_the_index_paging_links_carry_the_filters(session):
    app = make_app(
        stub(
            {
                ("GET", f"/v2/customers/{CID}/virtual-accounts"): httpx.Response(
                    200,
                    json={"data": [EUR], "meta": {"total": 1, "nextCursor": "cur_next"}},
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"/accounts?customerId={CID}&asset=EUR&status=active")

    next_link = re.search(r'href="([^"]*accounts_cursor=cur_next[^"]*)"', response.text)
    assert next_link, "no Next link rendered"
    href = unquote_plus(next_link.group(1)).replace("&amp;", "&")
    carried = parse_qs(urlsplit(href).query)
    assert carried["customerId"] == [CID]
    assert carried["asset"] == ["EUR"]
    assert carried["status"] == ["active"]


async def test_the_index_asset_options_are_the_assets_on_file(session):
    await observe(session, activated("vac_eur", CID, "EUR"))

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts")).text

    assert '<option value="EUR"' in html
    assert '<option value="USD"' not in html  # never a currency with no row behind it


async def test_the_index_renders_fully_with_zero_conduit_answers(session):
    """**The contract, rewritten deliberately**.

    It used to be "zero Conduit *calls*", asserted on the transport log, and it
    was written that way for a real reason: the page had carried a customer-name
    datalist as a "silent" convenience read, and silent
    on *failure* is not the same as free — a black-holed host does not fail fast,
    it hangs for the client's whole retry budget, which would hang the one page
    whose purpose is being readable during exactly that outage.

    What that contract was protecting is **availability**, not asceticism. The
    page now makes the console's one bounded, gathered, silent-on-failure
    customers read (`with_customer_names`), and availability survives it because
    the resolver's honest-miss clause is the degraded mode: no answer, no names,
    no banner, every row still on screen with its id. So the claim this test
    makes is the claim the page actually needs — **it renders completely with
    zero Conduit ANSWERS** — and the names are the bonus for when Conduit is up.

    The hang risk that killed the first datalist is answered by the client's own
    retry budget rather than by never calling: this read is gathered, so it
    cannot serialise behind anything, and a page that arrives late but complete
    is what every other list in this console already accepts.
    """
    from app import projections

    await observe(session, activated("vac_1"))
    # And the ribbon's open-RFI badge, which renders on every page including
    # this one, is local too — one COUNT over this installation's projections.
    await projections.apply_observation(
        session, resource_kind="rfis", resource_id="rfi_1", observed={"status": "open"}
    )
    calls: list = []

    # Nothing is stubbed: every Conduit read this render makes is refused.
    app = make_app(stub({}, calls))
    async with signed_in(app) as web:
        response = await web.get("/accounts")

    assert response.status_code == 200
    assert "vac_1" in response.text and "Showing 1 account" in response.text
    assert 'href="/rfis">RFIs<span class="count"' in response.text
    # The rows, the filters, the pager and the customer column are all there —
    # the column carries the bare id, which is the honest miss.
    assert f"<code>{CID}</code>" in response.text
    assert "Rows" in response.text and ">25</a>" in response.text
    # No banner about an outage this page survived (the resolver's silence rule).
    assert "could not be read" not in response.text
    # And exactly ONE read was attempted: the bounded customers page, never one
    # per row. The local SELECTs are not on this log.
    assert [(method, path) for method, path, _ in calls] == [("GET", "/v2/customers")]


async def test_the_index_names_its_customers_or_shows_the_bare_id(session):
    """Names over ids, and the miss rendered honestly — the pair.

    The report: "a list with only IDs and no customer name... a human
    operator does not understand IDs". The name comes from the one bounded read;
    a customer beyond it, or a directory that could not be read at all, leaves
    the `cus_…` alone rather than a placeholder or a guess."""
    await observe(session, activated("vac_1"))
    known = {"id": CID, "legalName": "ZZZTEST Console E2E EOOD", "type": "business"}

    app = make_app(stub({("GET", "/v2/customers"): page([known])}))
    async with signed_in(app) as web:
        named = (await web.get("/accounts")).text
    app = make_app(stub({("GET", "/v2/customers"): page([])}))
    async with signed_in(app) as web:
        anonymous = (await web.get("/accounts")).text

    # Name prominent and linked, id demoted to the mono line (`m.customer_cell`).
    assert f'<a href="/customers/{CID}">ZZZTEST Console E2E EOOD</a>' in named
    assert f'<div class="muted"><code>{CID}</code></div>' in named
    # The miss: the id, linked, and nothing invented beside it.
    assert f'<a href="/customers/{CID}"><code>{CID}</code></a>' in anonymous
    assert "ZZZTEST" not in anonymous


async def test_the_index_customer_filter_suggests_names_and_sends_ids(session):
    """Name search, the house way: the datalist turns a
    typed name into the id the filter actually sends. The same bounded read
    fills it and the names column — one read, two jobs."""
    await observe(session, activated("vac_1"))
    known = {"id": CID, "legalName": "ZZZTEST Console E2E EOOD", "type": "business"}

    app = make_app(stub({("GET", "/v2/customers"): page([known])}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts")).text

    assert 'list="known-customers"' in html
    assert f'<option value="{CID}">ZZZTEST Console E2E EOOD</option>' in html
    assert 'placeholder="name or cus_…"' in html


async def test_the_index_says_when_the_suggestions_are_the_first_page(session):
    """The first-N honesty the other five pickers state: a completion list that
    quietly stopped at 25 would let an operator conclude a customer does not
    exist."""
    await observe(session, activated("vac_1"))
    truncated = httpx.Response(
        200, json={"data": [{"id": CID, "legalName": "A"}], "meta": {"nextCursor": "more"}}
    )

    app = make_app(stub({("GET", "/v2/customers"): truncated}))
    async with signed_in(app) as web:
        capped = (await web.get("/accounts")).text
    app = make_app(stub({("GET", "/v2/customers"): page([{"id": CID, "legalName": "A"}])}))
    async with signed_in(app) as web:
        whole = (await web.get("/accounts")).text

    assert "first 25" in capped and 'href="/customers"' in capped
    assert "first 25" not in whole


async def test_the_index_pages_its_rows_and_carries_the_filters_across(session):
    """The cap becomes a pager. It was 100 rows with the cap
    stated — honest, but an operator with more than a screen of accounts had no
    way to reach row 101 except by narrowing filters until it appeared."""
    for n in range(26):
        await observe(session, activated(f"vac_{n:02d}"))
    await observe(session, activated("vac_eur", OTHER_CID, "EUR"))

    app = make_app(stub({}))
    async with signed_in(app) as web:
        first = (await web.get("/accounts?asset=USD")).text
        second = (await web.get("/accounts?asset=USD&limit=25&offset=25")).text
        wide = (await web.get("/accounts?asset=USD&limit=50")).text

    # 25 of the 26 USD rows, then the 26th — and the EUR row is on neither page,
    # because the filter travels with the page turn.
    assert first.count("<code>vac_") == 25
    assert "vac_eur" not in first and "vac_eur" not in second
    assert second.count("<code>vac_") == 1
    # The Next link is built server-side with the filter on it.
    assert "/accounts?asset=USD&amp;limit=25&amp;offset=25" in first
    assert "Previous" in second
    # The three house page sizes, and the chosen one marked.
    assert '<a class="btn selected" href="/accounts?asset=USD&amp;limit=25">25</a>' in first
    assert wide.count("<code>vac_") == 26 and "Next" not in wide
    # The old silent-truncation sentence is gone: there is a page to turn to now.
    assert "narrow with the filters above" not in first


async def test_the_index_export_still_exports_the_whole_filtered_set(session):
    """The export is the view's filters and everything behind them — never the
    screen's page. `export_url` drops paging parameters; this pins that the
    accounts link does too, now that the page has some."""
    await observe(session, activated("vac_1"))

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts?asset=USD&limit=50&offset=50")).text

    assert 'href="/export/accounts.csv?asset=USD"' in html


async def test_an_account_with_no_customer_on_the_record_is_unlinked(session):
    """The detail page is customer-scoped because Conduit's read path is. An
    observation that never named a customer therefore has no URL to build, and
    the row says so instead of linking to a 404."""
    await observe(session, {"virtualAccountId": "vac_orphan", "status": "active"})

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts")).text

    assert "vac_orphan" in html
    assert "accounts/vac_orphan" not in html
    assert "no customer on this record" in html
    assert ">—</span>" in html  # the asset it never stated, as an em-dash


async def test_an_empty_index_says_what_fills_it(session):
    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts")).text

    assert "No account has been observed yet" in html
    assert 'href="/customers"' in html
    assert "No rows" not in html


async def test_a_filtered_empty_index_does_not_claim_there_are_none(session):
    await observe(session, activated("vac_1"))

    app = make_app(stub({}))
    async with signed_in(app) as web:
        html = (await web.get("/accounts?asset=JPY")).text

    assert "No observed account matches these filters" in html
    assert "No account has been observed yet" not in html


async def test_a_viewer_can_read_the_index(session):
    await observe(session, activated("vac_1"))

    app = make_app(stub({}))
    async with signed_in(app, groups="readers") as web:
        response = await web.get("/accounts")

    assert response.status_code == 200 and "vac_1" in response.text


async def test_the_index_head_carries_no_action_pill_by_design(session):
    """The other deliberate emptiness. An account is requested from one
    customer's own page, so the only pill this list could wear is a link to the
    Directory — which is already two entries up in the ribbon. Asserted for an
    operator, who would see a pill if one existed."""
    await observe(session, activated(VID))

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get("/accounts")).text

    assert "<h1>Virtual accounts</h1>" in html
    assert '<div class="action">' not in html


async def test_the_index_pins_the_nav_and_the_detail_page_does_not(session):
    """The global list is its own section; the customer-scoped detail page stays
    under Customers, where it is reached from and what it is about."""
    await observe(session, activated(VID))

    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        index = await web.get("/accounts")
        detail = await web.get(URL)

    assert '<a class="item on" href="/accounts" aria-current="page">Accounts</a>' in index.text
    assert '<a class="item on" href="/customers"' not in index.text
    assert '<a class="item on" href="/customers" aria-current="page">Directory</a>' in detail.text
    assert '<a class="item on" href="/accounts"' not in detail.text
    # Both are subs of the same ribbon group, so the group head is lit either
    # way — the sub is what says which of the two pages you are on.
    for text in (index.text, detail.text):
        assert (
            '<div class="group on"\n       role="group" aria-labelledby="grp-customers">'
            '\n    <p class="group-head" id="grp-customers">Customers</p>'
        ) in text
