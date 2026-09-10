"""The permission catalog: the regression pin, the coverage rule, the invariants.

Three things are load-bearing here.

1. **Nothing moved.** `role_matrix_before.json` is the route × built-in-role
   matrix as it stood at f6e3b7c, generated from the `require("operator")` gates
   this phase replaced. Every route must answer the same way for viewer,
   operator and admin as it did then.
2. **Nothing can ship ungated.** Every mutating route declares a cataloged
   permission, checked against the app's own routing table rather than by
   grepping — so a new POST with no gate fails this file by name.
3. **A bad role file does not boot.** Each startup invariant is exercised
   through `Settings`, which is where a deployment meets it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.auth.providers import ProxyProvider
from app.auth.web import route_gates
from app.config import Settings
from app.main import create_app
from app.permissions import (
    ADMIN_ONLY,
    BUILTIN_ROLES,
    OPERATOR,
    PERMISSIONS,
    REQUIRES,
    ROLES,
    VIEW,
    parse,
    permissions_for,
    role_table,
)
from scripts.generate_permission_matrix import OUTPUT, generate
from tests.web_harness import PROXY_SECRET, client, make_app, post, stub

ROOT = Path(__file__).resolve().parent.parent
BEFORE = json.loads((Path(__file__).parent / "role_matrix_before.json").read_text())

# Six routes carry no permission on purpose; PERMISSIONS.md states each reason.
UNGATED = {
    "GET /health/live",
    "GET /health/ready",
    "POST /webhooks/conduit",  # Conduit's signature, no operator, no session
    "GET /auth/login",
    "POST /auth/logout",  # anyone signed in may end their own session
    "GET /customers/{customer_id}/counterparties",  # a 301 to the renamed page
}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def gates() -> list[tuple[str, str, str, frozenset[str]]]:
    return route_gates(create_app())


# --- 1. the regression pin ------------------------------------------------------------


def test_the_builtin_role_matrix_is_byte_identical_to_before_the_catalog():
    """Route × role, before (roles) == after (permissions), for all three built-ins."""
    after = {
        f"{method} {path}": sorted(
            role for role in ROLES if declared <= BUILTIN_ROLES[role]
        )
        for method, path, _endpoint, declared in gates()
    }
    assert after == BEFORE


def test_admin_was_refused_nothing_before_and_is_refused_nothing_now():
    assert all("admin" in allowed for allowed in BEFORE.values())
    admin = BUILTIN_ROLES["admin"]
    assert all(declared <= admin for _m, _p, _e, declared in gates())


def test_the_snapshot_covers_the_whole_app_and_is_not_vacuous():
    # A matrix that had quietly lost its rows would compare equal to itself.
    assert len(BEFORE) == len(gates()) > 75
    denied_to_viewer = [route for route, roles in BEFORE.items() if "viewer" not in roles]
    assert len(denied_to_viewer) > 30


@pytest.mark.parametrize("groups,role", [("readers", "viewer"), ("ops", "operator")])
async def test_a_built_in_role_is_refused_exactly_where_it_was_before(groups, role):
    """The structural matrix above, proven through the real middleware on the
    routes each role must NOT reach. Denial is checked live because it is the
    half that has to happen before the handler runs — the grants are the other
    1600 tests, which sign in as these same roles."""
    app = make_app(stub({}))
    refused = [
        route
        for route, allowed in BEFORE.items()
        if role not in allowed and route.startswith("POST ")
    ]
    assert refused, role
    async with client(app, groups) as web:
        page = await web.get("/drafts")  # mints the CSRF cookie a browser holds
        assert page.status_code == 200
        for route in refused:
            path = route.removeprefix("POST ").replace("{", "").replace("}", "")
            response = await post(web, path)
            assert response.status_code == 403, f"{role} was not refused {route}"


# --- 2. the coverage rule -------------------------------------------------------------


def test_every_mutating_route_declares_a_cataloged_permission():
    ungated = [
        f"{method} {path}"
        for method, path, _endpoint, declared in gates()
        if method in MUTATING and not declared and f"{method} {path}" not in UNGATED
    ]
    assert not ungated, (
        "these routes change state with no permission — add `Depends(require(...))` "
        f"with a name from app/permissions.py: {ungated}"
    )


def test_every_route_that_declares_something_declares_a_real_permission():
    unknown = {
        name
        for _m, _p, _e, declared in gates()
        for name in declared
        if name not in PERMISSIONS
    }
    assert not unknown


def test_reads_are_the_view_family_and_the_two_named_exceptions():
    """The documented rule for GETs: `console.view`, unless the page IS the
    action (a contact's edit form, a batch's dispatch confirmation) or the read
    leaves the screen (`export.csv`)."""
    exceptions = {"export.csv", "contact.edit", "batch.dispatch"}
    for method, path, _endpoint, declared in gates():
        if method != "GET" or f"{method} {path}" in UNGATED:
            continue
        assert declared <= {VIEW} | exceptions, f"GET {path} declares {sorted(declared)}"


def test_no_route_module_still_speaks_in_roles():
    """The migration is complete: role names live in the auth layer and the
    catalog, nowhere else."""
    offenders = []
    for path in sorted((ROOT / "app" / "web").glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r'''(require|can)\(\s*["'](viewer|operator|admin)["']''', line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders


# --- 3. the catalog itself ------------------------------------------------------------


def test_every_permission_is_enforced_somewhere():
    """A name nobody checks is a promise to a client that nothing keeps."""
    enforced = {name for _m, _p, _e, declared in gates() for name in declared}
    # `onboarding.access_any` gates a RECORD, not a URL: whether a draft that
    # belongs to another operator opens at all (app/web/onboarding.py::_draft).
    assert set(PERMISSIONS) - enforced == {"onboarding.access_any"}
    assert "onboarding.access_any" in (ROOT / "app/web/onboarding.py").read_text()


def test_the_builtin_bundles_nest_and_admin_holds_the_whole_catalog():
    assert BUILTIN_ROLES["viewer"] < BUILTIN_ROLES["operator"] < BUILTIN_ROLES["admin"]
    assert BUILTIN_ROLES["admin"] == set(PERMISSIONS)
    assert ADMIN_ONLY and not (ADMIN_ONLY & OPERATOR)


def test_every_action_requires_the_read_it_is_rendered_on():
    assert set(REQUIRES) == set(PERMISSIONS) - {VIEW}
    assert all(needs == {VIEW} for needs in REQUIRES.values())


def test_a_permission_name_is_a_noun_and_a_verb():
    for name in PERMISSIONS:
        assert re.fullmatch(r"[a-z]+\.[a-z_]+", name), name


# --- 4. custom roles: the happy path, end to end --------------------------------------


def settings_with(tmp_path, roles: dict, **kwargs) -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(roles))
    return Settings(
        **{
            "auth_mode": "proxy",
            "proxy_shared_secret": PROXY_SECRET,
            "roles_file": str(path),
            **kwargs,
        }
    )


async def test_a_custom_role_reaches_a_route_through_the_existing_group_map(tmp_path):
    """Config → role → AUTH_ROLE_MAP → the route's own gate, with nothing about
    the mapping changed: a custom name travels exactly as `operator` does."""
    settings = settings_with(
        tmp_path,
        {"payments-clerk": [VIEW, "batch.upload"]},
        auth_role_map="finance=payments-clerk",
    )
    app = make_app(stub({}))
    app.state.auth_provider = ProxyProvider(settings)

    async with client(app, "finance") as web:
        assert (await web.get("/drafts")).status_code == 200  # console.view
        batch = "/customers/cus_1/batches/00000000-0000-0000-0000-000000000000"
        # Held: the gate lets it through, and the handler answers on its own terms.
        assert (await post(web, f"{batch}/abandon")).status_code != 403
        # Not held: dispatch is a separate permission, and it is refused.
        assert (await post(web, f"{batch}/dispatch")).status_code == 403
        assert (await web.get(f"{batch}/confirm")).status_code == 403


async def test_an_unmapped_group_is_still_403_with_a_role_file_present(tmp_path):
    settings = settings_with(tmp_path, {"payments-clerk": [VIEW, "batch.upload"]})
    app = make_app(stub({}))
    app.state.auth_provider = ProxyProvider(settings)
    async with client(app, "some-other-group") as web:
        assert (await web.get("/drafts")).status_code == 403


def test_a_custom_role_resolves_to_exactly_what_the_file_says(tmp_path):
    settings = settings_with(tmp_path, {"reporting": [VIEW, "export.csv"]})
    table = settings.roles
    assert table["reporting"] == {VIEW, "export.csv"}
    assert table["operator"] == BUILTIN_ROLES["operator"]
    assert permissions_for({"reporting"}, table) == {VIEW, "export.csv"}


# --- 5. the startup invariants --------------------------------------------------------


def refusal(tmp_path, roles: dict, **kwargs) -> str:
    with pytest.raises(RuntimeError) as raised:
        settings_with(tmp_path, roles, **kwargs)
    return str(raised.value)


def test_an_unknown_permission_names_the_key_and_the_nearest_real_name(tmp_path):
    message = refusal(tmp_path, {"clerk": [VIEW, "payout.send"]})
    assert "ROLES_FILE" in message and "'clerk'" in message
    assert "'payout.send'" in message and "did you mean 'payout.create'" in message
    assert "PERMISSIONS.md" in message


def test_an_unknown_permission_with_no_near_match_says_so(tmp_path):
    message = refusal(tmp_path, {"clerk": [VIEW, "teleport.everything"]})
    assert "no permission has that name" in message


def test_a_write_without_its_read_is_refused(tmp_path):
    message = refusal(tmp_path, {"clerk": ["payout.create"]})
    assert "'clerk'" in message and "'payout.create'" in message
    assert "'console.view'" in message and "requires" in message


def test_an_admin_class_permission_needs_the_operator_base(tmp_path):
    message = refusal(tmp_path, {"cleaner": [VIEW, "contact.delete"]})
    assert "admin-class permission 'contact.delete'" in message
    assert "operator base" in message and "missing operator permissions" in message


def test_sandbox_simulate_is_admin_class_off_the_sandbox_host(tmp_path):
    roles = {"tester": [VIEW, "sandbox.simulate"]}
    # Sandbox: an ordinary operator-tier permission.
    assert settings_with(tmp_path / "ok", roles).roles["tester"]
    message = refusal(
        tmp_path / "prod",
        roles,
        conduit_env="staging",
        session_secret="staging-session-value-0123456789abcd",
        proxy_shared_secret="staging-proxy-value-0123456789abcdef",
    )
    assert "admin-class permission 'sandbox.simulate'" in message


def test_an_empty_role_is_refused(tmp_path):
    message = refusal(tmp_path, {"ghost": []})
    assert "'ghost'" in message and "grants nothing" in message


@pytest.mark.parametrize("name", ["viewer", "operator", "admin"])
def test_a_builtin_role_cannot_be_redefined_or_shadowed(tmp_path, name):
    message = refusal(tmp_path, {name: [VIEW]})
    assert f"defines '{name}'" in message and "built-in role" in message
    # And the built-in still means what it meant.
    assert BUILTIN_ROLES[name] == role_table("", "sandbox")[name]


def test_a_malformed_file_names_the_setting_not_the_contents(tmp_path):
    path = tmp_path / "roles.json"
    path.write_text('{"clerk": ["console.view",]}')
    with pytest.raises(RuntimeError) as raised:
        Settings(auth_mode="proxy", proxy_shared_secret=PROXY_SECRET, roles_file=str(path))
    assert "ROLES_FILE is not valid JSON" in str(raised.value)


def test_a_role_defined_twice_is_refused(tmp_path):
    """The file is reviewed top to bottom; JSON's last-wins would let the
    definition a reviewer reads differ from the one that takes effect."""
    path = tmp_path / "roles.json"
    path.write_text('{"clerk": ["console.view"], "clerk": ["console.view", "payout.create"]}')
    with pytest.raises(RuntimeError) as raised:
        Settings(auth_mode="proxy", proxy_shared_secret=PROXY_SECRET, roles_file=str(path))
    assert "defines the role 'clerk' twice" in str(raised.value)


def test_a_missing_file_is_refused_at_startup(tmp_path):
    with pytest.raises(RuntimeError) as raised:
        Settings(
            auth_mode="proxy",
            proxy_shared_secret=PROXY_SECRET,
            roles_file=str(tmp_path / "nope.json"),
        )
    assert "ROLES_FILE could not be read" in str(raised.value)


def test_a_role_must_be_a_list_of_names(tmp_path):
    assert "must be a list of permission names" in refusal(tmp_path, {"clerk": VIEW})
    assert "must be a JSON object" in str(
        pytest.raises(RuntimeError, parse, "[]", env="sandbox").value
    )


def test_no_roles_file_means_the_three_builtins_and_nothing_else():
    assert role_table("", "sandbox") == BUILTIN_ROLES


# --- 6. the generated doc -------------------------------------------------------------


def test_permissions_md_matches_the_catalog_and_the_routes():
    assert OUTPUT.read_text(encoding="utf-8") == generate(), (
        "PERMISSIONS.md is stale — run `python scripts/generate_permission_matrix.py`. "
        "The client-facing grid is generated so it cannot drift from enforcement."
    )


def test_the_generated_doc_describes_the_copy_pattern_rather_than_a_known_limitation():
    text = OUTPUT.read_text(encoding="utf-8")
    assert "Known limitation" not in text and "operator role" not in text
    assert "No sentence names a role." in text


def test_the_generated_doc_carries_the_contract_warning_and_every_name():
    text = OUTPUT.read_text(encoding="utf-8")
    assert "These names are contract" in text
    for name in PERMISSIONS:
        assert f"`{name}`" in text
    for route in UNGATED:
        method, path = route.split(" ", 1)
        assert path in text, f"{route} is exempt but the doc does not say why"


# --- 7. the operator-facing copy ------------------------------------------------------

# Every surface an operator reads: the templates and the one script that puts
# sentences on screen (the product tour).
COPY = sorted((ROOT / "app" / "web" / "templates").rglob("*.html")) + [ROOT / "static" / "app.js"]

# The only `noun.verb` in mono on a page that is not a permission: discovery's
# own flag on a payout route, quoted as Conduit's word.
NOT_A_PERMISSION = {"documentation.required"}


def test_every_permission_named_in_copy_is_a_real_permission():
    """A gating sentence that names a permission nobody can be granted is worse
    than the vague sentence it replaced: it sends an operator to their admin
    asking for a string that does not exist."""
    named = {
        (path.name, name)
        for path in COPY
        for name in re.findall(r"<code>([a-z]+\.[a-z_]+)</code>", path.read_text())
        if name not in NOT_A_PERMISSION
    }
    assert named, "the sweep put permission names in copy — this must not go vacuous"
    assert not {row for row in named if row[1] not in PERMISSIONS}


def test_no_gating_copy_explains_itself_with_a_role():
    """The rule: gating copy names the capability and
    then the permission, never who holds it. Which role carries `payout.create`
    is the deployment's decision — a console that guesses at it is describing
    someone else's org chart, and a custom role holding the permission without
    the name would be told, falsely, that it cannot act."""
    banned = re.compile(
        r"operator role|admin role|viewer role|ask an admin|needs admin\b|viewers can", re.I
    )
    offenders = [path.name for path in COPY if banned.search(path.read_text())]
    assert not offenders


# --- the file picker follows the documents route's own gate --------------------------
#
# Every `data-upload` input posts to POST /documents, whose gate is
# `document.upload` — not the permission of the form around it. A custom role
# holding the form's action without the upload must get the stated note, never a
# picker that 403s. `forbidden_affordances` cannot see this control (its target
# lives in app.js, not an hx attribute), so it is pinned here directly.

import httpx  # noqa: E402

from tests.payments_fixtures import (  # noqa: E402
    CID,
    FEDWIRE_BUSINESS,
    PENDING,
    REGISTERED,
    USD_ACCOUNT,
    WHITELIST_PATH,
    page,
)
from tests.web_harness import signed_in_as  # noqa: E402

PAYOUT_FORM = (
    f"/customers/{CID}/payouts/new"
    "?purpose=payment_for_goods_or_services&rail=fedwire&recipientType=business"
    "&destinationCountry=USA"
)
PAYOUT_STUBS = {
    ("GET", "/v2/payouts/requirements"): httpx.Response(200, json=FEDWIRE_BUSINESS),
    ("GET", f"/v2/customers/{CID}/virtual-accounts"): page([USD_ACCOUNT]),
    ("GET", WHITELIST_PATH): page([REGISTERED, PENDING]),
}


async def test_the_file_picker_hides_without_document_upload():
    """The macro path (`m.document_widget`), which serves payouts, transfers and
    batch detail at once."""
    app = make_app(stub(PAYOUT_STUBS))
    async with signed_in_as(app, "payout.create") as web:
        html = (await web.get(PAYOUT_FORM)).text
        assert "data-upload" not in html, "picker painted for a role whose upload 403s"
        assert "document.upload" in html, "the withheld picker must state its gate"
    async with signed_in_as(app, "payout.create", "document.upload") as web:
        assert "data-upload" in (await web.get(PAYOUT_FORM)).text


async def test_the_inline_whitelist_picker_follows_the_same_gate():
    """One of the five inline (non-macro) sites: the whitelist registration form
    requires evidence documents, so without `document.upload` the register flow
    states the missing permission instead of offering a dead control."""
    app = make_app(stub(PAYOUT_STUBS))
    url = f"/customers/{CID}/recipients"
    async with signed_in_as(app, "whitelist.register") as web:
        html = (await web.get(url)).text
        assert "data-upload" not in html
        assert "document.upload" in html
    async with signed_in_as(app, "whitelist.register", "document.upload") as web:
        assert "data-upload" in (await web.get(url)).text
