"""Typed client: problem-detail parsing, read policy, classification, paging
(plan v2 §4, OPERATIONS_SPEC §2). Conduit is stubbed at the HTTP layer."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.conduit import (
    ConduitClient,
    Outcome,
    Page,
    Problem,
    RateLimited,
    Success,
    TransportFailure,
    ValidationError,
    classify,
)

FIXTURES = Path(__file__).parent / "fixtures"


def client(handler, sleeps: list | None = None) -> ConduitClient:
    async def sleep(delay):
        (sleeps if sleeps is not None else []).append(delay)

    return ConduitClient(transport=httpx.MockTransport(handler), sleep=sleep)


def responder(*responses):
    """Serve the given responses in order; exceptions are raised instead."""
    queue = list(responses)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# --- problem-detail parsing -------------------------------------------------------


async def test_parses_the_live_problem_detail_fixture():
    body = json.loads((FIXTURES / "problem_detail_422_no_eligible_provider.json").read_text())
    result = await client(responder(httpx.Response(422, json=body))).get("/v2/anything")

    assert isinstance(result, Problem)
    assert (result.status, result.type) == (422, "NO_ELIGIBLE_PROVIDER")
    # A3: the code survives verbatim and the PROSE does not — `title` and
    # `resolution` are this console's (`conduit.problems.TITLES`), and `detail`
    # is dropped rather than carried. The fixture's own wording is asserted
    # absent below, from `raw`, which still holds every byte of it.
    assert result.title == "No banking provider can hold this currency for this customer"
    assert result.detail == ""
    assert result.resolution.startswith("Nothing was requested.")
    assert result.correlation_id == "fe69d526-4a07-48a5-adbd-cebffd6b8ccb"
    assert result.docs and result.instance and result.timestamp
    assert result.raw == body  # stored verbatim on operations.error
    assert body["title"] not in (result.title, result.detail, result.resolution)
    assert body["detail"] not in (result.title, result.detail, result.resolution)


async def test_parses_validation_errors():
    body = {
        "type": "VALIDATION_ERROR",
        "title": "Validation failed",
        "status": 422,
        "detail": "2 fields are invalid",
        "resolution": "Fix the fields and resubmit.",
        "correlationId": "cor_9",
        "errors": [
            {"pointer": "/rail", "detail": "Unsupported", "allowedValues": ["ach", "sepa"]},
            {"pointer": "/documents", "detail": "Missing", "category": "document"},
        ],
    }
    result = await client(responder(httpx.Response(422, json=body))).get("/v2/anything")

    assert isinstance(result, ValidationError)
    assert [e.pointer for e in result.errors] == ["/rail", "/documents"]
    assert result.errors[0].allowed_values == ["ach", "sepa"]
    assert (result.errors[1].category, result.errors[1].allowed_values) == ("document", [])
    assert result.correlation_id == "cor_9"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>gateway</html>"),
        httpx.Response(503, content=b""),
        httpx.Response(400, json=["not", "an", "object"]),
        httpx.Response(404, json={"status": 404}),
    ],
)
async def test_parsing_is_tolerant_of_broken_error_bodies(response):
    result = await client(responder(response), sleeps=[]).get("/v2/anything")
    assert isinstance(result, Problem)
    assert result.status == response.status_code and result.title


async def test_rate_limited_falls_back_to_the_body_retry_after():
    sleeps: list[float] = []
    result = await client(
        responder(httpx.Response(429, json={"type": "RATE_LIMITED", "retryAfterSeconds": 3})),
        sleeps,
    ).get("/v2/anything")
    assert isinstance(result, RateLimited) and result.retry_after == 3.0
    assert sleeps == [3.0, 3.0, 3.0]  # every attempt waited what Conduit asked for


# --- read policy --------------------------------------------------------------------


async def test_get_honors_retry_after_then_succeeds():
    sleeps: list[float] = []
    handler = responder(
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        httpx.Response(200, json={"ok": True}),
    )
    result = await client(handler, sleeps).get("/v2/transactions", limit=5, cursor=None)

    assert isinstance(result, Success) and result.data == {"ok": True}
    assert sleeps == [2.0]  # the header, not our backoff
    assert handler.calls[0].url.params.get("limit") == "5"
    assert "cursor" not in handler.calls[0].url.params  # None params are dropped


async def test_get_retries_transient_transport_errors_and_gives_up_bounded():
    sleeps: list[float] = []
    handler = responder(httpx.ConnectError("no route"))
    result = await client(handler, sleeps).get("/v2/transactions")

    assert isinstance(result, TransportFailure) and result.error == "ConnectError"
    assert len(handler.calls) == 4  # read_max_attempts
    assert len(sleeps) == 3 and all(0 <= s <= 8 for s in sleeps)


async def test_get_retries_5xx_then_returns_the_problem():
    handler = responder(httpx.Response(502, json={"type": "BAD_GATEWAY"}))
    result = await client(handler, sleeps=[]).get("/v2/transactions")
    assert isinstance(result, Problem) and len(handler.calls) == 4


async def test_mutations_are_never_retried():
    for outcome in (httpx.ReadError("dropped"), httpx.Response(500, json={}), httpx.Response(429, json={})):
        handler = responder(outcome)
        await client(handler, sleeps=[]).mutate("POST", "/v2/payouts", json={}, idempotency_key="k")
        assert len(handler.calls) == 1


async def test_mutate_sends_the_api_key_and_idempotency_key():
    handler = responder(httpx.Response(202, json={"id": "txn_1"}))
    await client(handler).mutate("POST", "/v2/payouts", json={"a": 1}, idempotency_key="key-1")

    request = handler.calls[0]
    assert request.headers["Idempotency-Key"] == "key-1"
    assert request.headers["x-api-key"] == "test-key-not-real"
    assert request.url.path == "/v2/payouts"
    assert json.loads(request.content) == {"a": 1}


async def test_get_does_not_send_an_idempotency_key():
    handler = responder(httpx.Response(200, json={}))
    await client(handler).get("/v2/transactions")
    assert "idempotency-key" not in handler.calls[0].headers


# --- classification (OPERATIONS_SPEC §2) -------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={}), Outcome.CONFIRMED),
        (httpx.Response(202, json={}), Outcome.CONFIRMED),
        (httpx.Response(204), Outcome.CONFIRMED),
        (httpx.Response(400, json={"type": "BAD_REQUEST"}), Outcome.REJECTED),
        (httpx.Response(403, json={"type": "FORBIDDEN"}), Outcome.REJECTED),
        (httpx.Response(404, json={"type": "CUSTOMER_NOT_FOUND"}), Outcome.REJECTED),
        (httpx.Response(422, json={"type": "NO_ELIGIBLE_PROVIDER"}), Outcome.REJECTED),
        (httpx.Response(409, json={"type": "IDEMPOTENCY_KEY_CONFLICT"}), Outcome.AMBIGUOUS),
        (httpx.Response(409, json={"type": "ONBOARDING_ALREADY_SUBMITTED"}), Outcome.AMBIGUOUS),
        (httpx.Response(429, json={"type": "RATE_LIMITED"}), Outcome.AMBIGUOUS),
        (httpx.Response(500, json={}), Outcome.AMBIGUOUS),
        (httpx.Response(503, json={}), Outcome.AMBIGUOUS),
        (httpx.ReadTimeout("timeout"), Outcome.AMBIGUOUS),
        (httpx.ConnectError("refused"), Outcome.AMBIGUOUS),
    ],
)
async def test_mutation_classification_table(response, expected):
    result = await client(responder(response)).mutate("POST", "/v2/payouts", idempotency_key="k")
    assert classify(result) is expected


# --- pagination ----------------------------------------------------------------------


async def test_page_passes_one_cursor_page_through():
    handler = responder(
        httpx.Response(
            200,
            json={
                "data": [{"id": "txn_1"}, {"id": "txn_2"}],
                "meta": {
                    "mode": "cursor",
                    "nextCursor": "next-1",
                    "previousCursor": None,
                    "total": 42,
                },
            },
        )
    )
    page = await client(handler).page(
        "/v2/transactions", cursor="c0", limit=2, direction="forward", customerId="cus_1"
    )

    assert isinstance(page, Page)
    assert [i["id"] for i in page.items] == ["txn_1", "txn_2"]
    assert (page.next_cursor, page.prev_cursor, page.total) == ("next-1", None, 42)
    assert len(handler.calls) == 1  # never walks the next cursor
    params = handler.calls[0].url.params
    assert (params["cursor"], params["limit"], params["direction"]) == ("c0", "2", "forward")
    assert params["customerId"] == "cus_1"


async def test_the_app_lifespan_opens_and_closes_one_client():
    from app.main import create_app, lifespan

    app = create_app()
    async with lifespan(app):
        conduit = app.state.conduit
        assert isinstance(conduit, ConduitClient)
        assert str(conduit._http.base_url) == "https://api.sandbox.conduit.financial"
        assert not conduit._http.is_closed
    assert conduit._http.is_closed


async def test_page_returns_the_error_result_rather_than_an_empty_page():
    result = await client(responder(httpx.Response(404, json={"type": "NOT_FOUND"}))).page(
        "/v2/transactions"
    )
    assert isinstance(result, Problem)  # a failed read must never look like "no rows"


async def test_a_crafted_resource_id_cannot_steer_the_client_off_its_path():
    """`customer_id` became a
    query parameter, so it can contain what a path parameter structurally
    cannot: `?customer_id=../../v2/organization/api-keys?x=` walked this
    client — which carries the org API key — to an arbitrary GET path, and the
    response was partially rendered. One guard at `_send`, because eight call
    sites interpolate ids into f-string paths today and the ninth would forget:
    a separator in the path refuses LOCALLY, before any request exists, as the
    TransportFailure every caller already renders as "could not be read".
    """
    sent: list = []

    async def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    conduit = client(handler)
    for crafted in (
        "../../v2/organization/api-keys?x=",
        "cus_1?limit=1000",
        "cus_1#frag",
        "cus_1/../../v2/quotes",
        "cus%2e%2e",
        "cus\\x",
    ):
        result = await conduit.get(f"/v2/customers/{crafted}/virtual-accounts")
        assert isinstance(result, TransportFailure), crafted
        assert result.error == "InvalidResourcePath", crafted
    assert sent == [], "a crafted id reached the wire"

    # And a real id still flows: the guard refuses separators, not customers.
    ok = await conduit.get("/v2/customers/cus_034GTiOfAFAhfDVahyir2N/virtual-accounts")
    assert isinstance(ok, Success) and len(sent) == 1
