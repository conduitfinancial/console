"""The `worker` runtime role (plan v2 §1/§5): `python -m app.worker`.

One loop, four jobs. The inbox is drained on every tick because a delivery the
operator is waiting on should not sit for a minute; the reconciler and the two
housekeeping jobs run on `RECONCILE_INTERVAL`.

    inbox   claim pending webhook_events (FOR UPDATE SKIP LOCKED, oldest first)
            → apply_observation → processed | processed_ignored | failed
    repair  reconcile_pass (OPERATIONS_SPEC §3)
    ttl     never-sent operations → abandoned (§2);
            unsubmitted drafts past DRAFT_TTL_DAYS discarded (plan v2 §3)
    purge   request_body and document blobs of long-terminal operations (§6);
            raw bodies of settled webhook events; payloads of dispatched batches

A poison event is retried MAX_ATTEMPTS times and then marked `failed` with its
error, so it can never wedge the queue behind it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import batches, documents, operations, projections, reconciliation
from app.conduit import ConduitClient
from app.config import get_settings
from app.db import sessionmaker
from app.models import WebhookEvent
from app.onboarding import drafts

log = logging.getLogger(__name__)

BATCH = 50
MAX_ATTEMPTS = 3
POLL_SECONDS = 1.0
# A claim is a lease. If the worker that took a row dies before recording an
# outcome, the row would otherwise sit in `processing` forever; after the lease
# any worker may take it again. Generous compared with the
# work itself — reclaiming a row someone is still processing is harmless
# (apply_observation is idempotent) but pointless.
CLAIM_LEASE_SECONDS = 300

# The two outcomes that mean the worker will never look at `raw_body` again, and
# therefore the only two whose bytes retention may drop (`purge_raw_bodies`).
SETTLED_STATUSES = ("processed", "processed_ignored")

# Which delivery mode belongs to which environment. Staging is deliberately
# absent: it self-labels as production and its mode value is unverified, so
# nothing is filtered there until a live delivery says what it sends.
EXPECTED_MODE: dict[str, str] = {"production": "live", "sandbox": "sandbox"}

# Event type prefix → projection kind (plan v2 §5's relevant-events list).
# Payouts emit `transaction.*`; there are no `payout.*` events.
KIND_BY_PREFIX: dict[str, str] = {
    "transaction": "transactions",
    "application": "applications",
    "order": "orders",
    "virtual_account": "virtual_accounts",
    "whitelist_recipient": "whitelist_recipients",
    "customer": "customers",
    # The six `rfi.*` types (live `GET /v2/webhooks/event-types` probe against
    # the sandbox, 2026-08-30; the pinned spec's subscription enum lists the
    # same six). All `beta`, category `compliance`, and every one of them
    # carries `data` of exactly `{"rfiId": "rfi_…"}` — no status, no subjects,
    # with the description saying "Fetch details via GET /v2/rfis/{id}". So for
    # this kind the suffix table below is not a fallback, it is the only state
    # the delivery carries, and the §4 stale sweep is what fills in the rest.
    "rfi": "rfis",
}

# Fallback only, for events whose `data` carries no `status`: the event-type
# suffix, mapped where Conduit's verb differs from its status vocabulary.
#
# The `rfi.*` entries below are the RFI lifecycle read against its 5-status
# vocabulary (`draft|open|responded|resolved|cancelled`): `published` opens one,
# `more_info_requested` re-opens an answered one, `response_submitted` answers
# it. `resolved` and `cancelled` are already the status word and pass through.
# `deadline_extended` maps to nothing — it moves `dueAt`, and asserting a status
# from it would be inventing one. (This table is keyed by suffix across all
# kinds; none of these five suffixes is emitted by any other family — checked
# against all 51 live event types.)
STATE_BY_SUFFIX: dict[str, str] = {
    "created": "pending",
    "activated": "active",
    "published": "open",
    "more_info_requested": "open",
    "response_submitted": "responded",
    "deadline_extended": "",
}


def _id_key(prefix: str) -> str:
    """`virtual_account` → `virtualAccountId`: the key an event's `data` uses to
    name its own resource."""
    head, *rest = prefix.split("_")
    return head + "".join(word.title() for word in rest) + "Id"


# --- inbox -------------------------------------------------------------------------


def observation(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Event → `apply_observation` kwargs, or None for "nothing to project".

    `data` carries the resource; the kind comes from the event type's prefix.
    Anything unmapped or id-less is ignored rather than guessed at.
    """
    event_type = payload.get("type")
    data = payload.get("data")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    # One endpoint can receive both live and sandbox events; projecting a
    # sandbox resource into a production console would be a lie about money
    #. An absent `mode` is tolerated — the field's presence
    # is unverified against a real delivery.
    mode, expected = payload.get("mode"), EXPECTED_MODE.get(get_settings().conduit_env)
    if isinstance(mode, str) and expected is not None and mode != expected:
        log.warning("ignoring a %r event on a %r installation", mode, get_settings().conduit_env)
        return None
    prefix = event_type.split(".")[0]
    kind = KIND_BY_PREFIX.get(prefix)
    # Live deliveries name the resource after itself — `applicationId`,
    # `transactionId`, `virtualAccountId` — and never `id`; `GET
    # /v2/webhooks/event-types` says so for all 51 types (verified live
    # 2026-08-28). `id` is kept as a fallback for the resource-shaped payload
    # the fixtures assumed.
    resource_id = data.get(_id_key(prefix)) or data.get("id")
    if kind is None or not isinstance(resource_id, str) or not resource_id:
        return None

    observed = dict(data)
    if not isinstance(observed.get("status"), str):
        suffix = event_type.partition(".")[2]
        asserted = STATE_BY_SUFFIX.get(suffix, suffix) or None
        # A delivery that carries no `status` names an EVENT, and not every
        # event name is a status. Eight of the 51 live types have a suffix that
        # is not in its kind's vocabulary — six on transactions
        # (`awaiting_signature`, `quorum_met`, `signature_collected`,
        # `awaiting_sender_information`, `awaiting_user_signature`, `rejected`),
        # which are stages of a payment, not states of one. Deriving a status
        # from them put the console's own coinage on screen in the slot reserved
        # for Conduit's word — rendered "Unknown: quorum_met", which reads as a
        # status Conduit sent and is not one. The table already said as much
        # about `deadline_extended` ("asserting a status from it would be
        # inventing one"); this applies that rule to every suffix instead of the
        # one that was noticed.
        #
        # Asserting nothing is safe rather than lossy: a `None` state ranks
        # below every real one and is non-terminal, so `projections._advances`
        # refuses it against any known status — it can never erase what we
        # already know — and the §4 stale-projection sweep reads the real status
        # from Conduit. `STATE_RANKS` is the kind's own vocabulary, pinned to the
        # spec by `tests/test_vocabulary_drift.py`; a kind with no ladder has no
        # vocabulary to judge against, so its events are left as they were.
        known = projections.STATE_RANKS.get(kind)
        if asserted is not None and known and asserted not in known:
            asserted = None
        observed["status"] = asserted
    return {
        "resource_kind": kind,
        "resource_id": resource_id,
        "observed": observed,
        "observed_at": _timestamp(payload.get("createdAt")),
    }


def _timestamp(value: Any) -> datetime | None:
    """`None` lets `apply_observation` fall back to now — an event without a
    usable timestamp must not sort as 1970 and lose to everything."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def reclaim_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Return rows whose claim lease ran out to the queue.

    A crash between `claim` and recording an outcome used to strand the whole
    claimed batch in `processing` forever. `attempts` is not reset, so a row
    that keeps killing its worker still exhausts its budget and fails rather
    than looping.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=CLAIM_LEASE_SECONDS)
    released = await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.status == "processing", WebhookEvent.claimed_at < cutoff)
        .values(status="pending", claimed_at=None)
    )
    await session.commit()
    if released.rowcount:
        log.warning("reclaimed %s webhook events from an expired lease", released.rowcount)
    return released.rowcount


async def claim(session: AsyncSession, limit: int = BATCH) -> list[WebhookEvent]:
    """Take the oldest pending deliveries. SKIP LOCKED so a second worker (or a
    still-running tick) takes different rows instead of waiting (§8)."""
    events = (
        (
            await session.execute(
                select(WebhookEvent)
                .where(WebhookEvent.status == "pending")
                .order_by(WebhookEvent.received_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for event in events:
        event.status, event.attempts, event.claimed_at = "processing", event.attempts + 1, now
    await session.commit()
    return list(events)


async def process_pending(
    session: AsyncSession, *, limit: int = BATCH, max_attempts: int = MAX_ATTEMPTS
) -> dict[str, int]:
    """Drain one batch. Returns a count per resulting status, for logs and tests."""
    counts: Counter[str] = Counter()
    await reclaim_expired(session)
    for event in await claim(session, limit):
        # Read off the row before the work: a rollback expires the instance.
        event_id, attempts, raw_body = event.id, event.attempts, event.raw_body
        error = None
        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("event payload is not a JSON object")
            arguments = observation(payload)
            if arguments is None:
                status = "processed_ignored"
            else:
                await projections.apply_observation(session, **arguments)
                status = "processed"
        except Exception as exc:  # noqa: BLE001 — one bad event must not stop the batch
            await session.rollback()
            status = "failed" if attempts >= max_attempts else "pending"
            error = f"{type(exc).__name__}: {exc}"
            log.warning("webhook event %s failed (attempt %s): %s", event_id, attempts, error)
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                status=status,
                error=error,
                claimed_at=None,  # the lease ends with the outcome
                processed_at=None if status == "pending" else datetime.now(UTC),
            )
        )
        await session.commit()
        counts[status] += 1
    return dict(counts)


async def purge_raw_bodies(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Retention for the inbox's own encrypted column.

    `raw_body` is the delivery byte-for-byte, and a delivery carries the resource
    whole — a withdrawal's `destination.recipient` is the payee's account number,
    legal name and postal address. 0011 encrypted it; nothing aged it out, so it
    was the last encrypted column in this database with no retention job.

    **Only settled events, at any age.** `processed` and `processed_ignored` are
    the two outcomes that mean the worker is finished with the bytes. `pending`
    and `processing` still have to be parsed; `failed` is a poison event whose
    body is the only evidence of why it is poison, and a human is expected to
    read it — so none of the three is ever purged here, however old. That is a
    deliberate hole: a permanently failed delivery keeps its payload until
    someone deals with the row. Stated in deploy/README §5.

    NULLs the column and nothing else: status, error, timestamps and everything
    already projected from the event stay exactly as they are.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(
        days=get_settings().webhook_raw_retention_days
    )
    purged = await session.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.status.in_(SETTLED_STATUSES),
            WebhookEvent.processed_at < cutoff,
            WebhookEvent.raw_body.is_not(None),
        )
        .values(raw_body=None)
    )
    await session.commit()
    return purged.rowcount


# --- the loop ----------------------------------------------------------------------


async def tick(session: AsyncSession, client: ConduitClient) -> None:
    """The periodic half: repair, then housekeeping."""
    await reconciliation.reconcile_pass(session, client)
    await operations.abandon_expired(session)
    await operations.purge_request_bodies(session)
    # The blob purge is a second statement rather than a second job —
    # same window, same state set, same tick as `purge_request_bodies`; it only
    # lives elsewhere because the column does.
    await documents.purge_blobs(session)
    # Same tick, two more encrypted columns on their own windows: the inbox's
    # raw deliveries, and the assembled destinations of dispatched batch rows.
    await purge_raw_bodies(session)
    await batches.purge_row_payloads(session)
    await drafts.discard_stale(session)
    # Submitted drafts keep their answers until the application is finally
    # settled — rejection correction re-opens them (drafts module docstring).
    await drafts.purge_settled(session)


def heartbeat() -> None:
    """Touch the liveness file, if one is configured.

    The worker serves nothing, so "is it alive?" has no endpoint to ask. A file
    it touches after every successful pass gives a container healthcheck an
    mtime to compare against — see `deploy/docker-compose.yml`. Failing to write
    it is logged and otherwise ignored: a read-only /tmp is a deployment
    mistake, not a reason to stop reconciling money.
    """
    path = get_settings().worker_heartbeat_path
    if not path:
        return
    try:
        Path(path).touch()
    except OSError as exc:
        log.warning("worker heartbeat %s is not writable: %s", path, type(exc).__name__)


async def _every(
    seconds: float, stop: asyncio.Event, name: str, work, gave_up: list[str] | None = None
) -> None:
    """Run `work` on an interval until `stop`, surviving its own failures.

    Each loop owns a session per iteration: a poisoned transaction is discarded
    with it rather than carried into the next round.

    Surviving failures is right for a bad *pass* and wrong for a bad *world*: a
    loop that has failed `WORKER_MAX_FAILED_PASSES` times in a row is not
    working through a blip, it is a process that will never recover on its own
    (a dropped database, a revoked key). It stops the worker and records itself
    in `gave_up`, which makes `run()` return False and the container exit
    non-zero — so the restart policy acts instead of a queue silently growing.
    """
    limit = get_settings().worker_max_failed_passes
    failures = 0
    while not stop.is_set():
        async with sessionmaker()() as session:
            try:
                await work(session)
                failures = 0
                heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad pass must not kill the worker
                log.exception("worker %s pass failed", name)
                await session.rollback()
                failures += 1
                if failures >= limit:
                    log.error("worker %s failed %s consecutive passes — giving up", name, failures)
                    if gave_up is not None:
                        gave_up.append(name)
                    stop.set()
                    return
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)


async def run(*, stop: asyncio.Event | None = None) -> bool:
    """Two independent loops.

    They used to share one: a reconciliation pass can legitimately spend a
    hundred requests against a timing-out API, and for the tens of minutes that
    takes, no webhook was processed and SIGTERM was not answered. Separate tasks
    mean the inbox keeps draining through an outage, and shutdown cancels the
    repair pass mid-flight — safe, because every step of it commits on its own.
    """
    settings = get_settings()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    if stop is None:  # a caller-supplied event owns shutdown instead
        stop = asyncio.Event()
        for received in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):  # not the main thread
                loop.add_signal_handler(received, stop.set)
                installed.append(received)

    client = ConduitClient()
    log.info("worker started (reconcile every %ss)", settings.reconcile_interval_seconds)
    # Written to by whichever loop gives up; `run` returns False if either did,
    # and `__main__` turns that into a non-zero exit.
    gave_up: list[str] = []
    tasks = [
        asyncio.create_task(
            _every(POLL_SECONDS, stop, "inbox", process_pending, gave_up), name="inbox"
        ),
        asyncio.create_task(
            _every(
                settings.reconcile_interval_seconds,
                stop,
                "repair",
                lambda session: tick(session, client),
                gave_up,
            ),
            name="repair",
        ),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        for task, outcome in zip(tasks, await asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(outcome, Exception):
                log.error("worker %s loop exited: %r", task.get_name(), outcome)
        for received in installed:  # leave the process's signals as we found them
            loop.remove_signal_handler(received)
        await client.aclose()
        log.info("worker stopped%s", f" — {', '.join(gave_up)} gave up" if gave_up else "")
    return not gave_up


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(0 if asyncio.run(run()) else 1)
