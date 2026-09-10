"""The industry-requirements sentinel's own guards (tests/e2e/12_…).

The probe watches for Conduit's announced industry-specific onboarding
requirements (relayed 2026-09-02; the parameter is silently ignored as of that
date). These tests keep its REFUSALS honest without any network: the hermetic
half of the sweep-script idiom (tests/test_counterparties.py's pattern).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "e2e" / "12_requirements_industry_probe.py"


def run(env_overrides: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--check-config"],
        env={**os.environ, **env_overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_probe_refuses_a_non_sandbox_key():
    refused = run({"CONDUIT_SANDBOX_API_KEY": "ck_live_not_a_sandbox_key",
                   "CONDUIT_SANDBOX_HOST": "https://api.sandbox.conduit.financial"})
    assert refused.returncode != 0
    assert "not a ck_sandbox_ key" in refused.stdout + refused.stderr


def test_the_probe_refuses_a_wrong_host():
    refused = run({"CONDUIT_SANDBOX_API_KEY": "ck_sandbox_fake_for_check_config",
                   "CONDUIT_SANDBOX_HOST": "https://api.conduit.financial"})
    assert refused.returncode != 0
    assert "CONDUIT_SANDBOX_HOST is not" in refused.stdout + refused.stderr


def test_check_config_passes_with_fake_credentials_and_sends_nothing():
    ok = run({"CONDUIT_SANDBOX_API_KEY": "ck_sandbox_fake_for_check_config",
              "CONDUIT_SANDBOX_HOST": "https://api.sandbox.conduit.financial"})
    assert ok.returncode == 0
    assert "read-only probe" in ok.stdout
