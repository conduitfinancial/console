"""Operator authentication: pluggable identity, sessions, CSRF, roles (plan v2 §2)."""

from app.auth.actor import ROLES, Actor, IdentityProvider, map_roles
from app.auth.providers import DisabledProvider, OIDCProvider, ProxyProvider, build_provider
from app.auth.web import csrf_hx_headers, csrf_token, current_actor, install_auth, require

__all__ = [
    "ROLES",
    "Actor",
    "DisabledProvider",
    "IdentityProvider",
    "OIDCProvider",
    "ProxyProvider",
    "build_provider",
    "csrf_hx_headers",
    "csrf_token",
    "current_actor",
    "install_auth",
    "map_roles",
    "require",
]
