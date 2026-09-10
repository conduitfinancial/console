"""Pinned OpenAPI snapshot + drift report (plan v2 §4, §8 CI).

No network here: the live fetch is the script's job, the comparison is ours.
"""

from __future__ import annotations

import copy
import json

from scripts.check_openapi_drift import PINNED, WATCHED_SCHEMAS, diff


def spec() -> dict:
    return json.loads(PINNED.read_text())


def test_the_pinned_snapshot_is_the_spec_we_built_against():
    pinned = spec()
    assert pinned["openapi"].startswith("3.")
    assert {s["url"] for s in pinned["servers"]} == {
        "https://api.conduit.financial/v2",
        "https://api.sandbox.conduit.financial/v2",
    }
    for path in ("/payouts", "/orders", "/onboarding", "/transactions", "/applications"):
        assert path in pinned["paths"]
    for name in WATCHED_SCHEMAS:
        assert name in pinned["components"]["schemas"]


def test_a_spec_does_not_drift_from_itself():
    assert diff(spec(), spec()) == []


def test_drift_reports_added_and_removed_endpoints():
    doctored = spec()
    doctored["paths"].pop("/payouts")
    doctored["paths"]["/teleports"] = {"post": {}}

    findings = diff(spec(), doctored)
    assert "endpoint removed: POST /payouts" in findings
    assert "endpoint added:   POST /teleports" in findings


def test_drift_reports_enum_and_property_changes_on_watched_dtos():
    doctored = spec()
    schemas = doctored["components"]["schemas"]
    schemas["OrderExternalResponseDto"]["properties"]["status"]["enum"] = [
        "pending",
        "succeeded",
        "failed",
        "cancelled",
        "expired",  # the status the plan says does not exist — we want to hear about it
    ]
    schemas["ProblemDetailDto"]["properties"].pop("resolution")
    schemas["ValidationErrorDto"]["properties"]["errors"]["items"]["properties"]["category"][
        "enum"
    ] = ["field"]

    findings = diff(spec(), doctored)
    assert any("OrderExternalResponseDto" in f and "expired" in f for f in findings)
    assert any(f.startswith("ProblemDetailDto: removed") and "resolution" in f for f in findings)
    assert any("ValidationErrorDto" in f and "category enum" in f for f in findings)


def test_drift_ignores_prose_edits():
    doctored = spec()
    problem = doctored["components"]["schemas"]["ProblemDetailDto"]["properties"]
    problem["detail"]["description"] = "Reworded by the API team."
    problem["title"]["example"] = "Something Else"
    assert diff(spec(), doctored) == []


def test_a_removed_schema_is_reported_once():
    doctored = copy.deepcopy(spec())
    doctored["components"]["schemas"].pop("PayoutRequirementsResponseDto")
    assert diff(spec(), doctored) == ["schema removed: PayoutRequirementsResponseDto"]
