"""The three `IdentityProvider` adapters (plan v2 §2).

Selected by `AUTH_MODE`; nothing downstream knows which one is installed.
"""

from __future__ import annotations

import hmac
import logging

from starlette.requests import Request

from app.auth.actor import Actor, IdentityProvider, ROLES, map_roles, split_claim
from app.permissions import ADMIN, permissions_for
from app.auth.oidc import OIDCClient
from app.auth.revocation import revoked
from app.auth.tokens import SESSION_COOKIE, actor_from_session, cookie_name, session_payload
from app.config import Settings

log = logging.getLogger(__name__)


class ProxyProvider:
    """Identity headers from an upstream auth proxy — trusted only when the
    request also carries the shared proxy-verification secret.

    Without that gate the headers are just headers: anyone who can reach the app
    port is an admin. The compare is constant-time, and an unconfigured secret
    rejects everything (fail closed) rather than matching an absent header.
    """

    login_url = None  # the proxy owns login; the app never renders one

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def authenticate(self, request: Request) -> Actor | None:
        s = self.settings
        expected = s.proxy_shared_secret.get_secret_value()
        provided = request.headers.get(s.proxy_secret_header, "")
        if not expected or not provided.isascii() or not hmac.compare_digest(expected, provided):
            # No header values in the log line: they are the identity claim, and
            # one of them is a secret.
            log.warning("proxy auth rejected: missing or wrong proxy secret")
            return None
        user = (request.headers.get(s.proxy_user_header) or "").strip()
        email = (request.headers.get(s.proxy_email_header) or "").strip()
        if not user and not email:
            log.warning("proxy auth rejected: no identity headers behind a valid secret")
            return None
        table = s.roles
        roles = map_roles(
            (request.headers.get(s.proxy_groups_header) or "").split(","), s.role_map, table
        )
        return Actor(
            id=user or email,
            email=email,
            display_name=user or email,
            roles=roles,
            permissions=permissions_for(roles, table),
        )


class DisabledProvider:
    """Development only — config refuses `disabled` with a production host."""

    login_url = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def authenticate(self, request: Request) -> Actor:
        return DEV_ACTOR


DEV_ACTOR = Actor(
    id="dev",
    email="dev@localhost",
    display_name="Development (AUTH_MODE=disabled)",
    roles=frozenset(ROLES),
    # The whole catalog, not a resolved role table: `disabled` is refused with a
    # production host, and a custom role file must not be able to *narrow* the
    # development actor into looking like a working deployment.
    permissions=ADMIN,
)


class OIDCProvider:
    """Per-request identity comes from the signed session cookie; the login
    ceremony that writes it lives in `app.auth.web`.

    **Revocation lives here, not in the middleware**, because this is the only
    adapter with a session to revoke. `ProxyProvider` is handed identity afresh
    on every request by an upstream that owns sign-out; `DisabledProvider` is
    development. Refusing returns `None`, so the middleware's existing
    "no verified identity" path does the rest — the operator is sent to sign in
    again, exactly as an expired cookie sends them, and signing in mints a cookie
    the revocation no longer covers.
    """

    def __init__(self, settings: Settings, *, client: OIDCClient | None = None) -> None:
        self.settings = settings
        self.client = client or OIDCClient(settings)
        self.login_url = "/auth/login"

    async def authenticate(self, request: Request) -> Actor | None:
        payload = session_payload(
            request.cookies.get(
                cookie_name(SESSION_COOKIE, secure=self.settings.cookie_secure)
            ),
            secret=self.settings.session_secret.get_secret_value(),
        )
        if payload is None:
            return None
        if await revoked(str(payload["id"]), int(payload["iat"])):
            log.warning("session refused: issued before this operator's revocation")
            return None
        return actor_from_session(payload, roles=self.settings.roles)

    def actor_from_claims(self, claims: dict) -> Actor:
        subject = str(claims.get("sub") or "")
        email = str(claims.get("email") or "")
        table = self.settings.roles
        roles = map_roles(
            split_claim(claims.get(self.settings.oidc_role_claim)), self.settings.role_map, table
        )
        return Actor(
            id=subject or email,
            email=email,
            display_name=str(claims.get("name") or claims.get("preferred_username") or email),
            roles=roles,
            permissions=permissions_for(roles, table),
        )


def build_provider(settings: Settings) -> IdentityProvider:
    return {"proxy": ProxyProvider, "oidc": OIDCProvider, "disabled": DisabledProvider}[
        settings.auth_mode
    ](settings)
