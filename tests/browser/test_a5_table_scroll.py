"""Browser pins for the shared `.table-scroll` wrapper.

Two things a template diff cannot prove on its own: that wrapping every list
table in `overflow-x: auto` actually removes the page-level horizontal
overflow the wrapper exists for (§6's "no horizontal overflow at 375 / 768 /
1024 / 1440"), and that the sticky table head keeps working above the width
the wrapper activates at and gives it up below it — the decision DESIGN.md's
A5 row logs, reached by measuring a standalone reduction before this file
existed: `position: sticky` sticks to the nearest ancestor that establishes a
scroll container, not to the viewport, so an always-on wrapper would have
silently broken every sticky head in the app instead of fixing the overflow.

One representative page per long-tail family: the
onboarding wizard, a batch's detail page, the payout form, an operation's
detail page, a customer's contacts and the accounts directory.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from playwright.sync_api import Page

from app.crypto import fernet

from tests.browser.conftest import CID, sql
from tests.browser.test_journeys import BATCH_ROW, CORRIDOR, PAYOUTS, _filled, open_draft

WIDTHS = (375, 768, 1024, 1440)


def _batch_detail_url(page: Page, tmp_path) -> str:
    template = page.request.get(
        f"/customers/{CID}/batches/template.csv{CORRIDOR}"
    ).text()
    good = tmp_path / "a5-batch.csv"
    good.write_bytes(_filled(template, [BATCH_ROW]))
    page.goto(f"/customers/{CID}/batches/new{CORRIDOR}")
    page.locator("input[type=file][data-batch]").set_input_files(good)
    page.wait_for_url(re.compile(r"/batches/[0-9a-f-]{8}-"))
    return page.url


def _operation_url() -> str:
    op_id = uuid.uuid4()
    sql(
        """insert into operations
             (id, type, actor_id, actor_email, request_path, request_hash,
              idempotency_key, state, attempt_count, reconcile_count)
           values (%s, 'payout_cancel', 'usr_browser', 'ops@example.com',
                   '/v2/payouts/txn_payout_1/cancel', %s, %s, 'stalled', 1, 5)""",
        (op_id, f"a5hash{op_id.hex}"[:64], uuid.uuid4()),
    )
    return f"/operations/{op_id}"


def _seed_contact() -> None:
    sql(
        """insert into counterparties
             (id, customer_id, label, recipient, rail_family, recipient_type,
              destination_country, created_by_actor_id, created_by_actor_email)
           values (%s, %s, 'ZZZTEST A5 Supplies', %s, 'us', 'business', 'USA',
                   'usr_browser', 'ops@example.com')""",
        (
            uuid.uuid4(),
            CID,
            fernet().encrypt(
                json.dumps(
                    {
                        "accountNumber": "000123456789",
                        "routingNumber": "021000021",
                        "legalName": "ZZZTEST A5 Supplies LLC",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        ),
    )


def _seed_accounts(n: int = 5) -> None:
    for i in range(n):
        sql(
            "insert into projections (id, resource_kind, resource_id, state, payload, observed_at)"
            " values (gen_random_uuid(), 'virtual_accounts', %s, 'active', %s::jsonb, now())",
            (
                f"vac_a5_{i:03d}",
                json.dumps(
                    {
                        "virtualAccountId": f"vac_a5_{i:03d}",
                        "customerId": CID,
                        "asset": {"code": "USD"},
                        "status": "active",
                    }
                ),
            ),
        )


@pytest.fixture
def pages(page: Page, tmp_path) -> dict[str, str]:
    """One URL per long-tail family, built once so both theme runs reuse it."""
    _seed_contact()
    _seed_accounts()
    return {
        "onboarding wizard": open_draft(page),
        "batch detail": _batch_detail_url(page, tmp_path),
        "payout form": PAYOUTS,
        "operation detail": _operation_url(),
        "contacts": f"/customers/{CID}/contacts",
        "accounts": "/accounts?limit=100",
    }


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_no_long_tail_page_overflows_the_viewport(page: Page, pages: dict[str, str], theme: str):
    """§6's "no horizontal overflow at 375 / 768 / 1024 / 1440", strengthened
    to the plain invariant the wrapper makes true: a scrolling *descendant*
    (`overflow-x: auto`) does not count toward its ancestor's `scrollWidth`,
    so once every table sits inside `.table-scroll`, the document itself never
    needs to grow past the viewport to hold one — nothing left to exclude, the
    way the older A2/A4 pins had to exclude anything inside a `<table>`.
    """
    page.emulate_media(color_scheme=theme)
    for name, url in pages.items():
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(url)
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll_width <= width + 1, (
                f"{name} ({url}) overflows at {width}px in {theme}: "
                f"scrollWidth={scroll_width}"
            )


GAP_WIDTHS = (1025, 1100, 1179)

TABLE_OUTGROWS_ITS_WRAPPER = """
    () => [...document.querySelectorAll('.table-scroll')].some(
        wrapper => [...wrapper.querySelectorAll('table')].some(
            table => table.scrollWidth > wrapper.clientWidth))"""


def test_no_long_tail_page_overflows_between_1024_and_the_shells_cap(
    page: Page, pages: dict[str, str]
):
    """A4 stopped at 1024, so the band up to the shell's cap was never probed: a
    table clean at 1024 is not clean at 1025."""
    probes = {
        **pages,
        "applications": "/applications",
        "orders": "/orders",
        "transactions": "/transactions",
    }
    carried = []
    for name, url in probes.items():
        for width in GAP_WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(url)
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll_width <= width + 1, (
                f"{name} ({url}) overflows at {width}px: scrollWidth={scroll_width}"
            )
            if page.evaluate(TABLE_OUTGROWS_ITS_WRAPPER):
                carried.append(f"{name}@{width}px")
    assert carried, (
        f"no probed table was wider than its own wrapper at any of {GAP_WIDTHS}, "
        f"so nothing above measured the band: {list(probes)}"
    )


def test_the_wrapper_only_scrolls_below_the_width_tables_overflow_at(page: Page):
    """The decision itself, pinned: `.table-scroll` is a no-op above the
    shell's own cap, 1180px / 73.75rem (sticky heads keep working exactly as
    before A5) and takes `overflow-x: auto` at and below it — widened from
    the original 1024px, which was the width band DESIGN.md's A4 row
    measured the pre-existing overflow at, but not the width band a table
    can actually need it in (see the gap test above)."""
    _seed_accounts()
    page.goto("/accounts?limit=100")

    page.set_viewport_size({"width": 1440, "height": 900})
    assert (
        page.evaluate(
            "() => getComputedStyle(document.querySelector('.table-scroll')).overflowX"
        )
        == "visible"
    )

    page.set_viewport_size({"width": 1024, "height": 900})
    assert (
        page.evaluate(
            "() => getComputedStyle(document.querySelector('.table-scroll')).overflowX"
        )
        == "auto"
    )


def test_sticky_head_survives_above_the_wrapper_width_and_the_page_still_does_not_overflow_below_it(
    page: Page,
):
    """The other half of the same decision, in a real scroll rather than a
    computed style: at 1440 the header still tracks the viewport exactly as
    `test_the_column_headers_stay_put_when_a_long_list_scrolls` proves for the
    default (unconstrained) viewport; below 1024 the wrapper takes over and
    the page itself carries no horizontal overflow — the trade this slice
    made instead of losing both.
    """
    for n in range(40):
        sql(
            "insert into projections (id, resource_kind, resource_id, state, payload, observed_at)"
            " values (gen_random_uuid(), 'virtual_accounts', %s, 'active', %s::jsonb, now())",
            (
                f"vac_a5sticky_{n:03d}",
                json.dumps(
                    {
                        "virtualAccountId": f"vac_a5sticky_{n:03d}",
                        "customerId": CID,
                        "asset": {"code": "USD"},
                        "status": "active",
                    }
                ),
            ),
        )

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/accounts?limit=100")
    header = page.locator("table tr", has=page.locator("th")).first
    top_before = header.bounding_box()["y"]
    page.evaluate("() => window.scrollTo(0, 2000)")
    page.wait_for_timeout(200)
    box = header.bounding_box()
    assert box is not None and 0 <= box["y"] < top_before, "sticky head lost above 1024px"

    page.set_viewport_size({"width": 768, "height": 900})
    page.goto("/accounts?limit=100")
    assert page.evaluate("() => document.documentElement.scrollWidth") <= 769
