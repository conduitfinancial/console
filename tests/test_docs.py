"""The environment reference cannot fall behind `app/config.py`.

Documentation drift is the failure mode of a self-hosted app: a setting that
exists and is undocumented is a setting nobody sets, and a documented setting
that no longer exists is a support call. One assertion each, both cheap.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_README = (ROOT / "deploy" / "README.md").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()

NAMES = [name.upper() for name in Settings.model_fields]


def test_every_setting_is_in_the_deployment_reference():
    missing = [f"`{name}`" for name in NAMES if f"`{name}`" not in DEPLOY_README]
    assert not missing, f"undocumented settings: {missing}"


def test_every_setting_is_in_the_example_env_file():
    # Commented-out lines count: the example's job is to *name* every knob, with
    # the optional ones shown as their defaults.
    missing = [name for name in NAMES if name not in ENV_EXAMPLE]
    assert not missing, f"settings absent from .env.example: {missing}"
