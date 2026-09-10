"""`X-Conduit-Signature` verification (OPERATIONS_SPEC §4, plan v2 §5).

    X-Conduit-Signature: t=<unix-seconds>,v1=<hex>[,v1=<hex>]

Each `v1` is HMAC-SHA256 over `"{t}.{rawBody}"`. Two things the pinned spec is
emphatic about, and both are silent failures if you get them wrong:

* the key is the **full** secret, `whsec_` prefix included;
* the digest is over the **raw request bytes** — a re-serialized body changes
  key order and whitespace, and every delivery then looks tampered with.

Multiple `v1` segments appear during a secret rotation grace window (one per
currently-valid secret); any match is a pass.
"""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-Conduit-Signature"
TOLERANCE_SECONDS = 300


def verify(
    raw_body: bytes,
    header: str | None,
    secret: str,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE_SECONDS,
) -> bool:
    """True only for a well-formed, in-window header with a matching digest."""
    if not header or not secret:
        return False

    timestamp: str | None = None
    candidates: list[str] = []
    for segment in header.split(","):
        name, _, value = segment.strip().partition("=")
        if name == "t":
            timestamp = value
        elif name == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - signed_at) > tolerance:
        return False

    # The timestamp is fed to the HMAC exactly as it arrived, not as re-rendered
    # from the parsed int — leading zeros or a `+` would change the digest.
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    # A non-ASCII candidate is just a malformed header — but `compare_digest`
    # raises on one rather than returning False, so it is filtered out here
    # instead of being handed to it.
    return any(
        candidate.isascii() and hmac.compare_digest(expected, candidate)
        for candidate in candidates
    )
