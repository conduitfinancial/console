#!/usr/bin/env python
"""Onboarding live end-to-end: discovery → assembly → upload → 422 → submit → cancel.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…` (it used to
read the live staging pair and write with it). Every record it creates is
obviously synthetic (legal name prefixed `ZZZTEST Console E2E`) and the
application it submits is cancelled before the script exits.

    .venv/bin/python tests/e2e/02_onboarding.py

Safe to re-run: every run mints a fresh `clientReferenceId` and fresh
idempotency keys, so nothing collides with a previous one. The API key is read
from the environment and never printed.

What it cannot do here: approval, rejection and IDV links. Staging has no
`/v2/sandbox/*` simulate routes (verified 2026-08-28) and decisions there are
manual, so those branches are covered by the stubbed integration tests in
`tests/test_web_applications.py` instead — the script says so on the way out.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import forms  # noqa: E402
from app.onboarding.requirements import merge_policy_subjects  # noqa: E402

COUNTRY = "BGR"
FIXTURES = ROOT / "tests" / "fixtures"
PREFIX = "ZZZTEST Console E2E"
# A 300-byte PDF that is a real PDF (magic bytes matter — `app.documents.sniff`
# and Conduit both decide the type from the bytes).
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


SANDBOX_HOST = "https://api.sandbox.conduit.financial"


def env() -> tuple[str, str]:
    """The sandbox pair, or a refusal — the same guard `06_sandbox_sweep.py`
    carries, backported verbatim.

    This script used to read `SANDBOX_API_KEY`/`SANDBOX_HOST`, which in this
    engagement's `../.env` is a **live key against a production-labelled host**,
    and it writes. Nothing here is worth a real payout: a key that is not
    `ck_sandbox_…`, or any host but the sandbox, and the script exits before it
    opens a connection.
    """
    values = {}
    for line in (ROOT.parent / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    key = values.get("CONDUIT_SANDBOX_API_KEY") or ""
    host = (values.get("CONDUIT_SANDBOX_HOST") or "").rstrip("/")
    if not key.startswith("ck_sandbox_"):
        sys.exit("refusing to run: CONDUIT_SANDBOX_API_KEY is not a ck_sandbox_ key")
    if host != SANDBOX_HOST:
        sys.exit(f"refusing to run: CONDUIT_SANDBOX_HOST is not {SANDBOX_HOST}")
    return host, key


def say(step: str, detail: str = "") -> None:
    print(f"  {step:<34} {detail}", flush=True)


# --- synthetic answers ---------------------------------------------------------------


# Plausible shapes for the handful of free-text fields where "ZZZTEST …" would
# be obviously wrong data rather than obviously synthetic data. Matched on the
# field's own name tail — a script-local convenience, never app logic.
HINTS: tuple[tuple[str, str], ...] = (
    ("postalcode", "1000"),
    ("zip", "1000"),
    ("taxidnumber", "1234567890"),
    ("taxid", "1234567890"),
    ("state", "Sofia"),
    ("city", "Sofia"),
    ("licensenumber", "ZZZTEST-1234"),
)

# Enum picks where "the first allowed value" would be internally inconsistent
# with the rest of the synthetic record. `taxIdType` defaults to `SSN`, and
# Conduit then (rightly) demands a real US Social Security Number from a
# Bulgarian person — a cross-field rule discovery does not advertise.
ENUM_PREFERENCES = {"taxIdType": "NATIONAL_ID"}


def sample(field: forms.Field, today: date) -> object:
    """A plausible, obviously-synthetic answer that satisfies the field's own
    constraints. Discovery-driven: enums take their first allowed value, so no
    country's vocabulary is baked in here."""
    kind = field.effective_type
    constraints = field.constraints
    if field.must_equal is not None:
        return field.must_equal
    if field.allowed_values:
        first = field.allowed_values[0]
        preferred = ENUM_PREFERENCES.get(field.path[-1])
        if preferred in field.allowed_values:
            first = preferred
        if COUNTRY in field.allowed_values and kind == "country":
            first = COUNTRY
        return [first] if kind == "enumArray" else first
    if kind == "boolean":
        return False
    if kind == "date":
        years = constraints.min_age_years or 0
        return date(today.year - years - 1, 1, 15).isoformat()
    if kind == "email":
        return "zzztest.console.e2e@example.com"
    if kind == "phone":
        return "+15550100"
    if kind == "url":
        return "https://zzztest-console-e2e.example.com"
    if kind in ("number", "integer"):
        low = constraints.min if constraints.min is not None else 1
        return int(low) if kind == "integer" else float(low)
    if kind == "stringArray":
        return [f"{PREFIX} entry"]
    tail = field.path[-1].lower()
    text = next((value for hint, value in HINTS if hint in tail), f"{PREFIX} {field.path[-1]}")
    if constraints.pattern and not re.search(constraints.pattern, text):
        text = constraints.example or "ZZZTEST1"
    if constraints.max_length:
        text = text[: constraints.max_length].strip()
    if constraints.min_length and len(text) < constraints.min_length:
        text = text.ljust(constraints.min_length, "X")
    return text


def answer(model: forms.FormModel, document_id: str) -> forms.FormValues:
    """Fill every required field, then every field a required one gates on, until
    the engine's own validator is satisfied."""
    today = date.today()
    values = forms.FormValues(document_ids=[document_id])
    for _ in range(6):  # conditions cascade: answering one field reveals another
        for field in forms.active_fields(model, values):
            state = forms.field_state(field, values.root)
            if not state["required"] or forms.present(forms.lookup(field.path, values.root)):
                continue
            forms._set_path(values.root, field.path, sample(field, today))
        values.persons = [
            person
            for row in model.persons
            for person in _people(model, row, values, today, document_id)
        ]
        if forms.validate(model, values).ok:
            return values
    return values


def _people(model, row, values, today, document_id) -> list[forms.PersonValues]:
    existing = [p for p in values.persons if p.role == row.role]
    while len(existing) < max(row.min_count, 1):
        existing.append(forms.PersonValues(role=row.role))
    for person in existing:
        for field in forms.active_person_fields(row, person, values):
            state = forms.field_state(field, values.root, person.values)
            if not state["required"] or forms.present(forms.lookup(field.path, person.values)):
                continue
            forms._set_path(person.values, field.path, sample(field, today))
        # The row's own role when it names one; the catch-all `any` row takes a
        # different real role from the same vocabulary (see forms.person_roles).
        allowed = next((f.allowed_values or [] for f in row.fields if f.path == ("roles",)), [])
        if row.role != forms.ROLE_WILDCARD:
            person.values["roles"] = [row.role]
        elif len(allowed) > 1:
            person.values["roles"] = [allowed[1]]
        person.document_ids = [document_id]
    return existing


# --- the run -------------------------------------------------------------------------


def main() -> int:
    host, key = env()
    reference = f"zzztest-console-e2e-{uuid.uuid4().hex[:12]}"
    print(f"\nConduit Console — onboarding e2e\n  host {host}  reference {reference}\n")

    with httpx.Client(
        base_url=f"{host}/v2", headers={"x-api-key": key}, timeout=60.0
    ) as client:
        # 1. discovery: requirements + both policy-subject catalogs, merged into
        #    the one snapshot a draft would pin.
        requirements = client.get("/onboarding/requirements", params={"country": COUNTRY})
        requirements.raise_for_status()
        snapshot = requirements.json()
        for axis in ("INDUSTRY", "REGULATED_ACTIVITY"):
            catalog = client.get("/onboarding/policy-subjects", params={"axis": axis})
            catalog.raise_for_status()
            merge_policy_subjects(snapshot, catalog.json().get("data") or [])
        model = forms.parse(snapshot)
        say("discovery", f"schemaVersion {model.schema_version}, {len(model.fields)} fields, "
                         f"{len(model.persons)} person rows, {len(model.documents)} documents")

        # 2. one synthetic document.
        upload = client.post(
            "/documents",
            files={"file": ("zzztest-console-e2e.pdf", PDF, "application/pdf")},
            data={"purpose": "organization_onboarding", "name": f"{PREFIX} evidence"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if upload.status_code >= 400:
            say("document upload FAILED", f"{upload.status_code} {upload.text[:300]}")
            return 1
        document_id = upload.json().get("id", "")
        say("document uploaded", document_id)

        # 3. a body the engine itself calls valid.
        values = answer(model, document_id)
        errors = forms.validate(model, values)
        if not errors.ok:
            say("local validation FAILED", json.dumps(
                {name: [m.detail for m in messages] for name, messages in errors.fields.items()}
                | {"form": [m.detail for m in errors.form]}, indent=1)[:1200])
            return 1
        body = forms.assemble(model, values)
        body["businessInfo"]["legalName"] = f"{PREFIX} {reference[-8:]} EOOD"
        body["clientReferenceId"] = reference
        say("assembled body", f"{len(json.dumps(body))} bytes, "
                              f"{len(body.get('ownership', {}).get('persons', []))} persons")

        # 4. the long-awaited 422: submit with a required section missing.
        captured = None
        for omit in ("ownership", "companyClassification", "registeredAddress"):
            if omit not in body:
                continue
            incomplete = {k: v for k, v in body.items() if k != omit}
            response = client.post(
                "/onboarding", json=incomplete, headers={"Idempotency-Key": str(uuid.uuid4())}
            )
            say(f"incomplete submit (no {omit})", f"HTTP {response.status_code}")
            if response.status_code == 422 and isinstance(response.json().get("errors"), list):
                captured = response.json()
                break
            if response.status_code < 400:
                say("UNEXPECTED", "an incomplete body was accepted — investigate before trusting")
                return 1
        if captured is None:
            say("422 capture FAILED", "no ValidationErrorDto came back; fixture not written")
            return 1
        target = FIXTURES / "validation_error_422.json"
        target.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
        say("422 fixture captured", f"{target.name} — keys {sorted(captured)}, "
                                    f"{len(captured['errors'])} errors")

        # 5. the real submission.
        submitted = client.post(
            "/onboarding", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        if submitted.status_code >= 400:
            problem = submitted.json() if submitted.headers.get("content-type", "").startswith(
                "application/json"
            ) else {}
            say("submit FAILED", f"{submitted.status_code} {problem.get('type')}")
            for error in problem.get("errors") or []:
                say("", f"{error.get('pointer')}: {error.get('detail')}")
            return 1
        application = submitted.json()
        application_id = application["id"]
        say("submitted", f"{application_id} status={application.get('status')} "
                         f"HTTP {submitted.status_code}")

        # 6. follow it until Conduit is working on it.
        for _ in range(12):
            current = client.get(f"/applications/{application_id}").json()
            if current.get("status") != "pending":
                break
            client.get("/applications", params={"limit": 1})  # spacing, not a tight loop
        say("polled", f"status={current.get('status')} persons="
                      f"{[p.get('referenceId') for p in current.get('persons') or []]}")

        # 7. clean up after ourselves.
        cancelled = client.post(f"/applications/{application_id}/cancel")
        say("cancel", f"HTTP {cancelled.status_code}")
        final = client.get(f"/applications/{application_id}").json()
        say("final status", final.get("status", "?"))
        ok = final.get("status") == "cancelled"

    print(
        "\n  Not exercisable on this host: approval, rejection-with-category and IDV links.\n"
        "  Staging has no /v2/sandbox/* simulate routes and decides applications manually;\n"
        "  those branches are asserted in tests/test_web_applications.py against stubs.\n"
    )
    print(f"  result: {'PASS' if ok else 'FAIL'}  application {application_id}  ref {reference}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
