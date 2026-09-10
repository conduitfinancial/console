#!/usr/bin/env python3
"""Report material drift between the pinned OpenAPI snapshot and the live spec.

    python scripts/check_openapi_drift.py            # fetch live, compare, exit 1 on drift
    python scripts/check_openapi_drift.py --update   # re-pin (review the diff first!)

"Material" means: endpoints that appeared or vanished, and — for the DTOs this
app actually consumes — properties and closed value sets that changed. A generic
JSON differ would drown the signal in description edits, so this walks only what
we depend on. Everything else drifts silently on purpose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

LIVE_SPEC_URL = "https://api.conduit.financial/v2/api-docs/openapi.json"
PINNED = Path(__file__).resolve().parent.parent / "contracts" / "openapi_production.json"

# Schemas whose shape we parse or render. `status` entries are the enums the UI
# must never be surprised by (plan v2 §7 "unknown statuses").
WATCHED_SCHEMAS = (
    "ProblemDetailDto",
    "ValidationErrorDto",
    "RateLimitedErrorDto",
    "PayoutRequirementsResponseDto",
    "QuoteRedemptionOrderDto",
    "OrderExternalResponseDto",  # order status enum
    "PublicWithdrawalViewDto",  # payout/transaction status enum
    "CustomerOnboardingApplicationDto",  # application status enum
    "WhitelistRecipientResponseDto",
)


def _walk(node: Any, pointer: str, out: dict[str, str]) -> None:
    """Flatten a schema into `pointer -> fingerprint` entries for properties and
    enums. Nested objects, arrays and oneOf/anyOf branches are followed; prose
    (description, example) is ignored."""
    if not isinstance(node, dict):
        return
    if enum := node.get("enum"):
        out[f"{pointer} enum"] = ", ".join(sorted(map(str, enum)))
    for name, child in (node.get("properties") or {}).items():
        out[f"{pointer}/{name}"] = str(child.get("type") or child.get("$ref") or "any")
        _walk(child, f"{pointer}/{name}", out)
    if required := node.get("required"):
        out[f"{pointer} required"] = ", ".join(sorted(required))
    _walk(node.get("items"), f"{pointer}[]", out)
    for key in ("oneOf", "anyOf", "allOf"):
        for index, branch in enumerate(node.get(key) or []):
            _walk(branch, f"{pointer}({key}{index})", out)


def _endpoints(spec: dict) -> set[str]:
    return {
        f"{method.upper()} {path}"
        for path, item in (spec.get("paths") or {}).items()
        for method in item
        if method in ("get", "post", "put", "patch", "delete")
    }


def _fingerprint(spec: dict, name: str) -> dict[str, str]:
    schema = (spec.get("components", {}).get("schemas") or {}).get(name)
    if schema is None:
        return {}
    out: dict[str, str] = {}
    _walk(schema, name, out)
    return out


def diff(pinned: dict, live: dict) -> list[str]:
    """Human-readable drift lines; empty means the pin is still accurate."""
    findings = []
    old, new = _endpoints(pinned), _endpoints(live)
    findings += [f"endpoint removed: {e}" for e in sorted(old - new)]
    findings += [f"endpoint added:   {e}" for e in sorted(new - old)]

    for name in WATCHED_SCHEMAS:
        before, after = _fingerprint(pinned, name), _fingerprint(live, name)
        if before and not after:
            findings.append(f"schema removed: {name}")
            continue
        if after and not before:
            findings.append(f"schema added: {name}")
            continue
        for key in sorted(set(before) | set(after)):
            was, now = before.get(key), after.get(key)
            if was == now:
                continue
            if was is None:
                findings.append(f"{name}: added {key} ({now})")
            elif now is None:
                findings.append(f"{name}: removed {key} (was {was})")
            else:
                findings.append(f"{name}: changed {key}: {was} -> {now}")
    return findings


def fetch(url: str = LIVE_SPEC_URL) -> dict:
    return httpx.get(url, timeout=60, follow_redirects=True).raise_for_status().json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="overwrite the pinned snapshot")
    parser.add_argument("--url", default=LIVE_SPEC_URL)
    args = parser.parse_args()

    live = fetch(args.url)
    if args.update:
        PINNED.write_text(json.dumps(live, indent=1, sort_keys=True) + "\n")
        print(f"pinned {args.url} -> {PINNED}")
        return 0

    findings = diff(json.loads(PINNED.read_text()), live)
    if not findings:
        print(f"no material drift ({len(_endpoints(live))} endpoints, "
              f"{len(WATCHED_SCHEMAS)} watched schemas)")
        return 0
    print(f"OpenAPI drift — {len(findings)} material difference(s) vs {PINNED.name}:")
    for line in findings:
        print(f"  - {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
