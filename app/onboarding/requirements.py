"""Onboarding discovery: the snapshot a draft pins (plan v2 §6/§7).

One fetch produces one immutable snapshot: `GET /v2/onboarding/requirements` for
the country, with both policy-subject catalogs folded into the fields they
describe. The catalogs are part of the *snapshot*, not a lookup performed at
render time — a draft that started against one catalog must keep rendering the
labels it was answered with, exactly as it keeps its own requirements payload
(`drafts.model`).

A catalog that cannot be read fails the whole fetch. Continuing without it would
pin a snapshot that is permanently missing labels, and nothing later re-fetches.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.conduit.client import Result, Success

AXES = ("INDUSTRY", "REGULATED_ACTIVITY")


def merge_policy_subjects(snapshot: dict, entries: list[dict]) -> dict:
    """Give every `allowedValues` key the catalog's label and description.

    Discovery currently inlines these already, so this is usually a no-op — it
    exists because the two are separate endpoints and only one of them is
    promised to carry display text. Matching is by key, so no field name is
    hardcoded: a catalog entry lands wherever an enum offers that value.
    """
    catalog = {
        str(e["key"]): e for e in entries if isinstance(e, Mapping) and e.get("key") is not None
    }
    if not catalog:
        return snapshot
    for field in snapshot.get("fields") or []:
        allowed = field.get("allowedValues") or []
        options = list(field.get("options") or [])
        known = {o.get("value") for o in options if isinstance(o, Mapping)}
        added = [
            {
                "value": key,
                "label": catalog[key].get("label") or key,
                "description": catalog[key].get("description"),
            }
            for key in allowed
            if key in catalog and key not in known
        ]
        if added:
            field["options"] = options + added
    return snapshot


async def fetch_snapshot(client, country: str) -> dict | Result:
    """The pinned snapshot for `country`, or the failing `Result` unchanged so
    the route can render Conduit's own problem-detail."""
    result = await client.get("/v2/onboarding/requirements", country=country)
    if not isinstance(result, Success) or not isinstance(result.data, dict):
        return result
    snapshot = result.data
    for axis in AXES:
        catalog = await client.get("/v2/onboarding/policy-subjects", axis=axis)
        if not isinstance(catalog, Success) or not isinstance(catalog.data, dict):
            return catalog
        merge_policy_subjects(snapshot, catalog.data.get("data") or [])
    return snapshot
