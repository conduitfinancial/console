"""Typed Conduit HTTP client (plan v2 §4, OPERATIONS_SPEC §2).

One client per process (FastAPI lifespan / worker startup), never per request.
Everything an endpoint can answer with becomes one of five typed results, so no
caller ever touches `httpx.Response` or has to remember what a 409 means:

    Success | ValidationError | RateLimited | Problem | TransportFailure

`ValidationError` and `RateLimited` are `Problem`s, so `isinstance(r, Problem)`
still means "Conduit answered with a problem-detail" — and `resolution` +
`correlation_id` survive on every one of them (they are what the operator gives
support).

Reads retry (429 with Retry-After, 5xx, transport blips). **Mutations never
retry** — `classify()` turns the response into the operations layer's verdict.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.conduit.problems import code_of, console_words

log = logging.getLogger(__name__)


# --- typed results --------------------------------------------------------------


@dataclass(frozen=True)
class Success:
    status: int
    data: Any  # parsed JSON body; None for 204 and non-JSON bodies


@dataclass(frozen=True)
class Problem:
    """RFC7807 problem-detail (ProblemDetailDto). Parsed tolerantly: a truncated
    or non-JSON error body still produces a usable Problem.

    `type` is Conduit's own code, verbatim — it is what the reconciler's
    `DEFINITIVE_CONFLICTS` matches on and what support quotes. `title`,
    `detail` and `resolution` are **this console's words** (`app.conduit.problems`
    translates them in `parse_problem`, the one boundary); Conduit's own prose
    survives verbatim in `raw` alone, which is evidence for the operations
    ledger and is never rendered.
    """

    status: int
    type: str
    title: str
    detail: str
    resolution: str
    docs: str | None
    instance: str | None
    correlation_id: str | None
    timestamp: str | None
    raw: dict  # stored verbatim on operations.error


@dataclass(frozen=True)
class FieldError:
    pointer: str
    detail: str
    category: str | None
    allowed_values: list[str]


@dataclass(frozen=True)
class ValidationError(Problem):
    """ValidationErrorDto: problem-detail + `errors[]` the form engine maps."""

    errors: list[FieldError]


@dataclass(frozen=True)
class RateLimited(Problem):
    """RateLimitedErrorDto. `retry_after` comes from the header, then the body."""

    retry_after: float | None


@dataclass(frozen=True)
class TransportFailure:
    """No usable HTTP response: timeout, connection reset, DNS. Carries the
    exception class only — messages can echo URLs, and URLs can carry ids."""

    error: str


Result = Success | Problem | ValidationError | RateLimited | TransportFailure


@dataclass(frozen=True)
class Page:
    """One cursor page, passed straight through to the UI (plan v2 §4: never
    eagerly walk cursors)."""

    items: list[dict]
    next_cursor: str | None
    prev_cursor: str | None
    total: int | None


class Outcome(StrEnum):
    """What a mutation's response means for the operation row (OPERATIONS_SPEC §2)."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


def classify(result: Result) -> Outcome:
    """2xx → confirmed · definitive 4xx → rejected · conflict/429/5xx/transport →
    ambiguous (the reconciler finds out what really happened).

    *Every* 409 is ambiguous, not just the idempotency/already-submitted
    codes. A 409 that was really definitive (`PAYOUT_NOT_CANCELLABLE`) still ends
    up rejected — one reconciler read later, via its §3 recipe. Cheaper than a
    hand-maintained table of ~20 conflict codes, and it can never mislabel a
    conflict as a rejection. 429 is ambiguous for the same reason: a rate limiter
    in front of the API is provably pre-processing, one inside it is not.
    """
    if isinstance(result, Success):
        return Outcome.CONFIRMED
    if isinstance(result, TransportFailure):
        return Outcome.AMBIGUOUS
    if result.status in (409, 429) or result.status >= 500:
        return Outcome.AMBIGUOUS
    if 400 <= result.status < 500:
        # §2 says "4xx **with a parsed problem-detail**". A 4xx whose body is
        # HTML, empty, or an object with none of Conduit's fields did not come
        # from Conduit's own validation — it came from something in front of it,
        # which knows nothing about whether the mutation was applied. Calling
        # that a rejection releases the double-submit guard on an outcome that
        # is genuinely unknown.
        return (
            Outcome.REJECTED
            if result.raw.get("type") or result.raw.get("title")
            else Outcome.AMBIGUOUS
        )
    return Outcome.AMBIGUOUS


# --- parsing ---------------------------------------------------------------------


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after(response: httpx.Response, body: dict) -> float | None:
    header = response.headers.get("retry-after")
    for candidate in (header, body.get("retryAfterSeconds")):
        try:
            return float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue  # HTTP-date form of Retry-After: fall back to our backoff
    return None


def parse_problem(response: httpx.Response) -> Problem:
    """**The one translation boundary** (spec §5).

    Every Conduit error body in this process becomes a `Problem` here and
    nowhere else, so this is where the vendor's developer-facing prose stops:
    `title` and `resolution` are replaced by `problems.console_words`, and
    `detail` is dropped to `""` rather than carried forward — seven call sites
    render `result.title` (and two of them `result.detail`) straight into a
    flash banner or a batch note, and translating here fixes all of them at
    once instead of asking each to remember.

    The whole body is still kept, unaltered, on `raw`: `classify` reads it, the
    operations ledger stores it as the evidence of what Conduit actually said,
    and the reconciler matches `type` against it. Nothing about that changes —
    what changes is only what an operator READS.
    """
    body = _json(response)
    if not isinstance(body, dict):
        body = {}
    # `code_of` and not `str(...)`: `type` is a free string on the wire (RFC 7807
    # calls it a URI), and whatever it holds is printed — in a title, and from
    # there into a `?err=` query string. Anything that is not SCREAMING_SNAKE is
    # not a code and becomes UNKNOWN.
    code = code_of(body.get("type"))
    title, resolution = console_words(
        code, response.status_code, str(body.get("resolution") or "")
    )
    fields = dict(
        status=response.status_code,
        type=code,
        title=title,
        # Never Conduit's `detail`. It is the developer prose the Arca audit
        # found on screen (spec §0.2) and it is one `str()` away from a leak;
        # the field stays on the dataclass because `local_problem` and the
        # templates share the shape, and this console fills it when it has
        # something of its own to say.
        detail="",
        resolution=resolution,
        docs=body.get("docs"),
        instance=body.get("instance"),
        correlation_id=body.get("correlationId"),
        timestamp=body.get("timestamp"),
        raw=body,
    )
    if response.status_code == 429:
        return RateLimited(**fields, retry_after=_retry_after(response, body))
    errors = body.get("errors")
    if isinstance(errors, list):
        return ValidationError(
            **fields,
            errors=[
                FieldError(
                    pointer=str(e.get("pointer") or ""),
                    detail=str(e.get("detail") or ""),
                    category=e.get("category"),
                    allowed_values=list(e.get("allowedValues") or []),
                )
                for e in errors
                if isinstance(e, dict)
            ],
        )
    return Problem(**fields)


# --- the client -------------------------------------------------------------------


class ConduitClient:
    """`transport` and `sleep` exist for tests (httpx.MockTransport, no real waits)."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._sleep = sleep
        self.requests = 0  # HTTP requests sent, retries included (budget accounting)
        self._http = httpx.AsyncClient(
            base_url=self.settings.conduit_base_url,
            timeout=self.settings.op_client_timeout,
            transport=transport,
            # The key lives here and nowhere else: never logged, never rendered.
            headers={
                "x-api-key": self.settings.conduit_api_key.get_secret_value(),
                "accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, *, max_attempts: int | None = None, **params: Any) -> Result:
        """Read policy: bounded retries on 429 (Retry-After), 5xx and transport
        errors, full-jitter exponential backoff. `None` params are dropped.

        `max_attempts` lowers the retry ceiling for this call — the reconciler
        passes its remaining request budget so one read cannot spend four wire
        requests it was not allocated.
        """
        query = {k: v for k, v in params.items() if v is not None}
        attempts = max(1, min(self.settings.read_max_attempts, max_attempts or 1 << 30))
        for attempt in range(1, attempts + 1):
            result = await self._send("GET", path, params=query)
            if isinstance(result, RateLimited):
                delay = (
                    result.retry_after
                    if result.retry_after is not None
                    else self._backoff(attempt)
                )
            elif isinstance(result, TransportFailure) or (
                isinstance(result, Problem) and result.status >= 500
            ):
                delay = self._backoff(attempt)
            else:
                return result
            if attempt == attempts:
                return result
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def page(
        self,
        path: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        direction: str | None = None,
        max_attempts: int | None = None,
        **filters: Any,
    ) -> Page | Result:
        """Single-page passthrough. Returns the error `Result` unchanged when the
        read failed — callers must not read a failure as an empty page.

        A 2xx whose body is not a list envelope is returned unchanged for the
        same reason: an HTML gateway page parses to no items, and "no items" is
        what the reconciler reads as proof a payout does not exist before it
        replays. Absence has to be stated by Conduit, not inferred from a body
        we could not understand.
        """
        result = await self.get(
            path,
            cursor=cursor,
            limit=limit,
            direction=direction,
            max_attempts=max_attempts,
            **filters,
        )
        if not isinstance(result, Success):
            return result
        body = result.data if isinstance(result.data, dict) else {}
        items = body.get("data")
        if not isinstance(items, list):
            log.warning("conduit GET %s: 2xx body is not a list envelope", path)
            return result
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        return Page(
            items=[i for i in items if isinstance(i, dict)],
            next_cursor=meta.get("nextCursor"),
            prev_cursor=meta.get("previousCursor"),
            total=meta.get("total"),
        )

    async def mutate(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        idempotency_key: Any = None,
    ) -> Result:
        """NEVER retried (OPERATIONS_SPEC §2): a retry here is how you create two
        payouts. The caller records the classified outcome and lets the
        reconciler resolve anything ambiguous.

        `files`/`data` send `multipart/form-data` instead of JSON — document
        uploads. httpx sets the boundary and the part headers; the
        `Idempotency-Key` rides on the request either way.
        """
        headers = {"Idempotency-Key": str(idempotency_key)} if idempotency_key else None
        return await self._send(method, path, json=json, files=files, data=data, headers=headers)

    def _backoff(self, attempt: int) -> float:
        ceiling = min(
            self.settings.read_backoff_base_seconds * 2 ** (attempt - 1),
            self.settings.read_backoff_max_seconds,
        )
        return random.uniform(0, ceiling)  # full jitter

    async def _send(self, method: str, path: str, **kwargs: Any) -> Result:
        # Every path this console builds is a literal `/v2/…` template with
        # resource ids interpolated in — and a real Conduit id never contains a
        # separator. A crafted id CAN: `customer_id` moved from a path
        # parameter (structurally `/`-free) to a query parameter, and the
        # review steered this client — which carries the org API key —
        # to arbitrary GET paths with `?customer_id=../../v2/…?x=`. The guard
        # lives HERE, not per call site, because eight f-strings interpolate ids
        # today and the ninth would forget: any traversal or query-splitting
        # character in the path refuses locally as a TransportFailure, which
        # every caller already renders as "could not be read" — the honest
        # answer for an id that cannot name a resource. Query values are not
        # affected; they ride `params=`, never the path.
        if any(ch in path for ch in "?#\\") or "//" in path or ".." in path or "%" in path:
            log.warning("conduit %s refused: path %r is not a resource path", method, path)
            return TransportFailure("InvalidResourcePath")
        self.requests += 1
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("conduit %s %s: %s", method, path, type(exc).__name__)
            return TransportFailure(type(exc).__name__)
        if response.is_success:
            return Success(response.status_code, _json(response))
        problem = parse_problem(response)
        log.warning(
            "conduit %s %s -> %s %s (correlationId=%s)",
            method,
            path,
            problem.status,
            problem.type,
            problem.correlation_id,
        )
        return problem
