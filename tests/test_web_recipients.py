"""Whitelisting a destination: the registration form, revoke, the group-entity
shortcut, and the saved-contact bridge.

The load-bearing assertions here are the ones a payout depends on: only
`registered` entries are offered downstream, both mutations are ledgered so a
double-click cannot register a bank account twice, and neither prefill can be
edited into registering coordinates the stored record does not name.

**The list itself is `/customers/{id}/contacts`** — a
registration is one capability of a contact, not a page of its own — so the
assertions about rows, pills and the just-registered explainer live against
`LIST` below and in `tests/test_contacts.py`.
"""

from __future__ import annotations

import json
from urllib.parse import unquote_plus

import httpx
from sqlalchemy import func, select

from app import documents, operations
from app.models import AuditEvent, Operation
from tests.conftest import settings_override
from tests.payments_fixtures import (
    CID,
    EUR_ACCOUNT,
    EUR_ACTIVE,
    OTHER_CID,
    PENDING,
    REGISTERED,
    REVOKED,
    USD_ACCOUNT,
    WHITELIST_PATH,
    encoded,
    page,
    recipient_form,
)
from tests.web_harness import (
    PNG,
    documents_stub,
    make_app,
    minted_intent,
    post,
    signed_in,
    stub,
    upload,
)

URL = f"/customers/{CID}/recipients"
LIST = f"/customers/{CID}/contacts"
TARGET_ACCOUNT = f"/v2/customers/{OTHER_CID}/virtual-accounts/{USD_ACCOUNT['id']}"

# What the evidence widget on this form uploads under (`payments.EVIDENCE_PURPOSE`,
# rendered as `data-purpose` and sent by `static/app.js`). The submit
# resolves every `evidenceDocumentIds` entry against this console's own upload
# ledger for exactly this purpose, so a test that registers has to upload first.
EVIDENCE_PURPOSE = "feature_request"


def routes(items=None, extra=None):
    return {
        ("GET", WHITELIST_PATH): page(items if items is not None else [REGISTERED, PENDING]),
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


async def evidence(web, *doc_ids: str) -> None:
    """Really upload the evidence a registration is about to name.

    `documents_stub` answers with the filename's stem, so naming the file after
    the id lets a test say which `doc_` id it means without depending on upload
    ordering. Default is `recipient_form`'s own — every registration that expects
    to reach Conduit needs it.
    """
    for doc_id in doc_ids or ("doc_evidence_1",):
        response = await upload(web, purpose=EVIDENCE_PURPOSE, filename=f"{doc_id}.png")
        assert response.status_code == 200, response.text


# --- list -----------------------------------------------------------------------------------


async def test_the_list_shows_every_status_with_its_own_pill():
    app = make_app(stub(routes([REGISTERED, PENDING, REVOKED])))
    async with signed_in(app) as web:
        response = await web.get(LIST)

    assert response.status_code == 200
    for status in ("Registered", "Pending review", "Revoked"):
        assert status in response.text
    # The ABA is the *fallback* identity on Contacts (`counterparties.identify`:
    # masked account coordinates, else the public bank identifier), and these
    # entries state an account number — so the masked form is what shows.
    assert "••••6789" in response.text and "ZZZTEST Globex Supplies LLC" in response.text
    # A revoked entry is not offered a Revoke button.
    assert response.text.count("Revoke</button>") == 2


async def test_an_unknown_status_renders_neutrally():
    """plan v2 §7: never inferred, never terminal, never a crash."""
    app = make_app(stub(routes([{**REGISTERED, "status": "quarantined"}])))
    async with signed_in(app) as web:
        response = await web.get(LIST)
    assert "Unknown: quarantined" in response.text


async def test_an_unreadable_whitelist_is_a_problem_not_an_empty_list():
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): httpx.Response(
                    500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(LIST)
    assert "Conduit refused this: SERVER_ERROR" in response.text  # A3
    assert "No whitelist recipients registered" not in response.text


# --- the three DTO variants ------------------------------------------------------------------


async def test_the_rail_picker_renders_each_static_dto():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        us = await web.get(URL + "?rail=us")
        swift = await web.get(URL + "?rail=swift")
        sepa = await web.get(URL + "?rail=sepa")

    assert 'name="f.routingNumber"' in us.text and 'name="f.iban"' not in us.text
    assert 'name="f.bic"' in swift.text and 'name="f.iban"' in swift.text
    assert 'name="f.iban"' in sepa.text and 'name="f.bic"' not in sepa.text
    for text in (us.text, swift.text, sepa.text):
        assert 'value="group_entity"' in text and 'value="self"' in text


async def test_an_unknown_rail_is_refused_rather_than_guessed():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(URL + "?rail=carrier_pigeon")
    assert "Unknown rail" in unquote_plus(response.headers["location"])


# --- registering -----------------------------------------------------------------------------


async def test_registering_sends_the_dto_and_ledgers_it(session):
    """SUPERSEDED in its `doc_1`: this test used to hand the form a `doc_1`
    nobody had uploaded and assert it on the wire, which pinned the gap as
    though it were the contract. The id is uploaded for real now — same
    assertion, one real document behind it.
    """
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", WHITELIST_PATH): httpx.Response(201, json=PENDING)}), calls)
    )
    async with signed_in(app) as web:
        await evidence(web, "doc_1")
        response = await post(web, URL, encoded(recipient_form(documentIds="doc_1")))

    # The redirect names the entry it created, so the list can explain that
    # entry's status rather than flash one sentence for every outcome.
    # The redirect lands on **Contacts**, where the list is and where this
    # registration is now a capability of a row.
    assert response.headers["HX-Redirect"] == f"{LIST}?registered={PENDING['id']}"
    # By path, not by method: the evidence upload above is a POST too.
    sent = next(c for c in calls if c[1] == WHITELIST_PATH)
    assert json.loads(sent[2]) == {
        "rail": "us",
        "routingNumber": "021000021",
        "accountNumber": "000123456789",
        "relationship": "self",
        "legalName": "ZZZTEST Own Account LLC",
        "evidenceDocumentIds": ["doc_1"],
    }
    assert "documentIds" not in json.loads(sent[2])  # the DTO's own key, not the widget's
    op = (
        await session.execute(select(Operation).where(Operation.type == "whitelist_create"))
    ).scalar_one()
    assert op.state == "confirmed"
    assert op.conduit_resource_id == PENDING["id"]


async def test_evidence_the_operator_did_not_upload_here_never_reached_conduit(session):
    """`evidenceDocumentIds` is what a reviewer at Conduit reads to decide
    whether this account may be paid, and it was whatever the browser sent: a
    `doc_` id lifted off the operations panel, or simply guessed at, was
    published under this organisation's name as proof about a relationship
    nobody had evidenced.

    Both halves of that are here — another actor's real upload and an id that
    never existed — because they fail for the same reason and the operator has
    to be told the same thing about both.
    """
    other, _ = await documents.intake(
        session,
        data=PNG,
        filename="their-passport.png",
        purpose=EVIDENCE_PURPOSE,
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

    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", WHITELIST_PATH): httpx.Response(201, json=PENDING)}), calls)
    )
    async with signed_in(app) as web:
        refused = await post(
            web, URL, encoded(recipient_form(documentIds=["doc_theirs", "doc_guessed_9999"]))
        )

    assert refused.status_code == 422
    assert "Attach only documents you uploaded here for this purpose" in refused.text
    assert "2 of the attachments could not be matched" in refused.text
    # Nothing sent, nothing ledgered: the refusal is in front of
    # `operations.start`, not a rollback after it.
    assert [c for c in calls if c[1] == WHITELIST_PATH] == []
    assert (
        await session.execute(select(Operation).where(Operation.type == "whitelist_create"))
    ).scalars().all() == []
    # The form comes back with what the operator typed, rather than emptied.
    assert "021000021" in refused.text
    # …and the other actor's document is still perfectly attachable by them.
    assert await documents.attachable(
        session, ["doc_theirs"], purpose=EVIDENCE_PURPOSE, actor_id="usr_someone_else"
    ) == {"doc_theirs"}


async def test_a_registration_with_no_evidence_never_reaches_conduit(session):
    """**Verified live 2026-08-28.** `evidenceDocumentIds` is not merely
    `required`, it must be non-empty — an empty array answers `400
    VALIDATION_ERROR`, `/evidenceDocumentIds`, "Too small: expected array to have
    >=1 items". The DTO's own prose reads as though a `self` registration would
    be exempt; it is not."""
    calls: list = []
    app = make_app(stub(routes(), calls))
    form = recipient_form()
    del form["documentIds"]
    async with signed_in(app) as web:
        response = await post(web, URL, encoded(form))

    assert response.status_code == 422
    assert "at least 1 evidence document" in response.text
    assert "including a `self` one" in response.text
    assert [c for c in calls if c[0] == "POST"] == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_bad_aba_checksum_never_reaches_conduit(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(web, URL, encoded(recipient_form(**{"f.routingNumber": "021000029"})))

    assert response.status_code == 422
    assert "checksum failed" in response.text
    assert [c for c in calls if c[0] == "POST"] == []
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_swift_needs_an_iban_or_an_account_number(session):
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web,
            URL,
            encoded(
                {
                    "rail": "swift",
                    "f.bic": "CITIUS33XXX",
                    "f.relationship": "self",
                    "f.legalName": "ZZZTEST SWIFT Ltd",
                }
            ),
        )
    assert response.status_code == 422
    assert "needs an IBAN or an account number" in response.text
    assert [c for c in calls if c[0] == "POST"] == []


async def test_a_bad_iban_is_caught_by_mod_97(session):
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await post(
            web,
            URL,
            encoded(
                {
                    "rail": "sepa",
                    "f.iban": "DE89370400440532013001",
                    "f.relationship": "self",
                    "f.legalName": "ZZZTEST SEPA Ltd",
                }
            ),
        )
    assert response.status_code == 422 and "mod-97" in response.text


async def test_a_double_click_registers_one_recipient(session):
    calls: list = []
    app = make_app(
        stub(routes(extra={("POST", WHITELIST_PATH): httpx.Response(201, json=PENDING)}), calls)
    )
    body = encoded(recipient_form())
    async with signed_in(app) as web:
        await evidence(web)
        await post(web, URL, body)
        await post(web, URL, body)
    # The second submit resolved to the first (terminal) operation? No — the
    # first is `confirmed` and therefore terminal, so an intentional identical
    # resubmit is a new operation (OPERATIONS_SPEC §1). What must never happen is
    # two rows *in flight* at once, which the guard covers; here the assertion is
    # that both were ledgered and neither was sent outside the ledger.
    ops = (
        await session.execute(select(Operation).where(Operation.type == "whitelist_create"))
    ).scalars().all()
    assert len(ops) == 2
    assert len([c for c in calls if c[1] == WHITELIST_PATH and c[0] == "POST"]) == len(ops)


async def test_conduit_field_errors_land_on_their_fields(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("POST", WHITELIST_PATH): httpx.Response(
                        400,
                        json={
                            "type": "VALIDATION_ERROR",
                            "title": "Invalid recipient",
                            "correlationId": "corr_1",
                            "errors": [
                                {"pointer": "/routingNumber", "detail": "Unknown routing number."}
                            ],
                        },
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        await evidence(web)
        response = await post(web, URL, encoded(recipient_form()))
    assert response.status_code == 422
    assert "Unknown routing number." in response.text and "corr_1" in response.text


# --- revoke ----------------------------------------------------------------------------------


async def test_revoke_sends_a_delete_through_the_ledger(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("DELETE", f"{WHITELIST_PATH}/{REGISTERED['id']}"): httpx.Response(204),
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/{REGISTERED['id']}/revoke")

    assert "msg=Whitelisting+revoked" in response.headers["HX-Redirect"]
    assert [c[0] for c in calls if c[0] == "DELETE"] == ["DELETE"]
    op = (await session.execute(select(Operation).where(Operation.type == "whitelist_revoke"))).scalar_one()
    assert op.state == "confirmed" and op.conduit_resource_id == REGISTERED["id"]


async def test_a_refused_revoke_shows_conduits_own_title(session):
    app = make_app(
        stub(
            routes(
                extra={
                    ("DELETE", f"{WHITELIST_PATH}/{REGISTERED['id']}"): httpx.Response(
                        404, json={"type": "NOT_FOUND", "title": "No such recipient"}
                    )
                }
            )
        )
    )
    async with signed_in(app) as web:
        response = await post(web, f"{URL}/{REGISTERED['id']}/revoke")
    # A3 gate M1: the revoke banner reads the stored refusal through
    # `problem_of`, so it carries this console's words and not Conduit's title.
    banner = unquote_plus(response.headers["HX-Redirect"])
    assert "Conduit has no record of this" in banner
    assert "No such recipient" not in banner


async def test_a_spent_revoke_nonce_replayed_at_another_contact_is_refused(session):
    """`by_intent` is scoped to the operation type, so the nonce spent
    revoking contact A answered a revoke of contact B — and the route flashed
    "Whitelisting revoked" for a contact that stays registered and payable."""
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    # Both stubbed to succeed: the only thing that may differ
                    # between the two submits is whether the call is made at all.
                    ("DELETE", f"{WHITELIST_PATH}/{REGISTERED['id']}"): httpx.Response(204),
                    ("DELETE", f"{WHITELIST_PATH}/{PENDING['id']}"): httpx.Response(204),
                }
            ),
            calls,
        )
    )
    nonce = minted_intent()
    async with signed_in(app) as web:
        first = await post(
            web, f"{URL}/{REGISTERED['id']}/revoke", encoded({"intent": nonce})
        )
        replay = await post(web, f"{URL}/{PENDING['id']}/revoke", encoded({"intent": nonce}))

    assert "msg=Whitelisting+revoked" in first.headers["HX-Redirect"]
    # B's registration was never deleted and never got an operation of its own —
    # both true before the guard existed too. Only the sentence was wrong.
    assert [c for c in calls if c[1] == f"{WHITELIST_PATH}/{PENDING['id']}"] == []
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(
                Operation.request_path == f"{WHITELIST_PATH}/{PENDING['id']}"
            )
        )
    ) == 0
    landing = unquote_plus(replay.headers.get("HX-Redirect") or replay.headers["location"])
    assert "Whitelisting revoked" not in landing
    assert "had already been used" in landing


# --- the group-entity shortcut ----------------------------------------------------------------


async def test_the_shortcut_prefills_from_the_target_accounts_own_instructions():
    app = make_app(
        stub(routes(extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT)}))
    )
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={OTHER_CID}&account={USD_ACCOUNT['id']}")

    assert response.status_code == 200
    # us_domestic → the `us` DTO, with the coordinates a payer would be handed —
    # **stated masked, not rendered as inputs** (the submit re-reads the
    # account, so these were printed to prefill fields whose contents are
    # discarded). The routing number identifies a bank and is public.
    assert 'value="9876543210"' not in response.text
    assert "••••3210" in response.text and "101019644" in response.text
    # The legal name is server-owned too (it is in `COORDINATE_KEYS`), so it is
    # stated rather than typed — in full: a name is not a coordinate to mask.
    assert "ZZZTEST Console E2E EOOD" in response.text
    assert 'value="ZZZTEST Console E2E EOOD"' not in response.text
    # Another customer's account is by definition not this customer's own.
    assert '<option value="group_entity" selected>' in response.text


async def test_the_shortcut_prefills_sepa_from_a_eur_account():
    eur_id = EUR_ACTIVE["id"]
    path = f"/v2/customers/{OTHER_CID}/virtual-accounts/{eur_id}"
    app = make_app(stub(routes(extra={("GET", path): httpx.Response(200, json=EUR_ACTIVE)})))
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={OTHER_CID}&account={eur_id}")
    # Masked for the same reason the `us` shortcut's account number is.
    assert 'value="DE89370400440532013000"' not in response.text
    assert "••••3000" in response.text


async def test_an_account_that_is_not_active_is_refused_as_a_target():
    """Only an active account publishes the coordinates a payer is
    handed, so a pending one is not a destination to register."""
    eur_id = EUR_ACCOUNT["id"]  # pending_activation
    path = f"/v2/customers/{OTHER_CID}/virtual-accounts/{eur_id}"
    app = make_app(stub(routes(extra={("GET", path): httpx.Response(200, json=EUR_ACCOUNT)})))
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={OTHER_CID}&account={eur_id}")
    assert "That account is not active" in response.text
    assert "pending_activation" in response.text
    assert 'value="DE89370400440532013000"' not in response.text


async def test_the_customers_own_account_is_not_a_group_entity():
    path = f"/v2/customers/{CID}/virtual-accounts/{USD_ACCOUNT['id']}"
    app = make_app(stub(routes(extra={("GET", path): httpx.Response(200, json=USD_ACCOUNT)})))
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={CID}&account={USD_ACCOUNT['id']}")
    assert "own account" in response.text and "group entity" in response.text
    assert 'value="9876543210"' not in response.text


async def test_the_shortcut_registers_the_target_accounts_own_coordinates(session):
    """The prefill was render-only, so a browser could keep the
    `group_entity` framing and register a different account number. The submit
    re-reads the target and overwrites what was typed."""
    calls: list = []
    app = make_app(
        stub(
            routes(extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT)}),
            calls,
        )
    )
    async with signed_in(app) as web:
        await evidence(web)
        await post(
            web,
            URL,
            encoded(
                recipient_form(
                    target=OTHER_CID,
                    account=USD_ACCOUNT["id"],
                    # what the browser claims instead
                    **{
                        "f.accountNumber": "000000000000",
                        "f.routingNumber": "021000021",
                        "f.relationship": "self",
                    },
                )
            ),
        )
    sent = json.loads(next(c[2] for c in calls if c[1] == WHITELIST_PATH and c[0] == "POST"))
    assert sent["accountNumber"] == "9876543210"  # the target account's own
    assert sent["routingNumber"] == "101019644"
    assert sent["relationship"] == "group_entity"


async def test_a_forged_coordinate_cannot_survive_in_a_slot_the_target_does_not_publish(session):
    """Re-reading the target and *overlaying* its coordinates only replaced the keys
    that read publishes. A SWIFT block is required to publish a `bic` and may
    publish neither `accountNumber` nor `iban`
    (`payments.prefill_from_account`), while the SWIFT whitelist DTO has fields
    for all three — so a forged account number typed into the prefilled form sat
    in a slot the fresh read never wrote to, and went to Conduit beside the real
    coordinates as though the target account had published it. The fix strips
    every server-owned key before the overlay: they come from the read or from
    nowhere.
    """
    swift_only = {
        **USD_ACCOUNT,
        "depositInstructions": [
            {
                "type": "swift",
                "beneficiaryName": "ZZZTEST Console E2E EOOD",
                "bank": {"bic": "COBADEFFXXX"},
                "iban": "DE89370400440532013000",
                # …and deliberately no `accountNumber`: this is the gap.
            }
        ],
    }
    calls: list = []
    app = make_app(
        stub(routes(extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=swift_only)}), calls)
    )
    async with signed_in(app) as web:
        await evidence(web)
        await post(
            web,
            URL,
            encoded(
                recipient_form(
                    target=OTHER_CID,
                    account=USD_ACCOUNT["id"],
                    **{
                        "f.accountNumber": "000000000000",  # the forgery
                        "f.legalName": "ZZZTEST Not This Company Ltd",
                        "f.relationship": "self",
                    },
                )
            ),
        )

    sent = json.loads(next(c[2] for c in calls if c[1] == WHITELIST_PATH and c[0] == "POST"))
    assert "000000000000" not in json.dumps(sent), "a forged account number reached Conduit"
    assert sent.get("accountNumber") in (None, "")
    assert sent["bic"] == "COBADEFFXXX"
    assert sent["iban"] == "DE89370400440532013000"
    # The name and the relationship are the target's and the shortcut's too.
    assert sent["legalName"] == "ZZZTEST Console E2E EOOD"
    assert sent["relationship"] == "group_entity"


async def test_a_shortcut_naming_an_unreadable_account_registers_nothing():
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", TARGET_ACCOUNT): httpx.Response(
                        404, json={"type": "NOT_FOUND", "title": "No such account"}
                    )
                }
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web,
            URL,
            encoded(recipient_form(target=OTHER_CID, account=USD_ACCOUNT["id"])),
        )
    assert response.status_code == 422
    assert "Conduit has no record of this" in response.text  # A3: NOT_FOUND, our words
    assert [c for c in calls if c[0] == "POST" and c[1] == WHITELIST_PATH] == []


async def test_a_shortcut_pointed_at_the_customer_itself_registers_nothing():
    calls: list = []
    own = f"/v2/customers/{CID}/virtual-accounts/{USD_ACCOUNT['id']}"
    app = make_app(
        stub(routes(extra={("GET", own): httpx.Response(200, json=USD_ACCOUNT)}), calls)
    )
    async with signed_in(app) as web:
        response = await post(
            web, URL, encoded(recipient_form(target=CID, account=USD_ACCOUNT["id"]))
        )
    assert response.status_code == 422 and "own account" in response.text
    assert [c for c in calls if c[0] == "POST" and c[1] == WHITELIST_PATH] == []


async def test_an_account_with_nothing_registrable_prefills_nothing():
    bare = {**USD_ACCOUNT, "depositInstructions": [{"type": "carrier_pigeon", "currency": "USD"}]}
    path = f"/v2/customers/{OTHER_CID}/virtual-accounts/{bare['id']}"
    app = make_app(stub(routes(extra={("GET", path): httpx.Response(200, json=bare)})))
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={OTHER_CID}&account={bare['id']}")
    assert "Nothing to prefill" in response.text
    assert 'name="f.accountNumber" value="9876543210"' not in response.text


# --- sandbox review ---------------------------------------------------------------------------

SIMULATE = f"/v2/sandbox/whitelist-recipients/{PENDING['id']}/simulate-approve"


async def test_the_sandbox_review_buttons_are_hidden_off_sandbox():
    app = make_app(stub(routes()))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await web.get(LIST)
    assert "Simulate approve" not in response.text


async def test_simulate_is_refused_off_sandbox_even_when_posted():
    calls: list = []
    app = make_app(stub(routes(), calls))
    with settings_override(conduit_env="staging"):
        async with signed_in(app) as web:
            response = await post(
                web, f"{URL}/{PENDING['id']}/simulate", encoded({"outcome": "approve"})
            )
    assert "sandbox-only" in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if "sandbox" in c[1]] == []


async def test_simulate_sends_an_empty_body_and_audits_it(session):
    calls: list = []
    app = make_app(
        stub(
            routes(
                extra={("POST", SIMULATE): httpx.Response(200, json={**PENDING, "status": "registered"})}
            ),
            calls,
        )
    )
    async with signed_in(app) as web:
        response = await post(
            web, f"{URL}/{PENDING['id']}/simulate", encoded({"outcome": "approve"})
        )

    assert "msg=Simulated+approve" in response.headers["HX-Redirect"]
    assert json.loads(next(c for c in calls if c[1] == SIMULATE)[2]) == {}
    audit = (await session.execute(select(AuditEvent))).scalar_one()
    assert audit.action == "sandbox.simulate_whitelist" and audit.detail["ok"] is True
    # Sandbox scaffolding is outside the ledger.
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_an_unknown_simulated_outcome_is_refused():
    calls: list = []
    app = make_app(stub(routes(), calls))
    async with signed_in(app) as web:
        response = await post(
            web, f"{URL}/{PENDING['id']}/simulate", encoded({"outcome": "obliterate"})
        )
    assert "Unknown simulated outcome" in unquote_plus(response.headers["HX-Redirect"])
    assert [c for c in calls if "sandbox" in c[1]] == []


# --- roles and CSRF ----------------------------------------------------------------------------


async def test_a_viewer_reads_the_list_but_is_offered_no_form():
    app = make_app(stub(routes()))
    async with signed_in(app, groups="readers") as web:
        response = await web.get(LIST)
        form_page = await web.get(URL)
        refused = await post(web, URL, encoded(recipient_form()))
        revoke = await post(web, f"{URL}/{REGISTERED['id']}/revoke")
    assert response.status_code == 200 and "ZZZTEST Globex Supplies LLC" in response.text
    # The registration page itself names the one permission it is gated on.
    assert form_page.status_code == 200
    assert (
        "Read-only: registering a destination needs the "
        "<code>whitelist.register</code> permission." in form_page.text
    )
    # `LIST` is the contacts page, which names the permission for each action
    # it withholds — registering among them.
    assert "<code>whitelist.register</code>" in response.text
    assert 'id="wizard"' not in response.text
    assert refused.status_code == 403 and revoke.status_code == 403


async def test_the_mutations_need_the_csrf_header():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        for url in (URL, f"{URL}/{REGISTERED['id']}/revoke", f"{URL}/{PENDING['id']}/simulate"):
            response = await web.post(
                url,
                content=encoded(recipient_form()),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 403 and "CSRF" in response.text


async def test_the_form_carries_hx_sync_against_a_double_click():
    app = make_app(stub(routes()))
    async with signed_in(app) as web:
        response = await web.get(URL)
    assert 'hx-sync="this:drop"' in response.text
    assert 'hx-disabled-elt="find button[type=submit]"' in response.text


# --- the shortcut explains itself ---------------------------------


async def test_the_shortcut_states_whose_account_goes_onto_whose_whitelist():
    """The form is otherwise a page of bank fields with no statement of whose
    they are, on the one screen where two customers are involved."""
    app = make_app(
        stub(routes(extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT)}))
    )
    async with signed_in(app) as web:
        response = await web.get(f"{URL}?target={OTHER_CID}&account={USD_ACCOUNT['id']}")

    assert "Registering another customer's account onto this customer's whitelist" in response.text
    assert USD_ACCOUNT["id"] in response.text and OTHER_CID in response.text
    assert CID in response.text
    # Why it is a group entity, and what Conduit does next.
    assert "different customer of this organization" in response.text
    assert "<code>pending_review</code>" in response.text
    assert "<code>registered</code>" in response.text


async def test_the_sandbox_approve_note_is_sandbox_only():
    routes_with_target = routes(
        extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT)}
    )
    url = f"{URL}?target={OTHER_CID}&account={USD_ACCOUNT['id']}"
    app = make_app(stub(routes_with_target))
    async with signed_in(app) as web:
        sandbox = await web.get(url)
    assert "simulate that decision" in sandbox.text

    with settings_override(conduit_env="staging"):
        app = make_app(stub(routes_with_target))
        async with signed_in(app) as web:
            live = await web.get(url)
    assert "simulate that decision" not in live.text


# --- what the submission produced ----------------------------------------------------------


async def test_a_pending_registration_says_what_happens_next():
    app = make_app(stub(routes([PENDING])))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?registered={PENDING['id']}")

    assert "Conduit is reviewing it" in response.text
    assert "cannot be paid yet" in response.text
    assert "nothing further is owed from you" in response.text


async def test_a_registered_entry_says_go_and_pay_it_and_links_to_the_right_flow():
    """SUPERSEDED `test_a_registered_entry_says_go_and_transfer_and_links_to_it`,
    which pointed at `transfers/new?whitelistRecipientId=`. A registration gates
    an intercompany payout to a **bank** account, and that flow
    lives on the payout page — the transfers screen moves money between Conduit
    accounts and gates on nothing. The rule is unchanged and is what is asserted:
    a just-registered destination hands the operator the flow it unlocked, with
    the entry already picked."""
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?registered={REGISTERED['id']}")

    assert "you can pay it now" in response.text
    assert (
        f'/customers/{CID}/payouts/new?purpose=intercompany&amp;'
        f'whitelistRecipientId={REGISTERED["id"]}' in response.text
    )
    assert "Conduit is reviewing it" not in response.text


async def test_the_guidance_reads_the_status_conduit_just_answered_with():
    """Not what the submit assumed: a sandbox approval between the redirect and
    this render would make "we are waiting on a review" a lie."""
    app = make_app(stub(routes([{**REGISTERED, "id": PENDING["id"], "status": "suspended"}])))
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?registered={PENDING['id']}")
    assert "its status is suspended" in response.text
    assert "Conduit is reviewing it" not in response.text


# --- naming the target of a group-entity entry ---------------------------------------------
#
# Derived from this console's own ledger — the operation that registered the
# entry, plus the audit row the shortcut wrote — never from a new table and never
# from crawling every customer's accounts to match coordinates.


async def test_a_shortcut_registration_records_its_target_and_the_row_names_it(session):
    listed = {**PENDING, "id": PENDING["id"], "relationship": "group_entity"}
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): page([listed]),
                ("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT),
                ("POST", WHITELIST_PATH): httpx.Response(201, json=PENDING),
                ("POST", "/v2/documents"): documents_stub,
            }
        )
    )
    async with signed_in(app) as web:
        await evidence(web)
        await post(
            web,
            URL,
            encoded(recipient_form(target=OTHER_CID, account=USD_ACCOUNT["id"])),
        )
        response = await web.get(LIST)

    event = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "whitelist.group_entity")
        )
    ).scalar_one()
    op = (
        await session.execute(select(Operation).where(Operation.type == "whitelist_create"))
    ).scalar_one()
    assert event.operation_id == op.id
    assert event.detail == {"target_customer": OTHER_CID, "target_account": USD_ACCOUNT["id"]}
    # …and the row now names the customer whose account it is.
    assert f'<a href="/customers/{OTHER_CID}"' in response.text


async def test_an_entry_this_console_did_not_register_names_no_target():
    """No guess: an entry registered by hand, elsewhere, or before this feature
    is rendered exactly as it always was."""
    app = make_app(stub(routes([REGISTERED])))
    async with signed_in(app) as web:
        response = await web.get(LIST)
    assert REGISTERED["relationship"] == "group_entity"
    assert "account of" not in response.text


async def test_an_entry_the_page_cannot_see_is_not_called_pending(session):
    """The list read failed, so its status is unknown — and unknown is not
    `pending_review`. The console does not state a status it was not told."""
    app = make_app(
        stub(
            {
                ("GET", WHITELIST_PATH): httpx.Response(
                    500, json={"type": "SERVER_ERROR", "title": "Upstream failure"}
                )
            }
        )
    )
    async with signed_in(app) as web:
        response = await web.get(f"{LIST}?registered={PENDING['id']}")
    assert "its status is not on this page yet" in response.text
    assert "Conduit is reviewing it" not in response.text


async def test_a_refused_shortcut_submit_still_states_the_coordinates_masked():
    """The seal is a property of the SURFACE, not of the GET.

    `test_the_shortcut_prefills_…` pins the rendered form; this pins the same
    page re-rendered at 422, which is the likelier one to be seen — a validation
    error is how an operator meets this form a second time. The POST overlays the
    freshly re-read coordinates onto `values` before validating, so a re-render
    that forgets `sealed` prints the account number the submit itself discards
    (cross-cutting PII sweep, 2026-09-01).
    """
    app = make_app(
        stub(routes(extra={("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT)}))
    )
    async with signed_in(app) as web:
        # No document: `whitelist_errors` refuses, so the page comes back at 422
        # with the overlay already applied.
        refused = await post(
            web,
            URL,
            encoded(
                recipient_form(
                    target=OTHER_CID, account=USD_ACCOUNT["id"], documentIds="", f_rail="us"
                )
            ),
        )

    assert refused.status_code == 422
    assert 'value="9876543210"' not in refused.text, "the 422 re-render printed the account number"
    assert "9876543210" not in refused.text, "the account number reached the page at all"
    # Still identifiable, exactly as the GET states it.
    assert "••••3210" in refused.text and "101019644" in refused.text


async def test_a_conduit_refusal_of_a_shortcut_also_states_them_masked():
    """The second re-render on the same handler — Conduit's own rejection rather
    than local validation. Both paths carry the post-overlay `values`, so both
    need the seal; pinning only the one that happened to be found leaves the
    other free to regress.
    """
    app = make_app(
        stub(
            routes(
                extra={
                    ("GET", TARGET_ACCOUNT): httpx.Response(200, json=USD_ACCOUNT),
                    ("POST", WHITELIST_PATH): httpx.Response(
                        400,
                        json={
                            "type": "VALIDATION_ERROR",
                            "title": "Invalid recipient",
                            "correlationId": "corr_seal",
                            "errors": [
                                {"pointer": "/legalName", "detail": "Name does not match."}
                            ],
                        },
                    ),
                }
            )
        )
    )
    async with signed_in(app) as web:
        await evidence(web)
        refused = await post(
            web,
            URL,
            encoded(
                recipient_form(
                    target=OTHER_CID, account=USD_ACCOUNT["id"], documentIds="doc_evidence_1"
                )
            ),
        )

    assert refused.status_code == 422
    assert "Name does not match." in refused.text  # the refusal still lands
    assert "9876543210" not in refused.text, "Conduit's refusal printed the account number"
    assert "••••3210" in refused.text
