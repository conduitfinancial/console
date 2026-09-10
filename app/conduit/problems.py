"""Conduit's error catalogue, said in this console's voice.

## Why this file exists

`ProblemDetailDto.detail` and `.resolution` are written for the *developer
integrating the API*: "Verify the customer ID. Check you are using the correct
API key for this organization." A treasury operator at a client organization —
the audience the Arca spec re-ranked this console for (§0.1) — was being shown
that, verbatim, about their own payment. It is the loudest "internal tool"
signal in the app and the spec's first blocking finding (§0.2).

So every problem Conduit answers with is translated **once**, here, keyed by the
machine-readable `type` code, and the vendor's own `detail` prose is never
rendered. The code itself stays on screen (`Problem.type` is untouched, and the
unknown shape prints it) because it is what a support conversation with Conduit
quotes — the same rule `failure_label` and the rejection macro already follow.

## The drift pin

`tests/test_failure_paths.py` reads the pinned spec (`contracts/`), pulls
every error code out of the 4xx/5xx response descriptions — the catalogue is
machine-extractable, each response documents its codes as `**CODE**: …` — and
asserts that every one of them is either in `TITLES` or explicitly listed in
`UNTRANSLATED`. A code Conduit adds is in neither, and fails the test rather
than reaching an operator as vendor prose.

`UNTRANSLATED` is not a loophole: those codes render the same safe shape an
unknown code does (`Conduit refused this: {code}`), and listing them is a
recorded decision that this console's flows cannot reach that feature. It is
there so that ~70 codes about custodial crypto wallets, passkey signing rosters
and prefunding — none of which this console operates — do not become ~70
invented sentences about screens that do not exist.
"""

from __future__ import annotations

import re

# What a Conduit error code is allowed to look like. Everything downstream —
# the title of a problem card, a `?err=` banner, a CSV cell — prints this
# string, and until A3's gate nothing checked it: RFC 7807 says `type` is a
# URI, so a body carrying `about:blank`, `https://vendor.example/errors#x?k=…`
# or a whole sentence would have been rendered as "Conduit refused this: …"
# and pushed into a URL. A code is SCREAMING_SNAKE or it is not a code.
CODE = re.compile(r"[A-Z][A-Z0-9_]{2,64}")

# code -> (title, resolution). Two short sentences, in the console's register:
# the title says what happened to the operator's thing, the resolution says what
# to do next. Neither quotes Conduit's copy; where a fact only Conduit knows is
# needed ("which chain", "which field"), the sentence points at support rather
# than inventing it.
TITLES: dict[str, tuple[str, str]] = {
    # --- the request itself, and this console's credentials ----------------------
    "API_KEY_MISSING": (
        "This console's Conduit credentials are missing",
        "Nothing was sent. Tell whoever operates this install — the API key is not reaching Conduit.",
    ),
    "API_KEY_INVALID": (
        "Conduit does not recognise this console's credentials",
        "Nothing was sent. Tell whoever operates this install — the API key has been revoked or replaced.",
    ),
    "API_KEY_READ_ONLY": (
        "This console's credentials cannot change anything",
        "Nothing was sent. Its Conduit key is read-only; whoever operates this install has to issue one that can write.",
    ),
    "FEATURE_NOT_ENABLED": (
        "Conduit has not enabled this for your organization",
        "Nothing was sent. Ask Conduit to enable it before trying again.",
    ),
    "RATE_LIMITED": (
        "Conduit is asking this console to slow down",
        "Nothing was lost. Wait a moment and try again.",
    ),
    "INTERNAL_ERROR": (
        "Conduit failed while handling this",
        "Whether it took effect is not yet known — check the record before retrying, and quote the correlation id to Conduit.",
    ),
    "SERVICE_UNAVAILABLE": (
        "Conduit is temporarily unavailable",
        "Nothing was recorded as done. Try again shortly.",
    ),
    "NOT_FOUND": (
        "Conduit has no record of this",
        "It may belong to another organization, or it may never have existed. Reload the page you came from.",
    ),
    "CONFLICT": (
        "Conduit refused this against the current state of the record",
        "Reload the record and read its state before trying again — it may already have moved.",
    ),
    "UNPROCESSABLE_ENTITY": (
        "Conduit accepted the shape of this but not its content",
        "Check the values on this screen against the record, then try again.",
    ),
    "VALIDATION_ERROR": (
        "Conduit refused some of the values on this form",
        "The fields it named are marked below. Correct them and submit again.",
    ),
    "MALFORMED_JSON": (
        "Conduit could not read what this console sent",
        "Nothing was applied. This is a fault in the console, not in what you typed — report it with the correlation id.",
    ),
    "UNSUPPORTED_MEDIA_TYPE": (
        "Conduit refused the format this console sent",
        "Nothing was applied. This is a fault in the console — report it with the correlation id.",
    ),
    "INVALID_OID_FORMAT": (
        "That is not a well-formed Conduit id",
        "Check the id you typed or followed; a Conduit id carries its own prefix, like cus_ or txn_.",
    ),
    "INVALID_CURSOR": (
        "This page of results has expired",
        "Go back to the first page of the list and page forward again.",
    ),
    "IDEMPOTENCY_KEY_REQUIRED": (
        "Conduit refused a submission that carried no duplicate guard",
        "Nothing was sent. This is a fault in the console — report it with the correlation id.",
    ),
    "IDEMPOTENCY_KEY_INVALID": (
        "Conduit refused this submission's duplicate guard",
        "Nothing was sent. This is a fault in the console — report it with the correlation id.",
    ),
    "IDEMPOTENCY_BODY_TOO_NESTED": (
        "Conduit refused this submission as too deeply structured to guard against duplicates",
        "Nothing was sent. This is a fault in the console — report it with the correlation id.",
    ),
    "IDEMPOTENCY_KEY_CONFLICT": (
        "This submission reuses a guard from a different request",
        "Reload the page so it mints a fresh one, then submit again — do not resend this form.",
    ),
    "IDEMPOTENCY_KEY_REQUEST_IN_PROGRESS": (
        "An identical submission is still running at Conduit",
        "Do not send it again. Wait, then reload the record to see how the first one ended.",
    ),
    # --- customers, onboarding, applications ------------------------------------
    "CUSTOMER_NOT_FOUND": (
        "Conduit has no such customer",
        "The id may belong to another organization. Pick the customer from the directory instead of typing an id.",
    ),
    "CUSTOMER_NOT_ONBOARDED": (
        "This customer has not finished onboarding",
        "Finish and submit their onboarding application first; nothing can move for them until Conduit approves it.",
    ),
    "CUSTOMER_ALREADY_ONBOARDED": (
        "An approved customer with this tax identifier already exists",
        "Nothing new was created. Use the existing customer — the id is named above.",
    ),
    "CUSTOMER_UPDATE_ALREADY_PENDING": (
        "This customer already has an update waiting at Conduit",
        "Only one can be in flight. Wait for the pending one to be decided before sending another.",
    ),
    "CUSTOMER_KYB_INCOMPLETE": (
        "This customer's business verification is not approved yet",
        "Wait for Conduit's decision on their application before trying this again.",
    ),
    "APPLICATION_NOT_FOUND": (
        "Conduit has no such application",
        "It may belong to another organization. Open it from the applications list instead of typing an id.",
    ),
    "APPLICATION_INVALID_STATUS": (
        "This application is not in a state that allows that",
        "Reload it and read its status — a decision may already have been made.",
    ),
    "ONBOARDING_NOT_READY": (
        "Conduit says this onboarding is still incomplete",
        "Something required is still missing or is not an accepted value. The fields it named are marked below.",
    ),
    "ONBOARDING_ALREADY_SUBMITTED": (
        "This onboarding has already been submitted",
        "Nothing was sent twice. Open the application to see where it stands.",
    ),
    "KYC_INQUIRY_NOT_FOUND": (
        "Conduit has no identity check on file for this person",
        "This application may not have raised one. Open the application and check the person's row.",
    ),
    "KYC_INQUIRY_LINK_UNAVAILABLE": (
        "No further identity-check link can be issued for this person",
        "Their check has already settled or can no longer be resumed. Open the application to see where it stands.",
    ),
    "KYC_INQUIRY_LINK_RATE_LIMITED": (
        "A link for this person is already being issued",
        "Wait a moment and reload rather than pressing again — a second press issues nothing.",
    ),
    "KYC_UPSTREAM_UNAVAILABLE": (
        "Conduit's identity-check provider did not answer",
        "The check itself is unharmed. Try again shortly.",
    ),
    # --- documents ---------------------------------------------------------------
    "DOCUMENT_NOT_FOUND": (
        "Conduit has no such document",
        "It may belong to another organization. Upload the file again rather than reusing the id.",
    ),
    "DOCUMENT_IDS_NOT_FOUND": (
        "Conduit does not hold one of the attached documents for this customer",
        "Remove the attachments and upload them again on this screen.",
    ),
    "FILE_TOO_LARGE": (
        "That file is above Conduit's 10 MB limit",
        "Nothing was uploaded. Send a smaller file, or split it.",
    ),
    "UNSUPPORTED_FILE_TYPE": (
        "Conduit does not accept that kind of file",
        "Nothing was uploaded. It reads the file's contents, not its name — send a PDF, JPEG or PNG.",
    ),
    "INVALID_FILE_NAME": (
        "Conduit refused that file's name",
        "Nothing was uploaded. Rename it to plain letters, digits, dots and dashes, then upload again.",
    ),
    "DOCUMENTATION_REQUIRED": (
        "This payment needs a supporting document",
        "Nothing was sent. Attach the document this route requires and submit again.",
    ),
    "POLICY_EVALUATION_UNAVAILABLE": (
        "Conduit could not check its documentation policy",
        "No transaction was created. Try again shortly.",
    ),
    # --- accounts and virtual accounts -------------------------------------------
    "VIRTUAL_ACCOUNT_NOT_FOUND": (
        "Conduit has no such account for this customer",
        "Reload the customer's accounts — the one you picked may have been closed.",
    ),
    "NO_ELIGIBLE_PROVIDER": (
        "No banking provider can hold this currency for this customer",
        "Nothing was requested. Conduit decides this from the customer's country and industry — ask Conduit before trying another currency.",
    ),
    "FEATURE_ALREADY_EXISTS": (
        "This customer already has that feature",
        "Nothing new was requested. Open the customer to see what they already hold.",
    ),
    # --- whitelist recipients and contacts ---------------------------------------
    "WHITELIST_RECIPIENT_NOT_FOUND": (
        "Conduit has no such registered recipient",
        "Reload the customer's registered recipients — it may have been revoked.",
    ),
    "WHITELIST_RECIPIENT_CONFLICT": (
        "These bank details are already registered with different particulars",
        "Nothing new was registered. Open the existing entry rather than registering a second one.",
    ),
    "WHITELIST_INVALID_TRANSITION": (
        "This registration is not in a state that allows that",
        "Reload the list and read its status — Conduit may already have decided it.",
    ),
    "RECIPIENT_NOT_WHITELISTED": (
        "This route only pays a registered recipient",
        "Nothing was sent. Pick one of the customer's registered recipients, or register this destination first.",
    ),
    "RECIPIENT_VALIDATION_FAILED": (
        "Conduit refused these destination details for this route",
        "Nothing was sent. Check the coordinates against the account they belong to, then submit again.",
    ),
    # --- payouts and transactions -------------------------------------------------
    "PAYOUT_NOT_FOUND": (
        "Conduit has no such payout",
        "It may belong to another organization. Open it from the ledger instead of typing an id.",
    ),
    "PAYOUT_NOT_CANCELLABLE": (
        "This payout can no longer be cancelled",
        "It has finished, or its money has already started moving. Reload it and read its state.",
    ),
    "PAYOUT_CANCEL_IN_PROGRESS": (
        "This payout was only just created and cannot be cancelled yet",
        "It is still cancellable. Wait a moment, reload, and cancel again.",
    ),
    "PAYOUT_DESTINATION_CURRENCY_MISMATCH": (
        "The destination account holds a different currency",
        "Nothing was sent. A payout moves one currency and never converts — fund it from an account in the same currency.",
    ),
    "PAYOUT_RAIL_CURRENCY_MISMATCH": (
        "This rail does not settle the currency being sent",
        "Nothing was sent. A payout moves one currency and never converts — pick a rail that settles it.",
    ),
    # Conduit answers this as a 503, not a 4xx: nothing on the form is wrong.
    "RAIL_UNAVAILABLE": (
        "Conduit could not route this payment",
        "Nothing was sent. Try again shortly — and if it keeps failing, ask Conduit, because the route may not be open at all.",
    ),
    "PAYOUT_DESTINATION_RESTRICTED": (
        "Conduit has restricted the destination from receiving funds",
        "Nothing was sent. Ask Conduit about the destination before trying again.",
    ),
    "PAYOUT_SELF_DESTINATION": (
        "This payout's destination is the account it is funded from",
        "Nothing was sent, and nothing would have moved. Pick a different destination.",
    ),
    "PAYOUT_QUEUE_FULL": (
        "Too many payouts are already waiting for signatures on this account",
        "Nothing was sent. They are signed one at a time — wait for the queue to clear.",
    ),
    "INSUFFICIENT_FUNDS": (
        "The funding account does not hold enough",
        "Nothing was sent. The balance must cover the amount and the fee — fund the account or lower the amount.",
    ),
    "AMOUNT_OUT_OF_RANGE": (
        "That amount is outside what this route accepts",
        "Nothing was sent. It is below the minimum, above the maximum, or too small to survive rounding.",
    ),
    "TRANSACTION_BLOCKED": (
        "Conduit's policy blocked this payment",
        "The decision is final and nothing moved. Ask Conduit before attempting it another way.",
    ),
    "DUPLICATE_CLIENT_REFERENCE_ID": (
        "This reference is already used by another record",
        "Nothing new was created. A reference is unique per record — use a different one.",
    ),
    "DESTINATION_NOT_ACTIVATED": (
        "The destination account is not open on its chain yet",
        "Nothing was sent. It has to be activated before it can receive; quote the correlation id to Conduit for which chain.",
    ),
    "DESTINATION_ASSET_NOT_ACCEPTED": (
        "The destination cannot receive this asset right now",
        "Nothing was sent. Ask the destination's owner whether it accepts this asset, then try again.",
    ),
    "TRANSACTION_NOT_FOUND": (
        "Conduit has no such transaction",
        "It may belong to another organization. Open it from the ledger instead of typing an id.",
    ),
    # --- conversions, quotes, orders ----------------------------------------------
    "ORDER_NOT_FOUND": (
        "Conduit has no such conversion order",
        "It may have expired. Open it from the orders list instead of typing an id.",
    ),
    "ORDER_EXPIRED": (
        "This order has expired",
        "Nothing was executed. Start a new conversion to get a fresh price.",
    ),
    "ORDER_NOT_CANCELLABLE": (
        "This order can no longer be cancelled",
        "It has finished, or its money has already started moving. Reload it and read its state.",
    ),
    # Deliberately not the reassurance `PAYOUT_CANCEL_IN_PROGRESS` gives: Conduit
    # says this one is transient but *not* a promise the order reaches
    # `cancelled` — an uncertain source transfer can still settle or be refused.
    "ORDER_CANCEL_IN_PROGRESS": (
        "This order's cancellation has not been answered yet",
        "The cancellation is still in flight and may not win. Reload the order and read its state before acting again.",
    ),
    "ORDER_NOT_EXECUTABLE": (
        "This order is no longer waiting to be executed",
        "Reload it — it has already been executed, or it failed.",
    ),
    "QUOTE_NOT_FOUND": (
        "Conduit has no such quote",
        "Prices are short-lived. Ask for a new quote on this screen.",
    ),
    "QUOTE_EXPIRED": (
        "This price has expired",
        "Nothing was executed. Ask for a new quote and check the amount before executing.",
    ),
    "QUOTE_ALREADY_USED": (
        "This price has already been used by another order",
        "Nothing was executed. A quote is good for one order — ask for a new one.",
    ),
    "QUOTE_ASSET_MISMATCH": (
        "This price was not quoted for the currencies on this order",
        "Nothing was executed. Ask for a new quote for the pair you are converting.",
    ),
    "QUOTE_RECIPIENT_TYPE_UNPRICED": (
        "This price does not hold for this recipient",
        "Nothing was executed. Conduit prices some recipients differently — ask for a new quote with the recipient chosen.",
    ),
    "UNSUPPORTED_PAIR": (
        "Conduit does not convert between these two currencies",
        "Nothing was sent. Pick a pair Conduit supports.",
    ),
    "UNSUPPORTED_ASSET": (
        "Conduit does not support that asset here",
        "Nothing was sent. Pick one of the assets offered on this screen.",
    ),
    "RATE_UNAVAILABLE": (
        "No live rate is available for this pair",
        "Nothing was priced. Try again shortly.",
    ),
    "RATE_UNAVAILABLE_AFTER_HOURS": (
        "This market is closed, so no price can be fixed",
        "Nothing was priced. Try again when the market for this pair reopens.",
    ),
    "INVALID_ORDER_COMBO": (
        "Conduit does not support this combination of source and destination",
        "Nothing was created. Change one side of the conversion and try again.",
    ),
    "MISSING_REQUIRED_FIELDS": (
        "Something this destination requires was not filled in",
        "Nothing was created. The fields Conduit named are marked below.",
    ),
    "DEPOSIT_ADDRESS_UNAVAILABLE": (
        "This customer's funding address is still being prepared",
        "Nothing was created and no money moved. Try again shortly.",
    ),
    # --- RFIs and webhooks ----------------------------------------------------------
    "RFI_NOT_FOUND": (
        "Conduit has no such request for information",
        "It may belong to another organization, or it may not be published yet. Open it from the RFIs list.",
    ),
    "RFI_NOT_OPEN_FOR_RESPONSE": (
        "This request is not open for a response",
        "Nothing was sent. The round may already be answered, or the request resolved — reload it.",
    ),
    "WEBHOOK_ENDPOINT_NOT_FOUND": (
        "Conduit has no such webhook endpoint",
        "Whoever operates this install should re-register it with Conduit.",
    ),
}


# Documented by Conduit, unreachable from this console's flows: custodial and
# non-custodial crypto wallets, passkey / machine-key signing rosters and their
# ceremonies, prefunding, registered addresses, deposit sender information,
# client markup, and per-delivery webhook replays. This console operates none of
# those surfaces — several of these codes are listed on endpoints it *does* call
# (`/payouts`, `/transactions`) but only for wallet-signed flows it never
# creates. If one ever arrives it renders as `Conduit refused this: {code}` with
# Conduit's resolution line, which is exactly what support quotes.
#
# Adding a row to TITLES for one of these is a deliberate promotion, not a
# cleanup: it claims a screen exists where an operator could act on it.
UNTRANSLATED: frozenset[str] = frozenset(
    {
        # crypto wallets, custody, claims
        "CHAIN_ACTIVATION_UNAVAILABLE",
        "CLAIM_NOT_FOUND",
        "CRYPTO_FEATURE_NOT_APPROVED",
        "CRYPTO_NOT_AVAILABLE_IN_JURISDICTION",
        "CRYPTO_WALLET_CONSENSUS_REQUIRED",
        "CUSTOMER_ALREADY_CUSTODIAL",
        "CUSTOMER_ALREADY_NON_CUSTODIAL",
        "INVALID_ADDRESS_FORMAT",
        "INVALID_TRANSFER_PAIR",
        "NON_CUSTODIAL_SOURCE_NOT_SUPPORTED",
        "PROVIDER_ACCOUNT_NOT_FOUND",
        "WALLET_ACCOUNT_NOT_READY",
        "WALLET_AWAITING_ADMIN_APPROVAL",
        "WALLET_CLIENT_REFERENCE_IMMUTABLE",
        "WALLET_CUSTODY_NOT_CLAIMED",
        "WALLET_NOT_ACTIVE",
        "WALLET_NOT_FOUND",
        "WALLET_NOT_ROTATABLE",
        "WALLET_NO_PROVIDER_ACCOUNT",
        "WALLET_PROVIDER_CONFLICT",
        "WALLET_PROVISIONING_REJECTED",
        "WALLET_ROTATION_BLOCKED_BALANCE",
        "WALLET_ROTATION_IN_PROGRESS",
        # signer rosters, quorums, stamps, ceremonies
        "API_KEY_CREDENTIAL_NOT_SUPPORTED",
        "API_KEY_PUBLIC_KEY_DUPLICATE",
        "API_KEY_PUBLIC_KEY_INVALID",
        "API_KEY_PUBLIC_KEY_IN_USE",
        "API_KEY_PUBLIC_KEY_REQUIRED",
        "CEREMONY_IN_FLIGHT",
        "DEMOTE_TARGET_NOT_IN_ROOT",
        "PROGRAMMATIC_QUORUM_UNREACHABLE",
        "QUORUM_THRESHOLD_EXCEEDS_SIGNERS",
        "QUORUM_WALLET_OVERRIDE_CANNOT_RAISE",
        "ROOT_AT_CAPACITY_SWAP_REQUIRED",
        "SIGNATURE_REJECTED",
        "SIGNERS_NOT_SUPPORTED",
        "SIGNER_ALREADY_ADMIN",
        "SIGNER_IS_ROOT_MEMBER",
        "SIGNER_NOT_ACTIVE",
        "SIGNER_NOT_ADMIN",
        "SIGNER_NOT_ENROLLABLE",
        "SIGNER_NOT_FOUND",
        "SIGNER_NO_PENDING_ADMIN_APPROVAL",
        "SIGNER_PASSKEY_COUNT_TOO_LOW",
        "SIGNER_PENDING_ADMIN_APPROVAL",
        "SIGNER_REMOVAL_FORBIDDEN",
        "SIGNING_LINK_NOT_HUMAN_SIGNABLE",
        "SIGNING_MODE_NOT_PROGRAMMATIC",
        "SIGNING_MODE_ROSTER_INVALID",
        "SIGNING_PROVIDER_UNAVAILABLE",
        "SIGNING_REQUEST_NOT_FOUND",
        "SIGNING_STAMP_ATTRIBUTION_MISMATCH",
        "SIGNING_STAMP_INVALID",
        "SIGNING_STAMP_SIGNER_UNKNOWN",
        "SWAP_WOULD_BREAK_F12",
        "TRANSACTION_NOT_AWAITING_SIGNATURE",
        "WOULD_BREAK_MIN_ADMINS",
        # prefunding (house accounts), registered addresses, deposit sender info
        "PREFUNDING_AMBIGUOUS_MATCH",
        "PREFUNDING_DESTINATION_NOT_FOUND",
        "PREFUNDING_SOURCE_NOT_HOUSE_ACCOUNT",
        "PREFUNDING_UNSUPPORTED_RAIL",
        "REGISTERED_ADDRESS_COMPLIANCE_REJECTED",
        "REGISTERED_ADDRESS_NOT_FOUND",
        "REGISTERED_ADDRESS_SUSPENDED",
        "SENDER_INFO_ALREADY_RECORDED",
        "SENDER_INFO_NOT_A_DEPOSIT",
        "SENDER_INFO_SUBMISSION_IN_FLIGHT",
        "UNREGISTERED_ADDRESS_NOT_AWAITING",
        # client markup on conversions (not enabled for this console's key)
        "CLIENT_MARKUP_EXCEEDS_PAYOUT",
        "CLIENT_MARKUP_NOT_ENABLED",
        "CLIENT_MARKUP_SPREAD_EXCEEDED",
        # cancelling an onboarding application — `POST /applications/{id}/cancel`
        # is a route this console never calls: it submits applications and reads
        # them, and withdrawing one is not a screen it offers.
        "CANCEL_IN_PROGRESS",
        # per-delivery webhook replay (this console registers endpoints only)
        "WEBHOOK_DELIVERY_NOT_FOUND",
        "WEBHOOK_DELIVERY_NOT_RETRYABLE",
        "VERIFICATION_TYPE_NOT_REISSUABLE",
    }
)


# Codes this console MINTS ITSELF and then stores on `operations.error`, so the
# replay path (`web.problem_of`) meets them coming back out of the ledger. They
# are not Conduit's and must never be rendered as "Conduit refused this" — the
# reconciler observing a terminal state is not a refusal. Kept out of `TITLES`
# so the drift pin can hold `TITLES | UNTRANSLATED == the spec's catalogue`
# exactly; the drift pin asserts these are NOT catalogue codes, which is what
# makes minting one Conduit later defines a test failure rather than a collision.
MINTED_HERE: dict[str, tuple[str, str]] = {
    # `reconciliation.service._observed` — the resource reached a terminal state
    # on its own; the words are that Reject's own, unchanged.
    "RESOURCE_NOT_ACTIONABLE": (
        "Too late to apply",
        "No action needed — the resource reached a terminal state on its own.",
    ),
}


# What an operator is told when Conduit answers with something this console has
# no sentence for. The CODE is on screen deliberately: it is the one token
# support can act on, and inventing prose for a refusal we do not understand is
# the failure mode this whole file exists to prevent.
UNKNOWN_TITLE = "Conduit refused this: {code}"

# …and when the body carried no code at all — a gateway's HTML page, a truncated
# body, anything in front of Conduit. Naming a code we did not receive would be
# a lie, and the status is the only fact there is.
NO_CODE_TITLE = "Conduit's answer could not be read (HTTP {status})"
NO_CODE_TITLE_NO_STATUS = "Conduit's answer could not be read"
NO_CODE_RESOLUTION = (
    "Whether it took effect is not yet known — check the record before retrying."
)


def code_of(raw: object) -> str:
    """The machine code from a problem body's `type`, or `"UNKNOWN"`.

    One validator for both callers — `parse_problem` (live) and `web.problem_of`
    (replayed from the ledger) — because the shape of what gets printed cannot
    depend on which door the body came through.
    """
    text = raw if isinstance(raw, str) else ""
    return text if CODE.fullmatch(text) else "UNKNOWN"


def console_words(code: str, status: int, resolution: str = "") -> tuple[str, str]:
    """`(title, resolution)` for one problem, in this console's voice.

    The single translation. `code` is Conduit's `type`; `resolution` is Conduit's
    own resolution line, used **only** on the unknown path, where it is the one
    remaining piece of guidance and is at least action-shaped. Conduit's `detail`
    is never returned by this function and never rendered — it is the developer
    prose the Arca audit found (spec §0.2), and it survives verbatim only in
    `Problem.raw`, which is evidence for the operations ledger, not display.
    """
    if code in TITLES:
        return TITLES[code]
    if code in MINTED_HERE:
        return MINTED_HERE[code]
    if not code or code == "UNKNOWN":
        # No status either — a replayed ledger snapshot that recorded none. The
        # status is a fact, so it is printed when there is one and not invented
        # when there is not.
        title = (
            NO_CODE_TITLE.format(status=status) if status else NO_CODE_TITLE_NO_STATUS
        )
        return title, NO_CODE_RESOLUTION
    return UNKNOWN_TITLE.format(code=code), resolution
