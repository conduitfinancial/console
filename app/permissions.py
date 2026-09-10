"""The permission catalog: every gated action in this console, named once.

Routes declare a permission, never a role (`Depends(require("payout.create"))`).
Roles are bundles over this catalog: the three built-ins below, plus whatever a
deployment defines in `ROLES_FILE`.

**These names are client-facing contract.** They appear in a client's
`roles.json`, in their change-control diffs and in their security reviews, so a
rename here is a breaking change for every deployment. Spell one correctly the
first time or add a new one; do not re-point an existing name at a different
action.

Grammar: `noun.verb`, in the same vocabulary as the audit-action strings an
auditor reads in `audit_events` — `payout.create`, `contact.delete`. Reads are
one permission, `console.view`, because the console has exactly one read
surface: every list and detail page is gated the same way and always has been.
The exceptions are named separately because they are not just reading —
`export.csv` takes PII off the screen and into a file, and `contact.edit`'s form
page is part of the edit, not part of browsing.
"""

from __future__ import annotations

import difflib
import json
from functools import lru_cache
from pathlib import Path

VIEW = "console.view"

# name -> one line an operator (or their auditor) can read cold. PERMISSIONS.md
# is generated from this dict; it is not a second copy to keep in step.
PERMISSIONS: dict[str, str] = {
    VIEW: "Read every list and detail page in the console.",
    "export.csv": "Download a list as CSV, including the masked recipient coordinates on it.",
    "onboarding.edit": "Start, fill in, and discard onboarding drafts, and reopen a rejected application for correction.",
    "onboarding.submit": "Submit a completed onboarding draft to Conduit.",
    "onboarding.idv_link": "Fetch a person's identity-verification link (a one-time bearer URL).",
    "onboarding.access_any": "Open onboarding drafts started by another operator (your own always open).",
    "document.upload": "Upload a supporting document and attach it to an onboarding, a payout, a batch or an RFI response.",
    "rfi.respond": "Acknowledge and answer Conduit's requests for information.",
    "account.request": "Request a virtual account for a customer.",
    "payout.create": "Send a payout to a recipient outside the organization.",
    "payout.cancel": "Cancel a payout that has not settled yet.",
    "batch.upload": "Upload a payout batch file, mark it ready, or abandon it before dispatch.",
    "batch.dispatch": "Dispatch a prepared batch: one payout sent to Conduit for every row in it.",
    "transfer.create": "Transfer funds between two Conduit accounts of this organization.",
    "order.create": "Take a conversion quote and create the order behind it.",
    "order.execute": "Execute a pending conversion order at its quoted rate.",
    "order.cancel": "Cancel a pending conversion order and give up its rate.",
    "contact.edit": "Rename a saved contact or change its stored payment coordinates.",
    "contact.archive": "Archive a saved contact so it stops being offered.",
    "contact.delete": "Purge a saved contact's stored coordinates permanently.",
    "whitelist.register": "Register a recipient with Conduit for intercompany payouts.",
    "whitelist.revoke": "Revoke a registered recipient.",
    "operation.retry": "Retry an operation this console left unresolved.",
    "operation.abandon": "Mark an unresolved operation abandoned, releasing its duplicate guard.",
    "sandbox.simulate": "Drive the sandbox's simulation endpoints (approve, settle, reject, deposit).",
}

# What a permission needs alongside it. Every action in this console starts from
# a page — the button that fires it is rendered by a `console.view` route — so a
# role that may act but may not look is a role whose actions no operator can
# reach. Startup refuses that combination rather than shipping dead buttons.
REQUIRES: dict[str, frozenset[str]] = {
    name: frozenset({VIEW}) for name in PERMISSIONS if name != VIEW
}

# --- the three built-in bundles ----------------------------------------------------
#
# These reproduce the roles this console shipped with, exactly: viewer reads,
# operator moves money, admin additionally holds the release valves. They are not
# redefinable — a deployment that wants something else names a new role.

ADMIN_ONLY: frozenset[str] = frozenset(
    {"contact.delete", "operation.abandon", "onboarding.access_any"}
)
VIEWER: frozenset[str] = frozenset({VIEW, "export.csv"})
OPERATOR: frozenset[str] = frozenset(PERMISSIONS) - ADMIN_ONLY
ADMIN: frozenset[str] = frozenset(PERMISSIONS)

BUILTIN_ROLES: dict[str, frozenset[str]] = {
    "viewer": VIEWER,
    "operator": OPERATOR,
    "admin": ADMIN,
}
# Ordered least → most privileged, for the places that still print roles.
ROLES: tuple[str, ...] = tuple(BUILTIN_ROLES)


def admin_class(env: str) -> frozenset[str]:
    """Permissions that may only be granted on top of the whole operator base.

    The three the admin bundle owns are release valves: they undo or erase what
    another operator did. `sandbox.simulate` joins them anywhere the word
    "sandbox" is no longer literally true — off the sandbox host those routes
    still exist and still post to Conduit, so a role that can only simulate is a
    role with an unexplained write to a real host.
    """
    return ADMIN_ONLY | (frozenset() if env == "sandbox" else frozenset({"sandbox.simulate"}))


def permissions_for(roles: frozenset[str] | set[str], table: dict[str, frozenset[str]]) -> frozenset[str]:
    """Everything the named roles grant together. An unknown name grants nothing."""
    return frozenset().union(*[table.get(role, frozenset()) for role in roles]) if roles else frozenset()


# --- ROLES_FILE ---------------------------------------------------------------------


def _nearest(name: str) -> str:
    """The suggestion half of a refusal. Empty when nothing is close enough."""
    close = difflib.get_close_matches(name, PERMISSIONS, n=1, cutoff=0.6)
    return close[0] if close else ""


def _refuse(message: str) -> None:
    # RuntimeError for the same reason config.py uses it: a ValueError inside a
    # pydantic validator is re-raised with the input echoed back.
    raise RuntimeError(f"ROLES_FILE {message}")


def parse(text: str, *, env: str) -> dict[str, frozenset[str]]:
    """`{role: [permission, ...]}` → validated custom roles, or RuntimeError.

    Every refusal names the offending key and what to do about it, because the
    only person who reads it is looking at the file that caused it.
    """
    def _no_dupes(pairs):
        # This file is change-controlled and reviewed top to bottom; JSON's
        # last-wins on a repeated key would let the definition a reviewer reads
        # differ from the one that takes effect. Refuse it instead.
        seen = set()
        for key, value in pairs:
            if key in seen:
                _refuse(
                    f"defines the role {key!r} twice — the second silently overrides the first. "
                    "Each role name must appear once: remove or rename the duplicate"
                )
            seen.add(key)
        return dict(pairs)

    try:
        raw = json.loads(text, object_pairs_hook=_no_dupes)
    except json.JSONDecodeError as exc:
        _refuse(f"is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        _refuse("must be a JSON object of {\"role name\": [\"permission\", ...]}")

    custom: dict[str, frozenset[str]] = {}
    for role, granted in raw.items():
        if not isinstance(role, str) or not role.strip():
            _refuse("has a role with an empty name")
        if role in BUILTIN_ROLES:
            _refuse(
                f"defines {role!r}, which is a built-in role — the built-ins are fixed so that "
                "an upgrade cannot quietly change what an existing role means; give this one "
                "another name"
            )
        if not isinstance(granted, list) or not all(isinstance(p, str) for p in granted):
            _refuse(f"role {role!r} must be a list of permission names")
        for name in granted:
            if name not in PERMISSIONS:
                near = _nearest(name)
                _refuse(
                    f"role {role!r} lists unknown permission {name!r}"
                    + (f" — did you mean {near!r}?" if near else " — no permission has that name")
                    + " (the catalog is PERMISSIONS.md)"
                )
        custom[role] = frozenset(granted)

    for role, granted in custom.items():
        if not granted:
            _refuse(
                f"role {role!r} grants nothing — remove it, or give it at least "
                f"{VIEW!r}; a role that grants nothing is a 403 with extra steps"
            )
        for name in sorted(granted):
            missing = REQUIRES.get(name, frozenset()) - granted
            if missing:
                _refuse(
                    f"role {role!r} has {name!r} without {', '.join(map(repr, sorted(missing)))}, "
                    f"which it requires — the pages that offer {name!r} are {VIEW!r} pages"
                )
        elevated = sorted(granted & admin_class(env))
        if elevated:
            short = sorted(OPERATOR - granted)
            if short:
                _refuse(
                    f"role {role!r} has the admin-class permission {elevated[0]!r} without the "
                    f"operator base — add the {len(short)} missing operator permissions "
                    f"({', '.join(short[:3])}{', …' if len(short) > 3 else ''}) or drop it"
                )
    return custom


@lru_cache
def role_table(path: str, env: str) -> dict[str, frozenset[str]]:
    """Built-in bundles + `ROLES_FILE`'s roles. Read once per process, like any
    other setting; validating it *is* the startup guard (see `app.config`)."""
    if not path:
        return dict(BUILTIN_ROLES)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"ROLES_FILE could not be read ({type(exc).__name__}): {path}") from None
    return {**BUILTIN_ROLES, **parse(text, env=env)}
