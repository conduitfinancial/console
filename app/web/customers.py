"""Customers: list, detail, and the bank-account request (plan v2 §7 Accounts).

The detail page is driven entirely by Conduit's own `features[] {feature,
isActive}` — inactive `virtual_account` offers the request button, active shows
the accounts, and anything this build does not implement (`crypto_wallet`, plus
whatever Conduit adds next) renders inert. No feature vocabulary is assumed.

The request flow is discovery and a POST with no local state:

    ?             → asset picker: the currencies discovery names for this
                    customer, from one call that omits `asset` entirely
    ?asset=USD    → that same picker plus discovery's form for the chosen
                    currency (form engine, Dialect A)
    POST          → re-read discovery, validate the body against the *submitted*
                    asset's schema, `POST /customers/{id}/features` via the ledger

The catalog and the per-currency answer are two different questions, which is
why `_discovery` asks both. The catalog says what this customer may hold at all;
the per-currency call says whether a provider will actually hold it right now,
and one currency 422-ing `NO_ELIGIBLE_PROVIDER` says nothing about any other —
so a refusal is named beside the picker rather than shortening it.

No draft table row. Onboarding pins a snapshot because it is a long
questionnaire whose answers must survive restarts; this is five fields answered
in one sitting, so the schema is fetched for the render and again for the
submit — and because the body is validated against the schema for the asset that
was actually submitted, it is always judged by the same rules Conduit will use.
If discovery moves between the two, the operator sees field errors on the
re-rendered form rather than a silent mismatch.
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, Response

from app import accounts, documents, forms, operations
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit import execute_operation
from app.conduit.client import ConduitClient, Page, Problem, Success
from app.web import (
    MOVE_MONEY,
    conduit,
    db,
    form_items,
    intent_of,
    local_problem,
    page_size,
    pager,
    problem_of,
    problem_view,
    redirect,
    render,
)
from app.web import onboarding  # `field_errors`: one 422→engine mapping in the app

router = APIRouter()

UNREACHABLE = "Conduit is unreachable"


def _problem(result, what: str, resource_id: str = "") -> dict:
    return (
        problem_view(result)
        if isinstance(result, Problem)
        else local_problem(UNREACHABLE, f"{what} could not be read.", resource_id=resource_id)
    )


# --- list ------------------------------------------------------------------------------


def list_query(query) -> dict:
    """This page's filters as the Conduit query they produce — paging aside.

    Customers have no status field — the dashboard tracks *application* status —
    so the filters are the ones the endpoint actually offers: customer type and
    the operator's own `clientReferenceId`. One parser for the page and for its
    CSV export (`app/web/exports.py`).

    `name` is deliberately **not** here: it is not a Conduit parameter, it is
    this console walking the list and matching locally (`name_of`, `walk_named`).
    Putting it on the wire would be inventing a filter the endpoint does not
    have — the thing this page has said out loud since it was written.
    """
    return {
        "type": [v for v in query.getlist("type") if v] or None,
        "clientReferenceId": query.get("clientReferenceId") or None,
    }


# How far a name search walks. Conduit serves 100 customers a page, so this is
# ten requests and ten thousand customers — and it is a **stated** ceiling: a
# search that reaches it says so, because a capped search presented as a complete
# one is exactly the lie this console does not tell. Aligned with the CSV
# export's own row cap (`exports.CAP_ROWS`) so the two surfaces reach the same
# distance into the list.
NAME_WALK_PAGES = 10
NAME_WALK_LIMIT = 100


def name_of(query) -> str:
    """The console-side name filter, or `""`."""
    return (query.get("name") or "").strip()


def matches(customer: dict, needle: str) -> bool:
    """Case-insensitive substring, over the names a customer can carry — and the
    id (2026-09-02): one search box answers both "find
    acme" and "find cus_034FD…", including a partial id pasted from anywhere.
    The id joins the haystack rather than gating on a `cus_` prefix, so a
    fragment from the MIDDLE of an id still matches.

    `display_name`'s chain is about *rendering* one name; this asks whether any
    of them matches, because an operator typing "acme" should find a customer
    whose trade name is ACME and whose legal name is something else entirely.
    """
    hay = " ".join(
        str(customer.get(key) or "")
        for key in ("legalName", "tradeName", "firstName", "lastName", "id")
    )
    return needle.lower() in hay.lower()


async def walk_named(client: ConduitClient, needle: str, wire: dict):
    """Every customer matching `needle`, up to the walk cap.
    `(matches, capped, problem)`.

    **`GET /v2/customers` has no name filter** — it takes `clientReferenceId` and
    `type`, and the applications endpoint's `search` was probed live and matches
    only a whole client reference. So a name search is this
    console reading the list and comparing, which is honest and bounded, and the
    page states both halves: what was searched, and that it stopped.

    The wire filters ride along, so the walk searches the population the operator
    asked about rather than a wider one. A failure ends the walk as a failure —
    a partial list answered as a complete search would be the same lie the cap
    exists to avoid, one step worse for being silent.
    """
    found: list[dict] = []
    cursor: str | None = None
    for _ in range(NAME_WALK_PAGES):
        result = await client.page(
            "/v2/customers", cursor=cursor, limit=NAME_WALK_LIMIT, **wire
        )
        if not isinstance(result, Page):
            return [], False, _problem(result, "The customer list")
        found += [item for item in result.items if matches(item, needle)]
        cursor = result.next_cursor
        if not cursor:
            return found, False, None
    return found, True, None


@router.get("/customers", response_class=HTMLResponse)
async def index(
    request: Request,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """One cursor page, filters passed straight through (plan v2 §4) — unless a
    name was typed, in which case the list is walked and matched here."""
    query = request.query_params
    wire = list_query(query)
    types, reference = wire["type"] or [], wire["clientReferenceId"]
    limit = page_size(query)
    name = name_of(query)
    capped = False
    problem = None
    if name:
        # A console-side search over a bounded walk: no cursor, because the
        # result is a *set* and not a position in Conduit's pagination.
        matched, capped, problem = await walk_named(client, name, wire)
        page = Page(items=matched, next_cursor=None, prev_cursor=None, total=None)
    else:
        page = await client.page(
            "/v2/customers",
            cursor=query.get("cursor") or None,
            direction=query.get("direction") or None,
            limit=limit,
            **wire,
        )
        if not isinstance(page, Page):
            problem = _problem(page, "The customer list")
            page = Page(items=[], next_cursor=None, prev_cursor=None, total=None)
    return render(
        request,
        "customers/list.html",
        section="customers",
        page=page,
        pager=pager(
            "/customers",
            {"clientReferenceId": reference, "type": types, "name": name},
            page,
            size=limit,
        ),
        problem=problem,
        # `search_name`, not `name`: `render`'s own second parameter is the
        # template name, and a context key called `name` collides with it.
        search_name=name,
        name_capped=capped,
        walk_pages=NAME_WALK_PAGES * NAME_WALK_LIMIT,
        types=sorted({str(i.get("type")) for i in page.items if i.get("type")} | set(types)),
        selected_types=types,
        reference=reference or "",
        limit=limit,
    )


# --- detail ----------------------------------------------------------------------------


def _identity(customer: dict) -> list[tuple[str, str]]:
    """Whatever identity fields this customer actually carries.

    Business and individual customers are different DTOs; rather than two
    templates, the page renders the scalar fields present, in the order Conduit
    sent them, minus the ones with their own place on the page.
    """
    skip = {"id", "type", "features", "applicationId"}
    rows = []
    for key, value in customer.items():
        if key in skip:
            continue
        if isinstance(value, (str, int, float, bool)):
            rows.append((accounts.humanize(key), str(value)))
        elif isinstance(value, dict):
            parts = [str(v) for v in value.values() if isinstance(v, (str, int, float))]
            if parts:
                rows.append((accounts.humanize(key), ", ".join(parts)))
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            rows.append((accounts.humanize(key), ", ".join(value)))
    return rows


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    result = await client.get(f"/v2/customers/{customer_id}")
    customer = result.data if isinstance(result, Success) and isinstance(result.data, dict) else None
    problem = None if customer else _problem(result, "The customer", customer_id)

    page, accounts_problem = None, None
    if accounts.has_active(customer):
        # Only when Conduit says the feature is live: asking otherwise spends a
        # request to be told the same thing `features[]` already said.
        page = await accounts.fetch_accounts(
            client,
            customer_id,
            cursor=request.query_params.get("accounts_cursor") or None,
            direction=request.query_params.get("accounts_direction") or None,
        )
        if not isinstance(page, Page):
            accounts_problem = _problem(page, "The virtual accounts", customer_id)
            page = None
    return render(
        request,
        "customers/detail.html",
        section="customers",
        customer_id=customer_id,
        customer=customer,
        identity=_identity(customer or {}),
        features=accounts.feature_rows(customer),
        # Balances for the table below, out of the rows the list call already
        # returned — `GET /v2/customers/{id}/virtual-accounts` ships `balances[]`
        # on every item (pinned `VirtualAccountListResponseClass`, and confirmed
        # live on the sandbox 2026-08-29). The same shaper the account's own page
        # uses, so the two surfaces cannot disagree about a number.
        balance_rows=accounts.balance_rows,
        accounts_page=page,
        accounts_problem=accounts_problem,
        problem=problem,
        can_act=actor.can_any(*MOVE_MONEY),
        can_request_account=actor.can("account.request"),
    )


# --- the bank-account request ----------------------------------------------------------


def _parse(snapshot: dict | None) -> tuple[forms.FormModel | None, dict | None]:
    """The one place a requirements snapshot becomes a model.

    `forms.parse` raises on an unsupported `schemaVersion`, and both the GET and
    the POST parse — so the guard lives here rather than at each call site,
    where the GET's was missing and turned a schema bump into a 500
    (FORM_ENGINE_SPEC §1: never render best-effort).
    """
    if snapshot is None:
        return None, None
    try:
        return forms.parse(snapshot), None
    except forms.SchemaVersionMismatch as mismatch:
        return None, local_problem(
            "Requirements schema not supported",
            f"{mismatch} — this console needs an update before it can request an account.",
        )


def _render_request(
    request: Request,
    customer_id: str,
    asset: str,
    snapshot: dict | None,
    options: list[str],
    values: forms.FormValues | None = None,
    errors: forms.FormErrors | None = None,
    problems: list[dict] | None = None,
    refused: dict[str, dict] | None = None,
    can_request_account: bool = True,
    status_code: int = 200,
) -> Response:
    model, mismatch = _parse(snapshot)
    problems = list(problems or [])
    if mismatch:
        problems.append(mismatch)
        status_code = 502
    return render(
        request,
        "customers/request.html",
        section="customers",
        customer_id=customer_id,
        asset=asset,
        assets=options,
        model=model,
        rm=forms.render_model(model, values, errors) if model else None,
        purpose=accounts.PURPOSE,
        problems=problems,
        refused=refused or {},
        can_request_account=can_request_account,
        status_code=status_code,
    )


class Discovery(NamedTuple):
    """What one page view of the request flow learned from Conduit.

    Four fields because the page asks four different things of it, and folding
    any two together loses a distinction the operator can see: `options` is the
    picker (Conduit's own currency list, verbatim); `snapshot` is the chosen
    currency's requirements, or `None` when none was chosen or it was refused;
    `problems` are the failures that earn a full card; and `refused` names the
    chosen currency's refusal *beside* the picker, so "why can I not have EUR"
    is one click from Conduit's answer instead of a silently shorter list.
    """

    options: list[str]
    snapshot: dict | None
    problems: list[dict]
    refused: dict[str, dict]


async def _discovery(
    client: ConduitClient, customer_id: str, asset: str = ""
) -> Discovery:
    """The catalog call always; the chosen currency's own call only when there
    is one.

    The catalog call omits `asset`, so Conduit resolves over every eligible
    provider and answers with this customer's whole currency list. It is the
    only source of the picker: when it fails there is no picker and the failure
    is rendered as itself — never a fallback list, the same rule
    `accounts.active()` keeps for an unreadable account list.

    The chosen currency is probed even when the catalog did not name it. Its own
    `422 NO_ELIGIBLE_PROVIDER`, with the `resolution` text that says what to do
    next, is a better answer than a console-invented "not available".

    One requirements call for the bare picker, two when a currency is
    chosen — flat in the number of currencies, where the old per-asset probe
    loop was one call each. No cache: it would have to key on
    (customer, asset, schemaVersion) and expire, for two calls.
    """
    problems: list[dict] = []
    refused: dict[str, dict] = {}
    catalog = await accounts.fetch_requirements(client, customer_id)
    options = []
    if not isinstance(catalog, dict):
        problems.append(_problem(catalog, "The account currencies", customer_id))
    elif accounts.states_allowed_assets(catalog):
        options = accounts.allowed_assets(catalog)
    else:
        # A 200 this console could not read renders as the same empty picker as
        # "Conduit named none", and only one of those is a fact about the
        # customer.
        problems.append(
            local_problem(
                "The account currencies could not be read",
                "Conduit answered, but the answer carried no currency list this console "
                "could find — so this is not Conduit saying there are none.",
                "Reload before requesting an account: a request is reviewed by a human.",
                resource_id=customer_id,
            )
        )
    snapshot = None
    if asset:
        result = await accounts.fetch_requirements(client, customer_id, asset)
        if isinstance(result, dict):
            snapshot = result
        else:
            refused[asset] = _problem(result, f"{asset} account requirements", customer_id)
            problems.append(refused[asset])
    return Discovery(options, snapshot, problems, refused)


@router.get("/customers/{customer_id}/request-account", response_class=HTMLResponse)
async def request_form(
    request: Request,
    customer_id: str,
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("console.view")),
) -> Response:
    """No asset yet → the picker; an asset → the picker and discovery's form for
    it.

    The bare picker carries no red card of its own: a currency is only refused
    once it has been chosen, so nothing here can open with a refusal about a
    currency the operator never asked for. What a chosen currency's refusal
    does carry is both — the full card, and its name beside the picker.
    """
    asset = (request.query_params.get("asset") or "").strip().upper()
    found = await _discovery(client, customer_id, asset)
    values = forms.FormValues(root={"asset": {"code": asset}}) if asset else None
    return _render_request(
        request,
        customer_id,
        asset,
        found.snapshot,
        found.options,
        values=values,
        problems=found.problems,
        refused=found.refused,
        can_request_account=actor.can("account.request"),
    )


@router.post("/customers/{customer_id}/request-account")
async def request_submit(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("account.request")),
) -> Response:
    """`POST /v2/customers/{id}/features` through the operations ledger.

    Feature applications never auto-resolve: the answer is an `app_…` the
    dashboard already tracks, so a confirmed request redirects there.
    """
    items = await form_items(request)
    asset = accounts.submitted_asset(items)
    if not asset:
        # Only a malformed code stops here — nothing local knows which
        # well-shaped codes Conduit accepts, so everything else is asked.
        return redirect(
            request,
            f"/customers/{customer_id}/request-account",
            err="Pick an account currency — a three-letter code such as USD.",
        )
    found = await _discovery(client, customer_id, asset)
    model, mismatch = _parse(found.snapshot)
    if model is None:
        # Refused, unreachable, or a schema version this build cannot render:
        # in every case the request is not sent, and the operator is shown why.
        return _render_request(
            request,
            customer_id,
            asset,
            None,
            found.options,
            problems=[p for p in (*found.problems, mismatch) if p],
            refused=found.refused,
            status_code=502,
        )
    values = forms.parse_submission(model, items)
    errors = forms.validate(model, values)
    # The same rule the RFI and money forms hold to (`documents.attachable`): a
    # `doc_` id on this form has to be one this operator uploaded here, for this
    # purpose. The engine's own `minDocuments` floor above only counts them —
    # counting is satisfied just as well by another customer's bank statement,
    # and this application is exactly where a reviewer at Conduit reads them as
    # proof about *this* customer. On `errors.documents` and before
    # `operations.start`, so the re-render keeps the operator's answers and
    # nothing has been sent.
    if refused := await documents.unattachable(
        session, values.document_ids, purpose=accounts.PURPOSE, actor_id=actor.id
    ):
        errors.documents.append(
            forms.Message(documents.REFUSED_ATTACHMENT.format(count=len(refused)))
        )
    if not errors.ok:
        return _render_request(
            request,
            customer_id,
            asset,
            found.snapshot,
            found.options,
            values,
            errors,
            status_code=422,
        )

    op, is_new = await operations.start(
        session,
        type="feature_request",
        actor_id=actor.id,
        actor_email=actor.email,
        path=accounts.PATH.format(customer_id=customer_id),
        body=accounts.request_body(model, values),
        customer_id=customer_id,
        intent=intent_of(dict(items)),
    )
    if is_new:
        op = await execute_operation(
            session, op, client=client, actor_id=actor.id, actor_email=actor.email
        )
    if op.state == "confirmed" and op.conduit_resource_id:
        return redirect(request, f"/applications/{op.conduit_resource_id}")
    if op.state == "rejected":
        # Conduit's own refusal, rendered as itself: `NO_ELIGIBLE_PROVIDER`
        # arrives here as well as on discovery, and its `resolution` text is the
        # only thing that says what to do about it. Field errors, when the
        # rejection carries any, land on their fields through the same mapper
        # onboarding uses.
        return _render_request(
            request,
            customer_id,
            asset,
            found.snapshot,
            found.options,
            values,
            forms.map_validation_errors(model, onboarding.field_errors(op.error)),
            problems=[problem_of(op.error, op.conduit_resource_id)],
            status_code=422,
        )
    return redirect(request, f"/operations/{op.id}")
