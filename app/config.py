"""Settings + startup guards (plan v2 §2, OPERATIONS_SPEC §6).

Constructing Settings *is* the startup guard: an unsafe combination raises before
the app can serve. Never log `Settings` — it holds secrets (pydantic's SecretStr
keeps them out of reprs, but don't tempt it).
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import permissions

# Hardcoded host allowlist — the only Conduit origins this app may ever talk to.
CONDUIT_HOSTS: dict[str, str] = {
    "sandbox": "https://api.sandbox.conduit.financial",
    "staging": "https://api.staging.conduit.financial",
    "production": "https://api.conduit.financial",
}

# `sandbox` is this app's notion of local development: it is the only place a
# plaintext cookie or a placeholder secret is tolerated. Staging carries a real
# key against a production-labelled host, so it is held to production's bar.
DEV_ENV = "sandbox"

MIN_SECRET_LENGTH = 32
# Substrings that mean "nobody replaced this". `.env.example` ships these
# blank except ENCRYPTION_KEY's `replace-me-with-a-generated-fernet-key` —
# but ENCRYPTION_KEY is never run through this list (it is not in the
# weak-secret loop below); that value is refused only because it is not
# valid base64, by the Fernet-format check further down. A copied config is
# exactly how a published value reaches a real deployment.
# Deliberately NOT "secret": a strong, randomly generated 32+ char passphrase
# is legitimate even when the word "secret" appears in it
# — length plus these literal placeholder words is the guard, for the
# settings that go through it.
PLACEHOLDER_MARKERS = (
    "replace-me",
    "replace_me",
    "changeme",
    "change-me",
    "example",
    "placeholder",
)

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def weak_secret(value: str) -> str | None:
    """Why this secret is unusable, or None. Never returns the value itself."""
    if len(value) < MIN_SECRET_LENGTH:
        return f"shorter than {MIN_SECRET_LENGTH} characters"
    lowered = value.lower()
    marker = next((m for m in PLACEHOLDER_MARKERS if m in lowered), None)
    return f"contains the placeholder text {marker!r}" if marker else None


def https_or_loopback(url: str) -> bool:
    """https anywhere, or plain http only to a loopback address for local dev.

    An http issuer means the token exchange — client secret, authorization code
    and PKCE verifier — crosses the network in the clear.
    """
    if url.startswith("https://"):
        return True
    return url.startswith("http://") and (urlsplit(url).hostname or "") in LOOPBACK_HOSTS


def callback_path_ok(path: str) -> bool:
    """An OIDC callback path that cannot swallow the whole app.

    `install_auth` adds this value to the anonymous prefixes and matches with
    `str.startswith`, so `"/"` (or `""`, which startswith accepts against every
    string) makes every route unauthenticated. Two non-empty segments, no
    trailing slash.
    """
    return (
        path.startswith("/")
        and not path.endswith("/")
        and len([s for s in path.split("/") if s]) >= 2
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    conduit_env: Literal["sandbox", "staging", "production"] = "sandbox"
    conduit_api_key: SecretStr = SecretStr("")
    # Optional explicit origin; must match the allowlist entry for conduit_env.
    conduit_api_base: str | None = None

    database_url: str
    session_secret: SecretStr
    # One or more Fernet keys, comma-separated, newest first: the first key
    # encrypts, every key decrypts (rotation = prepend a key and redeploy).
    encryption_key: SecretStr
    auth_mode: Literal["proxy", "oidc", "disabled"] = "proxy"
    # Per-endpoint webhook signing secret, `whsec_` prefix included — the prefix
    # is part of the HMAC key. Empty = no endpoint registered yet, and the
    # receiver route is not mounted at all (404).
    conduit_webhook_secret: SecretStr = SecretStr("")

    # --- auth: proxy adapter (plan v2 §2) ---
    # Identity headers are trusted ONLY when the request also carries this
    # secret in `proxy_secret_header` — otherwise anyone who reaches the app
    # port can set X-Auth-Request-* themselves.
    proxy_shared_secret: SecretStr = SecretStr("")
    proxy_secret_header: str = "X-Proxy-Auth"
    proxy_user_header: str = "X-Auth-Request-User"
    proxy_email_header: str = "X-Auth-Request-Email"
    proxy_groups_header: str = "X-Auth-Request-Groups"

    # --- auth: oidc adapter ---
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_callback_path: str = "/auth/callback"
    # Absolute callback URL, used verbatim for the authorization redirect and
    # the token exchange. Unset (dev only): derived from the request's base URL,
    # which is wrong behind a TLS-terminating proxy unless forwarded headers are
    # trusted — which login should not have to do.
    oidc_redirect_uri: str = ""
    oidc_role_claim: str = "roles"
    oidc_scopes: str = "openid email profile"

    # Group / role-claim value -> console role, as "group=role" pairs. A value
    # not listed maps to the role of the same name; anything else grants
    # nothing (an unmapped user gets 403, never a default role).
    auth_role_map: str = ""
    # Path to a JSON file of custom roles: {"role name": ["permission", ...]}.
    # A file rather than an env-JSON blob because a role definition belongs in
    # the client's change control, where it is diffed and reviewed line by line
    # — and because a refusal can then name the role that caused it. Unset = the
    # three built-in roles and nothing else. See PERMISSIONS.md.
    roles_file: str = ""
    session_max_age_seconds: int = 12 * 3600
    # Only ever false for local http development.
    cookie_secure: bool = True

    # Per-operation money ceiling, a decimal string. Empty = no ceiling, which
    # is what every deployment ran before. Asset-agnostic on purpose:
    # one number, stated on screen beside the host. A per-asset map is YAGNI
    # until a second currency argues for it.
    # One number for every asset; a {asset: ceiling} map is the
    # upgrade path, and only when a deployment actually holds two currencies
    # whose ceilings must differ.
    money_ceiling: str = ""

    # OPERATIONS_SPEC §6
    op_client_timeout: float = 30.0
    op_stale_inflight_factor: float = 2.0
    op_created_ttl_seconds: int = 24 * 3600
    op_body_retention_days: int = 30
    # The raw webhook delivery is kept for replay and for reading back what
    # Conduit actually sent when a projection looks wrong. Its own window rather
    # than OP_BODY_RETENTION's: an event is not an operation — it is settled the
    # moment the worker processes it, and nothing ever replays it afterwards, so
    # the only reason to hold it is a human investigation. Unprocessed and
    # failed events are never purged at any age (`worker.purge_raw_bodies`).
    webhook_raw_retention_days: int = 30
    # Unsubmitted drafts are discarded after this long without an edit (plan v2
    # §3 retention). Submitted ones are kept — their payload is already purged.
    draft_ttl_days: int = 30
    reconcile_max_attempts: int = 5
    reconcile_interval_seconds: int = 60
    # A non-terminal projection untouched for this long is repaired by reading
    # the resource back (plan v2 §5). Longer than any settle time we expect, so
    # a resource that is simply still working is not re-read every tick.
    projection_stale_seconds: int = 900
    # Conduit client read policy (plan v2 §4) — reads only; mutations never retry.
    read_max_attempts: int = 4
    read_backoff_base_seconds: float = 0.25
    read_backoff_max_seconds: float = 8.0
    # Conduit requests one reconciler pass may spend, so it cannot starve
    # interactive traffic or push it into rate limiting.
    reconcile_request_budget: int = 100

    # --- worker liveness (deploy/README §4) ---
    # A file the worker touches after every successful loop pass. Empty (the
    # default) writes nothing; a container healthcheck reads its mtime, which
    # is the only externally visible sign that a process with no port is alive.
    worker_heartbeat_path: str = ""
    # Consecutive fully-failed passes of one loop before the worker gives up and
    # exits non-zero, so the restart policy — not a human — deals with it. A
    # transient outage resets the count on the first pass that works.
    worker_max_failed_passes: int = 10

    @property
    def conduit_base_url(self) -> str:
        return self.conduit_api_base or CONDUIT_HOSTS[self.conduit_env]

    @property
    def encryption_keys(self) -> list[str]:
        return [k for k in self.encryption_key.get_secret_value().split(",") if k.strip()]

    @property
    def ceiling(self) -> Decimal | None:
        """The configured ceiling, or None. Validated at boot, so this parses."""
        return Decimal(self.money_ceiling) if self.money_ceiling.strip() else None

    @property
    def stale_inflight_seconds(self) -> float:
        return self.op_client_timeout * self.op_stale_inflight_factor

    @property
    def role_map(self) -> dict[str, str]:
        pairs = (p.split("=", 1) for p in self.auth_role_map.split(",") if "=" in p)
        return {group.strip(): role.strip() for group, role in pairs}

    @property
    def roles(self) -> dict[str, frozenset[str]]:
        """Role name -> permissions: the built-in bundles plus `ROLES_FILE`.

        Read once per process (`role_table` is cached on the path), so this is a
        dict lookup on the authentication path, not a file read.
        """
        return permissions.role_table(self.roles_file, self.conduit_env)

    @model_validator(mode="before")
    @classmethod
    def _secret_files(cls, values):
        """`FOO_FILE=/run/secrets/foo` supplies FOO from a file's contents.

        Docker secrets, Kubernetes projected volumes and systemd credentials all
        deliver a secret as a file rather than as an environment variable —
        which is the better shape, because a file is not inherited by every
        child process and does not appear in `docker inspect`.

        Which settings support it is derived, not listed: every `SecretStr`
        field plus `DATABASE_URL`, whose DSN carries a password. So a new secret
        gets the file form for free, and this cannot fall behind the model.

        Setting **both** FOO and FOO_FILE is an error rather than a precedence
        rule. Either order surprises somebody — and the situation itself means
        two deployment mechanisms disagree about a secret, which is worth
        stopping for. Neither the file's contents nor its path appears in any
        message here: the error names the setting.
        """
        if not isinstance(values, dict):
            return values
        supported = {
            name
            for name, field in cls.model_fields.items()
            if field.annotation is SecretStr
        } | {"database_url"}
        for name in sorted(supported):
            variable = name.upper()
            path = os.environ.get(f"{variable}_FILE")
            if not path:
                continue
            if os.environ.get(variable):
                raise RuntimeError(
                    f"{variable} and {variable}_FILE are both set — supply the secret one way"
                )
            try:
                # Only trailing newlines: `echo` adds one, and a secret is
                # allowed to end in anything else, including a space.
                values[name] = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeError(
                    f"{variable}_FILE could not be read ({type(exc).__name__})"
                ) from None
        return values

    @model_validator(mode="after")
    def _guards(self) -> "Settings":
        # RuntimeError, not ValueError: pydantic wraps ValueError into a
        # ValidationError that echoes the input dict — which holds secrets.
        # `!= DEV_ENV`, like every sibling guard below: staging carries a live
        # key against a host that self-labels "Production", so an unauthenticated
        # console there is an unauthenticated console over real money. Only
        # sandbox — this app's notion of local development — may run open.
        if self.auth_mode == "disabled" and self.conduit_env != DEV_ENV:
            raise RuntimeError(
                f"AUTH_MODE=disabled is refused with CONDUIT_ENV={self.conduit_env}"
            )
        expected = CONDUIT_HOSTS[self.conduit_env]
        if self.conduit_api_base and self.conduit_api_base.rstrip("/") != expected:
            raise RuntimeError(
                f"CONDUIT_API_BASE {self.conduit_api_base!r} is not the allowlisted "
                f"host for CONDUIT_ENV={self.conduit_env} ({expected})"
            )
        # An adapter missing its own configuration is a boot failure, not a
        # runtime 401 nobody can explain.
        if self.auth_mode == "proxy" and not self.proxy_shared_secret.get_secret_value():
            raise RuntimeError("AUTH_MODE=proxy requires PROXY_SHARED_SECRET")
        if self.auth_mode == "oidc" and not (
            self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret.get_secret_value()
        ):
            raise RuntimeError(
                "AUTH_MODE=oidc requires OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET"
            )
        if (
            self.auth_mode == "oidc"
            and self.conduit_env == "production"
            and not self.oidc_redirect_uri.startswith("https://")
        ):
            raise RuntimeError(
                "CONDUIT_ENV=production requires an https OIDC_REDIRECT_URI: deriving it from "
                "the request would mean trusting forwarded headers for login"
            )
        # `/` or `""` would make ANY path anonymous: `app.auth.web` mounts the
        # callback as an anonymous prefix and the check is `path.startswith(...)`,
        # so a one-character value turns the whole console off. Two segments
        # minimum and no trailing slash, refused at boot rather than discovered.
        if self.auth_mode == "oidc" and not callback_path_ok(self.oidc_callback_path):
            raise RuntimeError(
                f"OIDC_CALLBACK_PATH {self.oidc_callback_path!r} is not usable: it must be an "
                "absolute path of at least two segments with no trailing slash (e.g. "
                "/auth/callback) — a shorter one would make every path anonymous"
            )
        if self.auth_mode == "oidc" and not https_or_loopback(self.oidc_issuer):
            raise RuntimeError(
                "OIDC_ISSUER must be https:// (a loopback http:// address is allowed for local "
                "development only) — the token exchange carries the client secret and the code"
            )
        if not self.cookie_secure and self.conduit_env != DEV_ENV:
            raise RuntimeError(
                f"COOKIE_SECURE=false is refused with CONDUIT_ENV={self.conduit_env}: the "
                "session and CSRF cookies would travel in plaintext"
            )
        # A ceiling nobody can parse, or one that refuses everything, is a
        # deployment mistake — and it is the setting whose whole job is to refuse
        # money, so it fails at boot rather than at the first payout.
        if self.money_ceiling.strip():
            try:
                value = Decimal(self.money_ceiling)
            except (InvalidOperation, ValueError):
                raise RuntimeError(
                    f"MONEY_CEILING {self.money_ceiling!r} is not a decimal number"
                ) from None
            if not value.is_finite() or value <= 0:
                raise RuntimeError(
                    f"MONEY_CEILING {self.money_ceiling!r} must be a positive finite number"
                )
        if not self.session_secret.get_secret_value():
            raise RuntimeError("SESSION_SECRET is empty")
        # Outside development every secret in play must actually be a secret.
        # The message names the setting and the reason, never the value.
        if self.conduit_env != DEV_ENV:
            required = [("SESSION_SECRET", self.session_secret)]
            if self.auth_mode == "proxy":
                required.append(("PROXY_SHARED_SECRET", self.proxy_shared_secret))
            if self.auth_mode == "oidc":
                required.append(("OIDC_CLIENT_SECRET", self.oidc_client_secret))
            if self.conduit_webhook_secret.get_secret_value():
                required.append(("CONDUIT_WEBHOOK_SECRET", self.conduit_webhook_secret))
            for name, secret in required:
                problem = weak_secret(secret.get_secret_value())
                if problem:
                    raise RuntimeError(
                        f"{name} is {problem} — refused with CONDUIT_ENV={self.conduit_env}"
                    )
        # Custom roles are config, so a bad one is a boot failure like any other
        # — never a role that silently grants less than the file says.
        permissions.role_table(self.roles_file, self.conduit_env)
        # Fail at boot, not at the first write. Errors carry the position only.
        if not self.encryption_keys:
            raise RuntimeError("ENCRYPTION_KEY is empty")
        for position, key in enumerate(self.encryption_keys, start=1):
            try:
                Fernet(key.strip())
            except Exception as exc:
                raise RuntimeError(
                    f"ENCRYPTION_KEY entry #{position} is not a valid Fernet key"
                ) from exc
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
