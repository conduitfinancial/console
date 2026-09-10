"""Signed, expiring cookie values: the session and the CSRF token.

`hmac` + `base64` instead of itsdangerous — sign/unsign below is the
whole of what that dependency would have been used for, and pinning the format
here means the session cookie and the CSRF cookie share one verification path.
Ceiling: values are signed, not encrypted. Nothing secret goes in them (id,
email, display name, roles — all of which the operator already knows).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.auth.actor import Actor
from app.config import get_settings
from app.permissions import permissions_for

SESSION_COOKIE = "console_session"
CSRF_COOKIE = "console_csrf"
CSRF_HEADER = "X-CSRF-Token"
LOGIN_COOKIE = "console_login"  # short-lived: OIDC state + nonce
LOGIN_TTL_SECONDS = 600

HOST_PREFIX = "__Host-"


def cookie_name(base: str, *, secure: bool) -> str:
    """`__Host-`-prefix the cookie when it can carry the `Secure` flag.

    The prefix is a browser-enforced promise that the cookie was set by exactly
    this origin, with `Path=/` and no `Domain`. Without it a sibling subdomain
    can plant a *validly signed* session or login cookie in the victim's browser
    and have money actions audited under the attacker's identity — signature
    checking cannot see that, because the cookie really is signed; it is simply
    not the one this browser earned.

    Browsers reject `__Host-` cookies without `Secure`, so plain-http local
    development keeps the bare name — and config refuses `COOKIE_SECURE=false`
    anywhere but sandbox.
    """
    return f"{HOST_PREFIX}{base}" if secure else base


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(body: str, secret: str) -> bytes:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()


def sign(payload: dict, *, secret: str, ttl: int, now: float | None = None) -> str:
    body = b64e(
        json.dumps(
            {**payload, "exp": int(now or time.time()) + ttl},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return f"{body}.{b64e(_mac(body, secret))}"


def unsign(token: str | None, *, secret: str, now: float | None = None) -> dict | None:
    """Payload, or None for anything tampered with, malformed or expired."""
    if not token or not token.isascii() or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    try:
        if not hmac.compare_digest(b64d(signature), _mac(body, secret)):
            return None
        payload = json.loads(b64d(body))
    except Exception:  # noqa: BLE001 — any decode failure is just an invalid token
        return None
    if not isinstance(payload, dict) or float(payload.get("exp", 0)) <= (now or time.time()):
        return None
    return payload


# --- session ---------------------------------------------------------------------


def session_value(actor: Actor, *, secret: str, ttl: int, now: float | None = None) -> str:
    """`iat` is what makes a session revocable. `exp` alone says when
    a cookie stops working; `iat` says *when it was minted*, which is the only
    thing a "sign this operator out" record can be compared against — see
    `app.auth.revocation`."""
    issued = int(now or time.time())
    return sign(
        {"id": actor.id, "email": actor.email, "name": actor.display_name,
         "roles": sorted(actor.roles), "iat": issued},
        secret=secret,
        ttl=ttl,
        now=issued,
    )


def session_payload(token: str | None, *, secret: str, now: float | None = None) -> dict | None:
    """The session cookie's verified claims, or None for anything this app will
    not act on.

    **A cookie with no `iat` is refused as expired**. Cookies minted
    before that field existed cannot be compared against a revocation, so
    honouring them would leave a class of session that revocation silently
    cannot reach — for as long as SESSION_MAX_AGE. Refusing costs every operator
    one sign-in, once, at the upgrade; see RELEASE_NOTES.
    """
    payload = unsign(token, secret=secret, now=now)
    if payload is None or not payload.get("id") or not isinstance(payload.get("iat"), int):
        return None
    return payload


def actor_from_session(payload: dict, *, roles: dict[str, frozenset[str]]) -> Actor:
    """The cookie carries role NAMES, and `roles` (the deployment's role table)
    turns them into permissions on every request — so editing ROLES_FILE and
    restarting changes what an existing session may do, without invalidating it
    or having to trust what the cookie says the permissions were."""
    named = frozenset(str(r) for r in payload.get("roles") or [])
    return Actor(
        id=str(payload["id"]),
        email=str(payload.get("email") or ""),
        display_name=str(payload.get("name") or ""),
        roles=named,
        permissions=permissions_for(named, roles),
    )


def session_actor(
    token: str | None, *, secret: str, roles: dict[str, frozenset[str]]
) -> Actor | None:
    """Verify and resolve in one call — the whole of what a caller needs when it
    has no revocation check to make in between (`OIDCProvider` does, and uses the
    two halves)."""
    payload = session_payload(token, secret=secret)
    return None if payload is None else actor_from_session(payload, roles=roles)


# --- csrf ------------------------------------------------------------------------


def new_csrf_token(subject: str, *, secret: str, ttl: int) -> str:
    """Bound to the operator it was issued to (`sub`), so a token from another
    console account is not a licence to act as this one."""
    return sign({"csrf": secrets.token_urlsafe(16), "sub": subject}, secret=secret, ttl=ttl)


def csrf_ok(cookie: str | None, sent: str | None, *, secret: str) -> bool:
    """Signed double-submit: the cookie must be one of ours, and the request must
    echo it exactly. Signing the cookie is what stops a sibling subdomain from
    injecting a value it also knows."""
    if not cookie or not sent or not cookie.isascii() or not sent.isascii():
        return False
    return hmac.compare_digest(cookie, sent) and unsign(cookie, secret=secret) is not None


# --- seal (round-trip a value through the browser, signed) -----------------------
#
# convert.py's option seal and payouts.py's quote seal were the same idiom
# written twice: fetch the session secret, `sign`/`unsign` a payload, strip
# `exp` on the way back. One shared pair here; each caller keeps its own ttl
# and whatever payload shape it needs.


def seal(payload: dict, *, ttl: int) -> str:
    """Sign `payload` with the app's session secret, for a round trip to the
    browser and back."""
    return sign(payload, secret=get_settings().session_secret.get_secret_value(), ttl=ttl)


def unseal(token: str) -> dict | None:
    """The payload `seal` signed, minus `exp` — or None for anything tampered
    with, malformed or expired. `unsign` returns exactly what `seal` signed,
    so there is no key filtering beyond dropping `exp`."""
    payload = unsign(token, secret=get_settings().session_secret.get_secret_value())
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k != "exp"}
