"""Shared stub data for the payments route tests.

Kept out of `web_harness.py` because it is payments-specific, and out of the test
modules because both the recipients tests and the payout tests need the same
whitelist entry and the same virtual account.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"

FEDWIRE_BUSINESS = json.loads((FIXTURES / "payout_requirements_fedwire_business.json").read_text())
FEDWIRE_INTERCOMPANY = json.loads(
    (FIXTURES / "payout_requirements_fedwire_intercompany.json").read_text()
)
SEPA_BUSINESS = json.loads((FIXTURES / "payout_requirements_sepa_business.json").read_text())
CHAPS_BUSINESS = json.loads((FIXTURES / "payout_requirements_chaps_business.json").read_text())
USD_ACCOUNT = json.loads((FIXTURES / "virtual_account_usd.json").read_text())
EUR_ACCOUNT = json.loads((FIXTURES / "virtual_account_eur.json").read_text())
GBP_ACCOUNT = json.loads((FIXTURES / "virtual_account_gbp.json").read_text())

CID = "cus_034Abbx1XrOVaY6sXUBtGT"
VID = USD_ACCOUNT["id"]
OTHER_CID = "cus_target_group_entity"

WHITELIST_PATH = f"/v2/customers/{CID}/whitelist-recipients"

# A real ABA: 021000021 passes the checksum, which the engine enforces.
REGISTERED = {
    "id": "wlr_registered",
    "customerId": CID,
    "rail": "us",
    "accountNumber": "000123456789",
    "routingNumber": "021000021",
    "bic": None,
    "iban": None,
    "legalName": "ZZZTEST Globex Supplies LLC",
    "relationship": "group_entity",
    "status": "registered",
    "evidenceDocumentIds": [],
    "label": "Globex",
    "rejectionReason": None,
    "createdAt": "2026-08-01T00:00:00.000Z",
    "updatedAt": "2026-08-02T00:00:00.000Z",
}
PENDING = {
    **REGISTERED,
    "id": "wlr_pending",
    "status": "pending_review",
    "legalName": "ZZZTEST Pending Ltd",
}
REVOKED = {**REGISTERED, "id": "wlr_revoked", "status": "revoked", "legalName": "ZZZTEST Gone SA"}

PAYOUT = {
    "id": "txn_payout_1",
    "type": "withdrawal",
    "status": "pending",
    "stage": "under_review",
    "customerId": CID,
    "customerName": "ZZZTEST Console E2E EOOD",
    "purpose": "payment_for_goods_or_services",
    "swiftUetr": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "hasRfi": True,
    "rfiId": "rfi_9",
    "requiresUserSignature": False,
    "clientReferenceId": "not-a-uuid",
    "fees": [
        {"type": "fixed", "owner": "conduit", "assetAmount": {"code": "USD", "amount": "12.50"}}
    ],
    "markup": {"bps": 25, "assetAmount": {"code": "USD", "amount": "2.50"}},
    "remittance": {"reference": "INV-4471", "description": "August supplies"},
    "source": {
        "type": "virtual_account",
        "virtualAccountId": VID,
        "assetAmount": {"code": "USD", "amount": "1000.00"},
    },
    "destination": {
        "type": "external_bank",
        "assetAmount": {"code": "USD", "amount": "987.50"},
        "fedwireImad": "20260827MMQFMP0100001",
    },
    "createdAt": "2026-08-27T10:00:00.000Z",
}
DEPOSIT = {
    "id": "txn_deposit_1",
    "type": "deposit",
    "status": "completed",
    "customerId": CID,
    "hasRfi": False,
    "fees": [],
    "source": {
        "type": "external_bank_inbound",
        "assetAmount": {"code": "USD", "amount": "5000.00"},
        "achTraceNumber": "091000010000001",
        "sender": {"name": "ZZZTEST Payer Inc", "bic": "CITIUS33XXX", "country": "US"},
    },
    "destination": {
        "type": "virtual_account",
        "virtualAccountId": VID,
        "assetAmount": {"code": "USD", "amount": "5000.00"},
    },
    "createdAt": "2026-08-26T10:00:00.000Z",
    "completedAt": "2026-08-26T10:05:00.000Z",
}
# The money leg of a conversion — typed, and carrying the order
# that owns it.
CONVERSION = {
    "id": "txn_conv_1",
    "type": "fiat_conversion",
    "status": "completed",
    "customerId": CID,
    "hasRfi": False,
    "fees": [],
    "linkedOrderId": "ord_conv_1",
    "conversionRate": "0.9123",
    "source": {"type": "virtual_account", "assetAmount": {"code": "USD", "amount": "1000.00"}},
    "destination": {"type": "virtual_account", "assetAmount": {"code": "EUR", "amount": "912.30"}},
    "createdAt": "2026-08-25T10:00:00.000Z",
}

QUOTE = {
    "id": "qte_1",
    "lockSide": "source",
    "amount": {"code": "USD", "amount": "1000.00"},
    "sourceAssetRef": {"code": "USD"},
    "destinationAssetRef": {"code": "USD"},
    "createdAt": "2026-08-27T10:00:00.000Z",
    "expiresAt": "2099-01-01T00:00:00.000Z",
    "options": [
        {
            "id": "qopt_fedwire",
            "rail": "fedwire",
            "rate": {"endUserRate": "1.0", "referenceRate": "1.0", "totalSpreadBps": "0.00"},
            "fees": [
                {
                    "type": "fixed",
                    "charge": "payout",
                    "owner": "conduit",
                    "assetAmount": {"code": "USD", "amount": "20.00"},
                }
            ],
            "sourceAmount": {"code": "USD", "amount": "1000.00"},
            "destinationAmount": {"code": "USD", "amount": "980.00"},
            "recipientAmount": {"code": "USD", "amount": "980.00"},
            "totalDebit": {"code": "USD", "amount": "1020.00"},
        },
        {
            "id": "qopt_rtp",
            "rail": "rtp",
            "rate": {"endUserRate": "1.0", "referenceRate": "1.0", "totalSpreadBps": "0.00"},
            "fees": [
                {
                    "type": "fixed",
                    "charge": "payout",
                    "owner": "conduit",
                    "assetAmount": {"code": "USD", "amount": "1.00"},
                }
            ],
            "sourceAmount": {"code": "USD", "amount": "1000.00"},
            "destinationAmount": {"code": "USD", "amount": "999.00"},
            "recipientAmount": {"code": "USD", "amount": "999.00"},
            "totalDebit": {"code": "USD", "amount": "1001.00"},
        },
    ],
}
EXPIRED_QUOTE = {**QUOTE, "expiresAt": "2020-01-01T00:00:00.000Z"}

# --- conversions --------------------------------------------------------------

# The customer's second account, active, in the other currency — a conversion
# needs one to land in.
EUR_ACTIVE = {**EUR_ACCOUNT, "status": "active", "activatedAt": "2026-08-26T09:00:00.000Z"}
EUR_VID = EUR_ACTIVE["id"]

# `POST /v2/quotes` in **conversion** mode: differing assets, no
# `destinationCountry`, so `rate` is populated (unlike the same-asset withdrawal
# quote captured live, where every option's rate is null).
CONVERSION_QUOTE = {
    "id": "qte_conv_1",
    "lockSide": "source",
    "amount": {"code": "USD", "amount": "1000.00"},
    "sourceAssetRef": {"code": "USD"},
    "destinationAssetRef": {"code": "EUR"},
    "createdAt": "2026-08-28T10:00:00.000Z",
    "expiresAt": "2099-01-01T00:00:00.000Z",
    "options": [
        {
            "id": "qop_conv_a",
            # Live truth (2026-08-28): a conversion option carries **no** rail —
            # there is no payment rail in a fiat-to-fiat conversion, and the
            # panel renders the null as "—".
            "rail": None,
            "rate": {"endUserRate": "0.9123", "referenceRate": "0.9200", "totalSpreadBps": "84.00"},
            "fees": [
                {
                    "type": "fixed",
                    "charge": "conversion",
                    "owner": "conduit",
                    "assetAmount": {"code": "USD", "amount": "2.00"},
                }
            ],
            "sourceAmount": {"code": "USD", "amount": "1000.00"},
            "destinationAmount": {"code": "EUR", "amount": "912.30"},
            "recipientAmount": {"code": "EUR", "amount": "912.30"},
            "totalDebit": {"code": "USD", "amount": "1002.00"},
        }
    ],
}
EXPIRED_CONVERSION_QUOTE = {
    **CONVERSION_QUOTE,
    "expiresAt": "2020-01-01T00:00:00.000Z",
    "options": [{**CONVERSION_QUOTE["options"][0], "id": "qop_conv_old"}],
}

# The **real** `OrderExternalResponseDto` shape: each leg's amount lives inside
# its own asset block, and there is no `sourceAmount`/`destinationAmount` key —
# not in the pinned spec, and not on any live order (see
# `tests/fixtures/order_live_conversion_usd_eur.json`, captured 2026-08-29,
# which `test_conversions.py` asserts this fixture agrees with). Those two keys
# belong to a *quote option*, and this fixture used to carry them: the console
# read what nothing sends, so every real row's Out and In rendered as em-dashes
# while the suite stayed green.
ORDER = {
    "id": "ord_conv_1",
    "customerId": CID,
    "type": "fiat_conversion",
    "status": "pending",
    "source": {"type": "virtual_account", "id": VID},
    "destination": {"type": "virtual_account", "id": EUR_VID},
    "sourceAsset": {"code": "USD", "amount": "1000.00"},
    "destinationAsset": {"code": "EUR", "amount": "912.30"},
    "totalDebit": {"code": "USD", "amount": "1002.00"},
    "lockSide": "source",
    "lockExpiresAt": "2099-01-01T00:10:00.000Z",
    "rate": {"endUserRate": "0.9123", "referenceRate": "0.9200", "totalSpreadBps": "84.00"},
    "fees": [
        {
            "type": "fixed",
            "charge": "conversion",
            "owner": "conduit",
            "assetAmount": {"code": "USD", "amount": "2.00"},
        }
    ],
    "autoExecute": False,
    "quoteOptionId": "qop_conv_a",
    "linkedTransactionIds": ["txn_conv_1"],
    "createdAt": "2026-08-28T10:00:05.000Z",
}


def requirements_handler(default=None, per_purpose: dict | None = None, refuse=()) -> object:
    """A `GET /v2/payouts/requirements` stub that answers **per purpose**.

    A batch template reads all seven purposes for one corridor, so a stub keyed
    only on the path would answer every purpose identically and no test could
    tell a mixed file from a single-purpose one. `intercompany` defaults to the
    whitelist-gated fixture because that is what it is live; `refuse` names the
    purposes Conduit will not answer for this corridor, which the template has to
    survive.
    """
    fallback = FEDWIRE_BUSINESS if default is None else default
    # A `Response` default means "this is what discovery does on this corridor",
    # so it applies to every purpose — including `intercompany`, which otherwise
    # gets the gated fixture it has live. A payload default is the ordinary case
    # and leaves the gated purpose gated.
    per = {} if isinstance(fallback, httpx.Response) else {"intercompany": FEDWIRE_INTERCOMPANY}
    per |= per_purpose or {}

    def handler(request: httpx.Request) -> httpx.Response:
        purpose = request.url.params.get("purpose") or ""
        if purpose in refuse:
            return httpx.Response(
                422,
                json={"type": "UNSUPPORTED_PURPOSE", "title": f"{purpose} is not available"},
            )
        payload = per.get(purpose, fallback)
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    return handler


def page(items: list[dict], next_cursor: str | None = None) -> httpx.Response:
    meta: dict = {"total": len(items)}
    if next_cursor:
        meta["nextCursor"] = next_cursor
    return httpx.Response(200, json={"data": items, "meta": meta})


def encoded(fields: dict) -> bytes:
    """`web_harness.form()` mangles dots out of keyword names; these field names
    *are* dotted paths, so they are encoded straight from a dict."""
    items = []
    for name, value in fields.items():
        for one in value if isinstance(value, list) else [value]:
            items.append((name, str(one)))
    return str(httpx.QueryParams(items)).encode()


def recipient_form(**overrides: str) -> dict:
    """A valid `us` whitelist registration, as the browser would send it."""
    return {
        "rail": "us",
        "f.routingNumber": "021000021",
        "f.accountNumber": "000123456789",
        "f.relationship": "self",
        "f.legalName": "ZZZTEST Own Account LLC",
        # Conduit requires at least one, `self` registrations included — verified
        # live 2026-08-28 (`/evidenceDocumentIds: Too small`).
        "documentIds": "doc_evidence_1",
        **overrides,
    }


def payout_form(**overrides: str) -> dict:
    """A valid fedwire/business payout, as the browser would send it — every
    field discovery declares required, plus the route's own money fields."""
    body = {
        "purpose": "payment_for_goods_or_services",
        "rail": "fedwire",
        "recipientType": "business",
        "destinationCountry": "USA",
        "virtualAccountId": VID,
        "asset": "USD",
        "amount": "1000.00",
        "f.destination.type": "fiat",
        "f.destination.rail": "fedwire",
        "f.destination.recipient.accountNumber": "000123456789",
        "f.destination.recipient.routingNumber": "021000021",
        "f.destination.recipient.accountType": "CHECKING",
        "f.destination.recipient.type": "BUSINESS",
        "f.destination.recipient.legalName": "ZZZTEST Globex Supplies LLC",
        "f.destination.recipient.bankAddress.addressLine1": "270 Park Ave",
        "f.destination.recipient.bankAddress.city": "New York",
        "f.destination.recipient.bankAddress.country": "US",
        "f.destination.recipient.postalAddress.addressLine1": "500 Market St",
        "f.destination.recipient.postalAddress.city": "New York",
        "f.destination.recipient.postalAddress.country": "US",
        # `requiredWhen` on the fixture: a country outside the no-postcode list
        # makes this mandatory, and the US ZIP pre-check (§6.7) applies too.
        "f.destination.recipient.postalAddress.postalCode": "10010",
        "f.destination.ach.authorizationType": "corporate_agreement",
        "f.destination.remittance.reference": "INV-4471",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v != ""}


# An ACH route, where `destination.ach.authorizationType` is genuinely the
# rail's own field rather than the off-rail declaration the fedwire fixtures
# carry (see `payments._spurious_ach`).
ACH_INDIVIDUAL = json.loads((FIXTURES / "payout_requirements_ach_individual.json").read_text())
