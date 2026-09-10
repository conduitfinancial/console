"""FastAPI app factory: health endpoints + the auth layer (plan v2 §2).

`/health/*` stays unauthenticated on purpose — deployment probes have no
credentials — and so does `/webhooks/*`, which authenticates by signature.
Everything else is behind `install_auth`.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import webhooks
from app.auth import install_auth
from app.conduit import ConduitClient
from app.config import get_settings
from app.db import engine
from app.operations import IntentTypeMismatch, OperationAdvanced
from app.web import (
    UnmintedIntent,
    install_web,
    local_problem,
    redirect,
    render_error,
    templates,
)

log = logging.getLogger(__name__)

# The receiver's mount point. Its caller is Conduit's sender, which reads a
# status and a JSON body — so `http_error` leaves those responses alone rather
# than answering a machine with a page (A3 gate, n7).
WEBHOOK_PREFIX = "/webhooks/"

# What an operator is told when their send lost the race to begin
# (`operation_already_moved`). Every word of it has to be true of every way that
# race can be lost — a webhook confirming the row, a second dispatch sending it,
# an admin or the TTL job abandoning it — so it claims only the two things that
# hold in all of them: this request sent nothing further, and the operation's own
# page is the record of what did happen. "Already sent" would have been a lie
# about money on the abandoned road, where nothing was ever on the wire.
ALREADY_MOVED = (
    "This submission had already moved on, so nothing further was sent — this page is its record."
)

# One dict so the pinning test asserts the same object the middleware sets.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def _route_sentence(exc: StarletteHTTPException) -> str:
    """The route's own explanation of a refusal, or "" — see `http_error`."""
    detail = str(exc.detail or "")
    try:
        stock = HTTPStatus(exc.status_code).phrase
    except ValueError:  # a status HTTPStatus has never heard of
        stock = ""
    # Case-insensitively: FastAPI's own is "Not Found", but a route writing
    # `detail="not found"` was being echoed onto the page as if it were help
    # (A3 gate, n6). Either way it is the status said twice.
    return "" if detail.casefold() == stock.casefold() else detail


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One pooled Conduit client per process (plan v2 §4). The worker builds its
    own the same way: `ConduitClient()` … `await client.aclose()`."""
    app.state.conduit = ConduitClient()
    try:
        yield
    finally:
        await app.state.conduit.aclose()


def create_app() -> FastAPI:
    settings = get_settings()  # startup guards run here — refuse to boot if unsafe
    # No `/docs`, `/redoc` or `/openapi.json`: this app is an operator console,
    # not an API, and its own schema is route inventory nobody outside needs
    #. Nothing in-app or in the tests reads them — the drift
    # test reads `contracts/`, which is *Conduit's* spec.
    app = FastAPI(
        title="Conduit Console",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live")
    async def live() -> dict:
        # No `environment` key: it named the environment to an unauthenticated
        # caller for no operational reason. Nothing in this
        # repo or deploy/ reads that key — see tests/test_health.py.
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        try:
            async with engine().connect() as conn:
                await conn.execute(text("select 1"))
        except Exception:  # noqa: BLE001 — surface as unready, not a 500
            # No exception text in the body: DSNs carry credentials.
            log.exception("readiness check failed")
            return JSONResponse({"status": "unready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    # No secret configured = no endpoint registered with Conduit yet, so the
    # receiver is not mounted at all: an unverifiable POST gets a 404, not a
    # route that accepts everything (OPERATIONS_SPEC §4).
    if settings.conduit_webhook_secret.get_secret_value():
        app.include_router(webhooks.router)

    install_web(app)
    install_auth(app, settings=settings)

    @app.exception_handler(IntentTypeMismatch)
    async def wrong_kind_of_intent(request: Request, exc: IntentTypeMismatch) -> HTMLResponse:
        """A submission carrying a nonce that belongs to another kind of operation.

        `operations.start` refuses it for all of its callers (a nonce belongs to
        the render that minted it), so the refusal is answered here rather than
        in each route: without this the twelve call sites that have no handler of
        their own turned an honest refusal into a 500. The payouts form keeps its
        own re-render, which puts the operator's values back on the screen; this
        is the floor under everybody else.

        422 and the `problem` macro because that is what every other refusal in
        this console is: htmx swaps a 422 (`base.html`'s `responseHandling`), so
        the sentence lands where the form's own errors land.
        """
        log.warning("refused a %s intent submitted to a %s form", exc.found, exc.expected)
        macros = templates.env.get_template("macros.html").module
        return HTMLResponse(
            str(
                macros.problem(
                    local_problem(
                        "This form's submission token belongs to something else",
                        f"The token sent with this form was minted for a {exc.found} "
                        "operation, so nothing was sent.",
                        resolution="Reload the page and submit it again.",
                    )
                )
            ),
            status_code=422,
        )

    @app.exception_handler(UnmintedIntent)
    async def intent_we_never_minted(request: Request, exc: UnmintedIntent) -> HTMLResponse:
        """A submission carrying a token this console did not issue.

        The sibling of `wrong_kind_of_intent` above, and answered here for the
        same reason: `intent_of` refuses for all twelve of its callers, and a
        refusal with no handler is a 500 — which reads to the operator as "the
        console broke" rather than "that submission was not accepted".

        Deliberately says nothing about *why* the token failed. Tampered, forged
        and expired are one answer on purpose: an error page that distinguished
        them would be a signing oracle, and none of the three is anything the
        operator can act on differently. Reloading the page mints a fresh one and
        is the whole of the remedy.
        """
        log.warning("refused a submission token this console did not mint")
        macros = templates.env.get_template("macros.html").module
        return HTMLResponse(
            str(
                macros.problem(
                    local_problem(
                        "This form's submission token was not issued here",
                        "The token sent with this form is not one this console minted — it "
                        "may have been altered, or the page may have been open too long — "
                        "so nothing was sent.",
                        resolution="Reload the page and submit it again.",
                    )
                )
            ),
            status_code=422,
        )

    @app.exception_handler(OperationAdvanced)
    async def operation_already_moved(request: Request, exc: OperationAdvanced) -> Response:
        """A send that lost the race to begin.

        The third refusal answered here for the reason the two above are: the
        row moved between a route's state check and `operations.in_flight`'s
        locked `-> in_flight`, every one of the dozen routes that send through
        `execute_operation` can lose that race, and none of them had a handler —
        so an honest convergence came back a 500. That is the worst possible
        answer to give this particular loser: it is a 500 on a submit, on a
        money page, and it is what provokes the resubmit that pays twice
        (`operations._Result._record` refuses to produce one for exactly this
        reason, and this is the same ruling applied one step earlier).

        A redirect and not a `problem` macro, because unlike the two above there
        is nothing wrong to report — the operation exists, somebody is dealing
        with it, and the page that says so is its own. The operator gets what
        they would have got had they clicked a moment later.

        Deliberately says nothing about *what* moved the row or *who* won it.
        Partly for the reason `intent_we_never_minted` gives — none of the
        answers is anything the operator can act on differently — and partly
        because the banner must be true of all of them: a webhook confirming, a
        second dispatch sending, and a TTL job abandoning a row that was never
        sent are three very different facts about money, and only the operation
        page underneath can say which one this was.
        """
        log.info("operation %s: send arrived after it had moved to %s", exc.op_id, exc.from_state)
        return redirect(request, f"/operations/{exc.op_id}", msg=ALREADY_MOVED)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """404, 403 and the rest, inside the chrome.

        Replaces FastAPI's `{"detail": …}` JSON, which is what a mistyped URL
        rendered: a framework default with no chrome, no way back and no voice
        — the Arca spec's second blocking finding (§0.2).

        `exc.detail` reaches the page when this console wrote it — the
        framework's stock phrase is dropped (case-insensitively; it is the
        status said twice, and `web.ERROR_COPY` already says it in the
        product's voice). What survives is the useful half: `exports.py`'s
        dozen refusals, which explain why a file would have lied, and — on a
        403 — the name of the permission `require()` names.

        **The permission IS named** (A3 gate ruling, m4). The first cut hid it
        on the theory that it maps the console's gates for whoever probed one;
        the ruling is that an operator who cannot tell their administrator
        which action to grant is stranded on a page that exists to unstrand
        them, and the gate names are already in `PERMISSIONS.md` and in every
        deployment's `ROLES_FILE`. What is still never named is which roles
        hold it, or who does.

        None of these sentences are Conduit's: an upstream problem never
        becomes an `HTTPException` in this app.

        Three responses keep the framework shape: 401, because the auth layer's
        own `_unauthenticated` answers those before a route is reached and a
        401 rendered as a page would swallow the login redirect; anything
        carrying `headers` (a redirect or a `Retry-After`), which is a
        machine's answer and not a page; and **`/webhooks/*`, whose caller is
        Conduit's sender** — it reads a status and a JSON body, and an HTML
        page with a ribbon in it is not an answer to a machine (A3 gate, n7).
        """
        if (
            exc.status_code == 401
            or exc.headers
            or request.url.path.startswith(WEBHOOK_PREFIX)
        ):
            return await http_exception_handler(request, exc)
        log.info(
            "%s %s -> %s [%s]",
            request.method,
            request.url.path,
            exc.status_code,
            # The request id, so a 403 or a 404 an operator reports can be
            # matched to its line (A3 gate, n8) — the same id the 500 page
            # prints.
            getattr(request.state, "request_id", ""),
        )
        return render_error(request, exc.status_code, detail=_route_sentence(exc))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        """The 500 page: a reference, and nothing else about the failure.

        The reference is `request.state.request_id`, minted by the middleware
        below and logged here beside the traceback — so the operator has a token
        to quote and the page carries no traceback, no host, no key and no
        request body. The traceback goes to the log, which is where it belongs.

        Registered on `Exception`, so Starlette routes it through
        `ServerErrorMiddleware` — which sits OUTSIDE the middleware below, so
        this response is the one that would otherwise ship without the security
        headers. It applies them itself; `test_hardening` pins that.

        If rendering this page ALSO fails (a template error, a dead
        template loader) Starlette falls back to its own bare 500. Catching that
        would mean a second hand-written page for a failure mode that is a
        deployment fault, and the fallback is already safe — it leaks nothing.
        """
        reference = getattr(request.state, "request_id", "")
        log.exception("unhandled error [%s] on %s %s", reference, request.method, request.url.path)
        response = render_error(request, 500, reference=reference)
        response.headers.update(SECURITY_HEADERS)
        return response

    # Added LAST, so it runs OUTERMOST: Starlette runs middleware in reverse
    # registration order, and these headers must be on the 401 and the 403 the
    # auth layer returns as much as on a rendered page.
    #
    # `script-src 'self'` with no `unsafe-inline`: every script in this console
    # is a file under `/static` (htmx, conditions.js, app.js) and every handler
    # is an htmx attribute, which needs no script-src allowance. Styles keep
    # `'unsafe-inline'` because `base.html` carries a `<style>` block and the
    # form engine sets inline widths. HSTS is deliberately absent — it belongs
    # at the TLS terminator, which is the thing that knows whether TLS is on.
    @app.middleware("http")
    async def security_headers(request, call_next):
        # The request's own id, minted here because this is the outermost thing
        # that runs on every request — and read by the 500 handler, which runs
        # OUTSIDE this middleware but on the same ASGI scope, where
        # `request.state` lives. Twelve hex characters: enough to find one line
        # in a log, short enough to read down a phone.
        #
        # It is not Conduit's `correlationId`. That belongs to an upstream call
        # and is unreachable from here: the client is process-wide and holds no
        # request, and Starlette's `BaseHTTPMiddleware` runs the app in its own
        # task, so a contextvar set downstream does not propagate back out to
        # the error handler. An honest console id beats a correlation id that
        # would be right only sometimes.
        request.state.request_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    return app
