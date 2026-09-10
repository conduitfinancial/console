#!/usr/bin/env python
"""Payments live end-to-end: payout requirements, quotes, payout, whitelist.

Runs against **api.sandbox.conduit.financial only** — the script refuses any
other host and any key that is not `ck_sandbox_…` (it used to
read the live staging pair and post payouts with it). Every write is either
cancelled or revoked before the script exits, so the run leaves no residue:

    payout   → 202 pending → POST /payouts/{id}/cancel   → cancelled
    whitelist→ 201 pending_review → DELETE …/{id}        → revoked / gone

    .venv/bin/python tests/e2e/04_payments.py

The render-path checks go through the app's own functions (`app.payments`,
`app.forms`), not re-implementations, so what this proves is what an operator's
page would show. HTTP is raw httpx — no database, no settings, no ASGI app.

Fresh idempotency keys per run; the API key is read from the environment and
never printed. All synthetic data is prefixed ZZZTEST.

Staging has **no `/v2/sandbox/*` routes** (verified live), so the payout
review-approve / review-reject / settle simulators cannot be exercised here —
their wire bodies are asserted against a stub in
`tests/test_web_transactions.py` instead. The same goes for the whitelist
approve/reject simulators: a registration stays `pending_review` on this host.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import accounts, forms, payments  # noqa: E402
from app.conduit.client import parse_problem  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

# A 300-byte file that really is a PDF — both `app.documents.sniff` and Conduit
# decide the type from the bytes, not from the name.
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

# Synthetic but *valid* coordinates: 021000021 is a real ABA and passes the
# checksum both this console and Conduit's DTO enforce.
ABA = "021000021"
ACCOUNT_NUMBER = "000123456789"

# The payout **create** leg is opt-in: on this staging host a cancel is accepted
# but never completes, so an unguarded run strands one payout per invocation.
# Set `E2E_CREATE_PAYOUT=1` on a host whose sandbox routes can resolve a review.
CREATE_PAYOUT = os.environ.get("E2E_CREATE_PAYOUT") == "1"


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


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def capture(name: str, payload: object) -> None:
    (FIXTURES / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    say("fixture captured", name)


def answer(model: forms.FormModel, run: str) -> forms.FormValues:
    """Fill discovery's payout form the way an operator would.

    Script-local and deliberately dumb: discovery's first allowed value for
    anything enumerated, a real ABA where a validator says so, a US address
    where the pre-checks want one, and ZZZTEST text everywhere else.
    """
    values = forms.FormValues()
    for field in model.fields:
        leaf = field.path[-1]
        if field.allowed_values:
            value = field.allowed_values[0]
        elif field.validator == "aba":
            value = ABA
        elif leaf == "accountNumber":
            value = ACCOUNT_NUMBER
        elif leaf == "country":
            value = "US"
        elif leaf == "postalCode":
            value = "10010"
        elif leaf == "city":
            value = "New York"
        elif leaf == "state":
            value = "US-NY"
        elif leaf == "addressLine1":
            value = "500 Market St"
        elif leaf == "legalName":
            value = f"ZZZTEST Globex Supplies {run}"
        elif leaf in ("firstName", "lastName"):
            value = "ZZZTEST"
        elif leaf == "reference":
            value = f"ZZZTEST-{run}"
        elif field.required:
            value = f"ZZZTEST {run}"
        else:
            continue
        forms._set_path(values.root, field.path, value)
    return values


def errors_of(errors: forms.FormErrors) -> str:
    return json.dumps(
        {name: [m.detail for m in messages] for name, messages in errors.fields.items()}
        | {"form": [m.detail for m in errors.form]}
        | {"documents": [m.detail for m in errors.documents]},
        indent=1,
    )


ROUTES = [
    # (purpose, rail, recipientType, destinationCountry) — two polarities of the
    # same gate pair, which is the whole point of the metadata being discovered.
    ("payment_for_goods_or_services", "fedwire", "business", "USA"),
    ("intercompany", "fedwire", "business", "USA"),
    ("payroll", "ach", "individual", "USA"),
    ("payment_for_goods_or_services", "swift", "business", "DEU"),
]


def main() -> int:  # noqa: C901 — a linear script, read top to bottom
    host, key = env()
    run = uuid.uuid4().hex[:12]
    print(f"\nConduit Console — payments live e2e\n  host {host}  run {run}\n")
    ok = True

    with httpx.Client(base_url=f"{host}/v2", headers={"x-api-key": key}, timeout=60.0) as http:
        # --- (a) route metadata drives the form model ---------------------------------
        print("(a) payout requirements — does the metadata drive the form?")
        models: dict[tuple, forms.FormModel] = {}
        snapshots: dict[tuple, dict] = {}
        for purpose, rail, recipient_type, country in ROUTES:
            response = http.get(
                "/payouts/requirements",
                params={
                    "purpose": purpose,
                    "rail": rail,
                    "recipientType": recipient_type,
                    "destinationCountry": country,
                },
            )
            label = f"{purpose}/{rail}/{recipient_type}/{country}"
            if response.status_code >= 400:
                problem = parse_problem(response)
                say(f"  {rail} {purpose[:18]}", f"HTTP {response.status_code} {problem.type}")
                continue
            snapshot = response.json()
            model = payments.payout_model(snapshot)
            models[(purpose, rail, recipient_type, country)] = model
            snapshots[(purpose, rail, recipient_type, country)] = snapshot
            gates = (
                f"whitelist={model.whitelist.get('required')} "
                f"documentation={model.documentation.get('required')} "
                f"blocked={len(model.blocked_jurisdictions)}"
            )
            say(f"  {rail} {purpose[:18]}", f"{len(model.fields)} fields · {gates}")
            if model.warnings:
                say("  WARNINGS", "; ".join(model.warnings))
                ok = False
            # The form's shape follows the flags — the console has no
            # purpose→gate table to consult.
            if model.whitelist.get("required"):
                removed = len(model.fields) - len(payments.recipient_model(model).fields)
                shape = f"recipient picker replaces {removed} identity fields"
            else:
                shape = "free-form recipient fields"
            if model.documentation.get("required"):
                kinds = len(model.documentation.get("acceptedDocumentTypes") or [])
                shape += f"; upload widget listing {kinds} accepted types"
            else:
                shape += "; no document gate"
            say("    form model", shape)

        goods = models.get(("payment_for_goods_or_services", "fedwire", "business", "USA"))
        inter = models.get(("intercompany", "fedwire", "business", "USA"))
        if goods and inter:
            polarity = (
                goods.documentation.get("required") is True
                and goods.whitelist.get("required") is False
                and inter.whitelist.get("required") is True
            )
            say("  polarity matrix", "CONFIRMED live" if polarity else "CHANGED — review the gates")
            ok = ok and polarity
            capture(
                "payout_requirements_live_fedwire_goods.json",
                snapshots[("payment_for_goods_or_services", "fedwire", "business", "USA")],
            )
        else:
            say("  polarity matrix", "SKIPPED — a route did not answer")
            ok = False

        # --- (b) the indicative quote --------------------------------------------------
        print("\n(b) indicative quote — POST /v2/quotes, USD→USD, destinationCountry")
        quote_body = payments.quote_request(
            source="USD", destination="USD", destination_country="USA", amount_text="1000.00"
        )
        # No `Idempotency-Key`: this endpoint **refuses** one —
        # `400 VALIDATION_ERROR`, "Idempotency-Key is not supported on this
        # endpoint — a replay always reprices…" (found live 2026-08-28). It is
        # the one exception to OPERATIONS_SPEC §5's "send it unconditionally",
        # and the console's quote route sends none.
        quoted = http.post("/quotes", json=quote_body)
        if quoted.status_code >= 400:
            problem = parse_problem(quoted)
            say("quote", f"HTTP {quoted.status_code} {problem.type} — {problem.detail[:110]}")
            say("  request", json.dumps(quote_body))
            ok = False
        else:
            body = quoted.json()
            capture("quote_live_usd_withdrawal.json", body)
            view = payments.quote_view(body)
            if view is None:
                say("quote", "priced no options — the indicative panel renders nothing")
                ok = False
            else:
                say("quote", f"{view['id']} expires {view['expires_at']} · stale={view['stale']}")
                for option in view["options"]:
                    say(
                        f"  {option['rail']}",
                        f"debit {option['total_debit']} → recipient {option['recipient_amount']} "
                        f"· rate {option['end_user_rate']} · "
                        f"fees {', '.join(f['amount'] for f in option['fees']) or 'none'}",
                    )
                rails = {o["rail"] for o in view["options"]}
                say("  rails priced", ", ".join(sorted(rails)))
                if not {"fedwire", "rtp"} & rails:
                    say("  NOTE", "neither fedwire nor rtp was priced for this corridor")

        # --- find the funded customer the accounts run used ---
        print("\n(c) payout create + cancel")
        listing = http.get("/customers", params={"limit": 25})
        listing.raise_for_status()
        customers = [c for c in listing.json().get("data") or [] if accounts.has_active(c)]
        candidates: list[tuple[str, dict]] = []
        for candidate in customers:
            page = http.get(f"/customers/{candidate['id']}/virtual-accounts", params={"limit": 25})
            if page.status_code >= 400:
                continue
            for entry in page.json().get("data") or []:
                if entry.get("status") != "active":
                    continue
                if any(
                    (b.get("available") or {}).get("amount", "0") not in ("0", "0.00", None)
                    for b in entry.get("balances") or []
                ):
                    candidates.append((candidate["id"], entry))
        if not candidates:
            say("FAILED", "no staging customer has a funded active virtual account")
            return 1
        say("funded accounts", ", ".join(
            f"{cid}/{a['id']} "
            + ",".join(f"{b['code']} {b['available']}" for b in accounts.balance_rows(a))
            for cid, a in candidates
        ))

        # The **create** leg is opt-in. Staging accepts a cancel (200) but never
        # resolves the compliance review behind it, so every run that creates a
        # payout strands one `pending`/`under_review` payout forever (see the
        # fixtures README). Requirements, quote and whitelist stay always-on;
        # this one waits for a host that can finish what it starts.
        customer_id = candidates[0][0]
        if not CREATE_PAYOUT:
            say("payout create", "SKIPPED — set E2E_CREATE_PAYOUT=1 to run it")
            say("  why", "staging cannot complete a cancellation: the cancel is accepted, "
                         "then the payout parks at pending/under_review with no "
                         "/v2/sandbox/* route to resolve the review, so each run would "
                         "leave one behind")
        else:
            model = goods
            if model is None:
                say("FAILED", "fedwire/goods requirements did not answer; cannot build a payout")
                return 1

            # The supporting document the metadata asked for.
            document_ids: list[str] = []
            if model.documentation.get("required"):
                upload = http.post(
                    "/documents",
                    files={"file": ("zzztest-console-e2e.pdf", PDF, "application/pdf")},
                    data={
                        "purpose": payments.DOCUMENT_PURPOSE,
                        "name": f"ZZZTEST payout support {run}",
                    },
                    headers=idem(),
                )
                if upload.status_code >= 400:
                    say("document upload FAILED", f"{upload.status_code} {upload.text[:200]}")
                    return 1
                document_ids = [upload.json()["id"]]
                say("support document", f"{document_ids[0]} (purpose={payments.DOCUMENT_PURPOSE})")

            values = answer(model, run)
            local = forms.validate(model, values)
            if not local.ok:
                say("local validation FAILED", errors_of(local)[:800])
                return 1
            # Some staging customers answer `403 CAPABILITY_SUSPENDED` — the org can
            # read them but not move money out of them. That is a real refusal, not
            # a bug, so the script tries the next funded account rather than calling
            # the whole route broken.
            payout = customer_id = account = None
            for index, (candidate_id, candidate_account) in enumerate(candidates):
                asset = (candidate_account.get("asset") or {}).get("code") or "USD"
                body = payments.payout_body(
                    model,
                    answer(model, run),
                    customer_id=candidate_id,
                    virtual_account_id=candidate_account["id"],
                    asset=asset,
                    amount_text=payments.amount("11.00") or "11.00",
                    purpose="payment_for_goods_or_services",
                    document_ids=document_ids,
                )
                body["clientReferenceId"] = f"zzztest-console-{run}"
                if index == 0:
                    say("payout body", json.dumps(body, sort_keys=True)[:460])
                sent = http.post("/payouts", json=body, headers=idem())
                if sent.status_code < 400:
                    payout, customer_id, account = sent.json(), candidate_id, candidate_account
                    break
                problem = parse_problem(sent)
                say(f"  {candidate_id[:16]}…", f"{problem.status} {problem.type} — {problem.detail[:110]}")
                for error in getattr(problem, "errors", []):
                    say("    field", f"{error.pointer}: {error.detail}")
                if problem.status != 403:
                    if problem.resolution:
                        say("  resolution", problem.resolution[:180])
                    return 1
            if payout is None:
                say("payout FAILED", "every funded staging account refused the payout")
                return 1
            payout_id = payout["id"]
            say("payout created", f"{payout_id} HTTP {sent.status_code} status={payout.get('status')} "
                                  f"stage={payout.get('stage')}")
            capture("payout_live_withdrawal.json", payout)

            # The detail page's own rendering, from the app's own functions.
            say("  renders as", f"{payments.amount_of(payout)} · {payments.stage_label(payout) or 'no stage'} "
                                f"· cancellable={payments.can_cancel(payout)}")
            say("  fees", ", ".join(f"{f['owner']} {f['amount']}" for f in payments.fee_rows(payout))
                          or "none")
            say("  swiftUetr", payout.get("swiftUetr") or "not present on this payout")
            if payout.get("hasRfi"):
                say("  RFI", payout.get("rfiId") or "hasRfi with no rfiId — defect")

            # It is findable by the reconciler's own recipe (which needs `type`).
            lookup = http.get(
                "/transactions",
                params={"type": "withdrawal", "clientReferenceId": body["clientReferenceId"]},
            )
            found = [t for t in (lookup.json().get("data") or []) if t.get("id") == payout_id]
            say("reconciler lookup", f"HTTP {lookup.status_code} · "
                                     f"{'found by clientReferenceId' if found else 'NOT FOUND'}")
            ok = ok and bool(found)

            # Cancel while pending — the residue rule.
            #
            # **Found live 2026-08-28:** a cancel issued immediately after creation
            # answers `409 PAYOUT_CANCEL_IN_PROGRESS` — "was just created; retry the
            # cancellation". The console classifies every 409 as ambiguous
            # (OPERATIONS_SPEC §2), so the operation lands in `outcome_unknown` and
            # the reconciler's `payout_cancel` recipe re-reads and replays it with
            # the same key; here the script does the waiting itself so the run
            # leaves nothing behind.
            final: dict = {}
            for attempt in range(6):
                cancelled = http.post(f"/payouts/{payout_id}/cancel", headers=idem())
                say(f"cancel (attempt {attempt + 1})", f"HTTP {cancelled.status_code}")
                if cancelled.status_code < 400:
                    break
                problem = parse_problem(cancelled)
                say("  refusal", f"{problem.type} — {problem.detail[:120]}")
                if problem.type != "PAYOUT_CANCEL_IN_PROGRESS":
                    break
                time.sleep(10)
            # Cancellation is asynchronous: the 200 means "accepted", not "done".
            for tick in range(10):
                final = http.get(f"/payouts/{payout_id}").json()
                say(
                    f"  t={tick * 15}s",
                    f"{final.get('status')} · {payments.stage_label(final) or 'no stage'}",
                )
                if final.get("status") in ("cancelled", "completed", "failed"):
                    break
                time.sleep(15)
            say("final status", f"{final.get('status')} "
                                f"reason={final.get('cancellationReason') or '—'}")
            if final.get("status") != "cancelled":
                # Not a console defect — the cancel was *accepted* — but staging has
                # no `/v2/sandbox/*` route to resolve the compliance review, so the
                # payout parks in `under_review` forever. Reported loudly so the id
                # is never lost, and counted as a failure because residue is residue.
                say("RESIDUE", f"{payout_id} is {final.get('status')} / "
                               f"{final.get('stage')} — cancel accepted, review unresolvable "
                               f"on this host; chase it by hand")
                ok = False

        # --- (d) whitelist create + revoke ------------------------------------------------
        print("\n(d) whitelist recipient create + revoke")
        # Conduit refuses an empty `evidenceDocumentIds` even on a `self`
        # registration (found live: "Too small: expected array to have >=1
        # items"), so the console's own widget purpose is used here too.
        evidence = http.post(
            "/documents",
            files={"file": ("zzztest-console-evidence.pdf", PDF, "application/pdf")},
            data={
                "purpose": payments.EVIDENCE_PURPOSE,
                "name": f"ZZZTEST whitelist evidence {run}",
            },
            headers=idem(),
        )
        if evidence.status_code >= 400:
            say("evidence upload FAILED", f"{evidence.status_code} {evidence.text[:200]}")
            return 1
        say("evidence document", f"{evidence.json()['id']} (purpose={payments.EVIDENCE_PURPOSE})")

        whitelist_model = payments.whitelist_model("us")
        whitelist_values = forms.FormValues(
            document_ids=[evidence.json()["id"]],
            root={
                "routingNumber": ABA,
                "accountNumber": ACCOUNT_NUMBER,
                "relationship": "self",
                "legalName": f"ZZZTEST Own Account {run}",
                "label": f"zzztest-{run}",
            }
        )
        wl_errors = forms.validate(whitelist_model, whitelist_values)
        payments.whitelist_errors("us", whitelist_values, wl_errors)
        if not wl_errors.ok:
            say("local validation FAILED", errors_of(wl_errors)[:600])
            return 1
        wl_body = payments.whitelist_body("us", whitelist_values, whitelist_model)
        say("whitelist body", json.dumps(wl_body, sort_keys=True))

        path = f"/customers/{customer_id}/whitelist-recipients"
        created = http.post(path, json=wl_body, headers=idem())
        if created.status_code >= 400:
            problem = parse_problem(created)
            say("whitelist FAILED", f"{problem.status} {problem.type} — {problem.detail[:200]}")
            for error in getattr(problem, "errors", []):
                say("  field", f"{error.pointer}: {error.detail}")
            return 1
        recipient = created.json()
        recipient_id = recipient["id"]
        say("registered", f"{recipient_id} HTTP {created.status_code} "
                          f"status={recipient.get('status')}")
        capture("whitelist_recipient_live.json", recipient)
        if recipient.get("status") != "pending_review":
            say("NOTE", f"expected pending_review, got {recipient.get('status')}")

        listed = http.get(path, params={"limit": 100})
        entries = listed.json().get("data") or []
        mine = next((e for e in entries if e.get("id") == recipient_id), None)
        say("in the list", f"{'yes' if mine else 'NO'} · "
                           f"{len(payments.registered_only(entries))} registered of {len(entries)}")
        ok = ok and mine is not None
        # The payout picker must not offer a pending_review entry.
        if any(e.get("id") == recipient_id for e in payments.registered_only(entries)):
            say("PICKER DEFECT", "a pending_review entry reached the registered-only list")
            ok = False

        revoked = http.delete(f"{path}/{recipient_id}")
        say("revoke", f"HTTP {revoked.status_code}")
        after = http.get(f"{path}/{recipient_id}")
        if after.status_code == 404:
            say("final state", "gone (404) — nothing left behind")
        else:
            state = after.json().get("status")
            say("final state", state or "?")
            if state not in ("revoked", "rejected"):
                say("RESIDUE", f"{recipient_id} is {state} — revoke it by hand")
                ok = False

    print(
        "\n  Not exercisable on this host: the payout review-approve / review-reject / settle\n"
        "  simulators and the whitelist approve/reject simulators. Staging has no /v2/sandbox/*\n"
        "  routes (verified 2026-08-28), so those wire bodies are asserted against stubs in\n"
        "  tests/test_web_transactions.py and tests/test_web_recipients.py instead.\n"
        f"  Payout create+cancel: {'RAN' if CREATE_PAYOUT else 'skipped (E2E_CREATE_PAYOUT unset)'}.\n"
    )
    print(f"  result: {'PASS' if ok else 'FAIL'}  run {run}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
