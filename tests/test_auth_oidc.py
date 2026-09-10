"""OIDC login against a stubbed issuer (plan v2 §2, §9.6). No live IdP, no network.

The stub serves discovery, JWKS and the token endpoint over httpx.MockTransport;
ID tokens are signed here with a throwaway RSA key, so every negative case (bad
signature, wrong aud, expired, alg=none, unknown kid) is a real token that fails
for exactly one reason.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from fastapi import Depends, FastAPI
from sqlalchemy import select

from app.auth import Actor, OIDCProvider, install_auth
from app.auth.oidc import OIDCClient, challenge
from app.auth.revocation import revoke
from scripts.revoke_sessions import clean_sub
from app.auth.tokens import (
    CSRF_HEADER,
    LOGIN_COOKIE,
    SESSION_COOKIE,
    b64e,
    cookie_name,
    sign,
    unsign,
)
from app.auth.web import current_actor, require
from app.config import Settings
from app.models import AuditEvent

ISSUER = "https://idp.example.test"
CLIENT_ID = "conduit-console"
SESSION_SECRET = "test-session-secret"  # conftest puts this in the environment

SETTINGS = Settings(
    auth_mode="oidc",
    oidc_issuer=ISSUER,
    oidc_client_id=CLIENT_ID,
    oidc_client_secret="idp-client-secret",
    oidc_role_claim="console_roles",
    auth_role_map="ops=operator",
)

# What the browser actually receives: __Host- is applied wherever Secure is on.
LOGIN = cookie_name(LOGIN_COOKIE, secure=SETTINGS.cookie_secure)
SESSION = cookie_name(SESSION_COOKIE, secure=SETTINGS.cookie_secure)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64uint(value: int) -> str:
    return b64e(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwk(kid: str = "k1") -> dict:
    numbers = KEY.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64uint(numbers.n),
        "e": _b64uint(numbers.e),
    }


def id_token(claims: dict, *, key=KEY, kid: str = "k1", alg: str = "RS256") -> str:
    header = b64e(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = b64e(json.dumps(claims).encode())
    if alg == "none":
        return f"{header}.{payload}."
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64e(signature)}"


def claims(**overrides) -> dict:
    return {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "idp-user-1",
        "email": "ops@example.com",
        "name": "Ops Person",
        "console_roles": ["ops"],
        "exp": int(time.time()) + 300,
        **overrides,
    }


class StubIssuer:
    """Discovery + JWKS + token endpoint. `self.token` is what /token hands back."""

    def __init__(self) -> None:
        self.token = ""
        self.keys = [jwk()]
        self.calls: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            path = request.url.path
            if path == "/.well-known/openid-configuration":
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/jwks",
                    },
                )
            if path == "/jwks":
                return httpx.Response(200, json={"keys": self.keys})
            if path == "/token":
                form = dict(httpx.QueryParams(request.content.decode()))
                if not form.get("code_verifier"):  # a PKCE-requiring IdP
                    return httpx.Response(400, json={"error": "invalid_request"})
                return httpx.Response(200, json={"id_token": self.token, "token_type": "Bearer"})
            return httpx.Response(404)  # pragma: no cover

        return httpx.MockTransport(handler)


def make_app(issuer: StubIssuer, *, now=time.time) -> FastAPI:
    provider = OIDCProvider(
        SETTINGS, client=OIDCClient(SETTINGS, transport=issuer.transport(), now=now)
    )
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(actor: Actor = Depends(current_actor)) -> dict:
        return {"id": actor.id, "email": actor.email, "roles": sorted(actor.roles)}

    @app.post("/act", dependencies=[Depends(require("payout.create"))])
    async def act() -> dict:
        return {"ok": True}

    install_auth(app, provider=provider, settings=SETTINGS)
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://console.test"
    )


async def begin_login(c: httpx.AsyncClient) -> dict:
    """GET /auth/login, returning the state/nonce/verifier the app committed to."""
    response = await c.get("/auth/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    pending = unsign(c.cookies[LOGIN], secret=SESSION_SECRET)
    assert pending is not None
    # PKCE: the challenge on the wire is S256 of the verifier we kept.
    query = httpx.URL(location).params
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == challenge(pending["verifier"])
    return pending


async def login_with(issuer: StubIssuer, token_for, *, now=time.time):
    """Run the full ceremony; `token_for(nonce)` builds the ID token."""
    app = make_app(issuer, now=now)
    c = client(app)
    pending = await begin_login(c)
    issuer.token = token_for(pending["nonce"])
    return c, await c.get(f"/auth/callback?code=abc123&state={pending['state']}"), pending


# --- happy path -----------------------------------------------------------------------


async def test_login_callback_sets_a_session_and_authenticates(session):
    issuer = StubIssuer()
    c, response, pending = await login_with(issuer, lambda nonce: id_token(claims(nonce=nonce)))
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION in c.cookies
    assert LOGIN not in c.cookies  # single-use

    who = await c.get("/whoami")
    assert who.status_code == 200
    assert who.json() == {"id": "idp-user-1", "email": "ops@example.com", "roles": ["operator"]}
    await c.aclose()

    posted = [r for r in issuer.calls if r.url.path == "/token"]
    assert posted and b"grant_type=authorization_code" in posted[0].content
    # PKCE end to end: the code was redeemed with the verifier behind the challenge.
    assert f"code_verifier={pending['verifier']}".encode() in posted[0].content
    logged = (await session.execute(select(AuditEvent.action, AuditEvent.actor_id))).all()
    assert ("auth.login", "idp-user-1") in logged


async def test_authorization_url_carries_state_nonce_and_callback():
    async with client(make_app(StubIssuer())) as c:
        location = (await c.get("/auth/login")).headers["location"]
    query = httpx.URL(location).params
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == "https://console.test/auth/callback"
    assert query["state"] and query["nonce"]


async def test_configured_redirect_uri_is_used_verbatim():
    """Behind a TLS-terminating proxy the derived base_url is http://; the
    configured value is what both the redirect and the exchange must carry."""
    configured = "https://console.example.com/auth/callback"
    settings = SETTINGS.model_copy(update={"oidc_redirect_uri": configured})
    issuer = StubIssuer()
    app = FastAPI()
    install_auth(
        app,
        provider=OIDCProvider(
            settings, client=OIDCClient(settings, transport=issuer.transport())
        ),
        settings=settings,
    )
    async with client(app) as c:
        location = (await c.get("/auth/login")).headers["location"]
        assert httpx.URL(location).params["redirect_uri"] == configured
        pending = unsign(c.cookies[LOGIN], secret=SESSION_SECRET)
        issuer.token = id_token(claims(nonce=pending["nonce"]))
        response = await c.get(f"/auth/callback?code=x&state={pending['state']}")
    assert response.status_code == 303
    posted = next(r for r in issuer.calls if r.url.path == "/token")
    assert dict(httpx.QueryParams(posted.content.decode()))["redirect_uri"] == configured


async def test_session_survives_a_mutating_request_with_csrf():
    issuer = StubIssuer()
    c, _, _pending = await login_with(issuer, lambda nonce: id_token(claims(nonce=nonce)))
    await c.get("/whoami")  # warms the CSRF cookie, as loading a page would
    csrf = c.cookies[cookie_name("console_csrf", secure=SETTINGS.cookie_secure)]
    assert (await c.post("/act", headers={CSRF_HEADER: csrf})).status_code == 200
    await c.aclose()


# --- negative paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,token_for",
    [
        ("bad signature", lambda nonce: id_token(claims(nonce=nonce), key=OTHER_KEY)),
        ("wrong aud", lambda nonce: id_token(claims(nonce=nonce, aud="another-client"))),
        ("wrong issuer", lambda nonce: id_token(claims(nonce=nonce, iss="https://evil.test"))),
        ("expired", lambda nonce: id_token(claims(nonce=nonce, exp=int(time.time()) - 3600))),
        ("alg none", lambda nonce: id_token(claims(nonce=nonce), alg="none")),
        ("unknown kid", lambda nonce: id_token(claims(nonce=nonce), kid="rotated")),
        ("replayed nonce", lambda nonce: id_token(claims(nonce="someone-elses-nonce"))),
        ("no nonce", lambda nonce: id_token(claims())),
        ("not a jwt", lambda nonce: "not.a.jwt"),
        (
            "azp names another client",
            lambda nonce: id_token(
                claims(nonce=nonce, aud=[CLIENT_ID, "another-client"], azp="another-client")
            ),
        ),
        (
            "multiple audiences with no azp",
            lambda nonce: id_token(claims(nonce=nonce, aud=[CLIENT_ID, "another-client"])),
        ),
        ("no sub", lambda nonce: id_token(claims(nonce=nonce, sub=None))),
    ],
)
async def test_bad_id_tokens_are_refused(name, token_for, session):
    issuer = StubIssuer()
    c, response, _pending = await login_with(issuer, token_for)
    assert response.status_code == 401, name
    assert SESSION not in c.cookies, name
    assert (await c.get("/whoami")).status_code == 302, name  # still anonymous
    await c.aclose()
    failures = (
        (await session.execute(select(AuditEvent).where(AuditEvent.action == "auth.login_failed")))
        .scalars()
        .all()
    )
    assert len(failures) == 1 and failures[0].actor_id == "system"


async def test_callback_state_mismatch_is_refused():
    issuer = StubIssuer()
    app = make_app(issuer)
    async with client(app) as c:
        pending = await begin_login(c)
        issuer.token = id_token(claims(nonce=pending["nonce"]))
        response = await c.get("/auth/callback?code=abc123&state=forged")
    assert response.status_code == 401


async def test_callback_without_a_pending_login_is_refused():
    async with client(make_app(StubIssuer())) as c:
        assert (await c.get("/auth/callback?code=abc&state=x")).status_code == 401


async def test_discovery_document_must_claim_the_configured_issuer():
    from app.auth.oidc import OIDCError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": "https://evil.test"})

    oidc = OIDCClient(SETTINGS, transport=httpx.MockTransport(handler))
    with pytest.raises(OIDCError, match="issuer does not match"):
        await oidc.discover()


async def test_rotated_signing_key_is_picked_up_on_an_unknown_kid():
    """A cached JWKS must not lock the app out after the issuer rotates."""
    issuer = StubIssuer()
    app = make_app(issuer)
    async with client(app) as c:
        pending = await begin_login(c)
        issuer.token = id_token(claims(nonce=pending["nonce"]))
        assert (await c.get(f"/auth/callback?code=a&state={pending['state']}")).status_code == 303

        issuer.keys = [jwk("k2")]  # rotation
        pending = await begin_login(c)
        issuer.token = id_token(claims(nonce=pending["nonce"]), kid="k2")
        assert (await c.get(f"/auth/callback?code=b&state={pending['state']}")).status_code == 303


# --- session cookie tampering -------------------------------------------------------------


@pytest.mark.parametrize("forged", ["garbage", "a.b", ""])
async def test_tampered_session_cookie_is_not_accepted(forged):
    app = make_app(StubIssuer())
    async with client(app) as c:
        c.cookies.set(SESSION, forged, domain="console.test")
        assert (await c.get("/whoami")).status_code == 302  # browser: back to login
        assert (
            await c.get("/whoami", headers={"hx-request": "true"})
        ).status_code == 401  # htmx: 401 + HX-Redirect


async def test_privilege_escalation_by_resigning_is_rejected():
    from app.auth.tokens import sign

    app = make_app(StubIssuer())
    async with client(app) as c:
        c.cookies.set(
            SESSION,
            sign(
                {"id": "u", "email": "u@x", "name": "U", "roles": ["admin"]},
                secret="a-secret-the-app-does-not-use",
                ttl=600,
            ),
            domain="console.test",
        )
        assert (await c.get("/whoami")).status_code == 302


# --- session revocation ----------------------------------------------------------


async def signed_in() -> httpx.AsyncClient:
    """A client holding a real session cookie from a real login ceremony."""
    c, response, _ = await login_with(StubIssuer(), lambda nonce: id_token(claims(nonce=nonce)))
    assert response.status_code == 303
    return c


async def test_a_session_issued_before_the_revocation_is_refused(session):
    """The whole point: a cookie already in a browser stops working, without
    rotating SESSION_SECRET and signing everybody out."""
    c = await signed_in()
    assert (await c.get("/whoami")).status_code == 200

    await revoke(session, "idp-user-1")

    assert (await c.get("/whoami")).status_code == 302  # browser: back to login
    assert (
        await c.get("/whoami", headers={"hx-request": "true"})
    ).status_code == 401  # htmx: 401 + HX-Redirect
    await c.aclose()


async def test_a_session_issued_after_the_revocation_is_accepted(session):
    """Revoking ends the sessions that exist; it does not ban the account. The
    operator signs in again and works — that is the designed behaviour, and the
    reason this is not a substitute for removing someone's role."""
    await revoke(session, "idp-user-1", now=datetime.now(UTC) - timedelta(minutes=5))

    c = await signed_in()
    assert (await c.get("/whoami")).status_code == 200
    await c.aclose()


async def test_another_operators_revocation_does_not_touch_this_one(session):
    await revoke(session, "someone-else")

    c = await signed_in()
    assert (await c.get("/whoami")).status_code == 200
    await c.aclose()


async def test_no_revocation_row_is_no_revocation(session):
    c = await signed_in()
    assert (await c.get("/whoami")).status_code == 200
    await c.aclose()


async def test_revoking_twice_moves_the_line_forward(session):
    """Idempotent by upsert — and the second call must not be a no-op, or a
    second incident after a first sign-out would not take effect."""
    first = await revoke(session, "idp-user-1", now=datetime.now(UTC) - timedelta(hours=1))
    c = await signed_in()
    assert (await c.get("/whoami")).status_code == 200  # issued after `first`

    second = await revoke(session, "idp-user-1")
    assert second > first
    assert (await c.get("/whoami")).status_code == 302
    await c.aclose()


def test_the_script_strips_the_subject_it_is_given():
    """A `sub` copy-pasted out of a terminal, a spreadsheet cell or a chat
    message arrives with whitespace attached. Unstripped it is a *different*
    subject: the row is written, nothing ever matches it, and the script prints
    "revoked" over a session that is still live."""
    assert clean_sub("  idp-user-1\n") == "idp-user-1"
    assert clean_sub("idp-user-1") == "idp-user-1"
    for empty in ("", "   ", "\t\n"):
        with pytest.raises(argparse.ArgumentTypeError):
            clean_sub(empty)


async def test_a_pre_upgrade_cookie_without_iat_is_refused_as_expired(session):
    """A cookie minted before revocation shipped carries no `iat`, so no revocation can be
    compared against it — honouring it would leave a class of session revocation
    silently cannot reach, for as long as SESSION_MAX_AGE. It is refused exactly
    as an expired one is: everyone signs in once at the upgrade (RELEASE_NOTES).
    """
    app = make_app(StubIssuer())
    async with client(app) as c:
        c.cookies.set(
            SESSION,
            # The v1.1.0 payload, byte for byte, correctly signed by this app.
            sign(
                {"id": "idp-user-1", "email": "ops@example.com", "name": "Ops Person",
                 "roles": ["operator"]},
                secret=SESSION_SECRET,
                ttl=600,
            ),
            domain="console.test",
        )
        assert (await c.get("/whoami")).status_code == 302
        assert (
            await c.get("/whoami", headers={"hx-request": "true"})
        ).status_code == 401


async def test_a_current_cookie_carries_an_issued_at(session):
    """The pin under the one above: `session_value` stamps `iat`, and it is the
    minting time rather than the expiry."""
    c = await signed_in()
    payload = unsign(c.cookies[SESSION], secret=SESSION_SECRET)
    assert payload is not None
    assert isinstance(payload["iat"], int)
    assert payload["iat"] < payload["exp"]
    assert abs(payload["iat"] - time.time()) < 60
    await c.aclose()
