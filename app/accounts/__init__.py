"""Virtual accounts: discovery, request bodies, deposit instructions
(plan v2 §7 Accounts).

Route-free on purpose — `app/web/customers.py` and `app/web/accounts.py` render
what this module shapes, and the live e2e script calls the same functions the
pages do, so what it proves is what an operator sees.

Two facts drive everything here:

* **A bank account is a feature application, not a create.** `POST
  /v2/customers/{id}/features` answers 202 with an application that a human
  decides later; the console follows it in the applications dashboard like any
  other. There is no "create account" call to make.
* **Eligibility is discovered twice.** `asset` is optional on the requirements
  endpoint, and omitting it resolves the feature over *every* eligible provider
  — so one unassetted call answers with the `/asset/code` `allowedValues` that
  is this customer's currency list, whole. That list is the picker, verbatim:
  this console holds no currency vocabulary of its own and never offers one
  Conduit did not name. The second discovery is per currency, because the field
  schema varies by provider, and it can *still* refuse with `422
  NO_ELIGIBLE_PROVIDER` (verified live on this org for EUR) — which is an answer
  about this customer, rendered as Conduit's own problem-detail rather than
  treated as a bug.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app import forms
from app.conduit.client import Page, Result, Success

FEATURE = "virtual_account"
PURPOSE = "feature_request"  # DocumentUploadDto's purpose for these uploads
PATH = "/v2/customers/{customer_id}/features"
ASSET_PATH = ("asset", "code")


# --- discovery ------------------------------------------------------------------------


async def fetch_requirements(
    client, customer_id: str, asset: str | None = None
) -> dict | Result:
    """`GET /customers/{id}/features/requirements`, or the failing `Result`
    unchanged so the route can render Conduit's own problem-detail.

    **With no asset the parameter is omitted entirely**, not sent empty: the
    pinned contract says an absent `asset` resolves the requirements over every
    eligible provider, which is the one call that can answer what currencies
    this customer may hold at all. With an asset the answer narrows to the
    providers that can hold it — and can be `422 NO_ELIGIBLE_PROVIDER`, which
    for EUR on this org is routine, and is a real answer about this customer
    rather than a transport failure.
    """
    result = await client.get(
        f"/v2/customers/{customer_id}/features/requirements",
        type=FEATURE,
        **({"asset": asset} if asset else {}),
    )
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    return result


async def fetch_accounts(
    client,
    customer_id: str,
    cursor: str | None = None,
    direction: str | None = None,
    asset: str | None = None,
    limit: int = 25,
):
    """One cursor page of `GET /customers/{id}/virtual-accounts` (`Page`), or the
    failing `Result` — an unreadable list is never rendered as an empty one.

    The cursor is passed through rather than walked (plan v2 §4): the customer
    detail page renders prev/next from the page's own cursors.

    `asset` is Conduit's own query parameter; this endpoint has no `status`.
    `limit` is clamped to the 100 the endpoint documents as its maximum.
    """
    return await client.page(
        f"/v2/customers/{customer_id}/virtual-accounts",
        cursor=cursor,
        direction=direction,
        limit=max(1, min(limit, 100)),
        **({"asset": asset} if asset else {}),
    )


async def active(client, customer_id: str) -> tuple[list[dict], Result | None]:
    """`(the customer's **active** virtual accounts, the failing Result)` — the
    only ones any screen can fund a movement from.

    One definition of "active" for Convert, Payouts, Transfers and Batches, and
    one honest shape for the failure: `fetch_accounts` hands its caller the
    failing `Result` precisely so that an unreadable list is never rendered as an
    empty one, and every one of those callers used to flatten it to `[]`. The
    page then told the operator this customer has no account — which sends them
    to request a duplicate one a human reviews, or to tell a client their account
    is missing. The problem card is minted by the caller (that is web layer's
    vocabulary); what this returns is the raw failure, so no caller can lose it
    without deleting a name.
    """
    page = await fetch_accounts(client, customer_id)
    if not isinstance(page, Page):
        return [], page
    return [a for a in page.items if a.get("status") == "active"], None


async def fetch_account(client, customer_id: str, virtual_account_id: str) -> dict | Result:
    result = await client.get(
        f"/v2/customers/{customer_id}/virtual-accounts/{virtual_account_id}"
    )
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    return result


def allowed_assets(snapshot: Mapping | None) -> list[str]:
    """The picker's options: discovery's `/asset/code` `allowedValues`, verbatim
    and in the order Conduit sent them.

    Conduit is the only thing that knows which currencies this customer may
    hold, so this states its answer and adds nothing to it. A snapshot that is
    missing, carries no `/asset/code` field, or names an empty list therefore
    offers **nothing** — there is no local currency list left for "unconstrained"
    to fall back to, and inventing one would put a currency on screen that
    Conduit never said it would accept.
    """
    return [
        code
        for code in (_asset_field(snapshot) or {}).get("allowedValues") or []
        if isinstance(code, str) and code.strip()
    ]


def _asset_field(snapshot: Mapping | None) -> Mapping | None:
    return next(
        (
            f
            for f in (snapshot or {}).get("fields") or []
            if isinstance(f, Mapping) and f.get("pointer") == "/asset/code"
        ),
        None,
    )


def states_allowed_assets(snapshot: Mapping | None) -> bool:
    """Whether the catalog answered the currency question at all — an
    `/asset/code` field carrying an `allowedValues` list.

    `allowed_assets` returns `[]` both for a catalog this console could not read
    and for one Conduit says names nothing. Those are opposite claims and only
    the second is a fact about the customer, so the caller asks this before
    rendering the empty picker as that fact.
    """
    field = _asset_field(snapshot)
    return field is not None and isinstance(field.get("allowedValues"), list)


# --- the request body -----------------------------------------------------------------


def request_body(model: forms.FormModel, values: forms.FormValues) -> dict:
    """`FeatureRequestDto {type, asset:{code}, fields, documentIds}`.

    The engine assembles one nested dict from the pointers discovery shipped;
    `/asset/code` is a field like any other there, and the DTO wants it at the
    top level next to `fields`. So this lifts `asset` and `documentIds` out and
    everything else goes under `fields` — no field name is hardcoded.
    """
    assembled = forms.assemble(model, values)
    document_ids = assembled.pop("documentIds", None)
    asset = assembled.pop("asset", None)
    body: dict[str, Any] = {"type": FEATURE}
    if isinstance(asset, Mapping) and asset.get("code"):
        body["asset"] = {"code": asset["code"]}
    if assembled:
        body["fields"] = assembled
    if document_ids:
        body["documentIds"] = document_ids
    return body


def submitted_asset(items: Sequence[tuple[str, str]]) -> str:
    """The currency the operator picked, read off the raw submission.

    The schema to validate against depends on the asset, and the asset is
    answered inside the form — so it is read before a model exists, then
    validated by that model like any other enum.

    Shape-only check (three ASCII letters), never membership. With no
    local currency list there is nothing here that *could* know an unknown code
    is wrong, and discovery is what knows — so a well-shaped code Conduit has
    never heard of costs one refused request and gets Conduit's own answer,
    which is better than a console-invented "not available". The same call
    `app/web/accounts.py` makes for the sandbox deposit simulator.
    """
    name = forms.field_name(ASSET_PATH)
    value = next((v for n, v in items if n == name), "").strip().upper()
    return value if re.fullmatch(r"[A-Z]{3}", value) else ""


# --- customer features ----------------------------------------------------------------


def feature_rows(customer: Mapping | None) -> list[dict]:
    """`features[] {feature, isActive}` → what the detail page may offer.

    `crypto_wallet` is out of v1 (plan v2 §0) and anything Conduit adds later is
    unknown to this build: both render inert with a plain sentence, never a
    button that would post a body this console cannot construct.
    """
    rows = []
    for entry in (customer or {}).get("features") or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("feature") or "")
        rows.append(
            {
                "feature": name,
                "label": name.replace("_", " ").capitalize() or "Unknown feature",
                "is_active": entry.get("isActive") is True,
                "supported": name == FEATURE,
                "note": "" if name == FEATURE else "not available in this console",
            }
        )
    return rows


def has_active(customer: Mapping | None, feature: str = FEATURE) -> bool:
    return any(r["feature"] == feature and r["is_active"] for r in feature_rows(customer))


# --- deposit instructions -------------------------------------------------------------

# Ordered `(dotted path, label)` pairs, walked against every instruction block
# whatever its `type`. One table rather than three per-variant renderers: the
# variants overlap almost completely (us_domestic adds `rails`, swift adds
# `correspondent`, sepa is the swift block without the hop), and a block that
# ships a field this table has never heard of still renders — see `_extra`.
LABELS: tuple[tuple[str, str], ...] = (
    ("beneficiaryName", "Beneficiary name"),
    ("beneficiaryAddress", "Beneficiary address"),
    ("accountNumber", "Account number"),
    ("iban", "IBAN"),
    ("bic", "BIC"),
    ("paymentReference", "Payment reference"),
    ("bank.legalName", "Bank name"),
    ("bank.address", "Bank address"),
    ("bank.bic", "Bank BIC"),
    ("correspondent.name", "Correspondent bank"),
    ("correspondent.address", "Correspondent address"),
    ("correspondent.bic", "Correspondent BIC"),
    ("correspondent.accountNumber", "Correspondent account number"),
)

# Top-level keys the card renders through something other than a copy row: the
# block's own kind and currency are the card's header. `rails` is a list, so it
# never reaches the scalar branch anyway — it is named here for the reader.
STRUCTURAL_SCALARS = frozenset({"type", "currency", "rails"})

TITLES = {
    "us_domestic": "US domestic (ACH · Fedwire · RTP)",
    "swift": "SWIFT (international)",
    "sepa": "SEPA (euro area)",
}


def _dig(block: Mapping, dotted: str) -> Any:
    node: Any = block
    for key in dotted.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and str(value).strip() != ""


LABELLED = frozenset(dotted for dotted, _ in LABELS)


def _extra(block: Mapping, path: tuple[str, ...] = ()) -> list[dict]:
    """Every scalar this build has no label for, at any depth, rendered with a
    title-cased key path rather than dropped.

    Recursion is the point: `beneficiaryPostalAddress`, `bank.postalAddress` and
    `correspondent.postalAddress` are *structured* addresses, and a
    scalars-only pass at the top level silently dropped all three — on the swift
    and sepa blocks, where the free-text `beneficiaryAddress` is optional, that
    could mean rendering a card with no payee address on it at all.
    """
    rows = []
    for key, value in block.items():
        here = path + (key,)
        dotted = ".".join(here)
        if dotted in LABELLED or (not path and key in STRUCTURAL_SCALARS):
            continue
        if isinstance(value, Mapping):
            rows += _extra(value, here)
        elif _scalar(value):
            rows.append(_row(" · ".join(humanize(part) for part in here), value))
    return rows


def humanize(key: str) -> str:
    """`sortCode` / `sort_code` → "Sort code". Used for keys this build has no
    label for, so an unknown field is displayed rather than dropped."""
    spaced = "".join(f" {c.lower()}" if c.isupper() else c for c in key.replace("_", " ")).strip()
    return spaced[:1].upper() + spaced[1:]


def _row(label: str, value: Any, note: str = "") -> dict:
    """One copy row. `multiline` decides the widget: a postal address arrives
    with real newlines, and `<input>`'s value sanitizer strips them — so those
    rows are copied out of a textarea instead, or the operator pastes a
    one-line address into a wire form."""
    text = str(value)
    return {"label": label, "value": text, "note": note, "multiline": "\n" in text}


def _rail_rows(block: Mapping) -> list[dict]:
    """`rails[]` carries one routing number per US network; swift/sepa blocks
    carry the rail name and nothing to copy."""
    rows = []
    for entry in block.get("rails") or []:
        if not isinstance(entry, Mapping):
            continue
        rail = str(entry.get("rail") or "").upper()
        routing = entry.get("routingNumber")
        notes = []
        if entry.get("sameDayEligible"):
            notes.append("same-day eligible")
        if entry.get("networks"):
            notes.append("networks: " + ", ".join(str(n) for n in entry["networks"]))
        if entry.get("fednowCap"):
            notes.append(f"FedNow cap {entry['fednowCap']}")
        if _scalar(routing):
            rows.append(
                _row(f"{rail} routing number" if rail else "Routing number", routing,
                     " · ".join(notes))
            )
        elif rail:
            rows.append(_row("Rail", rail, " · ".join(notes)))
    return rows


def deposit_cards(account: Mapping | None) -> list[dict]:
    """One card per `depositInstructions[]` block: a title, the currency it
    collects, and labelled rows the operator copies one at a time."""
    cards = []
    for block in (account or {}).get("depositInstructions") or []:
        if not isinstance(block, Mapping):
            continue
        kind = str(block.get("type") or "")
        rows = [
            _row(label, _dig(block, dotted))
            for dotted, label in LABELS
            if _scalar(_dig(block, dotted))
        ]
        rows += _rail_rows(block)
        rows += _extra(block)
        cards.append(
            {
                "type": kind,
                "title": TITLES.get(kind) or humanize(kind) or "Deposit instructions",
                "known": kind in TITLES,
                "currency": block.get("currency") or "",
                "rows": rows,
            }
        )
    return cards


def balance_rows(account: Mapping | None) -> list[dict]:
    """`balances[]` → `{code, available, pending, frozen}`, amounts kept as the
    decimal strings Conduit sent (never parsed into a float — this is money).

    **A bucket Conduit did not state is `None`, never `"0"`.** The house rule
    already applied one level up — an account carrying no `balances` block at
    all renders an em-dash, because "we were told nothing" and "the balance is
    zero" are different facts — and it applies inside the block too: a balance
    object that carries `available` and omits `frozen` has told us nothing about
    frozen funds. Printing a zero there is the console inventing the reassuring
    answer, which on the frozen bucket is exactly the wrong direction to invent
    in. `m.amount` is what turns the None into the em-dash on screen.
    """
    rows = []
    for balance in (account or {}).get("balances") or []:
        if not isinstance(balance, Mapping):
            continue
        buckets = {
            name: balance.get(name) if isinstance(balance.get(name), Mapping) else {}
            for name in ("available", "pending", "frozen")
        }
        rows.append(
            {
                "code": next(
                    (b.get("code") for b in buckets.values() if b.get("code")),
                    (account or {}).get("asset", {}).get("code", ""),
                ),
                **{name: bucket.get("amount") or None for name, bucket in buckets.items()},
            }
        )
    return rows


def deposits_for(items: Sequence[Mapping], virtual_account_id: str) -> list[dict]:
    """The deposits on one account, out of the customer's page of them.

    `GET /v2/transactions` filters by customer and type but not by virtual
    account, so the narrowing happens here — within the single page the client
    fetched, never by walking cursors until enough rows match (plan v2 §4).
    """
    return [
        item
        for item in items
        if isinstance(item, Mapping)
        and _dig(item, "destination.virtualAccountId") == virtual_account_id
    ]


def amount_of(transaction: Mapping) -> str:
    """What landed, from whichever side of the transaction states it."""
    for side in ("destination", "source"):
        asset_amount = _dig(transaction, f"{side}.assetAmount")
        if isinstance(asset_amount, Mapping) and asset_amount.get("amount"):
            return f"{asset_amount['amount']} {asset_amount.get('code') or ''}".strip()
    return "—"
