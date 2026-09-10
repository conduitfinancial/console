"""USD↔EUR conversions: quote → option → order → execution (plan v2 §7).

Route-free like `app/accounts/` and `app/payments/`: `app/web/convert.py` renders
what this module shapes and `tests/e2e/05_transfers_conversions.py` calls the
same functions, so what the live run proves is what an operator sees.

Four facts drive everything here.

* **A conversion quote is a different call from a withdrawal quote, on the same
  endpoint.** `CreateQuoteRequestDto.destinationCountry` is the discriminator the
  spec spells out: *differing assets + no country prices a conversion*; the same
  asset + a country prices a withdrawal (that mode is `payments.quote_request`).
  So the only difference here is what is *absent*.

* **`POST /v2/quotes` refuses an `Idempotency-Key`** (verified live 2026-08-28,
  OPERATIONS_SPEC §5) and creates nothing, so quoting is not ledgered — in either
  mode. Everything after it is: `order_create`, `order_execute`, `order_cancel`.

* **The option is what is redeemed, not the quote.** `QuoteRedemptionOrderDto`
  carries `quoteOptionId` and *must not* carry `amount`/`lockSide` — both are
  inherited from the option. Expiry is a property of the option, which is why the
  stale-confirmation guard lives at the confirm step and orders have no `expired`
  status (plan v2 §11).

* **Money is a decimal string, end to end** — `payments.amount` parses through
  `Decimal` only to refuse what is not a number and hands the operator's own
  digits back. `tests/test_conversions.py` greps this module for float calls —
  no binary float ever touches an amount here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app import payments, projections
from app.conduit.client import Result, Success

QUOTE_PATH = "/v2/quotes"
ORDER_PATH = "/v2/orders"

# Pairs Conduit will quote, deliberately not the currencies an account may hold:
# the two coincide today and are not the same fact.
CONVERT_ASSETS: tuple[str, ...] = ("USD", "EUR")

# `CreateQuoteRequestDto.lockSide` — which side the operator typed.
LOCK_SIDES: tuple[tuple[str, str], ...] = (
    ("source", "Convert exactly this much out of the source account."),
    ("destination", "Land exactly this much in the destination account."),
)
LOCK_SIDE_VALUES = tuple(value for value, _ in LOCK_SIDES)

# `OrderExternalResponseDto.status` — no `expired` (plan v2 §11). Read from the
# projections ladder so the pill, the reconciler and this module cannot drift.
ORDER_STATUSES: tuple[str, ...] = tuple(projections.STATE_RANKS["orders"])


def opposite_assets(asset: str) -> list[str]:
    """The currencies a conversion out of `asset` can land in: `CONVERT_ASSETS`
    minus the source itself, and **nothing** out of a source Conduit will not
    quote from at all — the whole pair is not "both are available", it is two
    pairs Conduit refuses. The empty string names no such source either."""
    source = (asset or "").upper()
    if source not in CONVERT_ASSETS:
        return []
    return [code for code in CONVERT_ASSETS if code != source]


def quote_request(*, source: str, destination: str, amount_text: str, lock_side: str) -> dict:
    """`CreateQuoteRequestDto` in **conversion** mode: differing assets and no
    `destinationCountry`. The absence is the discriminator, so there is
    deliberately no country argument to pass by mistake."""
    return {
        "source": {"code": source},
        "destination": {"code": destination},
        "lockSide": lock_side,
        "amount": amount_text,
    }


def option_of(view: Mapping | None, option_id: str) -> dict | None:
    """One option out of `payments.quote_view`'s panel, by id."""
    for option in (view or {}).get("options") or []:
        if option.get("id") == option_id:
            return dict(option)
    return None


def selection(view: Mapping, option: Mapping) -> dict:
    """What is stored about the operator's choice *before* the order is sent.

    The order DTO itself only carries the option id, so the expiry the confirm
    screen counts down against and the rate the operator agreed to have nowhere
    to live on the operation row. They live on the operation's own audit event
    (`app.web.convert.OPTION_SELECTED`) — an actor-attributed record of a
    decision, which is exactly what an audit row is for, and it needs no column
    and no migration.
    """
    return {
        "quoteId": view.get("id"),
        "quoteOptionId": option.get("id"),
        "expiresAt": view.get("expires_at"),
        "lockSide": view.get("lock_side"),
        "endUserRate": option.get("end_user_rate"),
        "referenceRate": option.get("reference_rate"),
        "spreadBps": option.get("spread_bps"),
        "sourceAmount": option.get("source_amount"),
        "destinationAmount": option.get("destination_amount"),
        "totalDebit": option.get("total_debit"),
        "fees": list(option.get("fees") or []),
    }


def order_body(*, quote_option_id: str, source_id: str, destination_id: str) -> dict:
    """`QuoteRedemptionOrderDto`.

    `amount` and `lockSide` are inherited from the option and the DTO refuses
    them (`additionalProperties: false`); `clientReferenceId` is injected at call
    time by `app.conduit.execute` and never stored, so the double-submit hash
    stays stable (OPERATIONS_SPEC §5).

    `autoExecute` is deliberately not sent: the default is false, which is the
    behaviour the console renders — an explicit Execute button the operator
    presses once the source is funded.
    """
    return {
        "quoteOptionId": quote_option_id,
        "source": {"type": "virtual_account", "id": source_id},
        "destination": {"type": "virtual_account", "id": destination_id},
    }


# --- reads ------------------------------------------------------------------------------


async def fetch_order(client, order_id: str) -> dict | Result:
    """`GET /v2/orders/{id}` — the dict, or the failing `Result` unchanged so the
    route renders Conduit's own problem-detail."""
    result = await client.get(f"{ORDER_PATH}/{order_id}")
    if isinstance(result, Success) and isinstance(result.data, dict):
        return result.data
    return result


# --- the order detail view ---------------------------------------------------------------


def _money(value: Any) -> str:
    if not isinstance(value, Mapping) or not value.get("amount"):
        return ""
    return f"{value['amount']} {value.get('code') or ''}".strip()


def rate_rows(order: Mapping | None) -> list[dict]:
    """`rate {endUserRate, referenceRate, totalSpreadBps, …}`, whatever it
    carries — walked rather than switched on, so a new rate field shows up
    instead of disappearing."""
    rate = (order or {}).get("rate")
    if not isinstance(rate, Mapping):
        return []
    return [
        {"label": payments.humanize(key), "value": str(value)}
        for key, value in rate.items()
        if isinstance(value, (str, int, float)) and str(value).strip()
    ]


def order_view(order: Mapping | None) -> dict:
    """The typed conversion detail (plan v2 §7). Amounts stay strings.

    **An order states each leg's amount inside its own asset block** —
    `sourceAsset: {code, amount}` — and there is no `sourceAmount` /
    `destinationAmount` key anywhere in `OrderExternalResponseDto` (pinned spec,
    and confirmed against live sandbox orders 2026-08-29). Reading the keys that
    do not exist rendered every real row's Out and In as em-dashes while the
    fabricated fixture, which had invented them, kept the tests green.
    """
    order = order or {}
    return {
        "type": order.get("type") or "",
        "source_asset": (order.get("sourceAsset") or {}).get("code") or "",
        "destination_asset": (order.get("destinationAsset") or {}).get("code") or "",
        "source_amount": _money(order.get("sourceAsset")),
        "destination_amount": _money(order.get("destinationAsset")),
        "total_debit": _money(order.get("totalDebit")),
        "lock_side": order.get("lockSide") or "",
        "lock_expires_at": order.get("lockExpiresAt") or "",
        "execution_trigger": order.get("executionTrigger") or "",
        "quote_option_id": order.get("quoteOptionId") or "",
        "auto_execute": order.get("autoExecute") is True,
        "failure_code": order.get("failureCode") or "",
        "failure_message": order.get("failureMessage") or "",
        "cancellation_reason": order.get("cancellationReason") or "",
        "fees": payments.fee_rows(order),
        "markup": payments.markup_row(order),
        "rate": rate_rows(order),
        "transactions": [t for t in order.get("linkedTransactionIds") or [] if isinstance(t, str)],
    }


def _actionable(order: Mapping | None) -> bool:
    """A status this build has never heard of enables nothing (plan v2 §7): we
    do not know whether it is terminal, so we do not offer a state-dependent
    action on it."""
    return str((order or {}).get("status") or "") in ORDER_STATUSES


def can_execute(order: Mapping | None) -> bool:
    """`POST /v2/orders/{id}/execute` is callable while the order is pending and
    nothing has claimed execution yet — including on an `autoExecute` order,
    which the spec says explicitly."""
    order = order or {}
    return (
        _actionable(order)
        and order.get("status") == "pending"
        and not order.get("executionTrigger")
    )


def can_cancel(order: Mapping | None) -> bool:
    order = order or {}
    return _actionable(order) and order.get("status") == "pending"


# --- sandbox simulators -------------------------------------------------------------------

# Sandbox host only; the pinned production spec has no `/v2/sandbox` path at all.
# `simulate/cosign` is **not** here: it is the non-custodial crypto signing leg,
# which v1 does not touch. These two are what the sandbox spec offers for a fiat
# conversion order (read 2026-08-28 from the sandbox OpenAPI), and settlement of
# the resulting `fiat_conversion` transaction is the transactions page's
# `simulate/terminal`.
SIMULATE_PATH = "/v2/sandbox/orders/{id}/simulate/{action}"
SIMULATE_ACTIONS: tuple[tuple[str, str], ...] = (
    ("conversion-failed", "Drive the conversion to failed."),
    ("rate-lock-expired", "Backdate the rate lock so the order cancels as expired."),
)
SIMULATE_VALUES = tuple(action for action, _ in SIMULATE_ACTIONS)


def order_rows(order: Mapping | None) -> list[dict]:
    """Every scalar Conduit sent on an order, for the raw panel — the same
    fallback the transactions ledger uses for a type it has no view for.

    This is also why there is no typed funding-address panel. A
    deposit-funded order publishes `depositInstructions`, which v1 never creates
    (both sides are virtual accounts) — and if one ever arrives, every field of
    it is already on the page here rather than hidden.
    """
    return payments.generic_rows(order or {})
