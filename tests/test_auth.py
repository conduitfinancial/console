"""Proxy/disabled adapters, CSRF, role guards, sessions (plan v2 §2, §9.6).

Everything goes through the real middleware on a real ASGI app: an assertion
here is a statement about what the deployed app does, not about a helper.
"""

from __future__ import annotations

import inspect
import logging

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import select

from app.auth import Actor, DisabledProvider, ProxyProvider, install_auth
from app.auth.tokens import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    cookie_name,
    session_value,
    sign,
)
from app.auth.web import csrf_hx_headers, csrf_token, current_actor, require
from app.permissions import BUILTIN_ROLES, permissions_for
from app.config import Settings
from app.models import AuditEvent

SECRET = "shared-proxy-secret"
SESSION_SECRET = "test-session-secret"  # conftest puts this in the environment

PROXY = Settings(
    auth_mode="proxy",
    proxy_shared_secret=SECRET,
    auth_role_map="conduit-ops=operator,conduit-admins=admin",
)
DISABLED = Settings(auth_mode="disabled")

# The names the browser sees: __Host- wherever Secure is on.
CSRF = cookie_name(CSRF_COOKIE, secure=PROXY.cookie_secure)
SESSION = cookie_name(SESSION_COOKIE, secure=PROXY.cookie_secure)


def make_app(provider) -> FastAPI:
    app = FastAPI()

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    async def whoami(request: Request, actor: Actor = Depends(current_actor)) -> dict:
        return {
            "id": actor.id,
            "email": actor.email,
            "name": actor.display_name,
            "roles": sorted(actor.roles),
            "csrf": csrf_token(request),
            "hx": csrf_hx_headers(request),
        }

    @app.get("/read", dependencies=[Depends(require("console.view"))])
    async def read() -> dict:
        return {"ok": True}

    @app.post("/act", dependencies=[Depends(require("payout.create"))])
    async def act() -> dict:
        return {"ok": True}

    @app.post("/configure", dependencies=[Depends(require("operation.abandon"))])
    async def configure() -> dict:
        return {"ok": True}

    install_auth(app, provider=provider, settings=PROXY)
    return app


def client(app: FastAPI, **headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",  # Secure cookies are not stored over http
        headers=headers,
    )


def proxy_headers(*, secret=SECRET, user="ops@example.com", groups="conduit-ops") -> dict:
    headers = {}
    if secret is not None:
        headers["X-Proxy-Auth"] = secret
    if user is not None:
        headers["X-Auth-Request-User"] = user
        headers["X-Auth-Request-Email"] = user
    if groups is not None:
        headers["X-Auth-Request-Groups"] = groups
    return headers


async def authenticated(app: FastAPI, **headers) -> tuple[httpx.AsyncClient, str]:
    """A client holding a CSRF cookie, plus the token to echo — what a browser
    has after loading one page."""
    c = client(app, **headers)
    response = await c.get("/whoami")
    assert response.status_code == 200, response.text
    return c, response.json()["csrf"]


# --- proxy adapter: the spoofing matrix -------------------------------------------


@pytest.mark.parametrize(
    "secret,user,expected",
    [
        (SECRET, "ops@example.com", 200),  # the only accepted combination
        (SECRET, None, 401),  # secret, no identity headers
        ("wrong-secret", "ops@example.com", 401),  # wrong secret
        (None, "ops@example.com", 401),  # spoofed identity, no secret at all
        (None, None, 401),
        ("", "ops@example.com", 401),  # empty header must not match anything
    ],
)
async def test_proxy_header_matrix(secret, user, expected):
    async with client(make_app(ProxyProvider(PROXY))) as c:
        response = await c.get("/whoami", headers=proxy_headers(secret=secret, user=user))
    assert response.status_code == expected


async def test_unconfigured_proxy_secret_rejects_everything():
    """Fail closed: an empty configured secret must not match an empty header."""
    provider = ProxyProvider(Settings(auth_mode="disabled", proxy_shared_secret=""))
    async with client(make_app(provider)) as c:
        for headers in (proxy_headers(secret=""), proxy_headers(secret=None), proxy_headers()):
            assert (await c.get("/whoami", headers=headers)).status_code == 401


def test_proxy_secret_compare_is_constant_time():
    source = inspect.getsource(ProxyProvider.authenticate)
    assert "compare_digest" in source
    assert "==" not in source.split("provided")[1].split("\n")[0]


async def test_proxy_maps_groups_to_roles():
    async with client(make_app(ProxyProvider(PROXY))) as c:
        body = (
            await c.get("/whoami", headers=proxy_headers(groups="conduit-admins,other"))
        ).json()
    assert body["roles"] == ["admin"]
    assert body["id"] == body["email"] == "ops@example.com"


async def test_unknown_group_maps_to_no_role_and_403(session):
    async with client(make_app(ProxyProvider(PROXY))) as c:
        response = await c.get("/whoami", headers=proxy_headers(groups="marketing"))
    assert response.status_code == 403  # not 401, and certainly not 500
    # A3: the refusal is the console's 403 page now, inside the chrome, and it
    # says what the reader can act on ("An administrator can grant it") rather
    # than the middleware's own sentence. Still a 403, still not a login loop.
    assert "You do not hold this action" in response.text
    assert "An administrator can grant it" in response.text
    denied = await session.execute(
        select(AuditEvent.actor_id).where(AuditEvent.action == "auth.denied")
    )
    assert denied.scalars().all() == ["ops@example.com"]


def test_denied_page_escapes_its_reason():
    """No caller passes untrusted input today, but the
    escape belongs on the sink, not on trusting every future caller."""
    from app.auth.web import _denied

    response = _denied("<script>alert(1)</script>")
    assert "<script>" not in response.body.decode()
    assert "&lt;script&gt;" in response.body.decode()


async def test_headers_are_never_logged():
    # A local handler, not caplog: alembic's fileConfig (run by conftest's
    # migration fixture) replaces the root handlers, pytest's included.
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("app.auth.providers")
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        async with client(make_app(ProxyProvider(PROXY))) as c:
            await c.get("/whoami", headers=proxy_headers(secret="wrong-secret"))
            await c.get("/whoami", headers=proxy_headers(secret=SECRET, groups=None))
    finally:
        logger.removeHandler(handler)

    logged = "\n".join(r.getMessage() for r in records)
    assert "rejected" in logged  # it did report both refusals
    for leak in ("wrong-secret", SECRET, "ops@example.com"):
        assert leak not in logged


# --- disabled adapter ---------------------------------------------------------------


async def test_disabled_mode_yields_a_dev_actor_with_every_role():
    async with client(make_app(DisabledProvider(DISABLED))) as c:
        body = (await c.get("/whoami")).json()
    assert body["id"] == "dev"
    assert body["roles"] == ["admin", "operator", "viewer"]


# --- health stays open ---------------------------------------------------------------


@pytest.mark.parametrize("provider", [ProxyProvider(PROXY), DisabledProvider(DISABLED)])
async def test_health_needs_no_credentials(provider):
    async with client(make_app(provider)) as c:
        response = await c.get("/health/live")
    assert response.status_code == 200


# --- csrf ------------------------------------------------------------------------------


async def test_csrf_token_is_issued_and_hx_headers_render():
    app = make_app(ProxyProvider(PROXY))
    async with client(app, **proxy_headers()) as c:
        body = (await c.get("/whoami")).json()
        assert body["csrf"]
        assert body["hx"] == '{"X-CSRF-Token": "%s"}' % body["csrf"]
        assert c.cookies[CSRF] == body["csrf"]
        # A second request reuses the token rather than rotating it per response.
        assert (await c.get("/whoami")).json()["csrf"] == body["csrf"]


async def test_mutation_without_a_token_is_403():
    app = make_app(ProxyProvider(PROXY))
    async with client(app, **proxy_headers()) as c:
        assert (await c.post("/act")).status_code == 403


async def test_mutation_with_a_wrong_token_is_403():
    app = make_app(ProxyProvider(PROXY))
    c, token = await authenticated(app, **proxy_headers())
    forged = sign({"csrf": "forged"}, secret="not-the-session-secret", ttl=600)
    for wrong in (token[:-4] + "aaaa", forged, "garbage"):
        response = await c.post("/act", headers={CSRF_HEADER: wrong})
        assert response.status_code == 403, wrong
    await c.aclose()


async def test_mutation_with_a_valid_token_passes():
    app = make_app(ProxyProvider(PROXY))
    c, token = await authenticated(app, **proxy_headers())
    assert (await c.post("/act", headers={CSRF_HEADER: token})).status_code == 200
    await c.aclose()


async def test_another_operators_token_is_refused():
    """The token is bound to who it was issued to, so a colleague's valid token
    cannot be planted on this operator's request."""
    app = make_app(ProxyProvider(PROXY))
    other, other_token = await authenticated(app, **proxy_headers(user="someone@example.com"))
    await other.aclose()

    c, _ = await authenticated(app, **proxy_headers())
    assert (await c.post("/act", headers={CSRF_HEADER: other_token})).status_code == 403
    c.cookies.set(CSRF, other_token, domain="test")  # cookie and echo agree…
    assert (await c.post("/act", headers={CSRF_HEADER: other_token})).status_code == 403
    await c.aclose()


async def test_csrf_is_checked_before_identity():
    """A cross-site POST is refused whether or not the browser had a session."""
    app = make_app(ProxyProvider(PROXY))
    async with client(app) as c:
        assert (await c.post("/act")).status_code == 403


async def test_a_non_ascii_csrf_cookie_is_403_not_500():
    """`csrf_ok` used to feed the cookie straight into `compare_digest`; a cookie
    value outside ASCII raised TypeError there instead of just failing the
    check, turning a garbled or forged cookie into a 500 for an anonymous
    caller on a route that never even reaches identity resolution.

    httpx's own `cookies.set()` refuses to encode a non-ASCII value onto the
    request at all, so the header is built by hand here — bytes, past the
    layer that would otherwise reject it — the way a real attacker's raw
    socket would."""
    app = make_app(ProxyProvider(PROXY))
    async with client(app, **proxy_headers()) as c:
        response = await c.post(
            "/act",
            headers={
                CSRF_HEADER: "whatever",
                b"Cookie": f"{CSRF}=caf\xe9".encode("latin-1"),
            },
        )
    assert response.status_code == 403


async def test_an_ascii_but_wrong_csrf_cookie_is_still_403():
    """Control for the fix above: adding the ASCII guard must not disturb the
    ordinary wrong-cookie path, which was already a clean 403. Same hand-built
    header as the non-ASCII case above, so the two differ only in the one byte
    that matters."""
    app = make_app(ProxyProvider(PROXY))
    async with client(app, **proxy_headers()) as c:
        response = await c.post(
            "/act",
            headers={
                CSRF_HEADER: "whatever",
                b"Cookie": f"{CSRF}=wrong-cookie-value".encode("latin-1"),
            },
        )
    assert response.status_code == 403


async def test_get_is_never_treated_as_mutating():
    app = make_app(ProxyProvider(PROXY))
    async with client(app, **proxy_headers()) as c:
        assert (await c.get("/read")).status_code == 200  # no token held yet


# --- role guards ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "groups,read,act,configure",
    [
        ("viewer", 200, 403, 403),
        ("conduit-ops", 200, 200, 403),
        ("conduit-admins", 200, 200, 200),
    ],
)
async def test_role_matrix_proxy(groups, read, act, configure):
    app = make_app(ProxyProvider(PROXY))
    c, token = await authenticated(app, **proxy_headers(groups=groups))
    assert (await c.get("/read")).status_code == read
    assert (await c.post("/act", headers={CSRF_HEADER: token})).status_code == act
    assert (await c.post("/configure", headers={CSRF_HEADER: token})).status_code == configure
    await c.aclose()


async def test_role_matrix_disabled_mode_can_do_everything():
    app = make_app(DisabledProvider(DISABLED))
    c, token = await authenticated(app)
    assert (await c.get("/read")).status_code == 200
    assert (await c.post("/configure", headers={CSRF_HEADER: token})).status_code == 200
    await c.aclose()


def test_the_builtin_bundles_nest_one_way():
    """viewer ⊂ operator ⊂ admin, and an actor with no role holds nothing."""
    assert BUILTIN_ROLES["viewer"] < BUILTIN_ROLES["operator"] < BUILTIN_ROLES["admin"]
    viewer = Actor("v", "v@x", "V", frozenset({"viewer"}),
                   permissions_for({"viewer"}, BUILTIN_ROLES))
    admin = Actor("a", "a@x", "A", frozenset({"admin"}),
                  permissions_for({"admin"}, BUILTIN_ROLES))
    assert admin.can("console.view") and admin.can("payout.create") and admin.can("operation.abandon")
    assert viewer.can("console.view")
    assert not viewer.can("payout.create") and not viewer.can("operation.abandon")
    assert not Actor("n", "n@x", "N").can("console.view")


# --- sessions ---------------------------------------------------------------------------


def test_session_value_round_trips():
    from app.auth.tokens import session_actor

    actor = Actor("u1", "u1@x", "One", frozenset({"operator"}),
                  permissions_for({"operator"}, BUILTIN_ROLES))
    assert session_actor(
        session_value(actor, secret=SESSION_SECRET, ttl=60),
        secret=SESSION_SECRET,
        roles=BUILTIN_ROLES,
    ) == actor


@pytest.mark.parametrize(
    "cookie",
    [
        "garbage",
        sign({"id": "x", "email": "x@x", "name": "X", "roles": ["admin"]},
             secret="another-secret", ttl=600),  # right shape, wrong signer
        sign({"id": "x", "email": "x@x", "name": "X", "roles": ["admin"]},
             secret=SESSION_SECRET, ttl=-10),  # ours, but expired
    ],
)
def test_tampered_or_expired_session_is_not_an_actor(cookie):
    from app.auth.tokens import session_actor

    assert session_actor(cookie, secret=SESSION_SECRET, roles=BUILTIN_ROLES) is None


def test_flipping_a_payload_bit_invalidates_the_signature():
    from app.auth.tokens import b64d, b64e, session_actor

    viewer = Actor("u", "u@x", "U", frozenset({"viewer"}))
    good = session_value(viewer, secret=SESSION_SECRET, ttl=60)
    body, signature = good.split(".")
    escalated = b64e(b64d(body).replace(b'"viewer"', b'"admin"'))
    assert session_actor(
        f"{escalated}.{signature}", secret=SESSION_SECRET, roles=BUILTIN_ROLES
    ) is None


# --- logout -----------------------------------------------------------------------------


async def test_logout_needs_csrf_then_clears_the_session(session):
    app = make_app(ProxyProvider(PROXY))
    c, token = await authenticated(app, **proxy_headers())
    c.cookies.set(SESSION, "whatever", domain="test")
    assert (await c.post("/auth/logout")).status_code == 403  # no token
    response = await c.post("/auth/logout", headers={CSRF_HEADER: token})
    await c.aclose()
    assert response.status_code == 303
    cleared = response.headers.get_list("set-cookie")

    # BOTH cookies, and each deletion must carry the attributes the cookie was
    # SET with. `Response.delete_cookie` defaults to `secure=False,
    # httponly=False`, and with `COOKIE_SECURE=true` these carry the `__Host-`
    # prefix — which a browser REJECTS without `Secure`. So a substring match on
    # `console_session=""` is not a pin: it passes on a header no browser will
    # honour, which is exactly what shipped until a review caught it.
    for name in (SESSION, CSRF):
        header = next((h for h in cleared if h.startswith(f"{name}=")), None)
        assert header is not None, f"{name} was not cleared: {cleared}"
        assert header.startswith(f'{name}=""'), header
        assert "Secure" in header, header
        assert "HttpOnly" in header, header
        # The rest of the `__Host-` contract, which the prefix makes mandatory.
        assert "Path=/" in header and "Domain" not in header, header

    actions = (await session.execute(select(AuditEvent.action))).scalars().all()
    assert "auth.logout" in actions
