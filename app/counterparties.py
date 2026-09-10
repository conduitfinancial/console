"""Reusable counterparties — this console's own address book.

Route-free like `app/payments/`: `app/web/contacts.py` renders what this module
shapes, `app/web/payouts.py` saves and prefills through it, and
`tests/e2e/07_counterparties.py` drives the same functions.

**Operator language is "contact"**. The table, this module
and every identifier in it keep the name they were written under — renaming a
stored `counterparty.save` audit action would falsify recorded history, and a
column rename buys nothing an operator can see. What changed is the surface: one
Contacts page shows a saved record and its whitelist twin as two *capabilities*
of one row, matched by coordinates (`merge`).

---

## The design decision, and the spec evidence behind it

Whether "save this recipient" is Conduit's whitelist surfaced better, or a
console-side record, is **decided from the API, not assumed**. The answer is
*both*, split by route, and the split is forced by the API:

1. **`whitelist-recipients` is Conduit's only server-side recipient store.**
   The pinned spec (`contracts/openapi_production.json`, 67 paths) has exactly
   four recipient-shaped operations, all under
   `/customers/{customerId}/whitelist-recipients[/{id}]`, all tagged
   *Whitelist Recipients*. An exhaustive scan of every path, schema name, tag
   and operation body for `recipient|counterpart|benefic|contact|payee|
   address.?book` finds no other store. The **live sandbox** spec
   (`https://api.sandbox.conduit.financial/v2/api-docs/openapi.json`, 92 paths,
   read-only fetch 2026-08-30) has the same tag list plus `Sandbox`, and all 25
   of its live-only paths are `/sandbox/*` simulators. Nothing has been added
   since the pin.

2. **That store is the intercompany gate, not an address book.** `POST
   /customers/{customerId}/whitelist-recipients` says so itself: *"Registers a
   bank recipient as an intercompany counterparty for this customer… Only
   registered entries satisfy purpose=intercompany payouts."* Registration is
   compliance-reviewed (`pending_review` → `registered`), needs at least one
   evidence document, and 409s on a conflicting re-registration.

3. **A payout cannot reference a stored recipient.** `FiatPayoutDto`'s
   properties are `assetAmount, clientReferenceId, customerId, destination,
   documents, markupAmount, markupBps, purpose, virtualAccountId` — and
   `destination.recipient` is the full inline object in every branch of its
   `oneOf`. There is no `recipientId`, no `whitelistRecipientId`, nothing to
   point at a saved record.

So:

* **Whitelist-gated routes already have saved counterparties** — they are the
  registered entries, and the payout form picks from them
  (`payments.resolve_recipient`, `payments.apply_recipient`). Nothing is built
  for those here; only the surface language changed.
* **Free-form routes have no server-side save at all.** goods/services, payroll,
  treasury_management, investments, prefunding, other — every one of them
  retypes an account number, a routing number, a bank address and two postal
  addresses per payment. That is what this module fixes, console-side.

## What a counterparty is, exactly

The `destination.recipient` subtree of a payout Conduit accepted — nothing more.
Not `destination.rail`/`type` (the route's), not `destination.remittance` (the
*payment's* — an invoice reference is not a property of who is being paid), not
the amount, not the funding account.

**It is never sent to Conduit as a record.** At use its fields are written back
onto the form and travel inside `FiatPayoutDto` like any typed value. Conduit
never learns this table exists (OPERATIONS_SPEC §1).

**Saving is not a Conduit mutation**, so it is not an operations-ledger row: a
double-clicked save cannot create a duplicate payment, cannot leave an outcome
unknown, and has no idempotency story to get wrong. It is a plain audited write
— the exception is documented alongside the others in `app/web/__init__.py`.

## Two semantics worth stating

**Prefill, not lock.** A picked counterparty writes its values into the form and
stops there. Every field stays editable, and `payments.payout_errors` validates
what was *submitted*, exactly as for a hand-typed destination. This is the
opposite of the whitelist gate, where the coordinates are overwritten
server-side and unconditionally — and deliberately so: the gate exists because
Conduit will only pay a registered destination, while a saved counterparty is a
convenience with no authority behind it.

**Saving under a label you already used updates that counterparty.** The
alternative — always insert — is one line shorter and fills the picker with
seven rows called "Globex" that an operator cannot tell apart, which is worse
than the thing it avoids. A partial unique index on `(customer_id,
lower(label))` over the live rows makes the update atomic, and an *archived* row
never blocks a new one of the same name.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import LargeBinary, and_, func, or_, select, text, type_coerce
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import read_json
from app.models import AuditEvent, Counterparty, Operation

# The subtree a counterparty *is*. One name, so the saver and the prefiller
# cannot drift apart about what was stored.
RECIPIENT_PATH = ("destination", "recipient")

LABEL_MAX = 128  # matches the column, and `operations.reference`'s bound

# The coordinate keys a list may never print in full. Bank identifiers
# (`routingNumber`, `bic`, `bankName`) are public routing data and are not here;
# these two identify an *account*, and four digits is all an operator needs to
# tell two of them apart.
MASKED_KEYS = ("accountNumber", "iban")
KEEP = 4


def mask(value: object) -> str:
    """`000123456789` → `••••6789`. Never the full coordinate, anywhere.

    A value with nothing to keep (four characters or fewer, including the empty
    string) is masked whole rather than shown: the point is that the list cannot
    be read as a source of account numbers, and a short one would be.
    """
    text_value = "" if value is None else str(value)
    return "•" * KEEP + text_value[-KEEP:] if len(text_value) > KEEP else "•" * KEEP


def coordinates(recipient: Mapping | None) -> list[str]:
    """The masked coordinates of one counterparty, for a list cell. Empty when
    the payload states none — never a guess, never a full number."""
    return [mask(recipient[key]) for key in MASKED_KEYS if (recipient or {}).get(key)]


# What makes two destinations the *same* destination: where the money lands and
# whose name is on it. Deliberately not the whole subtree — a corrected postal
# line or a phone number is the same payee, and comparing every key would call
# an honest prefill "modified" on a whitespace difference.
IDENTITY_KEYS = ("accountNumber", "iban", "bic", "routingNumber", "legalName")


def same_destination(submitted: Mapping | None, stored: Mapping | None) -> bool:
    """Whether a submitted recipient is still the stored one.

    The evidence test behind `counterparty.used`: the hidden id
    on the form says which record was *offered*, and a prefill is editable by
    design, so only the submitted coordinates can say where the payment actually
    went. An unreadable stored row (`recipient` is None) can prove nothing and
    is never a match.
    """
    if not stored:
        return False
    return all(
        str((submitted or {}).get(key) or "").strip() == str(stored.get(key) or "").strip()
        for key in IDENTITY_KEYS
    )


# The keys that *prove* two destinations are one account rather than merely
# agreeing about a name. `same_destination` compares `legalName` too — two
# different accounts at one company must not merge — but a pair that shares only
# a name has established nothing, and a pair that shares only empty strings has
# established less than that.
PROOF_KEYS = MASKED_KEYS + ("bic", "routingNumber")


def same_entity(entry: Mapping | None, recipient: Mapping | None, family: str = "") -> bool:
    """Whether a registered whitelist entry and a saved recipient are the same
    destination — **the transfer gate's own test, not a second one**.

    `same_destination` is what `app/web/payouts.py` already uses to decide
    whether a payout went where its prefill said it would; the Contacts page asks
    the identical question of a different pair, so it calls the identical
    function. A near-miss — one digit, a different legal name — is not a match
    here for exactly the reason it is not one there: the console asserts identity
    only where the coordinates prove it, and two rows are the honest answer when
    they do not.

    `family` is the rail check on the way through, the same one
    `payments.RAILS_FOR` makes for a payout: a whitelist entry's `rail` and a
    saved counterparty's `rail_family` speak the same three-word vocabulary
    (`us`/`sepa`/`swift`), and an entry on another rail is another destination
    whatever its digits say. Empty means "do not check" — for a saved row whose
    family this build has never seen, which is offered to nothing anyway.
    """
    if not entry or not recipient:
        return False
    if family and str(entry.get("rail") or "") != family:
        return False
    # Nothing to prove identity *with* is not a match: a pair of rows that state
    # no account and no bank would otherwise merge on a shared legal name.
    if not any(str((recipient or {}).get(key) or "").strip() for key in PROOF_KEYS):
        return False
    return same_destination(entry, recipient)


def _contact(saved: Mapping | None, entry: Mapping | None, *, ambiguous: int = 0) -> dict:
    """One Contacts row: what it is called, where it lands, and which of the two
    stores know about it. The capabilities themselves are read off `saved` and
    `entry` by the template — this shapes the facts, it does not decide what the
    row is allowed to claim."""
    recipient = (saved or {}).get("recipient")
    unreadable = saved is not None and recipient is None
    # A deleted contact's coordinates were destroyed on purpose, which is neither
    # "unreadable" nor "none": the row is a shell kept so the payments that named
    # it still resolve to a name. It can hold no capability and match no
    # registration — `same_entity` already refuses a payload with nothing to
    # prove identity with — so `entry` is not consulted for its facts either.
    deleted = is_purged(recipient)
    facts: Mapping = recipient or ({} if deleted else entry or {})
    return {
        "saved": saved,
        "entry": entry,
        "label": (
            (saved or {}).get("label")
            or str((entry or {}).get("label") or "")
            or str(facts.get("legalName") or "")
            or "—"
        ),
        "legal_name": str(facts.get("legalName") or ""),
        "family": (saved or {}).get("rail_family") or str((entry or {}).get("rail") or ""),
        # An unreadable row's coordinates are not "none" — they are unknown, and
        # the template says so rather than printing an empty cell.
        "coordinates": [] if unreadable else coordinates(facts),
        "bank": [
            value
            for value in (
                str(facts.get("routingNumber") or ""),
                str(facts.get("bic") or ""),
            )
            if value
        ],
        "unreadable": unreadable,
        "deleted": deleted,
        # How many saved records share this destination, or 0. Non-zero means the
        # console cannot say which of them a registration — or a payment — belongs
        # to, so the row states the collision instead of picking a winner.
        "ambiguous": ambiguous,
    }


def merge(saved: list[dict], entries: list[Mapping]) -> list[dict]:
    """The unified Contacts list: saved records, their whitelist twins folded in,
    then the registrations nothing was saved for.

    A whitelist entry is claimed by **at most one** saved row (`taken`): two
    console records that both match one registration would otherwise each show a
    "whitelisted" capability the customer has only once.
    """
    shared = _duplicates(saved)
    rows: list[dict] = []
    taken: set[str] = set()
    for row in saved:
        # An ambiguous row claims nothing. Two saved records with one set of
        # coordinates cannot both be the registration, and *sort order* was
        # deciding which one was — silently, and then again for every transfer
        # attributed through this same function. Neither answer is provable, so
        # neither is given.
        ambiguous = shared.get(str(row.get("id") or ""), 0)
        twin = (
            None
            if ambiguous
            else next(
                (
                    entry
                    for entry in entries
                    if str(entry.get("id") or "") not in taken
                    and same_entity(entry, row.get("recipient"), row.get("rail_family") or "")
                ),
                None,
            )
        )
        if twin is not None:
            taken.add(str(twin.get("id") or ""))
        rows.append(_contact(row, twin, ambiguous=ambiguous))
    return rows + [
        _contact(None, entry) for entry in entries if str(entry.get("id") or "") not in taken
    ]


def _duplicates(saved: list[dict]) -> dict[str, int]:
    """`{saved id: how many saved rows share its destination}`, for the rows that
    share one with another — `{}` when every destination is its own.

    Grouped on `IDENTITY_KEYS`, which is what `same_destination` compares, so
    "these two are the same destination" means here exactly what it means
    everywhere else. A row with nothing to prove identity *with* is never a
    duplicate, for the same reason `same_entity` refuses to match on one:
    agreeing about an empty string establishes nothing.
    """
    groups: dict[tuple, list[str]] = {}
    for row in saved:
        recipient = row.get("recipient") or {}
        if not any(str(recipient.get(key) or "").strip() for key in PROOF_KEYS):
            continue
        key = tuple(str(recipient.get(name) or "").strip() for name in IDENTITY_KEYS)
        groups.setdefault(key, []).append(str(row.get("id") or ""))
    return {
        row_id: len(ids) for ids in groups.values() if len(ids) > 1 for row_id in ids
    }


def identify(recipient: Mapping | None) -> str:
    """One line that tells two destinations apart without printing either.

    The same masking policy as `coordinates`, for the places that have room for
    one string instead of a column: masked account coordinates, else the public
    bank identifier, else an em dash. **Conduit's whitelist entries render
    through here too** — the recipients list and both
    registered-destination pickers printed full account numbers and IBANs, which
    contradicted the policy this module states three lines above and made a
    payout form a better place to harvest account numbers than the address book
    it was written to protect. Identification needs the last four, the legal
    name and the rail; it never needed the whole number.
    """
    return " ".join(coordinates(recipient)) or str((recipient or {}).get("bic") or "") or "—"


def label_for(submitted: Mapping, recipient: Mapping | None) -> str:
    """The operator's label, or the recipient's own legal name as the fallback.

    `legalName` is required on every fiat route discovery describes, so the
    fallback is real rather than theoretical. Empty means there is nothing to
    call this destination and nothing is saved — a row labelled "" is a row
    nobody can pick.
    """
    typed = (submitted.get("counterparty_label") or "").strip()
    return (typed or str((recipient or {}).get("legalName") or "").strip())[:LABEL_MAX]


def wants_save(submitted: Mapping) -> bool:
    """Whether the submission ticked "save as counterparty"."""
    return bool((submitted.get("save_counterparty") or "").strip())


def recipient_of(root: Mapping | None) -> dict:
    """`destination.recipient` out of a `FormValues.root`, or `{}`."""
    node: object = root or {}
    for key in RECIPIENT_PATH:
        node = node.get(key) if isinstance(node, Mapping) else None
    return dict(node) if isinstance(node, Mapping) else {}


# --- reads --------------------------------------------------------------------------


def _decrypt(blob: object) -> dict | None:
    """The recipient behind one row's ciphertext, or None when it cannot be read.

    The column is NOT NULL, so None is unambiguous here: it means *unreadable*
    (corrupt ciphertext, or a key rotated away from under the row), never
    "empty". Reading the blob raw and decrypting per row — rather than letting
    `EncryptedJSON` do it inside the query — is what keeps one bad row from
    taking down a whole customer's list. A review found the same failure on
    the dashboard; it does not get to reappear on a page that must decrypt to
    render at all.

    The tolerance itself moved to `app/crypto.py` when the batch rows became the
    second table that has to decrypt to render — one reader,
    so the two cannot come to differ about what an unreadable row is.
    """
    return read_json(blob)


_COLUMNS = (
    Counterparty.id,
    Counterparty.label,
    Counterparty.rail_family,
    Counterparty.recipient_type,
    Counterparty.destination_country,
    Counterparty.created_by_actor_email,
    Counterparty.created_at,
    Counterparty.updated_at,
    Counterparty.archived_at,
)


async def rows(
    session: AsyncSession,
    customer_id: str,
    *,
    family: str = "",
    recipient_type: str = "",
    country: str = "",
    archived: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """This customer's counterparties, newest label order, recipients decrypted.

    `limit` is a **SQL** `LIMIT`, not a slice of the result: every row this
    returns costs a Fernet decrypt, so a caller with a budget (the CSV export's
    row cap) has to spend it in the query rather than after it. The pickers and
    the management page pass nothing and are unchanged.

    **`customer_id` is not optional and is never widened.** Every caller — the
    payout picker, the management page, the archive and rename handlers — comes
    through here or through `get`, so "customer B sees customer A's address book"
    has one place it could be introduced and it is this line.

    The three route filters are the compatibility gate: a `sepa` IBAN is not
    payable over fedwire (`payments.RAILS_FOR`), and a destination saved for an
    individual in Germany is not the one for a business in the US.

    `offset` pages **this customer's** rows. `/contacts?customerId=` used to page
    the global table and filter the page in Python, which meant a customer whose
    contacts sat past the global page rendered as "no contact matches". The scope
    belongs in the query, where the pickers and the
    export already put it.
    """
    return await _read(
        session,
        Counterparty.customer_id == customer_id,
        family=family,
        recipient_type=recipient_type,
        country=country,
        archived=archived,
        limit=limit,
        offset=offset,
    )


async def everyones(
    session: AsyncSession,
    *,
    family: str = "",
    customer_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Every customer's contacts, for the cross-customer page.

    A **separate function**, not a nullable `customer_id` on `rows`: that
    predicate is the one line keeping customer B out of customer A's address
    book, and a parameter that can switch it off is a parameter someone will
    eventually pass by accident. This one has no customer scope by construction,
    reads exactly the same columns, and is reachable only from `/contacts` —
    which is a page about the whole installation, like `/accounts`.

    `offset` is real paging over a local table (there is no cursor to carry), and
    a row this returns still costs a Fernet decrypt, so the caller's page size is
    a SQL `LIMIT` here exactly as the export's cap is.

    `customer_ids` narrows to a *set* of customers — what a name search resolves
    to, since one typed name can match several. It is still not `rows`'
    single-customer predicate and cannot be used to widen one: an empty list is
    "no customer matched", which correctly selects nothing rather than everything.
    """
    scope = [] if customer_ids is None else [Counterparty.customer_id.in_(customer_ids)]
    return await _read(session, *scope, family=family, limit=limit, offset=offset)


async def _read(
    session: AsyncSession,
    *scope,
    family: str = "",
    recipient_type: str = "",
    country: str = "",
    archived: bool = False,
    limit: int | None = None,
    offset: int = 0,
    lock: bool = False,
) -> list[dict]:
    """The one query both reads are built from — same columns, same decrypt, same
    order — so a filter can never mean two different things on two pages.

    `lock` adds `FOR UPDATE` and belongs to exactly one caller (`live_by_label`,
    below, which explains why). Every other read here is a page, and a page that
    took row locks would make rendering a list block a write.
    """
    query = select(
        *_COLUMNS,
        Counterparty.customer_id,
        type_coerce(Counterparty.recipient, LargeBinary).label("blob"),
    ).where(*scope)
    if not archived:
        query = query.where(Counterparty.archived_at.is_(None))
    if family:
        query = query.where(Counterparty.rail_family == family)
    if recipient_type:
        query = query.where(Counterparty.recipient_type == recipient_type)
    if country:
        query = query.where(Counterparty.destination_country == country.upper())
    # The id is the tiebreaker, not decoration: `lower(label)` is not unique —
    # one customer may save "Globex" for `us` and for `sepa` — and offset paging
    # over a non-unique sort key lets two requests disagree about which row sits
    # at position N, so a page turn can skip a contact or show one twice. A total
    # order makes the offset mean one thing.
    query = query.order_by(func.lower(Counterparty.label), Counterparty.id)
    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)
    if lock:
        query = query.with_for_update()
    result = await session.execute(query)
    return [
        {
            "id": row.id,
            "customer_id": row.customer_id,
            "label": row.label,
            "rail_family": row.rail_family,
            "recipient_type": row.recipient_type,
            "destination_country": row.destination_country,
            "created_by": row.created_by_actor_email,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "archived_at": row.archived_at,
            "recipient": _decrypt(row.blob),
        }
        for row in result.all()
    ]


# Which counterparty a payout operation used, and whether it saved one. Both are
# audit rows against the operation, and the trail is the *record*: no column, no
# migration, no join to a table whose row may since have been renamed or
# archived. `counterparty.save` is written by the saver on 202; `counterparty.
# used` by the payout route when a submission named a saved destination, and by
# the transfer route when the registered destination it sent to *is* a saved
# contact. Newest wins, so a payout that used one and saved
# under another name shows what it ended up stored as.
USED_ACTION = "counterparty.used"
SAVE_ACTION = "counterparty.save"
USE_ACTIONS = (USED_ACTION, SAVE_ACTION)


async def attached(session: AsyncSession, operation_id) -> dict:
    """The counterparty this operation used or saved — `{"id", "label"}` — or `{}`.

    The **label and the id** — never coordinates. The audit detail carries no
    bank fields for exactly this reason: the operation panel is a page about a
    payment, not an address book, and a masked coordinate on it would still be a
    coordinate on a page nobody asked for one on. The id is here so the panel can
    *link* to the contact rather than name a record the
    operator then has to go and find; both actions record it now (the save side
    later), so the only rows without one are **historical** — written
    before it was recorded — and they render as the plain label they always did.

    Same shape as `convert._selection_of`: an operation's extra console-local
    facts are read off its own audit trail, which is where this console already
    keeps them.
    """
    row = (
        await session.execute(
            select(AuditEvent.detail)
            .where(
                AuditEvent.operation_id == operation_id,
                AuditEvent.action.in_(USE_ACTIONS),
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    label = str((row or {}).get("label") or "")
    return {"label": label, "id": str((row or {}).get("counterparty") or "")} if label else {}


async def linked_transactions(
    session: AsyncSession,
    *,
    customer_id: str,
    contact_id: str,
    limit: int,
    offset: int = 0,
) -> list[str]:
    """The transactions this console sent **to** one contact, newest first.

    The whole of the evidence, in one query: a `counterparty.used`
    audit row says the operator addressed a payment from this contact *and* that
    what was submitted still was it (`same_destination` decided that before the
    row was written — see `app/web/payouts.py`), and its operation carries the
    transaction Conduit created. Nothing else is a link, and nothing else is
    claimed:

    * **`counterparty.modified_prefill` is excluded by construction** — it is not
      `USED_ACTION`. That is the truth-in-audit decision: the prefill was
      edited, so the payment did not go to the saved contact, and a filter that
      returned it would be the console asserting a destination it disproved.
    * **`counterparty.save` is excluded too**, and this is the one exclusion that
      needs saying out loud: the payout that *first saved* a contact typed its
      destination, it did not pick it, and "sent to contact X" is a claim about
      picking — which is what the boundary line on the ledger says. The
      exclusion is the `action` predicate below and has always been: the save
      row records the contact's id too (so the operation panel
      can link to what it created), and that deliberately did **not** widen this
      filter by a row.
    * An operation with no `conduit_resource_id` created no transaction (rejected,
      or still in flight), so there is no ledger row to show.

    `customer_id` is a second predicate on top of the contact id, which is
    already customer-scoped: contacts are per-customer everywhere in this module
    and the filter must not become the one place that isn't.

    No index beyond the two `audit_events` already carries
    (`occurred_at`, `operation_id`) — this is one action value on an internal
    console's own trail. A partial index on `(action) where action =
    'counterparty.used'` is the upgrade path if the table ever grows past the
    planner's patience.
    """
    if not customer_id or not contact_id:
        return []
    found = await session.execute(
        select(Operation.conduit_resource_id)
        .join(AuditEvent, AuditEvent.operation_id == Operation.id)
        .where(
            AuditEvent.action == USED_ACTION,
            AuditEvent.detail["counterparty"].astext == str(contact_id),
            Operation.customer_id == customer_id,
            Operation.conduit_resource_id.is_not(None),
        )
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [row[0] for row in found]


async def history(
    session: AsyncSession,
    customer_id: str,
    cp_id: str,
    *,
    label: str = "",
    since: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Everything this console recorded about one contact, newest first.

    **The trail is the record** — the same principle the sent-to-contact filter
    is built on (`linked_transactions`). There is no history table and no new
    column: every write in this module is already an audited one, and this is
    that audit read back for a single contact.

    Two predicates, and the second one is the interesting one:

    * `detail->>'counterparty' = <id>` catches everything written *about* a
      contact — `used`, `modified_prefill`, `rename`, `edited`, `archive`,
      `deleted`, and the bridge (`app/web/recipients.py`). Actions this build has
      never heard of match too and are rendered raw rather than dropped, exactly
      as an unknown status is (plan v2 §7).
    * `counterparty.save` records the **label**, never the id — the saver stores
      what it stored, and a label is renameable (see `linked_transactions`). So a
      save is matched by `(customer, label, at or after this row was created)`,
      which is provable: one live label per customer is a unique index, and a
      save belonging to an *earlier* contact of the same name necessarily
      predates this row. The cost is stated on the panel: a save recorded under a
      name this contact has since been renamed away from is not shown, because
      nothing here could prove it was this contact's.

    The operation's `conduit_resource_id` rides along so a row that came from a
    payout can link to the transaction it created, and `Operation.type` so the
    panel can say which kind of write it was.

    **No index added.** `audit_events`' indexes are decided together
    with the projections and dashboard ones, and a single-installation console's
    trail is small enough that a bounded `LIMIT 50` on one contact does not earn
    a schema migration ahead of that. The
    upgrade path is unchanged and already written down beside
    `linked_transactions`: a partial index on `action`, plus an expression index
    on `(detail->>'counterparty')` if this panel is ever the slow one.
    """
    key = _key(cp_id)
    if key is None:
        return []
    named = AuditEvent.detail["counterparty"].astext == str(key)
    scope = [named]
    if label and since is not None:
        scope.append(
            and_(
                AuditEvent.action == SAVE_ACTION,
                AuditEvent.detail["customer"].astext == customer_id,
                func.lower(AuditEvent.detail["label"].astext) == label.lower(),
                AuditEvent.occurred_at >= since,
            )
        )
    found = await session.execute(
        select(
            AuditEvent.action,
            AuditEvent.actor_email,
            AuditEvent.occurred_at,
            AuditEvent.detail,
            AuditEvent.operation_id,
            Operation.type,
            Operation.conduit_resource_id,
        )
        .outerjoin(Operation, AuditEvent.operation_id == Operation.id)
        .where(or_(*scope))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
    )
    return [
        {
            "action": row.action,
            "actor": row.actor_email,
            "at": row.occurred_at,
            "detail": row.detail or {},
            "operation": row.operation_id,
            "operation_type": row.type,
            "transaction": row.conduit_resource_id,
        }
        for row in found.all()
    ]


def _key(cp_id: object) -> uuid.UUID | None:
    """A counterparty id, or None for anything that is not one."""
    try:
        return uuid.UUID(str(cp_id))
    except (ValueError, AttributeError):
        return None


async def get(
    session: AsyncSession, customer_id: str, cp_id: str, *, include_archived: bool = False
) -> dict | None:
    """One live counterparty **of this customer** as a plain dict, or None.

    The `customer_id` predicate is the route-level isolation guard: a URL that
    names customer B and a counterparty of customer A resolves to nothing, which
    is the same answer an id that never existed gets. No cross-customer read is
    possible through a mistyped id.

    **Raw blob + `_decrypt`, exactly like `rows`**:
    selecting the ORM entity ran `EncryptedJSON`'s result processor inside the
    query, so one corrupt or key-rotated row raised `InvalidToken` out of
    `session.execute` and 500ed the *page* — while `rows()` had tolerated the
    same row for a year. `recipient` is None when it cannot be read, which the
    prefill already renders as a refusal. The tolerance now lives on both read
    paths, which is the only way it stays true.

    `include_archived` is for the readers that are about **history**, not about
    what may be used: archiving retires a contact from every picker, it does not
    unsend the payments that named it, so a bookmarked ledger filter has to keep
    resolving. The returned row says `archived` so a
    caller can label it; every *writing* and prefilling caller leaves the flag
    alone and still cannot reach an archived record.
    """
    key = _key(cp_id)
    if key is None:
        return None
    scope = [Counterparty.id == key, Counterparty.customer_id == customer_id]
    if not include_archived:
        scope.append(Counterparty.archived_at.is_(None))
    row = (
        await session.execute(
            select(
                *_COLUMNS, type_coerce(Counterparty.recipient, LargeBinary).label("blob")
            ).where(*scope)
        )
    ).first()
    if row is None:
        return None
    recipient = _decrypt(row.blob)
    return {
        "id": row.id,
        "label": row.label,
        "rail_family": row.rail_family,
        "recipient_type": row.recipient_type,
        "destination_country": row.destination_country,
        "recipient": recipient,
        "archived": row.archived_at is not None,
        # Deleted is a *kind* of archived, and the callers that resolve archived
        # rows on purpose (the ledger's contact filter, the history panel) have
        # to be able to tell an operator which one they are looking at.
        "deleted": is_purged(recipient),
        "created_at": row.created_at,
        "created_by": row.created_by_actor_email,
    }


async def find(session: AsyncSession, cp_id: str) -> dict | None:
    """One contact by id **alone**, whoever's it is — `everyones`' single-row twin.

    It belongs to that family and not to `get` for the reason `get`'s docstring
    gives: the customer predicate there is the one line keeping customer B out of
    customer A's address book, and a parameter that switched it off would
    eventually be passed by accident. This function has no customer scope by
    construction, and exactly one caller: the cross-customer picker
    (`GET /payouts/contact`), whose whole job is to *supply* the
    customer — the hand-off it builds is the customer-scoped payout URL every
    other prefill caller already uses, and the prefill there still goes through
    `get` under that customer.

    Archived rows come back rather than reading as "never existed"
    (`archived_at` says which): the picker refuses an archived contact in the
    contacts page's own words, which it could not do if the row resolved to
    nothing.
    """
    key = _key(cp_id)
    if key is None:
        return None
    found = await _read(session, Counterparty.id == key, archived=True)
    return found[0] if found else None


# --- writes -------------------------------------------------------------------------
#
# None of these is a Conduit mutation, so none is an operations-ledger row: there
# is no remote resource to duplicate and no outcome that can stay unknown. They
# are audited by their callers instead (`app/web/__init__.py` documents the
# exception alongside the others). Committing is the caller's, so a save lands in
# the same transaction as its audit row.


# The advisory-lock namespace for "this customer's use of this label". The high
# half is a constant so these keys cannot collide with another feature's advisory
# locks in the same database; the low half is a digest of `save`'s conflict
# target. "CONT", for the module.
_LABEL_LOCK = 0x434F4E54


def label_lock_key(customer_id: str, label: str) -> int:
    """The 64-bit `pg_advisory_xact_lock` key for `save`'s conflict target,
    `(customer_id, lower(label))`.

    A digest, because advisory locks take integers rather than the strings the
    index is on. Two different labels can therefore collide, and that is
    harmless by construction: a collision costs one save a wait behind an
    unrelated one and can never produce a wrong answer, because the lock only
    orders the writers — the partial unique index is still what decides the
    outcome.
    """
    subject = f"{customer_id}\x00{(label or '')[:LABEL_MAX].lower()}"
    digest = hashlib.blake2b(subject.encode(), digest_size=4).digest()
    return (_LABEL_LOCK << 32) | int.from_bytes(digest, "big")


async def hold_label(session: AsyncSession, customer_id: str, label: str) -> None:
    """Take the transaction-scoped advisory lock on this customer's use of this
    label, blocking until it is free and holding it to the caller's commit.

    **Every writer that can come to occupy a label takes this**, which is what
    makes it worth anything: a lock one path skips is not a lock. `live_by_label`
    takes it for the payout form's save, `insert` for the Contacts page's clone.
    """
    await session.execute(select(func.pg_advisory_xact_lock(label_lock_key(customer_id, label))))


async def live_by_label(session: AsyncSession, customer_id: str, label: str) -> dict | None:
    """The live contact this customer already keeps under `label`, **locked** —
    the row a `save` under that name is about to overwrite — or None when the
    name is free. A read, kept here beside the write it exists to describe.

    **The `FOR UPDATE` is the whole point, not a precaution.** `save` upserts by
    index rather than by read-then-write precisely so that it cannot lose a race;
    a plain read taken before it would reintroduce one on the audit side, where
    the trail could end up recording a before/after against coordinates a third
    transaction had already replaced — a diff nobody's save ever performed, which
    is worse than no diff at all. The lock is held from here to the caller's
    commit, so the upsert (and `update`, `rename`, `archive` — every writer goes
    through the row) waits behind it, and what this returns is provably what the
    upsert overwrote.

    **A row lock alone could not cover a label nobody holds yet**, and that gap
    was real: with no row there is nothing to lock, so two saves creating the
    same name at the same instant both saw it free, the loser's upsert updated
    the winner's fresh row, and its audit recorded a *create* — the one shape a
    reader takes as "nothing was overwritten here". `hold_label` closes it by
    locking the conflict target rather than the row, so the key exists before
    the row does. The loser now waits, re-reads, finds the winner's contact and
    audits the overwrite it actually performed.

    The match is `save`'s own conflict target — `(customer_id, lower(label))` over
    the live rows, the partial unique index — so at most one row can come back,
    and it is the row the upsert will find.
    """
    label = (label or "")[:LABEL_MAX]
    if not label.strip():
        return None
    # Before the read, not after: serialising the read is the entire point, and
    # a lock taken afterwards would order nothing.
    await hold_label(session, customer_id, label)
    found = await _read(
        session,
        Counterparty.customer_id == customer_id,
        func.lower(Counterparty.label) == label.lower(),
        lock=True,
    )
    return found[0] if found else None


async def save(
    session: AsyncSession,
    *,
    customer_id: str,
    label: str,
    recipient: Mapping,
    rail_family: str,
    recipient_type: str,
    destination_country: str,
    actor_id: str,
    actor_email: str,
) -> uuid.UUID:
    """Insert this counterparty, or update the live one already wearing its label.

    Atomic by index rather than by read-then-write, so two submits racing on the
    same label produce one row instead of a unique-violation 500.
    """
    statement = (
        pg_insert(Counterparty)
        .values(
            id=uuid.uuid4(),
            customer_id=customer_id,
            label=label[:LABEL_MAX],
            recipient=dict(recipient),
            rail_family=rail_family,
            recipient_type=recipient_type,
            destination_country=(destination_country or "").upper(),
            created_by_actor_id=actor_id,
            created_by_actor_email=actor_email,
        )
        .returning(Counterparty.id)
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Counterparty.customer_id, text("lower(label)")],
        index_where=Counterparty.archived_at.is_(None),
        set_={
            # `excluded` is the proposed row, so the ciphertext the bind
            # processor just produced — the payload is never re-encrypted or
            # round-tripped through Python here.
            "label": statement.excluded.label,
            "recipient": statement.excluded.recipient,
            "rail_family": statement.excluded.rail_family,
            "recipient_type": statement.excluded.recipient_type,
            "destination_country": statement.excluded.destination_country,
            "updated_at": func.now(),
        },
    )
    return (await session.execute(statement)).scalar_one()


async def insert(
    session: AsyncSession,
    *,
    customer_id: str,
    label: str,
    recipient: Mapping,
    rail_family: str,
    recipient_type: str,
    destination_country: str,
    actor_id: str,
    actor_email: str,
) -> uuid.UUID | None:
    """A **new** contact, or None when this customer already has a live one wearing
    that label. `save`'s twin for the one caller that must never update.

    `save` upserts on purpose — saving a destination under a name you already use
    is a correction of that contact, and the docstring above argues it. A clone
    is the opposite intent: it exists to produce a SECOND record, so an upsert
    there is not "the same contact updated", it is another contact's coordinates
    silently overwritten. The caller checks the label first; this is what makes
    the check unraceable, because the answer comes from the partial unique index
    itself rather than from a read that a commit can invalidate a millisecond
    later (gate finding m1).

    The insert runs in a SAVEPOINT so that the refusal is a value and not a
    poisoned transaction: the caller re-renders its form from the same session.

    It takes `hold_label` even though the index already decides its own outcome,
    because a lock is only worth what its least careful holder makes it worth: a
    clone committing between a payout save's read and its upsert would leave
    that save overwriting this row while auditing a create. Here the clone waits
    and then loses to the index, which is the answer it wanted anyway.
    """
    await hold_label(session, customer_id, label)
    row = Counterparty(
        customer_id=customer_id,
        label=label[:LABEL_MAX],
        recipient=dict(recipient),
        rail_family=rail_family,
        recipient_type=recipient_type,
        destination_country=(destination_country or "").upper(),
        created_by_actor_id=actor_id,
        created_by_actor_email=actor_email,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return None
    return row.id


# The scope every write shares: this customer's, live, and named by a real id.
def _scoped(statement, customer_id: str, key: uuid.UUID):
    return statement.where(
        Counterparty.id == key,
        Counterparty.customer_id == customer_id,
        Counterparty.archived_at.is_(None),
    )


async def archive(session: AsyncSession, customer_id: str, cp_id: str) -> bool:
    """Soft-delete. True when a live counterparty of this customer was archived.

    A scoped UPDATE rather than load-then-mutate: archiving a row does not need
    its coordinates, and loading them meant a row whose ciphertext no longer
    decrypts could not be archived at all — the one row an operator most wants
    gone.
    """
    key = _key(cp_id)
    if key is None:
        return False
    result = await session.execute(
        _scoped(sql_update(Counterparty), customer_id, key).values(archived_at=datetime.now(UTC))
    )
    return bool(result.rowcount)


async def rename(session: AsyncSession, customer_id: str, cp_id: str, label: str) -> str:
    """Relabel. `""` on success, otherwise the sentence explaining why not.

    The name half of `update` below, kept as its own door because the Contacts
    list renames in place and has no business fetching a route's requirements to
    do it. Both go through `_taken` and refuse in the same words.

    A scoped UPDATE for the same reason `archive` is one: a name is not a
    coordinate, so renaming never needs to decrypt the row.
    """
    label = (label or "").strip()[:LABEL_MAX]
    if not label:
        return NO_NAME
    key = _key(cp_id)
    if key is None:
        return NOT_EDITABLE
    if await _taken(session, customer_id, key, label):
        return taken_message(label)
    result = await session.execute(
        _scoped(sql_update(Counterparty), customer_id, key).values(label=label)
    )
    return "" if result.rowcount else NOT_EDITABLE


# The refusals `rename` and `update` share, word for word: they are two doors
# into one write, and an operator who meets the same wall twice should not be
# told two different things about it.
NO_NAME = "A contact needs a name — an unnamed one cannot be picked."
NOT_EDITABLE = "That contact is not this customer's, or is archived."


def taken_message(label: str) -> str:
    return f"This customer already has a contact called {label!r}."


async def _taken(session: AsyncSession, customer_id: str, key: uuid.UUID, label: str) -> bool:
    await hold_label(session, customer_id, label)
    return bool(
        (
            await session.execute(
                select(Counterparty.id).where(
                    Counterparty.customer_id == customer_id,
                    Counterparty.archived_at.is_(None),
                    Counterparty.id != key,
                    func.lower(Counterparty.label) == label.lower(),
                )
            )
        ).first()
    )


async def update(
    session: AsyncSession,
    customer_id: str,
    cp_id: str,
    *,
    label: str,
    recipient: Mapping,
) -> str:
    """Replace one live contact's name **and** its stored destination. `""` on
    success, otherwise the sentence explaining why not.

    The same `_scoped` UPDATE `rename` and `archive` use, for the same reasons:
    customer-scoped (a URL naming another customer's contact resolves to nothing,
    exactly as a made-up id does) and live-only (an archived contact is retired,
    and editing one would quietly bring a retired destination back into the
    console's records without putting it back in the pickers). A **deleted** row
    is archived too, so this refuses one without needing to know what deletion
    is.

    `recipient` is written whole, not merged: what the caller assembled is what
    the row becomes. The caller is `app/web/contacts.py`, which builds it from
    discovery's own fields over the stored payload — the merge rule lives there,
    beside the model that decides which keys the form owns.

    An empty recipient is refused rather than stored: a contact with no
    destination is a delete performed by accident, and deleting has its own door
    (`purge`) with its own confirmation.
    """
    label = (label or "").strip()[:LABEL_MAX]
    if not label:
        return NO_NAME
    if not recipient:
        return (
            "A contact needs a destination. Saving an empty one would delete it by "
            "accident — use Delete contact if that is what you meant."
        )
    key = _key(cp_id)
    if key is None:
        return NOT_EDITABLE
    if await _taken(session, customer_id, key, label):
        return taken_message(label)
    result = await session.execute(
        _scoped(sql_update(Counterparty), customer_id, key).values(
            label=label, recipient=dict(recipient)
        )
    )
    return "" if result.rowcount else NOT_EDITABLE


# What a **deleted** contact's payload becomes: readable, and empty. The column
# is NOT NULL and `None` already means *unreadable* on every read path in this
# module (`_decrypt`), so a purge that nulled it would make a deliberate deletion
# and a corrupt row indistinguishable — and "we cannot read this" is not the same
# fact as "an admin destroyed this". An empty object is the honest third state,
# it needs no migration, and nothing can be recovered from it.
#
# Nothing else can write it: `save` refuses an empty recipient (`payouts.
# _save_counterparty` returns early), and so does `update` above — which is what
# makes the marker unambiguous rather than merely conventional.
PURGED: dict = {}


def is_purged(recipient: object) -> bool:
    """Whether this row's coordinates were deliberately destroyed."""
    return recipient == PURGED


async def purge(session: AsyncSession, customer_id: str, cp_id: str) -> bool:
    """**Destroy** one contact's coordinates and archive it. True when a contact
    of this customer was purged.

    The two-tier delete's second tier (`app/web/contacts.py`). Archiving is the
    everyday removal and is reversible in the only sense that matters — the
    coordinates are still there. This is not: the ciphertext is overwritten with
    `PURGED` and no key, backup of this row or later read brings it back.

    **The shell stays, and that is the point.** The row keeps its id, its label,
    its actor and its timestamps, so every `counterparty.used` audit row still
    resolves to a name and the ledger's sent-to-contact filter keeps answering
    (`app/web/transactions.py`). Deleting the row itself would falsify history:
    the payments were sent, and the console would stop being able to say to whom.

    Unlike every other write here it is **not** `_scoped`: an archived contact
    still holds its coordinates, and "archive it, then destroy what it held" is
    the obvious order to do this in. `archived_at` is coalesced rather than set,
    so a purge does not rewrite when the contact was retired.
    """
    key = _key(cp_id)
    if key is None:
        return False
    result = await session.execute(
        sql_update(Counterparty)
        .where(Counterparty.id == key, Counterparty.customer_id == customer_id)
        .values(
            recipient=PURGED,
            archived_at=func.coalesce(Counterparty.archived_at, func.now()),
        )
    )
    return bool(result.rowcount)


# --- what changed, said without saying the coordinates --------------------------------


def diff(before: Mapping | None, after: Mapping | None) -> dict[str, str]:
    """`{identity key: "before→after"}` for the identity keys an edit changed.

    **The masking policy of this module, applied to a durable record instead of a
    screen** (DESIGN.md's last-4 row). `accountNumber` and `iban` are masked
    because they identify an account; `routingNumber`, `bic` and `legalName` are
    written plainly for exactly the reason the Contacts list prints them plainly
    — an ABA and a BIC identify a *bank* and are public routing data, and a legal
    name is a name. One policy, two surfaces; a second, stricter rule invented
    for the audit trail would be a second answer to "what may this console
    print".

    Only `IDENTITY_KEYS`, and only the ones that actually changed: an audit row
    is not a copy of the record, it is what happened to it.

    **`before is None` means the prior could not be read, and is recorded as
    exactly that**. The repair path edits a contact whose
    ciphertext no longer decrypts — that is the only way this function is handed
    a `None` — and treating it as an empty record wrote `legalName: —→ZZZTEST
    Globex`, which asserts the row previously had no legal name. Nobody knows
    what it had; that is the whole reason the form started empty. A trail that
    fabricates an absence is worse than one that says "unknown", because a
    reader cannot tell the two apart afterwards.
    """
    unknown_prior = before is None
    changed = {}
    for key in IDENTITY_KEYS:
        was = str((before or {}).get(key) or "").strip()
        now = str((after or {}).get(key) or "").strip()
        if was == now and not unknown_prior:
            continue
        show = mask if key in MASKED_KEYS else (lambda value: value or "—")
        if unknown_prior:
            # Every key this repair wrote, and nothing claimed about any of them
            # beforehand. A key the repair left empty is skipped: there is
            # nothing to record on either side of the arrow.
            if now:
                changed[key] = f"unreadable→{show(now)}"
            continue
        changed[key] = f"{show(was)}→{show(now)}"
    return changed


def changed_keys(before: Mapping | None, after: Mapping | None) -> list[str]:
    """The dotted names of every leaf an edit changed — **names only, no values**.

    The rest of the recipient subtree is a bank address and a postal address: the
    audit row says *which* of them moved, because "edited" with nothing beside it
    tells an operator nothing, and printing the values would put a person's home
    address in a log the retention rules do not reach.
    """

    def leaves(node: object, path: tuple[str, ...] = ()) -> dict[str, str]:
        if isinstance(node, Mapping):
            flat: dict[str, str] = {}
            for key, value in node.items():
                flat |= leaves(value, path + (str(key),))
            return flat
        return {".".join(path): "" if node is None else str(node)}

    was, now = leaves(before or {}), leaves(after or {})
    return sorted({key for key in was | now if was.get(key, "") != now.get(key, "")})
