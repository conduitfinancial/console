#!/usr/bin/env python
"""Does `GET /v2/onboarding/requirements` vary by industry yet? Exit says.

Conduit has said industry-specific onboarding requirements are coming (relayed
2026-09-02), and the vocabulary groundwork is visibly mid-rollout: the
policy-subjects industry catalog holds 71 entries while requirements still
constrain the field to the old 15 (CONDUIT_FINDINGS addendum, 2026-09-02).
Probed the same day: `?industry=` / `?coreIndustry=` / `?primaryIndustry=` are
all **silently ignored** — 200 with byte-identical requirements for every value.

The console deliberately builds nothing speculative on this (the codebase's own
rule: no branches on response shapes that do not exist yet — sending a silently
ignored parameter is how an integration breaks LATER, when the parameter starts
meaning something). This probe is the sentinel instead:

    exit 0  ignored-and-identical — today's baseline; nothing to do
    exit 2  requirements VARY by industry — build the onboarding refetch:
            the wizard re-fetches and reshapes on industry change, the payout
            route-row idiom
    exit 3  the parameter is refused or the response shape changed — the
            rollout took a different form; read what it says before building

    .venv/bin/python tests/e2e/12_requirements_industry_probe.py

Read-only: three GETs against the requirements endpoint, nothing written
anywhere. Same credential guards as its siblings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_HOST = "https://api.sandbox.conduit.financial"

# Two industries far apart in the 71-entry catalog, plus the baseline. If
# industry-specific requirements exist for anything, a fintech and a waste
# hauler are a reasonable bet to differ.
PROBES = ("fintech_companies", "waste_management")
PARAMS = ("industry", "coreIndustry", "primaryIndustry")
COUNTRY = "BGR"


def load_env() -> str:
    values = {}
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    key = os.environ.get("CONDUIT_SANDBOX_API_KEY") or values.get("CONDUIT_SANDBOX_API_KEY") or ""
    host = (os.environ.get("CONDUIT_SANDBOX_HOST") or values.get("CONDUIT_SANDBOX_HOST") or "").rstrip("/")
    if not key.startswith("ck_sandbox_"):
        sys.exit("refusing to run: CONDUIT_SANDBOX_API_KEY is not a ck_sandbox_ key")
    if host != SANDBOX_HOST:
        sys.exit(f"refusing to run: CONDUIT_SANDBOX_HOST is not {SANDBOX_HOST}")
    return key


KEY = load_env()

if "--check-config" in sys.argv:
    print(f"config ok: {SANDBOX_HOST}, read-only probe")
    sys.exit(0)


def get(path: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"{SANDBOX_HOST}{path}", headers={"x-api-key": KEY})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


def digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]


def main() -> int:
    status, baseline = get(f"/v2/onboarding/requirements?country={COUNTRY}")
    if status != 200:
        print(f"baseline requirements read failed: HTTP {status} — cannot probe")
        return 3
    base = digest(baseline)
    print(f"baseline {COUNTRY}: {len(baseline.get('fields', []))} fields, digest {base}")

    varied = refused = False
    for param in PARAMS:
        for industry in PROBES:
            status, body = get(
                f"/v2/onboarding/requirements?country={COUNTRY}&{param}={industry}"
            )
            if status != 200:
                detail = str((body.get("errors") or [{}])[0].get("detail", ""))[:80]
                print(f"?{param}={industry}: HTTP {status} {detail}")
                refused = True
                continue
            d = digest(body)
            marker = "identical" if d == base else "*** DIFFERENT ***"
            print(f"?{param}={industry}: 200, digest {d} ({marker})")
            varied = varied or d != base

    if varied:
        print(
            "\n*** REQUIREMENTS VARY BY INDUSTRY *** — build the onboarding refetch"
            " (industry change re-fetches and reshapes, the payout route-row idiom)."
        )
        return 2
    if refused:
        print(
            "\nThe parameter is REFUSED now (it was silently ignored on 2026-09-02)"
            " — the rollout took a different shape; read the refusal before building."
        )
        return 3
    print("\nBASELINE UNCHANGED — the parameter is still silently ignored; nothing to build yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
