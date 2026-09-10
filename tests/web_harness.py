"""Shared rig for the route tests: the real ASGI app, the real auth middleware,
the real templates, real Postgres — Conduit stubbed at the HTTP layer.

The point of going through `create_app()` rather than a hand-built router is
that an assertion here is a statement about the deployed console: its CSRF, its
role guards, its rendering.
"""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI

from app.auth.providers import ProxyProvider
from app.auth.tokens import CSRF_COOKIE, CSRF_HEADER, cookie_name
from app.auth.web import route_gates
from app.conduit import ConduitClient
from app.config import Settings
from app.main import create_app
from app.permissions import VIEW
from app.web import seal_intent

PROXY_SECRET = "proxy-shared-value-for-route-tests"
PROXY = Settings(
    auth_mode="proxy",
    proxy_shared_secret=PROXY_SECRET,
    auth_role_map="ops=operator,admins=admin,readers=viewer",
)
CSRF_NAME = cookie_name(CSRF_COOKIE, secure=True)

Handler = Callable[[httpx.Request], httpx.Response]


def stub(routes: dict[tuple[str, str], object], calls: list | None = None) -> Handler:
    """`{(method, path): response-or-callable}`; anything unlisted 404s loudly so
    a test never silently exercises a path it did not mean to."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path, request.read()))
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404,
                json={"type": "NOT_FOUND", "title": f"no stub for {request.method} {request.url.path}"},
            )
        return route(request) if callable(route) else route

    return handler


def make_app(handler: Handler) -> FastAPI:
    app = create_app()
    app.state.conduit = ConduitClient(transport=httpx.MockTransport(handler))
    # The middleware is already installed with the environment's settings; only
    # the identity source is swapped, so role variation is a header away.
    app.state.auth_provider = ProxyProvider(PROXY)
    return app


def client(app: FastAPI, groups: str = "ops") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://console.test",  # https: `__Host-` cookies need Secure
        headers={
            "X-Proxy-Auth": PROXY_SECRET,
            "X-Auth-Request-User": "ops@example.com",
            "X-Auth-Request-Email": "ops@example.com",
            "X-Auth-Request-Groups": groups,
        },
    )


@asynccontextmanager
async def signed_in(app: FastAPI, groups: str = "ops"):
    """A client holding the CSRF cookie a browser would have after one page."""
    async with client(app, groups) as web:
        response = await web.get("/drafts")
        assert response.status_code == 200, response.text
        yield web


@asynccontextmanager
async def signed_in_as(app: FastAPI, *permissions: str):
    """Signed in holding `console.view` and exactly the permissions named.

    The built-in bundles grant whole sets together — `operator` holds all five
    contact-and-whitelist actions at once — so a *partial* holder can only be a
    deployment-defined role, which is what a client's `ROLES_FILE` produces
    (PERMISSIONS.md, "Custom roles"). This builds one, through the same
    `Settings` → `ProxyProvider` → `AUTH_ROLE_MAP` path a deployment uses, so
    what a test renders is what that deployment would see.
    """
    path = Path(tempfile.mkdtemp()) / "roles.json"
    path.write_text(json.dumps({"partial": [VIEW, *permissions]}))
    app.state.auth_provider = ProxyProvider(
        Settings(
            auth_mode="proxy",
            proxy_shared_secret=PROXY_SECRET,
            auth_role_map="partial=partial",
            roles_file=str(path),
        )
    )
    async with signed_in(app, groups="partial") as web:
        yield web


def _targets(html: str) -> set[tuple[str, str]]:
    """`(method, path)` for every control the page offers — htmx requesters and
    ordinary links alike. Fragments, external URLs and query strings are dropped:
    what is being asked is which ROUTE the affordance points at."""
    found = set()
    for method, attribute in (("POST", "hx-post"), ("GET", "hx-get"), ("GET", "href")):
        for url in re.findall(rf'{attribute}="([^"]*)"', html):
            path = url.split("?")[0].split("#")[0]
            if path.startswith("/"):
                found.add((method, path))
    return found


def hero_numerals(html: str) -> list[str]:
    """Every element on this page carrying the editorial-numeral class.

    §2.3's rule has two halves and this is how both are stated as a test:
    the hero treatment is *present* on a page's summary figures, and *absent*
    from every table cell. `cells_with_hero` below is the second half; keeping
    them next to each other is deliberate, because a rule with only its
    positive half pinned is how a 44px amount ends up down a ledger column.
    """
    return re.findall(r'<[^>]*class="[^"]*\bhero-num\b[^"]*"[^>]*>', html)


def cells_with_hero(html: str) -> list[str]:
    """Every `<td>` whose content carries the hero class — must always be [].

    Cheap and deliberately syntactic: each cell's own markup, up to the next
    `</td>`, searched for the class. A grep, in other words, which is exactly
    what the rule is — "never a table cell" is not a thing a computed style can
    answer, because the offending cell would look perfectly valid.
    """
    return [cell for cell in re.findall(r"<td\b.*?</td>", html, re.S) if "hero-num" in cell]


def forbidden_affordances(app: FastAPI, html: str, permissions: set[str]) -> list[str]:
    """Every control on this page whose own route would answer 403.

    A gated surface may withhold a button and say why; what it must never do is
    paint one that refuses when pressed. Matching is against the app's real
    routing table, so a control pointing at a route whose gate later changes
    fails here rather than in front of an operator.
    """
    gates = [
        (method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"), declared)
        for method, path, _endpoint, declared in route_gates(app)
    ]
    return sorted(
        f"{method} {path} needs {sorted(declared - set(permissions))}"
        for method, path in _targets(html)
        for gate_method, pattern, declared in gates
        if gate_method == method and pattern.match(path) and not declared <= set(permissions)
    )


# A structurally-whole PNG: signature + IHDR, which is what `documents.sniff`
# checks. Small enough to inline, real enough to pass the trust boundary.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64 + b"\n%%EOF\n"


def documents_stub(request: httpx.Request) -> httpx.Response:
    """`POST /v2/documents`, minting the id the test asked for.

    The console sends the file's name in the multipart body, so a test names the
    `doc_` id it is about to attach by uploading under that filename — no shared
    counter, no ordering between uploads. Conduit's real ids are opaque, so
    which string comes back is the stub's business either way.
    """
    match = re.search(rb'filename="([^"]+)"', request.read())
    name = match.group(1).decode() if match else "doc_1.png"
    return httpx.Response(201, json={"id": name.rsplit(".", 1)[0]})


async def upload(
    web: httpx.AsyncClient, *, purpose: str, filename: str = "doc_1.png", content: bytes = PNG
) -> httpx.Response:
    """One file through the real upload route, exactly as `static/app.js` sends
    it: raw bytes as the body, everything else in the query string.

    Tests that attach a document have to go through this rather than inventing a
    `doc_` id, because an id that is not in this console's own
    upload ledger is refused before it can reach Conduit.
    """
    return await web.post(
        f"/documents?purpose={purpose}&filename={filename}",
        content=content,
        headers={
            "content-type": "application/octet-stream",
            CSRF_HEADER: web.cookies.get(CSRF_NAME) or "",
        },
    )


def form(**fields: object) -> bytes:
    items = []
    for name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        items += [(name.replace("__", "."), str(v)) for v in values]
    return str(httpx.QueryParams(items)).encode()


async def post(web: httpx.AsyncClient, url: str, body: bytes = b"", **kwargs) -> httpx.Response:
    """A mutation exactly as htmx sends it: urlencoded body, CSRF in the header."""
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        CSRF_HEADER: web.cookies.get(CSRF_NAME) or "",
        "HX-Request": "true",
        **kwargs.pop("headers", {}),
    }
    return await web.post(url, content=body, headers=headers, **kwargs)


def minted_intent(nonce: uuid.UUID | None = None) -> str:
    """A submission token exactly as a render would have put one on the page.

    The `intent` field carries a *sealed* nonce, and a bare uuid is
    refused with a 422 before anything is sent — so a test that hand-mints one
    is now testing the forgery guard rather than whatever it meant to test.
    Scraping the real render with `intent()` above is still better where the test
    has a render to scrape; this is for the cases that have not rendered the form
    (a nonce shared with a directly-seeded `operations.start`, or one deliberately
    replayed at a second resource).

    `nonce` is the uuid to seal, for the tests that also hand it to
    `operations.start` and need both halves to name the same one.
    """
    return seal_intent(nonce or uuid.uuid4())


def intent(html: str) -> str:
    """The one-use nonce this render minted (OPERATIONS_SPEC §1).

    A test that hand-builds a body without it is testing a form no browser ever
    submitted — the guard only works if the value comes from the render.
    """
    marker = 'name="intent" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def hx_headers(html: str) -> dict:
    """The `hx-headers` object every htmx request under <body> inherits — this is
    where the CSRF token for every mutating form in the page comes from."""
    marker = "hx-headers='"
    start = html.index(marker) + len(marker)
    return json.loads(html[start : html.index("'", start)].replace("&#34;", '"'))
