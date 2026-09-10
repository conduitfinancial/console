"""The §2 write sequence: the only place a mutation is sent to Conduit.

    op, is_new = await operations.start(session, type="payout_create", ...)
    if is_new:
        await execute_operation(session, op, client=client, **actor)

Routes never call `client.mutate()` themselves — that is what keeps "insert →
in_flight → call → record" true for every mutation in the app.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import documents, operations
from app.conduit.client import ConduitClient, Outcome, Problem, Result, classify
from app.models import Operation

# POST unless stated; DELETE has no body and no idempotency key of its own.
MUTATION_METHOD: dict[str, str] = {"whitelist_revoke": "DELETE"}

# Request DTOs that carry `clientReferenceId` (verified against the pinned spec:
# OnboardingSubmitDto, CustomerUpdateSubmitDto, FiatPayoutDto/CryptoPayoutDto and
# the order DTOs — FeatureRequestDto, the whitelist DTOs and the RFI/webhook DTOs
# do not).
CLIENT_REFERENCE_TYPES = frozenset(
    {"onboarding_submit", "customer_update", "payout_create", "order_create"}
)


def outbound_body(op: Operation) -> dict | None:
    """The bytes actually sent — the stored body plus `clientReferenceId = op.id`.

    The reference is injected here, not stored in `request_body`, for two
    reasons: the replay reproduces it byte-identically from the row, and the
    stored body stays free of a per-row value that would make every
    `request_hash` unique and quietly disable the double-submit guard (§1).
    """
    if op.request_body is None or op.type not in CLIENT_REFERENCE_TYPES:
        return op.request_body
    return {**op.request_body, "clientReferenceId": str(op.id)}

# Path tails that are actions, not resource ids — see `target_id`.
_ACTIONS = ("cancel", "execute", "responses", "features", "updates", "acknowledge")


def target_id(op: Operation) -> str | None:
    """The Conduit resource a mutation acts *on* (payout being cancelled, RFI
    being answered). For create-shaped operations this is the collection name,
    which is why callers prefer the response's own `id`."""
    parts = op.request_path.strip("/").split("/")
    if len(parts) < 2:
        return None
    return parts[-2] if parts[-1] in _ACTIONS else parts[-1]


def resource_id(result: Result, op: Operation) -> str | None:
    data = getattr(result, "data", None)
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return data["id"]
    return target_id(op)  # 204s (whitelist_revoke) and bodies without an id


async def send(session: AsyncSession, op: Operation, client) -> Result:
    """Put the operation's request on the wire, in whatever encoding its endpoint
    speaks. The single place that decision is made, so the first attempt and the
    reconciler's replay are byte-identical by construction.

    `client` is a `ConduitClient` or the reconciler's budgeted wrapper — both
    expose `mutate`.
    """
    if op.type == "document_upload":
        # Multipart, rebuilt from the encrypted blob (`app.documents`). Same
        # file, same purpose, same name, same idempotency key on every attempt.
        return await client.mutate(
            "POST",
            op.request_path,
            idempotency_key=op.idempotency_key,
            **await documents.multipart(session, op),
        )
    return await client.mutate(
        MUTATION_METHOD.get(op.type, "POST"),
        op.request_path,
        json=outbound_body(op),
        idempotency_key=op.idempotency_key,
    )


async def execute_operation(
    session: AsyncSession,
    op: Operation,
    *,
    client: ConduitClient,
    actor_id: str,
    actor_email: str,
) -> Operation:
    """Run one attempt of `op` and record its classified outcome. Also the
    audited `stalled → in_flight` retry path (OPERATIONS_SPEC §5) — same row,
    same idempotency key, by construction."""
    async with operations.in_flight(session, op, actor_id=actor_id, actor_email=actor_email) as rec:
        result = await send(session, op, client)
        match classify(result):
            case Outcome.CONFIRMED:
                await rec.confirmed(resource_id(result, op))
            case Outcome.REJECTED:
                await rec.rejected(result.raw, correlation_id=result.correlation_id)  # type: ignore[union-attr]
            case _:
                await rec.unknown(
                    reason=result.error
                    if not isinstance(result, Problem)
                    else f"{result.status} {result.type}",
                    correlation_id=getattr(result, "correlation_id", None),
                )
    return (
        await session.execute(
            select(Operation).where(Operation.id == op.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
