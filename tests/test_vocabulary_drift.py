"""Every status vocabulary this console renders, pinned against the spec it came from.

## Why this file exists

`PILL_TONES` had no `orders` entry at all, so for a long time every
conversion order rendered "Unknown: succeeded" — through four review rounds
and 1123 green tests. Nothing caught it because every Unknown-assertion in the
suite used a *fabricated* status: the tests proved the fallback works, never that
the vocabulary is complete. Two per-surface guards were added afterwards (orders
in `test_web_convert.py`, rfis in `test_web_rfis.py`) and the other four kinds
were left unpinned — correct today, unguarded tomorrow.

The standing rule, applied to the artifact that taught it: **evidence needs its
own drift pins.** A display vocabulary is a
claim about what Conduit can send. Three pins make that claim keep itself honest:

1. **Completeness** — the map and the spec must agree in BOTH directions, so a
   re-pin that adds a status fails here rather than reaching an operator as
   "Unknown", and a stale entry we no longer need is visible too.
2. **Loud on rename** — the DTO names below are resolved from the spec and the
   resolution ASSERTS it found them. A renamed DTO fails loudly instead of
   silently comparing against an empty set, which would pass while checking
   nothing.
3. **Non-vacuity** — every resolved vocabulary is asserted non-empty, for the
   same reason.

The per-surface tests remain: they prove a status RENDERS correctly. This file
proves the vocabulary is COMPLETE. Both are needed — the six-phase bug passed
rendering tests for statuses it knew and never saw the one it didn't.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import payments
from app.web import PILL_TONES

SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "openapi_production.json").read_text()
)["components"]["schemas"]

# kind -> the DTOs Conduit answers with on the surfaces that render that kind.
# A kind backed by several DTOs takes their UNION: the ledger renders every
# transaction shape through one vocabulary, so a divergence in any one of them is
# a hole in that vocabulary, not someone else's problem.
SOURCES: dict[str, tuple[str, ...]] = {
    "applications": (
        "CustomerOnboardingApplicationDto",
        "CustomerUpdateApplicationDto",
        "VirtualAccountFeatureApplicationDto",
        "CryptoWalletFeatureApplicationDto",
    ),
    "transactions": (
        "PublicWithdrawalViewDto",
        "PublicDepositViewDto",
        "PublicDepositReturnViewDto",
        "PublicOnrampViewDto",
        "PublicOfframpViewDto",
        "PublicFiatConversionViewDto",
        "PublicCryptoConversionViewDto",
    ),
    "orders": ("OrderExternalResponseDto",),
    "virtual_accounts": ("VirtualAccountResponseClass",),
    "whitelist_recipients": ("WhitelistRecipientResponseDto",),
    "rfis": ("ClientRfiDetailDto",),
}


def spec_statuses(dto: str) -> set[str]:
    """The `status` enum of one DTO — or a failure naming it.

    The loud-on-rename pin. `SPEC.get(dto, {})` would return an empty set for a
    DTO Conduit renamed, and an empty set compares equal to nothing useful while
    still letting a poorly written assertion pass.
    """
    schema = SPEC.get(dto)
    assert schema is not None, (
        f"{dto} is not in the pinned spec — Conduit renamed or removed it. This file's "
        "vocabulary check is only as good as these names: repoint it, do not delete the row."
    )
    enum = schema.get("properties", {}).get("status", {}).get("enum")
    assert isinstance(enum, list) and enum, f"{dto}.status is no longer an inline enum"
    return set(enum)


@pytest.mark.parametrize("kind", sorted(SOURCES))
def test_the_rendered_vocabulary_matches_the_spec_exactly(kind: str):
    """Both directions. Spec-only is the six-phase bug (a real status rendering as
    "Unknown"); map-only is a status this console believes in and Conduit does not
    — a label nobody will ever see, which is how a vocabulary rots quietly.
    """
    declared = set(PILL_TONES[kind])
    published: set[str] = set()
    for dto in SOURCES[kind]:
        published |= spec_statuses(dto)

    assert published, f"resolved no statuses for {kind} — the check would be vacuous"
    assert declared, f"PILL_TONES[{kind!r}] is empty"

    missing = published - declared
    extra = declared - published
    assert not missing, (
        f"PILL_TONES[{kind!r}] does not know {sorted(missing)} — Conduit publishes them, so an "
        f"operator sees 'Unknown: <status>'. Add them with a tone, do not widen this test."
    )
    assert not extra, (
        f"PILL_TONES[{kind!r}] declares {sorted(extra)}, which {SOURCES[kind]} no longer "
        "publish. Confirm against the live spec before deleting — a status Conduit dropped is "
        "still worth rendering for historical rows."
    )


def test_no_next_step_sentence_names_a_state_conduit_cannot_send():
    """The ledger's "what happens next" sentences.

    Same failure shape as the one this file exists for, one layer up: a
    sentence keyed on a status Conduit never sends is copy nobody will ever
    read, and a *renamed* status silently drops the sentence for a state that
    still occurs. `PILL_TONES["transactions"]` is already pinned against the
    spec above, so pinning to it inherits that.

    Subset, not equality: `completed` deliberately has no sentence — nothing
    happens next on a payment that landed — and the console will not invent one
    to satisfy a test.
    """
    unknown = set(payments.TRANSACTION_NEXT_STEP) - set(PILL_TONES["transactions"])
    assert not unknown, (
        f"TRANSACTION_NEXT_STEP answers for {sorted(unknown)}, which is not a transaction "
        "status this console renders. Never invent a state to hang a sentence on."
    )
    # And the keys are statuses, not stages: `STAGE_LABELS` is a different
    # vocabulary answering a different question, and the template picks the
    # stage over the sentence when Conduit named one.
    assert not set(payments.TRANSACTION_NEXT_STEP) & set(payments.STAGE_LABELS)


def test_every_kind_this_console_renders_is_pinned_here():
    """The completeness pin on the pin itself.

    A future slice that adds a `PILL_TONES` kind must add its source DTOs here
    too, or this fails — otherwise the new vocabulary is exactly as unguarded as
    `orders` was, and this file's existence would imply otherwise.

    Console-owned kinds are excluded BY NAME with their reason: their vocabulary
    is this application's own state machine, not Conduit's, so there is no spec
    enum to compare against (they are pinned by their own tests against the
    constants they come from).
    """
    console_owned = {
        "operations",  # app.models.OPERATION_STATES — this console's ledger
        "payout_batches",  # app.batches.LEGAL_STATUS — this console's batch lifecycle
        "payout_batch_rows",  # app.batches.ROW_STATES — likewise
    }
    unpinned = set(PILL_TONES) - set(SOURCES) - console_owned
    assert not unpinned, (
        f"{sorted(unpinned)} render a status vocabulary that nothing pins against the spec. "
        "Add the DTOs to SOURCES, or add the kind to `console_owned` with the constant it "
        "comes from."
    )
