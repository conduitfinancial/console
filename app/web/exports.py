"""CSV export of any list surface's current filtered view.

One route — `GET /export/{surface}.csv` — for all eight lists, because the eight
differ only in three things: where the rows come from, which filters the page
parses, and what the columns are called. Eight near-identical routes would be
eight places for the export to drift away from the page it claims to be a copy
of.

**The filters are never re-parsed here.** Each list module owns one
`list_query()` (Conduit-backed) or `list_filters()`/`where()` (local), and both
the HTML page and this export call it. That is the whole reason those functions
exist: an export that applied *nearly* the page's filters would be a file whose
contents nobody can account for, and a second copy of the parsing would be one
`if` away from becoming that at any time.

**Reads only, through the client's own budget.** A Conduit-backed export walks
the cursor with `client.page` — the same retry/backoff every read in this
console gets — hard-capped at `CAP_PAGES` pages or `CAP_ROWS` rows, whichever
comes first. Three endings, and the file states which one it got:

* walked to the end → the rows, and nothing else;
* hit a cap → the rows plus a final `# TRUNCATED …` row naming the cap;
* Conduit stopped answering → the rows read so far plus a final
  `# EXPORT INCOMPLETE …` row naming what happened.

A silently short file is the one outcome that is not allowed: an operator who
reconciles against a truncated export and cannot tell it was truncated is worse
off than one who got no file at all.

**Masking is the HTML's, exactly.** Coordinates go out last-4 masked through
`counterparties.identify` — the same function the two pages render — and a full
account number or IBAN is in no column of any surface. A CSV is the easiest
thing in this console to mail somewhere, which is precisely why it gets no
weaker policy than the screen.

**Every export is an audited PII egress.** One `export.csv` row per request —
actor, surface, the filters that were applied, the row count, whether it was
truncated — written and committed *before* the first byte is streamed, so a
reader who disconnects mid-download still leaves the record behind.

Viewers may export: it is a read, and it can only contain what the page already
showed them.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app import audit, batches, conversions, counterparties as cp, payments
from app.auth.actor import Actor
from app.auth.web import require
from app.conduit.client import ConduitClient, Page, Problem
from app.models import Projection
from app.web import NAME_LIMIT, conduit, customer_names, db, display_name
from app.web import accounts, applications, contacts, convert, customers, rfis, transactions

log = logging.getLogger(__name__)
router = APIRouter()

# The walk's ceiling. Whichever is reached first ends
# the walk and puts the truncation row in the file.
CAP_PAGES = 200
CAP_ROWS = 10_000
# Conduit's own maximum for `limit` (pinned spec) — the fewest requests the walk
# can spend for a given number of rows. Deliberately not the operator's
# rows-per-page: that is a property of the screen, and this is not the screen.
WALK_LIMIT = 100

TRUNCATED_NOTE = (
    f"# TRUNCATED — this export stopped at the {CAP_ROWS}-row / {CAP_PAGES}-page cap. "
    "It is NOT the whole result set: narrow the filters and export again."
)

# Two notes about *coverage* rather than about a cap — the same mechanism, for
# the two files whose row set is narrower than the screen that produced it.
# A filename token is not a boundary statement: it names the filter, not what the
# filter leaves out.
SAVED_ONLY_NOTE = (
    "# SCOPE — this file is this console's SAVED contacts. Conduit registrations with no "
    "saved record are not in this file, though the Contacts page counts them: export the "
    "recipients surface for those."
)
WHITELIST_UNREAD_NOTE = (
    "# EXPORT INCOMPLETE — Conduit's whitelist for this customer could not be read, so the "
    "`whitelisted` column says `unknown` on every row. It does not say `false`: nothing "
    "established that."
)
WHITELIST_CAPPED_NOTE = (
    "# EXPORT INCOMPLETE — this customer has more registrations than this export walks, so the "
    "`whitelisted` column says `unknown`: a registration past the walk cannot be matched."
)
SAVED_BY_NOTE = (
    "# SCOPE — `saved_by` is blank in a cross-customer export: the /contacts page it mirrors "
    "does not show who saved a contact. Export one customer for that column."
)
# The two things a blank `customer_name` can mean, never merged into one
# sentence — the file's version of the page's honest miss.
NAMES_BOUND_NOTE = (
    f"# SCOPE — `customer_name` is resolved from the first {NAME_LIMIT} customers Conduit "
    "lists, the same bounded read the page makes. A blank name is an id that read did not "
    "resolve, never a claim that the customer has no name."
)
NAMES_UNREAD_NOTE = (
    "# EXPORT INCOMPLETE — the customer directory could not be read, so `customer_name` is "
    "blank on every row. The accounts themselves are this installation's own records and are "
    "complete; only the names are missing."
)
# The ledger's All view exported: several kinds in one file. Said out loud
# because a reconciler opening it has no tab strip to tell them — the `type`
# column is the answer, and this names it (the page's own default, one read).
MULTI_KIND_NOTE = (
    "# SCOPE — this is the ledger's All view: every kind this console works in, in one "
    "newest-first feed. Rows of several transaction kinds are mixed together and the `type` "
    "column says which each one is. The crypto kinds (onramp, offramp, conversion) are out of "
    "this console's scope and are in no file it writes."
)
CONTACT_NOTE = (
    "# SCOPE — payouts this console sent and recorded this contact on. A payment made "
    "outside this console, one made before the contact existed, one sent on an edited "
    "prefill, and the payout that first saved the contact are all absent by design."
)


# --- cells ----------------------------------------------------------------------------


def _cell(value: object) -> str:
    """One CSV cell, safe to open in a spreadsheet.

    A cell whose first non-blank character is `=`, `+`, `-` or `@` is a *formula*
    to Excel, LibreOffice and Sheets — including `=cmd|'…'!A1`, which is how a
    CSV becomes remote code execution on someone else's laptop. The leading
    apostrophe is the standard neutraliser: the spreadsheet shows the text and
    evaluates nothing.

    It applies to negative numbers too (`-5.00` → `'-5.00`), and that is the
    correct trade: the alternative is a rule that tries to tell a number from a
    payload, and the day it is wrong is the day this file executes something.
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        # `True` is Python's spelling, not a data format's; a boolean column a
        # spreadsheet or a script reads should say `true`/`false`.
        text = "true" if value else "false"
    else:
        text = str(value)
    return "'" + text if text.lstrip()[:1] in ("=", "+", "-", "@") else text


def _iso(value: object) -> str:
    """A timestamp as ISO 8601 UTC — a machine file, so never the `ts` filter's
    reading form.

    Conduit's own stamps are already ISO strings and go out verbatim (an
    unparseable one is still what Conduit said, and this file must not silently
    improve it); this console's datetimes are normalised to UTC first, because a
    naive one is UTC by construction everywhere in this app.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    return "" if value is None else str(value)


def _plain(text: str) -> str:
    """The em-dash the templates print for "nothing here" is a *display* answer;
    a CSV says nothing by being empty."""
    return "" if text == "—" else text


# --- surfaces --------------------------------------------------------------------------
#
# `(path, filters, headers, row)` per Conduit-backed surface. `path` and
# `filters` are the list route's own — never re-derived here (see the module
# docstring). Headers are stable snake_case: a saved spreadsheet formula must
# not break because a column was renamed for looks.


def _customer(query) -> str:
    """The customer a customer-scoped surface is about. Its list page carries it
    in the path; the export link carries it in the query string."""
    return (query.get("customerId") or "").strip()


def _transaction_row(item: dict) -> list:
    return [
        item.get("id"),
        item.get("type"),
        item.get("status"),
        payments.stage_label(item),
        # The list cell exactly: a conversion leg's content is the pair of
        # amounts, everything else states one.
        payments.converted(item) or _plain(payments.amount_of(item)),
        item.get("customerId"),
        # The row's own name where the DTO carries one. The page's other source
        # — the bounded 25-customer resolver — is deliberately not used here: a
        # name column that resolved for the first 25 customers of an export of
        # 10,000 rows would look like data and be an artefact of a lookup table.
        item.get("customerName"),
        item.get("hasRfi"),
        _iso(item.get("createdAt")),
        _iso(item.get("completedAt")),
    ]


def _order_row(item: dict) -> list:
    view = conversions.order_view(item)
    return [
        item.get("id"),
        item.get("customerId"),
        view["type"],
        item.get("status"),
        view["source_asset"],
        view["source_amount"],
        view["destination_asset"],
        view["destination_amount"],
        _iso(item.get("createdAt")),
    ]


def _rfi_row(item: dict) -> list:
    subjects = rfis._subjects(item)
    return [
        item.get("id"),
        item.get("title"),
        item.get("summary"),
        item.get("status"),
        # All of them: an RFI carries a *list* of subjects, and the column that
        # showed one would hide the rest.
        "; ".join(f"{s['kind']}:{s['id']}" for s in subjects),
        _iso(item.get("dueAt")),
        _iso(item.get("publishedAt")),
    ]


def _recipient_row(item: dict) -> list:
    return [
        item.get("id"),
        item.get("legalName"),
        item.get("label"),
        item.get("rail"),
        # Masked, exactly as the page renders it (`counterparties.identify`).
        # `routingNumber`/`bic` stay whole for the same reason they do on
        # screen: an ABA and a BIC identify a *bank*, and are public.
        cp.identify(item),
        item.get("routingNumber"),
        item.get("bic"),
        item.get("relationship"),
        item.get("status"),
        item.get("rejectionReason"),
    ]


CONDUIT_SURFACES: dict[str, tuple] = {
    "transactions": (
        lambda q: payments.TRANSACTIONS_PATH,
        transactions.list_query,
        (
            "transaction_id",
            "type",
            "status",
            "stage",
            "amount",
            "customer_id",
            "customer_name",
            "has_rfi",
            "created_at",
            "completed_at",
        ),
        _transaction_row,
    ),
    "customers": (
        lambda q: "/v2/customers",
        customers.list_query,
        ("customer_id", "name", "type", "created_at"),
        lambda i: [
            i.get("id"),
            # The console's one name-fallback chain, so the column says what the
            # page's Customer cell says.
            display_name(i),
            i.get("type"),
            _iso(i.get("createdAt")),
        ],
    ),
    "applications": (
        lambda q: "/v2/applications",
        applications.list_query,
        (
            "application_id",
            "type",
            "status",
            "customer_id",
            "client_reference_id",
            "created_at",
            "updated_at",
        ),
        lambda i: [
            i.get("id"),
            i.get("type"),
            i.get("status"),
            i.get("customerId"),
            i.get("clientReferenceId"),
            _iso(i.get("createdAt")),
            _iso(i.get("updatedAt")),
        ],
    ),
    "rfis": (
        lambda q: "/v2/rfis",
        rfis.list_query,
        ("rfi_id", "title", "summary", "status", "subjects", "due_at", "published_at"),
        _rfi_row,
    ),
    "orders": (
        lambda q: conversions.ORDER_PATH,
        convert.list_query,
        (
            "order_id",
            "customer_id",
            "type",
            "status",
            "source_asset",
            "source_amount",
            "destination_asset",
            "destination_amount",
            "created_at",
        ),
        _order_row,
    ),
    "recipients": (
        lambda q: payments.WHITELIST_PATH.format(customer_id=_customer(q)),
        # The whitelist page filters nothing server-side — `?rail=` picks which
        # registration *form* is rendered, not which rows are listed — so the
        # export is that customer's whole whitelist, exactly as the table is.
        lambda q: {},
        (
            "recipient_id",
            "legal_name",
            "label",
            "rail",
            "coordinates_masked",
            "routing_number",
            "bic",
            "relationship",
            "status",
            "rejection_reason",
        ),
        _recipient_row,
    ),
}

# Surfaces whose rows never leave this installation. Same shape, but the "walk"
# is one SELECT — and the cap still applies, so a local export cannot become an
# unbounded response either.
LOCAL_SURFACES: dict[str, tuple] = {
    "accounts": (
        accounts.list_filters,
        # `customer_name` is APPENDED, never inserted (the `counterparties` rule
        # two entries down): a saved spreadsheet formula must not break because
        # the console started resolving names.
        ("virtual_account_id", "customer_id", "asset", "status", "observed_at", "customer_name"),
        lambda r: [
            r["id"],
            r["customer_id"],
            r["asset"],
            r["state"],
            _iso(r["observed"]),
            # Empty is the honest miss, exactly as the page renders it: an id
            # past the bounded directory read, a customer with no registered
            # name, or a directory that could not be read at all. The notes say
            # which; the cell never guesses.
            r.get("customer_name", ""),
        ],
    ),
    # The **saved** half of Contacts. Conduit's registrations are the
    # `recipients` surface and are not folded in here: they are a different
    # store with different columns, and a file that merged them would have a
    # blank half on every row from one side. The two capabilities are the
    # screen's join; the files stay the shape of what they came from.
    "counterparties": (
        contacts.list_filters,
        (
            "counterparty_id",
            "label",
            "rail_family",
            "recipient_type",
            "destination_country",
            "coordinates_masked",
            "legal_name",
            "saved_by",
            "created_at",
            "updated_at",
            # Appended, never inserted: a saved spreadsheet formula must not
            # break because a column moved. `whitelisted` is empty unless the
            # export named a customer — the only scope Conduit lists a whitelist
            # for — and says `unknown` rather than `false` when it did not.
            "customer_id",
            "whitelisted",
        ),
        lambda r: [
            r["id"],
            r["label"],
            r["rail_family"],
            r["recipient_type"],
            r["destination_country"],
            # `None` is an unreadable row, not an empty one — the list says so
            # on screen and the file says so here.
            "unreadable" if r["recipient"] is None else cp.identify(r["recipient"]),
            (r["recipient"] or {}).get("legalName"),
            # **Only where the screen shows it.** The per-customer Contacts
            # page has a *Saved by* column; the cross-customer `/contacts` does
            # not, so an unscoped file would be handing out staff email
            # addresses the view it mirrors never displayed. The column itself
            # stays — a saved spreadsheet formula must not break because a
            # column moved — and is simply empty.
            r["created_by"] if r.get("scoped") else "",
            _iso(r["created_at"]),
            _iso(r["updated_at"]),
            r["customer_id"],
            r.get("whitelisted", "unknown"),
        ],
    ),
}

LOCAL_SURFACES["batch_rows"] = (
    # One batch's results. Local by nature: the rows are this
    # console's record of a file it validated, and their outcomes are the
    # operations ledger's — Conduit is never asked anything to build this file.
    batches.list_filters,
    (
        "row",
        # The row's own purpose (multi-purpose batches): a results file whose
        # rows may each be a different kind of payment has to say which each was,
        # and the correction path — fix those rows, upload a new batch — needs
        # the column back.
        "purpose",
        "contact",
        "recipient_masked",
        "legal_name",
        "amount",
        "asset",
        "state",
        "transaction_id",
        "operation_id",
        "problem",
    ),
    lambda r: [
        r["row_number"],
        r["purpose"],
        r["contact_label"] or r["contact_id"],
        # The screen's masking, exactly (`counterparties.identify`): a results
        # file is the easiest artefact in this feature to forward to a finance
        # team, and it gets no weaker policy than the page.
        # Three states, not two: a purged destination is not a missing one, and
        # the file is read further from the page than anything else here.
        "destination purged after retention"
        if r["purged"]
        else ("unreadable" if r["unreadable"] else cp.identify(r["recipient"])),
        (r["recipient"] or {}).get("legalName"),
        r["amount"],
        r["asset"],
        r["state"],
        # The payment itself, where there is one. `conduit_resource_id` on a
        # confirmed `payout_create` IS the transaction id — a payout is a
        # transaction (OPERATIONS_SPEC §6).
        r["transaction_id"],
        r["operation_id"],
        # Why a row did not become a payment, in one column: Conduit's own
        # problem title where it refused, this console's sentence where it
        # declined to send, and the first validation complaint where the row
        # never qualified.
        r["problem_title"],
    ],
)

SURFACES = tuple(CONDUIT_SURFACES) + tuple(LOCAL_SURFACES)
# Wire parameters that are not filters: the transactions list asks Conduit for a
# sort order, and a filename or an audit row that named it would be describing
# the query rather than the view.
NOT_FILTERS = ("sortBy", "sortOrder")
# The surface whose every row belongs to one customer. Without one there is no
# view to export — not an empty file, which would read as "this customer has
# none". `counterparties` used to be here and no longer is: `/contacts` is a
# real cross-customer view now, and its export is that view.
CUSTOMER_SCOPED = ("recipients",)


# --- the walk ---------------------------------------------------------------------------


async def _walk(client: ConduitClient, path: str, params: dict) -> tuple[list[dict], bool, str]:
    """Every page of one filtered list, bounded. `(rows, truncated, failure)`.

    Through `client.page`, so the retry/backoff, the typed results and the
    request accounting are the console's existing ones — nothing here touches
    httpx. `page` returns its failures as values, so a mid-walk outage ends the
    loop with the rows already read rather than raising over them.
    """
    rows: list[dict] = []
    cursor: str | None = None
    pages = 0
    failure = ""
    while True:
        result = await client.page(path, cursor=cursor, limit=WALK_LIMIT, **params)
        pages += 1
        if not isinstance(result, Page):
            failure = (
                f"{result.status} {result.title}"
                if isinstance(result, Problem)
                else "Conduit is unreachable"
            )
            break
        rows += result.items
        cursor = result.next_cursor
        if not cursor or pages >= CAP_PAGES or len(rows) >= CAP_ROWS:
            break
    truncated = not failure and (bool(cursor) or len(rows) > CAP_ROWS)
    return rows[:CAP_ROWS], truncated, failure


# How many console-linked payouts one contact-filtered file reads. Each id is
# its own `GET /v2/transactions/{id}`, so this is a request budget, not a row
# budget — two orders of magnitude under `CAP_ROWS` for that reason, and stated
# in the file when it is reached.
CONTACT_CAP = 500
CONTACT_CHUNK = 25


async def _by_contact(
    session: AsyncSession,
    client: ConduitClient,
    customer_id: str,
    contact_id: str,
    notes: list[str],
) -> tuple[list[dict], bool, str]:
    """The console-linked payouts to one contact, read by id. `(rows, truncated, failure)`.

    **Driven by the trail, exactly like the screen**.
    This used to walk Conduit's transaction list and intersect it with the trail's
    ids, which can only ever return what the walk happened to contain: an
    audit-linked transaction the list did not answer for was silently missing from
    the file while `transactions._by_contact` showed it on screen as a row that
    could not be read. A file quietly shorter than the page it is exported from is
    the failure this whole filter exists not to commit.

    A read that fails is a row carrying the id and nothing else, plus the honest
    INCOMPLETE note — never a dropped row. Gathered in chunks so a large contact
    history is bounded concurrency rather than five hundred simultaneous reads.
    """
    ids = await cp.linked_transactions(
        session, customer_id=customer_id, contact_id=contact_id, limit=CONTACT_CAP + 1
    )
    truncated = len(ids) > CONTACT_CAP
    ids = ids[:CONTACT_CAP]
    rows: list[dict] = []
    unread = 0
    for start in range(0, len(ids), CONTACT_CHUNK):
        chunk = ids[start : start + CONTACT_CHUNK]
        for tid, found in zip(
            chunk,
            await asyncio.gather(*(payments.fetch_transaction(client, i) for i in chunk)),
        ):
            if isinstance(found, dict):
                rows.append(found)
            else:
                unread += 1
                rows.append({"id": tid})
    if unread:
        notes.append(
            f"# EXPORT INCOMPLETE — {unread} of these transactions could not be read from "
            "Conduit. Their rows carry the id and nothing else; they are linked payments, "
            "not missing ones."
        )
    return rows, truncated, ""


async def _local_rows(
    session: AsyncSession,
    client: ConduitClient,
    surface: str,
    filters: dict,
    notes: list[str],
) -> tuple[list, bool]:
    """The local surfaces' equivalent of the walk: one SELECT, same row cap.

    The *whole* filtered set, not the screen's first hundred: an export that
    stopped where the page stops would be a page, and the operator already has
    that one.
    """
    if surface == "batch_rows":
        # A batch is a set, not a filtered view: the file is all of it, and the
        # row cap of the upload (500) is an order of magnitude under this one.
        # A batch id that is not this customer's is a 404 rather than an empty
        # file — "this batch has no rows" is a different claim, and the wrong one.
        found = await batches.results(session, filters["customerId"], filters["batchId"])
        if found is None:
            raise HTTPException(404, "No such batch for this customer.")
        return found[:CAP_ROWS], len(found) > CAP_ROWS
    if surface == "accounts":
        found = (
            (
                await session.execute(
                    select(Projection)
                    .where(*accounts.where(filters))
                    .order_by(accounts._observed().desc())
                    .limit(CAP_ROWS + 1)
                )
            )
            .scalars()
            .all()
        )
        rows = [accounts.row_of(projection) for projection in found]
        # The page's names, in the file. The SAME bounded read the
        # page makes — one call for the whole export, never one per row — so the
        # column resolves exactly the customers the screen resolves and no more.
        # The file states which of the two truths a blank cell is, because a CSV
        # cannot wear a banner: nothing resolved it, or nothing could be read.
        directory = await client.page("/v2/customers", limit=NAME_LIMIT)
        names = customer_names(directory.items) if isinstance(directory, Page) else {}
        for row in rows:
            row["customer_name"] = names.get(row["customer_id"], "")
        notes.append(
            NAMES_UNREAD_NOTE if not isinstance(directory, Page) else NAMES_BOUND_NOTE
        )
    else:
        # The cap goes in the query, not in the slice below: every contact row
        # costs a Fernet decrypt, so fetching a whole large address book to
        # throw most of it away would spend the budget this cap exists to
        # bound.
        #
        # Name **or** id, resolved exactly as the page resolves it —
        # one parser and one resolver, so the file and the screen cannot disagree
        # about which customers "acme" meant.
        ids, _matched, name_capped, directory_problem = await contacts.resolve_customers(
            client, filters["customerId"]
        )
        if directory_problem is not None:
            # An outage is not an absence. Refusing with the
            # *right* reason matters here more than on the page: an operator who
            # is told "no customer matches" edits the name and tries again, and
            # the name was never the problem.
            raise HTTPException(
                502,
                "The customer directory could not be read, so this name could not be "
                "resolved and this file would not be the view you asked for. Try again.",
            )
        if filters["customerId"] and not ids:
            raise HTTPException(
                400,
                "No customer matches that name, so this file would be the unfiltered view "
                "rather than an empty answer. Refine the name, or paste the id.",
            )
        # **A capped walk cannot prove a single match is the only match**, so
        # it does not earn the scoped view — the whitelist
        # join and the capability filter both mean "this customer's, all of
        # them". The rows are the same either way; what changes is what the file
        # is allowed to claim about them.
        customer_id = ids[0] if len(ids) == 1 and not name_capped else ""
        if name_capped:
            notes.append(
                f"# SEARCH CAPPED — the customer-name search stopped at "
                f"{customers.NAME_WALK_PAGES * customers.NAME_WALK_LIMIT} customers, so there "
                "may be matching customers whose contacts are not in this file."
            )
        if filters["capability"] in contacts.NEEDS_CUSTOMER and not customer_id:
            # The page states this on screen (`unanswerable`) and shows the rows
            # unfiltered; a file cannot wear a banner, and its name and audit row
            # would both claim a filter that never ran.
            raise HTTPException(
                400,
                "A whitelist capability is answerable for one customer at a time — Conduit "
                "lists a whitelist per customer. This name resolved to "
                f"{len(ids)} of them{' (and the search was capped)' if name_capped else ''}: "
                "export one customer, or export without that filter.",
            )
        rows = (
            await cp.rows(
                session, customer_id, family=filters["family"], limit=CAP_ROWS + 1
            )
            if customer_id
            else await cp.everyones(
                session,
                family=filters["family"],
                customer_ids=ids or None,
                limit=CAP_ROWS + 1,
            )
        )
        # The sentinel row is what the cap is *for*, so it has to be read before
        # it is thrown away: this branch fetched `CAP_ROWS + 1`, sliced, and
        # returned `False` — a short file whose audit row said it was whole
        # (the accounts branch below never lost it).
        # The capability filter runs after the cap on purpose: the cap bounds the
        # *decrypts*, not the matches, and a capped file says TRUNCATED whatever
        # the filter then leaves in it.
        truncated = len(rows) > CAP_ROWS
        rows = rows[:CAP_ROWS]
        if filters["contactName"]:
            # The page's own matcher, over the label and the decrypted legal
            # name. Unreadable rows cannot match, and the note says so rather
            # than the file being quietly shorter than the screen.
            unreadable = sum(1 for row in rows if row["recipient"] is None)
            rows = [row for row in rows if contacts.name_matches(row, filters["contactName"])]
            if unreadable:
                notes.append(
                    f"# SCOPE — {unreadable} contact(s) could not be decrypted and were "
                    "therefore not matched against this name."
                )
        for row in rows:
            row["scoped"] = bool(customer_id)
        if customer_id:
            # One whitelist read for the whole file, exactly as the page makes
            # one for the whole screen — never one per row. The capability is
            # written per row from `same_entity`, the transfer gate's own test,
            # so the column says what the badge says.
            entries, problem, capped = await contacts._whitelist(client, customer_id)
            for merged in cp.merge(rows, entries):
                saved = merged["saved"]
                if saved is None:
                    continue
                # Three states in the column too: `unknown` when the read
                # failed, when the walk stopped short of the whole whitelist, or
                # when two saved records share this destination and nothing can
                # say which of them is registered. `false` is a claim, and none
                # of those three earns it.
                saved["whitelisted"] = (
                    "unknown"
                    if problem is not None or capped or merged["ambiguous"]
                    else (merged["entry"] or {}).get("status") == payments.USABLE_STATUS
                )
            if problem is not None:
                if filters["capability"] in contacts.NEEDS_CUSTOMER:
                    # The filter's answer lives in the half that failed. Refused
                    # rather than applied to a `False` nobody established — the
                    # same refusal the unscoped case already gets.
                    raise HTTPException(
                        400,
                        "This customer's whitelist could not be read, so a capability filter "
                        "has no answer — export without it, or try again.",
                    )
                notes.append(WHITELIST_UNREAD_NOTE)
            elif capped:
                notes.append(WHITELIST_CAPPED_NOTE)
            if filters["capability"] in contacts.NEEDS_CUSTOMER:
                # Refused above when it cannot be resolved, so reaching here means
                # the answer is real. Every row in this file is a saved record by
                # construction, so `saved` filters nothing and the other two are
                # the same question: does Conduit call it registered.
                rows = [row for row in rows if row["whitelisted"] is True]
        return rows, truncated
    return rows[:CAP_ROWS], len(rows) > CAP_ROWS


# --- the file ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def filename(surface: str, filters: dict) -> str:
    """`transactions_type-withdrawal_status-pending_20260831T104500Z.csv`.

    The surface, what was filtered, and when — because the operator who finds
    three of these in a downloads folder next month has no other way to tell
    which view produced which. Everything outside `[A-Za-z0-9._-]` collapses to
    `-`: the name is a header value and a filename on somebody's disk, and a
    Conduit id with a slash in it must be neither a path nor a second header.
    """
    parts = [surface]
    for key, value in sorted(filters.items()):
        values = [v for v in (value if isinstance(value, (list, tuple)) else [value]) if v]
        if values:
            parts.append(f"{key}-{'.'.join(str(v) for v in values)}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{_UNSAFE.sub('-', '_'.join(parts))[:120]}_{stamp}.csv"


def _render(rows: list, build) -> tuple[list[list[str]], str]:
    """Every row turned into cells **before** anything is audited or streamed.
    `(cells, the reason the rest are missing)`.

    Building lazily inside the response generator was a way for the file and the
    audit row to disagree: `build` would run *after* the
    audit row committed and after the headers had gone out, so one Conduit item
    shaped in a way no builder anticipated — an order whose `sourceAsset` is a
    string rather than an object — truncated the download mid-flight while the
    ledger recorded a complete export. Nothing may claim a row left this console
    until the row exists.

    The walk is already bounded, so materialising costs nothing the cap did not
    already allow. `Exception` is caught broadly on purpose: this is the trust
    boundary where an arbitrary remote payload meets a builder, and the honest
    outcome is the partial file this console already knows how to write — not a
    500 halfway through a download.
    """
    cells: list[list[str]] = []
    for item in rows:
        try:
            cells.append([_cell(value) for value in build(item)])
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.exception("export: row %s could not be built", len(cells) + 1)
            return cells, f"{type(exc).__name__} on row {len(cells) + 1}"
    return cells, ""


def _stream(
    headers: tuple, cells: list[list[str]], notes: list[str], preamble: Sequence[str] = ()
):
    """The CSV itself, a row at a time.

    `csv.writer` over a rewound buffer rather than string joining: quoting,
    embedded commas, newlines and doubled quotes are the standard library's
    problem, and the injection rule was applied per cell in `_render`.

    `preamble` is the batch template's `#` header block —
    rows *above* the header, where `notes` are rows below it. Same writer, same
    quoting, same `_cell`, so the one file this console asks an operator to fill
    in and send back is written by the machinery that writes the ones it only
    hands out.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def flush() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return text

    for line in preamble:
        writer.writerow([_cell(f"# {line}")])
        yield flush()
    writer.writerow(headers)
    yield flush()
    for row in cells:
        writer.writerow(row)
        yield flush()
    for note in notes:
        # Its own row, first cell: a spreadsheet shows it, and a script reading
        # the file sees a row that is not a record.
        writer.writerow([_cell(note)])
        yield flush()


@router.get("/export/{surface}.csv")
async def export(
    request: Request,
    surface: str,
    session: AsyncSession = Depends(db),
    client: ConduitClient = Depends(conduit),
    actor: Actor = Depends(require("export.csv")),
) -> StreamingResponse:
    """One filtered view as CSV. Read-only, viewer-visible, always audited."""
    if surface not in SURFACES:
        raise HTTPException(404, "No such export surface.")
    query = request.query_params
    if surface in CUSTOMER_SCOPED and not _customer(query):
        raise HTTPException(400, "This export is about one customer, and none was named.")
    if surface == "counterparties" and not _customer(query):
        # The whitelist half is a per-customer read, so a capability the file
        # cannot resolve is refused rather than answered with a file that
        # silently ignored the filter it is named after.
        if (query.get("capability") or "").strip() in contacts.NEEDS_CUSTOMER:
            raise HTTPException(
                400,
                "Whitelist capability is a per-customer read — name a customer, or export "
                "without that filter.",
            )

    notes: list[str] = []
    if surface in CONDUIT_SURFACES:
        path, parse, headers, build = CONDUIT_SURFACES[surface]
        wire = parse(query)
        # The audit row and the filename say what the view was *filtered* to.
        # A sort is not a filter, and a customer-scoped surface carries its
        # scope in the path rather than on the wire — so both are corrected
        # here, and neither goes anywhere near `_walk`.
        filters = {k: v for k, v in wire.items() if k not in NOT_FILTERS}
        if surface in CUSTOMER_SCOPED:
            filters["customerId"] = _customer(query)
        if surface == "transactions" and isinstance(wire["type"], list):
            # The page's parser is the only thing that decides this: a list is
            # what `transactions.list_query` returns for the All view, and the
            # export inherits it rather than re-deciding what "all" means.
            notes.append(MULTI_KIND_NOTE)
        contact = transactions.contact_of(query) if surface == "transactions" else ""
        if contact:
            # Scoped to this customer, archived included: archiving retires a
            # contact from the pickers, not the history of what it was paid
            #. A contact that is not this customer's is still
            # refused — a file that silently became the unfiltered view is worse
            # than no file.
            if await cp.get(
                session, wire["customerId"] or "", contact, include_archived=True
            ) is None:
                raise HTTPException(
                    400,
                    "That contact is not this customer's — so this file would silently be "
                    "the unfiltered view.",
                )
            rows, truncated, failure = await _by_contact(
                session, client, wire["customerId"] or "", contact, notes
            )
            filters["contact"] = contact
            notes.append(CONTACT_NOTE)
        elif surface == "customers" and (name := customers.name_of(query)):
            # **The page's own name walk, not this module's cursor walk.**
            # `?name=` is not a Conduit parameter — the directory
            # page walks the list and matches locally — so `list_query` rightly
            # omits it, and the export inherited a walk that returned *every*
            # customer while the operator believed they were exporting a search.
            # One search on both surfaces, per the standing page-and-file rule.
            matched, capped, problem = await customers.walk_named(client, name, wire)
            rows, truncated = matched, capped
            failure = problem["title"] if problem else ""
            # The filter that shaped the file, named where every other one is.
            filters["name"] = name
            if capped:
                notes.append(
                    f"# SEARCH CAPPED — this name search stopped at "
                    f"{customers.NAME_WALK_PAGES * customers.NAME_WALK_LIMIT} customers, the "
                    "same ceiling the page states. There may be further matches beyond it."
                )
        else:
            rows, truncated, failure = await _walk(client, path(query), wire)
        if failure:
            notes.append(
                f"# EXPORT INCOMPLETE — Conduit stopped answering after {len(rows)} rows "
                f"({failure}). This file is partial; export again."
            )
    else:
        parse, headers, build = LOCAL_SURFACES[surface]
        filters = parse(query)
        rows, truncated = await _local_rows(session, client, surface, filters, notes)
        failure = ""
        if surface == "counterparties" and not filters["customerId"]:
            notes.append(SAVED_BY_NOTE)
        if surface == "counterparties" and filters["customerId"]:
            # The scoped Contacts page merges Conduit's registrations in and
            # counts them; this file is the saved half. One row of a one-contact
            # page could otherwise export as a header and nothing else, with
            # nothing in the file saying why.
            notes.append(SAVED_ONLY_NOTE)

    # The rows become cells *here* — before the audit row and before the
    # response — so that every ending below is a fact rather than a prediction.
    cells, bad_row = _render(rows, build)
    if bad_row:
        notes.append(
            f"# EXPORT INCOMPLETE — a row this console could not read ({bad_row}) ended the "
            f"file after {len(cells)} rows. This file is partial; report it and export again."
        )
        # The rows after the bad one were never rendered, so whatever the walk
        # thought about reaching the end of the list is no longer what this file
        # is: "incomplete" is the whole answer, and TRUNCATED would be a second,
        # narrower claim about a set this file does not contain.
        truncated = False
    if truncated:
        notes.append(TRUNCATED_NOTE)
    failed = "; ".join(part for part in (failure, bad_row) if part)

    # Before a single byte goes out: this is the PII-egress record, and a
    # download the reader abandoned is still a read that happened. Every number
    # in it is counted off `cells` — what actually leaves.
    audit.record(
        session,
        action="export.csv",
        actor_id=actor.id,
        actor_email=actor.email,
        detail={
            "surface": surface,
            "filters": {k: v for k, v in filters.items() if v},
            "rows": len(cells),
            "truncated": truncated,
            **({"failed": failed} if failed else {}),
        },
    )
    await session.commit()
    return StreamingResponse(
        _stream(headers, cells, notes),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename(surface, filters)}"'},
    )
