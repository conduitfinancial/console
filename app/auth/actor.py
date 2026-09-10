"""The verified operator and what they are allowed to do (plan v2 §2).

Everything downstream of authentication sees an `Actor` and nothing else — no
headers, no tokens, no claims. The operations layer stays on strings
(`actor.id`, `actor.email`), so it never learns which adapter produced them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request

from app.permissions import ROLES


@dataclass(frozen=True)
class Actor:
    """Who is acting, and what that lets them do.

    `roles` are names — from the IdP, through AUTH_ROLE_MAP; they are what the
    session cookie and the audit trail carry. `permissions` is what the roles
    resolved to against this deployment's role table, and it is the only thing
    any gate looks at. Resolving at authentication time (not at check time)
    keeps the deployment's config out of every call site — and an Actor built
    without it can do nothing, which is the right direction to fail.
    """

    id: str
    email: str
    display_name: str
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def can_any(self, *permissions: str) -> bool:
        """For a heading or a tray that only earns its place if some button under
        it does — never for the button itself."""
        return any(p in self.permissions for p in permissions)


class IdentityProvider(Protocol):
    """plan v2 §2. `None` means "no verified identity" — never a fallback actor."""

    # Where an unauthenticated browser should be sent, or None for adapters
    # where login happens somewhere else entirely (the proxy) or not at all.
    login_url: str | None

    async def authenticate(self, request: Request) -> Actor | None: ...


def map_roles(
    values: Iterable[str], mapping: dict[str, str], known: Iterable[str] = ROLES
) -> frozenset[str]:
    """Proxy groups / OIDC role-claim values → console roles.

    A value with no mapping entry maps to the role of its own name; anything
    that is not a role grants nothing. So an unmapped user ends up with an empty
    role set — authenticated, authorized for nothing (403), never a default.

    `known` is the deployment's role table (built-ins plus ROLES_FILE), so a
    custom role reaches an operator through exactly the same mapping — unchanged
    — as a built-in one.
    """
    granted = {mapping.get(v.strip(), v.strip()) for v in values if v.strip()}
    return frozenset(granted & set(known))


def split_claim(value: object) -> list[str]:
    """Role claims come as a list, or as a space/comma-delimited string."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return value.replace(",", " ").split()
    return []
