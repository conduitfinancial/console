"""Sandbox-only decision simulator.

`POST /v2/sandbox/applications/{id}/simulate/decision` exists on Conduit's
sandbox host and nowhere else — the pinned production spec has no `/v2/sandbox`
path at all, and the staging host this installation currently points at returns
nothing for one (verified 2026-08-28). So the panel is rendered only when
`CONDUIT_ENV=sandbox` **and** the route re-checks it server-side: a template
condition is a display decision, not an authorization one, and the check that
matters is the one on the request that would actually send.

Outside the operations ledger by the rule in `app.web`: it is test scaffolding
for an environment whose state is fake by construction, it creates no resource
this console owns, and a replay against an already-decided application answers
200 with the same application — idempotent, never a duplicate. It is audited
like every other operator action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import audit
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Problem, Success
from app.web import conduit, db, form_items, is_sandbox, problem_line, redirect

router = APIRouter()

PATH = "/v2/sandbox/applications/{id}/simulate/decision"

# `SimulateApplicationDecisionDto`: `{outcome, category?, field?}`. `category`
# is meaningful only on a rejection, `field` only alongside a `category`, and
# field names are lowercase (`tax_id`, `business_name`, `ubo_first_name`, …).
# `resubmittable` is Conduit's own output on the application, never an input
# here. The phase-0 spec also listed `reason?`, but the live sandbox refuses it
# (400 `Unrecognized key: "reason"`, verified 2026-08-28) — the operator's note
# goes to the audit trail instead, never on the wire.
OUTCOMES = ("approved", "rejected")
CATEGORIES = ("document_mismatch", "data_sourcing_mismatch", "compliance", "generic")
REASON_MAX = 500


@router.post("/applications/{application_id}/simulate")
async def simulate(
    request: Request,
    application_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("sandbox.simulate")),
) -> Response:
    back = f"/applications/{application_id}"
    if not is_sandbox():
        return redirect(request, back, err="Simulation is available on the sandbox host only.")
    values = dict(await form_items(request))
    outcome = values.get("outcome", "")
    if outcome not in OUTCOMES:
        return redirect(request, back, err="Unknown simulated outcome.")

    body: dict = {"outcome": outcome}
    category = values.get("category", "").strip()
    field = values.get("field", "").strip().lower()
    reason = values.get("reason", "").strip()
    if outcome == "rejected" and category:
        if category not in CATEGORIES:
            return redirect(request, back, err="Unknown rejection category.")
        body["category"] = category
        if field:
            body["field"] = field
    elif field:
        # `field` is only accepted with a `category`, and `category` only on a
        # rejection — refuse rather than send a body the sandbox will reject.
        return redirect(request, back, err="A field needs a rejection category.")
    result = await client.mutate("POST", PATH.format(id=application_id), json=body)
    detail = {"application": application_id, "ok": isinstance(result, Success), **body}
    if reason:
        # Audit-only: the sandbox DTO refuses `reason` on the wire.
        detail["reason"] = reason[:REASON_MAX]
    audit.record(
        session,
        action="sandbox.simulate_decision",
        actor_id=actor.id,
        actor_email=actor.email,
        detail=detail,
    )
    await session.commit()
    if isinstance(result, Success):
        return redirect(request, back, msg=f"Simulated {outcome}.")
    # A replay against an already-decided application answers 409
    # APPLICATION_ALREADY_DECIDED (verified live 2026-08-28) — surfaced like
    # any other refusal, in this console's words.
    return redirect(request, back, err=problem_line(result))
