"""Wiring: one middleware resolves the Actor and enforces CSRF for every
request, plus the minimal login/logout/denied surface (plan v2 §2).

Order inside the middleware is deliberate: CSRF is checked before identity, so a
forged cross-site mutation is refused whether or not the victim's browser also
carried a valid session.
"""

from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute, _IncludedRouter
from markupsafe import escape
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import audit
from app.auth.actor import Actor, IdentityProvider
from app.auth.oidc import OIDCError
from app.auth.providers import OIDCProvider, build_provider
from app.auth.tokens import (
    CSRF_COOKIE,
    cookie_name,
    CSRF_HEADER,
    LOGIN_COOKIE,
    LOGIN_TTL_SECONDS,
    SESSION_COOKIE,
    csrf_ok,
    new_csrf_token,
    session_value,
    sign,
    unsign,
)
from app.config import Settings, get_settings
from app.db import sessionmaker
from app.permissions import PERMISSIONS

log = logging.getLogger(__name__)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_MESSAGE = "Missing or invalid CSRF token. Reload the page and try again."

# Deployment probes must not need credentials (plan v2 §2 / §8); Conduit's
# webhook deliveries carry a signature, not a session.
ANONYMOUS_PREFIXES: tuple[str, ...] = (
    "/health/",
    "/auth/login",
    "/auth/denied",
    "/webhooks/",
)

# Requests that authenticate by something other than a cookie, so a CSRF token
# would be meaningless. The webhook receiver is verified by Conduit's signature.
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = ("/webhooks/",)

router = APIRouter()


# --- dependencies ------------------------------------------------------------------


def current_actor(request: Request) -> Actor:
    """The verified Actor for this request. The middleware has already refused
    anything without one, so this only trips for routes mounted outside it."""
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return actor


def require(permission: str):
    """`Depends(require("payout.create"))` — a route declares the ACTION it
    gates, never a role. Which roles hold it is the deployment's business
    (`app.permissions`, PERMISSIONS.md).

    An unknown name raises at import, not at request time: a typo here would
    otherwise be a route nobody can reach, discovered by an operator.
    """
    if permission not in PERMISSIONS:
        raise KeyError(f"{permission!r} is not in the permission catalog (app/permissions.py)")

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if not actor.can(permission):
            # The sentence lands on the 403 PAGE (`main.http_error`), so it is
            # written for the operator reading it: it names the action to ask
            # for, by the name an administrator grants (A3 gate ruling, m4).
            # Never which roles hold it, and never who does.
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action needs the {permission} permission. "
                    "An administrator grants it by that name."
                ),
            )
        return actor

    # Read back by `route_gates` — this attribute is what makes the coverage test
    # structural rather than a grep.
    dependency.permission = permission  # type: ignore[attr-defined]
    return dependency


def route_gates(app: FastAPI) -> list[tuple[str, str, str, frozenset[str]]]:
    """Every route as `(method, path, endpoint name, declared permissions)`.

    Walks the app's own routing table — including the routers mounted lazily by
    `include_router` — so it sees what is actually served, not what a module
    appears to declare.
    """

    def declared(dependant) -> frozenset[str]:
        found: set[str] = set()
        for sub in dependant.dependencies:
            name = getattr(sub.call, "permission", None)
            if name:
                found.add(name)
            found |= declared(sub)
        return frozenset(found)

    def routes(items):
        for route in items:
            if isinstance(route, _IncludedRouter):  # FastAPI ≥0.140 defers inclusion
                for ctx in route.effective_route_contexts():
                    yield ctx.methods, ctx.path, ctx.endpoint, ctx.dependant
            elif isinstance(route, APIRoute):
                yield route.methods, route.path, route.endpoint, route.dependant

    gates = []
    for methods, path, endpoint, dependant in routes(app.routes):
        for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
            gates.append((method, path, endpoint.__name__, declared(dependant)))
    return sorted(gates, key=lambda row: (row[1], row[0]))


# --- csrf helpers for templates ----------------------------------------------------


def csrf_token(request: Request) -> str:
    return getattr(request.state, "csrf_token", "")


def csrf_hx_headers(request: Request) -> str:
    """Drop into a template as `hx-headers='{{ csrf_hx_headers(request) }}'` on
    <body>; every htmx request below it then carries the token."""
    return json.dumps({CSRF_HEADER: csrf_token(request)})


# --- middleware --------------------------------------------------------------------


def _denied(reason: str, request: Request | None = None) -> HTMLResponse:
    """The middleware's own 403: a CSRF refusal, or an account mapped to no role.

    Rendered inside the chrome when there is a request to render it against
    — a bare `<h1>` on white was the same "framework default showing through"
    the 404 was. The import is deferred because `app.web`
    imports this module: a module-level one would be a cycle.

    **`reason` is not rendered by the chrome path.** The two callers' sentences
    are already in `web.ERROR_COPY`'s 403 copy in substance, and a `reason` in
    a page is one careless caller away from reflected content — the bare
    fallback below keeps escaping it for the same reason it always did.
    """
    if request is not None:
        try:
            from app.web import render_error

            return render_error(request, 403)
        except Exception:  # noqa: BLE001 — a refusal must never become a 500
            log.exception("could not render the 403 page")
    # No request in hand (or the template failed): the pre-A3 page, unchanged.
    # `reason` is escaped — the only thing standing between a future caller and
    # reflected XSS.
    return HTMLResponse(
        f"<!doctype html><title>Not permitted</title><h1>Not permitted</h1><p>{escape(reason)}</p>",
        status_code=403,
    )


def _unauthenticated(request: Request, provider: IdentityProvider) -> Response:
    login_url = getattr(provider, "login_url", None)
    if login_url and request.method == "GET":
        if request.headers.get("hx-request"):  # htmx cannot follow a 302 usefully
            return Response(status_code=401, headers={"HX-Redirect": login_url})
        return RedirectResponse(login_url, status_code=302)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


def install_auth(
    app: FastAPI, *, provider: IdentityProvider | None = None, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    provider = provider or build_provider(settings)
    app.state.auth_provider = provider
    app.include_router(router)
    anonymous = ANONYMOUS_PREFIXES
    if isinstance(provider, OIDCProvider):
        # The callback is where identity is established, so it cannot require it.
        anonymous += (provider.settings.oidc_callback_path,)
        app.add_api_route(
            provider.settings.oidc_callback_path,
            oidc_callback,
            methods=["GET"],
            name="oidc_callback",
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        secret = settings.session_secret.get_secret_value()
        path = request.url.path
        mutating = request.method in MUTATING_METHODS and not path.startswith(
            CSRF_EXEMPT_PREFIXES
        )
        cookie = request.cookies.get(cookie_name(CSRF_COOKIE, secure=settings.cookie_secure))
        token = unsign(cookie, secret=secret)

        if mutating and not csrf_ok(cookie, request.headers.get(CSRF_HEADER), secret=secret):
            log.warning("csrf rejected: %s %s", request.method, path)
            return _denied(CSRF_MESSAGE, request)

        anonymous_path = path.startswith(anonymous)
        subject = ""
        if not anonymous_path:
            actor = await request.app.state.auth_provider.authenticate(request)
            if actor is None:
                return _unauthenticated(request, request.app.state.auth_provider)
            if not actor.roles or not actor.permissions:
                # Authenticated but mapped to nothing: 403, and worth auditing —
                # unlike a 401, it is bounded by an upstream that already let
                # them in. `permissions` is the second half: a live session whose
                # role has since been removed from ROLES_FILE still names a role,
                # and would otherwise wander into a 403 on every page instead of
                # being told once, here, what is wrong.
                await _audit("auth.denied", actor, detail={"path": path})
                return _denied(
                    "Your account has no console role. Ask an administrator.", request
                )
            request.state.actor = actor
            subject = actor.id
            if mutating and token is not None and token.get("sub") != subject:
                log.warning("csrf rejected: token was issued to another operator")
                return _denied(CSRF_MESSAGE, request)

        # Anonymous pages (login, health) never mint one: it would be unbound,
        # and would then be replaced on the first page load after login anyway.
        issued = (
            None
            if anonymous_path or (token and token.get("sub") == subject)
            else new_csrf_token(subject, secret=secret, ttl=settings.session_max_age_seconds)
        )
        request.state.csrf_token = issued or cookie

        response = await call_next(request)
        if issued:
            _set_cookie(response, CSRF_COOKIE, issued, settings, http_only=True)
        return response


def _set_cookie(
    response: Response, name: str, value: str, settings: Settings, *, http_only: bool = True
) -> None:
    response.set_cookie(
        cookie_name(name, secure=settings.cookie_secure),
        value,
        max_age=settings.session_max_age_seconds,
        httponly=http_only,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, name: str, *, secure: bool) -> None:
    """Delete a cookie with the SAME attributes it was set with.

    `Response.delete_cookie` defaults to `secure=False, httponly=False`, and a
    deletion whose attributes do not match is not a deletion. It is fatal for
    these three in particular: with `COOKIE_SECURE=true` they carry the
    `__Host-` prefix, and a browser REJECTS any `__Host-` cookie without
    `Secure` — so the expiry never landed and logout signed nobody out anywhere
    the flag is on, which is everywhere but sandbox.

    Mirrors `_set_cookie` deliberately: same name derivation, same flags, so the
    pair cannot drift apart again.
    """
    response.delete_cookie(
        cookie_name(name, secure=secure),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


async def _audit(action: str, actor: Actor | None, *, detail: dict | None = None) -> None:
    """Login/logout trail (plan v2 §3). Never carries a token, header value or
    claim body — only the decision and the reason."""
    async with sessionmaker()() as session:
        audit.record(
            session,
            action=action,
            actor_id=actor.id if actor else audit.SYSTEM_ACTOR_ID,
            actor_email=actor.email if actor else audit.SYSTEM_ACTOR_EMAIL,
            detail=detail,
        )
        await session.commit()


# --- routes ------------------------------------------------------------------------


async def _login_failed(exc: OIDCError) -> Response:
    """One rendering for both halves of the ceremony. The reason reaches the log
    and the audit trail — never the page, because it can name claims."""
    log.warning("oidc login failed: %s", exc)
    await _audit("auth.login_failed", None, detail={"reason": str(exc)})
    return HTMLResponse(
        "<!doctype html><title>Sign-in failed</title><h1>Sign-in failed</h1>"
        "<p><a href='/auth/login'>Try again</a></p>",
        status_code=401,
    )


@router.get("/auth/login")
async def login(request: Request) -> Response:
    provider = request.app.state.auth_provider
    if not isinstance(provider, OIDCProvider):
        raise HTTPException(status_code=404, detail="login is handled upstream")
    settings = provider.settings
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)  # PKCE: 86 chars, within RFC 7636's 43–128
    redirect_uri = _redirect_uri(request, settings)
    try:
        # Discovery runs here, so an unreachable or misconfigured issuer surfaces
        # on the login route too — as an audited sign-in failure, not an
        # unaudited 500.
        url = await provider.client.authorization_url(
            state=state, nonce=nonce, redirect_uri=redirect_uri, verifier=verifier
        )
    except OIDCError as exc:
        return await _login_failed(exc)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        cookie_name(LOGIN_COOKIE, secure=settings.cookie_secure),
        sign(
            {"state": state, "nonce": nonce, "verifier": verifier},
            secret=settings.session_secret.get_secret_value(),
            ttl=LOGIN_TTL_SECONDS,
        ),
        max_age=LOGIN_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


async def oidc_callback(request: Request) -> Response:
    """Registered at OIDC_CALLBACK_PATH by `install_auth` (oidc mode only)."""
    provider = request.app.state.auth_provider
    settings = provider.settings
    secret = settings.session_secret.get_secret_value()
    pending = unsign(
        request.cookies.get(cookie_name(LOGIN_COOKIE, secure=settings.cookie_secure)),
        secret=secret,
    )
    try:
        if pending is None:
            raise OIDCError("no pending login (expired or forged callback)")
        sent_state = request.query_params.get("state", "")
        if not sent_state.isascii() or not secrets.compare_digest(
            str(pending.get("state")), sent_state
        ):
            raise OIDCError("state mismatch")
        code = request.query_params.get("code", "")
        if not code:
            raise OIDCError(f"callback carried no code (error={request.query_params.get('error')})")
        id_token = await provider.client.exchange(
            code=code,
            redirect_uri=_redirect_uri(request, settings),
            verifier=str(pending.get("verifier")),
        )
        claims = await provider.client.verify(id_token, nonce=str(pending.get("nonce")))
    except OIDCError as exc:
        return await _login_failed(exc)

    actor = provider.actor_from_claims(claims)
    await _audit("auth.login", actor, detail={"roles": sorted(actor.roles)})
    response = RedirectResponse("/", status_code=303)
    _set_cookie(
        response,
        SESSION_COOKIE,
        session_value(actor, secret=secret, ttl=settings.session_max_age_seconds),
        settings,
    )
    _clear_cookie(response, LOGIN_COOKIE, secure=settings.cookie_secure)
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    actor = getattr(request.state, "actor", None)
    await _audit("auth.logout", actor)
    login_url = getattr(request.app.state.auth_provider, "login_url", None)
    response = RedirectResponse(login_url or "/", status_code=303)
    secure = get_settings().cookie_secure
    _clear_cookie(response, SESSION_COOKIE, secure=secure)
    # The CSRF cookie too: it is signed with the same secret
    # and carries the actor's `sub`, so leaving it on a shared machine leaves a
    # token naming who was last signed in. The middleware mints a fresh one on
    # the next request that needs it.
    _clear_cookie(response, CSRF_COOKIE, secure=secure)
    return response


def _redirect_uri(request: Request, settings: Settings) -> str:
    """Configured value wins; the derived one is a dev convenience — behind a
    TLS-terminating proxy `base_url` is http:// unless forwarded headers are
    trusted, which config refuses to rely on in production."""
    return settings.oidc_redirect_uri or (
        str(request.base_url).rstrip("/") + settings.oidc_callback_path
    )
