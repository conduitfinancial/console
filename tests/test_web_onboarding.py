"""The onboarding wizard end to end, through the real app (plan v2 §7).

Conduit is stubbed at the HTTP layer; everything else — auth middleware, CSRF,
the form engine, the drafts table, the operations ledger, the Jinja templates —
is the real thing.
"""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import select

from app import documents, forms, operations
from app.auth.providers import ProxyProvider
from app.config import Settings
from app.models import DocumentBlob, Draft, Operation
from app.onboarding import drafts
from app.permissions import VIEW
from app.web.onboarding import PERSON_PURPOSE, PURPOSE
from tests.web_harness import (
    PROXY_SECRET,
    documents_stub,
    forbidden_affordances,
    form,
    hx_headers,
    make_app,
    minted_intent,
    post,
    signed_in,
    signed_in_as,
    stub,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUIREMENTS = json.loads((FIXTURES / "onboarding_requirements_BGR.json").read_text())
INDUSTRY = json.loads((FIXTURES / "policy_subjects_industry.json").read_text())
ACTIVITY = json.loads((FIXTURES / "policy_subjects_regulated_activity.json").read_text())
VALIDATION_422 = json.loads((FIXTURES / "validation_error_422.json").read_text())

# Every ledger row this file's submit tests are about. Scoped to the type since
# A filled draft really uploads its three attachments — so the
# ledger holds four operations per submission and `select(Operation)` alone
# stopped meaning "the submission".
SUBMISSIONS = select(Operation).where(Operation.type == "onboarding_submit")

APPLICATION = {
    "id": "app_1",
    "type": "customer_onboarding",
    "status": "processing",
    "createdAt": "2026-08-28T05:00:00.000Z",
    "updatedAt": "2026-08-28T05:00:00.000Z",
}


def discovery(extra: dict | None = None) -> dict:
    return {
        ("GET", "/v2/onboarding/requirements"): httpx.Response(200, json=REQUIREMENTS),
        ("GET", "/v2/onboarding/policy-subjects"): lambda r: httpx.Response(
            200, json=INDUSTRY if r.url.params.get("axis") == "INDUSTRY" else ACTIVITY
        ),
        # A submitted `doc_` id has to be in this console's own upload
        # ledger, against this draft — so a filled draft uploads for real, and
        # every stub needs the upload route. `documents_stub` mints the id from
        # the filename, which is what lets `complete_body`'s ids be named ahead
        # of the upload that produces them.
        ("POST", "/v2/documents"): documents_stub,
        **(extra or {}),
    }


async def new_draft(web) -> str:
    response = await post(web, "/onboarding", form(country="bgr"))
    assert response.status_code == 204, response.text
    return response.headers["HX-Redirect"]


async def upload_to(web, draft_url: str, doc_id: str, *, person: int | None = None) -> str:
    """One file through the real upload route, tied to this draft.

    A test that means to attach `doc_x` uploads a file called `doc_x.png`:
    `documents_stub` answers with the filename's stem, so the id is chosen by
    the test rather than by upload ordering.
    """
    purpose = PERSON_PURPOSE if person is not None else PURPOSE
    query = f"purpose={purpose}&filename={doc_id}.png&draft={draft_url.rsplit('/', 1)[-1]}"
    if person is not None:
        query += f"&person={person}"
    chip = await web.post(
        f"/documents?{query}",
        content=PNG,
        headers={
            "content-type": "application/octet-stream",
            "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
        },
    )
    assert doc_id in chip.text, chip.text
    return doc_id


# --- the happy path ------------------------------------------------------------------


async def test_country_to_draft_to_render(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        page = await web.get(url)

    assert page.status_code == 200
    html = page.text
    # Rendered from the pinned snapshot: every group, both person rows, the
    # document checklist.
    for title in ("Business information", "Company classification", "Certification"):
        assert title in html
    assert html.count('class="card person-card"') == 2
    assert 'name="p.0.role"' in html and 'name="p.1.role"' in html
    assert "Удостоверение за актуално състояние" in html  # document titles, verbatim
    # Conditions travel to the browser as data, evaluated by static/conditions.js.
    assert "data-conditions=" in html
    # …and the policy catalogs are in the pinned snapshot, not fetched at render.
    assert "Accounting and Auditing" in html

    stored = await session.get(Draft, uuid.UUID(url.rsplit("/", 1)[-1]))
    assert stored.country == "BGR"
    assert stored.requirements_snapshot["schemaVersion"] == "3"
    assert len(stored.payload["persons"]) == 2  # seeded to each row's minCount


async def test_unlabelled_enum_options_gain_a_name_without_changing_what_they_submit(session):
    """Discovery ships the 248 country options with no labels
    at all, so the operator was picking between `BGR` and `BFA` by eye. The
    option's *text* gains a name; its `value` — what the server, the condition
    engine and Conduit's own errors compare — is byte-identical."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        page = await web.get(await new_draft(web))
    html = page.text

    assert 'value="CAN" >CAN — Canada</option>' in html
    assert 'value="BGR" >BGR — Bulgaria</option>' in html
    # A SNAKE_CASE constant nobody labelled reads as words, raw value kept.
    assert 'value="NATIONAL_ID" >NATIONAL_ID — National id</option>' in html
    # Same treatment in the checkbox groups (`roles`, `countriesOfActivity`).
    assert 'value="BENEFICIAL_OWNER"\n        > BENEFICIAL_OWNER — Beneficial owner</label>' in html
    # A label discovery *did* ship is never second-guessed.
    assert ">Accounting and Auditing</option>" in html
    assert "accounting_and_auditing — " not in html


async def test_the_start_page_suggests_country_codes_without_constraining_them(session):
    """The datalist is an affordance, not a gate: the input stays free text and
    discovery remains the only authority on which codes are servable."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        page = await web.get("/onboarding")
    html = page.text

    assert '<datalist id="iso-countries">' in html
    assert '<option value="BGR">Bulgaria</option>' in html
    assert 'list="iso-countries"' in html
    # Still a plain text input — no `enum`, no select, nothing that could refuse
    # a code this table has never heard of.
    assert 'type="text" id="country" name="country" required' in html


async def test_the_snapshot_is_pinned_not_refetched(session):
    """The wizard renders one draft against one questionnaire for its whole life
    (plan v2 §3). Discovery moving on must not change what is on screen."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        # Conduit now answers something else entirely — including for the
        # catalogs the draft folded in.
        app.state.conduit = make_app(
            stub({("GET", "/v2/onboarding/requirements"): httpx.Response(500, json={})})
        ).state.conduit
        page = await web.get(url)
    assert page.status_code == 200
    assert "Business information" in page.text


async def test_section_save_stores_answers_and_survives_a_reload(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        draft_id = url.rsplit("/", 1)[-1]
        saved = await post(
            web,
            f"{url}/save",
            form(
                **{
                    "f.businessInfo.legalName": "ZZZTEST Acme OOD",
                    "f.registeredAddress.country": "BGR",
                    "p.0.role": "any",
                    "p.0.f.firstName": "Ada",
                }
            ),
        )
        assert saved.status_code == 200
        assert "Draft saved" in saved.text
        page = await web.get(url)

    assert 'value="ZZZTEST Acme OOD"' in page.text
    assert 'value="Ada"' in page.text
    stored = await session.get(Draft, uuid.UUID(draft_id))
    assert stored.payload["root"]["businessInfo"]["legalName"] == "ZZZTEST Acme OOD"
    assert stored.payload["persons"][0]["values"]["firstName"] == "Ada"


async def test_person_cards_respect_min_and_max(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        cards = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER"})
        added = await post(web, f"{url}/persons", cards + b"&new_role=BENEFICIAL_OWNER")
        assert added.status_code == 200
        assert added.text.count('class="card person-card"') == 3
        # minCount is 1 per row, so the extra beneficial owner can go…
        assert added.text.count("/persons/") >= 1
        three = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER",
                        "p.2.role": "BENEFICIAL_OWNER"})
        removed = await post(web, f"{url}/persons/2/remove", three)
        assert removed.text.count('class="card person-card"') == 2
        # …but the last one of a row cannot: no remove control is offered.
        again = await post(web, f"{url}/persons/1/remove", cards)
        assert again.text.count('class="card person-card"') == 2

    stored = (await session.execute(select(Draft))).scalars().first()
    assert [p["role"] for p in stored.payload["persons"]] == ["any", "BENEFICIAL_OWNER"]


async def test_a_replayed_removal_does_not_shift_the_demands_twice(session):
    """The renumber is a relative shift; the payload write beside it is absolute.

    Split across two commits they raced — a double-fired Remove read the same
    stored indices twice and shifted twice, landing a demand on a card Conduit
    never named. Folded into one locked write, the shift is applied only when the
    stored payload really did lose a card, so the replay moves nothing.
    """
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        draft_id = url.rsplit("/", 1)[-1]
        cards = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER"})
        await post(web, f"{url}/persons", cards + b"&new_role=BENEFICIAL_OWNER")
        three = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER",
                        "p.2.role": "BENEFICIAL_OWNER"})

        draft = await session.get(Draft, uuid.UUID(draft_id))
        draft.requirements_snapshot = {
            **(draft.requirements_snapshot or {}),
            forms.LEARNED_KEY: [{
                "pointer": "/residencyPermitExpiryDate",
                "label": "Residency permit expiry date",
                "type": "date",
                "required": False,
                "group": forms.LEARNED_GROUP,
                forms.LEARNED_SCOPE: "person",
                forms.LEARNED_INDICES: [2],
            }],
        }
        await session.commit()

        await post(web, f"{url}/persons/0/remove", three)
        # The same POST again, with the same stale body the first one carried.
        await post(web, f"{url}/persons/0/remove", three)

    await session.refresh(draft)
    learned = draft.requirements_snapshot[forms.LEARNED_KEY]
    # One card left the list, so the demand moved once — to 1, not to 0.
    assert [d[forms.LEARNED_INDICES] for d in learned] == [[1]]
    assert len(draft.payload["persons"]) == 2


async def test_removing_a_card_renumbers_the_learned_demands_above_it(session):
    """The wiring for `forms.forget_person_index`: the route has to call it.

    The demand is stored as a position in `ownership.persons[]`. Remove a card
    below it and, untouched, it keeps pointing at whoever slid into the slot —
    so the wrong person is asked for a field Conduit never demanded of them and
    the person who owes it resubmits into the same 422.
    """
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        draft_id = url.rsplit("/", 1)[-1]
        cards = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER"})
        await post(web, f"{url}/persons", cards + b"&new_role=BENEFICIAL_OWNER")
        three = form(**{"p.0.role": "any", "p.1.role": "BENEFICIAL_OWNER",
                        "p.2.role": "BENEFICIAL_OWNER"})

        draft = await session.get(Draft, uuid.UUID(draft_id))
        draft.requirements_snapshot = {
            **(draft.requirements_snapshot or {}),
            forms.LEARNED_KEY: [
                {
                    "pointer": "/residencyPermitExpiryDate",
                    "label": "Residency permit expiry date",
                    "type": "date",
                    "required": False,
                    "group": forms.LEARNED_GROUP,
                    forms.LEARNED_SCOPE: "person",
                    forms.LEARNED_INDICES: [2],
                }
            ],
        }
        await session.commit()

        await post(web, f"{url}/persons/0/remove", three)

    await session.refresh(draft)
    learned = draft.requirements_snapshot[forms.LEARNED_KEY]
    assert [d[forms.LEARNED_INDICES] for d in learned] == [[1]]


# --- documents -----------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"0" * 64


async def test_upload_produces_a_chip_and_an_operation(session):
    app = make_app(
        stub(discovery({("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_9"})}))
    )
    async with signed_in(app) as web:
        url = await new_draft(web)
        draft_id = url.rsplit("/", 1)[-1]
        chip = await web.post(
            f"/documents?purpose=organization_onboarding&filename=x.png&draft={draft_id}",
            content=PNG,
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )

    assert chip.status_code == 200
    assert 'name="documentIds"' in chip.text and "doc_9" in chip.text
    op = (await session.execute(select(Operation))).scalars().one()
    assert (op.type, op.state, op.conduit_resource_id) == ("document_upload", "confirmed", "doc_9")


async def test_upload_rejects_a_file_that_is_not_what_it_claims(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        await new_draft(web)
        chip = await web.post(
            "/documents?purpose=organization_onboarding&filename=payload.pdf",
            content=b"<html>not a pdf at all</html>",
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )
    assert "only pdf, jpeg and png" in chip.text.lower()
    assert (await session.execute(select(Operation))).scalars().all() == []


async def test_a_spent_upload_nonce_replayed_with_another_file_is_refused(session):
    """`by_intent` is scoped to the operation type, so the nonce spent on
    the first upload resolved the second one onto it — and because
    `ON CONFLICT DO NOTHING` drops the second blob insert, the chip showed the
    FIRST file's `doc_` id beside the SECOND file's name. An operator attaches
    that id believing it is the file they just picked."""
    second = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"1" * 64
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_first"})}),
            calls,
        )
    )
    nonce = minted_intent()
    async with signed_in(app) as web:
        url = await new_draft(web)
        draft_id = url.rsplit("/", 1)[-1]

        async def send(filename: str, content: bytes):
            return await web.post(
                f"/documents?purpose=organization_onboarding&filename={filename}"
                f"&draft={draft_id}&intent={nonce}",
                content=content,
                headers={
                    "content-type": "application/octet-stream",
                    "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
                },
            )

        first = await send("first.png", PNG)
        replay = await send("second.png", second)

    assert "doc_first" in first.text
    # The second file's bytes never reached Conduit and never reached the blob
    # table — both true before the guard existed too. What was wrong was the
    # chip, which named a stored document for an unstored file.
    assert len([c for c in calls if c[1] == "/v2/documents"]) == 1
    blobs = (await session.execute(select(DocumentBlob))).scalars().all()
    assert [b.filename for b in blobs] == ["first.png"]
    assert "doc_" not in replay.text
    assert "already been used" in replay.text


# --- review and submit ---------------------------------------------------------------


def complete_body(model: forms.FormModel) -> bytes:
    """A fully answered questionnaire, as the browser would post it.

    Reuses `test_forms.full_submission` — the same generator the engine's own
    assembly test is built on, so the wizard is exercised with a body already
    proven to satisfy `forms.validate`.
    """
    from tests.test_forms import full_submission

    return str(httpx.QueryParams(full_submission(model, persons=2))).encode()


def swap(model: forms.FormModel, field: str, value: str) -> bytes:
    """`complete_body`, with one answer replaced — an editor changing their mind
    (or somebody else's answers) between the review and the submit."""
    from tests.test_forms import full_submission

    items = [(name, value if name == field else sent) for name, sent in full_submission(model, persons=2)]
    return str(httpx.QueryParams(items)).encode()


async def filled_draft(web, model) -> str:
    """A draft answered in full, with `complete_body`'s three attachments really
    uploaded against it.

    They have to be real uploads: the submit resolves every `doc_` id
    on the assembled body against this console's upload ledger, scoped to this
    draft, so a hand-written id is now a test of the refusal rather than of
    whatever the test meant to exercise.
    """
    url = await new_draft(web)
    await upload_to(web, url, "doc_1")
    await upload_to(web, url, "doc_p0", person=0)
    await upload_to(web, url, "doc_p1", person=1)
    saved = await post(web, f"{url}/save", complete_body(model))
    assert saved.status_code == 200
    return url


def reviewed_seal(html: str) -> str:
    """The seal the review render put on its submit form.

    The counterpart of `web_harness.intent`, and for the same reason: since this
    guard exists, a submit body assembled by hand is a form no browser ever
    posted, and would be testing the guard rather than whatever the test meant.
    """
    marker = 'name="reviewed" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


async def read_the_review(web, url: str) -> bytes:
    """Render the review page and return the body its button would post back."""
    page = await web.get(f"{url}/review")
    assert page.status_code == 200, page.text
    return form(reviewed=reviewed_seal(page.text))


async def test_review_then_submit_redirects_to_the_application(session):
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        review = await post(web, f"{url}/review", complete_body(model))
        assert review.headers["HX-Redirect"].endswith("/review")
        page = await web.get(f"{url}/review")
        assert "Submit to Conduit" in page.text
        assert "Sample value" in page.text  # the assembled body is shown verbatim

        # Exactly what the button posts: the seal over what was just read.
        done = await post(web, f"{url}/submit", form(reviewed=reviewed_seal(page.text)))

    assert done.status_code == 204
    assert done.headers["HX-Redirect"] == "/applications/app_1"
    op = (await session.execute(select(Operation).where(Operation.type == "onboarding_submit"))).scalar_one()
    assert (op.state, op.conduit_resource_id) == ("confirmed", "app_1")
    # The ledger's rules, on the wire: one idempotency key, and the operation id
    # as the durable clientReferenceId.
    method, path, body = next(c for c in calls if c[1] == "/v2/onboarding")
    assert json.loads(body)["clientReferenceId"] == str(op.id)
    # The draft is stamped submitted but keeps its answers until the application
    # settles — a rejection is what re-opens them.
    draft = await drafts.load(session, op.draft_id)
    assert draft.submitted_at is not None and draft.payload is not None


async def test_an_incomplete_draft_cannot_submit(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        blocked = await post(web, f"{url}/submit", b"")
        assert blocked.headers["HX-Redirect"].endswith("/review")
        page = await web.get(f"{url}/review")

    assert "Not ready to submit" in page.text
    assert "This field is required." in page.text
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []


async def test_a_422_is_mapped_onto_the_form_it_came_from(session):
    """FORM_ENGINE_SPEC §7, through a real template render, driven by the
    response Conduit actually sent (`validation_error_422.json`)."""
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(422, json=VALIDATION_422)})
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        rejected = await post(web, f"{url}/submit", await read_the_review(web, url))
        assert "?operation=" in rejected.headers["HX-Redirect"]
        page = await web.get(rejected.headers["HX-Redirect"])

    assert page.status_code == 422
    html = page.text
    # The unmatched pointer is surfaced, not dropped (mapper contract) — and
    # exactly ONCE, under the form, because it names no field to sit under and
    # two copies of one refusal read as two refusals (A3).
    assert html.count("/individual:any: At least 1 any(s) required, got 0") == 1
    # …and the envelope the operator gives support.
    assert VALIDATION_422["correlationId"] in html
    # A3: the envelope's prose is this console's, and Conduit's own resolution
    # and detail are provably not on the page. The per-FIELD sentences below are
    # a different thing and do survive verbatim: they name the operator's own
    # values, and the mapper binds each to its field.
    assert "Conduit says this onboarding is still incomplete" in html
    assert VALIDATION_422["resolution"][:40] not in html
    assert VALIDATION_422["detail"] not in html
    op = (await session.execute(SUBMISSIONS)).scalars().one()
    assert op.state == "rejected"


async def test_a_field_level_422_lands_on_the_field(session):
    """The pointer-matching half of the mapper, in the same real render."""
    body = {
        **VALIDATION_422,
        "errors": [
            {
                "pointer": "/businessInfo/legalName",
                "detail": "Legal name is already registered",
                "category": "field",
            }
        ],
    }
    app = make_app(
        stub(discovery({("POST", "/v2/onboarding"): httpx.Response(422, json=body)}))
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        rejected = await post(web, f"{url}/submit", await read_the_review(web, url))
        page = await web.get(rejected.headers["HX-Redirect"])

    html = page.text
    legal_name = html.index('name="f.businessInfo.legalName"')
    assert "Legal name is already registered" in html[legal_name : legal_name + 600]


async def test_a_double_submit_resolves_to_the_same_operation(session):
    """OPERATIONS_SPEC §1 through the route: the second click never sends."""
    calls: list = []
    app = make_app(
        stub(
            discovery(
                {
                    ("POST", "/v2/onboarding"): httpx.Response(
                        500, json={"type": "SERVER_ERROR", "title": "boom"}
                    )
                }
            ),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        # One render, posted twice — which is what a double click is: the second
        # click carries the seal the first one did, and the draft has not moved,
        # so the seal guard is transparent to the double-submit guard behind it.
        clicked = await read_the_review(web, url)
        first = await post(web, f"{url}/submit", clicked)
        second = await post(web, f"{url}/submit", clicked)

    # 5xx is ambiguous: outcome_unknown, and the operator is sent to the status
    # page rather than offered a retry button.
    assert first.headers["HX-Redirect"].startswith("/operations/")
    assert second.headers["HX-Redirect"] == first.headers["HX-Redirect"]
    assert len([c for c in calls if c[1] == "/v2/onboarding"]) == 1
    op = (await session.execute(SUBMISSIONS)).scalars().one()
    assert op.state == "outcome_unknown"


# --- what the reviewer read ---------------------------------------------------


@asynccontextmanager
async def person(app, email: str, groups: str):
    """One named operator's browser: their own actor id, their own role."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://console.test",
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": email,
            "X-Auth-Request-Email": email,
            "X-Auth-Request-Groups": groups,
        },
    ) as web:
        landing = await web.get("/drafts")
        assert landing.status_code == 200, landing.text
        yield web


@asynccontextmanager
async def editor_and_reviewer(app):
    """Two people, two jobs — the split-role deployment.

    `signed_in_as` builds one partial role; this builds the *pair*, because the
    finding needs the edit and the submit to be held by different actor ids.

    The editor is a `ROLES_FILE` role that may fill drafts in and nothing else.
    The reviewer is the built-in **admin**, not a second custom role: reading
    somebody else's draft needs `onboarding.access_any`, and `permissions.parse`
    refuses an admin-class permission granted without the whole operator base
    (so a hand-cut "reviewer" role holding only `submit` + `access_any` is not a
    thing a deployment can define). That the reviewer *could* also edit is beside
    the point — what matters is that the person who read the page and the
    person who changed it underneath them are different actor ids, and the
    operation is attributed to the reader.
    """
    path = Path(tempfile.mkdtemp()) / "roles.json"
    path.write_text(json.dumps({"editor": [VIEW, "onboarding.edit", "document.upload"]}))
    app.state.auth_provider = ProxyProvider(
        Settings(
            auth_mode="proxy",
            proxy_shared_secret=PROXY_SECRET,
            auth_role_map="eds=editor,revs=admin",
            roles_file=str(path),
        )
    )
    async with person(app, "editor@example.com", "eds") as editor:
        async with person(app, "reviewer@example.com", "revs") as reviewer:
            yield editor, reviewer


async def test_a_save_between_the_review_and_the_submit_refused_the_submission(session):
    """Read the review, save over the draft, click.

    Before the seal existed this sent the *editor's* new person details to
    `POST /v2/onboarding` under the reviewer's actor id, and the console kept no
    record that what went out differed from what had been read — the review was
    a signature on a document somebody swapped afterwards.
    """
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with editor_and_reviewer(app) as (editor, reviewer):
        url = await filled_draft(editor, model)
        # 1. The reviewer reads the page, and reads "Sample value" on it.
        page = await reviewer.get(f"{url}/review")
        assert "Sample value" in page.text and "Mallory Swapped" not in page.text
        clicked = form(reviewed=reviewed_seal(page.text))

        # 2. The editor saves different person details underneath them.
        swapped = swap(model, "p.0.f.firstName", "Mallory Swapped")
        assert (await post(editor, f"{url}/save", swapped)).status_code == 200

        # 3. The reviewer presses the button they were already looking at.
        done = await post(reviewer, f"{url}/submit", clicked)

    sent = [c for c in calls if c[1] == "/v2/onboarding"]
    assert sent == [], f"the swapped body reached the wire: {sent}"
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []
    assert done.status_code == 204
    assert "/review" in done.headers["HX-Redirect"]
    assert "changed+since+it+was+reviewed" in done.headers["HX-Redirect"]


async def test_the_refused_reviewer_is_told_to_re_read_and_can_then_submit(session):
    """The other half of the refusal: it is a recoverable one.

    The redirect lands on the review page, which renders the sentence and — this
    is the point of sending them there rather than to an error page — shows the
    content that is actually stored now. Re-reading mints a fresh seal, and the
    submit that follows sends *those* values, attributed to the reviewer who read
    them.
    """
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with editor_and_reviewer(app) as (editor, reviewer):
        url = await filled_draft(editor, model)
        stale = form(reviewed=reviewed_seal((await reviewer.get(f"{url}/review")).text))
        await post(editor, f"{url}/save", swap(model, "p.0.f.firstName", "Mallory Swapped"))
        refused = await post(reviewer, f"{url}/submit", stale)

        landed = await reviewer.get(refused.headers["HX-Redirect"])
        assert "The draft changed since it was reviewed. Re-read it before submitting." in landed.text
        assert "Mallory Swapped" in landed.text  # what is really there, now read
        done = await post(reviewer, f"{url}/submit", form(reviewed=reviewed_seal(landed.text)))

    assert done.headers["HX-Redirect"] == "/applications/app_1"
    body = json.loads(next(c for c in calls if c[1] == "/v2/onboarding")[2])
    assert body["ownership"]["persons"][0]["firstName"] == "Mallory Swapped"
    op = (await session.execute(SUBMISSIONS)).scalars().one()
    assert op.actor_id == "reviewer@example.com"


async def test_an_unchanged_draft_submitted_exactly_as_it_did_before(session):
    """Criterion 2, stated on its own rather than left to the happy path: the
    guard costs a draft nobody touched nothing at all, across two actors."""
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with editor_and_reviewer(app) as (editor, reviewer):
        url = await filled_draft(editor, model)
        done = await post(reviewer, f"{url}/submit", await read_the_review(reviewer, url))

    assert done.headers["HX-Redirect"] == "/applications/app_1"
    body = json.loads(next(c for c in calls if c[1] == "/v2/onboarding")[2])
    assert body["ownership"]["persons"][0]["firstName"] == "Sample value"
    op = (await session.execute(SUBMISSIONS)).scalars().one()
    assert (op.state, op.actor_id) == ("confirmed", "reviewer@example.com")


async def test_a_save_that_changed_nothing_did_not_cost_the_reviewer_their_submit(session):
    """The reason this is a content hash and not an `updated_at` compare-and-set.

    An autosave that rewrites the same answers — the wizard fires one on every
    field blur — moves the row's timestamp and changes nothing a reviewer could
    have read differently. A version check would refuse that; a hash over the
    answers is indifferent to it, which is the whole distinction between "the
    row was written" and "the content moved".
    """
    app = make_app(
        stub(discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}))
    )
    model = forms.parse(REQUIREMENTS)
    async with editor_and_reviewer(app) as (editor, reviewer):
        url = await filled_draft(editor, model)
        clicked = await read_the_review(reviewer, url)
        assert (await post(editor, f"{url}/save", complete_body(model))).status_code == 200
        done = await post(reviewer, f"{url}/submit", clicked)

    assert done.headers["HX-Redirect"] == "/applications/app_1"


async def test_a_submit_carrying_no_review_seal_at_all_was_refused(session):
    """Absent is a refusal here, not a fallback.

    There is nothing for "no seal" to fall back *to* — the only way to hold one
    is to have been shown the review page — so treating an unreadable token as an
    absent one would hand a caller that skipped the review exactly the behaviour
    it was after.
    """
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        bare = await post(web, f"{url}/submit", b"")
        tampered = await post(web, f"{url}/submit", form(reviewed="not-a-seal"))
        page = await web.get(f"{url}/review")
        flipped = reviewed_seal(page.text).replace(".", ".A", 1)  # a signature, mangled
        forged = await post(web, f"{url}/submit", form(reviewed=flipped))

    for response in (bare, tampered, forged):
        assert "/review" in response.headers["HX-Redirect"]
        assert "changed+since+it+was+reviewed" in response.headers["HX-Redirect"]
    assert [c for c in calls if c[1] == "/v2/onboarding"] == []
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []


async def test_one_drafts_review_seal_did_not_authorise_another_drafts_submit(session):
    """Why the draft id is in the seal alongside the hash.

    Two drafts of one country, filled in from the same template, hash
    identically — so without the id the seal earned by reading the first is a
    live authorisation for the second, whose page nobody opened.
    """
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        read = await filled_draft(web, model)
        unread = await filled_draft(web, model)
        borrowed = await read_the_review(web, read)
        response = await post(web, f"{unread}/submit", borrowed)

    assert "/review" in response.headers["HX-Redirect"]
    assert "changed+since+it+was+reviewed" in response.headers["HX-Redirect"]
    assert [c for c in calls if c[1] == "/v2/onboarding"] == []


# --- attachments --------------------------------------------------------------


def body_with_documents(model, *, root, person0) -> bytes:
    """`complete_body`, with the checklist's and the first person card's `doc_`
    ids replaced. Everything else — including the second card's `doc_p1`, which
    `filled_draft` really uploaded — is left alone."""
    from tests.test_forms import full_submission

    items = [
        (name, value)
        for name, value in full_submission(model, persons=2)
        if name not in ("documentIds", "p.0.documentIds")
    ]
    items += [("documentIds", d) for d in root]
    items += [("p.0.documentIds", d) for d in person0]
    return str(httpx.QueryParams(items)).encode()


def test_attached_ids_are_found_by_key_not_by_the_two_places_todays_dto_uses():
    """`attached_document_ids`' docstring promises that reading off the assembled
    body keeps the check covering ids a future `assemble` sends from somewhere new.

    Hard-coding root and `ownership.persons[]` did not keep that promise: a third
    list would have reached Conduit unchecked while the sentence said otherwise.
    Today's DTO has only those two (OPERATIONS_SPEC §3), so this pins the rule
    rather than closing a live hole.
    """
    body = {
        "documentIds": ["doc_root"],
        "ownership": {"persons": [{"documentIds": ["doc_p0", "doc_root"]}]},
        # Neither of the two places the old walk knew about.
        "evidence": {"attachments": [{"documentIds": ["doc_deep"]}]},
    }
    assert forms.attached_document_ids(body) == ["doc_root", "doc_p0", "doc_deep"]

    # Root still first, and a repeat is one refusal rather than two.
    assert forms.attached_document_ids({"documentIds": ["a", "a"]}) == ["a"]
    assert forms.attached_document_ids({}) == []


async def test_a_document_id_this_draft_never_uploaded_never_reached_conduit(session):
    """Root and per-person `documentIds` went from the draft onto
    `POST /v2/onboarding` untouched: an id another operator had uploaded, or one
    simply guessed at, became identity evidence for *this* customer's
    application, and Conduit's validation is org-wide so nothing downstream
    caught it.

    Both paths onto the body are here — the checklist and a person card — because
    `forms.assemble` puts them in two different places and only one check covers
    both (`forms.attached_document_ids`).
    """
    other, _ = await documents.intake(
        session,
        data=PNG,
        filename="their-passport.png",
        purpose="organization_onboarding",
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
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        await post(
            web,
            f"{url}/save",
            body_with_documents(
                model,
                root=["doc_1", "doc_theirs", "doc_guessed_9999"],
                person0=["doc_p0", "doc_person_guessed"],
            ),
        )
        refused = await post(web, f"{url}/submit", await read_the_review(web, url))

    landed = unquote_plus(refused.headers["HX-Redirect"])
    assert landed.startswith(f"{url}/review")
    assert "Attach only documents you uploaded here for this purpose" in landed
    assert "3 of the attachments could not be matched" in landed
    # Nothing sent, nothing ledgered: refused in front of `operations.start`.
    assert [c for c in calls if c[1] == "/v2/onboarding"] == []
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []
    draft_id = uuid.UUID(url.rsplit("/", 1)[-1])
    assert await documents.attachable(
        session, ["doc_1"], purpose="organization_onboarding", draft_id=draft_id
    ) == {"doc_1"}
    assert await documents.attachable(
        session, ["doc_p0"], purpose="kyc", draft_id=draft_id
    ) == {"doc_p0"}


async def test_a_document_uploaded_against_another_draft_was_not_this_ones_to_send(session):
    """The draft, not the actor, is what onboarding's attachment rule compares.

    One operator running two applications at once is the ordinary case, and the
    substitution this ticket is about does not need a second person: an operator
    who uploads a passport while onboarding one customer can name that same
    `doc_` id on the other's submission, and it would be that customer's proof of
    identity. `documents.attachable` is given the draft, so it is not.
    """
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        first = await filled_draft(web, model)
        await upload_to(web, first, "doc_first_only")

        second = await filled_draft(web, model)
        await post(
            web,
            f"{second}/save",
            body_with_documents(model, root=["doc_1", "doc_first_only"], person0=["doc_p0"]),
        )
        refused = await post(web, f"{second}/submit", await read_the_review(web, second))

    landed = unquote_plus(refused.headers["HX-Redirect"])
    assert landed.startswith(f"{second}/review")
    assert "1 of the attachments could not be matched" in landed
    assert [c for c in calls if c[1] == "/v2/onboarding"] == []
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []
    # …and it is still perfectly attachable on the draft it was uploaded for, by
    # the same operator. The refusal is about the subject, not about them.
    assert await documents.attachable(
        session,
        ["doc_first_only"],
        purpose="organization_onboarding",
        draft_id=uuid.UUID(first.rsplit("/", 1)[-1]),
    ) == {"doc_first_only"}


# --- roles, CSRF, schema drift -------------------------------------------------------


@pytest.mark.parametrize(
    "url,body",
    [
        ("/onboarding", b"country=BGR"),
        ("/documents?purpose=organization_onboarding&filename=x.png", b""),
    ],
)
async def test_a_viewer_cannot_mutate(url, body):
    app = make_app(stub(discovery()))
    async with signed_in(app, groups="readers") as web:
        response = await post(web, url, body)
    assert response.status_code == 403


async def test_the_start_page_names_the_permission_a_draft_needs():
    app = make_app(stub(discovery()))
    async with signed_in(app, groups="readers") as web:
        page = await web.get("/onboarding")
    assert page.status_code == 200
    assert "starting one needs the <code>onboarding.edit</code> permission." in page.text
    assert "hx-post=\"/onboarding\"" not in page.text


async def test_a_viewer_sees_the_form_without_controls(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
    async with signed_in(app, groups="readers") as viewer:
        page = await viewer.get(url)
    assert page.status_code == 200
    assert "Read-only: filling in a draft needs the <code>onboarding.edit</code> permission." in page.text
    # `<main` rather than `<main>`: the tag carries the skip link's target id.
    assert "hx-post=" not in page.text.split("<main")[1]


async def test_a_role_that_may_fill_a_draft_in_but_not_send_it_is_told_where_it_stops(session):
    """Filling a draft in and submitting it are separate permissions, and the
    review page's one button is the second of them."""
    app = make_app(stub(discovery()))
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
    async with signed_in_as(app, "onboarding.edit", "document.upload") as web:
        page = await web.get(url)
        # The draft is completed under this role, so the review page is reached
        # the way it is really reached — nothing is hand-assembled.
        assert (await post(web, f"{url}/review", complete_body(model))).status_code == 204
        review = await web.get(url + "/review")
        refused = await post(web, url + "/submit")

    assert page.status_code == 200 and "Review &amp; submit" in page.text
    assert "onboarding.edit</code> permission" not in page.text
    assert review.status_code == 200
    assert "Submitting needs the <code>onboarding.submit</code> permission." in review.text
    assert "Submit to Conduit" not in review.text
    assert refused.status_code == 403


async def test_every_mutation_needs_the_csrf_header(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        naked = await web.post(
            "/onboarding",
            content=b"country=BGR",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert naked.status_code == 403
    assert "CSRF" in naked.text


async def test_pages_carry_the_token_every_htmx_form_inherits(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        page = await web.get(url)
    token = hx_headers(page.text)["X-CSRF-Token"]
    assert token and token == web.cookies.get("__Host-console_csrf")
    # No form in the page mutates outside htmx — a plain POST could not carry
    # the header, and would be refused.
    assert 'method="post"' not in page.text


async def test_a_schema_version_bump_refuses_to_render_best_effort():
    bumped = {**REQUIREMENTS, "schemaVersion": "4"}
    app = make_app(
        stub({("GET", "/v2/onboarding/requirements"): httpx.Response(200, json=bumped),
              ("GET", "/v2/onboarding/policy-subjects"): httpx.Response(200, json=INDUSTRY)})
    )
    async with signed_in(app) as web:
        response = await post(web, "/onboarding", form(country="BGR"))
    assert response.status_code == 502
    assert "schemaVersion" in response.text


async def test_the_wizard_s_error_re_renders_survive_htmx_response_handling():
    """These 400/502 re-renders answer an `hx-post`, and htmx 2 drops non-2xx
    responses by default — so before the global config they rendered to nothing
    and the operator saw a dead submit button. Latent: the route
    tests passed because httpx does not implement htmx's swap rules."""
    app = make_app(stub({}))
    async with signed_in(app) as web:
        page = await web.get("/onboarding")
        missing_country = await post(web, "/onboarding", form(country=""))

    assert missing_country.status_code == 400  # an intentional render status
    for code in ("400", "422", "502"):
        assert f'{{"code":"{code}","swap":true}}' in page.text.replace(" ", "")
    assert 'hx-target="main" hx-select="main" hx-swap="outerHTML"' in page.text
    assert 'hx-sync="this:drop"' in page.text
    assert 'hx-disabled-elt="find button[type=submit]"' in page.text


async def test_the_autosave_span_renders_its_attributes_verbatim():
    """A pin, not a style check. Every attribute on `#save-state` is load-bearing:
    the id is what the swap targets, `hx-post` is the endpoint, `hx-trigger`
    listens on the form (`from:#wizard`) with the debounce, `hx-include` is what
    gets posted, and `hx-sync="this:replace"` is the separate queue that keeps an
    in-flight autosave from swallowing the operator's submit. A later slice
    restyling or moving this line must not be able to break autosave silently —
    so the whole tag is asserted as one string, not attribute by attribute.

    `hx-disabled-elt="this"` joined the pin later (QA F-004): the
    wizard form's `find button[type=submit]` is *inherited* by this span, which
    resolved it against its own subtree, matched nothing, and made htmx log an
    error on every autosave. The override is part of the load-bearing set now —
    removing it brings the error back."""
    app = make_app(stub(discovery()))
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        wizard = await web.get(url)
    draft_id = url.rsplit("/", 1)[-1]

    assert (
        '<span id="save-state" class="muted" hx-sync="this:replace" hx-disabled-elt="this"\n'
        f'            hx-post="/onboarding/{draft_id}/save"\n'
        '            hx-trigger="change from:#wizard delay:800ms, draft-changed from:body"\n'
        '            hx-include="#wizard">Not saved yet</span>'
    ) in wizard.text
    # It stays inside the form: `hx-include="#wizard"` is explicit, but a span
    # posting the wizard's body from outside the wizard is one refactor away
    # from posting nothing.
    body = wizard.text.split('<form id="wizard"', 1)[1].split("</form>", 1)[0]
    assert 'id="save-state"' in body
    # The old sentence ("Every change is kept — safe to leave and
    # come back.") was unconditional markup making a claim only the pill can
    # make — it sat there unchanged while a save was failing.
    assert "Saved changes are kept — wait for Saved before leaving." in body


async def test_the_submit_and_wizard_forms_cannot_queue_a_second_submission(session):
    """A queued second POST lands after the first operation goes terminal, so
    the double-submit guard has already released and it creates a second
    application. `hx-sync` drops it instead."""
    app = make_app(stub(discovery()))
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        wizard = await web.get(url)
        await post(web, f"{url}/review", complete_body(model))
        review_page = await web.get(f"{url}/review")
    assert "Submit to Conduit" in review_page.text

    for page in (wizard, review_page):
        assert 'hx-sync="this:drop"' in page.text
        assert 'hx-disabled-elt="find button[type=submit]"' in page.text
    # `hx-sync` is inherited: every requester *inside* the wizard needs its own
    # queue, or an autosave or person-card swap in flight would swallow the
    # submit the operator just clicked.
    assert 'id="save-state" class="muted" hx-sync="this:replace"' in wizard.text
    assert wizard.text.count('<button type="button" hx-sync="this:drop"') >= 2
    # …and so is `hx-disabled-elt`, which is the half that was missed (QA F-004).
    # Every requester inside the form overrides it with `this`; the form's own
    # `find button[type=submit]` resolves from nothing else.
    assert wizard.text.count('hx-disabled-elt="find button[type=submit]"') == 1
    assert wizard.text.count('hx-disabled-elt="this"') == 1 + wizard.text.count(
        '<button type="button" hx-sync="this:drop"'
    )


async def test_a_typed_country_name_becomes_its_code_and_anything_else_is_sent_as_typed():
    """Human directive (2026-08-31): "United States" should reach the API as
    `USA`. Exact full names only — `resolve_country` is a lookup, not a guesser —
    so a partial or misspelt one goes to Conduit verbatim and Conduit's own
    refusal is what the operator reads. This asserts the *wire*, which is the
    only place the distinction is real."""
    seen: list[str] = []

    def requirements(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("country", ""))
        return httpx.Response(200, json=REQUIREMENTS)

    app = make_app(stub(discovery({("GET", "/v2/onboarding/requirements"): requirements})))
    async with signed_in(app) as web:
        assert (await post(web, "/onboarding", form(country="United States"))).status_code == 204
        assert (await post(web, "/onboarding", form(country="bgr"))).status_code == 204
        # Two letters that are not a name: sent as typed, for Conduit to judge.
        assert (await post(web, "/onboarding", form(country="zz"))).status_code == 204

    assert seen == ["USA", "BGR", "ZZ"]


async def test_a_country_that_is_neither_a_code_nor_a_name_is_named_as_such():
    """The one local refusal on this field, and it is about shape, not policy:
    `drafts.country` is three characters because the parameter is an ISO code, so
    something longer is neither a code nor a name this table knows. Before this,
    the browser's `pattern` refused it — along with every country *name*, which
    is the input `resolve_country` exists to accept — and the server would have
    met it with a 500 from the insert."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        response = await post(web, "/onboarding", form(country="Untied States"))

    assert response.status_code == 400
    assert "Not a country code, and not a country name this console has a code for." in response.text
    # What was typed comes back in the box, not a blank field.
    assert 'value="Untied States"' in response.text


async def test_an_unresolved_country_code_still_surfaces_conduits_own_refusal():
    """The other half of the rule: this console never decides which countries
    exist. A code it has no name for is sent anyway, and what the operator reads
    is Conduit's own CODE — not a locally invented "no such country". A3 changed
    the words around that code from Conduit's developer prose to this console's;
    the rule this test guards is untouched.
    """
    problem = {
        "type": "COUNTRY_NOT_SUPPORTED",
        "title": "Country not supported",
        "detail": "Onboarding is not available for ZZ.",
        "status": 400,
    }
    app = make_app(
        stub({("GET", "/v2/onboarding/requirements"): httpx.Response(400, json=problem)})
    )
    async with signed_in(app) as web:
        response = await post(web, "/onboarding", form(country="zz"))

    assert response.status_code == 502
    assert "Conduit refused this: COUNTRY_NOT_SUPPORTED" in response.text
    # Conduit's `detail` — developer prose about a country the OPERATOR typed —
    # never reaches the page (A3).
    assert "Onboarding is not available for ZZ." not in response.text


async def test_a_discovery_failure_shows_conduits_own_problem_detail():
    problem = {
        "type": "COUNTRY_NOT_SUPPORTED",
        "title": "Country not supported",
        "detail": "Onboarding is not available for XX.",
        "resolution": "Pick a supported jurisdiction.",
        "correlationId": "corr-77",
        "status": 400,
    }
    app = make_app(
        stub({("GET", "/v2/onboarding/requirements"): httpx.Response(400, json=problem)})
    )
    async with signed_in(app) as web:
        response = await post(web, "/onboarding", form(country="XX"))
    assert response.status_code == 502
    # A3: the code and the correlation id are what survive; the vendor's title,
    # resolution and detail do not.
    for shown in ("Conduit refused this: COUNTRY_NOT_SUPPORTED", "corr-77"):
        assert shown in response.text
    for hidden in ("Country not supported", "Onboarding is not available for XX."):
        assert hidden not in response.text


# --- UI foundation --------------------------------------------------------------------


async def test_static_assets_are_vendored_and_served():
    """Htmx is a committed file, not a CDN fetch: an air-gapped install has to
    render the same console the connected one does."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        assets = {name: await web.get(f"/static/{name}") for name in
                  ("htmx.min.js", "conditions.js", "app.js", "theme.js")}
    assert all(response.status_code == 200 for response in assets.values())
    assert "htmx" in assets["htmx.min.js"].text[:200]
    assert "Conditions" in assets["conditions.js"].text
    assert "data-theme" in assets["theme.js"].text


async def test_every_page_carries_the_chrome():
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        home = await web.get("/", follow_redirects=False)
        drafts_page = await web.get("/drafts")

    # `/` is the dashboard, not a redirect to a list.
    assert home.status_code == 200 and "<h1>Overview</h1>" in home.text
    for link in ("/applications", "/customers", "/drafts"):
        assert f'href="{link}"' in drafts_page.text
    # The environment badge names the environment, colour-coded; since A2 the
    # host is in its `title` rather than in its text (Arca §4.1).
    assert ">Sandbox</span>" in drafts_page.text
    assert "api.sandbox.conduit.financial" in drafts_page.text.split('id="env-badge"')[1][:300]
    assert 'class="badge ok"' in drafts_page.text
    # Only vendored assets: every script this page loads is served by us.
    sources = re.findall(r'<script src="([^"]+)"', drafts_page.text)
    assert sources and all(src.startswith("/static/") for src in sources)


async def test_the_onboard_page_carries_its_action_in_the_header():
    """`/drafts` is the Onboard tab, and per the slice-4 header
    convention its one forward action is the page header's primary pill, not a
    paragraph under the title. A viewer's header carries no action at all."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        operator = (await web.get("/drafts")).text
    async with signed_in(app, groups="readers") as web:
        viewer = (await web.get("/drafts")).text

    assert "<h1>Onboard</h1>" in operator
    assert "<title>Onboard · Conduit Console</title>" in operator
    # Everything between the title and the first section: the page head's action
    # slot (`m.page_head`'s caller) and its lede.
    header = operator.split("<h1>Onboard</h1>")[1].split("<h2>")[0]
    # "Onboard", the same words the chrome pill uses — one name for one
    # destination (design pass 2026-09-02). This is also the one page-head door
    # to /onboarding that survives beside the chrome's, because this page IS the
    # verb: the darkest thing on it is the thing you press.
    assert '<a class="btn primary" href="/onboarding">Onboard</a>' in header
    assert '<a class="btn primary" href="/onboarding">' not in viewer
    # The URL did not move — the Playwright suite and the live e2e navigate it.
    assert 'href="/drafts"' in operator


async def test_an_empty_list_says_what_would_fill_it():
    """DESIGN.md: an empty table is a sentence plus the one action that fills
    it, never a bare "No rows". The drafts list is the check for the sweep —
    an operator who lands on an empty one is told where onboardings start."""
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        empty = await web.get("/drafts")

    assert "Nothing half-finished" in empty.text
    assert 'href="/onboarding"' in empty.text
    # And not the bare version it replaced.
    assert "No drafts yet." not in empty.text


async def test_the_drafts_list_still_tells_the_three_states_apart(session):
    """`/drafts` selects columns rather than entities (it must render without
    the encryption key). Its state pill reads `submitted_at` and `purged_at`,
    and a column dropped from that select would not raise — Jinja would hand
    the template an Undefined and every row would quietly read "In progress"."""
    from sqlalchemy import text

    from app.onboarding import drafts

    ACTOR = "ops@example.com"  # who web_harness.signed_in signs in as
    open_one = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="BG"
    )
    sent = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="IT"
    )
    await drafts.submitted(session, sent.id)
    purged = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="FR"
    )
    await drafts.submitted(session, purged.id)
    await session.execute(
        text("update drafts set payload = null, purged_at = now() where id = :id"),
        {"id": purged.id},
    )
    await session.commit()

    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        html = (await web.get("/drafts")).text

    assert '<span class="pill">In progress</span>' in html
    assert '<span class="pill wait">Submitted</span>' in html
    assert '<span class="pill muted">Submitted · answers purged</span>' in html
    for row in (open_one, sent, purged):
        assert f'href="/onboarding/{row.id}"' in html


async def test_the_drafts_list_names_the_applicant_out_of_the_ciphertext(session):
    """The applicant's legal name is *inside* the
    encrypted answers, so the list decrypts per row — the tolerant pattern
    `app/counterparties.py` uses — rather than through the TypeDecorator, which
    would run inside the query and let one unreadable row take the page down.

    Nothing is stored in plaintext and no column was added: the name is read at
    render time and thrown away, and a row that cannot be read (or has not been
    answered yet) keeps its reference and says it has no name.
    """
    from sqlalchemy import text

    from app.onboarding import drafts

    ACTOR = "ops@example.com"
    named = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="BG"
    )
    await drafts.update_payload(
        session, named, {"root": {"businessInfo": {"legalName": "Acme Robotics LLC"}}, "persons": []}
    )
    blank = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="IT"
    )
    corrupt = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="FR"
    )
    await drafts.update_payload(
        session, corrupt, {"root": {"businessInfo": {"legalName": "Unreadable Ltd"}}}
    )
    await session.execute(
        text("update drafts set payload = :junk where id = :id"),
        {"junk": b"not-a-fernet-token", "id": corrupt.id},
    )
    await session.commit()

    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        html = (await web.get("/drafts")).text

    assert "Acme Robotics LLC" in html
    assert "Unreadable Ltd" not in html
    assert html.count("unnamed so far") == 2  # the unanswered one and the corrupt one
    for row in (named, blank, corrupt):
        assert f'href="/onboarding/{row.id}"' in html


async def test_the_drafts_state_filter_is_the_open_drafts_tiles_own_predicate(session):
    """The Overview's Open drafts tile counts this actor's *unsubmitted* drafts
    and links here, so `?state=open` has to be exactly that set — a link that
    landed on the unfiltered page would show more rows than the number the
    operator just read. An unknown value is the unfiltered page, not an error."""
    from app.onboarding import drafts

    ACTOR = "ops@example.com"
    await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="BG"
    )
    sent = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="IT"
    )
    await drafts.submitted(session, sent.id)
    await session.commit()

    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        pages = {
            state: (await web.get(f"/drafts?state={state}")).text
            for state in ("", "open", "submitted", "nonsense")
        }

    assert ">BG<" in pages["open"] and ">IT<" not in pages["open"]
    assert ">IT<" in pages["submitted"] and ">BG<" not in pages["submitted"]
    for unfiltered in (pages[""], pages["nonsense"]):
        assert ">BG<" in unfiltered and ">IT<" in unfiltered


async def test_the_drafts_list_is_paged_and_the_state_filter_rides_the_page_turn(session):
    """This list had **no bound at all** — every onboarding this
    actor ever started, one Fernet decrypt per row for its applicant name. The
    house offset pager, with `?state=` carried across the turn: a page two that
    silently dropped the filter would show a different question's rows."""
    from app.onboarding import drafts

    ACTOR = "ops@example.com"
    for n in range(26):
        await drafts.create(
            session,
            kind="onboarding",
            actor_id=ACTOR,
            requirements_snapshot={},
            country="BG",
            client_reference_id=f"ref-{n:02d}",
        )
    sent = await drafts.create(
        session, kind="onboarding", actor_id=ACTOR, requirements_snapshot={}, country="IT"
    )
    await drafts.submitted(session, sent.id)
    await session.commit()

    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        first = (await web.get("/drafts?state=open")).text
        second = (await web.get("/drafts?state=open&limit=25&offset=25")).text
        wide = (await web.get("/drafts?state=open&limit=50")).text

    assert first.count("ref-") == 25 and second.count("ref-") == 1
    # The submitted one is on neither page of the open filter.
    assert ">IT<" not in first and ">IT<" not in second
    assert "/drafts?state=open&amp;limit=25&amp;offset=25" in first
    assert "Previous" in second
    assert wide.count("ref-") == 26 and "Next" not in wide


async def test_the_api_key_never_reaches_a_rendered_page():
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
        pages = [await web.get(p) for p in ("/drafts", "/onboarding", url)]
    for page in pages:
        assert "test-key-not-real" not in page.text


# --- onboarding-submit findings -------------------------------------------------------

XSS = '<img src=x onerror="alert(1)">'


async def test_conduit_error_text_is_escaped_before_it_reaches_the_chip(session):
    """The chip is inserted with `insertAdjacentHTML`, and its text is Conduit's
    own `detail` — untrusted markup on the way into an executing sink."""
    problem = {
        "type": "DOCUMENT_REJECTED",
        "title": "Rejected",
        "detail": f"Bad document {XSS}",
        "status": 400,
    }
    app = make_app(
        stub(discovery({("POST", "/v2/documents"): httpx.Response(400, json=problem)}))
    )
    async with signed_in(app) as web:
        url = await new_draft(web)
        chip = await web.post(
            f"/documents?purpose=organization_onboarding&filename=x.png&draft={url.rsplit('/', 1)[-1]}",
            content=PNG,
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )

    # The markup is inert: no live tag, no live attribute. **A3 goes further
    # than escaping on this path** — the chip prints this console's sentence
    # for the code, so Conduit's `detail` never reaches the page in any form,
    # escaped or not. Jinja's escaping is unchanged and is still what protects
    # every string this console does render.
    assert "<img" not in chip.text
    assert 'onerror="' not in chip.text
    assert XSS not in chip.text and "&lt;img" not in chip.text
    assert "Conduit refused this: DOCUMENT_REJECTED" in chip.text


async def test_a_rejected_upload_reason_is_escaped_too(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        await new_draft(web)
        chip = await web.post(
            f"/documents?purpose=organization_onboarding&filename={XSS}.pdf",
            content=b"not a real document",
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )
    assert "<img" not in chip.text and 'onerror="' not in chip.text


# --- draft ownership ------------------------------------------------------------------


def other_operator(app):
    """A second, equally-authorised operator — the IDOR is between colleagues,
    not between an operator and an outsider."""
    from tests.web_harness import PROXY_SECRET

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://console.test",
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "other@example.com",
            "X-Auth-Request-Email": "other@example.com",
            "X-Auth-Request-Groups": "ops",
        },
    )


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/review")])
async def test_another_operator_cannot_read_a_draft(method, suffix, session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
    async with other_operator(app) as intruder:
        await intruder.get("/drafts")  # earn a CSRF cookie
        response = await intruder.request(method, url + suffix)
    # 404, not 403: a 403 would confirm the id exists.
    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["/save", "/persons", "/review", "/submit", "/discard"])
async def test_another_operator_cannot_write_a_draft(suffix, session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
    async with other_operator(app) as intruder:
        await intruder.get("/drafts")
        response = await post(intruder, url + suffix, b"")
    assert response.status_code == 404
    # …and the draft is untouched.
    assert await drafts.load(session, uuid.UUID(url.rsplit("/", 1)[-1])) is not None


async def test_another_operator_cannot_attach_a_document_to_a_draft(session):
    app = make_app(
        stub(discovery({("POST", "/v2/documents"): httpx.Response(201, json={"id": "doc_9"})}))
    )
    async with signed_in(app) as web:
        url = await new_draft(web)
    async with other_operator(app) as intruder:
        await intruder.get("/drafts")
        response = await intruder.post(
            f"/documents?purpose=organization_onboarding&filename=x.png&draft={url.rsplit('/', 1)[-1]}",
            content=PNG,
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": intruder.cookies.get("__Host-console_csrf"),
            },
        )
    assert response.status_code == 404


async def test_an_admin_may_open_a_colleagues_draft(session):
    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        url = await new_draft(web)
    async with signed_in(app, groups="admins") as boss:
        response = await boss.get(url)
    assert response.status_code == 200
    assert "Business information" in response.text


# --- upload cap -----------------------------------------------------------------------


async def test_an_oversize_upload_is_refused_without_buffering_it(session):
    """A chunked body has no Content-Length to check, so the cap has to apply to
    the stream itself."""
    sent = 0

    async def chunks():
        nonlocal sent
        for _ in range(40):  # 40 × 1 MiB, well past the 10 MiB limit
            sent += 1
            yield b"\x00" * (1024 * 1024)

    app = make_app(stub(discovery()))
    async with signed_in(app) as web:
        response = await web.post(
            "/documents?purpose=organization_onboarding&filename=big.pdf",
            content=chunks(),
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )

    assert response.status_code == 413
    assert "larger than 10 MB" in response.text
    # Aborted mid-stream: the whole 40 MiB was never read into memory.
    assert sent <= 12, f"read {sent} MiB before refusing"
    assert (await session.execute(SUBMISSIONS)).scalars().all() == []


async def test_a_person_document_is_checked_under_the_purpose_its_uploader_sends(session):
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        await upload_to(web, url, "doc_company")
        await upload_to(web, url, "doc_person", person=0)
        await post(
            web,
            f"{url}/save",
            body_with_documents(model, root=["doc_company"], person0=["doc_person"]),
        )
        sent = await post(web, f"{url}/submit", await read_the_review(web, url))

    landed = unquote_plus(sent.headers["HX-Redirect"])
    assert "could not be matched" not in landed
    assert [c for c in calls if c[1] == "/v2/onboarding"] != []

    draft_id = uuid.UUID(url.rsplit("/", 1)[-1])
    assert await documents.unattachable(
        session, ["doc_person"], purpose="organization_onboarding", draft_id=draft_id
    ) == ["doc_person"]
    assert await documents.unattachable(
        session, ["doc_person"], purpose="kyc", draft_id=draft_id
    ) == []


async def test_a_person_document_reused_as_a_company_one_is_refused(session):
    calls: list = []
    app = make_app(
        stub(
            discovery({("POST", "/v2/onboarding"): httpx.Response(202, json=APPLICATION)}),
            calls,
        )
    )
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        await upload_to(web, url, "doc_person", person=0)
        await post(
            web,
            f"{url}/save",
            body_with_documents(model, root=["doc_person"], person0=["doc_person"]),
        )
        sent = await post(web, f"{url}/submit", await read_the_review(web, url))

    landed = unquote_plus(sent.headers["HX-Redirect"])
    assert "could not be matched" in landed
    assert [c for c in calls if c[1] == "/v2/onboarding"] == [], "nothing may be sent"
    assert "1 of the attachments could not be matched" in landed


async def test_a_person_scoped_upload_is_stored_under_the_person_purpose(session):
    """The scope decides the purpose, not the caller.

    `upload` chips the result into `p.N.documentIds` whenever `person` is
    present, so a request naming the company purpose alongside `person` produced
    a person chip backed by an `organization_onboarding` blob — which submit then
    refuses, from a slot the operator cannot re-upload into any other way.
    """
    app = make_app(stub(discovery({})))
    model = forms.parse(REQUIREMENTS)
    async with signed_in(app) as web:
        url = await filled_draft(web, model)
        draft_id = url.rsplit("/", 1)[-1]
        chip = await web.post(
            f"/documents?purpose={PURPOSE}&person=0&filename=doc_scoped.png&draft={draft_id}",
            content=PNG,
            headers={
                "content-type": "application/octet-stream",
                "X-CSRF-Token": web.cookies.get("__Host-console_csrf"),
            },
        )
        assert 'name="p.0.documentIds"' in chip.text

    stored = (
        await session.execute(
            select(DocumentBlob.purpose).where(DocumentBlob.filename == "doc_scoped.png")
        )
    ).scalars().all()
    assert stored == [PERSON_PURPOSE]
