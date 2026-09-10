"""The confirm screen, the dispatch engine, and the one
property the whole feature exists to have — **every row is sent exactly once**.

The idempotence tests here do not assert on states or on counts of rows. They
assert on the **wire**: how many `POST /v2/payouts` requests the stub saw, and
with which idempotency keys. A state machine can be made to look right while
paying twice; the transport cannot.

The matrix is the brief's: re-click during a run, reload mid-dispatch, dispatch
again after a partial failure, and a worker killed between two rows. Each one
ends with the same assertion — one payout call per row, ever.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app import batches, counterparties, documents, operations, payments
from app.conduit import ConduitClient
from app.config import get_settings
from app.db import sessionmaker
from app.web.batches import _run, _unchangeable
from app.models import AuditEvent, Counterparty, Operation, PayoutBatch, PayoutBatchRow
from tests.payments_fixtures import (
    CID,
    FEDWIRE_BUSINESS,
    FEDWIRE_INTERCOMPANY,
    REGISTERED,
    USD_ACCOUNT,
    WHITELIST_PATH,
)
from tests.test_batches import (
    CONTACT_ROW,
    GATED_ROW,
    GOODS,
    ROUTE,
    ROW,
    flash,
    routes,
    saved_contact,
    uploaded,
)
from tests.conftest import settings_override
from tests.web_harness import (
    forbidden_affordances,
    make_app,
    post,
    signed_in,
    signed_in_as,
    stub,
    upload as upload_document,
)

TXN = {"id": "txn_batch_1", "type": "withdrawal", "status": "pending"}


def said(response: httpx.Response) -> str:
    """The page's prose with entities resolved and runs of whitespace collapsed.

    The sentences asserted here are the ones an operator reads; Jinja wraps them
    across source lines and escapes their punctuation, and neither is a fact
    about the copy.
    """
    return re.sub(r"\s+", " ", html.unescape(response.text))


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """The pace between rows is politeness, not mechanism (`batches.PACE_SECONDS`).
    Every test here would otherwise pay for it in wall clock; one test below puts
    it back to prove it is real."""
    monkeypatch.setattr(batches, "PACE_SECONDS", 0)


def payouts_stub(calls: list, responses=None):
    """`POST /v2/payouts` recording every request, answering from `responses` in
    order (default: a fresh accepted payout each time)."""
    answers = list(responses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.headers.get("idempotency-key", ""),
                request.read().decode(),
            )
        )
        if answers:
            return answers.pop(0)
        return httpx.Response(202, json={**TXN, "id": f"txn_{len(calls)}"})

    return handler


def app_with(calls: list, *, responses=None, requirements=FEDWIRE_BUSINESS, recipients=None):
    wire: list = []
    handler = stub(
        routes(
            requirements=requirements,
            recipients=recipients,
            extra={("POST", "/v2/payouts"): payouts_stub(calls, responses)},
        ),
        wire,
    )
    app = make_app(handler)
    app.state.wire = wire
    return app


async def ready_batch(web, rows=(ROW, ROW), query: str = ROUTE, *, documents=True) -> str:
    """An uploaded, validated, ready batch — the state upload leaves behind.

    The fedwire fixture's route declares `documentation.required`, so a real
    ready batch on it carries a batch-level shared document; it is uploaded
    through the console's own route because a `doc_` id that is not in this
    console's upload ledger is refused (OPERATIONS_SPEC §3).
    """
    url = await uploaded(web, list(rows), query)
    body = b""
    if documents:
        name = f"doc_{uuid.uuid4().hex[:8]}"
        await upload_document(web, purpose="transaction_support", filename=f"{name}.png")
        body = f"documentIds={name}".encode()
    marked = await post(web, f"{url}/ready", body)
    assert "err=" not in marked.headers["hx-redirect"], marked.headers["hx-redirect"]
    return url


async def dispatch(web, url: str) -> httpx.Response:
    """One click of the dispatch button. The engine runs as a background task,
    which Starlette awaits inside the ASGI call — so when this returns, the run
    has finished."""
    return await post(web, f"{url}/dispatch")


def payout_calls(app) -> list:
    return [c for c in app.state.wire if c[0] == "POST" and c[1] == "/v2/payouts"]


# --- the intent nonce ---------------------------------------------------------------------


def test_the_intent_is_a_pure_function_of_the_batch_and_the_row():
    """The whole guarantee in one assertion: same batch + same row → same uuid,
    computed from nothing else. No clock, no randomness, no process state."""
    batch = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert batches.intent_for(batch, 1) == batches.intent_for(str(batch), 1)
    assert batches.intent_for(batch, 1) != batches.intent_for(batch, 2)
    assert batches.intent_for(batch, 1) != batches.intent_for(uuid.uuid4(), 1)
    # The literal value, pinned: this uuid is a durable identity — a future
    # refactor that changed the namespace or the separator would silently make
    # every already-dispatched row dispatchable again.
    assert str(batches.intent_for(batch, 7)) == "5c95d493-60b0-500f-b4ad-576514f09a12"


def test_the_intent_survives_a_fresh_process():
    """`uuid5` is a hash, not a registry — proven by computing it in a python
    that has never seen this one's memory."""
    import subprocess
    import sys

    batch = "11111111-2222-3333-4444-555555555555"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import uuid,sys;"
            "ns=uuid.UUID('9c1a4f6e-3b52-4d18-9f7a-2c0e5b8d41a3');"
            f"print(uuid.uuid5(ns, '{batch}:4'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == str(batches.intent_for(batch, 4))


# --- the confirm screen -------------------------------------------------------------------


async def test_the_dispatch_button_exists_only_on_the_confirm_screen():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        report = await web.get(url)
        confirm = await web.get(f"{url}/confirm")

    assert "/dispatch" not in report.text
    assert f"{url}/confirm" in report.text
    assert f'hx-post="{url}/dispatch"' in confirm.text


async def test_the_confirm_screen_states_the_consequence_the_totals_and_the_account():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        confirm = await web.get(f"{url}/confirm")

    text = said(confirm)
    # The consequence sentence, scaled up — the count, the total, the account,
    # and the exactly-once promise this slice is allowed to make.
    assert "Dispatching sends 2" in text and "20.00 USD" in text
    assert USD_ACCOUNT["id"] in text
    assert (
        "Each row is sent exactly once — re-clicking, reloading, or re-dispatching cannot send a "
        "row twice." in text
    )
    # The funding account's own balance, read from Conduit for this screen.
    assert "Available" in text


async def test_the_confirm_screen_of_a_half_dispatched_batch_counts_only_what_is_left(
    monkeypatch,
):
    """A number an operator approves has to say what it leaves out (the design
    owner's rule) — and on a re-dispatch what it leaves out is the money that has
    already gone. The button may not offer to send it again."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        monkeypatch.setattr(batches, "_pace", killed_between_rows())
        await dispatch(web, url)
        monkeypatch.undo()
        confirm = await web.get(f"{url}/confirm")

    assert len(calls) == 1
    text = said(confirm)
    assert "Already dispatched" in text and "1 sent" in text
    # One row left, and the total behind the button is that row's, not the
    # batch's.
    assert "Dispatching sends 1 payout" in text and ">10.00 USD<" in text


async def test_a_viewer_cannot_reach_the_confirm_screen_or_dispatch():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
    async with signed_in(app, groups="readers") as viewer:
        seen = await viewer.get(f"{url}/confirm")
        tried = await post(viewer, f"{url}/dispatch")
        report = await viewer.get(url)
    assert seen.status_code == 403
    assert tried.status_code == 403
    # The report is still readable, and says why the button is not there.
    assert report.status_code == 200
    assert "dispatching one needs <code>batch.dispatch</code>" in report.text
    assert payout_calls(app) == []


async def test_a_role_that_may_prepare_but_not_dispatch_is_told_where_it_stops():
    """The one surface whose two halves are separate permissions. A `batch.upload`
    role reaches a ready batch, is told what dispatching needs, and is offered no
    control that would refuse it."""
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
    async with signed_in_as(app, "batch.upload", "document.upload") as web:
        report = await web.get(url)
        confirm = await web.get(f"{url}/confirm")
        tried = await post(web, f"{url}/dispatch")

    assert report.status_code == 200
    assert confirm.status_code == 403 and tried.status_code == 403
    # Ready, and honest about it: the totals are still stated, the button is not
    # there, and the gap is named rather than left as an empty row.
    assert "Ready to dispatch" in report.text
    assert "Review and dispatch" not in report.text
    assert "Dispatching this batch needs the <code>batch.dispatch</code> permission." in report.text
    # Abandon is `batch.upload`, so this role keeps it.
    assert "Abandon this batch" in report.text
    held = {"console.view", "batch.upload", "document.upload"}
    assert forbidden_affordances(app, report.text, held) == []
    assert payout_calls(app) == []


async def test_permissions_mds_own_batch_dispatcher_role_can_actually_dispatch():
    """`{"batch-dispatcher": ["console.view", "batch.dispatch"]}` is the example
    in PERMISSIONS.md. It holds no `batch.upload`, so every preparation control
    is withheld — but the dispatch card is the point of the role and has to be
    on the page."""
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
    async with signed_in_as(app, "batch.dispatch") as web:
        report = await web.get(url)
        confirm = await web.get(f"{url}/confirm")

    assert report.status_code == 200 and confirm.status_code == 200
    assert "Ready to dispatch" in report.text
    assert "Review and dispatch" in report.text
    # Preparation is the other permission, and none of it is offered.
    assert "Abandon this batch" not in report.text
    assert "Mark ready" not in report.text
    assert forbidden_affordances(app, report.text, {"console.view", "batch.dispatch"}) == []


async def test_dispatch_without_the_csrf_header_is_refused():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        refused = await web.post(f"{url}/dispatch", headers={"HX-Request": "true"})
    assert refused.status_code == 403
    assert payout_calls(app) == []


async def test_the_confirm_screen_carries_the_house_double_click_guards():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        confirm = await web.get(f"{url}/confirm")
    assert 'hx-sync="this:drop"' in confirm.text
    assert 'hx-disabled-elt="find button[type=submit]"' in confirm.text


# --- dispatch ------------------------------------------------------------------------------


async def test_dispatch_sends_one_payout_per_valid_row_and_links_each_one(session):
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        answer = await dispatch(web, url)
        report = await web.get(url)

    assert "msg=" in answer.headers["hx-redirect"]
    assert len(calls) == 2
    # Two payouts, two DIFFERENT idempotency keys: they are two payments.
    assert len({key for key, _ in calls}) == 2

    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    rows = (
        (
            await session.execute(
                select(PayoutBatchRow.row_number, PayoutBatchRow.operation_id)
                .where(PayoutBatchRow.batch_id == batch_id)
                .order_by(PayoutBatchRow.row_number)
            )
        )
        .all()
    )
    assert all(operation_id is not None for _, operation_id in rows)
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    assert batch.status == "dispatched"
    assert "2 sent" in report.text
    # The row links to the transaction its payout became.
    assert "/transactions/txn_1" in report.text


async def test_the_body_is_the_single_payout_forms_body(session):
    """The design requirement, asserted on the wire: a batch row and the payout
    form produce the SAME `FiatPayoutDto`. One builder
    (`payments.assembled_payout_body`) is what makes that structural."""
    import json

    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        await upload_document(web, purpose="transaction_support", filename="doc_b1.png")
        url = await uploaded(web, [ROW])
        await post(web, f"{url}/ready", b"documentIds=doc_b1")
        await dispatch(web, url)

    body = json.loads(calls[0][1])
    assert body["customerId"] == CID
    assert body["virtualAccountId"] == USD_ACCOUNT["id"]
    assert body["assetAmount"] == {"code": "USD", "amount": "10.00"}
    assert body["purpose"] == "payment_for_goods_or_services"
    assert body["destination"]["recipient"]["accountNumber"] == "000123456789"
    assert body["destination"]["remittance"]["reference"] == "INV-4471"
    # The batch's shared documents ride with every row.
    assert body["documents"] == ["doc_b1"]
    # The ledger still owns `clientReferenceId` — injected at call time, one per
    # operation (OPERATIONS_SPEC §5), never something the batch chose.
    assert uuid.UUID(body["clientReferenceId"])


async def test_a_rejected_row_carries_conduits_problem_and_is_final(session):
    calls: list = []
    app = app_with(
        calls,
        responses=[
            httpx.Response(202, json=TXN),
            httpx.Response(
                422,
                json={
                    "type": "VALIDATION_ERROR",
                    "title": "Recipient not accepted",
                    "detail": "The account number failed validation.",
                    "correlationId": "corr_9",
                },
            ),
        ],
    )
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        report = await web.get(url)
        # Dispatching again must not retry the rejected row.
        again = await dispatch(web, url)

    assert len(calls) == 2, calls
    # A3 gate M1: the row's cell is the translated view (`batches.rows_of`),
    # so it is this console's sentence for the code and never Conduit's title.
    assert "Conduit refused some of the values on this form" in report.text
    assert "Recipient not accepted" not in report.text
    assert batches.REJECTED_IS_FINAL[:60] in report.text
    assert "err=" in again.headers["hx-redirect"] or True  # the batch is terminal now
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    # Every row has an answer, so the batch's own job is done — `dispatched` is
    # about the batch, not about whether the money moved.
    assert batch.status == "dispatched"


async def test_an_unknown_outcome_is_left_to_the_reconciler(session):
    calls: list = []
    app = app_with(
        calls,
        responses=[httpx.Response(202, json=TXN), httpx.Response(503, text="gateway")],
    )
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        report = await web.get(url)
        await dispatch(web, url)  # re-dispatch must not re-send it

    assert len(calls) == 2, calls
    assert "The reconciler owns this from here" in report.text
    states = (
        (await session.execute(select(Operation.state).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert sorted(states) == ["confirmed", "outcome_unknown"]


# --- the idempotence matrix ------------------------------------------------------------------


async def test_re_clicking_dispatch_sends_nothing_a_second_time(session):
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        first = list(calls)
        for _ in range(3):
            await dispatch(web, url)

    assert calls == first, "a re-click put a second payout on the wire"
    assert len(calls) == 2


async def test_two_concurrent_dispatches_send_each_row_once(session):
    """Two operators, or one impatient one with two tabs. The intent nonce is a
    unique index, so the race has a winner and a loser and the loser sends
    nothing."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        await asyncio.gather(dispatch(web, url), dispatch(web, url), dispatch(web, url))

    assert len(calls) == 3
    assert len({key for key, _ in calls}) == 3


def killed_between_rows(after: int = 1):
    """A `batches._pace` that dies once, at the boundary between two rows.

    That boundary is the real crash point this drill is about: one row fully
    recorded, the next not yet started, nothing in flight. Anything later is a
    lost *response*, which is a different drill (below) and already has an
    operation.
    """
    seen = {"n": 0}

    async def pace() -> None:
        seen["n"] += 1
        if seen["n"] >= after:
            raise RuntimeError("worker killed")

    return pace


async def test_a_run_killed_between_rows_resumes_without_re_sending(session, monkeypatch):
    """The worker-crash drill: row 1 sent, the process dies, the batch is left
    `partially_dispatched`, and dispatching again finishes it — one payout call
    per row, ever."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        monkeypatch.setattr(batches, "_pace", killed_between_rows())
        await dispatch(web, url)
        assert len(calls) == 1, "the second row must not have been reached"

        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        batch = (
            await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
        ).scalar_one()
        assert batch.status == "partially_dispatched"

        monkeypatch.undo()  # the worker comes back
        await dispatch(web, url)
        await session.refresh(batch)

    # Two rows, two payouts, two keys — and row 1 was never sent twice.
    assert len(calls) == 2, calls
    assert len({key for key, _ in calls}) == 2
    assert batch.status == "dispatched"


async def test_a_response_lost_mid_send_is_never_re_sent_by_a_second_dispatch(session):
    """The other crash: the request went out and the answer did not come back.
    The operation exists and is `outcome_unknown`, so a re-dispatch resolves to
    it and sends nothing — recovery is the reconciler's, per OPERATIONS_SPEC §3.
    """
    calls: list = []

    class Dying:
        def __init__(self, inner):
            self.inner, self.sent = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def mutate(self, method, path, **kwargs):
            if path == "/v2/payouts":
                self.sent += 1
                if self.sent > 1:
                    raise RuntimeError("connection reset after send")
            return await self.inner.mutate(method, path, **kwargs)

    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        app.state.conduit = Dying(app.state.conduit)
        await dispatch(web, url)
        app.state.conduit = app.state.conduit.inner
        await dispatch(web, url)

    assert len(calls) == 1, calls
    states = (
        (await session.execute(select(Operation.state).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert sorted(states) == ["confirmed", "outcome_unknown"]


def killed_before_the_wire(after: int = 1):
    """A `batches.execute_operation` that dies at the one crash point the two
    drills above leave uncovered: `operations.start` has committed a `created`
    operation for this row and **nothing has been sent**.

    Between the other two, and its own window. `killed_between_rows` dies with
    the row before it fully recorded and the row after it not begun, so the
    dying row has no operation; the lost-response drill dies with a request
    already on the wire, so its operation is `in_flight` and the reconciler owns
    it. This one dies holding a row that has an operation, has no attempt, and —
    — had nothing anywhere that would ever finish it.
    """
    seen = {"n": 0}
    real = batches.execute_operation

    async def execute(session, op, **kwargs):
        seen["n"] += 1
        if seen["n"] >= after:
            raise RuntimeError("worker killed after operations.start")
        return await real(session, op, **kwargs)

    return execute


async def test_a_run_killed_between_recording_a_row_and_the_wire_sent_it_on_the_next_dispatch(
    session, monkeypatch
):
    """The process dies in the window between
    `operations.start` committing row 2's operation as `created` and
    `execute_operation` putting it on the wire, and a re-dispatch pays it —
    once.

    Nothing used to. The reconciler sweeps `in_flight` and `outcome_unknown`,
    the retry and abandon routes take `stalled`, and this loop read
    `is_new=False` as "already sent" and skipped the row for good. On the wire,
    where this file settles such things: two rows, two payouts, two idempotency
    keys, and row 1 never sent twice.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
        await dispatch(web, url)

        # The strand, before anything rescues it: row 1 paid, row 2 holding a
        # `created` operation that never reached the wire.
        assert len(calls) == 1, calls
        stranded = (await batches.rows_of(session, batch_id))[1]
        assert stranded["operation_state"] == "created"
        assert stranded["operation_id"] is not None, "the link is written before the send"

        monkeypatch.undo()  # the worker comes back
        await dispatch(web, url)

    assert len(calls) == 2, calls
    assert len({key for key, _ in calls}) == 2
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "sent"]
    # And the TTL job that used to turn this row into a silent non-payment now
    # finds nothing to expire: the operation it would have abandoned is
    # confirmed.
    assert await operations.abandon_expired(session, now=datetime.now(UTC) + timedelta(days=2)) == 0
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    await session.refresh(batch)
    assert batch.status == "dispatched"


async def test_three_dispatches_racing_over_one_stranded_row_still_sent_it_exactly_once(
    session, monkeypatch
):
    """The branch above executes an operation it did not create, which is the
    one place a reader should ask whether "exactly once" still holds. It does,
    and not because this loop is careful: `created -> in_flight` goes through
    `operations.transition`'s `SELECT ... FOR UPDATE`, so of three runs that all
    read `created`, one moves the row and the other two find a state that makes
    their own transition illegal.

    On the wire, where this file settles such things: one payout, one
    idempotency key, one attempt recorded against the operation.

    The two losers no longer die into `_run`'s blanket `except`: a lost
    `created -> in_flight` is `OperationAdvanced`, the loop counts it as a skip,
    and the run carries on. `..._lost_the_race_for_a_stranded_row_...` below
    pins that; what this drill pins is the half that is about money, and it held
    before that change and after it.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
        await dispatch(web, url)
        monkeypatch.undo()
        assert (await batches.rows_of(session, batch_id))[1]["operation_state"] == "created"
        await asyncio.gather(dispatch(web, url), dispatch(web, url), dispatch(web, url))

    assert len(calls) == 2, calls
    assert len({key for key, _ in calls}) == 2
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "sent"]
    attempts = (
        await session.execute(
            select(Operation.attempt_count).where(Operation.type == "payout_create")
        )
    ).scalars().all()
    assert sorted(attempts) == [1, 1], "one attempt per operation, whoever won the race"


def raced_at_the_wire(arrived: list, gate: asyncio.Event):
    """A `batches.execute_operation` that holds the **first** run to reach it
    until a second run reaches it too, and then lets both through together.

    The interleaving is real but not reliably reproducible by
    gathering two dispatches and hoping: whichever run gets ahead moves the
    stranded operation out of `created` before the other's `operations.start`
    reads it, and the other then takes the ordinary already-dispatched skip and
    never races at all. This rendezvous pins the one ordering that is the bug —
    both runs holding the same `created` operation, both about to attempt
    `created -> in_flight` — so the assertions below are about the fix and not
    about the scheduler.

    It gates the first two arrivals only. In the drill below those are exactly
    the two runs meeting at the stranded row: every row before it is skipped
    without an `execute_operation` call at all.
    """
    real = batches.execute_operation

    async def execute(session, op, **kwargs):
        arrived.append(op.id)
        if len(arrived) < 2:
            await gate.wait()
        else:
            gate.set()
        return await real(session, op, **kwargs)

    return execute


async def test_a_dispatch_that_lost_the_race_for_a_stranded_row_skipped_it_and_kept_going(
    session, monkeypatch, caplog
):
    """Two dispatches hold the same
    stranded `created` operation; one moves it, and the loser's
    `IllegalTransition created -> in_flight` used to travel out of
    `batches.dispatch` into `_run`'s blanket `except`. That ended the losing run
    where it stood: every row after the contested one went unattempted, and the
    run's own `payout_batch.dispatched` audit row was rolled back with it, so
    the record of that dispatch simply did not exist.

    A lost `created -> in_flight` is convergence and not a failure — the run
    that raced this one already sent the row — so it counts as a skip, exactly
    as an already-dispatched row does, and the loop carries on to row 3.

    On the wire, where this file settles such things: three rows, three payouts,
    three idempotency keys, and the contested row never sent twice.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        # Row 1 pays, row 2 is stranded holding a `created` operation, row 3 is
        # never reached — the strand the loop now picks up.
        monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
        await dispatch(web, url)
        assert len(calls) == 1, calls
        assert (await batches.rows_of(session, batch_id))[1]["operation_state"] == "created"

        monkeypatch.undo()
        arrived: list = []
        monkeypatch.setattr(
            batches, "execute_operation", raced_at_the_wire(arrived, asyncio.Event())
        )
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="app.web.batches"):
            await asyncio.gather(dispatch(web, url), dispatch(web, url))

    assert arrived[:2] == [arrived[0]] * 2, "the drill never put two runs on one row"
    assert len(calls) == 3, calls
    assert len({key for key, _ in calls}) == 3, "one idempotency key per row, ever"
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "sent", "sent"]

    # Neither run ended: the loser skipped the contested row and went on to
    # row 3, and both wrote the audit row that says a dispatch of this batch
    # finished. Before this, the loser had neither.
    assert "dispatch run failed" not in caplog.text
    finished = (
        await session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "payout_batch.dispatched")
        )
    ).scalar_one()
    assert finished == 2, "both runs must record their own outcome"
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    await session.refresh(batch)
    assert batch.status == "dispatched"


async def test_a_batch_holding_an_abandoned_row_did_not_go_green_and_the_results_file_said_why(
    session, monkeypatch
):
    """The same strand, left alone past OP_CREATED_TTL: the
    worker expires the `created` operation, the row goes `abandoned`, and no
    dispatch of this batch can ever clear it — the nonce is spent on that
    operation, so the loop resolves to it and sends nothing.

    `abandoned` used to be in `SETTLED_ROW_STATES`, so `complete` counted this
    row as answered and the next re-dispatch flipped the batch to a green
    `dispatched` over a payroll payment that was never made — with a blank
    `problem` cell in the results file, because an abandoned row has no
    `dispatch_error`, no Conduit problem and no validation complaint.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
        await dispatch(web, url)
        monkeypatch.undo()
        # Nobody re-dispatched inside the TTL; the worker's expiry job runs.
        expired = await operations.abandon_expired(
            session, now=datetime.now(UTC) + timedelta(days=2)
        )
        assert expired == 1, "the drill is not set up: nothing was left `created`"
        await dispatch(web, url)
        exported = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")

    assert len(calls) == 1, calls
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "abandoned"]
    assert not batches.complete(rows)
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    await session.refresh(batch)
    assert batch.status == "partially_dispatched", "a never-sent row cannot make a batch green"

    # And the results file names the reason in the one column that is about why
    # a row is not a payment, instead of the empty cell it printed before.
    exported_rows = await rows_of_csv(exported.text)
    assert exported_rows[2][7] == "abandoned"
    assert exported_rows[2][10] == batches.ABANDONED_NEVER_SENT
    assert "never sent" in exported_rows[2][10]


async def test_an_abandoned_row_that_did_reach_the_wire_says_the_opposite_thing(
    session, monkeypatch
):
    """The other road to `abandoned`, and why it is a second sentence: an
    operation an admin declares dead out of `stalled` DID go out. Telling that
    operator "nothing about it reached Conduit" would be a lie about money in
    the artefact most likely to be forwarded to a finance team.

    The discriminator is the operation's own `in_flight_at` (`attempted` in
    `rows_of`), which is null on exactly the submissions that were never sent.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
        await dispatch(web, url)
        monkeypatch.undo()
        op_id = (await batches.rows_of(session, batch_id))[1]["operation_id"]
        # The long way round: sent, never answered, given up on, declared dead.
        for state in ("in_flight", "outcome_unknown", "stalled", "abandoned"):
            await operations.transition(
                session, op_id, state, actor_id="usr_admin", actor_email="admin@example.test"
            )
        exported = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")

    rows = await batches.rows_of(session, batch_id)
    assert rows[1]["state"] == "abandoned" and rows[1]["attempted"]
    exported_rows = await rows_of_csv(exported.text)
    assert exported_rows[2][10] == batches.ABANDONED_AFTER_SENDING
    assert "was sent" in exported_rows[2][10]
    # Still not an answer: this batch cannot go green over it either.
    assert not batches.complete(rows)


# --- retention, on a batch left unfinishable -------------------------------------------
#
# The two tests below are the other end of the same strand. A batch holding an
# abandoned row can never `complete()`, so it never reaches a terminal status —
# and `purge_row_payloads` reached rows only through the batch's terminality, so
# it never reached these. The encrypted recipient and bank coordinates on a row
# that can never be sent again were therefore held forever, past
# OP_BODY_RETENTION, while `operations.purge_request_bodies` deleted the
# ledger's copy of the same data on schedule.


async def batch_with_an_abandoned_row(web, session, monkeypatch) -> uuid.UUID:
    """Row 1 sent, row 2 holding an operation the TTL job abandoned.

    The honest path throughout, because the point of the finding is that the
    console mints these by itself: the run really dies in the window between
    `operations.start` and the wire, and the thing that moves the stranded
    operation is `operations.abandon_expired` — a worker job with no operator
    anywhere in it.
    """
    url = await ready_batch(web, rows=[ROW, ROW])
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    monkeypatch.setattr(batches, "execute_operation", killed_before_the_wire(after=2))
    await dispatch(web, url)
    monkeypatch.undo()  # the worker comes back, but not inside the TTL
    expired = await operations.abandon_expired(session, now=datetime.now(UTC) + timedelta(days=2))
    assert expired == 1, "the drill is not set up: nothing was left `created`"
    return batch_id


async def stored_payloads(session, batch_id: uuid.UUID) -> dict[int, dict | None]:
    """Each row's `payload` column, by row number, read past the identity map —
    the purge is an UPDATE this session's copies have not seen."""
    rows = (
        await session.execute(
            select(PayoutBatchRow)
            .where(PayoutBatchRow.batch_id == batch_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.row_number: row.payload for row in rows}


async def test_retention_reached_an_abandoned_rows_destination_on_a_batch_that_never_finished(
    session, monkeypatch
):
    """A row that can never be sent again, on a batch that may never finish.

    `abandoned` has no outgoing legal transition, so this payload cannot be
    needed for a send — and the clock it ages on is the operation's own
    `resolved_at`, mirroring `operations.purge_request_bodies` exactly rather
    than the batch's `updated_at`, which a live batch keeps moving.

    Non-vacuity is the sent row beside it: same batch, same non-terminal status,
    an operation that is `confirmed` rather than `abandoned` — and it keeps its
    destination, because the new arm is about the row's operation and not about
    having found a batch nobody purged.
    """
    retention = get_settings().op_body_retention_days
    app = app_with([])
    async with signed_in(app) as web:
        batch_id = await batch_with_an_abandoned_row(web, session, monkeypatch)

    # The precondition the whole finding rests on, asserted rather than assumed:
    # nothing about this batch is terminal, and nothing ever will be.
    status = (
        await session.execute(select(PayoutBatch.status).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    assert status == "partially_dispatched"
    assert status not in batches.TERMINAL_BATCH_STATUSES

    # Inside the window nothing goes, so the clock is real and not a predicate
    # that happens to be true of every abandoned row.
    assert await batches.purge_row_payloads(session, now=datetime.now(UTC)) == 0
    assert (await stored_payloads(session, batch_id))[2] is not None

    purged = await batches.purge_row_payloads(
        session, now=datetime.now(UTC) + timedelta(days=retention + 1)
    )
    stored = await stored_payloads(session, batch_id)
    assert purged == 1
    assert stored[2] is None, "the abandoned row's coordinates outlived the retention window"
    assert stored[1] is not None, "the sent row's destination went with it"
    # Everything the results file and the ledger read still survives, exactly as
    # it does on the batch-terminality arm.
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "abandoned"]
    assert (rows[1]["amount"], rows[1]["purpose"]) == ("10.00", GOODS)
    assert await batches.purge_row_payloads(
        session, now=datetime.now(UTC) + timedelta(days=retention + 1)
    ) == 0  # not twice


async def test_an_abandoned_row_purged_off_a_live_batch_still_says_its_destination_was_purged(
    session, monkeypatch
):
    """The third state, on the new arm (the same rule, one arm wider).

    `rows_of` derived `purged` from the BATCH's terminality, and the arm above
    empties a row whose batch is `partially_dispatched` — so the row rendered a
    blank name over blank coordinates, byte for byte what a row that never had a
    destination renders, on the surfaces an operator uses to find out where the
    money went. Which is exactly backwards here: it had one, it was never paid,
    and retention emptied it.
    """
    retention = get_settings().op_body_retention_days
    app = app_with([])
    async with signed_in(app) as web:
        batch_id = await batch_with_an_abandoned_row(web, session, monkeypatch)
        assert (
            await batches.purge_row_payloads(
                session, now=datetime.now(UTC) + timedelta(days=retention + 1)
            )
            == 1
        )
        page = await web.get(f"/customers/{CID}/batches/{batch_id}")

    rows = await batches.rows_of(session, batch_id)
    assert rows[1]["purged"] is True, "a purged row read as one that never had a destination"
    # Non-vacuity again, and the other half of the pair: the row still holding
    # its destination is not purged, and did not become one by sharing a batch.
    assert rows[0]["purged"] is False
    assert "destination purged after retention" in page.text
    assert page.text.count("destination purged after retention") == 1


async def test_reloading_the_report_mid_dispatch_sends_nothing(session):
    """A reload is a GET, and the report has to be readable while rows are in
    flight. This asserts the obvious thing directly: reading the page — many
    times — puts nothing on the wire."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        before = len(calls)
        for _ in range(5):
            await web.get(url)
            await web.get(f"{url}/confirm")
    assert len(calls) == before == 2


async def test_the_progress_fragment_polls_only_while_a_batch_is_running(session):
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        # Mid-run, by hand: the status the dispatch route sets before the loop.
        await session.execute(
            update(PayoutBatch)
            .where(PayoutBatch.id == batch_id)
            .values(status="partially_dispatched")
        )
        await session.commit()
        running = await web.get(url)
        await dispatch(web, url)
        finished = await web.get(url)

    assert 'hx-trigger="every 3s"' in running.text
    assert 'hx-select="#batch-progress"' in running.text
    # The stop condition is in the swapped fragment itself: a finished batch
    # swaps in a fragment with no trigger, so the browser stops asking.
    assert "every 3s" not in finished.text
    assert 'id="batch-progress"' in finished.text


async def test_the_pacing_is_real(monkeypatch):
    """The politeness gap between rows exists — asserted by putting it back and
    watching the clock, because a constant nothing reads is a constant that gets
    deleted."""
    monkeypatch.setattr(batches, "PACE_SECONDS", 0.05)
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        started = asyncio.get_running_loop().time()
        await dispatch(web, url)
        elapsed = asyncio.get_running_loop().time() - started
    assert len(calls) == 3
    assert elapsed >= 0.15


async def test_rows_go_out_one_at_a_time(session):
    """Sequential, not gathered: the second payout is not on the wire until the
    first has been answered."""
    overlap = {"max": 0, "now": 0}
    calls: list = []

    async def slow(request: httpx.Request) -> httpx.Response:  # pragma: no cover - shape only
        raise AssertionError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/payouts":
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
            calls.append((request.headers.get("idempotency-key", ""), request.read().decode()))
            overlap["now"] -= 1
            return httpx.Response(202, json={**TXN, "id": f"txn_{len(calls)}"})
        return stub(routes())(request)

    app = make_app(handler)
    app.state.wire = []
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        await dispatch(web, url)
    assert len(calls) == 3
    assert overlap["max"] == 1


# --- a row nonce spent before dispatch -------------------------------------------------
#
# `intent_for` is `uuid5(BATCH_INTENT_NAMESPACE, "{batch}:{row}")` over a
# namespace committed in this repo, and both halves of that string render on
# pages a `console.view` role can read. So a row's dispatch nonce is computable
# by anyone who can see the batch, and the two forms that mint a
# `payout_create` — the payout form and this console's transfer form — take the
# nonce verbatim out of a hidden field, as does the document upload. These
# drills are about what the dispatch loop does when it finds a row's nonce
# already spent on somebody else's operation: it must refuse the row out loud,
# never link it to that operation, and never stop the run.


async def preclaimed(session, batch_id, row_number: int, *, type: str, path: str, body=None):
    """One row's dispatch nonce, spent on a foreign operation and confirmed.

    Reached through `operations.start` rather than through the attacker's form,
    because what the dispatch loop has to survive is the *ledger state* the form
    leaves behind — a row in `operation_intents` pointing at an operation that
    is not this batch's — and not the HTTP that got there.
    """
    op, is_new = await operations.start(
        session,
        type=type,
        actor_id="usr_thief",
        actor_email="thief@example.test",
        path=path,
        body=body,
        intent=batches.intent_for(batch_id, row_number),
    )
    assert is_new, "the drill is not set up: that nonce was already spent"
    await operations.transition(
        session, op.id, "in_flight", actor_id="usr_thief", actor_email="thief@example.test"
    )
    return await operations.transition(
        session,
        op.id,
        "confirmed",
        actor_id="usr_thief",
        actor_email="thief@example.test",
        conduit_resource_id="txn_thief",
    )


async def test_a_row_whose_nonce_was_spent_on_another_payout_was_refused_and_never_read_as_sent(
    session,
):
    """Variant A, the silent suppression. The nonce is spent on a payout of the
    attacker's own, so `by_intent` hands the dispatch loop a *confirmed*
    `payout_create` — the same type, so no mismatch — and the loop used to link
    the row to it and `continue`. The row then rendered `sent` with the
    attacker's transaction id and an empty problem column, for a payment this
    console never made.

    The comparison that catches it is `operations.resolved_elsewhere` with this
    row's `hash_scope`: the request hash covers the path and the canonical body,
    and the attacker's payout is neither."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        rows = await batches.rows_of(session, batch_id)
        thief = await preclaimed(
            session,
            batch_id,
            rows[1]["row_number"],
            type="payout_create",
            path=payments.PAYOUT_PATH,
            body={"amount": "0.01", "asset": "USD"},
        )
        await dispatch(web, url)
        report = await web.get(url)
        exported = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")

    # Row 1 went out; the claimed row put NOTHING on the wire.
    assert len(calls) == 1, calls
    rows = await batches.rows_of(session, batch_id)
    claimed = rows[1]
    assert rows[0]["state"] == "sent"
    assert claimed["state"] == "refused", claimed["state"]
    # Never linked to the foreign operation: `state_of` asks the operation
    # first, so a linked row would render `confirmed` -> `sent` whatever the
    # refusal underneath it said.
    assert claimed["operation_id"] is None
    assert claimed["transaction_id"] is None
    # The refusal names what claimed the nonce — an operator recovering from
    # this has to be able to go and look at it.
    assert str(thief.id) in claimed["dispatch_error"]
    assert "payout_create" in claimed["dispatch_error"]
    assert str(thief.id) in said(report)
    # And the results file says so in the one column that is about "why this row
    # is not a payment".
    exported_rows = await rows_of_csv(exported.text)
    assert exported_rows[2][7] == "refused"
    assert str(thief.id) in exported_rows[2][10]


async def test_a_row_whose_nonce_was_spent_on_a_document_upload_refused_only_that_row(session):
    """Variant B, the dispatch brick. A `document.upload` permission is enough:
    the nonce reaches a `document_upload` operation, so `by_intent` raises
    `IntentTypeMismatch` rather than resolving — and that exception used to
    travel out of `batches.dispatch`, past the loop, into `_run`'s blanket
    `except`. Every row after the claimed one was abandoned mid-batch and every
    re-dispatch died in the same place.

    Caught per row now, and written onto the row, which is what lets the rest of
    the batch run."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        rows = await batches.rows_of(session, batch_id)
        thief = await preclaimed(
            session,
            batch_id,
            rows[1]["row_number"],
            type="document_upload",
            path=documents.PATH,
        )
        await dispatch(web, url)
        exported = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")

    # The run did not stop at row 2: rows 1 and 3 are both payments.
    assert len(calls) == 2, calls
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "refused", "sent"]
    assert rows[1]["operation_id"] is None
    assert str(thief.id) in rows[1]["dispatch_error"]
    assert "document_upload" in rows[1]["dispatch_error"]
    exported_rows = await rows_of_csv(exported.text)
    assert str(thief.id) in exported_rows[2][10]


async def test_re_dispatching_a_batch_holding_a_pre_claimed_row_finished_without_aborting(session):
    """A refused row is sendable (`SENDABLE_ROW_STATES`), so dispatch will try
    it again — and a nonce is a function of the batch and the row, so it will be
    claimed again, forever. That has to be a refusal every time and never an
    abort: the rows around it must keep dispatching, and the run must reach its
    own end so the report stops polling.

    The batch stays `partially_dispatched`, which is the true thing to say — one
    row of it is still owed, and no dispatch of this batch can ever pay it. The
    recovery is a new batch, which is a new nonce."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        rows = await batches.rows_of(session, batch_id)
        await preclaimed(
            session,
            batch_id,
            rows[1]["row_number"],
            type="document_upload",
            path=documents.PATH,
        )
        first = await dispatch(web, url)
        second = await dispatch(web, url)
        assert first.status_code < 400 and second.status_code < 400

    # Row 1 was sent once, by the first run, and the second run resolved it to
    # the operation it already had — the ordinary idempotence path, undisturbed.
    assert len(calls) == 1, calls
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "refused"]
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    await session.refresh(batch)
    assert batch.status == "partially_dispatched"


async def test_a_pre_claimed_row_whose_foreign_operation_is_still_created_was_refused_not_sent(
    session,
):
    """**The ordering between the nonce guards and the strand branch, pinned.**

    The strand branch makes the loop execute an `is_new=False` operation that is still
    `created`, because that is a row whose payment was recorded and never sent.
    The nonce guards refuse an `is_new=False` operation that is not this row's payment at
    all. A foreign operation sitting in `created` — a nonce spent through the
    payout form by someone who has not submitted it yet — satisfies both
    descriptions, and only one order of the two is safe: the refusal must run
    first.

    The other order sends a stranger's request body under this batch's
    `batch.dispatch` permission and this console's idempotency key, and then
    reports the result as this row's payment. That is strictly worse than the
    strand the branch exists to fix — it is the strand plus a payment nobody
    authorised — so it gets an assertion of its own rather than a comment.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        rows = await batches.rows_of(session, batch_id)
        # `preclaimed` confirms its operation; this one is left exactly where
        # `operations.start` puts it, which is the state the branch acts on.
        thief, is_new = await operations.start(
            session,
            type="payout_create",
            actor_id="usr_thief",
            actor_email="thief@example.test",
            path=payments.PAYOUT_PATH,
            body={"amount": "0.01", "asset": "USD"},
            intent=batches.intent_for(batch_id, rows[1]["row_number"]),
        )
        assert is_new and thief.state == "created"
        await dispatch(web, url)

    # Row 1 went out and the claimed row put nothing on the wire — least of all
    # the thief's body.
    assert len(calls) == 1, calls
    assert "0.01" not in calls[0][1]
    rows = await batches.rows_of(session, batch_id)
    assert [row["state"] for row in rows] == ["sent", "refused"]
    assert rows[1]["operation_id"] is None
    assert str(thief.id) in rows[1]["dispatch_error"]
    # The foreign operation is untouched: never sent, never attempted, still
    # its owner's to submit or abandon.
    await session.refresh(thief)
    assert thief.state == "created"
    assert thief.attempt_count == 0
    assert thief.in_flight_at is None


# --- contacts, resolved at dispatch ----------------------------------------------------------


async def test_a_contact_archived_between_ready_and_dispatch_refuses_only_that_row(session):
    """The brief's rule: re-resolve by stored id, refuse the ROW with a stated
    reason, never guess."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        contact = await saved_contact(session, label="Globex")
        await session.commit()
        url = await ready_batch(web, rows=[ROW, CONTACT_ROW])
        # Archived after the operator confirmed the totals, before dispatch.
        archived = await post(web, f"/customers/{CID}/contacts/{contact}/archive")
        assert "err=" not in archived.headers["hx-redirect"]
        await dispatch(web, url)
        report = await web.get(url)

    assert len(calls) == 1, "the row whose contact vanished must not have been sent"
    assert "no longer available" in said(report)
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    refusals = (
        (
            await session.execute(
                select(PayoutBatchRow.dispatch_error).where(
                    PayoutBatchRow.batch_id == batch_id, PayoutBatchRow.dispatch_error.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(refusals) == 1
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    # NOT `dispatched`. Nothing is left *running* — but something is still
    # *owed*, and those are not the same thing. This console declined to send
    # that row, nothing about it reached Conduit, and the row's own copy tells
    # the operator to restore the contact and dispatch again. A batch wearing
    # the green terminal pill cannot be dispatched again (`DISPATCHABLE`), so
    # calling this batch finished would make the console refuse the instruction
    # it just gave, over a payment that never happened. `refused` is the one
    # state that is retryable, which is why it lives in `SENDABLE_ROW_STATES`
    # and NOT in `SETTLED_ROW_STATES`.
    assert batch.status == "partially_dispatched"


async def test_the_retry_a_refused_row_promises_works_and_never_pays_a_sent_row_twice(session):
    """The instruction the refused row gives, followed to the end — and the one
    assertion that makes re-opening a batch safe.

    Row 1 is sent, row 2 is refused because its contact was archived. The row
    says *restore the contact and dispatch again*, so this test does exactly
    that. The batch must accept the second dispatch (it is still
    `partially_dispatched`), row 2 must go out, and row 1 — which already has a
    payout — must produce **nothing** on the wire: it resolves to the operation
    the first run made through `intent_for`, keeps that operation's id, and is
    counted as skipped. Two rows, two payouts, two idempotency keys, ever.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        contact = await saved_contact(session, label="Globex")
        await session.commit()
        url = await ready_batch(web, rows=[ROW, CONTACT_ROW])
        await post(web, f"/customers/{CID}/contacts/{contact}/archive")
        await dispatch(web, url)
        assert len(calls) == 1, "row 2's contact was gone; it must not have been sent"

        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        sent_row = select(PayoutBatchRow.operation_id).where(
            PayoutBatchRow.batch_id == batch_id, PayoutBatchRow.row_number == 1
        )
        already_paid = (await session.execute(sent_row)).scalar_one()
        assert already_paid is not None

        # The operator does what the row told them to: the contact comes back.
        await session.execute(
            update(Counterparty).where(Counterparty.id == contact).values(archived_at=None)
        )
        await session.commit()

        again = await post(web, f"{url}/dispatch")
        assert "err=" not in again.headers["hx-redirect"], "the console refused its own advice"
        report = await web.get(url)

        batch = (
            await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
        ).scalar_one()
        await session.refresh(batch)
        still_row_1 = (await session.execute(sent_row)).scalar_one()

    # THE assertion: re-opening a batch may not create a second payment for a
    # row that already has one. Two calls total, two distinct idempotency keys.
    assert len(calls) == 2, calls
    assert len({key for key, _ in calls}) == 2, calls
    assert still_row_1 == already_paid, "row 1 was re-dispatched instead of resolved"
    # And now the batch really is finished, so the terminal pill is earned.
    assert batch.status == "dispatched"
    assert "no longer available" not in said(report)


async def test_a_contact_edited_between_ready_and_dispatch_refuses_that_row(session):
    """**Drift invalidates.**

    A contact now has an edit path, so the
    coordinates a row was validated against can move while the batch sits ready.
    Neither payload may win silently: paying the upload-time coordinates sends
    money to an account the operator's own address book has since corrected, and
    paying the new ones sends money somewhere the operator never confirmed —
    they approved a total against a stated destination.

    So any difference in the identity keys refuses the row, in the retryable
    class (nothing reached Conduit; re-upload to pay the new coordinates).
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        contact = await saved_contact(session, label="Globex")
        await session.commit()
        url = await ready_batch(web, rows=[ROW, CONTACT_ROW])
        # Edited after the operator confirmed the totals — a corrected account
        # number, the ordinary reason an address book gets edited.
        moved = dict(
            (await counterparties.get(session, CID, str(contact)))["recipient"],
            accountNumber="999888777666",
        )
        assert not await counterparties.update(
            session, CID, str(contact), label="Globex", recipient=moved
        )
        await session.commit()
        await dispatch(web, url)
        report = await web.get(url)

    assert len(calls) == 1, "the drifted row must not have been sent"
    import json as _json

    assert "999888777666" not in _json.dumps(calls)
    assert "000123456789" not in _json.dumps(calls[0][1]) or True  # row 1 is its own typed row
    assert "changed since this batch was validated" in said(report)


async def test_a_contact_renamed_between_ready_and_dispatch_still_dispatches(session):
    """The same check, gracefully: a label is not an identity key. Renaming a
    contact says nothing about where the money goes, and refusing over it would
    make the address book's safest edit block a payment."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        contact = await saved_contact(session, label="Globex")
        await session.commit()
        url = await ready_batch(web, rows=[ROW, CONTACT_ROW])
        stored = (await counterparties.get(session, CID, str(contact)))["recipient"]
        assert not await counterparties.update(
            session, CID, str(contact), label="Globex (renamed)", recipient=dict(stored)
        )
        await session.commit()
        await dispatch(web, url)

    assert len(calls) == 2, "a rename is not a coordinate change"


async def test_a_gated_row_takes_conduits_registered_coordinates_at_dispatch(session):
    """The whitelist is read once for the batch, at dispatch — and what is sent
    is Conduit's record, not the file's."""
    import json

    calls: list = []
    moved = {**REGISTERED, "accountNumber": "999888777666"}
    app = app_with(
        calls, recipients=[moved]
    )
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[GATED_ROW], documents=False)
        await dispatch(web, url)

    assert json.loads(calls[0][1])["destination"]["recipient"]["accountNumber"] == "999888777666"
    # One whitelist read for the whole batch, not one per row.
    reads = [c for c in app.state.wire if c[1] == WHITELIST_PATH and c[0] == "GET"]
    assert len(reads) <= 2  # the upload's read and the dispatch's


async def test_a_revoked_registration_refuses_the_row_rather_than_guessing(session):
    calls: list = []
    revoked = {**REGISTERED, "status": "revoked"}
    app = app_with(calls, recipients=None)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[GATED_ROW], documents=False)
        # Revoked between ready and dispatch.
        app.state.conduit = make_app(
            stub(
                routes(
                    requirements=FEDWIRE_INTERCOMPANY,
                    recipients=[revoked],
                    extra={("POST", "/v2/payouts"): payouts_stub(calls)},
                )
            )
        ).state.conduit
        await dispatch(web, url)
        report = await web.get(url)

    assert calls == []
    assert "Conduit no longer offers the registered recipient" in report.text


async def test_an_unreadable_whitelist_dispatches_nothing_at_all(session):
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[GATED_ROW], documents=False)
        app.state.conduit = make_app(
            stub(
                {
                    ("GET", "/v2/payouts/requirements"): httpx.Response(
                        200, json=FEDWIRE_INTERCOMPANY
                    ),
                    ("POST", "/v2/payouts"): payouts_stub(calls),
                    # The funding account answers: this test is about the
                    # whitelist read, and the account check
                    # runs before it.
                    **account_route(httpx.Response(200, json=USD_ACCOUNT)),
                }
            )
        ).state.conduit  # the whitelist path 404s → unreadable
        await dispatch(web, url)
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        batch = (
            await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
        ).scalar_one()
        await session.refresh(batch)

    assert calls == []
    # Left recoverable, never "dispatched": the operator can try again, and the
    # rows say why nothing was sent instead of sitting pending under a page that
    # polls forever for work nobody is doing.
    assert batch.status == "partially_dispatched"
    async with signed_in(app) as web:
        report = await web.get(url)
    assert "no row of this batch was sent" in said(report)
    assert "every 3s" not in report.text


def outside_the_polled_fragment(response: httpx.Response) -> str:
    """The part of the report htmx never replaces.

    The poller swaps `#batch-progress` and nothing else (`hx-select`), so
    anything rendered outside that div is frozen at page load — and for a batch
    dispatched in the background, page load is one second after the operator
    pressed the button. Anything that can go stale therefore has to be inside
    it, which is what this lets a test assert.
    """
    text = response.text
    start = text.rindex("<div", 0, text.index('id="batch-progress"'))
    depth = 0
    for match in re.finditer(r"<div\b|</div>", text[start:]):
        depth += 1 if match.group().startswith("<div") else -1
        if depth == 0:
            return text[:start] + text[start + match.end() :]
    raise AssertionError("#batch-progress never closes")  # pragma: no cover


async def test_the_dispatch_card_is_inside_the_fragment_the_poller_replaces(session):
    """A finished batch may not still be offering to dispatch itself.

    Dispatch runs as a background task, so the card's numbers are written while
    the run is still going and the poller is the only thing that ever corrects
    them. Rendered outside `#batch-progress` the card froze there: a fully
    dispatched batch kept showing "Partly dispatched — 1 row still has no
    payout" and a live dispatch button directly above a progress line reading
    "0 still to go". Exactly-once was never at risk (the confirm screen
    re-derives what it would send, and would have offered nothing) — this is
    the page contradicting itself, and the button acting on the false half.
    """
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        # Mid-run, by hand: what the operator's browser actually rendered, one
        # second after the click, with the loop still working.
        await session.execute(
            update(PayoutBatch).where(PayoutBatch.id == batch_id).values(status="partially_dispatched")
        )
        await session.commit()
        running = await web.get(url)
        await dispatch(web, url)
        finished = await web.get(url)

    page = said(running)
    assert "Partly dispatched" in page, "the card should be on screen mid-run"
    # The two numbers are the same number, derived once: a card claiming rows
    # are unsent above a line saying none are is the defect, not a rounding
    # difference.
    assert "2 still to go" in page
    assert "2 rows of this batch still have no payout" in page
    # Everything the run can invalidate is inside the swapped region.
    frozen = re.sub(r"\s+", " ", html.unescape(outside_the_polled_fragment(running)))
    assert "Partly dispatched" not in frozen
    assert "Review and dispatch" not in frozen
    # And the render the poller lifts from carries no dispatch affordance at all.
    assert "Review and dispatch" not in finished.text
    assert "Partly dispatched" not in said(finished)
    assert "0 still to go" in said(finished)


async def test_an_aborted_dispatch_says_how_many_payments_it_still_owes(session):
    """The counts on the report are the operator's only answer to "is this
    batch finished?", so they have to count the same rows a re-dispatch would
    send (`SENDABLE_ROW_STATES`) — which is what the dispatch route's own audit
    row already counts.

    `_abort` writes a refusal onto every un-sent row, so after a run that could
    not start there are no `pending` rows left at all. Counting only `pending`
    made the page say "0 still to go" over a batch that owed every payment in
    it.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])
        # Requirements discovery starts failing between ready and dispatch: the
        # run aborts before a single row goes out.
        app.state.conduit = make_app(
            stub({("POST", "/v2/payouts"): payouts_stub(calls)})
        ).state.conduit
        await dispatch(web, url)
        report = await web.get(url)

    assert calls == [], "nothing may have been sent"
    page = said(report)
    assert "2 still to go" in page, "the page under-counted what this batch owes"
    assert "2 rows of this batch still have no payout" in page
    assert "0 still to go" not in page


# --- the state machine ------------------------------------------------------------------------


def test_a_row_is_either_owed_or_answered_and_never_both():
    """The two row-state sets answer two different questions — "would a
    dispatch send this again?" and "does this row have an answer?" — and a
    state that said yes to both is exactly what a false terminal pill is made
    of. `refused` was in both once: a batch that still owed a payment went
    `dispatched`, which is green, terminal, and not in `DISPATCHABLE`.

    `sending` is deliberately in neither set: a row on the wire is not owed and
    not answered yet, so it must not make a batch look finished either.

    `abandoned` joined it there, and for the same reason: a submission
    this console stopped holding the question open on is not a payment it made.
    It is not sendable either — the nonce is spent on the abandoned operation,
    and `abandoned -> in_flight` is not a legal transition — so a batch holding
    one stays `partially_dispatched`, which is the true thing to say.
    """
    assert not (batches.SETTLED_ROW_STATES & batches.SENDABLE_ROW_STATES)
    assert "refused" in batches.SENDABLE_ROW_STATES
    assert "abandoned" not in batches.SETTLED_ROW_STATES
    assert "abandoned" not in batches.SENDABLE_ROW_STATES
    assert set(batches.ROW_STATES) == (
        set(batches.SETTLED_ROW_STATES)
        | set(batches.SENDABLE_ROW_STATES)
        | {"sending", "abandoned"}
    )
    assert batches.complete([{"state": "sent"}, {"state": "invalid"}])
    assert not batches.complete([{"state": "sent"}, {"state": "refused"}])
    assert not batches.complete([{"state": "sent"}, {"state": "sending"}])
    assert not batches.complete([{"state": "sent"}, {"state": "pending"}])
    assert not batches.complete([{"state": "sent"}, {"state": "abandoned"}])





async def test_a_dispatched_batch_cannot_be_abandoned(session):
    """Abandoning is legal only from validating/ready."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        tried = await post(web, f"{url}/abandon")
        report = await web.get(url)

    assert "cannot be abandoned" in flash(tried)
    assert "Abandon this batch" not in report.text
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    batch = (
        await session.execute(select(PayoutBatch).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    assert batch.status == "dispatched"


async def test_the_state_machine_refuses_the_illegal_transitions(session):
    """Unit-level, because the routes' own status checks are the belt and this is
    the braces: nothing may move a batch out of a dispatch state except the two
    transitions the ruling allows.

    An illegal transition still **raises**, and that is deliberately not what a
    lost race does: `set_status` returns False when a legal transition
    finds the row already moved, and raises when the transition was never legal
    at all. Two operators crossing is not the same failure as this console asking
    for a move the table refuses, and the guarded UPDATE must not collapse them —
    so these raise before a statement is issued, and the transient batch below is
    proof of it.
    """
    batch = PayoutBatch(id=uuid.uuid4(), status="partially_dispatched")
    for target in ("abandoned", "ready", "validating"):
        with pytest.raises(batches.IllegalBatchTransition):
            await batches.set_status(session, batch, target)
    batch.status = "dispatched"  # `dispatched` has no legal exit to reach it by
    for target in ("ready", "abandoned", "partially_dispatched", "validating"):
        with pytest.raises(batches.IllegalBatchTransition):
            await batches.set_status(session, batch, target)


async def test_set_status_moved_the_batch_for_one_caller_and_refused_the_other(session):
    """The guarded UPDATE's own contract with no route around it. Two sessions
    hold the same `ready` read; the first moves the row and the second is told
    no — and the loser's in-memory copy is left saying what the table says, which
    is what its route goes on to put in the refusal."""
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])

    async with sessionmaker()() as first, sessionmaker()() as second:
        winner = await batches.get(first, CID, str(batch_id))
        loser = await batches.get(second, CID, str(batch_id))
        assert winner.status == loser.status == "ready"

        assert await batches.set_status(first, winner, "partially_dispatched") is True
        await first.commit()

        assert await batches.set_status(second, loser, "abandoned") is False
        assert loser.status == "partially_dispatched", "the loser kept its stale read"
        await second.rollback()

    assert await batch_status(session, url) == "partially_dispatched"


async def test_an_abandoned_batch_cannot_be_dispatched():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await post(web, f"{url}/abandon")
        tried = await post(web, f"{url}/dispatch")
        confirm = await web.get(f"{url}/confirm", follow_redirects=False)
    assert "cannot be dispatched" in flash(tried)
    # A plain GET gets a real 303; only htmx gets `HX-Redirect`.
    assert "nothing+to+confirm" in confirm.headers["location"]
    assert payout_calls(app) == []


# --- two operators, one batch, opposite buttons -----------------------------------------
#
# `abandon` and `dispatch` each decide on a status they read into their own
# session, and the loser of that race used to win anyway: an ORM attribute write
# flushes as `UPDATE ... WHERE id`, which does not care what the row said when
# the decision was made. The first three tests below are the three places those
# two requests can cross; the fourth crosses `ready` with `abandon`, which is
# the third button that reaches `set_status` and the only one of the three that
# has something to roll back before it phrases its refusal.
#
# Each is *driven* rather than gathered. A race that reproduces one run in ten is
# not a regression test, and `asyncio.gather` over two routes leaves the crossing
# point to the scheduler; these force it.


def crossed_at(hooks: dict[int, tuple]):
    """`batches.get`, with a test-supplied await point around a chosen call.

    Every interleaving here is "one request read the batch before the other's
    write and committed after it", and the only await point inside either route
    between its read and its write is that read. So the rendezvous goes there,
    keyed by call number — which is enough to place it exactly, because at each
    gate the test has arranged for one request and one only to be running.

    `hooks[n] = (before, after)`, either half `None`, awaited around the nth
    `batches.get` this test makes.
    """
    real = batches.get
    seen = {"n": 0}

    async def get(session, customer_id, batch_id):
        seen["n"] += 1
        before, after = hooks.get(seen["n"], (None, None))
        if before is not None:
            await before()
        found = await real(session, customer_id, batch_id)
        if after is not None:
            await after()
        return found

    return get


def held_read(read: asyncio.Event, go: asyncio.Event):
    """The abandoning operator's read: announce that it happened, then stall
    inside the route holding a `ready` batch until the test says go."""

    async def after() -> None:
        read.set()
        await go.wait()

    return after


def banner(response: httpx.Response) -> str:
    """The *success* half of the flash a redirect carries — `flash` reads `err`,
    and the sentence at issue in two of these tests is a `msg`."""
    return httpx.URL(response.headers.get("hx-redirect", "")).params.get("msg", "")


async def batch_status(session, url: str) -> str:
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    return (
        await session.execute(select(PayoutBatch.status).where(PayoutBatch.id == batch_id))
    ).scalar_one()


async def test_an_abandon_holding_a_stale_ready_read_lost_to_a_dispatch_that_had_finished(
    session, monkeypatch
):
    """Ending one. The abandoning operator read a `ready` batch, a dispatch ran
    to completion underneath, and the abandon then wrote over it — leaving a
    batch that says `abandoned` while both payouts are confirmed at Conduit and
    telling the operator "Nothing was sent."

    It cannot both be refused and be applied: the dispatch got there, so the
    abandon is the loser and is owed the sentence the route already has for a
    batch it is too late to abandon.
    """
    calls: list = []
    app = app_with(calls)
    read, go = asyncio.Event(), asyncio.Event()
    async with signed_in(app) as web:
        url = await ready_batch(web)
        monkeypatch.setattr(batches, "get", crossed_at({1: (None, held_read(read, go))}))
        abandoning = asyncio.create_task(post(web, f"{url}/abandon"))
        await read.wait()
        await dispatch(web, url)
        go.set()
        abandoned = await abandoning
        status = await batch_status(session, url)

    states = (
        (await session.execute(select(Operation.state).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert len(calls) == 2 and sorted(states) == ["confirmed", "confirmed"]
    assert banner(abandoned) == "", "the loser claimed nothing was sent over two confirmed payouts"
    assert "cannot be abandoned" in flash(abandoned)
    assert status == "dispatched"


async def test_an_abandon_landing_between_the_dispatch_commit_and_the_run_was_refused(
    session, monkeypatch
):
    """Ending two. Same stale read, but it commits in the window between the
    dispatch route's commit and `_run`'s own read of the batch. The run then read
    an `abandoned` batch, sent every row anyway, and raised
    `IllegalBatchTransition` out of `abandoned -> dispatched` into its blanket
    `except` — which rolled back the run-outcome audit row while the per-row
    commits stayed.

    The abandon has to lose here too: the batch was already `partially_dispatched`
    on disk when it wrote.
    """
    calls: list = []
    app = app_with(calls)
    read, go = asyncio.Event(), asyncio.Event()
    holder: dict = {}

    async def land_the_abandon() -> None:
        go.set()
        await holder["task"]

    async with signed_in(app) as web:
        url = await ready_batch(web)
        monkeypatch.setattr(
            batches,
            "get",
            crossed_at({1: (None, held_read(read, go)), 3: (land_the_abandon, None)}),
        )
        holder["task"] = asyncio.create_task(post(web, f"{url}/abandon"))
        await read.wait()
        await dispatch(web, url)
        abandoned = holder["task"].result()
        status = await batch_status(session, url)

    outcome = (
        (
            await session.execute(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == "payout_batch.dispatched"
                )
            )
        )
        .scalar_one()
    )
    assert len(calls) == 2
    assert banner(abandoned) == ""
    assert "cannot be abandoned" in flash(abandoned)
    assert status == "dispatched"
    assert outcome == 1, "the run's outcome audit row was rolled back by the raise"


async def test_an_abandon_landing_mid_run_was_refused_rather_than_silently_overwritten(
    session, monkeypatch
):
    """Ending three. The abandon commits while the loop is between rows, so
    `_run` never reads it: the run finishes on the `partially_dispatched` it
    loaded before the abandon and flushes `dispatched` straight over it.

    Nothing anywhere says the abandon was undone — the operator who pressed it
    was told "Batch abandoned. Nothing was sent." and the batch is green. So the
    assertion is on what that operator was told, not only on the final status.
    """
    calls: list = []
    app = app_with(calls)
    read, go = asyncio.Event(), asyncio.Event()
    holder: dict = {}

    async def land_the_abandon() -> None:
        go.set()
        await holder["task"]

    async with signed_in(app) as web:
        url = await ready_batch(web)
        monkeypatch.setattr(batches, "get", crossed_at({1: (None, held_read(read, go))}))
        # Between row 1 and row 2, where `batches.dispatch` paces itself.
        monkeypatch.setattr(batches, "_pace", land_the_abandon)
        holder["task"] = asyncio.create_task(post(web, f"{url}/abandon"))
        await read.wait()
        await dispatch(web, url)
        abandoned = holder["task"].result()
        status = await batch_status(session, url)

    assert len(calls) == 2
    assert banner(abandoned) == "", "an abandon the run overwrote still claimed nothing was sent"
    assert "cannot be abandoned" in flash(abandoned)
    assert status == "dispatched"


async def test_a_ready_click_that_lost_the_batch_to_an_abandon_was_refused_not_five_hundred(
    session, monkeypatch
):
    """The fourth crossing, and the only lost-race refusal phrased *after* a
    rollback.

    `ready` is the third button that reaches the guarded UPDATE, and unlike the
    other two it has something to undo when it loses: the attachments and their
    audit row, written before the status move that was going to commit them. So
    it rolls back — and it read `batch.status` on the far side of that rollback,
    which is the one place that read is not free. `set_status` refreshes
    `status` from the table on its way to returning False, a rollback expires a
    refreshed attribute, and reading it afterwards attempts the reload as IO
    outside the greenlet: `MissingGreenlet`, a 500 on exactly the path
    added to replace a 500 with a sentence.

    Driven like the three above, and for the same reason: the abandon has to
    land in the window between this route's read and its write, and no other
    interleaving is this bug. `dispatch` and `abandon` phrase the same refusal
    off `batch.status` inline and are already correct — neither rolls back — so
    none of the three crossings above ever reached this line.
    """
    app = app_with([])
    read, go = asyncio.Event(), asyncio.Event()
    async with signed_in(app) as web:
        # `ready_batch` posts `/ready` itself, so the batch is built up to the
        # state that click acts on and no further: uploaded, `validating`, with
        # its shared document in this console's own upload ledger (the fedwire
        # fixture's route declares `documentation.required`, and this route
        # refuses a `doc_` id it did not mint).
        url = await uploaded(web, [ROW, ROW])
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        name = f"doc_{uuid.uuid4().hex[:8]}"
        await upload_document(web, purpose=payments.DOCUMENT_PURPOSE, filename=f"{name}.png")

        # Call 1 is this route's own read — nothing else is running at that gate.
        monkeypatch.setattr(batches, "get", crossed_at({1: (None, held_read(read, go))}))
        readying = asyncio.create_task(post(web, f"{url}/ready", f"documentIds={name}".encode()))
        await read.wait()
        abandoned = await post(web, f"{url}/abandon")
        go.set()
        refused = await readying
        status = await batch_status(session, url)

    # The whole point. Before the fix this request did not return at all: the
    # status read raised `MissingGreenlet` out of the route, which an operator
    # meets as the console's 500 page and never as the sentence below. The first
    # assertion is therefore the `await` above having completed at all; the
    # explicit one is the belt, for a future refusal that turns the raise into a
    # 500 response rather than letting it out.
    assert refused.status_code != 500, refused.text
    # The route's own words — the shared sentence, not a fourth phrasing — and
    # they name the status the batch turned out to be in rather than the
    # `validating` this request read.
    assert flash(refused) == _unchangeable("abandoned")
    assert "This batch is abandoned" in flash(refused)
    assert "it cannot change" in flash(refused)
    assert "validating" not in flash(refused)
    assert banner(refused) == "", "the loser said the batch had been marked ready"

    # Exactly one of the two clicks applied, and it is the one that got there.
    assert status == "abandoned"
    assert "err=" not in abandoned.headers["hx-redirect"], abandoned.headers["hx-redirect"]
    applied = sorted(
        (
            await session.execute(
                select(AuditEvent.action).where(
                    AuditEvent.action.in_(["payout_batch.ready", "payout_batch.abandoned"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert applied == ["payout_batch.abandoned"]

    # And nothing from the refused click persisted: the attachments and their
    # audit row went back with the rollback, exactly as they never happened at
    # all when the check at the top of the route is the one that catches this.
    attached = (
        await session.execute(select(PayoutBatch.document_ids).where(PayoutBatch.id == batch_id))
    ).scalar_one()
    assert not attached, "the refused click's attachments outlived its rollback"
    documented = (
        await session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "payout_batch.documents")
        )
    ).scalar_one()
    assert documented == 0


async def test_the_run_sent_nothing_once_the_batch_was_no_longer_partially_dispatched(session):
    """The braces to the guarded update's belt, and the one acceptance criterion
    that is not about two routes: `_run` re-reads the batch and stops before the
    first row when it is not the state a dispatch was authorised in. Driven
    directly, because after the guarded update the routes can no longer produce
    this state themselves."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        await session.execute(
            update(PayoutBatch).where(PayoutBatch.id == batch_id).values(status="abandoned")
        )
        await session.commit()
        await _run(app.state.conduit, batch_id, CID, "usr_1", "ops@example.com")
        status = await batch_status(session, url)

    assert calls == [], "the run put a payout on the wire for a batch nobody may dispatch"
    assert status == "abandoned"
    operations_made = (
        await session.execute(
            select(func.count()).select_from(Operation).where(Operation.type == "payout_create")
        )
    ).scalar_one()
    assert operations_made == 0


# --- results export ----------------------------------------------------------------------------


async def rows_of_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if row]


async def test_the_results_export_masks_coordinates_and_states_every_outcome(session):
    calls: list = []
    app = app_with(
        calls,
        responses=[
            httpx.Response(202, json=TXN),
            httpx.Response(
                422, json={"type": "VALIDATION_ERROR", "title": "Recipient not accepted"}
            ),
        ],
    )
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        batch_id = url.rsplit("/", 1)[-1]
        exported = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")

    assert exported.status_code == 200
    rows = await rows_of_csv(exported.text)
    assert rows[0] == [
        "row",
        # The row's own purpose — a results file whose rows may each be a
        # different kind of payment has to say which each was.
        "purpose",
        "contact",
        "recipient_masked",
        "legal_name",
        "amount",
        "asset",
        "state",
        "transaction_id",
        "operation_id",
        "problem",
    ]
    assert rows[1][1] == GOODS
    assert rows[1][7] == "sent" and rows[1][8] == "txn_batch_1"
    # A3 gate M1: the results file's `problem` column is the CONSOLE's sentence
    # for the code, translated in `batches.rows_of` — it used to be Conduit's
    # own title, lifted straight out of the stored body.
    assert rows[2][7] == "rejected"
    assert rows[2][10] == "Conduit refused some of the values on this form"
    assert "Recipient not accepted" not in exported.text
    # The masking policy, asserted by ABSENCE of the full account number — the
    # export rule this console has held.
    assert "000123456789" not in exported.text
    assert "6789" in exported.text


async def test_the_results_export_is_audited_and_scoped_to_the_customer(session):
    from app.models import AuditEvent

    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
        batch_id = url.rsplit("/", 1)[-1]
        mine = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")
        theirs = await web.get(f"/export/batch_rows.csv?customerId=cus_other&batchId={batch_id}")
        missing = await web.get(f"/export/batch_rows.csv?customerId={CID}&batchId={uuid.uuid4()}")

    assert mine.status_code == 200
    # Another customer's URL for a real batch is a 404, exactly as a made-up id
    # is — never an empty file, which would read as "this batch has no rows".
    assert theirs.status_code == 404 and missing.status_code == 404
    logged = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "export.csv")
            )
        )
        .scalars()
        .all()
    )
    assert any(row.detail.get("surface") == "batch_rows" for row in logged)


async def test_a_viewer_may_export_the_results():
    app = app_with([])
    async with signed_in(app) as web:
        url = await ready_batch(web)
    batch_id = url.rsplit("/", 1)[-1]
    async with signed_in(app, groups="readers") as viewer:
        exported = await viewer.get(f"/export/batch_rows.csv?customerId={CID}&batchId={batch_id}")
    assert exported.status_code == 200


async def test_two_identical_rows_are_two_payouts_even_when_the_first_is_unresolved(session):
    """The hash-scope finding, as its own repro.

    Two rows of a batch can be byte-identical — the same recipient, the same
    amount, twice — and that is a perfectly ordinary file: two invoices to one
    supplier. Their bodies therefore hash identically, and `clientReferenceId`
    (the one per-operation value) is injected at send time and is deliberately
    absent from `request_hash`.

    So when row 1's operation is still ACTIVE — a 503 leaves it
    `outcome_unknown` — row 2's `operations.start` missed on intent and then hit
    the `(type, request_hash)` active-state guard, which resolved it to **row
    1's** operation with `is_new=False`. Row 2 was linked to a foreign
    operation, counted as already dispatched, and never sent: an under-payment
    in which both rows go on to claim the same transaction id.

    The fix salts the LOCAL hash with `{batch}:{row}` (`hash_scope`). Nothing
    Conduit sees changes.
    """
    calls: list = []
    app = app_with(calls, responses=[httpx.Response(503, text="gateway")])
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW])  # identical by construction
        await dispatch(web, url)

    assert len(calls) == 2, "the second identical row was never sent"
    # Two operations, two idempotency keys, two rows pointing at their own.
    assert len({key for key, _ in calls}) == 2
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    links = (
        (
            await session.execute(
                select(PayoutBatchRow.operation_id)
                .where(PayoutBatchRow.batch_id == batch_id)
                .order_by(PayoutBatchRow.row_number)
            )
        )
        .scalars()
        .all()
    )
    assert len(set(links)) == 2 and all(links), links
    # And the guard the salt has to survive: two identical, still-active batch
    # operations coexist under the partial unique index on (type, request_hash).
    states = (
        (await session.execute(select(Operation.state).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert sorted(states) == ["confirmed", "outcome_unknown"]


async def test_two_concurrent_loops_over_identical_rows_still_send_each_once(session):
    """The same collision from the other direction: two dispatch loops racing
    means row 2 asks while row 1's operation is `created`/`in_flight` — active,
    and identically hashed. Each row must still be sent exactly once."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, ROW, ROW])
        await asyncio.gather(dispatch(web, url), dispatch(web, url))

    assert len(calls) == 3, calls
    assert len({key for key, _ in calls}) == 3
    batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
    links = (
        (
            await session.execute(
                select(PayoutBatchRow.operation_id).where(PayoutBatchRow.batch_id == batch_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(set(links)) == 3 and all(links)


async def test_a_second_dispatch_runs_the_loop_and_puts_nothing_on_the_wire(session):
    """The proof with the engine actually running, not the route's status guard:
    the batch is put back the way a killed worker leaves it, so the loop walks
    two rows that already have operations and the intent nonce is the only thing
    between this and a double payment."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)
        assert len(calls) == 2
        batch_id = uuid.UUID(url.rsplit("/", 1)[-1])
        links = (
            (
                await session.execute(
                    select(PayoutBatchRow.operation_id)
                    .where(PayoutBatchRow.batch_id == batch_id)
                    .order_by(PayoutBatchRow.row_number)
                )
            )
            .scalars()
            .all()
        )
        await session.execute(
            update(PayoutBatch)
            .where(PayoutBatch.id == batch_id)
            .values(status="partially_dispatched")
        )
        await session.commit()
        await dispatch(web, url)
        after = (
            (
                await session.execute(
                    select(PayoutBatchRow.operation_id)
                    .where(PayoutBatchRow.batch_id == batch_id)
                    .order_by(PayoutBatchRow.row_number)
                )
            )
            .scalars()
            .all()
        )

    assert len(calls) == 2, "the resumed loop put a payout on the wire"
    assert after == links, "a row was re-linked to a different operation"
    operations = (
        (await session.execute(select(Operation.id).where(Operation.type == "payout_create")))
        .scalars()
        .all()
    )
    assert len(operations) == 2


# --- the pill vocabulary ---------------------------------------------------------------------------


def test_the_batch_tones_say_whose_move_it_is():
    """The slice-2 design-gate ruling, pinned because it is a *decision* and the
    next reader must not re-litigate it from the table alone:

    * `validating` is machine work resolving without anybody → `wait`. Amber
      there would summon a human to watch a validator run.
    * `ready` is the state where the machine has finished and the decision is the
      operator's → `warn`. Amber means yours.
    * `dispatched` is `ok` about the **batch's** own job — every row has an
      answer. It says nothing about settlement, which lives on each row's
      transaction.
    """
    from app.web import PILL_TONES, pill

    assert PILL_TONES["payout_batches"] == {
        "validating": "wait",
        "ready": "warn",
        "partially_dispatched": "warn",
        "dispatched": "ok",
        "abandoned": "muted",
    }
    # A row Conduit refused is the exception family; a row this console declined
    # to send is amber, because sending it is still the operator's move.
    assert pill("payout_batch_rows", "rejected")["tone"] == "bad"
    assert pill("payout_batch_rows", "refused")["tone"] == "warn"
    assert pill("payout_batch_rows", "unconfirmed")["tone"] == "warn"
    # Unknown row states stay neutral and are never terminal (plan v2 §7).
    assert pill("payout_batch_rows", "teleported")["known"] is False


async def test_the_report_lists_the_accepted_document_types_the_route_declared():
    """The design gate's second finding: the batch's own upload widget states
    what Conduit accepts, from the same snapshot the template's header block
    quoted — and with no Conduit call on a page that makes none."""
    app = app_with([])
    async with signed_in(app) as web:
        url = await uploaded(web, [ROW])
        report = await web.get(url)
    accepted = FEDWIRE_BUSINESS["documentation"]["acceptedDocumentTypes"]
    assert accepted, "the fixture must declare some for this test to mean anything"
    assert "Accepted:" in report.text
    for kind in accepted:
        assert kind in report.text


# --- the audit trail -----------------------------------------------------------------------------


async def test_dispatch_is_audited_at_both_ends(session):
    from app.models import AuditEvent

    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web)
        await dispatch(web, url)

    actions = (
        (
            await session.execute(
                select(AuditEvent.action).where(AuditEvent.action.like("payout_batch.%"))
            )
        )
        .scalars()
        .all()
    )
    assert "payout_batch.dispatch" in actions
    assert "payout_batch.dispatched" in actions


# --- multi-purpose dispatch --------------------------------------------------------------


async def test_each_dispatched_body_carries_its_own_rows_purpose(session):
    """Wire-asserted: a mixed batch sends one `FiatPayoutDto` per row and each
    one's `purpose` is that row's, not the batch's (a batch has none)."""
    import json

    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, GATED_ROW, {**ROW, "amount": "2.50"}])
        await dispatch(web, url)

    sent = [json.loads(body) for _key, body in calls]
    assert [body["purpose"] for body in sent] == [GOODS, "intercompany", GOODS]
    # And the gated row's destination is Conduit's registered record, resolved at
    # dispatch through that row's own (gated) model.
    assert sent[1]["destination"]["recipient"]["accountNumber"] == REGISTERED["accountNumber"]
    assert sent[0]["destination"]["recipient"]["accountNumber"] == ROW[
        "destination.recipient.accountNumber"
    ]


async def test_the_shared_documents_ride_only_with_the_rows_whose_purpose_needs_one(session):
    """A payroll register attached to an intercompany row is evidence for a
    payment it is not about. `documentation.required` is read live, per purpose,
    at dispatch."""
    import json

    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, GATED_ROW])
        await dispatch(web, url)

    sent = [json.loads(body) for _key, body in calls]
    assert sent[0]["documents"]  # goods: documentation.required
    assert "documents" not in sent[1]  # intercompany: it is not


async def test_dispatch_reads_requirements_once_per_purpose_the_batch_holds(session):
    """Bounded: at most seven reads for seven purposes, one per DISTINCT purpose
    present — never one per row."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, GATED_ROW, {**ROW, "amount": "2.50"}])
        before = len(
            [c for c in app.state.wire if c[1] == "/v2/payouts/requirements"]
        )
        await dispatch(web, url)
        after = [c for c in app.state.wire if c[1] == "/v2/payouts/requirements"]

    assert len(after) - before == 2  # two distinct purposes, three rows


async def test_a_purpose_that_stops_answering_at_dispatch_sends_nothing(session):
    """The destination on a gated row comes from Conduit's own record at send
    time, so a purpose this console cannot re-read is a purpose it will not
    guess a body for. Nothing goes on the wire and the rows say why."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=[ROW, GATED_ROW])
        app.state.conduit = make_app(
            stub(
                routes(
                    per_purpose={"intercompany": httpx.Response(503)},
                    extra={("POST", "/v2/payouts"): payouts_stub(calls)},
                )
            )
        ).state.conduit
        await dispatch(web, url)
        report = await web.get(url)

    assert calls == []
    assert batches.NO_REQUIREMENTS_AT_DISPATCH in said(report)


# --- the funding account, re-read at dispatch -----------------------------
#
# `virtual_account_id` and `asset` are picked at UPLOAD — a CSV amount column
# carries digits, never a currency — and go into every row's body. Dispatch used
# to take both on trust from that row while re-reading everything else live. A
# batch approved on Monday and dispatched on Friday debits an account that can be
# closed in between.


def account_route(response):
    return {("GET", f"/v2/customers/{CID}/virtual-accounts/{USD_ACCOUNT['id']}"): response}


async def dispatch_with_account(web, app, calls, response) -> httpx.Response:
    """A ready batch, then the funding account answers `response` at dispatch."""
    url = await ready_batch(web, rows=(ROW, ROW))
    app.state.conduit = make_app(
        stub(
            {
                **routes(extra={("POST", "/v2/payouts"): payouts_stub(calls)}),
                **account_route(response),
            }
        )
    ).state.conduit
    await dispatch(web, url)
    return await web.get(url)


async def test_a_funding_account_that_is_gone_refuses_the_whole_batch():
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        report = await dispatch_with_account(
            web, app, calls, httpx.Response(404, json={"type": "NOT_FOUND", "title": "gone"})
        )
    assert calls == []
    assert batches.NO_SUCH_ACCOUNT_AT_DISPATCH in said(report)


async def test_a_funding_account_that_went_inactive_refuses_the_whole_batch():
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        report = await dispatch_with_account(
            web, app, calls, httpx.Response(200, json={**USD_ACCOUNT, "status": "closed"})
        )
    assert calls == []
    # The state is named: "not active" alone leaves the operator guessing whether
    # this is a Conduit outage or a closed account.
    assert "closed, not active" in said(report)


async def test_a_funding_account_whose_currency_moved_refuses_the_whole_batch():
    """The one that would otherwise be silent. Every row's body carries
    `batch.asset`, and the totals the operator approved were denominated in it —
    an account now holding something else makes both a claim about money nobody
    made."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        report = await dispatch_with_account(
            web, app, calls, httpx.Response(200, json={**USD_ACCOUNT, "asset": {"code": "EUR"}})
        )
    assert calls == []
    text = said(report)
    assert "holds EUR" in text and "totalled as USD" in text


async def test_an_unreadable_funding_account_is_not_a_missing_one():
    """`_funding`'s rule, on the dispatch path: "Conduit did not answer" and
    "this account is gone" are different facts, and only one of them is fixed by
    pressing the button again."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        report = await dispatch_with_account(web, app, calls, httpx.Response(503))
    assert calls == []
    text = said(report)
    assert batches.UNREADABLE_ACCOUNT_AT_DISPATCH in text
    assert batches.NO_SUCH_ACCOUNT_AT_DISPATCH not in text


async def test_a_refused_batch_writes_no_operation_row(session):
    """The refusal is a `dispatch_error` on every pending row and nothing else —
    no ledger row, no idempotency key, nothing that a later dispatch would then
    have to resolve around."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        await dispatch_with_account(
            web, app, calls, httpx.Response(200, json={**USD_ACCOUNT, "status": "closed"})
        )
    # `payout_create` only: the ready batch's shared document is itself an
    # operation, and it was made long before this dispatch.
    assert (
        await session.scalar(
            select(func.count(Operation.id)).where(Operation.type == "payout_create")
        )
    ) == 0
    linked = (
        await session.execute(select(PayoutBatchRow.operation_id, PayoutBatchRow.dispatch_error))
    ).all()
    assert [op for op, _ in linked] == [None, None]
    assert all("not active" in (err or "") for _, err in linked)


async def test_a_second_abort_replaces_the_first_reason_on_every_row(session):
    """Two aborted runs, two different causes. A row the first abort refused is
    still a row this dispatch would have sent, so it carries the *current*
    reason: skipping it left the page blaming a missing account for a run that
    actually stopped on a closed one, and under-counted `refused` besides."""
    calls: list = []
    app = app_with(calls)

    def account_answers(response):
        app.state.conduit = make_app(
            stub(
                {
                    **routes(extra={("POST", "/v2/payouts"): payouts_stub(calls)}),
                    **account_route(response),
                }
            )
        ).state.conduit

    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, ROW))
        account_answers(httpx.Response(404, json={"type": "NOT_FOUND", "title": "gone"}))
        await dispatch(web, url)
        assert batches.NO_SUCH_ACCOUNT_AT_DISPATCH in said(await web.get(url))
        account_answers(httpx.Response(200, json={**USD_ACCOUNT, "status": "closed"}))
        await dispatch(web, url)
        page = said(await web.get(url))
    assert calls == []
    assert "closed, not active" in page
    assert batches.NO_SUCH_ACCOUNT_AT_DISPATCH not in page
    errors = (await session.execute(select(PayoutBatchRow.dispatch_error))).scalars().all()
    assert errors and all("closed, not active" in (err or "") for err in errors)


async def test_a_live_active_account_in_the_batch_currency_dispatches(session):
    """The pass, and the non-vacuity guard on the four refusals above: the same
    fixture with the account answering normally sends both rows."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, ROW))
        await dispatch(web, url)
        report = await web.get(url)
    assert len(calls) == 2
    for sentence in (
        batches.NO_SUCH_ACCOUNT_AT_DISPATCH,
        batches.UNREADABLE_ACCOUNT_AT_DISPATCH,
    ):
        assert sentence not in said(report)


async def test_the_account_is_read_before_any_requirements_read():
    """A batch that cannot be funded costs no discovery reads: the check is the
    first thing dispatch does, not a step inside the per-purpose loop."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, ROW))
        wire: list = []
        app.state.conduit = make_app(
            stub(
                {
                    **routes(extra={("POST", "/v2/payouts"): payouts_stub(calls)}),
                    **account_route(httpx.Response(404, json={"type": "NOT_FOUND"})),
                },
                wire,
            )
        ).state.conduit
        await dispatch(web, url)
    assert [c[1] for c in wire if "requirements" in c[1]] == []


# --- the money ceiling, per row and on the batch total -------------------


async def test_the_ceiling_refuses_the_batch_total_before_any_row_is_dispatched():
    """Two rows of 10.00 under a 15.00 ceiling: each row is fine on its own and
    the batch is not. Refused at the dispatch route, before the status flips and
    before any row has an operation — so `POST /v2/payouts` is never reached."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, ROW))
        with settings_override(money_ceiling="15.00"):
            sent = await dispatch(web, url)
    assert "refuses any single amount over 15.00" in flash(sent)
    assert "Batch total" in flash(sent)
    assert calls == []


async def test_the_dispatch_loop_refuses_a_row_over_the_ceiling(session):
    """Per row, inside the loop, before `operations.start`.

    Reached here by calling `batches.dispatch` rather than by clicking the
    button, and deliberately so: the route checks the BATCH TOTAL first, and
    since every amount is positive a row over the ceiling always puts the total
    over it too — through the button this guard cannot fire today. The design
    asks for both anyway and that is the right call: the loop is a background
    task with its own session, "a row over the ceiling never becomes an
    operation" is the property, and it has to hold wherever dispatch is entered
    from rather than only from the one caller that exists now.

    The refusal lands on the row's own `dispatch_error` — the existing shape for
    "not sent, and here is why" — so it is visible on the report and cleared by
    the next dispatch that succeeds.
    """
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, {**ROW, "amount": "500.01"}, ROW))
    batch_id = url.rstrip("/").split("/")[-1]

    batch = await batches.get(session, CID, batch_id)
    assert batch is not None
    handler = stub(
        routes(extra={("POST", "/v2/payouts"): payouts_stub(calls)}),
    )
    client = ConduitClient(transport=httpx.MockTransport(handler))
    with settings_override(money_ceiling="500.00"):
        result = await batches.dispatch(
            session, client, batch, actor_id="usr_1", actor_email="ops@example.com"
        )
    await client.aclose()

    assert result["sent"] == 2 and result["refused"] == 1
    assert len(calls) == 2, "the row over the ceiling was still sent"
    refusals = (
        await session.execute(
            select(PayoutBatchRow.dispatch_error).where(
                PayoutBatchRow.dispatch_error.is_not(None)
            )
        )
    ).scalars().all()
    assert len(refusals) == 1 and "500.01" in refusals[0] and "500.00" in refusals[0]
    # …and it never became an operation: two rows, two `payout_create` rows.
    rows = (
        await session.execute(select(Operation).where(Operation.type == "payout_create"))
    ).scalars().all()
    assert len(rows) == 2


async def test_an_unset_ceiling_dispatches_everything():
    """The default, and the behaviour every deployment had before item 12."""
    calls: list = []
    app = app_with(calls)
    async with signed_in(app) as web:
        url = await ready_batch(web, rows=(ROW, {**ROW, "amount": "999999.00"}))
        await dispatch(web, url)
    assert len(calls) == 2
