"""Editing, deleting and the history panel (the contact-editing round).

Four things are load-bearing here, and each has its own section:

* **the edit is discovery's form.** What can be stored is what could be sent —
  the same `forms` objects the payout screen validates with, so an invalid ABA
  cannot be saved into an address book that feeds money movement.
* **the audit row says what changed without saying the coordinates.** Masked
  before→after for the identity keys, names only for the rest. One test walks
  *every* audit row this round can write and proves the stored account number and
  IBAN appear in none of them.
* **the whitelist consequence is stated before the save, not after it.** It fires
  only when a registration is actually at stake and an identity key actually
  moved — and it fires on an unreadable whitelist too, because "we could not ask"
  is not "there is none".
* **delete is a second tier, not a bigger archive.** The coordinates are
  destroyed; the record, its name and its trail stay, so the ledger can still say
  where the money went.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import func, select, text

from app import counterparties
from app.models import AuditEvent, Counterparty, Operation
from tests.payments_fixtures import CID, OTHER_CID, REGISTERED
from tests.test_contacts import LIST, TWIN, routes, store, visible
from tests.web_harness import make_app, post, signed_in, stub, upload

ROOT = Path(__file__).resolve().parents[1]

# A recipient that satisfies `payout_requirements_fedwire_business.json` in full,
# so the happy path is a real acceptance rather than a form with holes. The
# account number and ABA are REGISTERED's, so a saved row built from this is the
# whitelist entry's twin.
FULL = {
    "accountNumber": REGISTERED["accountNumber"],
    "routingNumber": REGISTERED["routingNumber"],
    "accountType": "CHECKING",
    "bankName": "ZZZTEST Bank",
    "bankAddress": {
        "addressLine1": "1 Bank Plaza",
        "city": "New York",
        "postalCode": "10010",
        "country": "US",
    },
    "type": "BUSINESS",
    "legalName": REGISTERED["legalName"],
    "postalAddress": {
        "addressLine1": "500 Market St",
        "city": "New York",
        "postalCode": "10010",
        "country": "US",
    },
}


def edit_url(cp_id, customer=CID) -> str:
    return f"/customers/{customer}/contacts/{cp_id}/edit"


def flat(recipient: dict, prefix: str = "f.destination.recipient") -> dict[str, str]:
    """One recipient payload as the form names the engine parses it under."""
    out: dict[str, str] = {}
    for key, value in recipient.items():
        if isinstance(value, dict):
            out |= flat(value, f"{prefix}.{key}")
        else:
            out[f"{prefix}.{key}"] = str(value)
    return out


def body(**overrides) -> bytes:
    """A complete edit submission: the label, the full recipient, and whatever
    this test changes."""
    fields = {"label": "ZZZTEST Globex", **flat(FULL)}
    fields.update(overrides.pop("fields", {}))
    fields.update({k: str(v) for k, v in overrides.items()})
    return str(httpx.QueryParams(sorted(fields.items()))).encode()


def token_of(html: str) -> str:
    """The nonce this render minted — what a confirm has to echo."""
    marker = 'name="intent" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


async def saved_row(session, cp_id) -> Counterparty:
    return (
        await session.execute(select(Counterparty).where(Counterparty.id == cp_id))
    ).scalar_one()


async def details(session) -> list[dict]:
    rows = (await session.execute(select(AuditEvent.action, AuditEvent.detail))).all()
    return [{"action": action, **(detail or {})} for action, detail in rows]


# --- the form is discovery's -----------------------------------------------------------


async def test_the_edit_form_is_the_recipient_subtree_of_this_contact_s_route(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id))).text

    # Discovery's own fields, prefilled with what is stored. The form an operator
    # types into is the one place this console shows a coordinate in full — the
    # masking rule's stated exception (DESIGN.md) and exactly what the payout
    # form's own prefill does.
    assert 'name="f.destination.recipient.accountNumber"' in html
    assert f'value="{FULL["accountNumber"]}"' in html
    assert 'name="f.destination.recipient.bankAddress.city"' in html
    # ...and nothing about the *payment*: an amount, a funding account or a
    # document widget on this page would be a payout form wearing a contact's
    # name.
    assert 'name="amount"' not in html
    assert 'name="virtualAccountId"' not in html
    assert "documentIds" not in html


async def test_an_unreachable_discovery_leaves_no_form_and_says_so(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", "/v2/payouts/requirements"): httpx.Response(
                        503, json={"type": "SERVER_ERROR", "title": "Discovery is down"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id))).text

    # A3: the console's own words for a code it has no sentence for — the CODE,
    # never the vendor's title. What the test protects is unchanged: the refusal
    # is on the page and no form is drawn from a discovery that failed.
    assert "Conduit refused this: SERVER_ERROR" in html
    assert "Discovery is down" not in html
    assert 'name="f.destination.recipient.accountNumber"' not in html


async def test_the_happy_path_stores_what_was_typed(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(
            web,
            edit_url(cp_id),
            body(
                label="ZZZTEST Globex Renamed",
                fields={
                    "f.destination.recipient.accountNumber": "000123456999",
                    "f.destination.recipient.bankAddress.city": "Boston",
                },
            ),
        )

    assert response.status_code == 204, response.text
    assert "err=" not in response.headers.get("hx-redirect", "")
    row = await saved_row(session, cp_id)
    stored = row.recipient
    assert row.label == "ZZZTEST Globex Renamed"
    assert stored["accountNumber"] == "000123456999"
    assert stored["bankAddress"]["city"] == "Boston"
    # Everything else survived the round trip through discovery's form.
    assert stored["legalName"] == FULL["legalName"]
    assert stored["postalAddress"] == FULL["postalAddress"]


async def test_an_invalid_aba_is_refused_and_nothing_is_written(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.routingNumber": "021000022"}),
        )

    assert response.status_code == 422
    row = await saved_row(session, cp_id)
    assert row.recipient["routingNumber"] == (
        FULL["routingNumber"]
    )
    assert await details(session) == []


async def test_a_missing_required_field_is_refused_by_the_same_engine(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(
            web, edit_url(cp_id), body(fields={"f.destination.recipient.legalName": ""})
        )

    assert response.status_code == 422
    assert (await saved_row(session, cp_id)).recipient[
        "legalName"
    ] == FULL["legalName"]


async def test_an_empty_name_is_refused(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(web, edit_url(cp_id), body(label="   "))

    assert response.status_code == 422
    assert counterparties.NO_NAME in response.text
    assert (await saved_row(session, cp_id)).label == "ZZZTEST Globex"


async def test_a_name_another_live_contact_holds_is_refused(session):
    await store(session, label="ZZZTEST Taken", recipient=dict(FULL))
    cp_id = await store(
        session, label="ZZZTEST Mine", recipient={**FULL, "accountNumber": "000999888777"}
    )
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(
            web,
            edit_url(cp_id),
            body(
                label="ZZZTEST Taken",
                fields={"f.destination.recipient.accountNumber": "000999888777"},
            ),
        )

    assert response.status_code == 422
    assert "already has a contact called" in response.text
    assert (await saved_row(session, cp_id)).label == "ZZZTEST Mine"


async def test_a_key_this_route_does_not_declare_is_kept_and_named(session):
    """Discovery describes one route on one day. A stored key it does not declare
    is neither editable here nor silently dropped."""
    cp_id = await store(session, recipient={**FULL, "somethingElse": "keep me"})
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        page = await web.get(edit_url(cp_id))
        response = await post(web, edit_url(cp_id), body())

    assert "somethingElse" in page.text and "keep me" not in page.text
    assert response.status_code == 204
    stored = (await saved_row(session, cp_id)).recipient
    assert stored["somethingElse"] == "keep me"


async def test_clearing_an_optional_field_removes_it(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        response = await post(
            web, edit_url(cp_id), body(fields={"f.destination.recipient.bankName": ""})
        )

    assert response.status_code == 204
    stored = (await saved_row(session, cp_id)).recipient
    assert "bankName" not in stored


# --- isolation: another customer's, an archived one, a deleted one ----------------------


async def test_another_customer_s_contact_is_not_editable(session):
    cp_id = await store(session, customer_id=OTHER_CID, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        page = await web.get(edit_url(cp_id))
        response = await post(web, edit_url(cp_id), body())

    # The same answer an invented id gets — never a 403, which would confirm the
    # id exists somewhere.
    assert page.status_code == 303 and response.status_code == 204
    assert "err=" in page.headers["location"]
    assert "err=" in response.headers["hx-redirect"]
    row = await saved_row(session, cp_id)
    assert row.label == "ZZZTEST Globex" and row.recipient == FULL


async def test_an_archived_contact_is_not_editable(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(web, f"{LIST}/{cp_id}/archive")
        page = await web.get(edit_url(cp_id))
        response = await post(web, edit_url(cp_id), body(label="ZZZTEST Zombie"))

    assert page.status_code in (204, 303)
    assert response.status_code in (204, 303)
    assert (await saved_row(session, cp_id)).label == "ZZZTEST Globex"


# --- the audit row ---------------------------------------------------------------------


async def test_the_audit_row_carries_masked_identity_diffs_and_field_names(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(
            web,
            edit_url(cp_id),
            body(
                label="ZZZTEST Globex Two",
                fields={
                    "f.destination.recipient.accountNumber": "000123456999",
                    "f.destination.recipient.bankAddress.city": "Boston",
                },
            ),
        )

    row = next(d for d in await details(session) if d["action"] == "counterparty.edited")
    assert row["identity"] == {"accountNumber": "••••6789→••••6999"}
    assert row["fields"] == ["accountNumber", "bankAddress.city"]
    # Labels are not PII and are the one thing a history panel is useless
    # without.
    assert row["label"] == "ZZZTEST Globex Two"
    assert row["renamed_from"] == "ZZZTEST Globex"
    assert row["counterparty"] == str(cp_id)


async def test_a_non_identity_edit_records_names_only(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(
            web, edit_url(cp_id), body(fields={"f.destination.recipient.phone": "+15550100"})
        )

    row = next(d for d in await details(session) if d["action"] == "counterparty.edited")
    assert "identity" not in row
    assert row["fields"] == ["phone"]


async def test_no_audit_row_this_round_writes_holds_a_full_coordinate(session):
    """The rule, proved by walking every row rather than by inspecting the two
    this round happens to write: an account number or an IBAN in the trail is a
    coordinate in a log the retention rules do not reach.
    """
    # The IBAN is a canary: this route does not declare one, so the form never
    # touches it — and it must still never turn up in a log.
    iban = "DE89370400440532013000"
    cp_id = await store(session, recipient={**FULL, "iban": iban})
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app, groups="admins") as web:
        # An identity change, so the edit row carries a diff to be masked. (The
        # stored IBAN is why this contact has no whitelist twin — REGISTERED
        # states none, and one is not the other.)
        await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.accountNumber": "000123450000"}),
        )
        await post(web, f"{LIST}/{cp_id}/rename", b"label=ZZZTEST+Renamed")
        page = await web.get(edit_url(cp_id))
        delete_token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        await post(
            web,
            f"{LIST}/{cp_id}/delete",
            f"intent={delete_token}&confirm={delete_token}".encode(),
        )

    assert page.status_code == 200
    written = str(await details(session))
    for secret in (FULL["accountNumber"], "000123450000", iban):
        assert secret not in written, secret
    assert {"counterparty.edited", "counterparty.deleted"} <= {
        d["action"] for d in await details(session)
    }


# --- the whitelist consequence ----------------------------------------------------------


async def test_a_registered_twin_is_stated_on_the_form_before_anything_is_typed(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id))).text

    assert "This contact is registered for intercompany payouts" in html
    assert REGISTERED["id"] in html


async def test_changing_an_identity_key_under_a_twin_asks_before_saving(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        first = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.accountNumber": "000123456999"}),
        )

    assert first.status_code == 200
    assert "This edit drops" in first.text
    assert "••••6789→••••6999" in first.text
    # Nothing was written by the step that only asked.
    stored = (await saved_row(session, cp_id)).recipient
    assert stored["accountNumber"] == FULL["accountNumber"]
    assert await details(session) == []


async def test_the_second_click_saves_and_the_capability_drops(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        before = await web.get(LIST)
        first = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.accountNumber": "000123456999"}),
        )
        token = token_of(first.text)
        second = await post(
            web,
            edit_url(cp_id),
            body(
                intent=token,
                confirm=token,
                fields={"f.destination.recipient.accountNumber": "000123456999"},
            ),
        )
        after = await web.get(LIST)

    assert "Showing 1 contact" in before.text
    assert before.text.count("Saved for payouts") == 1
    assert before.text.count("Registered for intercompany payouts") == 1
    assert second.status_code == 204
    # The capability is a live join and recomputes by itself — no cache to lie.
    # The coordinates no longer match, so the one row becomes two: this console's
    # contact, saved and not whitelisted, and Conduit's registration, still
    # registered and still pointing where it always did.
    assert "Showing 2 contacts" in after.text
    assert after.text.count("Saved for payouts") == 1
    assert after.text.count("Registered for intercompany payouts") == 1
    saved_cell = after.text[: after.text.index(REGISTERED["id"])]
    assert "Registered for intercompany payouts" not in saved_cell
    row = next(d for d in await details(session) if d["action"] == "counterparty.edited")
    assert row["dropped_whitelist"] == REGISTERED["id"]


async def test_a_non_identity_change_under_a_twin_needs_no_confirmation(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        response = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.bankAddress.city": "Boston"}),
        )
        after = await web.get(LIST)

    assert response.status_code == 204
    assert "Registered for intercompany payouts" in after.text
    assert (await saved_row(session, cp_id)).recipient[
        "bankAddress"
    ]["city"] == "Boston"


async def test_an_identity_change_with_no_twin_saves_straight_through(session):
    cp_id = await store(session, recipient={**FULL, "accountNumber": "000999888777"})
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        response = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.accountNumber": "000999888666"}),
        )

    assert response.status_code == 204
    assert (await saved_row(session, cp_id)).recipient[
        "accountNumber"
    ] == "000999888666"


async def test_an_unreadable_whitelist_gates_too(session):
    """Three states, not two: the absence of a twin from a failed read is not
    evidence that there is none, and a silent save on that absence would drop a
    capability without ever mentioning it."""
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", f"/v2/customers/{CID}/whitelist-recipients"): httpx.Response(
                        500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        page = await web.get(edit_url(cp_id))
        response = await post(
            web,
            edit_url(cp_id),
            body(fields={"f.destination.recipient.accountNumber": "000123456999"}),
        )

    assert "Whitelist status unknown" in page.text
    assert response.status_code == 200 and "cannot say whether a registration" in response.text
    assert await details(session) == []


# --- the second tier: delete ------------------------------------------------------------


async def test_delete_needs_admin(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:  # ops = operator
        refused = await post(web, f"{LIST}/{cp_id}/delete", b"")
        page = await web.get(edit_url(cp_id))

    assert refused.status_code == 403
    assert "Delete contact" not in page.text
    assert (await saved_row(session, cp_id)).recipient == FULL


async def test_an_admin_is_asked_before_the_coordinates_are_destroyed(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        page = await web.get(edit_url(cp_id))
        asked = await post(web, f"{LIST}/{cp_id}/delete", b"")

    assert "Delete contact" in page.text
    assert asked.status_code == 200 and "destroys the stored coordinates" in asked.text
    assert (await saved_row(session, cp_id)).recipient == FULL
    assert await details(session) == []


async def test_delete_purges_the_payload_and_keeps_the_shell(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        done = await post(
            web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode()
        )
        listing = await web.get(LIST)

    assert done.status_code == 204
    row = await saved_row(session, cp_id)
    # The payload is destroyed — readable, and empty. Not NULL: `None` already
    # means *unreadable* on every read path, and a deliberate deletion must not
    # look like a corrupt row.
    assert row.recipient == {}
    # The shell stays, and it is archived, so it is gone from every active list.
    assert row.label == "ZZZTEST Globex" and row.archived_at is not None
    assert "ZZZTEST Globex" not in listing.text

    audited = next(d for d in await details(session) if d["action"] == "counterparty.deleted")
    assert audited["label"] == "ZZZTEST Globex"
    assert "accountNumber" not in str(audited)


async def test_deleting_twice_is_refused_rather_than_silently_repeated(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        await post(web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode())
        again = await post(web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode())

    # The row is archived, so it resolves to nothing — the same answer an
    # invented id gets, and no second audit row.
    assert again.status_code == 204
    assert sum(1 for d in await details(session) if d["action"] == "counterparty.deleted") == 1


async def test_the_ledger_filter_still_resolves_a_deleted_contact(session):
    cp_id = await store(session, recipient=dict(FULL))
    operation = Operation(
        type="payout_create",
        state="confirmed",
        actor_id="usr_1",
        actor_email="ops@example.com",
        customer_id=CID,
        idempotency_key=str(uuid.uuid4()),
        request_path="/v2/payouts",
        request_hash="hash-for-the-contact-filter",
        conduit_resource_id="txn_kept",
    )
    session.add(operation)
    await session.flush()
    session.add(
        AuditEvent(
            action=counterparties.USED_ACTION,
            actor_id="usr_1",
            actor_email="ops@example.com",
            operation_id=operation.id,
            detail={"counterparty": str(cp_id), "label": "ZZZTEST Globex"},
        )
    )
    await session.commit()

    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", "/v2/transactions/txn_kept"): httpx.Response(
                        200,
                        json={
                            "id": "txn_kept",
                            "type": "withdrawal",
                            "status": "completed",
                            "customerId": CID,
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        await post(web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode())
        filtered = await web.get(
            f"/transactions?type=withdrawal&customerId={CID}&contact={cp_id}"
        )

    assert filtered.status_code == 200
    assert "No such contact" not in filtered.text
    assert "txn_kept" in filtered.text
    # Named honestly: the coordinates are gone, the name and the payments are not.
    assert "ZZZTEST Globex" in filtered.text
    assert "coordinates were purged" in filtered.text


async def test_a_deleted_contact_is_offered_to_nothing(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        await post(web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode())
        picker = await web.get(
            f"/customers/{CID}/payouts/new?purpose=payment_for_goods_or_services"
            "&rail=fedwire&recipientType=business&destinationCountry=USA"
        )
        prefill = await web.get(edit_url(cp_id))
        bridge = await web.get(f"/customers/{CID}/recipients?contact={cp_id}")

    assert "ZZZTEST Globex" not in picker.text
    assert prefill.status_code in (204, 303)
    assert "That contact could not be used" in bridge.text


async def test_purging_reaches_an_already_archived_contact(session):
    """The model function's own scope. Archive-then-purge is the order an operator
    would use, and `_scoped` (which refuses archived rows) is deliberately not what
    the purge uses."""
    cp_id = await store(session, recipient=dict(FULL))
    assert await counterparties.archive(session, CID, str(cp_id))
    await session.commit()
    assert await counterparties.purge(session, CID, str(cp_id))
    await session.commit()

    row = await saved_row(session, cp_id)
    assert row.recipient == {}
    # The archive timestamp is not rewritten: when a contact was retired is a
    # fact, and purging is not a re-archiving.
    assert row.archived_at is not None
    assert not await counterparties.purge(session, OTHER_CID, str(cp_id))


async def test_a_purged_row_can_claim_no_capability(session):
    """A deleted contact has nothing to prove identity with, so it merges with
    nothing — the same rule that keeps two blank records from becoming one row."""
    row = {
        "id": uuid.uuid4(),
        "label": "ZZZTEST Purged",
        "recipient": counterparties.PURGED,
        "rail_family": "us",
    }
    [merged] = counterparties.merge([row], [])
    assert merged["deleted"] is True and merged["unreadable"] is False
    assert counterparties.merge([row], [REGISTERED])[0]["entry"] is None


# --- the history panel ------------------------------------------------------------------


async def test_the_history_panel_tells_the_whole_story(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(web, f"{LIST}/{cp_id}/rename", b"label=ZZZTEST+Globex+II")
        await post(
            web,
            edit_url(cp_id),
            body(label="ZZZTEST Globex II", fields={"f.destination.recipient.phone": "+15550100"}),
        )
        listing = await web.get(LIST)
        await post(web, f"{LIST}/{cp_id}/archive")
        panel = await web.get(f"{LIST}/{cp_id}/history")

    assert panel.status_code == 200
    seen = visible(panel.text)
    assert "Renamed" in seen and "Edited" in seen and "Archived" in seen
    assert "phone" in seen
    # The boundary this panel cannot cross, stated on it.
    assert "not to this record" in seen
    # And the affordance that opens it is on the list, in the first cell.
    assert f'hx-get="{LIST}/{cp_id}/history"' in listing.text
    assert 'id="drawer-body"' in listing.text


async def test_a_trail_longer_than_the_panel_says_so_instead_of_ending_quietly(session):
    """`counterparties.history` has always capped at 50 and the
    drawer never mentioned it, so a long trail ended mid-story looking finished.
    A drawer is not a place for a pager; the honest form is the stated cap."""
    from app.web.contacts import HISTORY_LIMIT

    cp_id = await store(session, recipient=dict(FULL))
    for n in range(HISTORY_LIMIT + 1):
        session.add(
            AuditEvent(
                action="counterparty.rename",
                actor_id="usr_1",
                actor_email="ops@example.com",
                detail={"counterparty": str(cp_id), "customer": CID, "label": f"name-{n}"},
            )
        )
    await session.commit()

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        panel = await web.get(f"{LIST}/{cp_id}/history")

    assert f"The most recent {HISTORY_LIMIT} entries" in visible(panel.text)
    # The sentinel row is counted, never rendered: exactly the cap on screen.
    assert panel.text.count("<tr>") == HISTORY_LIMIT + 1  # the header row too

    # And a trail that fits carries no such sentence — a cap stated when it is
    # not in play is its own small lie.
    await session.execute(text("delete from audit_events where action = 'counterparty.rename'"))
    await session.commit()
    async with signed_in(make_app(stub(routes([])))) as web:
        short = await web.get(f"{LIST}/{cp_id}/history")
    assert "The most recent" not in visible(short.text)


async def test_an_unknown_action_renders_raw_rather_than_disappearing(session):
    cp_id = await store(session, recipient=dict(FULL))
    session.add(
        AuditEvent(
            action="counterparty.teleported",
            actor_id="usr_1",
            actor_email="ops@example.com",
            detail={"counterparty": str(cp_id), "customer": CID},
        )
    )
    await session.commit()

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        panel = await web.get(f"{LIST}/{cp_id}/history")

    assert "counterparty.teleported" in panel.text
    assert "no description for that action" in panel.text


async def test_a_save_is_matched_by_name_only_from_this_contact_s_own_lifetime(session):
    """`counterparty.save` records the label, never the id. A save belonging to an
    earlier contact of the same name necessarily predates this row, and is not
    claimed."""
    old = await store(session, label="ZZZTEST Globex", recipient=dict(FULL))
    session.add(
        AuditEvent(
            action=counterparties.SAVE_ACTION,
            actor_id="usr_1",
            actor_email="ops@example.com",
            detail={"customer": CID, "label": "ZZZTEST Globex", "rail_family": "us"},
        )
    )
    await session.commit()
    assert await counterparties.archive(session, CID, str(old))
    await session.commit()
    fresh = await store(session, label="ZZZTEST Globex", recipient=dict(FULL))

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        older = await web.get(f"{LIST}/{old}/history")
        newer = await web.get(f"{LIST}/{fresh}/history")

    assert "Saved from a payout" in visible(older.text)
    assert "Saved from a payout" not in visible(newer.text)


async def test_the_bridge_is_recorded_against_the_contact(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        page = await web.get(f"/customers/{CID}/recipients?contact={cp_id}")
        # Really uploaded: the registration resolves every evidence id
        # against this console's upload ledger before it sends (`documents_stub`
        # mints the id from the filename).
        assert (
            await upload(web, purpose="feature_request", filename="doc_evidence.png")
        ).status_code == 200
        await post(
            web,
            f"/customers/{CID}/recipients",
            str(
                httpx.QueryParams(
                    {
                        "rail": "us",
                        "contact": str(cp_id),
                        "intent": token_of(page.text),
                        "documentIds": "doc_evidence",
                        "f.relationship": "self",
                        "f.legalName": FULL["legalName"],
                    }
                )
            ).encode(),
        )
        panel = await web.get(f"{LIST}/{cp_id}/history")

    assert "Put forward for whitelisting" in visible(panel.text)


async def test_the_history_of_an_id_this_customer_does_not_have_is_a_card_not_a_500(session):
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        panel = await web.get(f"{LIST}/{uuid.uuid4()}/history")

    # Always 200: htmx drops a non-2xx without a swap, and a click that appears
    # to do nothing is the one outcome this pattern cannot have.
    assert panel.status_code == 200
    assert "No such contact for this customer" in panel.text


async def test_a_deleted_contact_s_history_says_which_kind_of_gone_it_is(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        await post(web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode())
        panel = await web.get(f"{LIST}/{cp_id}/history")

    seen = visible(panel.text)
    assert "deleted — coordinates purged" in seen
    assert FULL["accountNumber"] not in panel.text


# --- roles, CSRF, guards ------------------------------------------------------------------


async def test_a_viewer_may_read_history_and_nothing_else(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="readers") as web:
        panel = await web.get(f"{LIST}/{cp_id}/history")
        page = await web.get(edit_url(cp_id))
        edit = await post(web, edit_url(cp_id), body())
        listing = await web.get(LIST)

    assert panel.status_code == 200
    assert page.status_code == 403 and edit.status_code == 403
    assert "/edit\">Edit</a>" not in listing.text
    assert "History</button>" in listing.text


async def test_the_new_mutations_need_the_csrf_header(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        for url, payload in ((edit_url(cp_id), body()), (f"{LIST}/{cp_id}/delete", b"")):
            response = await web.post(
                url,
                content=payload,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403, url

    row = await saved_row(session, cp_id)
    assert row.recipient == FULL
    assert row.archived_at is None


async def test_the_edit_form_carries_the_house_hx_guards(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id))).text

    form = html[html.index('id="contact-edit"') : html.index('id="contact-edit"') + 400]
    assert 'hx-target="main"' in form and 'hx-select="main"' in form
    assert 'hx-sync="this:drop"' in form and "hx-disabled-elt" in form
    assert 'name="intent"' in form


async def test_the_unreadable_repair_warning_says_what_cancelling_costs(session):
    """The design gate's carried item: the warning had two clauses — saving
    REPLACES a destination nobody can see, and here is how to repair it — and
    was silent on the third question an operator actually has at that moment.

    An operator who opens this form, reads that saving overwrites coordinates
    the console cannot show them, and then hesitates needs to be told that
    backing out is free. Without clause (c) the safest action on the page is the
    one with no stated consequence, which is how a warning talks somebody into
    saving something they did not mean to.
    """
    from sqlalchemy import text

    cp_id = await store(session, recipient=dict(FULL))
    await session.execute(
        text("update counterparties set recipient = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": cp_id},
    )
    await session.commit()

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        page = await web.get(edit_url(cp_id))
    shown = " ".join(page.text.split())

    assert "This contact&#39;s stored coordinates cannot be read" in page.text
    assert "the only way to repair this row" in shown
    assert "Cancelling loses nothing that was not already lost" in shown
    assert "payments already made to them are unaffected" in shown


async def test_repairing_an_unreadable_contact_records_an_unknown_prior_not_a_blank_one(
    session,
):
    """The repair path's audit row read `legalName: —→ZZZTEST
    Globex`, which asserts the record previously had **no** legal name. Nobody
    knows what it had — that is the whole reason this form starts empty. An
    audit trail that fabricates an absence is worse than one that says
    "unknown", because a reader cannot tell the two apart later.
    """
    from sqlalchemy import text

    cp_id = await store(session, recipient=dict(FULL))
    await session.execute(
        text("update counterparties set recipient = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": cp_id},
    )
    await session.commit()

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        saved = await post(web, edit_url(cp_id), body())
    assert "err=" not in saved.headers["hx-redirect"], saved.headers["hx-redirect"]

    edited = next(row for row in await details(session) if row["action"] == "counterparty.edited")
    identity = edited["identity"]
    # Every identity key the repair wrote says the prior is unknown, and the new
    # value keeps the module's masking policy.
    assert identity["legalName"] == f"unreadable→{FULL['legalName']}"
    assert identity["accountNumber"].startswith("unreadable→")
    assert FULL["accountNumber"] not in identity["accountNumber"]
    # Never the fabricated absence.
    assert "—→" not in json.dumps(identity)


# --- cloning ----------------------------------------------------------------
#
# "Add the ability to clone a contact: same contact, but change the rail and
# account details." It is a MODE of this page, not a page of its own: same URL,
# same `contact.edit` permission, same validation, one branch in the handler —
# so the role matrix does not move for an action it already covers
# (`tests/test_permissions.py` is what proves that, and it is untouched).


CLONE = "?clone=1"


def clone_body(**overrides) -> bytes:
    return body(clone="1", **overrides)


async def test_the_clone_form_is_the_source_prefilled_with_no_name(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id) + CLONE)).text

    # The source's coordinates, in discovery's own boxes…
    assert f'value="{FULL["accountNumber"]}"' in html
    # …and no name: `counterparties.save` upserts on the label, so prefilling the
    # source's would make "save over the original" the default keystroke.
    assert '<input type="text" id="contact-label" name="label" value=""' in html
    assert 'name="clone" value="1"' in html
    assert "Save as a new contact" in html
    # Every rail is offered, the other families' included — a payee reachable
    # both ways is the case this feature exists for.
    rails = html.split('id="clone-rail-select"')[1].split("</select>")[0]
    for rail in ("fedwire", "ach", "rtp", "fednow", "swift", "sepa"):
        assert f'value="{rail}"' in rails
    # The honesty the design asks for, on the page rather than after the fact.
    assert "the first payout to it is what proves the coordinates" in " ".join(html.split())


async def test_the_clone_form_reshapes_to_the_rail_it_is_being_saved_for(session):
    """A `sepa` clone of a `us` contact asks for an IBAN and a BIC, because the
    fields are discovery's answer for the rail — not a copy of the source's."""
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(
        stub(
            routes(
                [],
                extra={("GET", "/v2/payouts/requirements"): httpx.Response(
                    200, json=json.loads(
                        (ROOT / "tests/fixtures/payout_requirements_sepa_business.json").read_text()
                    )
                )},
            )
        )
    )
    async with signed_in(app) as web:
        html = (await web.get(edit_url(cp_id) + CLONE + "&rail=sepa")).text

    assert 'name="f.destination.recipient.iban"' in html
    assert 'name="f.destination.recipient.routingNumber"' not in html
    # …and the keys this route does not describe are named as NOT copied, rather
    # than carried into a record whose own route would never send them.
    assert "not copied" in " ".join(html.split())


async def test_a_clone_is_a_new_row_and_the_source_is_untouched(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        done = await post(
            web,
            edit_url(cp_id),
            clone_body(label="ZZZTEST Globex EU",
                       fields={"f.destination.recipient.accountNumber": "000999888777"}),
        )

    assert "Cloned" in unquote_plus(done.headers["HX-Redirect"])
    rows = (
        await session.execute(select(Counterparty).order_by(Counterparty.label))
    ).scalars().all()
    assert [r.label for r in rows] == ["ZZZTEST Globex", "ZZZTEST Globex EU"]
    source, clone = rows
    # The source: byte for byte what it was.
    assert source.id == cp_id
    assert source.recipient == FULL
    # The clone: a new id, the typed coordinates, the same payee's corridor.
    assert clone.id != cp_id
    assert clone.recipient["accountNumber"] == "000999888777"
    assert clone.rail_family == "us"
    assert clone.recipient_type == source.recipient_type
    assert clone.destination_country == source.destination_country

    trail = [d for d in await details(session) if d["action"] == "counterparty.cloned"]
    assert len(trail) == 1
    assert trail[0]["counterparty"] == str(clone.id)
    assert trail[0]["cloned_from"] == str(cp_id)
    assert trail[0]["source_label"] == "ZZZTEST Globex"
    # The masking rule holds here as everywhere: no coordinate in an audit row.
    assert "000999888777" not in json.dumps(trail[0])


async def test_a_clone_may_change_the_rail_family(session):
    """The ask, in the requester's words — "same contact, but change the rail and account
    details" — and the family follows the rail it was saved for."""
    cp_id = await store(session, recipient=dict(FULL))
    sepa = json.loads(
        (ROOT / "tests/fixtures/payout_requirements_sepa_business.json").read_text()
    )
    app = make_app(
        stub(routes([], extra={
            ("GET", "/v2/payouts/requirements"): httpx.Response(200, json=sepa)}))
    )
    async with signed_in(app) as web:
        await post(
            web,
            edit_url(cp_id),
            body(
                clone="1",
                rail="sepa",
                label="ZZZTEST Globex SEPA",
                fields={
                    "f.destination.recipient.iban": "DE89370400440532013000",
                    "f.destination.recipient.bic": "COBADEFFXXX",
                    "f.destination.recipient.legalName": "ZZZTEST Globex Supplies GmbH",
                },
            ),
        )

    clone = (
        await session.execute(
            select(Counterparty).where(Counterparty.label == "ZZZTEST Globex SEPA")
        )
    ).scalar_one()
    assert clone.rail_family == "sepa"
    assert clone.recipient["iban"] == "DE89370400440532013000"
    # The us coordinates are NOT carried across: a record may only hold what its
    # own route could send.
    assert "accountNumber" not in clone.recipient
    assert "routingNumber" not in clone.recipient


async def test_a_clone_that_would_merge_back_into_its_source_is_refused(session):
    """`counterparties.merge` cannot tell two saved rows at one destination apart:
    both go AMBIGUOUS, neither may claim the registration, and every attribution
    built on that join inherits it. The clone is the one write that can produce
    the pair on purpose, so it refuses to."""
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        refused = await post(web, edit_url(cp_id), clone_body(label="ZZZTEST Globex Copy"))

    assert refused.status_code == 422
    body_text = " ".join(refused.text.split())
    assert "A clone has to be a different destination" in body_text
    # …and it names the contact it would have collided with.
    assert "already saved for this customer as" in body_text
    assert "ZZZTEST Globex" in body_text
    assert "Change the rail or the account details" in body_text
    assert (await session.execute(select(func.count(Counterparty.id)))).scalar_one() == 1


async def test_a_clone_may_not_take_a_live_contacts_name(session):
    """`counterparties.save` upserts on `(customer, lower(label))`, so a duplicate
    label is not a duplicate row — it is the other contact's coordinates being
    overwritten. Refused before anything is written, in the sentence every other
    naming collision in this module uses."""
    cp_id = await store(session, recipient=dict(FULL))
    await store(session, label="ZZZTEST Globex EU", recipient={"accountNumber": "1",
                                                              "legalName": "Other"})
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        for name in ("ZZZTEST Globex EU", "zzztest globex eu", "ZZZTEST Globex"):
            refused = await post(
                web,
                edit_url(cp_id),
                clone_body(label=name,
                           fields={"f.destination.recipient.accountNumber": "000999888777"}),
            )
            assert refused.status_code == 422, name
            assert "already has a contact called" in " ".join(refused.text.split()), name

    assert (await session.execute(select(func.count(Counterparty.id)))).scalar_one() == 2


async def test_a_clone_obeys_the_same_validation_a_payout_would(session):
    """No second kind of contact: the boxes are discovery's and an ABA that fails
    its checksum is refused here exactly as it is on the payout form."""
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        refused = await post(
            web,
            edit_url(cp_id),
            clone_body(label="ZZZTEST Globex Bad",
                       fields={"f.destination.recipient.routingNumber": "021000022"}),
        )
    assert refused.status_code == 422
    assert (await session.execute(select(func.count(Counterparty.id)))).scalar_one() == 1


async def test_a_clone_leaves_the_sources_whitelist_warning_alone(session):
    """The registration belongs to the coordinates Conduit reviewed, and a clone
    has new ones. The source's consequence warning is about editing the source,
    so it is not on this screen — and the clone cannot drop what it never had."""
    cp_id = await store(session, recipient=dict(TWIN))
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        editing = (await web.get(edit_url(cp_id))).text
        cloning = (await web.get(edit_url(cp_id) + CLONE)).text

    assert "registered for intercompany payouts" in editing
    assert "registered for intercompany payouts" not in cloning
    assert "keeps everything it has, including its whitelist registration" in (
        " ".join(cloning.split())
    )


async def test_the_clones_history_names_the_contact_it_came_from(session):
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        await post(
            web,
            edit_url(cp_id),
            clone_body(label="ZZZTEST Globex EU",
                       fields={"f.destination.recipient.accountNumber": "000999888777"}),
        )
        clone = (
            await session.execute(
                select(Counterparty.id).where(Counterparty.label == "ZZZTEST Globex EU")
            )
        ).scalar_one()
        panel = (await web.get(f"{edit_url(clone).removesuffix('/edit')}/history")).text

    assert "Cloned from another contact" in panel
    assert "ZZZTEST Globex" in panel
    assert str(cp_id) in panel


async def test_the_clone_page_paints_nothing_its_actor_cannot_open(session):
    """The rule on the new affordance. The clone link is a `contact.edit`
    GET on the URL its own page is already at — so an actor who is reading this
    page can open it, and one who cannot never sees the page at all (the route
    is gated, not the link). The delete tier stays separately gated, which is
    what makes this check non-vacuous."""
    from tests.web_harness import forbidden_affordances, signed_in_as

    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in_as(app, "contact.edit") as web:
        html = (await web.get(edit_url(cp_id) + CLONE)).text

    assert forbidden_affordances(app, html, {"console.view", "contact.edit"}) == []
    assert "Delete permanently" not in html and "Delete contact" not in html
    # And a viewer cannot reach either mode of the page in the first place.
    async with signed_in_as(app) as web:
        assert (await web.get(edit_url(cp_id) + CLONE)).status_code == 403
        assert (await web.get(edit_url(cp_id))).status_code == 403


async def test_a_clone_cannot_overwrite_a_row_the_label_check_never_saw(session, monkeypatch):
    """Gate finding m1, reproduced then fixed (probe P4).

    The label check is a READ, and the write used to be `counterparties.save` —
    an UPSERT on `(customer, lower(label))`. A row committed between the two was
    therefore not a refusal but a silent overwrite: another contact's coordinates
    replaced, and this clone's `counterparty.cloned` audit row landing on *that*
    contact's id. The after-the-fact "was this an update?" check could not see it
    either, because it compared against the same stale read.

    A clone is an INSERT now (`counterparties.insert`), so the refusal comes from
    the partial unique index itself and the race has nowhere to land. The racer's
    row is hidden from the read here, which is exactly what a commit landing one
    millisecond later does.
    """
    from app import counterparties as module

    source = await store(session, recipient=dict(FULL))
    racer = await store(
        session, label="ZZZTEST Racer", recipient={**FULL, "accountNumber": "111222333444"}
    )
    real_rows = module.rows

    async def rows_without_racer(*a, **kw):
        return [r for r in await real_rows(*a, **kw) if r["id"] != racer]

    monkeypatch.setattr(module, "rows", rows_without_racer)

    app = make_app(stub(routes([])))
    async with signed_in(app) as web:
        refused = await post(
            web,
            edit_url(source),
            body(clone="1", label="zzztest racer",
                 fields={"f.destination.recipient.accountNumber": "000999888777"}),
        )

    assert refused.status_code == 422
    assert "already has a contact called" in " ".join(refused.text.split())
    session.expire_all()
    rows = (await session.execute(select(Counterparty))).scalars().all()
    # Nothing was written: not a new row, not over the racer, not an audit row.
    assert len(rows) == 2
    racer_row = next(r for r in rows if r.id == racer)
    assert racer_row.label == "ZZZTEST Racer"
    assert racer_row.recipient["accountNumber"] == "111222333444"
    assert [d for d in await details(session) if d["action"] == "counterparty.cloned"] == []


async def test_the_confirm_echo_still_matches_now_that_the_token_is_sealed(session):
    """`_confirmed` compares the `confirm` field to the
    `intent` field — raw string equality, never through `intent_of` — so sealing
    the nonce had to leave it working: both fields are the *same* `{% set token %}`
    on the same render, so the equality holds whatever the token is made of. This
    pins that, and pins that the value really did become a seal rather than the
    bare uuid it used to be — a test asserting only "the delete went through"
    would still pass if the seal had silently been skipped on this screen.
    """
    from app.auth.tokens import unseal

    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        asked = await post(web, f"{LIST}/{cp_id}/delete", b"")
        token = token_of(asked.text)
        done = await post(
            web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm={token}".encode()
        )

    # A seal, not a uuid: this screen mints through `new_intent` like every other.
    with pytest.raises(ValueError):
        uuid.UUID(token)
    assert uuid.UUID((unseal(token) or {})["n"])
    # And the echo confirmed, so the second tier still has its consequence step.
    assert done.status_code == 204
    assert (await saved_row(session, cp_id)).recipient == {}


async def test_a_delete_whose_confirm_does_not_echo_the_token_stores_nothing(session):
    """The non-vacuity half of the test above: equality is doing the work, not the
    mere presence of two fields. A `confirm` from some other render — or from a
    forger's own keyboard — is not this screen's consequence."""
    cp_id = await store(session, recipient=dict(FULL))
    app = make_app(stub(routes([])))
    async with signed_in(app, groups="admins") as web:
        token = token_of((await post(web, f"{LIST}/{cp_id}/delete", b"")).text)
        answered = await post(
            web, f"{LIST}/{cp_id}/delete", f"intent={token}&confirm=something-else".encode()
        )

    # The consequence screen again, and the coordinates are all still there.
    assert answered.status_code == 200 and "destroys the stored coordinates" in answered.text
    assert (await saved_row(session, cp_id)).recipient == FULL
