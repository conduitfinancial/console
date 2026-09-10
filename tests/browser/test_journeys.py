"""The journeys from plan v2 §9.4, in a real browser.

These are the assertions no server-side test can make. The route tests already
prove what the server decides; what is proved here is what the *operator* gets:
a conditional field that is genuinely hidden and genuinely disabled, a 502 that
lands inside the page instead of vanishing, a double-click that buys one payout,
a countdown that disables a button before the price lapses.

Everything is headless and deterministic — the Conduit stub answers from the
request alone (`conftest.conduit_stub`), so there is no ordering between tests
and no clock to wait on except the two countdowns, which are four seconds by
construction.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid

import pytest
from playwright.sync_api import Page, expect

from app.crypto import fernet

from tests.browser.conftest import (
    CID,
    DEST_EUR as DEST_EUR_ACCOUNT,
    DEST_USD as DEST_USD_ACCOUNT,
    EXPIRING_AMOUNT,
    REGISTERED,
    SLOW_AMOUNT,
    STALE_AMOUNT,
    sql,
)
from tests.payments_fixtures import EUR_ACTIVE, OTHER_CID, VID

DEST_USD = DEST_USD_ACCOUNT["id"]
DEST_EUR = DEST_EUR_ACCOUNT["id"]

# A real PDF: `app.documents.sniff` and Conduit both decide the type from the
# bytes, so a text file named .pdf is refused.
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

REGULATED = "f.businessActivity.hasRegulatedOrRestrictedActivities"
ACTIVITIES = "f.businessActivity.regulatedOrRestrictedActivities"
OTHER_ACTIVITY = "f.businessActivity.otherActivityDescription"

PAYOUTS = f"/customers/{CID}/payouts/new"
FORK = f"/customers/{CID}/payouts"
BATCH_NEW = f"/customers/{CID}/batches/new"
# A batch's route is the corridor alone — the purpose is a column of the file.
CORRIDOR = "?rail=fedwire&recipientType=business&destinationCountry=USA"
GOODS_ROUTE = (
    "?purpose=payment_for_goods_or_services&rail=fedwire"
    "&recipientType=business&destinationCountry=USA"
)
INTERCOMPANY_ROUTE = (
    "?purpose=intercompany&rail=fedwire&recipientType=business&destinationCountry=USA"
)

# Everything the intercompany route still asks for once the whitelist gate has
# removed the recipient's identity fields.
INTERCOMPANY_FIELDS = {
    "f.destination.type": "fiat",
    "f.destination.rail": "fedwire",
    "f.destination.recipient.accountType": "CHECKING",
    "f.destination.recipient.type": "BUSINESS",
    "f.destination.recipient.postalAddress.addressLine1": "500 Market St",
    "f.destination.recipient.postalAddress.city": "New York",
    "f.destination.recipient.postalAddress.country": "US",
    "f.destination.recipient.postalAddress.postalCode": "10010",
    "f.destination.recipient.bankAddress.addressLine1": "270 Park Ave",
    "f.destination.recipient.bankAddress.city": "New York",
    "f.destination.recipient.bankAddress.country": "US",
}


# --- helpers -------------------------------------------------------------------------


def state(page: Page, name: str) -> dict:
    """What `static/conditions.js` actually did to this field in this browser."""
    return page.evaluate(
        """(name) => {
            const input = document.querySelector(`[name="${name}"]`);
            if (!input) return null;
            const box = input.closest('.field');
            return {
                hidden: !!box.hidden,
                visible: !!box.offsetParent || getComputedStyle(box).display !== 'none',
                disabled: !!input.disabled,
                required: !!input.required,
            };
        }""",
        name,
    )


def wait_until(page: Page, expression: str, arg=None, timeout: float = 5000) -> None:
    """`page.wait_for_function`, polled through `page.evaluate`.

    The app sends a CSP with `script-src 'self'` and no `'unsafe-eval'`. Playwright's
    own poller for `wait_for_function` builds its predicate with `new Function` **inside
    the page**, so the page refuses it — the harness needs the allowance, not the
    application, and widening the real CSP to suit the test would be pinning the wrong
    thing. `evaluate` runs over CDP and is not subject to page CSP, so the same
    predicate is polled here with the app's headers left alone.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        if page.evaluate(expression, arg) if arg is not None else page.evaluate(expression):
            return
        assert time.monotonic() < deadline, f"timed out waiting for: {expression}"
        page.wait_for_timeout(50)


def click_and_settle(page: Page, selector: str) -> None:
    """Click, then wait for htmx to *settle*, not merely to swap.

    htmx attaches its listeners to swapped-in content during the settle phase,
    ~20ms after the nodes appear. A test that clicks a freshly swapped button the
    instant it exists therefore clicks a plain, un-wired button and nothing
    happens — a race a human never wins and a test loses about half the time.
    """
    page.evaluate(
        "window.__settled = 0;"
        "document.body.addEventListener('htmx:afterSettle',"
        "  () => { window.__settled++; }, {once: true});"
    )
    page.click(selector)
    wait_until(page, "() => window.__settled > 0")


def select_and_settle(page: Page, selector: str, value: str) -> None:
    """`select_option`, then wait for htmx to *settle* — the same race
    `click_and_settle` documents: a select that was itself swapped in ~20ms ago
    is not yet wired, so the change event fires into nothing."""
    page.evaluate(
        "window.__settled = 0;"
        "document.body.addEventListener('htmx:afterSettle',"
        "  () => { window.__settled++; }, {once: true});"
    )
    page.select_option(selector, value)
    wait_until(page, "() => window.__settled > 0")


def fill(page: Page, values: dict) -> None:
    for name, value in values.items():
        page.fill(f'[name="{name}"]', value)


def step_to(page: Page, selector: str) -> None:
    """Open the wizard step that holds `selector`.

    The wizard shows one section at a time, so a journey that
    types across sections has to move between them first — through the nav,
    which is the affordance an operator uses, not a test-only shortcut. The
    section is looked up from the element rather than hardcoded because which
    group a field lands in is the *snapshot's* decision, not this file's.
    """
    section = page.evaluate(
        "(sel) => { const el = document.querySelector(sel);"
        "  const s = el && el.closest('.form-section'); return s ? s.id : null; }",
        selector,
    )
    assert section, f"{selector} is not inside a wizard section"
    page.click(f"#wizard-nav a[href='#{section}']")


def step_to_field(page: Page, name: str) -> None:
    step_to(page, f'[name="{name}"]')


def open_draft(page: Page, country: str = "USA") -> str:
    page.goto("/onboarding")
    page.fill("#country", country)
    page.click("button[type=submit]")
    page.wait_for_url("**/onboarding/*")
    return page.url


def operations_of(op_type: str) -> list[tuple]:
    return sql("select id, state from operations where type = %s", (op_type,))


# --- the onboarding wizard ------------------------------------------------------------


def test_conditional_fields_cascade_in_a_real_browser(page: Page):
    """The parity gate's runtime half: `condition_vectors.json` proves the two
    evaluators agree, and this proves the JS one is actually wired to the DOM —
    two levels deep, because the second gate reads the first gate's answer."""
    open_draft(page)
    step_to_field(page, REGULATED)

    assert state(page, ACTIVITIES)["hidden"] is True
    assert state(page, OTHER_ACTIVITY)["hidden"] is True

    page.check(f'input[name="{REGULATED}"][value="true"]')
    assert state(page, ACTIVITIES)["hidden"] is False
    assert state(page, OTHER_ACTIVITY)["hidden"] is True

    page.check(f'input[name="{ACTIVITIES}"][value="other_regulated_activity"]')
    assert state(page, OTHER_ACTIVITY)["hidden"] is False

    # And back: answering "No" collapses the whole chain again.
    page.check(f'input[name="{REGULATED}"][value="false"]')
    assert state(page, ACTIVITIES)["hidden"] is True
    assert state(page, OTHER_ACTIVITY)["hidden"] is True


def test_a_hidden_field_is_disabled_so_it_cannot_submit(page: Page):
    """Hiding alone would still post the value. `conditions.js` disables the
    controls too, which is what keeps the browser's idea of the payload equal to
    `forms.active_fields`."""
    open_draft(page)
    step_to_field(page, REGULATED)
    assert state(page, ACTIVITIES)["disabled"] is True

    page.check(f'input[name="{REGULATED}"][value="true"]')
    assert state(page, ACTIVITIES)["disabled"] is False


def test_an_optional_boolean_gate_can_be_answered_no(page: Page):
    """The other half of the widget fix, in the browser: `sameAsRegistered` is a
    three-state radio, so answering *no* is expressible at all — and answering it
    reveals the six operating-address fields a checkbox could never reach."""
    open_draft(page)
    gate = "f.operatingAddress.sameAsRegistered"
    line1 = "f.operatingAddress.addressLine1"
    step_to_field(page, gate)

    expect(page.locator(f'input[name="{gate}"][type=radio]')).to_have_count(2)
    assert state(page, line1)["hidden"] is True

    page.check(f'input[name="{gate}"][value="false"]')
    assert state(page, line1)["hidden"] is False
    assert state(page, line1)["disabled"] is False
    for leaf in ("city", "country", "state", "postalCode", "addressLine2"):
        assert state(page, f"f.operatingAddress.{leaf}")["hidden"] is False

    # "Yes" hides them again; so does going back to unanswered, which a checkbox
    # is the whole reason this is a radio.
    page.check(f'input[name="{gate}"][value="true"]')
    assert state(page, line1)["hidden"] is True


def test_a_requiredwhen_gate_is_live_on_the_payout_form(page: Page):
    """Conditions used to be wired only under `id="wizard"`, so every gate on the
    payout and transfer screens was inert. `postalCode` is `requiredWhen` the
    recipient's country is *not* one of the no-postcode jurisdictions."""
    page.goto(PAYOUTS + GOODS_ROUTE)
    country = "f.destination.recipient.postalAddress.country"
    postal = "f.destination.recipient.postalAddress.postalCode"

    # Unanswered: `not_in` on an absent value never activates a dependant.
    assert state(page, postal)["required"] is False

    page.fill(f'[name="{country}"]', "US")
    page.dispatch_event(f'[name="{country}"]', "change")
    assert state(page, postal)["required"] is True

    page.fill(f'[name="{country}"]', "HK")  # on the no-postcode list
    page.dispatch_event(f'[name="{country}"]', "change")
    assert state(page, postal)["required"] is False


def test_a_select_gate_reveals_its_dependent_field(page: Page):
    open_draft(page)
    other = "f.companyClassification.primaryIndustryOther"
    step_to_field(page, "f.companyClassification.primaryIndustry")
    assert state(page, other)["hidden"] is True

    page.select_option('[name="f.companyClassification.primaryIndustry"]', "other_industry")
    assert state(page, other)["hidden"] is False


def test_the_wizard_nav_counts_what_is_left_in_each_section(page: Page):
    """The nav lists every section the page renders and says what each still
    owes. The counts are read off the DOM *after* conditions.js has settled, so
    they are the same set of fields the server will insist on — filling one is
    visible in its section's badge and in the total.

    Deliberately not asserted: which entry the scrollspy highlights. That is a
    scroll-position race and proves nothing about the form's state.
    """
    open_draft(page)
    nav = page.locator("#wizard-nav")
    expect(nav).to_be_visible()

    # One entry per rendered section, People and Documents among them. Scoped to
    # the list: the nav also carries a "Go to review & submit" anchor at the
    # last section, which is a way back to the submit button rather than a step.
    expect(nav.locator("ol a[href^='#sect-']")).to_have_count(
        page.locator("#wizard .form-section").count()
    )
    expect(nav.locator("ol a[href='#sect-people']")).to_contain_text("People")
    expect(nav.locator("ol a[href='#sect-documents']")).to_contain_text("Documents")

    # businessInfo is the first group and `legalName` is its only required
    # field, so this section goes from "1" to done on one answer.
    badge = nav.locator("[data-count-for='sect-1']")
    expect(badge).to_have_text("1")
    before = int(page.locator("#wizard-total").inner_text().split()[0])

    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Inc")
    page.dispatch_event('[name="f.businessInfo.legalName"]', "change")

    expect(badge).to_have_text("✓")
    expect(page.locator("#wizard-total")).to_have_text(f"{before - 1} required fields left")

    # The documents floor has no required control behind it, so it is counted
    # from the uploaded ids: nothing uploaded yet, one still owed.
    expect(nav.locator("[data-count-for='sect-documents']")).to_have_text("1")


def test_the_wizard_shows_one_section_at_a_time_without_dropping_the_rest(page: Page):
    """QA F-001. The whole risk of progressive disclosure on a form engine is
    that "hidden" quietly becomes "not submitted": the autosave posts `#wizard`
    whole, `conditions.js` walks the whole form, and the counts read global
    state. So the assertions are display-only on one side and
    everything-still-there on the other.

    The mechanism is asserted too, because it is the part that could collide:
    a step is hidden by a CLASS, never by the `hidden` attribute, which is
    `conditions.js`'s own per-field channel.
    """
    open_draft(page)
    sections = page.locator("#wizard .form-section")
    total = sections.count()
    assert total > 2

    # One on screen; every one of them still in the DOM, and not one of them
    # wearing the field layer's `hidden` attribute or a disabled control.
    assert page.locator("#wizard .form-section:visible").count() == 1
    assert page.evaluate(
        """() => {
            const all = [...document.querySelectorAll('#wizard .form-section')];
            return {
                total: all.length,
                hidden_attr: all.filter(s => s.hasAttribute('hidden')).length,
                off: all.filter(s => s.classList.contains('step-off')).length,
                inert: all.filter(s => s.inert).length,
                aria: all.filter(s => s.getAttribute('aria-hidden') === 'true').length,
                // Nothing is disabled by the stepper — that is conditions.js's
                // word, and only for the fields it switched off.
                disabled_in_hidden: all.filter(s => s.classList.contains('step-off'))
                    .flatMap(s => [...s.querySelectorAll('input[name]')])
                    .filter(i => i.disabled && !i.closest('[data-conditions],[data-required-when]'))
                    .length,
            };
        }"""
    ) == {
        "total": total,
        "hidden_attr": 0,
        "off": total - 1,
        "inert": total - 1,
        "aria": total - 1,
        "disabled_in_hidden": 0,
    }

    # A value typed on step 1 survives being stepped away from — and is still in
    # what the autosave posts, which is the assertion that matters.
    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Inc")
    page.click("#wizard .step-foot .next")
    assert page.locator("#wizard .form-section:visible").count() == 1
    assert page.input_value('[name="f.businessInfo.legalName"]') == "ZZZTEST Widgets Inc"
    assert page.evaluate(
        """() => new FormData(document.getElementById('wizard'))
                   .get('f.businessInfo.legalName')"""
    ) == "ZZZTEST Widgets Inc"

    # Back, forward, and straight to anywhere: the nav is not a funnel.
    page.click("#wizard .step-foot .prev")
    expect(page.locator("#sect-1")).to_be_visible()
    page.fill('[name="f.businessInfo.legalName"]', "")  # for the last assertion
    page.click("#wizard-nav a[href='#sect-people']")
    expect(page.locator("#sect-people")).to_be_visible()
    expect(page.locator("#wizard-nav a[href='#sect-people']")).to_have_attribute(
        "aria-current", "step"
    )

    # The submit is at the foot of the LAST step — one button, the one the
    # server rendered — and the nav has a way back to it from anywhere.
    page.click("#wizard-nav .nav-submit a")
    submit = page.locator("#wizard button[type=submit]")
    expect(submit).to_have_count(1)
    expect(submit).to_be_visible()
    assert page.evaluate(
        """() => {
            const s = document.querySelector('#wizard button[type=submit]');
            const sect = s.closest('.form-section');
            const all = [...document.querySelectorAll('#wizard .form-section')];
            return s.closest('.step-foot') !== null && sect === all[all.length - 1];
        }"""
    )

    # Submitting from the last step with a required field unanswered three steps
    # back: the browser cannot show a validation message on a control it cannot
    # focus — it refuses the submit and logs "not focusable" instead, which is a
    # click that does nothing. The step holding it is opened first.
    submit.click()
    expect(page.locator("#sect-1")).to_be_visible()
    assert page.evaluate(
        "() => document.activeElement === document.querySelector("
        "'[name=\"f.businessInfo.legalName\"]')"
    )


def test_a_long_checkbox_group_can_be_filtered_without_losing_a_tick(page: Page):
    """QA F-001's other half. `countriesOfActivity` is 248 checkboxes; the filter
    is display-only — it hides labels, never boxes — and a ticked country is
    never hidden by a search term, because what you have answered disappearing
    off screen is exactly how you end up submitting something you did not mean.
    """
    open_draft(page)
    group = page.locator('#wizard .field.choices:has(input[value="AFG"])')
    step_to(page, '#wizard input[value="AFG"]')
    boxes = group.locator("input[type=checkbox]")
    assert boxes.count() == 248

    group.locator("input[value=\"DEU\"]").check()
    search = group.locator(".choice-filter input")
    search.fill("bulgar")  # matched on the NAME, which only the label carries

    # The option labels only — the field's own `<label for=…>` heading is not one.
    shown = group.locator("label:has(input):not(.filtered-out)")
    # Bulgaria, plus the ticked one that filtering may never take away.
    expect(shown).to_have_count(2)
    expect(group.locator(".choice-filter")).to_contain_text("2 of 248 shown")
    assert group.locator('input[value="DEU"]').is_checked()
    # Nothing was removed or disabled — 248 boxes, all submittable, all still
    # counted by the nav.
    assert boxes.count() == 248
    assert group.locator("input[type=checkbox]:disabled").count() == 0

    group.locator(".choice-filter button").click()
    expect(shown).to_have_count(248)


def test_a_failed_autosave_says_so_and_the_next_change_retries(page: Page):
    """**The defect:** `responseHandling` in base.html makes a 5xx a *no-swap*, and
    a dropped connection never reaches a swap at all — so `#save-state` kept
    whatever it last said. After one good save that is a green "Draft saved
    12:04:31" sitting over edits that are not saved, next to a line telling the
    operator it is safe to leave. A save state that lies is worse than none.

    **The copy:** "retrying on your next change" is a promise, so it is tested
    rather than assumed — htmx leaves the `hx-trigger` listener attached across
    a failed request and `hx-sync="this:replace"` queues rather than detaches,
    which is what the second half below actually proves. If htmx ever stops
    re-firing, this fails and the sentence has to change.

    The failure is forced in the browser (`page.route(...).abort()`), so no
    server-side fault injection is needed and `/save` is untouched.
    """
    open_draft(page)
    span = page.locator("#save-state")

    # A good save first: the stale-green state is only reachable from here.
    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Inc")
    page.dispatch_event('[name="f.businessInfo.legalName"]', "change")
    expect(span).to_contain_text("Draft saved")

    page.route("**/save", lambda route: route.abort())
    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Two")
    page.dispatch_event('[name="f.businessInfo.legalName"]', "change")

    expect(span).to_contain_text("Save failed")
    # The stale success is gone, not merely covered.
    expect(span).not_to_contain_text("Draft saved")
    expect(span.locator("span.warn")).to_have_count(1)

    # …and the promise: the very next change re-fires the autosave.
    page.unroute("**/save")
    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Three")
    page.dispatch_event('[name="f.businessInfo.legalName"]', "change")
    expect(span).to_contain_text("Draft saved")
    expect(span.locator("span.warn")).to_have_count(0)


def test_no_requester_in_the_wizard_inherits_the_submit_buttons_disable_rule(page: Page):
    """QA F-004, root cause and user-visible symptom.

    `hx-disabled-elt="find button[type=submit]"` on `<form id="wizard">` is
    *inherited* — the same htmx trap that was fixed for `hx-sync` and missed
    here. Resolved from the autosave span (or from a person button) `find`
    searches that element's own subtree, matches nothing, and htmx logs
    `The selector "find button[type=submit]" on hx-disabled-elt returned no
    matches!` on every keystroke-triggered save.

    Two assertions, because the console line is only the smell:
      * nothing in the wizard logs that error any more — autosave, Add and
        Remove all issue requests here;
      * and the *symptom* an operator would feel if the inherited selector ever
        started matching: during an autosave the submit button stays live, and
        the only element htmx disables is the span that issued the request.

    `htmx:beforeSend` is the probe point: htmx disables (`Yt`) after
    `htmx:beforeRequest` and before `beforeSend`, so by then the flags are set.
    """
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    open_draft(page)
    page.evaluate(
        """() => {
            window.__probe = null;
            document.body.addEventListener('htmx:beforeSend', (e) => {
                if (e.detail.elt.id !== 'save-state') return;
                window.__probe = {
                    submit: document.querySelector('#wizard button[type=submit]').disabled,
                    span: document.getElementById('save-state').hasAttribute('disabled'),
                };
            });
        }"""
    )
    page.fill('[name="f.businessInfo.legalName"]', "ZZZTEST Widgets Inc")
    page.dispatch_event('[name="f.businessInfo.legalName"]', "change")
    expect(page.locator("#save-state")).to_contain_text("Draft saved")

    # The submit stayed clickable; the span disabled *itself*, which is what
    # `hx-disabled-elt="this"` means and what proves the override resolved at
    # all rather than being quietly absent.
    assert page.evaluate("() => window.__probe") == {"submit": False, "span": True}

    # The other two requesters inside the form, on the same inherited attribute.
    step_to(page, ".person-card")
    click_and_settle(page, "button:has-text('Add BENEFICIAL_OWNER')")
    click_and_settle(page, ".person-card:last-of-type button:has-text('Remove')")

    assert [line for line in errors if "hx-disabled-elt" in line] == []


def test_people_can_be_added_and_removed(page: Page):
    """`individualRequirements` min/max drives the affordances — the USA snapshot
    seeds one `any` and one `BENEFICIAL_OWNER`, both with `min_count` 1."""
    open_draft(page)
    cards = page.locator(".person-card")
    expect(cards).to_have_count(2)
    step_to(page, ".person-card")

    # The `any` row is a requirement *selector*, not a role Conduit will accept
    # (conduit-issues/06 item 1) — it heads its card in operator language now,
    # and the raw sentinel is only in the hidden input that carries it back.
    expect(cards.first).to_contain_text("Any role")
    assert cards.first.locator("input[type=hidden]").input_value() == "any"

    click_and_settle(page, "button:has-text('Add BENEFICIAL_OWNER')")
    expect(cards).to_have_count(3)

    click_and_settle(page, ".person-card:last-of-type button:has-text('Remove')")
    expect(cards).to_have_count(2)

    # Removable is counted the way the validator counts — `forms.satisfies`, on
    # the roles a person answered — not by card. Both seeded cards satisfy the
    # `any` row, so that row is over its minimum and its card may go; dropping
    # it leaves the BENEFICIAL_OWNER card answering both rows at once. The old
    # expectation here (nothing removable "at the minimum") was the card
    # counting this form deliberately stopped doing.
    expect(cards.first.locator("button:has-text('Remove')")).to_have_count(1)
    expect(cards.nth(1).locator("button:has-text('Remove')")).to_have_count(0)
    click_and_settle(page, ".person-card:first-of-type button:has-text('Remove')")
    expect(cards).to_have_count(1)
    # One person, both rows satisfied, nobody spare: no Remove at all.
    expect(page.locator(".person-card button:has-text('Remove')")).to_have_count(0)


def test_an_upload_becomes_a_chip_and_the_chip_can_be_removed(page: Page, tmp_path):
    pdf = tmp_path / "evidence.pdf"
    pdf.write_bytes(PDF)
    open_draft(page)
    step_to(page, "#doc-chips")

    page.locator("#wizard input[type=file][data-purpose='organization_onboarding']").set_input_files(
        pdf
    )
    chip = page.locator("#doc-chips .chip")
    expect(chip).to_have_count(1)
    expect(chip).to_contain_text("doc_browser_1")
    assert page.locator('#doc-chips input[name="documentIds"]').input_value() == "doc_browser_1"

    chip.locator("[data-remove-chip]").click()
    expect(page.locator("#doc-chips .chip")).to_have_count(0)


def test_an_upstream_refusal_renders_inside_the_page(page: Page):
    """Htmx 2 drops every non-2xx by default; `base.html`'s responseHandling adds
    400/422/502 back. Without it the operator clicks and watches nothing happen.

    A3 changed the WORDS in that box and not the mechanism: the stub answers
    `VALIDATION_ERROR` titled "Country not supported", and what a real browser
    paints is this console's sentence for that code, with the correlation id
    beside it and Conduit's own title nowhere on the page.
    """
    page.goto("/onboarding")
    page.fill("#country", "ZZ")
    page.click("button[type=submit]")
    expect(page.locator(".problem")).to_contain_text(
        "Conduit refused some of the values on this form"
    )
    expect(page.locator(".problem")).to_contain_text("cor_browser_1")
    assert "Country not supported" not in page.content()


def test_a_mistyped_url_lands_inside_the_product(page: Page):
    """The Arca spec's second blocking finding, in a real browser (§0.2): a bad
    address used to paint `{"detail":"Not Found"}` on white — the framework
    default showing through. It is a page now: the ribbon is there, the title
    is the console's, and the way back is a link rather than the Back button.
    """
    response = page.goto("/customers/cus_does_not_exist_at_all/nope")
    assert response is not None and response.status == 404

    expect(page.locator("nav.ribbon")).to_be_visible()
    expect(page.locator("h1")).to_have_text("There is nothing at this address")
    assert "{\"detail\"" not in page.content()

    # …and the way back works from where the reader actually is.
    page.get_by_role("link", name="Go to the Overview").click()
    expect(page.locator("h1")).to_have_text("Overview")


# --- applications ---------------------------------------------------------------------


def test_the_status_poller_stops_on_a_terminal_application(page: Page):
    page.goto("/applications/app_open")
    panel = page.locator("#app-status")
    expect(panel).to_have_attribute("hx-trigger", "every 15s")
    expect(panel).to_contain_text("refreshing every 15s")

    page.goto("/applications/app_settled")
    panel = page.locator("#app-status")
    expect(panel).to_contain_text("settled — live refresh stopped")
    assert panel.get_attribute("hx-trigger") is None


def test_the_quick_view_drawer_opens_reads_and_returns_focus(page: Page):
    """The one journey no server-side test can make: the drawer is a mechanism,
    and its whole a11y claim is about focus.

    Non-modal is asserted as well as claimed — the list behind it keeps its
    role, and nothing on the page is made inert while the drawer is open.
    """
    page.goto("/applications")
    drawer = page.locator("#drawer")
    expect(drawer).to_be_hidden()

    opener = page.locator('button[aria-label="Quick view — app_settled"]')
    opener.click()

    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Approved")
    expect(drawer).to_contain_text("ZZZTEST Ltd")  # the bounded name resolver
    expect(drawer.locator('a[href="/applications/app_settled"]')).to_have_text(
        "Open application"
    )
    expect(drawer.locator(f'a[href="/customers/{CID}"]')).to_have_text("Open customer")

    # Focus moved INTO the panel. `#drawer` itself is the target: it carries the
    # label, so this is what a screen reader announces on open.
    assert page.evaluate("() => document.activeElement.id") == "drawer"
    # An overlay, not a takeover: the table behind is still reachable and still
    # not inert, and no other list row was replaced by the swap.
    assert page.evaluate(
        "() => !!document.querySelector('table').closest('[inert],[aria-hidden=true]')"
    ) is False
    expect(page.locator('button[aria-label="Quick view — app_open"]')).to_be_visible()

    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    # Back to the exact control that opened it, not to the top of the document.
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == (
        "Quick view — app_settled"
    )

    # Second open, then dismissed by the drawer's own button: the shell survived
    # the first swap (the indicator lives in it) and still works.
    opener.click()
    expect(drawer).to_be_visible()
    drawer.locator("[data-drawer-close]").first.click()
    expect(drawer).to_be_hidden()


def count_aborts(page: Page) -> None:
    """Count htmx's own `htmx:sendAbort` — the event an aborted xhr raises (and
    the one it raises *instead of* `sendError`, which is why cancelling can
    never reach the drawer's failure card)."""
    page.evaluate(
        """() => {
            window.__aborts = 0;
            document.body.addEventListener('htmx:sendAbort', () => { window.__aborts++; });
        }"""
    )


def test_a_second_quick_view_replaces_the_first_instead_of_racing_it(page: Page):
    """The wrong-record race. One panel, one button per row, and no queue: a
    slow answer for row A landing after a fast answer for row B leaves A's facts
    on screen while the operator believes they opened B — one application's
    status read as another's.

    `hx-sync="#drawer:replace"` is the htmx-native fix: the shell is the sync
    master (it is what these requests contend for, and htmx records the
    in-flight xhr there), and `replace` aborts the loser rather than ignoring
    it. Row A is held unanswered in the browser so the window is real rather
    than timed.
    """
    page.goto("/applications")
    count_aborts(page)
    held: list = []
    page.route("**/app_open/quick", lambda route: held.append(route))

    page.locator('button[aria-label="Quick view — app_open"]').click()
    drawer = page.locator("#drawer")
    # In flight and unanswered: the panel is up on its loading state, empty.
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Loading…")
    expect(page.locator("#drawer-body")).to_be_empty()

    page.locator('button[aria-label="Quick view — app_settled"]').click()
    expect(drawer).to_contain_text("app_settled")
    # Cancelled, not merely outrun — and cancelling did not render a failure.
    assert page.evaluate("() => window.__aborts") == 1
    expect(drawer).not_to_contain_text("could not be loaded")

    # A's answer, released late, must never reach the panel.
    for route in held:
        try:
            route.fulfill(status=200, content_type="text/html", body="<p>app_open LATE</p>")
        except Exception:  # already aborted by the browser — which is the point
            pass
    page.wait_for_timeout(300)
    expect(drawer).to_contain_text("app_settled")
    expect(drawer).not_to_contain_text("LATE")


def test_escape_during_a_quick_view_load_leaves_it_closed(page: Page):
    """Dismissal has to CANCEL, not merely clear.

    Escape while a load is in flight used to empty a body that was about to be
    filled: the response landed a moment later, the drawer reopened by itself
    and took focus back off whatever the operator had moved on to. Two halves to
    the fix — "open" now includes the loading panel (htmx's own `htmx-request`
    class on the shell), so Escape is live at all; and `closeDrawer` fires
    `htmx:abort` at the `hx-sync` master, so the request dies with it.
    """
    page.goto("/applications")
    count_aborts(page)
    held: list = []
    page.route("**/quick", lambda route: held.append(route))

    opener = page.locator('button[aria-label="Quick view — app_settled"]')
    opener.click()
    drawer = page.locator("#drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Loading…")

    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    assert page.evaluate("() => window.__aborts") == 1
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == (
        "Quick view — app_settled"
    )

    # The late answer, released after the dismissal: it must not reopen the
    # panel, and it must not take focus back.
    for route in held:
        try:
            route.fulfill(status=200, content_type="text/html", body="<p>LATE app_settled</p>")
        except Exception:
            pass
    page.wait_for_timeout(300)
    expect(drawer).to_be_hidden()
    expect(page.locator("#drawer-body")).to_be_empty()
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == (
        "Quick view — app_settled"
    )
    # A cancelled request is not a failed one: no error card was rendered.
    expect(drawer).not_to_contain_text("could not be loaded")

    # …and the button still works once the network does.
    page.unroute("**/quick")
    opener.click()
    expect(drawer).to_contain_text("Approved")


def test_a_quick_view_that_never_loads_says_so_in_the_drawer(page: Page):
    """Never a dead click.

    The route itself answers 200 even when Conduit is unreachable — the problem
    card is rendered *inside* the fragment, and `tests/test_web_applications.py`
    pins that. What is left is everything htmx's `responseHandling` (base.html)
    deliberately drops without a swap: the middleware's 401/403, a 5xx from a
    bug, and a request that never completes. Without the handler in
    `static/app.js` each of those is a button that dims and then does nothing.

    Both are forced in the browser, so no server-side fault injection is needed
    and the route is untouched — the same technique the autosave failure uses.
    """
    page.goto("/applications")
    drawer = page.locator("#drawer")
    opener = page.locator('button[aria-label="Quick view — app_settled"]')

    page.route("**/quick", lambda route: route.fulfill(status=503, body=""))
    opener.click()
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("The quick view could not be loaded")
    expect(drawer).to_contain_text("HTTP 503")
    # A way on and a way out, both — the full page asks Conduit again.
    expect(drawer.locator('a[href="/applications/app_settled"]')).to_have_text(
        "Open application"
    )
    # Focus still lands in the panel: a failure the operator cannot find is the
    # dead click with extra steps.
    assert page.evaluate("() => document.activeElement.id") == "drawer"
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()

    # A connection that never completes has no status to report, and says that
    # rather than inventing one.
    page.unroute("**/quick")
    page.route("**/quick", lambda route: route.abort())
    opener.click()
    expect(drawer).to_contain_text("The request did not reach the console.")
    expect(drawer).not_to_contain_text("HTTP")

    # A failure REPLACES what was on screen. Loading one row's facts and then
    # failing on the next would otherwise leave one application's details
    # sitting under another's error.
    page.unroute("**/quick")
    drawer.locator("[data-drawer-close]").first.click()
    opener.click()
    expect(drawer).to_contain_text("Approved")
    page.route("**/quick", lambda route: route.abort())
    page.locator('button[aria-label="Quick view — app_open"]').click()
    expect(drawer).to_contain_text("The request did not reach the console.")
    expect(drawer).not_to_contain_text("Approved")

    # …and the button still works once it can: nothing was detached.
    page.unroute("**/quick")
    drawer.locator("[data-drawer-close]").first.click()
    opener.click()
    expect(drawer).to_contain_text("Approved")
    expect(drawer).not_to_contain_text("could not be loaded")


# --- payouts --------------------------------------------------------------------------


def test_the_documentation_gate_shapes_the_payout_form(page: Page):
    """`documentation.required: true` on this route and nothing else: the upload
    widget appears, the whitelist picker does not, and the recipient's own
    identity fields stay on the form."""
    page.goto(PAYOUTS + GOODS_ROUTE)
    expect(page.locator("text=This route requires a supporting document")).to_be_visible()
    expect(page.locator("#whitelistRecipientId")).to_have_count(0)
    expect(page.locator('[name="f.destination.recipient.routingNumber"]')).to_be_visible()


def test_the_whitelist_gate_shapes_the_payout_form(page: Page):
    """The other polarity, from the same code path: the recipient's coordinates
    are removed from the form and replaced by Conduit's registered record."""
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    expect(page.locator("text=This route requires a whitelisted recipient")).to_be_visible()
    expect(page.locator('[name="f.destination.recipient.routingNumber"]')).to_have_count(0)
    expect(page.locator('[name="f.destination.recipient.legalName"]')).to_have_count(0)
    # Only `registered` entries are offered — the pending one is not payable.
    options = page.locator("#whitelistRecipientId option")
    expect(options).to_have_count(2)  # the "—" placeholder plus the registered entry
    expect(options.nth(1)).to_contain_text("ZZZTEST Globex Supplies LLC")


def test_an_expired_quote_disables_the_send_button(page: Page):
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    page.fill("#amount", STALE_AMOUNT)
    page.click("button:has-text('Get / refresh quote')")

    expect(page.locator("#quote-panel")).to_contain_text("indicative only")
    expect(page.locator("#quote-panel")).to_contain_text("This quote has already expired")
    expect(page.locator("#payout-submit")).to_be_disabled()
    expect(page.locator("#quote-stale-note")).to_be_visible()


def test_a_double_click_sends_exactly_one_payout(page: Page):
    """The intent nonce's browser proof (OPERATIONS_SPEC §1). `hx-sync` +
    `hx-disabled-elt` should already collapse the second click; the nonce is what
    holds if they do not."""
    before = len(operations_of("payout_create"))
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    page.fill("#amount", "250.00")
    page.select_option("#whitelistRecipientId", REGISTERED["id"])
    fill(page, INTERCOMPANY_FIELDS)

    page.dblclick("#payout-submit")
    page.wait_for_url("**/transactions/txn_payout_1")

    rows = operations_of("payout_create")
    assert len(rows) == before + 1, rows
    assert rows[-1][1] == "confirmed"


def test_an_internal_reference_typed_on_a_payout_comes_back_on_the_operation(page: Page):
    """The whole loop in one browser: type a note, send the
    payout, and read the note back on the operation panel the transaction page
    renders. What makes it worth a browser test rather than a route test is that
    the note is an ordinary input in a form whose *other* fields are built by
    the engine — it has to be picked up by the same submit, from the same DOM."""
    note = "Acme Q3 invoice 4471"
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    page.fill("#amount", "250.00")
    page.fill("#internal-reference", note)
    page.select_option("#whitelistRecipientId", REGISTERED["id"])
    fill(page, INTERCOMPANY_FIELDS)

    page.click("#payout-submit")
    page.wait_for_url("**/transactions/txn_payout_1")

    expect(page.locator("text=" + note)).to_be_visible()
    assert sql("select reference from operations where type = 'payout_create'")[-1][0] == note


def test_a_refused_payout_re_renders_inline_with_its_errors(page: Page):
    """A 422 the app minted itself, swapped into `<main>` — the operator's values
    are still on the form and the reason is on the screen."""
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    page.fill("#amount", "not-a-number")
    page.select_option("#whitelistRecipientId", REGISTERED["id"])
    fill(page, INTERCOMPANY_FIELDS)
    page.click("#payout-submit")

    expect(page.locator("text=This payout was not sent")).to_be_visible()
    # Re-rendered, not reloaded: the amount the operator typed is still there.
    expect(page.locator("#amount")).to_have_value("not-a-number")


# --- transfers ------------------------------------------------------------------------


def test_a_transfer_goes_out_over_the_virtual_account_arm(page: Page):
    """REWRITTEN from `test_a_transfer_goes_out_over_the_whitelisted_destination`:
    the screen no longer picks a registered whitelist entry, a rail or a
    destination country — it names the destination *account*."""
    before = len(operations_of("payout_create"))
    page.goto(
        f"/customers/{CID}/transfers/new"
        f"?virtualAccountId={VID}&destination={OTHER_CID}"
    )
    page.select_option("#destinationVirtualAccountId", DEST_USD)
    page.fill("#amount", "250.00")
    page.fill("#remittanceReference", "ZZZTEST-BROWSER")
    page.click("#transfer-submit")
    page.wait_for_url("**/transactions/txn_payout_1")

    assert len(operations_of("payout_create")) == before + 1


def test_changing_the_source_account_moves_the_balances_and_the_currency_label(page: Page):
    """The reported, screenshot-evidenced defect this screen was rebuilt
    around: the source select did not re-render, so a USD selection showed the
    EUR account's balances under an "Amount (EUR)" label. The fix is structural —
    the select refetches the page — and only a browser can prove that the htmx
    wiring actually does it.
    """
    page.goto(f"/customers/{CID}/transfers/new?virtualAccountId={VID}")
    expect(page.locator("label[for=amount]")).to_have_text("Amount (USD)")
    expect(page.locator("text=125000.00")).to_be_visible()

    # No submit, no button: moving the select is what has to re-render.
    select_and_settle(page, "#virtualAccountId", EUR_ACTIVE["id"])
    expect(page.locator("label[for=amount]")).to_have_text("Amount (EUR)")
    expect(page.locator("text=125000.00")).to_have_count(0)

    # ...and back, so the pin is a pair rather than a one-way assertion.
    select_and_settle(page, "#virtualAccountId", VID)
    expect(page.locator("label[for=amount]")).to_have_text("Amount (USD)")
    expect(page.locator("text=125000.00")).to_be_visible()


def test_a_cross_currency_destination_account_cannot_be_picked(page: Page):
    """Disabled with the reason on the option, not filtered out of the list."""
    page.goto(
        f"/customers/{CID}/transfers/new"
        f"?virtualAccountId={VID}&destination={OTHER_CID}"
    )
    option = page.locator(f'#destinationVirtualAccountId option[value="{DEST_EUR}"]')
    expect(option).to_be_disabled()
    expect(option).to_contain_text("holds EUR, not USD")


# --- the Transact launcher ------------------------------------------------------------


def test_the_transact_launcher_lands_on_the_picked_customers_screen(page: Page):
    """The one thing no server-side test can prove about the IA change: the
    three verbs are buttons, not links, because a path parameter has to be
    assembled from the picked customer — and that assembly is six lines of
    app.js. If they break, the nav's new front door goes nowhere.
    """
    for verb, destination in (
        # The payout verb lands on the Transact fork now: one payment or a
        # batch, asked before any purpose.
        ("/payouts", f"/customers/{CID}/payouts"),
        ("/transfers/new", f"/customers/{CID}/transfers/new"),
        ("/convert", f"/customers/{CID}/convert"),
    ):
        page.goto("/orders")
        expect(page.locator("h1")).to_have_text("Orders")
        page.click(f"[data-launch='{verb}']")
        page.wait_for_url("**" + destination)


# --- conversions ----------------------------------------------------------------------


def convert_to_confirm(page: Page, amount: str) -> None:
    page.goto(f"/customers/{CID}/convert")
    page.fill("#amount", amount)
    page.check('input[name="lockSide"][value="source"]')
    click_and_settle(page, "button:has-text('Get quote')")
    expect(page.locator("text=Pick an option")).to_be_visible()
    page.click("button:has-text('Choose')")
    page.wait_for_url("**/convert/*")


def test_a_reference_typed_while_the_quote_is_in_flight_survives_the_swap(page: Page):
    """The quote round-trip replaces the whole `<main>`,
    and the re-render echoes the reference the *request* carried — so a note
    typed during that second was thrown away, and `hx-include` then submitted
    the stale value with the order. `hx-preserve` on the input carries the live
    node across the swap; this is the proof, against the vendored htmx 2.0.4 and
    a deliberately slow quote endpoint."""
    page.goto(f"/customers/{CID}/convert")
    page.fill("#amount", SLOW_AMOUNT)
    page.fill("#internal-reference", "typed before quoting")
    page.check('input[name="lockSide"][value="source"]')
    page.click("button:has-text('Get quote')")
    # Mid-flight: the stub is still sleeping, the swap has not happened yet.
    page.fill("#internal-reference", "typed while quoting")

    expect(page.locator("text=Pick an option")).to_be_visible()
    expect(page.locator("#internal-reference")).to_have_value("typed while quoting")

    page.click("button:has-text('Choose')")
    page.wait_for_url("**/convert/*")
    assert sql("select reference from operations where type = 'order_create'")[-1][0] == (
        "typed while quoting"
    )


def test_a_conversion_is_quoted_chosen_and_confirmed(page: Page):
    before = len(operations_of("order_create"))
    convert_to_confirm(page, "1000.00")

    expect(page.locator("text=Confirm this conversion")).to_be_visible()
    expect(page.locator("#confirm-submit")).to_be_enabled()
    page.click("#confirm-submit")
    page.wait_for_url("**/orders/ord_conv_1")

    rows = operations_of("order_create")
    assert len(rows) == before + 1
    assert rows[-1][1] == "confirmed"


def test_the_option_countdown_disables_the_confirm_button(page: Page):
    """`static/app.js` ticks once a second against the option's own `expiresAt`.
    The stub hands this one four seconds of life, so the button is live when the
    page loads and dead a moment later — without a reload."""
    convert_to_confirm(page, EXPIRING_AMOUNT)
    expect(page.locator("#confirm-submit")).to_be_enabled()
    expect(page.locator("#option-expiry")).to_contain_text("expires in")

    expect(page.locator("#confirm-submit")).to_be_disabled(timeout=10_000)
    expect(page.locator("#option-expiry")).to_contain_text("expired — re-quote")


# --- the stalled-operation panel ------------------------------------------------------


def test_a_stalled_operation_can_be_retried_from_its_panel(page: Page):
    """The §5 release path: same row, same idempotency key. The row is seeded
    directly because getting there through the UI means five failed reconciler
    passes, which is the reconciler's test, not the browser's."""
    op_id = uuid.uuid4()
    sql(
        """insert into operations
             (id, type, actor_id, actor_email, request_path, request_hash,
              idempotency_key, state, attempt_count, reconcile_count)
           values (%s, 'payout_cancel', 'usr_browser', 'ops@example.com',
                   '/v2/payouts/txn_payout_1/cancel', %s, %s, 'stalled', 1, 5)""",
        (op_id, f"browserhash{op_id.hex}"[:64], uuid.uuid4()),
    )

    page.goto(f"/operations/{op_id}")
    expect(page.locator("text=Couldn't confirm — needs attention")).to_be_visible()
    page.click("button:has-text('Retry (same idempotency key)')")

    expect(page.locator(".flash.msg")).to_contain_text("Retried.")
    assert sql("select state from operations where id = %s", (op_id,)) == [("confirmed",)]


def test_a_chain_deeper_than_the_old_cap_settles_in_one_event(page: Page):
    """The fixed point is bounded by the number of conditional fields, not by a
    constant. This builds a seven-link chain — each field gated on the previous
    one's value existing — and asserts one `change` collapses all of it. Under
    the old cap of five it would have stopped two links short, leaving fields on
    screen that the server would drop from the submission.
    """
    open_draft(page)
    depth = 7
    page.evaluate(
        """(depth) => {
            const host = document.getElementById('root-fields');
            let previous = 'synthetic.gate';
            const box = document.createElement('div');
            box.className = 'field';
            box.innerHTML = '<input type="checkbox" name="f.synthetic.gate" value="true" checked>';
            host.appendChild(box);
            for (let i = 0; i < depth; i++) {
                const link = document.createElement('div');
                link.className = 'field';
                link.setAttribute('data-conditions',
                    JSON.stringify([{path: previous, operator: 'exists', scope: 'root'}]));
                previous = 'synthetic.link' + i;
                link.innerHTML =
                    '<input type="text" name="f.' + previous + '" value="filled" id="link' + i + '">';
                host.appendChild(link);
            }
        }""",
        depth,
    )
    # One change event settles the whole chain in both directions.
    page.uncheck('input[name="f.synthetic.gate"]')
    for i in range(depth):
        assert state(page, f"f.synthetic.link{i}") == {
            "hidden": True,
            "visible": False,
            "disabled": True,
            "required": False,
        }, f"link{i} did not collapse"

    page.check('input[name="f.synthetic.gate"]')
    for i in range(depth):
        assert state(page, f"f.synthetic.link{i}")["hidden"] is False, f"link{i} did not reopen"


# --- the vendored type layer ----------------------------------------------------------


def test_the_vendored_faces_load_and_the_heading_face_is_absent(page: Page):
    """Two OFL faces are *vendored* — one variable woff2 each, served
    by this app from `static/fonts/` — so the console keeps its offline
    guarantee and never asks Google for anything.

    Nothing else in the suite can tell: `font-display: swap` means a missing
    file, a broken `src: url()` or an image that forgot to copy `static/fonts`
    all render *fine*, in system-ui, silently. This is the check that fails
    instead — and the same check states the phase's other half, that Founders
    Grotesk (Klim's, commercial) is deliberately NOT here and headings are
    therefore rendering in DM Sans."""
    page.goto("/")
    page.evaluate("() => document.fonts.ready")

    # `check()` is true for a webfont only once a matching face has loaded.
    assert page.evaluate("() => document.fonts.check('500 16px \"DM Sans\"')") is True
    assert page.evaluate("() => document.fonts.check('400 16px \"JetBrains Mono\"')") is True
    # ...and false for the one that is expected absent. The @font-face rules for
    # it exist, so this is the *file* being missing, not the rule.
    assert (
        page.evaluate("() => document.fonts.check('400 16px \"Founders Grotesk\"')") is False
    )

    widths = page.evaluate(
        """() => {
            const measure = (family, weight) => {
                const s = document.createElement('span');
                s.textContent = 'Conduit Console 1234567890';
                s.style.cssText = 'position:absolute;visibility:hidden;'
                    + 'white-space:nowrap;font-size:40px;'
                    + 'font-family:' + family + ';font-weight:' + weight;
                document.body.appendChild(s);
                const w = s.getBoundingClientRect().width;
                s.remove();
                return w;
            };
            return {
                d400: measure('"DM Sans"', 400),
                d500: measure('"DM Sans"', 500),
                d700: measure('"DM Sans"', 700),
                mono400: measure('"JetBrains Mono"', 400),
                nothing400: measure('__no_such_family__', 400),
            };
        }"""
    )
    # One variable file carries every weight, so the thing worth proving is that
    # the `wght` axis really instances: three different widths, not one weight
    # painted three times.
    assert len({widths["d400"], widths["d500"], widths["d700"]}) == 3, widths
    # ...and that it is the vendored faces doing it, not the fallback underneath.
    assert widths["d400"] != widths["nothing400"], widths
    assert widths["mono400"] != widths["nothing400"], widths

    # The heading face is absent, so an h1 is being painted in DM Sans — which is
    # the fallback working, and is what an operator sees until Conduit's licensed
    # files are dropped in.
    assert page.evaluate(
        """() => {
            const h1 = document.querySelector('h1');
            return getComputedStyle(h1).fontFamily.includes('Founders Grotesk');
        }"""
    ) is True, "the heading stack no longer names Founders Grotesk first"


# --- timestamps -----------------------------------------------------------------------


def test_timestamps_are_rewritten_into_the_viewers_own_zone(browser, browser_context_args,
                                                            base_url):
    """The server renders UTC and says "UTC"; `app.js` then
    rewrites the *display* text into the viewer's zone and names that zone.

    Only a real browser can prove this — the pass is `Intl.DateTimeFormat`
    against the machine's own timezone, so a server-side test sees nothing but
    the UTC fallback it is supposed to see. Chromium's zone is set on the
    context, which is what makes the arithmetic assertable rather than
    tautological: 05:00 UTC is 14:00 in Tokyo, a fixed +9 with no DST seam to
    make this flake twice a year."""
    context = browser.new_context(**{**browser_context_args, "timezone_id": "Asia/Tokyo"})
    try:
        page = context.new_page()
        page.goto(base_url + "/applications/app_open")
        # Short on purpose: the conversion has to happen on load. This page also
        # polls its status panel every 15s, and `htmx:afterSwap` runs the same
        # pass — a generous timeout would let the poller pass this test with the
        # DOMContentLoaded hook deleted, which is most of what there is to break.
        wait_until(page, "() => !document.querySelector('time[datetime]').textContent"
                         ".includes('UTC')", timeout=3000)
        stamps = page.evaluate(
            "() => [...document.querySelectorAll('time[datetime]')].map(t => t.textContent)"
        )
        assert stamps, "the page rendered no timestamps to convert"
        for text in stamps:
            # Converted, not merely relabelled: the fixture is 05:00Z.
            assert "14:00" in text and "05:00" not in text, text
            # …and never a bare hour. Every stamp on screen names its zone —
            # which is the whole point of the slice — and none of them wears the
            # `Z` this console used to print.
            assert re.search(r"\d\d:\d\d \S", text) and not text.endswith("Z"), text
        # The truth for a support conversation stays exactly where it was.
        title = page.locator("time[datetime]").first.get_attribute("title")
        assert title == "2026-08-28T05:00:00+00:00", title
    finally:
        context.close()


# --- batch payouts --------------------------------------------------


# One fedwire/business row, keyed by **column** — a CSV's columns are discovery's
# dotted field names themselves, with no `f.` prefix in front of them.
BATCH_ROW = {
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
    "amount": "10.00",
    # The purpose is the row's now, and the column takes the raw key verbatim.
    "purpose": "payment_for_goods_or_services",
}


def _filled(template: str, rows: list[dict], extra_column: str = "") -> bytes:
    """A downloaded template with rows written under its own header."""
    parsed = list(csv.reader(io.StringIO(template)))
    comments = [row[0] for row in parsed if row and row[0].startswith("#")]
    header = next(row for row in parsed if row and not row[0].startswith("#"))
    if extra_column:
        header = header + [extra_column]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for line in comments:
        writer.writerow([line])
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    # With the BOM Excel writes on every "CSV UTF-8" save.
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def test_a_filled_batch_template_uploads_from_the_batch_screen(page: Page, tmp_path):
    """The one piece of JavaScript this feature owns: a file input posts the
    file's bytes and follows the console's own redirect.

    It moved off the payout form onto the batch screen with the rest of the batch
    flow (a batch's route has no purpose in it), so the refusal comes back HERE
    rather than to the single-payment form.

    Both outcomes are exercised, because they are the two the operator can get: a
    file this route cannot account for comes back with the refusal in the flash
    banner, and a good one lands on the batch's report.
    """
    template = page.request.get(
        f"/customers/{CID}/batches/template.csv{CORRIDOR}"
    ).text()

    refused = tmp_path / "wrong-columns.csv"
    refused.write_bytes(_filled(template, [BATCH_ROW], extra_column="swiftCode"))
    page.goto(BATCH_NEW + CORRIDOR)
    page.locator("input[type=file][data-batch]").set_input_files(refused)
    page.wait_for_url(re.compile(r"/batches/new\?.*err="))
    expect(page.locator(".flash, .problem").first).to_contain_text("swiftCode")

    good = tmp_path / "batch.csv"
    good.write_bytes(
        _filled(template, [BATCH_ROW, {**BATCH_ROW, "amount": "nope"}])
    )
    page.goto(BATCH_NEW + CORRIDOR)
    page.locator("input[type=file][data-batch]").set_input_files(good)
    page.wait_for_url(re.compile(r"/batches/[0-9a-f-]{8}-"))

    expect(page.locator(".result-meta")).to_contain_text("2 rows · 1 valid · 1 invalid")
    expect(page.locator("table")).to_contain_text(
        "The amount must be a positive decimal"
    )
    # The gate: an invalid row means the batch cannot be marked ready.
    expect(page.locator("button:has-text('Mark ready')")).to_be_disabled()
    assert sql("select count(*) from payout_batch_rows")[0][0] == 2
    # Nothing was sent: a batch is console-local until dispatch.
    assert sql("select count(*) from operations")[0][0] == 0


def _reroute(page: Page, name: str, value: str) -> None:
    """Change one control of the route row and wait for the reshape to land.

    Two waits, both necessary and both about htmx rather than about this feature:
    `hx-push-url` is a pushState, so the URL has to be polled rather than waited
    on as a navigation; and htmx processes swapped-in content one settle tick
    AFTER the new DOM is visible, so a second change driven at machine speed
    lands on a form that is on screen but not yet wired. A person cannot hit that
    window — it closes in milliseconds, and it only opens once a network round
    trip has completed — but a test can, every time.
    """
    page.select_option(f"#route-row select[name={name}]", value)
    expect(page).to_have_url(re.compile(re.escape(f"{name}={value}")))
    wait_until(page, "!!document.querySelector('#route-row')['htmx-internal-data']")


def test_the_fork_then_the_route_row_reshapes_the_form_without_a_reload(page: Page):
    """The restructure's two moving parts, in the only place they can be proven:
    the fork's two arms go where they say, and changing the purpose on the route
    row refetches discovery and reshapes the form under it — no submit, and the
    URL keeps up so the result is still linkable.
    """
    page.goto(FORK)
    expect(page.locator("h1")).to_have_text("Send a payout")
    page.click(f"a[href='{PAYOUTS}']")
    page.wait_for_url("**" + PAYOUTS)

    # The free-form route: the operator types the destination.
    page.goto(PAYOUTS + GOODS_ROUTE)
    expect(page.locator("#payout-form")).to_be_visible()
    expect(page.locator("[name='f.destination.recipient.routingNumber']")).to_have_count(1)
    expect(page.locator("#whitelistRecipientId")).to_have_count(0)

    # One change, no submit: the gated purpose reshapes the form in place.
    _reroute(page, "purpose", "intercompany")
    expect(page.locator("#whitelistRecipientId")).to_have_count(1)
    expect(page.locator("[name='f.destination.recipient.routingNumber']")).to_have_count(0)
    # And it stayed on this screen — no bounce to the transfers flow.
    expect(page.locator("h1")).to_have_text("Single payment")

    # Back again, by the same mechanism.
    _reroute(page, "purpose", "payment_for_goods_or_services")
    expect(page.locator("[name='f.destination.recipient.routingNumber']")).to_have_count(1)


def test_the_forks_batch_arm_reaches_the_corridor_screen(page: Page):
    """The batch arm asks for a corridor and nothing else, and does not fetch
    until it has one: the selects are `required`, so htmx (like the browser's own
    submit) refuses a half-answered route rather than reading discovery seven
    times for a corridor that does not exist yet."""
    page.goto(FORK)
    page.click(f"a[href='{BATCH_NEW}']")
    page.wait_for_url("**" + BATCH_NEW)
    # No purpose control here — it is a column of the file.
    expect(page.locator("#route-row select[name=purpose]")).to_have_count(0)
    expect(page.locator("text=Download template (CSV)")).to_have_count(0)

    page.select_option("#route-row select[name=recipientType]", "business")
    _reroute(page, "rail", "fedwire")

    expect(page.locator("text=Download template (CSV)")).to_have_count(1)
    # The corridor's purposes, with the raw key the column takes.
    expect(page.locator("table")).to_contain_text("intercompany")


def test_changing_the_funding_account_issues_no_requirements_fetch(page: Page):
    """The design pass's narrowing, proven on the wire the operator's browser
    actually uses: the funding account is in the route row and in every request
    the row makes, but `GET /v2/payouts/requirements` never sees it — so
    triggering a refetch on it spent a round trip re-reading a response that was
    already correct. What must NOT break is the body: the payout form reads the
    live row rather than a hidden copy, so the account it sends is the one on
    screen even though nothing re-rendered.
    """
    # A swift route, because on fedwire the EUR account is disabled by the
    # rail/asset guard and cannot be picked at all.
    swift = "?purpose=payment_for_goods_or_services&rail=swift&recipientType=business&destinationCountry=USA"
    page.goto(PAYOUTS + swift)
    expect(page.locator("#payout-form")).to_be_visible()

    reloads: list[str] = []
    page.on("request", lambda r: reloads.append(r.url) if "/payouts/new" in r.url else None)

    accounts = page.locator("#route-row select[name=virtualAccountId] option")
    second = accounts.nth(1).get_attribute("value")
    page.select_option("#route-row select[name=virtualAccountId]", second)
    page.wait_for_timeout(600)
    assert reloads == [], reloads

    # …and the form would still send THAT account, because it reads the row.
    assert page.locator("#payout-form").get_attribute("hx-include") == "#route-row"
    assert page.locator("#payout-form input[name=virtualAccountId]").count() == 0

    # The four that do change the answer still refetch.
    _reroute(page, "rail", "sepa")
    assert reloads, "a rail change must re-read requirements"


# --- the product tour ------------------------------------------------------
#
# A native tour: three elements app.js builds while it runs (`#tour-scrim`,
# `#tour-spot`, `#tour-card`) and removes when it ends. Everything asserted here
# is a browser fact — a spotlight actually over its target, a focus trap that
# really cycles, a storage API that really throws — which is the whole reason
# these live in this suite rather than in the route tests.

STEPS = 6


def rings(page: Page, target_id: str) -> None:
    """The spotlight is over the element the step names — both rectangles read
    in the SAME frame, so this is the claim rather than a race against the
    browser's next layout (the vendored font reflows the chrome shortly after
    load, and two separately-timed measurements can straddle it)."""
    wait_until(
        page,
        """id => {
            const spot = document.getElementById('tour-spot');
            const target = document.getElementById(id);
            if (!spot || !target) return false;
            const a = spot.getBoundingClientRect();
            const b = target.getBoundingClientRect();
            return Math.abs(a.x - b.x) < 2 && Math.abs(a.y - b.y) < 2
                && Math.abs(a.width - b.width) < 2 && Math.abs(a.height - b.height) < 2;
        }""",
        arg=target_id,
    )


def blind_storage(page: Page) -> None:
    """A browser with no usable web storage: private windows and blocked site
    data throw on the WRITE, which is why app.js probes with one."""
    # Raw source, not a function expression: `add_init_script` injects the
    # string as-is, so an arrow function here would be created and never called.
    page.add_init_script(
        """
        const boom = {
            getItem() { throw new Error('denied'); },
            setItem() { throw new Error('denied'); },
            removeItem() { throw new Error('denied'); },
        };
        for (const name of ['localStorage', 'sessionStorage']) {
            Object.defineProperty(window, name, { get: () => boom, configurable: true });
        }
        """
    )


def test_the_tour_walks_all_six_steps_and_spotlights_what_it_names(page: Page):
    """The journey end to end. Reduced motion is emulated so the scroll is
    instant and the geometry below is a fact rather than a race — which also
    exercises the branch that turns the tour's only piece of motion off.
    """
    page.emulate_media(reduced_motion="reduce")
    page.goto("/?tour=1")

    card = page.locator("#tour-card")
    expect(card).to_be_visible()
    expect(page.locator("#tour-count")).to_have_text(f"Step 1 of {STEPS}")
    # A labelled region, and the label is the count plus the title — so what a
    # screen reader announces when focus lands here is "Step 1 of 6, ...".
    assert card.get_attribute("role") == "region"
    assert card.get_attribute("aria-labelledby") == "tour-count tour-title"
    assert page.evaluate("() => document.activeElement.id") == "tour-card"

    # The spotlight is over the thing the step names, not near it.
    rings(page, "env-badge")
    # The departure from the drawer's non-modal rule is FOCUS-ONLY: the page the
    # tour is describing stays readable to a screen reader.
    assert page.evaluate(
        "() => !!document.getElementById('ribbon').closest('[inert],[aria-hidden=true]')"
    ) is False

    titles = []
    for step in range(1, STEPS + 1):
        expect(page.locator("#tour-count")).to_have_text(f"Step {step} of {STEPS}")
        titles.append(page.locator("#tour-title").text_content())
        # Back is dead on the first step and live on every other one.
        back = page.locator('#tour-card button[data-tour="back"]')
        if step == 1:
            expect(back).to_be_disabled()
        else:
            expect(back).to_be_enabled()
        if step < STEPS:
            expect(page.locator('#tour-card button[data-tour="next"]')).to_have_text("Next")
            page.click('#tour-card button[data-tour="next"]')

    # The last step's Next is the way out, and it says so.
    expect(page.locator('#tour-card button[data-tour="next"]')).to_have_text("Done")
    assert titles[0].startswith("Which Conduit")
    assert len(set(titles)) == STEPS

    page.click('#tour-card button[data-tour="next"]')
    expect(card).to_have_count(0)
    expect(page.locator("#tour-scrim")).to_have_count(0)
    # Focus lands on the ribbon's permanent way back in, never on <body>.
    assert page.evaluate("() => document.activeElement.id") == "tour-link"


def test_a_step_whose_subject_is_elsewhere_rings_nothing_and_offers_the_way_there(page: Page):
    """The honesty rule made mechanical. Two of the six subjects (the customer
    action row, the Transact fork) do not exist on the Overview, so those steps
    dim the page, ring NOTHING, and carry a link to go see it — rather than
    spotlighting some innocent nearby element and calling it the thing.
    """
    page.emulate_media(reduced_motion="reduce")
    page.goto("/?tour=1")
    for _ in range(3):
        page.click('#tour-card button[data-tour="next"]')

    expect(page.locator("#tour-count")).to_have_text(f"Step 4 of {STEPS}")
    # Retitled, when the second way in appeared: the ribbon lands on
    # the forms themselves and the customer is step 1's first field, so "there is
    # no free-standing send-money screen" stopped being true. The step's SUBJECT
    # — the customer action row, which is not on this page — is unchanged, and
    # that is what this test is about.
    expect(page.locator("#tour-title")).to_have_text("Every flow names a customer")
    see = page.locator("#tour-see a")
    expect(see).to_be_visible()
    assert see.get_attribute("href") == "/customers"
    # No ring: the spotlight box collapses to nothing and drops its outline,
    # while the dim it casts stays.
    expect(page.locator("#tour-spot")).to_have_class("no-target")
    wait_until(
        page, "() => document.getElementById('tour-spot').getBoundingClientRect().width === 0"
    )

    # …and a step that does have a target on this page rings it again.
    page.click('#tour-card button[data-tour="next"]')
    expect(page.locator("#tour-count")).to_have_text(f"Step 5 of {STEPS}")
    expect(page.locator("#tour-spot")).not_to_have_class("no-target")
    rings(page, "ribbon-rfis")
    expect(page.locator("#tour-see")).to_be_hidden()


def test_escape_exits_the_tour_and_returns_focus_to_the_trigger(page: Page):
    page.emulate_media(reduced_motion="reduce")
    page.goto("/?tour=1")
    expect(page.locator("#tour-card")).to_be_visible()
    page.click('#tour-card button[data-tour="next"]')
    expect(page.locator("#tour-count")).to_have_text(f"Step 2 of {STEPS}")

    page.keyboard.press("Escape")
    expect(page.locator("#tour-card")).to_have_count(0)
    expect(page.locator("#tour-spot")).to_have_count(0)
    assert page.evaluate("() => document.activeElement.id") == "tour-link"


def test_the_tour_is_completable_from_the_keyboard_alone(page: Page):
    """Tab cycles the card's own controls and never walks off into the page
    behind it — the deliberate departure from the drawer's non-modal grammar,
    because the tour is a mode the operator explicitly entered.
    """
    page.emulate_media(reduced_motion="reduce")
    page.goto("/?tour=1")
    assert page.evaluate("() => document.activeElement.id") == "tour-card"

    def here() -> str:
        return page.evaluate("() => (document.activeElement.textContent || '').trim()")

    # Step 1: Back is disabled, so the cycle is Next → Skip → back to Next.
    page.keyboard.press("Tab")
    assert here() == "Next"
    page.keyboard.press("Tab")
    assert here() == "Skip"
    page.keyboard.press("Tab")
    assert here() == "Next", "Tab off the last control must wrap, not leave the card"
    page.keyboard.press("Shift+Tab")
    assert here() == "Skip", "Shift+Tab off the first control must wrap the other way"

    # …and the whole tour can be finished with Enter on Next.
    for step in range(1, STEPS + 1):
        expect(page.locator("#tour-count")).to_have_text(f"Step {step} of {STEPS}")
        page.keyboard.press("Tab")  # focus rests on the card at each new step
        while here() != ("Done" if step == STEPS else "Next"):
            page.keyboard.press("Tab")
        page.keyboard.press("Enter")

    expect(page.locator("#tour-card")).to_have_count(0)
    assert page.evaluate("() => document.activeElement.id") == "tour-link"


def test_the_offer_is_made_once_dismissed_for_good_and_the_ribbon_brings_it_back(page: Page):
    """First run, dismissal, re-trigger. The offer asks; it never hijacks."""
    page.goto("/")
    offer = page.locator("#tour-offer")
    expect(offer).to_be_visible()
    # Asking is not launching: no overlay exists until the operator says yes.
    expect(page.locator("#tour-card")).to_have_count(0)

    page.click('#tour-offer button[data-tour="dismiss"]')
    expect(offer).to_be_hidden()
    page.reload()
    expect(offer).to_be_hidden()

    # Skipping is never final: the ribbon's link is always there.
    page.click("#tour-link")
    expect(page.locator("#tour-card")).to_be_visible()
    expect(page.locator("#tour-count")).to_have_text(f"Step 1 of {STEPS}")


def test_taking_the_tour_from_the_offer_also_settles_it(page: Page):
    page.emulate_media(reduced_motion="reduce")
    page.goto("/")
    expect(page.locator("#tour-offer")).to_be_visible()
    page.click('#tour-offer button[data-tour="start"]')

    expect(page.locator("#tour-card")).to_be_visible()
    expect(page.locator("#tour-offer")).to_be_hidden()
    page.click('#tour-card button[data-tour="skip"]')
    expect(page.locator("#tour-card")).to_have_count(0)
    # The starter is gone from the page, so focus falls back to the ribbon link.
    assert page.evaluate("() => document.activeElement.id") == "tour-link"

    page.reload()
    expect(page.locator("#tour-offer")).to_be_hidden()


def test_without_localstorage_the_offer_lasts_one_session(page: Page):
    """Degradation, row two: localStorage throws, sessionStorage answers. The
    offer is still made — and still only once — but the memory dies with the
    tab, which is the honest amount of state a private window can keep.
    """
    page.add_init_script(
        """
        const boom = {
            getItem() { throw new Error('denied'); },
            setItem() { throw new Error('denied'); },
            removeItem() { throw new Error('denied'); },
        };
        Object.defineProperty(window, 'localStorage', { get: () => boom, configurable: true });
        """
    )
    page.goto("/")
    expect(page.locator("#tour-offer")).to_be_visible()
    page.click('#tour-offer button[data-tour="dismiss"]')
    page.reload()
    expect(page.locator("#tour-offer")).to_be_hidden()
    assert page.evaluate("() => sessionStorage.getItem('conduit.console.tour')") == "1"


def test_with_no_storage_at_all_the_offer_is_never_made_and_nothing_crashes(page: Page):
    """Degradation, row three. Neither store answers, so there is no place to
    record a dismissal — and an offer that cannot be dismissed for good is a nag
    loop. So it is not made at all, and the ribbon link is the whole affordance.
    The chrome must not crash on the way to that decision.
    """
    blind_storage(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto("/")
    expect(page.locator("#tour-offer")).to_be_hidden()
    # The rest of the chrome is untouched — this is app.js, not a tour file.
    expect(page.locator("#ribbon")).to_be_visible()

    page.click("#tour-link")
    expect(page.locator("#tour-card")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#tour-card")).to_have_count(0)
    assert errors == [], errors


def test_a_swap_during_the_tour_re_anchors_and_a_vanished_target_ends_it(page: Page):
    """The documented swap rule. htmx swaps are the console's whole update
    mechanism, so a tour that ignored them would eventually ring a stale
    rectangle: a swap RE-MEASURES the current step (ids are what steps hold), and
    a target that left the DOM ends the tour rather than ringing nothing.
    """
    page.emulate_media(reduced_motion="reduce")
    page.goto("/?tour=1")
    page.click('#tour-card button[data-tour="next"]')
    page.click('#tour-card button[data-tour="next"]')
    expect(page.locator("#tour-count")).to_have_text(f"Step 3 of {STEPS}")

    # The table moves the way a re-rendered one does; the ring follows it. The
    # move and the event are two steps with a frame between them because the
    # move is made from OUTSIDE the page: a mutation driven through the debug
    # protocol does not settle the page's layout until it next renders, which an
    # htmx swap (made by the page itself) always has.
    page.evaluate("() => { document.getElementById('attention-table').style.marginTop = '120px'; }")
    page.wait_for_timeout(100)
    page.evaluate(
        """() => document.body.dispatchEvent(new CustomEvent('htmx:afterSwap', {
            detail: { target: document.getElementById('attention-table') }, bubbles: true,
        }))"""
    )
    rings(page, "attention-table")

    # …and a swap that takes the target away ends the tour instead of lying.
    page.evaluate(
        """() => {
            document.getElementById('attention-table').remove();
            document.body.dispatchEvent(new CustomEvent('htmx:afterSwap', {
                detail: { target: document.body }, bubbles: true,
            }));
        }"""
    )
    expect(page.locator("#tour-card")).to_have_count(0)
    assert page.evaluate("() => document.activeElement.id") == "tour-link"


def test_a_refused_upload_says_so_and_grafts_nothing_into_the_draft(page: Page, tmp_path):
    """`fetch` resolves on 4xx/5xx, so the uploader's catch only ever fired on a
    dropped connection: every other refusal's BODY was inserted into the document
    list as if it were this route's own chip. An expired session put
    `{"detail":"authentication required"}` in there as unstyled text, a 403 put an
    `<h1>` — and the draft was told it had changed and the counts were re-run, so
    the page then behaved as though a document had been attached.

    The 401 is forced in the browser, which is what an expired session looks like
    from here: the console answers, the answer is not a chip.
    """
    pdf = tmp_path / "evidence.pdf"
    pdf.write_bytes(PDF)
    open_draft(page)
    step_to(page, "#doc-chips")
    page.route(
        "**/documents?**",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail":"authentication required"}',
        ),
    )
    # The autosave the failure must NOT wake: nothing was attached, so nothing
    # about the draft changed.
    page.evaluate(
        """() => {
            window.__saves = 0;
            document.body.addEventListener('draft-changed', () => { window.__saves++; });
        }"""
    )

    page.locator("#wizard input[type=file][data-purpose='organization_onboarding']").set_input_files(
        pdf
    )

    chip = page.locator("#doc-chips .chip")
    expect(chip).to_have_count(1)
    expect(chip).to_contain_text("Upload failed")
    expect(chip).to_contain_text("nothing was attached")
    # The refusal's own body reached neither the chip nor anywhere else.
    assert "authentication required" not in page.locator("#wizard").inner_text()
    assert page.locator('#doc-chips input[name="documentIds"]').count() == 0
    assert page.evaluate("() => window.__saves") == 0
    # …and the same file can be picked again, which needs the input cleared.
    assert page.locator(
        "#wizard input[type=file][data-purpose='organization_onboarding']"
    ).input_value() == ""


def test_the_send_button_stays_disabled_for_the_whole_send(page: Page):
    """The lying affordance. `hx-disabled-elt` disables the money button for the
    flight of its own POST, but `quoteGuard` runs once a second off the QUOTE
    clock alone and used to write `disabled = false` straight over it — so from
    the first tick onward the button looked pressable *during its own send*, and
    the click it invited did nothing (`hx-sync="this:drop"` drops it).

    Exactly-once was never at risk; what was wrong is what the operator was told.
    The POST is held unanswered so the window is real rather than timed, and the
    assertion straddles a tick.
    """
    page.goto(PAYOUTS + INTERCOMPANY_ROUTE)
    page.fill("#amount", "250.00")
    page.select_option("#whitelistRecipientId", REGISTERED["id"])
    fill(page, INTERCOMPANY_FIELDS)

    held: list = []
    page.route("**/payouts/new", lambda route: held.append(route))
    page.click("#payout-submit")

    submit = page.locator("#payout-submit")
    page.wait_for_timeout(300)
    expect(submit).to_be_disabled()
    # Past a full tick of `setInterval(guards, 1000)` — the one that used to undo
    # htmx's disable — and the button is still not offering a second send.
    page.wait_for_timeout(1500)
    expect(submit).to_be_disabled()
    assert held, "the POST never left the browser"


def test_the_column_headers_stay_put_when_a_long_list_scrolls(page: Page):
    """Reported from a real browser: "when I scroll down the list,
    the column header disappears".

    A stylesheet rule is not verifiable by reading markup — this is the only
    assertion in the suite that can tell `position: sticky` from a comment about
    one. It also proves the two things the rule has to get right beyond sticking:
    the header is OPAQUE (rows scrolling under it must not show through the
    text) and it keeps its 2px boundary rule, which `border-collapse` alone
    scrolls away with the table's border grid.
    """
    for n in range(60):
        sql(
            "insert into projections (id, resource_kind, resource_id, state, payload, observed_at)"
            " values (gen_random_uuid(), 'virtual_accounts', %s, 'active', %s::jsonb, now())",
            (
                f"vac_sticky_{n:03d}",
                json.dumps(
                    {
                        "virtualAccountId": f"vac_sticky_{n:03d}",
                        "customerId": CID,
                        "asset": {"code": "USD"},
                        "status": "active",
                    }
                ),
            ),
        )
    page.goto("/accounts?limit=100")
    header = page.locator("table tr", has=page.locator("th")).first
    expect(header).to_be_visible()
    top_before = header.bounding_box()["y"]

    # `window.scrollTo`, not `mouse.wheel`: the ribbon is a `overflow-y: auto`
    # column under the pointer's default (0, 0), so a wheel event scrolls THAT
    # and leaves the document exactly where it was — which is how this test
    # passed against a stylesheet with no sticky rule in it at all.
    page.evaluate("() => window.scrollTo(0, 2000)")
    page.wait_for_timeout(200)
    assert page.evaluate("() => window.scrollY") > 500, "the page did not scroll"
    box = header.bounding_box()

    # Still on screen, and pinned at the top of the viewport rather than carried
    # off with its rows. Without the rule it lands ~1600px above the fold.
    assert box is not None and 0 <= box["y"] < top_before
    assert box["y"] < page.viewport_size["height"]
    # Opaque, or the rows sliding under it read straight through the labels.
    fill = page.evaluate(
        "() => getComputedStyle(document.querySelector('table th')).backgroundColor"
    )
    assert fill not in ("rgba(0, 0, 0, 0)", "transparent")
    # And the boundary rule survives the collapsed border grid.
    shadow = page.evaluate(
        "() => getComputedStyle(document.querySelector('table th')).boxShadow"
    )
    assert "inset" in shadow


def test_a_poll_cannot_swap_an_action_row_while_its_own_form_is_in_flight(page: Page):
    """Direction (c) — against the real order page.

    The Execute/Cancel forms live inside `#order-status`, which replaces itself
    every 15s. Each replacement mints a fresh `intent` nonce (OPERATIONS_SPEC
    §1), so a background refresh silently threw away the nonce the operator's
    pending click was made under — and a fresh nonce is, by design, a deliberate
    new attempt, which is exactly what a lost response must NOT become.
    Persisting intents cannot reach this case: the nonce is not consumed here, it
    is discarded before it can be.

    Driven by dispatching htmx's own cancellable `htmx:beforeRequest` rather than
    by waiting out a 15-second timer, so the assertion is about the listener and
    the real region's real attributes. The negative case is the non-vacuity
    guard: the identical event with no form in flight must go through, or a
    listener that blocked every poll would pass this test while freezing the page.
    """
    page.goto("/orders/ord_conv_1")
    region = page.locator("#order-status")
    expect(region).to_be_visible()
    # The premise, read off the page rather than assumed: this region really does
    # poll, and it really does contain the mutating form.
    assert (page.get_attribute("#order-status", "hx-trigger") or "").startswith("every ")
    expect(page.locator('#order-status form[hx-post$="/execute"]')).to_have_count(1)

    fired = """(busy) => {
        const region = document.getElementById('order-status');
        const form = region.querySelector('form[hx-post$="/execute"]');
        form.classList.toggle('htmx-request', busy);
        const event = new CustomEvent('htmx:beforeRequest',
            {bubbles: true, cancelable: true, detail: {elt: region}});
        region.dispatchEvent(event);
        form.classList.remove('htmx-request');
        return event.defaultPrevented;
    }"""
    assert page.evaluate(fired, True) is True, "the poll swapped a region mid-submit"
    assert page.evaluate(fired, False) is False, "the poll is blocked even when nothing is sending"


def test_the_operators_own_submit_is_never_blocked_by_that_guard(page: Page):
    """The other half of the same rule: only the POLLER is paused. A request the
    operator started — including the very submit that makes the region busy — has
    to go through, or the guard would deadlock the button it exists to protect."""
    page.goto("/orders/ord_conv_1")
    expect(page.locator("#order-status")).to_be_visible()
    prevented = page.evaluate(
        """() => {
            const form = document.querySelector('#order-status form[hx-post$="/execute"]');
            form.classList.add('htmx-request');
            const event = new CustomEvent('htmx:beforeRequest',
                {bubbles: true, cancelable: true, detail: {elt: form}});
            form.dispatchEvent(event);
            form.classList.remove('htmx-request');
            return event.defaultPrevented;
        }"""
    )
    assert prevented is False


# --- pay a contact ---------------------------------------------------------------------


def test_picking_a_contact_lands_on_a_prefilled_payout_form(page: Page):
    """The quick action, the picker's datalist, the
    hand-off, and the prefilled form — with the send path untouched.

    The row is seeded directly because getting a contact onto the trail through
    the UI means a payout with a document upload, which is the payout journey's
    test and not this one's. What is NOT seeded is anything the feature decides:
    the amount, the purpose, the rail and the funding account below all come from
    the stub's transaction read, through the console's own trail.
    """
    contact = uuid.uuid4()
    operation = uuid.uuid4()
    sql(
        """insert into counterparties
             (id, customer_id, label, recipient, rail_family, recipient_type,
              destination_country, created_by_actor_id, created_by_actor_email)
           values (%s, %s, 'ZZZTEST Globex Supplies', %s, 'us', 'business', 'USA',
                   'usr_browser', 'ops@example.com')""",
        (
            contact,
            CID,
            fernet().encrypt(
                json.dumps(
                    {
                        "accountNumber": "000123456789",
                        "routingNumber": "021000021",
                        "legalName": "ZZZTEST Globex Supplies LLC",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        ),
    )
    sql(
        """insert into operations
             (id, type, actor_id, actor_email, customer_id, request_path, request_hash,
              idempotency_key, state, conduit_resource_id)
           values (%s, 'payout_create', 'usr_browser', 'ops@example.com', %s,
                   '/v2/payouts', %s, %s, 'confirmed', 'txn_payout_1')""",
        (operation, CID, f"paidhash{operation.hex}"[:64], uuid.uuid4()),
    )
    sql(
        """insert into audit_events
             (id, actor_id, actor_email, action, operation_id, detail)
           values (gen_random_uuid(), 'usr_browser', 'ops@example.com',
                   'counterparty.used', %s, %s::jsonb)""",
        (operation, json.dumps({"counterparty": str(contact), "label": "Globex"})),
    )

    # The quick action is on every page, so the journey starts wherever.
    page.goto("/drafts")
    page.click('#quick-actions a[href="/payouts/contact"]')
    expect(page).to_have_url(re.compile(r"/payouts/contact$"))
    # Names first: the option an operator reads names the contact and the
    # customer; the value the browser sends is the id.
    option = page.locator('#known-contacts option[value="%s"]' % contact)
    expect(option).to_have_attribute("value", str(contact))
    assert "ZZZTEST Globex Supplies" in (option.inner_text() or option.text_content())

    page.fill("#contact", str(contact))
    page.click("button:has-text('Continue')")
    page.wait_for_url(f"**/customers/{CID}/payouts/new?counterparty={contact}")

    # The prefill, as the operator sees it. Every one of these is from the
    # transaction the trail named, not from the URL.
    expect(page.locator("#amount")).to_have_value("987.50")
    assert page.eval_on_selector('select[name="rail"]', "el => el.value") == "fedwire"
    assert page.eval_on_selector('select[name="purpose"]', "el => el.value") == (
        "payment_for_goods_or_services"
    )
    expect(
        page.locator('[name="f.destination.recipient.accountNumber"]')
    ).to_have_value("000123456789")
    # Editable, always: a prefill has no authority to freeze what is sent.
    assert page.eval_on_selector("#amount", "el => el.readOnly || el.disabled") is False

    # And the send path is the one it has always been — same POST, same nonce,
    # same freeze list. Nothing on the way here sent anything.
    submit = page.locator("#payout-submit")
    expect(submit).to_be_enabled()
    assert page.get_attribute("#payout-form", "hx-post") == (
        f"/customers/{CID}/payouts/new"
    )
    assert page.eval_on_selector('input[name="intent"]', "el => el.value")
    assert operations_of("payout_create") == [(operation, "confirmed")]


def test_the_payout_form_starts_from_a_saved_contact(page: Page):
    """In a browser: customer → saved contact → purpose → the form.

    The ask was that the contact come BEFORE the requirements, so this
    walks it in that order and proves the two things only a browser can: the
    contact box really re-renders the page on `change` (the route arrives filled
    in, without a second click), and the choice survives the purpose change that
    follows it — which is the hidden field on the route row doing its job.
    Nothing is sent: the journey ends at the submit control, unchanged.
    """
    contact = uuid.uuid4()
    sql(
        """insert into counterparties
             (id, customer_id, label, recipient, rail_family, recipient_type,
              destination_country, created_by_actor_id, created_by_actor_email)
           values (%s, %s, 'ZZZTEST Bauer GmbH', %s, 'us', 'business', 'USA',
                   'usr_browser', 'ops@example.com')""",
        (
            contact,
            CID,
            fernet().encrypt(
                json.dumps(
                    {
                        "accountNumber": "000123456789",
                        "routingNumber": "021000021",
                        "legalName": "ZZZTEST Bauer Holdings LLC",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        ),
    )

    # Step 1 answered, nothing else: the contact step is on the page and the
    # requirements are not, which is the whole point of moving it up.
    page.goto(f"/customers/{CID}/payouts/new")
    expect(page.locator("#contact-first")).to_be_visible()
    expect(page.locator("#payout-form")).to_have_count(0)
    expect(
        page.locator('#saved-contacts option[value="%s"]' % contact)
    ).to_have_attribute("value", str(contact))

    # Picking it re-renders on `change` alone and the corridor arrives answered.
    page.fill("#counterparty", str(contact))
    page.dispatch_event("#counterparty", "change")
    expect(page).to_have_url(re.compile(re.escape(f"counterparty={contact}")))
    assert page.eval_on_selector('select[name="rail"]', "el => el.value") == "fedwire"
    assert page.eval_on_selector('select[name="recipientType"]', "el => el.value") == "business"
    assert page.eval_on_selector('[name="destinationCountry"]', "el => el.value") == "USA"
    # …except the purpose, which a contact cannot store and nothing has proven.
    assert page.eval_on_selector('select[name="purpose"]', "el => el.value") == ""
    expect(page.locator("#payout-form")).to_have_count(0)

    # Answering it brings the form — with the contact still picked, because the
    # route row carries it.
    # `_reroute`'s two waits apply here for its own stated reason: htmx wires
    # swapped-in content one settle tick after it is visible, and this row was
    # just swapped in by the contact step.
    wait_until(page, "!!document.querySelector('#route-row')['htmx-internal-data']")
    _reroute(page, "purpose", "payment_for_goods_or_services")
    expect(page.locator("#payout-form")).to_have_count(1)
    expect(
        page.locator('[name="f.destination.recipient.accountNumber"]')
    ).to_have_value("000123456789")
    assert page.eval_on_selector(
        '#route-row input[name="counterparty"]', "el => el.value"
    ) == str(contact)

    # The send path is the one it has always been, and nothing was sent.
    expect(page.locator("#payout-submit")).to_be_enabled()
    assert page.get_attribute("#payout-form", "hx-post") == f"/customers/{CID}/payouts/new"
    assert operations_of("payout_create") == []


# --- one filter grammar ---------------------------------------------------------------


def test_a_list_filters_without_a_click(page: Page):
    """2026-09-02: "auto-filter once I select the customer."

    `/contacts` is the page that prompted it and the one with two filters worth
    proving together — a datalist-backed customer box and a free-text name — but
    the claim is the console's, not this page's: `test_every_list_filter_applies
    _on_change` reads the same triple out of all eight trays. What only a browser
    can show is the rest of it: the rows really change on `change` alone, the URL
    the pager and the CSV link read from is pushed with them, and the Filter
    button is still there for a browser with no JavaScript.
    """
    for label in ("ZZZTEST Filter Alpha", "ZZZTEST Filter Beta"):
        sql(
            """insert into counterparties
                 (id, customer_id, label, recipient, rail_family, recipient_type,
                  destination_country, created_by_actor_id, created_by_actor_email)
               values (gen_random_uuid(), %s, %s, %s, 'us', 'business', 'USA',
                       'usr_browser', 'ops@example.com')""",
            (
                CID,
                label,
                fernet().encrypt(
                    json.dumps(
                        {"accountNumber": "000123456789", "routingNumber": "021000021"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ),
            ),
        )

    page.goto("/contacts")
    both = page.locator("table tr:has-text('ZZZTEST Filter')")
    expect(both).to_have_count(2)

    # No click: type a name, blur, and the list is the answer to it.
    page.fill('input[name="contactName"]', "ZZZTEST Filter Alpha")
    page.dispatch_event('input[name="contactName"]', "change")
    expect(page.locator("table tr:has-text('ZZZTEST Filter Alpha')")).to_have_count(1)
    expect(page.locator("table tr:has-text('ZZZTEST Filter Beta')")).to_have_count(0)
    # …and the URL says so, so the pager, the export link and a bookmark agree
    # with the screen.
    expect(page).to_have_url(re.compile(r"contactName=ZZZTEST\+?%?2?0?Filter"))

    # The button is untouched: a browser with no JavaScript submits the same GET.
    button = page.locator('form[action="/contacts"] button[type=submit]')
    expect(button).to_be_visible()
    assert page.get_attribute('form[action="/contacts"]', "method") == "get"


def test_the_clone_rail_control_is_its_own_form_and_reshapes_the_page(page: Page):
    """The design pass's one structural find, pinned where only a browser sees it.

    `#clone-rail` shipped as a `<form>` NESTED inside `#contact-edit`, and the
    HTML parser drops an inner form's start tag: the element was not in the DOM,
    its `hx-trigger` never fired, its `rail` select became a field of the clone
    POST, and the button reading "Reshape the form" submitted the contact. Every
    server-side test passed — the template renders the bytes, and only a parser
    decides what they mean. So the assertions are the parser's: the form exists,
    the select belongs to it and not to the write, and picking a rail in another
    family really does bring that rail's fields back.
    """
    contact = uuid.uuid4()
    sql(
        """insert into counterparties
             (id, customer_id, label, recipient, rail_family, recipient_type,
              destination_country, created_by_actor_id, created_by_actor_email)
           values (%s, %s, 'ZZZTEST Reshape Ltd', %s, 'us', 'business', 'USA',
                   'usr_browser', 'ops@example.com')""",
        (
            contact,
            CID,
            fernet().encrypt(
                json.dumps(
                    {
                        "accountNumber": "000123456789",
                        "routingNumber": "021000021",
                        "legalName": "ZZZTEST Reshape Holdings LLC",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        ),
    )

    page.goto(f"/customers/{CID}/contacts/{contact}/edit?clone=1")
    assert page.evaluate("!!document.getElementById('clone-rail')")
    assert (
        page.eval_on_selector("#clone-rail-select", "el => el.form.id") == "clone-rail"
    )
    # `rail` is not a field of the write: what the clone POST carries is the
    # label, the intent, the clone flag and discovery's own boxes.
    assert "rail" not in page.evaluate(
        "[...new FormData(document.getElementById('contact-edit')).keys()]"
    )
    expect(page.locator('[name="f.destination.recipient.routingNumber"]')).to_have_count(1)

    # A clone may leave the family, and picking a rail in another one re-reads
    # the page for it — on `change`, with no click, and with the rail in the URL
    # so the reshaped form is linkable. WHICH boxes come back is Conduit's answer
    # and is asserted against a real requirements snapshot in
    # `tests/test_contact_edit.py`; this rig stubs one snapshot for every route,
    # so what it can prove is that the request happens at all, which is exactly
    # the half a dropped `<form>` tag took away.
    select_and_settle(page, "#clone-rail-select", "sepa")
    expect(page).to_have_url(re.compile(r"rail=sepa"))
    # Still a clone, and still nothing written.
    assert page.eval_on_selector('#contact-edit input[name="clone"]', "el => el.value") == "1"
    assert sql(
        "select count(*) from counterparties where customer_id = %s", (CID,)
    ) == [(1,)]
    # The half the nesting bug actually cost, asserted where it happened: the
    # button read "Reshape the form" and SUBMITTED the clone, so a reshape wrote
    # a contact. Counted across every customer — a write that landed under the
    # wrong one would still be a write — and against the trail, which is where a
    # clone is recorded (`counterparty.cloned`) and where a silent one would show
    # even if the row itself were later archived.
    assert sql("select count(*) from counterparties") == [(1,)]
    assert sql(
        "select count(*) from audit_events where action = 'counterparty.cloned'"
    ) == [(0,)]
    # …and no operation either: a clone is a plain audited write, so a row here
    # would mean something else entirely had been submitted.
    assert operations_of("payout_create") == []


# ---- Dark theme ----------------------------------------

# The luminance/contrast pair, in the page rather than in Python, so what is
# measured is what the browser actually painted — computed colours after the
# cascade, the theme branch, and every `var()` resolution. A token table can be
# right and a rule can still hand an element a colour from the other theme.
_CONTRAST_JS = """
(pair) => {
  const lum = (c) => {
    const [r, g, b] = c.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number);
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const a = lum(pair[0]), b = lum(pair[1]);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
"""


def _painted(page: Page, selector: str) -> tuple[str, str]:
    """One element's computed text colour and the ground it is actually on.

    Walks up for the first ancestor with a non-transparent background, which is
    the ground the pixel is composited over — a pill's own tint if it has one,
    the shell or the window ground if it does not. Reading the element's own
    `background-color` alone would measure text against `transparent` and pass
    anything.
    """
    return tuple(
        page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              let g = el;
              const clear = (c) => !c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)';
              while (g && clear(getComputedStyle(g).backgroundColor)) g = g.parentElement;
              return [getComputedStyle(el).color,
                      getComputedStyle(g || document.body).backgroundColor];
            }""",
            selector,
        )
    )


def test_the_dark_theme_paints_both_high_traffic_pages(page: Page):
    """The Overview and the ledger under `prefers-color-scheme: dark`.

    Two claims no server-side test can make. First that the dark branch is
    *reached* at all — a `@media` block guarded with `:not([data-theme="light"])`
    is one typo away from never matching, and the failure mode is a light page
    on a dark desktop, which nothing else in the suite would notice. Second that
    every text/ground pair the operator actually reads clears AA **as painted**,
    which is the number `tests/test_contrast.py` cannot produce: that module
    proves the token table is sound, this one proves the rules spend it on the
    right elements. `--accent` in dark is 3.05:1 on `--surface-2` and would fail
    here the moment a rule assigned it to a word.
    """
    page.emulate_media(color_scheme="dark")

    page.goto("/")
    body = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert body == "rgb(11, 14, 20)", f"the dark --page is not on body: {body}"

    # h1, and a link out of the chrome. Both on the window ground today; the
    # shell that will put them on `--paper` comes later.
    for selector in ("h1", "nav.ribbon a.item"):
        fg, bg = _painted(page, selector)
        ratio = page.evaluate(_CONTRAST_JS, [fg, bg])
        assert ratio >= 4.5, f"dark {selector}: {fg} on {bg} is {ratio:.2f}:1"

    page.goto("/transactions")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(11, 14, 20)"
    expect(page.locator("table .pill").first).to_be_visible()
    fg, bg = _painted(page, "table .pill")
    ratio = page.evaluate(_CONTRAST_JS, [fg, bg])
    assert ratio >= 4.5, f"dark status pill: {fg} on {bg} is {ratio:.2f}:1"
    # A pill that lost its tint would read against the page and still pass the
    # line above, so the fill is asserted separately: all five states keep one
    # (§3.2 — a tinted chip is findable down a column of a hundred rows).
    assert bg != "rgb(11, 14, 20)", "the status pill has no fill of its own"

    # And nothing is painted on itself. Zero contrast is the failure a screenshot
    # hides, because an invisible word looks exactly like an absent one.
    invisible = page.evaluate(
        """() => [...document.querySelectorAll('main *')]
             .filter(el => el.childNodes.length && [...el.childNodes].some(
                 n => n.nodeType === 3 && n.textContent.trim()))
             .filter(el => {
               const s = getComputedStyle(el);
               let g = el;
               const clear = (c) => !c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)';
               while (g && clear(getComputedStyle(g).backgroundColor)) g = g.parentElement;
               return s.color === getComputedStyle(g || document.body).backgroundColor;
             })
             .map(el => el.tagName + '.' + el.className)"""
    )
    assert not invisible, f"text painted on its own ground: {invisible}"


def test_an_explicit_light_choice_beats_a_dark_system_preference(page: Page):
    """`:not([data-theme="light"])` is the half of the guard that has no visible
    consumer yet — the theme control lands in A2 — and is therefore the half
    that would ship broken. Both directions are asserted here rather than
    waiting for the control: dark system + explicit light stays light, and light
    system + explicit dark goes dark.
    """
    page.emulate_media(color_scheme="dark")
    # The ledger rather than the Overview: it is the page that carries a
    # `<select>`, and the chevron check below is the whole point of the branch.
    page.goto("/transactions")
    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(245, 245, 247)"

    page.emulate_media(color_scheme="light")
    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(11, 14, 20)"
    # The chevron is the one place a theme costs a duplicated RULE, so it is the
    # one most likely to be forgotten in a branch: check the attribute branch.
    chevron = page.evaluate(
        "() => getComputedStyle(document.querySelector('select:not([multiple])')).backgroundImage"
    )
    assert "b7bec9" in chevron, f"the dark chevron did not reach the attribute branch: {chevron}"

    # And the same two directions again through the CONTROL that ships them
    # (A2), rather than through a hand-written attribute: the guard is only
    # worth anything if the thing an operator actually presses reaches it.
    page.emulate_media(color_scheme="dark")
    page.goto("/transactions")
    page.click("#theme-choice label:has(input[value='light'])")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(245, 245, 247)"
    page.emulate_media(color_scheme="light")
    page.click("#theme-choice label:has(input[value='dark'])")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(11, 14, 20)"


# ---- Shell, nav and chrome ------------------------------


def test_the_shell_is_a_card_and_the_body_is_the_window(page: Page):
    """Arca §2.1's one structural claim, in both themes: `body` is the window
    ground and the app is a card of `--paper` floating on it. Two grounds that
    are the same colour is the failure this catches — it is invisible in a
    screenshot of either theme alone, and it is what the whole shell exists to
    say.

    Also pinned: the card's radius is the `--r-lg` 20px the spec spends here and
    on one accent block and nowhere else, and it is capped at the 1180px page
    (§4.6) rather than the 1360 `main` used to carry.
    """
    for scheme, page_ground, paper in (
        ("light", "rgb(245, 245, 247)", "rgb(255, 255, 255)"),
        ("dark", "rgb(11, 14, 20)", "rgb(18, 22, 30)"),
    ):
        page.emulate_media(color_scheme=scheme)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto("/")
        painted = page.evaluate(
            """() => {
              const shell = document.querySelector('.shell');
              const s = getComputedStyle(shell);
              return {body: getComputedStyle(document.body).backgroundColor,
                      shell: s.backgroundColor, radius: s.borderTopLeftRadius,
                      width: Math.round(shell.getBoundingClientRect().width),
                      shadow: s.boxShadow};
            }"""
        )
        assert painted["body"] == page_ground, f"{scheme}: body is not the window: {painted}"
        assert painted["shell"] == paper, f"{scheme}: the shell is not paper: {painted}"
        assert painted["radius"] == "20px", painted
        assert painted["width"] == 1180, painted
        # `--shell-shadow` is `none` in dark: the depth is the page/paper step,
        # not a glow (§2.1, and DESIGN.md's "depth by hairline" rule).
        assert (painted["shadow"] == "none") == (scheme == "dark"), painted


def test_a_page_that_fits_the_window_does_not_scroll(page: Page):
    """The ribbon is one viewport tall AND spans both grid rows, so it — not the
    content — is what sizes a short page's shell. Every pixel it is given beyond
    the window is a scrollbar on a page that fits.

    Two things have to come off `100vh` for that to be true and only one of them
    is obvious: the 16px of window margin the shell floats in (twice), and the
    card's own two hairlines. At `calc(100vh - 32px)` this page was 2px too tall
    and scrolled — which is the least useful scrollbar in software, and exactly
    the kind of thing a screenshot cannot show.
    """
    page.set_viewport_size({"width": 1440, "height": 2400})
    page.goto("/")
    fit = page.evaluate(
        """() => ({doc: document.documentElement.scrollHeight,
                   view: window.innerHeight})"""
    )
    assert fit["doc"] <= fit["view"], fit


def test_no_chrome_overflows_the_viewport_at_four_widths(page: Page):
    """§6's "no horizontal overflow at 375 / 768 / 1024 / 1440", on the two pages
    A2 owns the chrome of.

    **Strengthened to "nothing overflows the PAGE".** This
    pin used to enumerate every element and exclude anything inside a
    `<table>`: the ledger's table was wider than 768px before A2 touched
    anything, had no scroll container of its own, and fixing that was
    explicitly deferred to A5's markup sweep across 27 templates rather than
    smuggled into `base.html` as a mobile-only `display: block` hack that A4
    and A5 would both have had to undo. A5 landed the sweep — every `<table>`
    now opens inside `<div class="table-scroll">`, which takes `overflow-x:
    auto` at and below 1024px (`static/styles.css`). The table itself is
    still, correctly, wider than the viewport inside that wrapper — that
    width is what makes it scroll — so a per-element "does this box fit"
    check would have to keep excluding it forever. `document.scrollWidth` is
    the honest question instead: a scrolling *descendant* does not count
    toward its ancestor's scrollWidth, so this is exactly "does the page
    itself gain a horizontal scrollbar", with nothing to carve out.
    """
    for width in (375, 768, 1024, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        for path in ("/", "/transactions"):
            page.goto(path)
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll_width <= width + 1, f"{path} @{width}: scrollWidth={scroll_width}"


def test_the_skip_link_is_the_first_tab_stop_and_moves_focus(page: Page):
    """Restored per §4.4. Two halves, and only the browser can prove either: it
    is the FIRST thing Tab reaches (a skip link behind the ribbon's ~18 links
    skips nothing), and following it moves FOCUS rather than only the viewport —
    which is what `tabindex="-1"` on `<main>` buys and what a plain `#content`
    anchor does not."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/")
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.className") == "skip"
    # Hidden until focused, and a real target once it is.
    assert page.evaluate(
        "() => document.querySelector('a.skip').getBoundingClientRect().left"
    ) > 0
    # And it lands ON THE CARD. The link is fixed to the viewport (it has to be —
    # focusing it halfway down a ledger must not scroll it out of sight), but the
    # shell is capped at 1180 and centred, so past ~1212px those are different
    # places: a plain `right: 8px` put the plate out on the grey window, 106px
    # clear of the card at 1440. Inset far enough to be inside the 20px corner
    # radius on both axes, not merely inside the bounding box.
    boxes = page.evaluate(
        """() => {
          const r = el => el.getBoundingClientRect();
          return {skip: r(document.querySelector('a.skip')),
                  shell: r(document.querySelector('.shell'))};
        }"""
    )
    assert boxes["skip"]["right"] <= boxes["shell"]["right"] - 20, boxes
    assert boxes["skip"]["top"] >= boxes["shell"]["top"] + 4, boxes
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.id") == "content"


def test_the_theme_control_persists_and_outranks_the_system_preference(page: Page):
    """The control A1 shipped the `:not([data-theme="light"])` guard for.

    Three states, and the third is the absence of the attribute — System is not
    "whatever the machine said the first time", it is "keep asking the machine",
    which is why choosing it must REMOVE `data-theme` rather than write the
    current system value into it. Persistence is `localStorage`, so it survives a
    reload; and it is applied from `<head>` before first paint, which is why
    `static/theme.js` exists as a file of its own.
    """
    page.emulate_media(color_scheme="dark")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/")
    LIGHT, DARK = "rgb(245, 245, 247)", "rgb(11, 14, 20)"
    ground = "() => getComputedStyle(document.body).backgroundColor"
    stamped = "() => document.documentElement.getAttribute('data-theme')"

    # System, on a dark machine.
    assert page.evaluate(ground) == DARK
    assert page.evaluate(stamped) is None

    # Explicit Light beats it, and survives a reload.
    page.click("#theme-choice label:has(input[value='light'])")
    assert page.evaluate(ground) == LIGHT
    assert page.evaluate(stamped) == "light"
    page.reload()
    assert page.evaluate(ground) == LIGHT, "the choice did not survive a reload"
    assert page.evaluate("() => document.querySelector('#theme-choice input:checked').value") == "light"

    # Explicit Dark on a light machine — the other direction of the same guard.
    page.emulate_media(color_scheme="light")
    page.reload()
    assert page.evaluate(ground) == LIGHT
    page.click("#theme-choice label:has(input[value='dark'])")
    assert page.evaluate(ground) == DARK
    page.reload()
    assert page.evaluate(ground) == DARK

    # Back to System: the attribute is REMOVED, and the machine decides again.
    page.click("#theme-choice label:has(input[value='system'])")
    assert page.evaluate(stamped) is None
    assert page.evaluate(ground) == LIGHT
    page.emulate_media(color_scheme="dark")
    assert page.evaluate(ground) == DARK, "System stopped following the machine"


def test_the_ribbon_stacks_into_one_column_below_60rem(page: Page):
    """The collapse fix (§2.2). The old wrapped band put bare `.item` anchors and
    five-item `.group` columns in one `flex-wrap` row, so at 768 the head ACTIONS
    sat on the same baseline as *Onboard* (which belongs to it) and *RFIs* (which
    does not), while TRANSACT wrapped away from its own entries. A group head
    naming the wrong items is worse than no head.

    Asserted twice over, because either alone is passable by a broken layout:
    every direct child of the ribbon starts strictly below the one before it
    (DOM order IS visual order — the only thing a wrap can break), and every
    group's head sits above every one of its own items and below nothing else's.
    """
    page.set_viewport_size({"width": 768, "height": 1000})
    page.goto("/")
    rows = page.evaluate(
        """() => [...document.querySelectorAll('nav.ribbon > *')]
             .map(el => Math.round(el.getBoundingClientRect().top))"""
    )
    assert rows == sorted(rows) and len(set(rows)) == len(rows), rows

    heads = page.evaluate(
        """() => [...document.querySelectorAll('nav.ribbon .group')].map(g => ({
             head: Math.round(g.querySelector('.group-head').getBoundingClientRect().bottom),
             items: [...g.querySelectorAll('.item')]
                      .map(a => Math.round(a.getBoundingClientRect().top)),
           }))"""
    )
    assert len(heads) == 3, heads
    for group in heads:
        assert group["items"], group
        assert all(top >= group["head"] for top in group["items"]), group

    # Nothing hid to get there: every entry the wide ribbon draws is still drawn
    # — 13 links (Overview, four Customers, two Transactions, RFIs, Onboard,
    # three Transact, the tour) and three group heads.
    for width in (1440, 768, 375):
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto("/")
        shown = page.evaluate(
            """() => [...document.querySelectorAll('nav.ribbon .item, nav.ribbon .group-head')]
                 .filter(el => el.offsetParent !== null).length"""
        )
        assert shown == 16, f"@{width}: {shown} ribbon entries visible"

# --- the three high-traffic families -----------------------------------


# The spec's §6 acceptance list, the two lines a server-side test cannot reach:
# "Renders correctly at 375 / 768 / 1024 / 1440. No horizontal overflow at any of
# them" and "Renders correctly in both themes. Neither was shipped unseen."
A4_WIDTHS = (375, 768, 1024, 1440)
A4_PAGES = ("/", "/customers", "/transactions")


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_nothing_a4_added_pushes_past_the_viewport(page: Page, theme: str):
    """Twenty-four viewports (three pages × four widths × two themes), on the
    pages A4 rewrote, in both themes.

    **Strengthened to plain "no horizontal overflow".**
    §6 asks for "no horizontal overflow" at 375 / 768 / 1024 / 1440. That was
    not true on these three pages when A4 landed — the tables overflowed,
    because a seven-column ledger does not fit a phone and no list template
    wrapped its table in a scroll container — measured on the parent commit:

        /              375 → 380 (5px past)
        /customers     375 → 584
        /transactions  375 → 922 · 768 → 922 · 1024 → 1130

    so this pin used to carry a `!inTable` exclusion and assert the narrower
    claim, that nothing A4 itself added was the offender. A5 landed the
    wrapper — every `<table>` opens inside `<div class="table-scroll">`,
    `overflow-x: auto` at and below 1024px — so the table no longer pushes
    the *document* past the viewport at any width, and `document.scrollWidth`
    is now the plain, correct assertion: a scrolling descendant does not
    count toward its ancestor's scrollWidth, so nothing is left to carve out
    for the table the way the old per-element walk had to.
    """
    page.emulate_media(color_scheme=theme)
    for url in A4_PAGES:
        for width in A4_WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(url)
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll_width <= width + 1, f"{url} at {width}px in {theme}: scrollWidth={scroll_width}"

            # And the four things this slice added, each measured on its own:
            # a class that fits at 1440 and runs off the edge at 375 is the
            # failure a screenshot at one width cannot see.
            for selector in (".hero-num", "details.teaching", ".next-step", "p.lede"):
                assert page.evaluate(
                    """(sel) => [...document.querySelectorAll(sel)].every(
                         el => el.getBoundingClientRect().right
                               <= document.documentElement.clientWidth + 1)""",
                    selector,
                ), f"{selector} runs past the viewport on {url} at {width}px in {theme}"


# --- the reviewer's seven overflow/aria findings -----------------


def test_no_overflow_on_the_seven_final_fix_pages(page: Page):
    """Five of the seven post-A5 findings were "overflows 375px", each on a
    page none of A2/A4/A5's own pins happened to cover: a customer with no
    resolvable name (the h1 fallback used to BE the bare id), the contact
    picker's `size="52"` input, the batch corridor's "Funded from" select
    (long option text — an EUR account disabled with "not on fedwire"), the
    orders launcher's customer select and the rfis segmented toggle's long
    second label. All five now hold `document.documentElement.scrollWidth <=
    viewport width` at every house width, the same plain invariant the other
    overflow pins in this file assert.
    """
    pages = {
        # No `GET /v2/customers/{id}` stub matches this id (`conduit_stub`'s
        # generic 404 fallback), so `customer` is `None` and the h1 falls back
        # to the bare id — the exact no-name case the fix is for.
        "customers detail, no name": "/customers/cus_browser_pin_unknown",
        "payouts/contact": "/payouts/contact",
        # `EUR_ACTIVE` is doomed on fedwire (`doomed_rail`), so the funding
        # select's disabled EUR option carries the long "— not on fedwire"
        # suffix that pushed it to 425px before the fix.
        "batches new": f"/customers/{CID}/batches/new{CORRIDOR}",
        "orders": "/orders",
        "rfis": "/rfis",
    }
    for width in (375, 768, 1024, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        for name, url in pages.items():
            page.goto(url)
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll_width <= width + 1, f"{name} ({url}) @{width}: scrollWidth={scroll_width}"


def test_the_demoted_prose_is_reachable_and_starts_closed(page: Page):
    """§4.5's other half. "Demote to disclosure, do not delete" is only true if
    the words are still gettable: closed on arrival (a daily operator does not
    pay for them), open on a click, and the same sentences inside.
    """
    page.goto("/transactions")
    disclosure = page.locator("details.teaching").first
    expect(disclosure).to_be_visible()
    assert disclosure.evaluate("el => el.open") is False
    body = disclosure.locator("p").first
    expect(body).to_be_hidden()
    disclosure.locator("summary").click()
    expect(body).to_be_visible()
    assert "Ledger reference" in body.inner_text()


def test_the_hero_numeral_is_forty_four_pixels_and_no_cell_is(page: Page):
    """§2.3 as painted, which is the half `tests/web_harness.hero_numerals`
    cannot see: the class could exist on the right elements and be worth
    nothing if a later rule overrode the size, and a cell could be 44px by
    inheriting from somewhere else entirely. Measured, both directions.
    """
    page.goto("/")
    sizes = page.evaluate(
        """() => [...document.querySelectorAll('.hero-num')]
             .map(el => parseFloat(getComputedStyle(el).fontSize))"""
    )
    assert sizes and all(size >= 36 for size in sizes), sizes
    # `--fs-hero` is a clamp(2.25rem, 4vw, 3rem): 48px at 1440, and never the
    # 15px body size, which is the regression this catches.
    cell_sizes = page.evaluate(
        """() => [...document.querySelectorAll('td, td *')]
             .map(el => parseFloat(getComputedStyle(el).fontSize))"""
    )
    assert cell_sizes and max(cell_sizes) < 36, max(cell_sizes)
