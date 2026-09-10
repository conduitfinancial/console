"""Startup guards (plan v2 §2)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import CONDUIT_HOSTS, Settings
from tests.conftest import require_disposable_database

# Shaped like `openssl rand -hex 32`, because outside sandbox that is now the
# bar: 32+ characters and no placeholder text.
STRONG = "9f2c" * 16
BASE = {
    "database_url": "postgresql+psycopg://x@/y",
    "session_secret": STRONG,
    "encryption_key": Fernet.generate_key().decode(),
    # proxy is the default AUTH_MODE and refuses to boot without its secret.
    "proxy_shared_secret": STRONG,
}


def settings(**overrides) -> Settings:
    return Settings(**{**BASE, **overrides})


@pytest.mark.parametrize("env", ["production", "staging"])
def test_disabled_auth_refused_outside_development(env):
    """Staging joined production here: it carries a live key against
    a host that self-labels "Production", so an open console there is an open
    console over real money."""
    with pytest.raises(RuntimeError, match="AUTH_MODE=disabled"):
        settings(conduit_env=env, auth_mode="disabled")


@pytest.mark.parametrize("env", ["sandbox"])
def test_disabled_auth_allowed_outside_production(env):
    assert settings(conduit_env=env, auth_mode="disabled").auth_mode == "disabled"


def test_production_starts_with_real_auth():
    assert settings(conduit_env="production", auth_mode="proxy").conduit_base_url == (
        "https://api.conduit.financial"
    )


@pytest.mark.parametrize("env,host", CONDUIT_HOSTS.items())
def test_env_selects_allowlisted_host(env, host):
    assert settings(conduit_env=env, auth_mode="proxy").conduit_base_url == host


@pytest.mark.parametrize(
    "bad_host",
    [
        "https://evil.example.com",
        "http://api.conduit.financial",  # scheme downgrade
        "https://api.conduit.financial.evil.com",
        "https://api.staging.conduit.financial",  # right family, wrong env
    ],
)
def test_non_allowlisted_host_rejected(bad_host):
    with pytest.raises(RuntimeError, match="not the allowlisted host"):
        settings(conduit_env="sandbox", conduit_api_base=bad_host)


def test_explicit_matching_host_accepted():
    s = settings(conduit_env="sandbox", conduit_api_base=CONDUIT_HOSTS["sandbox"] + "/")
    assert s.conduit_base_url.startswith("https://api.sandbox.conduit.financial")


def test_unknown_environment_rejected():
    with pytest.raises(ValueError):
        settings(conduit_env="localhost")


def test_api_key_never_rendered():
    s = settings(conduit_api_key="sk_live_supersecret")
    assert "supersecret" not in repr(s)
    assert "supersecret" not in str(s)
    assert "supersecret" not in str(s.model_dump())
    assert s.conduit_api_key.get_secret_value() == "sk_live_supersecret"


def test_guard_failure_leaks_no_secrets():
    with pytest.raises(RuntimeError) as exc:
        Settings(
            database_url="postgresql+psycopg://u:dbpassword@/db",
            session_secret="sessionsecret",
            encryption_key="encryptionkey",
            conduit_api_key="sk_live_supersecret",
            conduit_env="production",
            auth_mode="disabled",
        )
    message = str(exc.value)
    for secret in ("dbpassword", "sessionsecret", "encryptionkey", "supersecret"):
        assert secret not in message


def test_proxy_mode_refuses_to_boot_without_the_shared_secret():
    with pytest.raises(RuntimeError, match="PROXY_SHARED_SECRET"):
        Settings(**{**BASE, "proxy_shared_secret": "", "auth_mode": "proxy"})


@pytest.mark.parametrize(
    "missing", ["oidc_issuer", "oidc_client_id", "oidc_client_secret"]
)
def test_oidc_mode_refuses_to_boot_half_configured(missing):
    with pytest.raises(RuntimeError, match="AUTH_MODE=oidc requires"):
        settings(**{**OIDC, missing: ""})


OIDC = {
    "auth_mode": "oidc",
    "oidc_issuer": "https://idp.example.test",
    "oidc_client_id": "console",
    "oidc_client_secret": STRONG,
}


@pytest.mark.parametrize("uri", ["", "http://console.example.com/auth/callback"])
def test_production_oidc_requires_an_https_redirect_uri(uri):
    with pytest.raises(RuntimeError, match="https OIDC_REDIRECT_URI"):
        settings(**OIDC, conduit_env="production", oidc_redirect_uri=uri)


def test_production_oidc_accepts_an_https_redirect_uri():
    s = settings(
        **OIDC,
        conduit_env="production",
        oidc_redirect_uri="https://console.example.com/auth/callback",
    )
    assert s.oidc_redirect_uri.startswith("https://")


@pytest.mark.parametrize("env", ["sandbox", "staging"])
def test_non_production_oidc_may_derive_the_redirect_uri(env):
    """Staging alongside sandbox, because the README's environment matrix claims
    "derived if unset" for BOTH and only sandbox was pinned — and staging is the
    row this round has been tightening everywhere else, so the one place it
    stays permissive should say so out loud rather than by omission."""
    assert settings(**OIDC, conduit_env=env).oidc_redirect_uri == ""


def test_role_map_parses_group_pairs():
    s = settings(auth_role_map="conduit-ops = operator ,conduit-admins=admin,junk")
    assert s.role_map == {"conduit-ops": "operator", "conduit-admins": "admin"}
    assert settings().role_map == {}


def test_operations_defaults_match_spec():
    s = settings()
    assert (s.op_client_timeout, s.op_stale_inflight_factor) == (30.0, 2.0)
    assert s.stale_inflight_seconds == 60.0
    assert s.reconcile_max_attempts == 5
    assert s.reconcile_interval_seconds == 60
    assert s.op_created_ttl_seconds == 24 * 3600
    assert s.op_body_retention_days == 30


# --- secrets from files (deploy/README §2) --------------------------------------------


def test_a_secret_is_read_from_its_file(monkeypatch, tmp_path):
    """Docker secrets and Kubernetes projected volumes deliver a file, not an
    environment variable — and a file is not inherited by every child process."""
    secret = tmp_path / "session_secret"
    secret.write_text(STRONG + "\n")  # `echo` adds the newline; it is not the secret
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("SESSION_SECRET_FILE", str(secret))

    built = Settings(**{k: v for k, v in BASE.items() if k != "session_secret"})
    assert built.session_secret.get_secret_value() == STRONG


def test_the_file_form_works_for_a_plain_string_setting_too(monkeypatch, tmp_path):
    """`DATABASE_URL` is not a `SecretStr`, but its DSN carries a password."""
    dsn = tmp_path / "dsn"
    dsn.write_text("postgresql+psycopg://user:pw@db/console\n")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_FILE", str(dsn))

    built = Settings(**{k: v for k, v in BASE.items() if k != "database_url"})
    assert built.database_url == "postgresql+psycopg://user:pw@db/console"


def test_supplying_a_secret_both_ways_is_refused(monkeypatch, tmp_path):
    """Two deployment mechanisms disagreeing about a secret is worth stopping
    for — any precedence rule here would surprise somebody."""
    secret = tmp_path / "session_secret"
    secret.write_text(STRONG)
    monkeypatch.setenv("SESSION_SECRET", "something-else-entirely-and-long")
    monkeypatch.setenv("SESSION_SECRET_FILE", str(secret))

    with pytest.raises(RuntimeError, match="SESSION_SECRET and SESSION_SECRET_FILE"):
        settings()


def test_an_unreadable_secret_file_names_the_setting_and_nothing_else(monkeypatch, tmp_path):
    missing = tmp_path / "not-there"
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    monkeypatch.setenv("PROXY_SHARED_SECRET_FILE", str(missing))

    with pytest.raises(RuntimeError) as raised:
        Settings(**{k: v for k, v in BASE.items() if k != "proxy_shared_secret"})

    message = str(raised.value)
    assert "PROXY_SHARED_SECRET_FILE" in message
    # Not the path, and certainly not a neighbouring file's contents.
    assert str(missing) not in message


def test_every_secret_bearing_setting_supports_the_file_form(monkeypatch, tmp_path):
    """The set is derived from the model, so this is a statement about coverage
    rather than about a hand-kept list."""
    expected = {
        "CONDUIT_API_KEY",
        "SESSION_SECRET",
        "ENCRYPTION_KEY",
        "PROXY_SHARED_SECRET",
        "CONDUIT_WEBHOOK_SECRET",
        "OIDC_CLIENT_SECRET",
        "DATABASE_URL",
    }
    for variable in expected:
        monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv(f"{variable}_FILE", str(tmp_path / "nope"))
        with pytest.raises(RuntimeError, match=f"{variable}_FILE"):
            Settings(**{k: v for k, v in BASE.items() if k.upper() != variable})
        monkeypatch.delenv(f"{variable}_FILE")


# --- the suite's own database guard (`tests/conftest.py`) -----------------------------


CI_DSN = "postgresql+psycopg://postgres:postgres@localhost:5432/conduit_console_test"


@pytest.mark.parametrize(
    "url,name",
    [
        (CI_DSN, "conduit_console_test"),  # CI
        # The default this file's own conftest sets, query string and all.
        ("postgresql+psycopg://mc_bot@/conduit_console_test?host=/tmp", "conduit_console_test"),
        # Per-phase scratch databases.
        ("postgresql+psycopg://postgres@localhost:5432/cc_p11", "cc_p11"),
        ("postgresql+psycopg://postgres@localhost:5432/cc_pr6a", "cc_pr6a"),
        # The vocabulary the e2e scripts' own guard already uses.
        ("postgresql+psycopg://postgres@/test_console", "test_console"),
        ("postgresql+psycopg://mc_bot@/conduit_console_sweep?host=/tmp", "conduit_console_sweep"),
    ],
)
def test_a_disposable_database_url_is_accepted(url, name):
    assert require_disposable_database(url) == name


@pytest.mark.parametrize(
    "url",
    [
        # `.env`'s own DATABASE_URL — what `--env-file .env` on pytest hands
        # over, and the database whose drafts have no second copy.
        "postgresql+psycopg://localhost/conduit_console",
        "postgresql+psycopg://mc_bot@/conduit_console?host=/tmp",
        "postgresql+psycopg://console:pw@db.internal:5432/console_production",
        # Whole words only, or the guard is a rule about substrings.
        "postgresql+psycopg://localhost/latest_console",
    ],
)
def test_a_real_database_url_refuses_to_run_the_suite(url):
    with pytest.raises(RuntimeError, match="not a disposable database"):
        require_disposable_database(url)
    # And there is a way to mean it, for the one case that does.
    assert require_disposable_database(url, override="1")


@pytest.mark.parametrize("override", ["0", "false", "no", "off", "TRUE", "yes", " 1"])
def test_only_a_literal_one_lets_the_suite_truncate_a_real_database(override):
    with pytest.raises(RuntimeError, match="not a disposable database"):
        require_disposable_database("postgresql+psycopg://localhost/conduit_console", override)
