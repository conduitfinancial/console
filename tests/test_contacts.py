"""Contacts — the unified surface.

Three things are load-bearing here and each has its own section below:

* **the capability matrix.** A row may be saved, whitelisted, or both — and
  "both" is a claim about identity that only coordinates may make. The four
  cases (saved-only, whitelisted-only, matched, near-miss) are the page's whole
  contract, and the near-miss is the one that matters: two rows is the honest
  answer when nothing proves the records are one destination.
* **the bridge.** "Register for intercompany payouts" prefills a registration from a saved
  contact — and, exactly like the group-entity shortcut it mirrors, the record is
  re-read server-side at submit and the browser's coordinates are dropped, so a
  prefilled form cannot be edited into registering an account the contact does
  not name.
* **the rename sweep.** Every operator-facing string says "contact"; the stored
  audit actions still say `counterparty.*`, because renaming recorded history is
  falsification, not a rename.
"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import unquote_plus

import httpx
from sqlalchemy import select, text

from app import counterparties
from app.models import AuditEvent, Counterparty, Operation
from tests.payments_fixtures import (
    CID,
    FEDWIRE_BUSINESS,
    OTHER_CID,
    PAYOUT,
    PENDING,
    REGISTERED,
    USD_ACCOUNT,
    WHITELIST_PATH,
    encoded,
    page,
    recipient_form,
)
from tests.web_harness import (
    documents_stub,
    forbidden_affordances,
    make_app,
    post,
    signed_in,
    signed_in_as,
    stub,
    upload,
)

LIST = f"/customers/{CID}/contacts"
FORM = f"/customers/{CID}/recipients"
GLOBAL = "/contacts"


async def evidence(web) -> None:
    """`recipient_form`'s own `doc_evidence_1`, really uploaded.

    The registration route resolves every `evidenceDocumentIds` entry
    against this console's upload ledger for `feature_request` before anything
    is sent, so a bridge test that expects to reach Conduit has to upload first.
    `documents_stub` mints the id from the filename.
    """
    response = await upload(web, purpose="feature_request", filename="doc_evidence_1.png")
    assert response.status_code == 200, response.text

# The saved record whose coordinates are REGISTERED's, key for key.
TWIN = {
    "accountNumber": REGISTERED["accountNumber"],
    "routingNumber": REGISTERED["routingNumber"],
    "legalName": REGISTERED["legalName"],
}


def routes(items=None, customer=CID, extra=None):
    return {
        ("GET", f"/v2/customers/{customer}/whitelist-recipients"): page(
            items if items is not None else []
        ),
        ("GET", "/v2/customers"): page([{"id": CID, "legalName": "ZZZTEST Console EOOD"}]),
        ("GET", "/v2/payouts/requirements"): httpx.Response(200, json=FEDWIRE_BUSINESS),
        ("GET", f"/v2/customers/{customer}/virtual-accounts"): page([USD_ACCOUNT]),
        ("POST", "/v2/payouts"): httpx.Response(202, json=PAYOUT),
        ("POST", "/v2/documents"): documents_stub,
        ("POST", WHITELIST_PATH): httpx.Response(201, json=PENDING),
        **(extra or {}),
    }


async def store(session, customer_id=CID, label="ZZZTEST Globex", **overrides) -> uuid.UUID:
    values = {
        "recipient": dict(TWIN),
        "rail_family": "us",
        "recipient_type": "business",
        "destination_country": "USA",
        **overrides,
    }
    cp_id = await counterparties.save(
        session,
        customer_id=customer_id,
        label=label,
        actor_id="usr_1",
        actor_email="ops@example.com",
        **values,
    )
    await session.commit()
    return cp_id


def visible(html: str) -> str:
    """The text an operator actually reads: no tags, so no attribute values, no
    hrefs and no form field names.

    That is exactly the boundary the naming rule draws — `name="counterparty"`,
    `/export/counterparties.csv` and `hx-post=".../contacts"` are wire
    identifiers, and the rule says code identifiers may stay. What may not stay
    is a word on the screen.
    """
    stripped = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"(?s)<[^>]*>", " ", stripped)


# --- the capability matrix ------------------------------------------------------------


async def test_a_saved_contact_with_no_registration_says_only_that(session):
    await store(session, label="ZZZTEST Saved Only")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "ZZZTEST Saved Only" in html
    assert "Saved for payouts" in html
    assert "Registered for intercompany payouts" not in html


async def test_a_registration_with_no_saved_record_says_only_that(session):
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert REGISTERED["legalName"] in html
    assert "Registered for intercompany payouts" in html
    assert "Saved for payouts" not in html


async def test_matching_coordinates_make_one_row_with_both_capabilities(session):
    await store(session, label="ZZZTEST Both")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Showing 1 contact" in html
    assert "Saved for payouts" in html and "Registered for intercompany payouts" in html
    # One row, so one masked coordinate cell — not the same account printed twice
    # under two headings.
    assert html.count("Saved for payouts") == 1


async def test_a_near_miss_stays_two_rows(session):
    """One digit apart is not the same account, and the console asserts identity
    only where the coordinates prove it."""
    await store(
        session,
        label="ZZZTEST Nearly",
        recipient={**TWIN, "accountNumber": "000123456788"},
    )
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Showing 2 contacts" in html
    assert "ZZZTEST Nearly" in html


async def test_a_different_legal_name_stays_two_rows(session):
    """`same_destination`'s own rule, reused rather than re-decided: two accounts
    at one bank with different names on them are two destinations."""
    await store(session, label="ZZZTEST Renamed", recipient={**TWIN, "legalName": "Other SA"})
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        assert "Showing 2 contacts" in (await web.get(LIST)).text


async def test_a_different_rail_family_stays_two_rows(session):
    """The rail check `payments.RAILS_FOR` makes for a payout, made here: an
    IBAN-addressed destination is not an ABA-addressed one whatever else agrees."""
    await store(session, label="ZZZTEST Sepa", rail_family="sepa")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        assert "Showing 2 contacts" in (await web.get(LIST)).text


async def test_a_pending_registration_is_not_a_capability(session):
    """An unreviewed registration is never rendered as an
    active capability. It says `Whitelisting` and wears Conduit's own pill."""
    await store(session, label="ZZZTEST Waiting")
    pending_twin = {**PENDING, "legalName": REGISTERED["legalName"]}
    app = make_app(stub(routes([pending_twin])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Whitelisting" in html and "Pending review" in html
    assert "Registered for intercompany payouts" not in html
    assert "Saved for payouts" in html


async def test_an_unmatchable_pair_of_empty_coordinates_never_merges(session):
    """A shared legal name is not evidence. Two records that state no account and
    no bank have established nothing, and must not become one row."""
    await store(session, label="ZZZTEST Nameless", recipient={"legalName": "ZZZTEST Ghost"})
    bare = {
        **REGISTERED,
        "accountNumber": None,
        "routingNumber": None,
        "legalName": "ZZZTEST Ghost",
    }
    app = make_app(stub(routes([bare])))
    async with signed_in(app) as web:
        assert "Showing 2 contacts" in (await web.get(LIST)).text


async def test_one_registration_is_claimed_by_at_most_one_saved_record(session):
    """Two console records with identical coordinates cannot both wear the one
    capability the customer actually has.

    **Strengthened: now NEITHER wears it.**
    This test used to assert that exactly one did — which was true, and which was
    the bug: *which* one was decided by `lower(label)` sort order, so "ZZZTEST A"
    carried a registration that was as much "ZZZTEST B"'s, and every transfer
    attributed through the same function inherited that guess. The registration
    is still listed, as its own unattributed row.
    """
    await store(session, label="ZZZTEST A")
    await store(session, label="ZZZTEST B")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Showing 3 contacts" in html
    assert html.count('Registered for intercompany payouts</span>') == 1
    assert html.count("Whitelist status not attributable") == 2


async def test_an_unreadable_whitelist_is_a_problem_not_an_empty_half(session):
    await store(session, label="ZZZTEST Saved")
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", WHITELIST_PATH): httpx.Response(
                        500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "Conduit refused this: SERVER_ERROR" in html  # A3: the code, not the vendor title
    # The saved half is local and still renders; the count refuses to speak for
    # the half that failed.
    assert "ZZZTEST Saved" in html and "Couldn't read this list" in html


# --- the bridge ------------------------------------------------------------------------


async def test_the_bridge_prefills_the_registration_from_the_saved_record(session):
    cp_id = await store(session, label="ZZZTEST Bridge Me")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{FORM}?contact={cp_id}")).text

    # **Stated, not rendered as an input.** This test used to assert the full
    # account number was in a `value=`; the POST re-reads the record and
    # overwrites whatever the browser sends, so that was a coordinate printed to
    # prefill a field whose contents are discarded. The routing number stays
    # whole — it identifies a bank and is public routing data, exactly as in
    # every list cell.
    assert 'value="000123456789"' not in html
    assert "••••6789" in html and "021000021" in html
    assert "ZZZTEST Bridge Me" in html
    # The relationship is a question here, not a conclusion — the bridge proves
    # coordinates, never ownership.
    assert "relationship is yours to answer" in html
    assert 'name="relationship"' not in html.split("<select", 1)[0]


async def test_the_bridge_is_offered_only_where_it_would_do_something(session):
    """A contact already registered has nothing to gain from a second one."""
    await store(session, label="ZZZTEST Both")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        assert "Register for intercompany payouts</a>" not in (await web.get(LIST)).text

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        assert "Register for intercompany payouts</a>" in (await web.get(LIST)).text


async def test_the_bridge_refuses_another_customers_contact(session):
    cp_id = await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{FORM}?contact={cp_id}")).text
    assert "That contact could not be used" in html
    assert "000123456789" not in html


async def test_a_bridged_submit_registers_the_stored_coordinates_not_the_forms(session):
    """The forgery strip, exactly as the group-entity shortcut does it: the
    record is re-read at submit and the browser's copy of every coordinate key is
    dropped before the fresh read is overlaid."""
    cp_id = await store(session, label="ZZZTEST Bridge")
    calls: list = []
    app = make_app(stub(routes([]), calls))
    async with signed_in(app) as web:
        await evidence(web)
        await post(
            web,
            FORM,
            encoded(
                recipient_form(
                    contact=str(cp_id),
                    **{
                        "f.accountNumber": "999999999999",
                        "f.routingNumber": "011000015",
                        "f.legalName": "ZZZTEST Forged Ltd",
                    },
                )
            ),
        )

    sent = json.loads(next(c for c in calls if c[0] == "POST" and "whitelist" in c[1])[2])
    assert sent["accountNumber"] == REGISTERED["accountNumber"]
    assert sent["routingNumber"] == REGISTERED["routingNumber"]
    assert sent["legalName"] == REGISTERED["legalName"]
    assert "999999999999" not in json.dumps(sent)


async def test_a_bridged_submit_keeps_the_operators_own_relationship(session):
    """Unlike the shortcut, which *proves* `group_entity`. A saved payout
    destination is no evidence of who owns the account."""
    cp_id = await store(session, label="ZZZTEST Bridge")
    calls: list = []
    app = make_app(stub(routes([]), calls))
    async with signed_in(app) as web:
        await evidence(web)
        await post(
            web,
            FORM,
            encoded(recipient_form(contact=str(cp_id), **{"f.relationship": "self"})),
        )

    sent = json.loads(next(c for c in calls if c[0] == "POST" and "whitelist" in c[1])[2])
    assert sent["relationship"] == "self"


async def test_a_bridged_registration_is_ledgered_and_lands_on_contacts(session):
    cp_id = await store(session, label="ZZZTEST Bridge")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await evidence(web)
        response = await post(web, FORM, encoded(recipient_form(contact=str(cp_id))))

    op = (
        await session.execute(select(Operation).where(Operation.type == "whitelist_create"))
    ).scalar_one()
    assert op.state == "confirmed"
    assert response.headers["HX-Redirect"] == f"{LIST}?registered={PENDING['id']}"


async def test_a_bridge_from_an_unregisterable_family_refuses(session):
    """A rail family Conduit has no registration DTO for is refused with the
    reason, not registered on a guess."""
    cp_id = await store(session, label="ZZZTEST Odd", rail_family="carrier_pigeon")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{FORM}?contact={cp_id}")).text
    assert "cannot be registered from here" in html


# --- the rename sweep -------------------------------------------------------------------


async def test_no_operator_facing_surface_says_counterparty(session):
    """The naming rule, asserted against what is rendered rather than against the
    source: attribute values and hrefs are wire identifiers and may keep the old
    name, and the audit action values must."""
    cp_id = await store(session, label="ZZZTEST Sweep")
    app = make_app(stub(routes([REGISTERED, PENDING])))
    async with signed_in(app) as web:
        pages = [
            await web.get(LIST),
            await web.get(GLOBAL),
            await web.get(FORM),
            await web.get(f"{FORM}?contact={cp_id}"),
            await web.get(f"/customers/{CID}"),
            await web.get(
                f"/customers/{CID}/payouts/new?purpose=payment_for_goods_or_services"
                "&rail=fedwire&recipientType=business&destinationCountry=USA"
            ),
        ]
    for response in pages:
        assert response.status_code == 200, response.request.url
        assert "counterpart" not in visible(response.text).lower(), response.request.url


async def test_the_audit_actions_still_say_counterparty(session):
    """Recorded history is never re-labelled. `counterparty.rename` /
    `.archive` are the values already written and every row carries."""
    cp_id = await store(session, label="ZZZTEST Ledgered")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(web, f"{LIST}/{cp_id}/rename", encoded({"label": "ZZZTEST Renamed"}))
        await post(web, f"{LIST}/{cp_id}/archive")

    actions = {
        row.action
        for row in (await session.execute(select(AuditEvent))).scalars().all()
    }
    assert {"counterparty.rename", "counterparty.archive"} <= actions


async def test_the_flashes_speak_contact(session):
    cp_id = await store(session, label="ZZZTEST Flash")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        renamed = await post(web, f"{LIST}/{cp_id}/rename", encoded({"label": "ZZZTEST New"}))
        archived = await post(web, f"{LIST}/{cp_id}/archive")

    assert "Contact renamed." in unquote_plus(renamed.headers["HX-Redirect"])
    assert "Contact archived" in unquote_plus(archived.headers["HX-Redirect"])


# --- the old paths ----------------------------------------------------------------------


async def test_the_phase_9_path_redirects_permanently():
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        moved = await web.get(f"/customers/{CID}/counterparties", follow_redirects=False)
    assert moved.status_code == 301
    assert moved.headers["location"] == LIST


async def test_the_redirect_keeps_the_query_string():
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        moved = await web.get(
            f"/customers/{CID}/counterparties?registered=wlr_1", follow_redirects=False
        )
    assert moved.headers["location"] == f"{LIST}?registered=wlr_1"


# --- the cross-customer view --------------------------------------------------------------


async def test_the_global_list_names_customers_and_spans_them(session):
    await store(session, label="ZZZTEST Ours")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(GLOBAL)).text

    assert "ZZZTEST Ours" in html and "ZZZTEST Theirs" in html
    # Names over ids, from the one bounded resolver; an id beyond it stays an id.
    assert "ZZZTEST Console EOOD" in html and OTHER_CID in html


async def test_the_global_list_filters_by_family(session):
    await store(session, label="ZZZTEST Us Row")
    await store(session, label="ZZZTEST Sepa Row", rail_family="sepa")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?family=sepa")).text

    assert "ZZZTEST Sepa Row" in html and "ZZZTEST Us Row" not in html


async def test_the_global_list_makes_no_whitelist_call_without_a_customer(session):
    """Conduit lists a whitelist per customer only, so an unscoped page must not
    invent a scope to ask about."""
    await store(session, label="ZZZTEST Local")
    calls: list = []
    app = make_app(stub(routes([]), calls))
    async with signed_in(app) as web:
        assert (await web.get(GLOBAL)).status_code == 200
    assert not [c for c in calls if "whitelist" in c[1]]


async def test_the_global_list_resolves_capabilities_once_a_customer_is_named(session):
    await store(session, label="ZZZTEST Both")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={CID}")).text

    assert "Saved for payouts" in html and "Registered for intercompany payouts" in html


async def test_an_unanswerable_capability_filter_says_so_rather_than_guessing(session):
    await store(session, label="ZZZTEST Local")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?capability=whitelisted")).text

    assert "That capability needs a customer" in html
    # …and the rows are still there, unfiltered, rather than a silently empty page.
    assert "ZZZTEST Local" in html


async def test_the_global_pager_carries_the_filters_and_drops_the_offset(session):
    for n in range(3):
        await store(session, label=f"ZZZTEST Row {n}")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?family=us&limit=25&offset=0")).text

    assert "family=us" in html
    href = re.search(r'href="(/export/counterparties\.csv[^"]*)"', html).group(1)
    assert "offset" not in href and "limit" not in href


async def test_the_global_export_spans_customers_and_names_them(session):
    await store(session, label="ZZZTEST Ours")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        csv = (await web.get("/export/counterparties.csv")).text

    assert "customer_id" in csv.splitlines()[0]
    assert CID in csv and OTHER_CID in csv
    # Masked exactly as the screen is — a CSV is the easiest thing to mail on.
    assert "••••6789" in csv and REGISTERED["accountNumber"] not in csv


async def test_the_scoped_export_states_the_whitelist_capability(session):
    await store(session, label="ZZZTEST Both")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        csv = (await web.get(f"/export/counterparties.csv?customerId={CID}")).text

    assert csv.splitlines()[0].endswith("customer_id,whitelisted")
    assert csv.splitlines()[1].endswith("true")


async def test_the_unscoped_export_says_unknown_rather_than_false(session):
    """Nothing was asked, so nothing is known — and `false` would be a claim."""
    await store(session, label="ZZZTEST Ours")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        csv = (await web.get("/export/counterparties.csv")).text
    assert csv.splitlines()[1].endswith("unknown")


# --- roles and CSRF -----------------------------------------------------------------------


async def test_a_viewer_reads_both_views_but_is_offered_no_action(session):
    cp_id = await store(session, label="ZZZTEST Readable")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app, groups="readers") as web:
        listing = await web.get(LIST)
        every = await web.get(GLOBAL)
        archive = await post(web, f"{LIST}/{cp_id}/archive")
        rename = await post(web, f"{LIST}/{cp_id}/rename", encoded({"label": "ZZZTEST No"}))

    assert listing.status_code == 200 and every.status_code == 200
    assert "ZZZTEST Readable" in listing.text
    assert "Register for intercompany payouts</a>" not in listing.text
    assert "Revoke</button>" not in listing.text
    assert archive.status_code == 403 and rename.status_code == 403


# --- partial roles ------------------------------------------------------------------------
#
# This list carries five separately-gated actions, and a deployment-defined role
# holds them one at a time. Each test renders under exactly one and asks the same
# three questions: is the held action offered, is every unheld one gone, and is
# there any control left on the page that would answer 403 if pressed.

CONTACT_ACTIONS = ("contact.edit", "contact.archive", "whitelist.register", "whitelist.revoke")


async def test_a_role_holding_only_contact_edit_gets_the_edit_half_and_nothing_else(session):
    await store(session, label="ZZZTEST Saved Only", recipient={"accountNumber": "9" * 10})
    app = make_app(stub(routes([])))
    async with signed_in_as(app, "contact.edit") as web:
        listing = await web.get(LIST)

    assert listing.status_code == 200 and "ZZZTEST Saved Only" in listing.text
    assert ">Rename</button>" in listing.text and ">Edit</a>" in listing.text
    assert "Archive</button>" not in listing.text
    # The bridge lands on a registration form this role cannot submit, so it is
    # not offered — the whole point of gating it on `whitelist.register`.
    assert "Register for intercompany payouts</a>" not in listing.text
    assert "Whitelist a destination</a>" not in listing.text
    # The action column has content, so the read-only sentence must not appear.
    assert "contact.edit</code> permission" not in listing.text
    assert forbidden_affordances(app, listing.text, {"console.view", "contact.edit"}) == []


async def test_a_role_holding_only_contact_archive_gets_no_rename_form(session):
    await store(session, label="ZZZTEST Archivable", recipient={"accountNumber": "9" * 10})
    app = make_app(stub(routes([])))
    async with signed_in_as(app, "contact.archive") as web:
        listing = await web.get(LIST)

    assert "Archive</button>" in listing.text
    assert ">Rename</button>" not in listing.text and ">Edit</a>" not in listing.text
    # The name is still readable — the row does not lose its label with its form.
    assert "ZZZTEST Archivable" in listing.text
    assert forbidden_affordances(app, listing.text, {"console.view", "contact.archive"}) == []


async def test_a_role_holding_only_whitelist_register_is_told_what_it_lacks(session):
    """`whitelist.register` sits outside `can_act`, so this role reaches the
    explanatory sentence while its own header action is on screen — which is why
    that sentence no longer opens with "Read-only"."""
    await store(session, label="ZZZTEST Unmanageable", recipient={"accountNumber": "9" * 10})
    app = make_app(stub(routes([])))
    async with signed_in_as(app, "whitelist.register") as web:
        listing = await web.get(LIST)

    assert "Whitelist a destination</a>" in listing.text
    assert "Archive</button>" not in listing.text and ">Rename</button>" not in listing.text
    assert "Read-only" not in listing.text
    for name in CONTACT_ACTIONS + ("contact.delete",):
        assert f"<code>{name}</code>" in listing.text
    assert forbidden_affordances(app, listing.text, {"console.view", "whitelist.register"}) == []


async def test_the_mutations_need_the_csrf_header(session):
    cp_id = await store(session, label="ZZZTEST Guarded")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        for url, body in (
            (f"{LIST}/{cp_id}/archive", b""),
            (f"{LIST}/{cp_id}/rename", encoded({"label": "ZZZTEST No"})),
        ):
            response = await web.post(
                url,
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403, url

    row = (await session.execute(select(Counterparty))).scalar_one()
    assert row.label == "ZZZTEST Guarded" and row.archived_at is None


# --- the deep review's three proven findings ---------------------------------


async def test_a_scoped_page_pages_the_scoped_query_not_a_global_slice(session):
    """The reviewer's own scenario. Thirty contacts for one
    customer and one for another; the second customer's page must show its single
    contact on page 1, not "no contact matches" because that row sits past the
    global page the console read and then filtered in Python.
    """
    for n in range(30):
        await store(session, label=f"ZZZTEST A{n:02d}")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Solo")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={OTHER_CID}&limit=25")).text

    assert "ZZZTEST Solo" in html
    assert "No contact matches these filters" not in html


async def test_a_scoped_page_fills_its_page_and_offers_a_truthful_next(session):
    """The other half of the same bug: a scoped page was as short as whatever
    survived the Python filter, and its Next was computed from the global read."""
    for n in range(30):
        await store(session, label=f"ZZZTEST A{n:02d}")
        await store(session, customer_id=OTHER_CID, label=f"ZZZTEST B{n:02d}")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        first = (await web.get(f"{GLOBAL}?customerId={OTHER_CID}&limit=25")).text
        second = (await web.get(f"{GLOBAL}?customerId={OTHER_CID}&limit=25&offset=25")).text

    assert first.count("ZZZTEST B") >= 25 and "ZZZTEST A" not in first
    assert "Showing 25 contacts" in first
    assert "offset=25" in first  # a Next that leads somewhere
    assert "ZZZTEST B29" in second


async def test_a_registration_with_no_saved_record_appears_once_across_pages(session):
    """`merge` appends the unclaimed registrations after the saved rows, so on a
    paged list they belong to the *last* page — repeating them under every offset
    would count one destination as many."""
    for n in range(30):
        # Distinct coordinates, so none of them is REGISTERED's twin — this test
        # is about the *unclaimed* registration, and the default recipient in
        # `store` is deliberately the same destination the fixture registers.
        await store(
            session,
            label=f"ZZZTEST A{n:02d}",
            recipient={"accountNumber": f"00012345{n:04d}", "routingNumber": "021000021"},
        )
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        first = (await web.get(f"{GLOBAL}?customerId={CID}&limit=25")).text
        last = (await web.get(f"{GLOBAL}?customerId={CID}&limit=25&offset=25")).text

    # The global table renders no whitelist id, so the badge chip is what says a
    # registration-only row is on screen (the page's lede names the capability in
    # prose, hence the chip markup rather than the words).
    chip = 'Registered for intercompany payouts</span>'
    assert chip not in first, "an unclaimed registration is not on every page"
    assert last.count(chip) == 1


async def test_the_row_order_is_stable_when_labels_tie(session):
    """Offset paging over a non-unique sort key can skip or repeat a row between
    two requests. `lower(label)` ties on a customer who saved the same name for
    two rails, so the id is the tiebreaker."""
    # One label, three rows: the partial unique index is per customer, so the
    # tie has to be built across customers (a same-customer re-save updates).
    for customer in (CID, OTHER_CID, "cus_third"):
        await store(session, customer_id=customer, label="ZZZTEST Same")
    first = await counterparties.everyones(session, limit=2, offset=0)
    second = await counterparties.everyones(session, limit=2, offset=1)
    assert [r["id"] for r in first][1] == [r["id"] for r in second][0]
    assert len({r["id"] for r in first} | {r["id"] for r in second}) == 3


async def test_a_capped_contacts_export_says_it_was_capped(session, monkeypatch):
    """A regression: the counterparties branch fetched the +1
    sentinel, threw it away and returned `truncated=False` — a short PII file
    whose audit row claimed it was whole."""
    from app.web import exports

    monkeypatch.setattr(exports, "CAP_ROWS", 2)
    for n in range(4):
        await store(session, label=f"ZZZTEST Row {n}")
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        csv = (await web.get("/export/counterparties.csv")).text

    assert "# TRUNCATED" in csv
    row = (
        await session.execute(select(AuditEvent).where(AuditEvent.action == "export.csv"))
    ).scalar_one()
    assert row.detail["truncated"] is True and row.detail["rows"] == 2


async def test_a_scoped_contacts_export_states_what_it_leaves_out(session):
    """The scoped page counts registered-only rows that this
    file omits by design, so a one-row page could export an empty file with
    nothing saying why."""
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        listing = (await web.get(f"{GLOBAL}?customerId={CID}")).text
        csv = (await web.get(f"/export/counterparties.csv?customerId={CID}")).text

    assert "Showing 1 contact" in listing  # the registration, with no saved record
    lines = [line for line in csv.splitlines() if line]
    assert len(lines) == 2, "a header, and the scope note — no data row"
    assert "registrations with no saved record are not in this file" in csv


async def test_the_capability_filtered_export_says_it_too(session):
    await store(session, label="ZZZTEST Both")
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        csv = (
            await web.get(f"/export/counterparties.csv?customerId={CID}&capability=whitelisted")
        ).text
    assert "registrations with no saved record are not in this file" in csv


# --- the follow-up pass --------------------------------------------


UNREADABLE = httpx.Response(500, json={"type": "SERVER_ERROR", "title": "Upstream failure"})


async def test_an_unreadable_whitelist_is_unknown_not_not_whitelisted(session):
    """`merge` against an empty list gives every row `entry=None`,
    which the badge column renders as the *absence* of a capability — the exact
    reading "we could not ask" must never produce."""
    await store(session, label="ZZZTEST Both", recipient=TWIN)
    app = make_app(stub(routes(extra={("GET", WHITELIST_PATH): UNREADABLE})))
    async with signed_in(app) as web:
        scoped = (await web.get(LIST)).text
        globally = (await web.get(f"{GLOBAL}?customerId={CID}")).text

    for html in (scoped, globally):
        assert "Whitelist status unknown" in html
        assert "Saved for payouts" in html


async def test_an_unreadable_whitelist_does_not_answer_a_capability_filter(session):
    """The filter's answer depends on the half that failed, so it is refused
    rather than applied to a `False` nobody established."""
    await store(session, label="ZZZTEST Both", recipient=TWIN)
    app = make_app(stub(routes(extra={("GET", WHITELIST_PATH): UNREADABLE})))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={CID}&capability=whitelisted")).text

    assert "ZZZTEST Both" in html, "rows are shown unfiltered, not silently dropped"
    assert "could not be applied" in html


async def test_an_unreadable_whitelist_export_refuses_a_capability_filter(session):
    await store(session, label="ZZZTEST Both", recipient=TWIN)
    app = make_app(stub(routes(extra={("GET", WHITELIST_PATH): UNREADABLE})))
    async with signed_in(app) as web:
        refused = await web.get(
            f"/export/counterparties.csv?customerId={CID}&capability=whitelisted"
        )
        plain = await web.get(f"/export/counterparties.csv?customerId={CID}")

    assert refused.status_code == 400
    # Without the filter the file is still produced — and says what it could not
    # resolve, with `unknown` rather than `false` in the column.
    assert plain.status_code == 200
    assert plain.text.splitlines()[1].endswith("unknown")
    assert "# EXPORT INCOMPLETE" in plain.text


async def test_a_capability_filter_searches_the_whole_customer_not_one_page(session):
    """The filter used to run after the page was cut, so a whitelisted
    contact on saved-page 2 was invisible to a filtered page 1."""
    for n in range(30):
        await store(
            session,
            label=f"ZZZTEST A{n:02d}",
            recipient={"accountNumber": f"00012345{n:04d}", "routingNumber": "021000021"},
        )
    await store(session, label="ZZZTEST Zulu Twin", recipient=TWIN)
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={CID}&capability=whitelisted&limit=25")).text

    assert "ZZZTEST Zulu Twin" in html
    assert "Showing 1 contact" in html


async def test_registrations_count_toward_the_page_size(session):
    """The other half: whitelist-only rows were appended after the
    slice, so a 25-row page could render a hundred of them."""
    entries = [
        {**REGISTERED, "id": f"wlr_{n}", "accountNumber": f"00099{n:07d}", "label": f"Reg {n}"}
        for n in range(40)
    ]
    app = make_app(stub(routes(entries)))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={CID}&limit=25")).text

    assert html.count('Registered for intercompany payouts</span>') == 25
    assert "Showing 25 contacts" in html


async def test_the_whitelist_read_walks_past_conduits_first_page(session):
    """`fetch_recipients` asks for one cursor page; a customer with
    more registrations than that had the rest silently absent — from the list
    *and* from the capability join."""
    first = page([{**REGISTERED, "id": "wlr_first", "accountNumber": "000900000001"}], "CUR2")
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", WHITELIST_PATH): lambda request: (
                        page([{**REGISTERED, "id": "wlr_second"}])
                        if b"CUR2" in request.url.query
                        else first
                    )
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "wlr_first" in html and "wlr_second" in html
    assert len([c for c in calls if "whitelist" in c[1]]) == 2


async def test_two_saved_records_with_one_destination_are_ambiguous(session):
    """Whichever sorted first silently took the registration — and
    with it the capability, the bridge and every future transfer's attribution."""
    await store(session, label="ZZZTEST Alpha", recipient=TWIN)
    await store(session, label="ZZZTEST Beta", recipient=TWIN)
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(LIST)).text

    assert "2 saved contacts share these coordinates" in html
    assert "Whitelist status not attributable" in html
    # The registration is still Conduit's fact and still listed — as its own
    # unattributed row, exactly like one nothing was ever saved for. What is
    # suppressed is the *claim that one of these two contacts is it*.
    assert html.count('Registered for intercompany payouts</span>') == 1
    assert html.count("Saved for payouts") == 2
    assert "Register for intercompany payouts</a>" not in html, "no bridge from an ambiguous row"


async def test_an_archived_contact_still_explains_a_bookmarked_filter(session):
    """Archiving retires the picker entry, not the payment history."""
    cp_id = await store(session, label="ZZZTEST Gone")
    assert await counterparties.archive(session, CID, str(cp_id))
    await session.commit()
    found = await counterparties.get(session, CID, str(cp_id), include_archived=True)
    assert found is not None and found["archived"] is True
    assert await counterparties.get(session, CID, str(cp_id)) is None


async def test_a_bridged_prefill_never_renders_the_full_coordinate(session):
    """The POST re-reads the record server-side, so these fields are
    not the operator's input — and a page that prints an account number to
    prefill a value it will not use is a coordinate on screen for nothing."""
    cp_id = await store(session, label="ZZZTEST Bridge")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{FORM}?contact={cp_id}")).text

    assert REGISTERED["accountNumber"] not in html
    assert "••••6789" in html
    # The value still reaches Conduit, from the server's own read.
    assert 'name="contact"' in html


async def test_the_global_export_omits_the_staff_email_the_page_never_shows(session):
    """The export-mirrors-the-page rule."""
    await store(session, label="ZZZTEST Ours")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        globally = (await web.get("/export/counterparties.csv")).text
        scoped = (await web.get(f"/export/counterparties.csv?customerId={CID}")).text

    assert "ops@example.com" not in globally
    assert "saved_by" in globally.splitlines()[0], "the column stays, so a saved formula survives"
    # The scoped page has a Saved by column, so its file keeps the value.
    assert "ops@example.com" in scoped


# --- item 10: name-or-id customer filter, and a contact-name filter --------------------


async def test_the_customer_filter_accepts_a_name(session):
    await store(session, label="ZZZTEST Ours")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs")
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", "/v2/customers"): page(
                        [
                            {"id": CID, "legalName": "ZZZTEST Console EOOD"},
                            {"id": OTHER_CID, "legalName": "Globex Holdings"},
                        ]
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId=globex")).text

    assert "ZZZTEST Theirs" in html and "ZZZTEST Ours" not in html


async def test_a_name_matching_several_customers_filters_by_all_of_them(session):
    await store(session, label="ZZZTEST Ours")
    await store(session, customer_id=OTHER_CID, label="ZZZTEST Theirs")
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", "/v2/customers"): page(
                        [
                            {"id": CID, "legalName": "Acme One"},
                            {"id": OTHER_CID, "legalName": "Acme Two"},
                        ]
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId=acme")).text

    assert "ZZZTEST Ours" in html and "ZZZTEST Theirs" in html
    assert "2 customers match" in html
    # Two customers is not one scope, so no whitelist read can be made for it.
    assert "Whitelist status unknown" not in html


async def test_a_customer_name_matching_nobody_is_an_honest_empty(session):
    await store(session, label="ZZZTEST Ours")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId=nobody-at-all")).text

    assert "ZZZTEST Ours" not in html
    assert "No customer matches" in html


async def test_an_exact_customer_id_still_resolves_the_capability(session):
    await store(session, label="ZZZTEST Both", recipient=TWIN)
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?customerId={CID}")).text
    assert 'Registered for intercompany payouts</span>' in html


async def test_the_contact_name_filter_matches_label_and_legal_name(session):
    await store(session, label="ZZZTEST Alpha", recipient={**TWIN, "accountNumber": "111111111"})
    await store(
        session,
        label="ZZZTEST Beta",
        recipient={"accountNumber": "222222222", "legalName": "Zephyr Trading GmbH"},
    )
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        by_label = (await web.get(f"{GLOBAL}?contactName=alpha")).text
        by_legal = (await web.get(f"{GLOBAL}?contactName=zephyr")).text

    assert "ZZZTEST Alpha" in by_label and "ZZZTEST Beta" not in by_label
    # The legal name lives inside the encrypted blob — matched after the decrypt
    # the page already pays for, never in SQL.
    assert "ZZZTEST Beta" in by_legal and "ZZZTEST Alpha" not in by_legal


async def test_an_unreadable_row_is_never_silently_dropped_by_a_name_filter(session):
    await store(session, label="ZZZTEST Readable")
    corrupt = await store(session, label="ZZZTEST Corrupt")
    await session.execute(
        text("update counterparties set recipient = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": corrupt},
    )
    await session.commit()
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?contactName=readable")).text

    assert "ZZZTEST Readable" in html
    # It cannot match — its legal name is unreadable — so it is *counted*, not
    # quietly absent.
    assert "1 contact could not be read" in html


async def test_the_contact_name_filter_composes_and_exports(session):
    await store(session, label="ZZZTEST Alpha")
    await store(session, label="ZZZTEST Beta", rail_family="sepa")
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        html = (await web.get(f"{GLOBAL}?contactName=alpha&family=us")).text
        csv = await web.get("/export/counterparties.csv?contactName=alpha")

    assert "ZZZTEST Alpha" in html and "ZZZTEST Beta" not in html
    body = csv.text
    assert "ZZZTEST Alpha" in body and "ZZZTEST Beta" not in body
    assert "contactName-alpha" in csv.headers["content-disposition"]
