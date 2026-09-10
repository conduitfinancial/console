# Release notes

What changed between tags, in plain language, for someone deciding whether to
upgrade.

---

## v1.5.0

The virtual-account currency picker stopped being a list this console kept
and started being a question it asks Conduit, per customer, on every visit.

### New in accounts

**The picker shows what Conduit allows this customer, not what this build
happened to ship with.** The currency options themselves do not move for the
production customer this was verified against: on 2026-09-07 the list comes
back `EUR, USD` — the same pair the old constant held. That is the point. The
pair is now Conduit's answer rather than this console's copy of it, so the day
a provider is added the picker carries the new currency without a release, and
the day one is withdrawn it stops offering it.
`GET /customers/{id}/features/requirements` had been carrying that list on its
`/asset/code` field all along whenever `asset` was left off the call; the
picker simply wasn't reading it. One caveat worth holding onto: the list is the
*schema's*, not a live eligibility check — EUR sits on it and still refuses at
request time on an org with no EUR provider.

**What does look different.** The up-front per-currency refusal probing is
gone, a customer with no accounts is now offered a button to request their
first, and the sandbox deposit simulator offers — and now sends — only the
account's own currency.

**One discovery call instead of one per currency.** The old picker built
itself by probing each known currency separately and keeping what didn't
fail; the new one asks once, with the asset left off entirely, and gets the
whole answer back. The page that used to cost one request per currency now
costs one request, full stop.

**Convert still offers USD and EUR only, and that's deliberate, not a
leftover.** Which currencies an account can *hold* and which pairs Conduit
will *quote a conversion between* are different questions with different
answers; this release only changed the first one. Convert keeps its own
list of eligible pairs and keeps naming just USD↔EUR until Conduit's
conversion side grows more.

### One thing to notice

**The page no longer names every refusal before you ask.** The old
per-currency probing named refusals up front: every hardcoded currency was
probed on every visit, so a refused one was still listed, muted, with its
refusal title beside it — never silently missing. The new catalog call
returns only what this customer is eligible for, so an ineligible currency
just isn't in the list and nothing names it up front. Choosing one
explicitly still probes it and still shows Conduit's own `422
NO_ELIGIBLE_PROVIDER`, with its `resolution` text, beside the picker — the
refusal is one ask away now, not handed to you unasked.

---

## v1.4.0

The console's audience was reopened this release, not just relabelled: not
"a handful of ours" but trained, daily, high-volume treasury teams at client
organizations, scanning hundreds of rows a session. Arca — the design
direction — ships against that audience in four slices, A0 through A4: two
real themes, a shell that finally carries its own weight, failure pages that
live inside the chrome instead of showing the framework's own, and the two
busiest pages rebuilt around what that audience actually does with them. At
A4: 2159 unit + 70 browser; final at tag.

### New in the product

**Light and dark, and a control that means it.** A System/Light/Dark segment
sits in the environment strip. System is the *absence* of a stamped
preference, not a one-time snapshot of what the machine said at load — it
keeps tracking `prefers-color-scheme` for as long as it's selected. It is set
before first paint by `static/theme.js`, so there is no flash of the wrong
theme to cover up.

**A shell that is a real element.** The page is a 1180px card — ribbon
included — on a page ground, with the ribbon's mobile collapse fixed by
deleting the rule that broke it rather than patching around it: below 60rem
the nav is one full-width column again, each group head sitting above its
own items. The skip link is restored, anchored to the card.

**The environment badge, quieter.** It now names the environment and the
`MONEY_CEILING` only — `Sandbox · ceiling 5000`, `Staging`, `Production`.
Host and `AUTH_MODE` haven't gone away; they moved into the badge's tooltip.
An operator who used to read the host straight off the badge now has to
hover for it.

**Failure pages live in the chrome.** `403`, `404`, `500` — and, because
`app/web/exports.py` alone raises a dozen of them, `400` and `409` too —
render inside the ribbon in the console's own voice, with exactly one way
on. Upstream Conduit problems are translated at the single boundary that
turns an error body into a `Problem`, through a table pinned against every
one of the spec's 155 machine codes, so a code the table has never seen
before still fails the build rather than shipping unnoticed. A `403` names
the permission it's missing. Form errors bind to their own control with
`aria-invalid` and `aria-describedby`, including every input of a radio or
checkbox group, which a wrapper `<div>` never announced.

**The ledger reads like a page, not a printout.** Hero-size figures are now
a class applied to summary numbers only — the Overview band, a
transaction's own amount — never a table cell, which is a rule a grep can
hold and a descendant selector couldn't. Roughly ninety words of teaching
prose that sat between a filter bar and its first row on every visit move,
word for word, into a closed `<details>` beside the filters. The
transactions ledger now answers "what happens next" for `pending`,
`processing`, `failed` and `cancelled` rows in the status cell itself,
clamped to two lines so a page of repeats keeps its density; `completed` and
an unknown status get no sentence, because there is nothing to say.

**Every table scrolls instead of overflowing.** All 44 tables sit in one shared wrapper that does nothing above 1024px, so sticky table heads keep working, and scrolls horizontally at and below it. The long-tail re-skin sweep found nothing left to change: every page already renders through the retokened shared classes. Four more pages demote their teaching prose to the closed disclosure.

### Stricter than v1.3.0 — read before upgrading

**The environment badge no longer prints the API host.** It reads
`Sandbox · ceiling 5000`, `Staging` or `Production`; the host and `AUTH_MODE`
moved into the badge's tooltip. A monitor, screenshot diff or test that matched
the hostname on the page will stop matching. Hover the badge, or read
`CONDUIT_ENV` from the deployment, instead.

**`static/theme.js` is a new blocking script in `<head>`.** It has to run
before first paint to stamp the theme without a flash of the wrong one, so
it cannot be deferred or async'd like `app.js`. It is same-origin, served
from this app like every other static file, so the existing
`script-src 'self'` Content-Security-Policy covers it with nothing to
allowlist — but a deployment that strips or overrides that header needs to
know a second script now runs in the head, not just `app.js` at the foot of
the body.

**The primary button is accent-filled, not the previous fill.** Any
deployment carrying its own override CSS against the old primary-button
colour will render that override against a different base and should expect
it to look different, not broken.

**The `403` page's body changed shape.** It now names the missing
permission instead of the bare page it used to render. Nothing that parses
or scrapes a `403` response for its old wording will still match.

**One thing that did not move: webhook receiver error bodies.** They stay
JSON. None of the failure-page work in this release touches
`/webhooks/conduit` or any other machine-consumed response — the chrome
changes are for pages a person reads in a browser.

### For reviewers

The upstream-problem translation table and its 155-code drift pin, the
`aria-invalid`/`aria-describedby` field binding, and the contrast recompute
that backs both themes are all new surface for a fresh pair of eyes.
`KNOWN_ISSUES.md` § (b) has the one design-time ceiling this release knows
about and is accepting for now (the dark-theme accent ring's contrast margin;
the ledger overflow A4 recorded was closed by A5's wrapper), and § (c) names
the new test files worth reading before trusting them:
`tests/test_contrast.py`, `tests/test_failure_paths.py`,
`tests/test_table_scroll_wrapper.py` and `tests/browser/test_a5_table_scroll.py`.

---

## v1.3.0

A fourth quick action, a friendlier payout form, and the console's type set
in Conduit's own faces. 1987 unit + 57 browser.

### New in payments

**Pay a contact.** A fourth quick action opens a names-first picker over
every live saved contact and lands on the existing payout form with the
recipient prefilled — and, when the last payment to that contact can be
read back from Conduit, the amount, funding account and purpose too. The
read-back is bound by contact id, never by the label shown on screen.

**Contact-first payouts.** The single payout form now offers a customer's
saved contacts before the route; choosing one implies the route and leaves
the purpose open. **Send a payout** and **Pay a contact** are two doors to
the same room — the ribbon answers "I know what," the picker answers "I
know who" — and the general form's contact step contains the special case,
so nothing is missing either way in.

**Cloning a contact.** A contact's edit page can save a new contact from it
with a new label, a different rail, and edited coordinates, as a plain
insert — nothing about the source contact is touched.

### Design pass

**Every list filter applies on change.** All eight filter trays (contacts,
customers, transactions, accounts, applications, orders, RFIs, drafts) now
re-filter as soon as a field changes, instead of waiting for the Filter
button — which still works, unchanged, for anyone without JavaScript.

**List-page ledes read wider.** A lede sentence above a table now uses the
full 88ch measure instead of the 62ch form-reading width, so it no longer
reads as a narrow column stranded beside empty paper on a wide screen.

### Type

**The console is set in Conduit's own type.** Headings in Founders
Grotesk, body in DM Sans, values and eyebrows in JetBrains Mono, all at the
one weight Conduit's own site actually renders. Founders Grotesk is a
commercial face and is **not shipped** — `static/fonts/
founders-grotesk-{regular,medium}.woff2` are expected files; until they are
dropped in, headings render in DM Sans with no code change required
(`deploy/README.md` § Fonts). Archivo is removed.

Nothing here is stricter than v1.2.1: `GET /payouts/contact` is a new,
additive route onto the existing payout form, and the font swap degrades
gracefully (`--font-heading` falls through to DM Sans when the licensed
files are absent) — an install that never adds the Founders Grotesk files
renders exactly as before, just relabelled.

---

## v1.2.1

LICENSE names Conduit Financial as the copyright holder. No code change.

---

## v1.2.0

Ten of the eleven items v1.1.0's KNOWN_ISSUES.md listed are closed; the file now
asks reviewers to review the fixes. Full unit suite 1933, browser 53.

### Stricter than v1.1.0 — read before upgrading

**Everyone signs in once, at the upgrade.** Session cookies now record when they
were issued, which is what makes a single operator's sessions revocable without
rotating `SESSION_SECRET` and signing out the whole team. A cookie minted before
this upgrade carries no issue time, so nothing can be compared against it — it is
treated as expired and the next page load goes to sign-in. Nothing is lost and no
action is needed; it happens once. (`AUTH_MODE=oidc` only — in proxy mode the
console holds no session.)

**A secret containing the word `placeholder` is now refused outside sandbox.**
The startup check matches literal placeholder markers, and `placeholder` is one
of them — a secret that merely contained the word "secret" was already exempt
(v1.1.0), but one containing the literal text "placeholder" was not, and never
should have been. A secret that booted v1.1.0 with that text in it will not
boot v1.2.0; generate a real one.

**`/health/live` no longer carries `environment`.** The body is just
`{"status":"ok"}`. A monitor or alert matching on the `environment` field needs
to change before this upgrade, not after.

**Migration `0013` is not rolling-safe** — a `web` replica still on v1.1.0
during the roll can create operations invisible to the new nonce guard; see
`deploy/README.md` §4 for the two ways to upgrade safely.

A nonce that reaches another kind of operation than the one it was scoped to is
now a visible `422` naming the mismatch, rather than silently missing the guard.

### New in operations

**Signing one operator out.** `scripts/revoke_sessions.py --sub <sub>` ends every
session that operator currently holds — a lost laptop, a leaver — on their next
request, with no restart and without touching anybody else. It ends sessions
rather than banning the account: they can sign in again, so remove the account
upstream too if that is what you mean. `deploy/README.md` § 3 has the procedure.

**Two more columns now age out.** Batch-payout row destinations and raw webhook
deliveries were the last stores of payee bank coordinates with no retention job.
Both are now emptied by the worker on the same tick as the existing purges — the
rows, their statuses and everything projected from them stay. `deploy/README.md`
§ 5 has the full table of what ages out and when. **One deliberate exception: a
webhook event that failed permanently keeps its body at any age**, because that
body is the only evidence of why it failed; clear failed rows rather than letting
them accumulate.

### Money-path fixes

**A consumed intent nonce lands on the same operation forever.** Every nonce
that reaches an operation — by creating it, or by being resolved onto it by the
request-hash guard — is now recorded in `operation_intents` in the same
transaction (migration `0013`, with a backfill: not rolling-safe — deploy
`web` non-rolling, or re-run the idempotent backfill once after the roll
completes; `deploy/README.md` §4 has both options). A lost redirect re-posting
after the operation settled
can no longer mint a second idempotency key. The lookup is scoped to the
operation type; a resubmit with a *changed* body under a spent nonce is refused
with a sentence saying the earlier one was recorded and this one was not sent.
A background poll can no longer swap a region while a form inside it is in
flight, which is the other way a nonce used to go missing mid-click.

**Batch dispatch re-reads the funding account first.** A batch whose account is
unreadable, gone, inactive, or in another currency is refused whole, with the
reason on every row and nothing written to the ledger. A second refusal replaces
the first reason rather than leaving the stale one on the page.

**The payout quote's expiry travels sealed.** Editing the hidden expiry, or
dropping the seal while keeping the claim, refuses the submit with nothing sent.

### Smaller fixes

`/health/live` no longer names the environment to unauthenticated callers. The
startup placeholder-secret check matches literal placeholders rather than the
word "secret", so a strong passphrase containing it is accepted. The 403 page
escapes its reason. A discovery-declared `number` field never widens through
`float`: integral values are sent as integers, fractional ones only when the
float round-trips exactly, otherwise the submission is refused. Docker base
images are pinned by digest, and dependabot watches `uv`, GitHub Actions and
docker weekly. A batch row whose destination has aged out says so on the page
and in the export instead of looking like it never had one.

### New settings

**`WEBHOOK_RAW_RETENTION_DAYS`** *(default `30`)*. How long a processed webhook
delivery's raw body is kept.

---

## v1.1.0

Six phases of product work plus a pre-share hardening round. Everything here is
additive to v1.0.0 — no setting changed meaning, no data migrated destructively,
and no flow was removed. Two behaviours are *stricter* than before; both are
called out below.

### New in the product

**A product tour.** First-run guidance over the real console: six steps that
point at the actual controls, keyboard-driven, honours reduced-motion, and never
touches the pages it explains.

**Configurable roles.** Every gated action is now a *named permission* rather
than a role check, and the three built-in roles (viewer, operator, admin) are
bundles over that catalog with byte-identical behaviour to before. A deployment
can define its own roles in a JSON file (`ROLES_FILE`) — config, so it lives in
your change control rather than in a runtime editor. An unknown permission name
refuses to start. `PERMISSIONS.md` is generated from the catalog, so the grid
you hand a security reviewer cannot drift from what is enforced.

**Transfer moves to the virtual-account arm.** A transfer between two of your
customers' accounts is sent as a virtual-account payout — no rail, no recipient
coordinates, no whitelist registration to keep in step. The receiving side now
shows a typed "Received via" row, so both ends of an internal transfer name the
other party.

**Convert-and-pay, and the hand-off when it cannot be one call.** Conduit has no
single call that converts and pays in one step, and this console does not
pretend otherwise. The refusal became a path: convert first, then the payout
form opens pre-filled from the order that just landed, with the amounts carried
across and the parties named rather than shown as ids.

**The Transact flows start at the action.** Send a payout, Transfer and Convert
are entry points in their own right — you pick the customer as the first field
instead of finding the customer first and the verb second.

**Batch payouts, hardened.** A batch row is either owed or answered, never both;
a template whose route requirements have since moved invalidates rather than
silently validating against a stale snapshot; the exported report says what it
left out.

**Names everywhere, lists that behave.** Every list that showed bare `cus_…` ids
now shows the customer's name beside them, list headers stay put when you
scroll, the Accounts list paginates properly, and you can search by name (the
value on the wire is still the id). The ledger unified into one feed: an **All**
tab, one read, a Type column, newest first.

### Stricter than v1.0.0 — read before upgrading

**`AUTH_MODE=disabled` is now refused with `CONDUIT_ENV=staging`.** It was
already refused in production. Staging carries a live key against a host that
self-labels "Production", so an unauthenticated console there is an
unauthenticated console over real money. If a staging deployment runs with
`AUTH_MODE=disabled`, it will not start after this upgrade — configure `proxy`
or `oidc`.

**Amounts must be plain decimals.** `amount()` used to validate through
`Decimal` and transmit the operator's raw text, which meant `1_000`, `1e3`, `+5`
and digits from non-Latin scripts all validated as numbers and went to Conduit
verbatim — the number shown and the number sent could differ. Only a plain
decimal is accepted now (`1000`, `1000.00`, `0.01`), and its canonical form is
what is transmitted and displayed. Trailing zeros are preserved; a run of
leading zeros is normalised.

A batch uploaded before v1.1.0 stored the operator's raw amount string, not the
canonical one. Those rows are unchanged by the upgrade and will dispatch what
they stored — **re-upload any batch that has not been dispatched yet** so its
amounts go through the new parser.

### New settings

**`MONEY_CEILING`** *(optional, empty by default = today's behaviour)*. A single
decimal. When set, **every operation this console creates** is refused locally,
before it reaches the operations ledger, if its amount is above the ceiling —
single payout, virtual-account transfer, each batch row, the batch total, and a
conversion order's source amount. The refusal is an ordinary form error naming
the ceiling, and the number appears on the environment strip beside the host so
a refusal is never a surprise. Per operation, not per day; and because the check
is pre-ledger, executing or retrying an operation that already exists does not
re-check it (`KNOWN_ISSUES.md` § (b) has the two routes and why). Set it before
the run it is for. Intended for a first validation run against production keys;
see `deploy/README.md` § Validation runs.

**`OIDC_CALLBACK_PATH` is now validated at startup.** It must be an absolute
path of at least two segments with no trailing slash. A shorter value (`/`, or
empty) made every path in the console anonymous, because the callback is mounted
as an anonymous prefix and the check is a prefix match.

### Security and privacy

- **Security headers** on every response: a Content-Security-Policy with
  `script-src 'self'` and no `unsafe-inline` or `unsafe-eval`, plus `nosniff`,
  `Referrer-Policy: same-origin` and `X-Frame-Options: DENY`. HSTS stays at your
  TLS terminator, which is the thing that knows whether TLS is on.
- **No `/docs`, `/redoc` or `/openapi.json`.** This is an operator console, not
  an API.
- **The retention purge now drops `error` as well as `request_body`.** Conduit's
  problem details echo submitted field values back, so a purge that kept the
  error kept the coordinates it was meant to drop.
- **Logging out actually logs you out.** Both cookies were being deleted with
  `Secure` off, and a browser rejects a `__Host-` prefixed cookie without it —
  so wherever `COOKIE_SECURE=true` (everything but sandbox) the session cookie
  survived logout and the session stayed live until it expired on its own. The
  deletions now carry the same flags the cookies were set with.
- **Logging out clears the CSRF cookie too**, not only the session cookie.
- **PII at rest, widened:** projections are minimised to a keep-list, raw
  webhook bodies are encrypted, and the scrub covers identity fields, not only
  bank coordinates.
- **The Conduit client refuses path traversal and query-splitting** in any path
  it is asked to fetch.
- **A test fixture that carried a real bank's routing and account number** has
  been replaced with the same mock-bank shape its EUR sibling already used.

### For reviewers

`KNOWN_ISSUES.md` is new: what we want a second pair of eyes on, what is known
and accepted with the reasoning, and what has already been verified so nobody
re-litigates it.

---

## v1.0.0

The first release: onboarding through requirements discovery, USD and EUR
virtual accounts, payouts driven end to end by `GET /v2/payouts/requirements`,
same-currency transfers, USD↔EUR conversions, batch payouts by CSV, a webhook
inbox with a reconciler, and an operations ledger where an ambiguous mutation
lands as `outcome_unknown` and resolves itself.

Tagged `v1.0.0`; `git show v1.0.0` for the commit.
