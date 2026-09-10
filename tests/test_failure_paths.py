"""Failure paths, in the chrome and in this console's voice.

Three claims, pinned here because each of them is a promise to a reader who
cannot walk over and ask anybody (Arca spec §0.1):

1. **403 / 404 / 500 are pages**, extending `base.html`, with the ribbon, the
   right status and a way back — not `{"detail": "Not Found"}` (§0.2).
2. **No upstream problem reaches a screen in Conduit's words.** The translation
   is at ONE boundary (`conduit.parse_problem`) and its table is drift-pinned
   against the spec's own error catalogue, so a code Conduit adds fails a test
   here rather than rendering developer prose to a treasury operator (§0.2).
3. **A field error is bound to its field** (`aria-invalid` + `aria-describedby`),
   and an error that matches no rendered field is listed once under the form
   rather than dropped (FORM_ENGINE_SPEC §7's mapper contract, said to a screen
   reader).

The 500 page gets its own section: what it must NOT contain is the whole point.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
import pytest

from app import forms
from app.conduit import FieldError, parse_problem
from app.conduit.problems import MINTED_HERE, TITLES, UNTRANSLATED, console_words
from app.web import problem_of, templates

from tests.web_harness import PROXY_SECRET, make_app, post, signed_in, stub
from tests.conftest import ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUIREMENTS = json.loads((FIXTURES / "onboarding_requirements_USA.json").read_text())


def nothing() -> object:
    """A Conduit stub every route can hold but no test in this file calls."""
    return stub({})


# --- the catalogue, and the drift pin -------------------------------------------------


def spec_error_codes() -> set[str]:
    """Every error code the pinned spec names, read the way the spec writes them.

    Conduit documents its catalogue **in the response descriptions**, as
    `**CODE**: what it means`, one paragraph per code — the `type` property
    itself is only `type: string` with an example, so a schema walk sees a
    fraction of it. This is the whole catalogue, 4xx and 5xx, every path.
    """
    spec = json.loads((ROOT / "contracts" / "openapi_production.json").read_text())
    found: set[str] = set()
    for item in spec["paths"].values():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            for status, response in (operation.get("responses") or {}).items():
                if status[:1] not in "45":
                    continue
                found |= set(
                    re.findall(r"\*\*([A-Z][A-Z0-9_]{2,})\*\*", response.get("description") or "")
                )
    return found


def test_the_extraction_finds_a_catalogue_at_all():
    """Non-vacuity, and loud on rename (the `test_vocabulary_drift` rule): if
    Conduit ever stops writing `**CODE**:` in its descriptions this reads zero
    codes and every assertion below would pass while checking nothing."""
    codes = spec_error_codes()
    assert len(codes) > 100, f"only {len(codes)} codes — has the spec's format changed?"
    # Three that must always be in any real catalogue.
    assert {"CUSTOMER_NOT_FOUND", "RATE_LIMITED", "INTERNAL_ERROR"} <= codes


def test_every_code_in_the_spec_is_accounted_for():
    """**The drift pin.** Both directions, like `PILL_TONES`:

    * a code Conduit ADDS is in neither `TITLES` nor `UNTRANSLATED` and fails
      here — it does not quietly reach an operator as vendor prose;
    * a row for a code Conduit REMOVED is visible too, rather than rotting.

    `UNTRANSLATED` is a decision, not a hole: those codes render the same
    `Conduit refused this: {code}` shape an unknown one does. Moving a code
    from it into `TITLES` is how a new surface earns a sentence.
    """
    assert TITLES.keys() | UNTRANSLATED == spec_error_codes()
    assert not (TITLES.keys() & UNTRANSLATED), "a code cannot be both translated and not"


def test_the_codes_this_console_mints_are_not_conduits():
    """`MINTED_HERE` is for codes the RECONCILER writes onto `operations.error`.
    If Conduit ever defines one of those names, the collision has to be seen
    here rather than discovered as a sentence attributed to the wrong author."""
    assert not (MINTED_HERE.keys() & spec_error_codes())


@pytest.mark.parametrize("code", sorted(TITLES))
def test_every_translation_is_two_sentences_of_this_consoles_own(code):
    title, resolution = TITLES[code]
    assert title and resolution
    assert not title.endswith("."), f"{code}: the title is a heading, not a sentence"
    assert resolution.endswith("."), f"{code}: the resolution is a full instruction"
    # The wire vocabulary never leaks into the prose: no SCREAMING_CASE, no
    # `camelCase` field names, no "the API".
    assert not re.search(r"[A-Z]{3,}_[A-Z]", title + resolution), code
    assert "API" not in title, code


# --- one boundary: nothing Conduit wrote reaches a screen -------------------------------


VENDOR = {
    "type": "CUSTOMER_NOT_FOUND",
    "title": "Customer Not Found",
    "status": 404,
    "detail": "Customer with id cus_034A0gCCVsxdV2PjHLx9k1 not found",
    "resolution": "Verify the customer ID. Check you are using the correct API key for "
    "this organization.",
    "docs": "https://conduit-v2.mintlify.app/errors#customer-not-found",
    "correlationId": "cor_boundary_1",
}


def test_the_client_replaces_the_vendors_prose_and_keeps_the_evidence():
    problem = parse_problem(httpx.Response(404, json=VENDOR))

    assert problem.type == "CUSTOMER_NOT_FOUND"  # the code is untouched: support quotes it
    assert (problem.title, problem.resolution) == TITLES["CUSTOMER_NOT_FOUND"]
    assert problem.detail == ""
    assert problem.correlation_id == "cor_boundary_1"
    # …and every byte Conduit sent is still there, for the ledger.
    assert problem.raw == VENDOR


def test_an_unknown_code_renders_as_the_code_and_never_as_the_vendors_detail():
    """The shape for anything the table has no sentence for — a code Conduit
    adds, or one of the `UNTRANSLATED` surfaces this console does not operate.

    The `detail` below is deliberately unmistakable: if it ever appears on a
    screen, this assertion is the thing that says where it came from.
    """
    body = {
        "type": "WALLET_ROTATION_IN_PROGRESS",
        "title": "Wallet Rotation In Progress",
        "detail": "GIRAFFE-CANARY-9137: inspect the optional 'field' member of the response",
        "resolution": "Wait for the in-flight rotation to finish.",
        "correlationId": "cor_unknown_1",
    }
    problem = parse_problem(httpx.Response(409, json=body))

    assert problem.title == "Conduit refused this: WALLET_ROTATION_IN_PROGRESS"
    # The resolution rides along — for a refusal this console cannot explain it
    # is the one remaining piece of guidance, and it is at least action-shaped.
    assert problem.resolution == "Wait for the in-flight rotation to finish."
    assert "GIRAFFE-CANARY-9137" not in (problem.title + problem.detail + problem.resolution)
    assert problem.detail == ""


@pytest.mark.parametrize(
    "wire",
    [
        "about:blank",
        "https://vendor.example/errors#customer-not-found?key=sk_live_1234",
        "Customer with id cus_1 not found",
        "",
        None,
        {"nested": "object"},
        "lowercase_code",
        "X" * 200,
    ],
)
def test_a_type_that_is_not_a_code_is_not_printed_as_one(wire):
    """**A3 gate m2.** `type` is a free string on the wire — RFC 7807 calls it a
    URI — and whatever it holds was being printed into a title and from there
    into a `?err=` query string. A code is SCREAMING_SNAKE or it is UNKNOWN.

    Both doors are pinned: the live parse and the ledger replay.
    """
    from app.web import problem_of

    problem = parse_problem(httpx.Response(422, json={"type": wire, "detail": "x"}))
    assert problem.title == "Conduit's answer could not be read (HTTP 422)"

    view = problem_of({"type": wire, "status": 422})
    assert view is not None
    assert view["title"] == "Conduit's answer could not be read (HTTP 422)"

    for rendered in (problem.title, view["title"]):
        assert "http" not in rendered and "sk_live" not in rendered


def test_a_body_with_no_code_is_not_given_one():
    """A gateway's HTML page, a truncated body, anything in front of Conduit.
    Naming a code that was never sent would be a lie, so the status is all the
    page claims."""
    problem = parse_problem(httpx.Response(502, content=b"<html>502 Bad Gateway</html>"))
    assert problem.title == "Conduit's answer could not be read (HTTP 502)"
    assert "502 Bad Gateway" not in problem.title + problem.resolution


def test_a_named_resource_is_printed_only_when_it_is_an_id():
    """**A3 gate m5.** `details.*Id` is vendor-supplied text printed verbatim
    beside a refusal — the one door the translation does not cover. It is held
    to the shape of a Conduit id now."""
    from app.web import named_resources

    good = {"details": {"customerId": "cus_2xPqN8RTLm9KvBcXjY5wHz"}}
    assert named_resources(good) == [("customer", "cus_2xPqN8RTLm9KvBcXjY5wHz")]

    hostile = {
        "details": {
            "customerId": "GIRAFFE-CANARY-9137 see https://vendor.example/errors",
            "orderId": "",
            "walletId": "<script>alert(1)</script>",
        }
    }
    assert named_resources(hostile) == []


def test_the_replay_path_translates_too():
    """`operations.error` keeps Conduit's body verbatim — that is the evidence
    of what was said — so the page that reads it back is the second and last
    place vendor prose could surface. `web.problem_of` uses the same table."""
    from app.web import problem_of

    view = problem_of(VENDOR)
    assert view is not None
    assert view["title"] == TITLES["CUSTOMER_NOT_FOUND"][0]
    assert view["detail"] == ""
    assert VENDOR["resolution"] not in view.values()


def test_a_snapshot_this_console_minted_keeps_its_own_words():
    """The reconciler observing a terminal state is not a refusal by Conduit,
    and must not be printed as one."""
    from app.web import problem_of

    view = problem_of({"type": "RESOURCE_NOT_ACTIONABLE", "title": "x", "status": 409})
    assert view is not None
    assert view["title"] == "Too late to apply"


def test_no_reader_of_a_stored_operation_error_bypasses_the_translation():
    """**A3 gate M1, pinned.** `Operation.error` holds Conduit's body verbatim —
    that is deliberate, it is the evidence — so every reader of it has to come
    back through `problem_of`. Four did not: the batch detail cell and the
    results CSV (via `batches.rows_of`), the revoke banner, the cancel banner
    and the order execute/cancel banner, each lifting `title` (one of them
    `detail` too) straight out of the snapshot.

    A grep, because what fails is a *new call site*: the type is a plain dict
    and nothing in the type system stops the sixth one.
    """
    watched = sorted(
        [Path("app") / "batches.py", *(Path("app") / "web").glob("*.py")]
    )
    offenders = []
    for path in watched:
        source = (ROOT / path).read_text()
        for pattern in (r'error\s*or\s*\{\}\)\.get\("title"', r'error\s*or\s*\{\}\)\.get\("detail"'):
            if re.search(pattern, source):
                offenders.append(f"{path}: {pattern}")
    assert not offenders, f"read the stored body directly — use problem_of: {offenders}"


def test_the_batch_row_carries_the_translated_view_not_the_stored_body():
    """The same claim from the other end: what `rows_of` puts on a row, and
    therefore what the detail page's cell and `problem_title` print."""
    from app import batches

    row = {"problem": problem_of(VENDOR), "dispatch_error": None, "errors": []}
    assert batches.problem_title(row) == TITLES["CUSTOMER_NOT_FOUND"][0]
    assert row["problem"]["detail"] == ""


def test_no_template_reads_a_problems_detail_from_the_wire():
    """The structural half of the boundary claim: `problem()` renders `p.detail`,
    and the only things that fill it are `local_problem` and `problem_of` — both
    of which write this console's own sentences. `parse_problem` sets it to "".

    Pinned as a grep because the leak this closes was a *call site* problem: a
    tenth route rendering `result.detail` into a banner would reopen it, and the
    thing to notice is that nobody has to remember, because the field is empty.
    """
    source = (ROOT / "app" / "conduit" / "client.py").read_text()
    assert 'detail=""' in source
    assert 'detail=str(body.get("detail")' not in source


# --- 403 / 404 / 500 are pages ----------------------------------------------------------


CHROME = ('<nav class="ribbon"', "Conduit Console", "</html>")


async def test_a_mistyped_url_is_a_page_not_json():
    app = make_app(nothing())
    async with signed_in(app) as web:
        response = await web.get("/customers/../not-a-page")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    for mark in CHROME:
        assert mark in response.text
    assert "There is nothing at this address" in response.text
    assert '<a href="/">Go to the Overview</a>' in response.text
    # The path is not echoed back: a 404 that quoted its own URL would render
    # whatever a link in an email put there.
    assert "not-a-page" not in response.text
    assert '{"detail"' not in response.text


async def test_a_viewer_posting_an_admin_action_gets_the_403_page():
    """A real gate: `require()` refuses, and the refusal is a page inside the
    chrome — the reader can see which roles they hold and where to go next."""
    app = make_app(nothing())
    async with signed_in(app, groups="readers") as web:
        # Through `post`, so the CSRF header is there and the refusal that
        # answers is `require()`'s and not the middleware's.
        response = await post(web, f"/operations/{uuid.uuid4()}/abandon")

    assert response.status_code == 403
    for mark in CHROME:
        assert mark in response.text
    assert "You do not hold this action" in response.text
    # **The permission IS named** (A3 gate ruling, m4): an operator who cannot
    # tell an administrator which action to grant is stranded on the page that
    # exists to unstrand them, and the gate names are already in PERMISSIONS.md
    # and in every deployment's ROLES_FILE.
    sentence = re.search(r'<p class="help">(.*?)</p>', response.text, re.S)
    assert sentence, response.text.split("<main")[1][:800]
    assert "operation.abandon" in sentence.group(1)
    assert "An administrator grants it by that name." in sentence.group(1)
    # What is still never named: WHICH roles hold it, or who does. The gate's
    # sentence names the action and stops there.
    assert not {"admin", "operator", "viewer"} & set(sentence.group(1).split())


async def test_the_500_page_carries_a_reference_and_nothing_else():
    """A route that raises. The page is in the chrome, it is a 500, and the only
    thing on it about the failure is an id the log also carries."""
    app = make_app(nothing())

    @app.get("/boom-for-the-test")
    async def boom():
        raise RuntimeError("SECRET-CANARY-4471 postgresql://user:pw@db/console")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://console.test",
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "ops@example.com",
            "X-Auth-Request-Email": "ops@example.com",
            "X-Auth-Request-Groups": "ops",
        },
    ) as web:
        response = await web.get("/boom-for-the-test")

    assert response.status_code == 500
    for mark in CHROME:
        assert mark in response.text
    assert "Something in this console broke" in response.text

    # The reference: on the page, and it is the request id, not a guess.
    reference = re.search(r'<code class="num">([0-9a-f]{12})</code>', response.text)
    assert reference, response.text[-2000:]

    # …and nothing else about the failure. Not the exception, not a traceback,
    # not the DSN it carried, not the path, not the key.
    for leak in (
        "SECRET-CANARY-4471",
        "postgresql://",
        "RuntimeError",
        "Traceback",
        "boom-for-the-test",
        "test-key-not-real",
    ):
        assert leak not in response.text, leak
    # The API HOST is asserted absent from the PAGE, not from the document: the
    # env badge prints it in the chrome of every page in this build, and taking
    # it out of the badge is a decided change (spec
    # §4.2), not a second edit to the same line from here. What A3 owes is that
    # the failure page itself names no host — and when A2 lands, this assertion
    # holds over the whole document without being touched.
    page = response.text.split("<main")[1]
    assert "conduit.financial" not in page


async def test_the_500_page_still_carries_the_security_headers():
    """`ServerErrorMiddleware` sits OUTSIDE the middleware that sets them, so
    this response is the one that would ship bare. The handler applies them
    itself — see `main.unhandled`."""
    from app.main import SECURITY_HEADERS

    app = make_app(nothing())

    @app.get("/boom-headers")
    async def boom():
        raise RuntimeError("nope")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://console.test",
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "ops@example.com",
            "X-Auth-Request-Email": "ops@example.com",
            "X-Auth-Request-Groups": "ops",
        },
    ) as web:
        response = await web.get("/boom-headers")

    assert response.status_code == 500
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_the_webhook_receiver_still_answers_a_machine():
    """**A3 gate n7.** Conduit's sender reads a status and a JSON body. An HTML
    page with a ribbon in it is not an answer to a machine, and the status has
    to survive unchanged — the sender's retry policy reads it."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        response = await web.post(
            "/webhooks/conduit", content=b"{}", headers={"content-type": "application/json"}
        )

    assert response.status_code == 400  # signature did not verify
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "invalid signature"}
    assert "<html" not in response.text


async def test_a_route_that_wrote_its_own_refusal_keeps_the_sentence():
    """`exports.py` explains why a file would have lied. That is this console's
    own copy and it survives; the framework's stock phrase does not."""
    app = make_app(nothing())
    async with signed_in(app) as web:
        response = await web.get("/export/nonsense.csv")

    assert response.status_code == 404
    assert "No such export surface." in response.text
    assert "There is nothing at this address" in response.text


# --- field errors are bound to their fields ---------------------------------------------


def render_field_with(pointer: str, detail: str, category: str | None = None):
    """One field, rendered through the real macro with a real mapped error."""
    model = forms.parse(REQUIREMENTS)
    errors = forms.map_validation_errors(
        model, [FieldError(pointer=pointer, detail=detail, category=category, allowed_values=[])]
    )
    rendered = forms.render_model(model, forms.FormValues(), errors)
    return rendered, str(templates.env.get_template("macros.html").module.field(
        next(f for group in rendered.groups for f in group.fields if f.errors)
    ))


def test_a_field_error_is_bound_to_its_field():
    _, html = render_field_with("/businessInfo/taxId", "Invalid tax identifier format")

    assert 'id="f.businessInfo.taxId"' in html
    assert 'aria-invalid="true"' in html
    assert 'aria-describedby="f.businessInfo.taxId-error"' in html
    # …and the thing it points at exists, exactly once, and holds the sentence.
    assert html.count('id="f.businessInfo.taxId-error"') == 1
    assert "Invalid tax identifier format" in html


def test_a_widget_with_no_single_control_binds_every_input():
    """A checkbox group and a Yes/No radio have no element carrying the field's
    id, so the binding goes on each input — `aria-describedby` is announced on
    the control that takes focus, never on the box around it."""
    _, group = render_field_with("/businessActivity/countriesOfActivity", "Pick at least one")
    assert group.count('aria-describedby="f.businessActivity.countriesOfActivity-error"') > 1
    assert group.count('id="f.businessActivity.countriesOfActivity-error"') == 1

    _, radio = render_field_with("/operatingAddress/sameAsRegistered", "Answer this")
    assert radio.count('aria-describedby="f.operatingAddress.sameAsRegistered-error"') == 2


def test_a_field_with_no_error_carries_no_aria_and_no_error_box():
    model = forms.parse(REQUIREMENTS)
    rendered = forms.render_model(model, forms.FormValues(), forms.FormErrors())
    html = str(
        templates.env.get_template("macros.html").module.field(rendered.groups[0].fields[0])
    )
    assert "aria-invalid" not in html and "aria-describedby" not in html
    assert "-error" not in html


def test_an_error_that_matches_no_rendered_field_is_listed_once():
    """The mapper's "never dropped" rule (FORM_ENGINE_SPEC §7), and *once*: an
    unbound pointer has no field to sit under, so the form-level list is the
    only place it can be — and two copies of one refusal reads as two refusals.
    """
    model = forms.parse(REQUIREMENTS)
    errors = forms.map_validation_errors(
        model,
        [
            FieldError(
                pointer="/individual:any",
                detail="At least 1 any(s) required, got 0",
                category="individual",
                allowed_values=[],
            )
        ],
    )
    rendered = forms.render_model(model, forms.FormValues(), errors)

    assert [m.detail for m in rendered.form_errors] == [
        "/individual:any: At least 1 any(s) required, got 0"
    ]
    # Nowhere else: not silently attached to a field it does not name, and not
    # dropped. The ONCE-on-a-real-page half is pinned where a real 422 renders
    # (`test_web_onboarding.test_a_422_is_mapped_onto_the_form_it_came_from`),
    # against the captured live fixture that carries exactly this error.
    assert not any(f.errors for group in rendered.groups for f in group.fields)
    assert rendered.document_errors == []


def test_every_code_the_reconciler_mints_has_words_of_its_own():
    """**A3 gate n11.** A snapshot this console writes onto `operations.error`
    and later reads back must not be printed as "Conduit refused this: …" — the
    reconciler observing a terminal state is not a refusal Conduit made. The
    codes it mints are literals in its own source; every one needs a row."""
    source = (ROOT / "app" / "reconciliation" / "service.py").read_text()
    minted = set(re.findall(r'"type":\s*"([A-Z][A-Z0-9_]+)"', source))
    assert minted, "the mint sites moved — this pin is checking nothing"
    assert minted <= MINTED_HERE.keys()


def test_a_stock_phrase_in_any_case_is_not_echoed_as_help():
    """**A3 gate n6.** A route writing `detail="not found"` was being printed
    under the heading as if it explained something."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.main import _route_sentence

    for stock in ("Not Found", "not found", "NOT FOUND"):
        assert _route_sentence(StarletteHTTPException(404, stock)) == ""
    assert (
        _route_sentence(StarletteHTTPException(404, "No such batch for this customer."))
        == "No such batch for this customer."
    )


def test_the_unknown_shape_says_nothing_a_reader_could_mistake_for_advice():
    """`console_words` on a code nobody has written a sentence for: the code,
    and Conduit's resolution only if it sent one. Never an invented instruction.
    """
    assert console_words("NEW_CODE_FROM_CONDUIT", 422, "") == (
        "Conduit refused this: NEW_CODE_FROM_CONDUIT",
        "",
    )
