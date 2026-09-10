# Known issues — start here

You are reviewing Conduit Console. This file is the honest map: what we are
unsure about, what we know and have accepted, and what has already been checked
so you do not spend your afternoon on it.

The console moves real money on Conduit's API. Its own claim is narrow: it owns
the operator's **intent** (an operations ledger with idempotency keys, so a
mutation happens once) and the operator's **work in progress** (encrypted
onboarding drafts). Everything else is read back from Conduit. Most of what
follows lives at that boundary.

Orientation: `README.md` (what it is), `deploy/README.md` (running it),
`OPERATIONS_SPEC.md` (the ledger — read this before the money paths),
`FORM_ENGINE_SPEC.md` (the discovery-driven forms), `PERMISSIONS.md` (the
authz grid), `IMPLEMENTATION_PLAN.md` (architecture).

---


## (a) Fixed since v1.1.0 — please review the fixes, not the questions

v1.1.0 shipped three open questions and eight accepted trade-offs; v1.2.0
closes ten of them. Each fix is small, pinned, and was passed through
a fresh-context review that found one real defect (the intent-claim
guard was dead code on this driver stack — `rowcount` is -1 on a no-op insert;
fixed with `RETURNING`, pinned both race orders). The money-path fixes are
where we most want your eyes.

1. **A consumed intent nonce now lands on the same operation forever.**
   `operation_intents` records every nonce that reached an operation, whether
   it created the row or was resolved onto it by the request-hash guard, in the
   same transaction (`app/operations/service.py`, migration
   `0013_operation_intents.py` with a backfill). A lost-redirect re-POST after
   the operation goes terminal finds its nonce and cannot mint a second
   idempotency key. The lookup is scoped to the operation type; a nonce that
   reached another kind of operation is refused, never filtered. A resubmit with
   a *different* body under a spent nonce is refused with a sentence saying the
   earlier one was recorded and this one was not sent. Pins:
   `tests/test_operations.py` (`…bound_to_that_operation_forever`,
   `…claimer_commits_first…`, `…creator_commits_first…`,
   `…fresh_nonce_after_a_refusal_is_still_a_new_operation`),
   `tests/test_web_payouts.py` (`…changed_amount_says_it_was_not_sent`).
   Companion: a 15s poll can no longer swap a region while a form inside it is
   in flight (`static/app.js` `pollBlocked`; `tests/browser/test_journeys.py`),
   which closes the window where a poll re-minted the nonce mid-click.
2. **Batch dispatch re-reads the funding account** before the loop and refuses
   the whole batch — no operation row, no discovery reads — when the account is
   unreadable, gone, not active, or in another currency (`app/batches.py`;
   `tests/test_batch_dispatch.py`). Not a lock: a closure mid-loop surfaces as
   per-row Conduit rejections by design.
3. **The payout quote's expiry travels sealed** with the session-secret HMAC the
   convert flow already uses; a tampered or missing seal refuses the submit with
   zero ledger rows (`app/web/payouts.py`; `tests/test_web_payouts.py`). The seal
   binds authenticity, not identity — a no-quote submit is still allowed where
   the flow allows one today.
4. **Retention** now purges dispatched batch rows' payloads and processed webhook
   raw bodies (`WEBHOOK_RAW_RETENTION_DAYS`, default 30) on the worker tick; a
   purged batch row says so on the page and in the export
   (`tests/test_worker.py`, `tests/test_batches.py`).
5. **Session revocation without rotating `SESSION_SECRET`:** sessions carry
   `iat`; `scripts/revoke_sessions.py --sub <sub>` refuses every session issued
   before now for that operator (OIDC mode only — proxy mode holds no session).
   Cookies minted before v1.2.0 read as expired once. Boundary is `<=` on the
   revocation second; the two clocks are the script host's and the web host's
   (`app/auth/revocation.py`; `tests/test_auth_oidc.py`).
6. **Small ones:** `/health/live` no longer names the environment; the
   placeholder-secret check matches literal placeholders, not the word
   "secret"; the denied page escapes its reason; a discovery-declared `number`
   never widens through `float` (integral → `int`, fractional → `float` only
   when it round-trips exactly, else refused); Docker base images are pinned by
   digest and dependabot watches `uv`, actions and docker.

## (b) Known, accepted for now — and why

- **`MONEY_CEILING` is a pre-ledger guard, so two paths get past it.** It
  refuses an amount at the moment this console *creates* an operation, which is
  the only moment it holds the amount. Two routes act on an operation that
  already exists and therefore never re-check:
  `POST /orders/{id}/execute` executes any pending order — including one
  created before the ceiling was set, or created outside this console — and
  `POST /operations/{id}/retry` replays a stalled operation's stored body
  byte-for-byte, which is the whole promise of a retry. Both are by design: set
  the ceiling *before* the run it is for, and treat it as a bound on what this
  console will start, not on what Conduit will accept.
- **A permanently failed webhook event keeps its raw body.** It is the only
  evidence of why the event was poison; `deploy/README.md` §5 has the query to
  find them and the key-rotation caveat.
- **Revocation rows are never garbage-collected.** One small row per revoked
  operator. Not worth a job at this scale.
- **Session revocation is OIDC-only.** In proxy mode the upstream owns sign-out
  and re-authenticates every request; there is nothing here to revoke.
- **The seal on a payout quote is not bound to the route or amount.** Dropping
  both quote fields still submits, which the flow allows today; binding the
  seal is the upgrade path for whatever slice makes a quote mandatory.
- **`payout_batch_rows.errors` is not purged.** The column is plaintext JSONB
  and outlives the encrypted `payload` its row was purged of, so what a
  validator writes there is permanent and readable without `ENCRYPTION_KEY`.
  The old entry said validator messages are constants; three of them were not
  — the unknown-contact and unknown-purpose refusals and the
  allowedValues message quoted the typed cell verbatim, so a mis-pasted account
  number was stored, and exported in the results CSV, in full. Those three now
  quote at most `counterparties.mask`'s four-character tail, and the purpose
  refusal quotes a key whole only when it is one of Conduit's own published
  purposes. Accepted on that basis, and only on it: **a new validator message
  that interpolates an operator-typed cell puts the exposure straight back**,
  and the retention job cannot answer for it (`purge_row_payloads`' docstring
  says the same). What remains stored whole in cleartext is the row's own
  `purpose` cell (`payout_batch_rows.purpose`, deliberately unconstrained so
  the report can show what the file said) — bounded, but not audited across
  every purpose.
- **`--accent` used as a focus ring on `--surface-2` in dark measures 3.05:1,
  with no margin to spare.** No dark surface lighter than `#1a1f29` may be
  introduced without recomputing every ratio in `tests/test_contrast.py`
  first — that file, not the comments beside the tokens, is what keeps this
  true.

---

## (c) Verified clean — please do not re-litigate

Each of these was checked in this round or an earlier one; the
evidence is named so you can re-check cheaply rather than re-investigate.

- **No secrets in git history.** 2,433 blobs scanned at v1.1.0; every blob
  added since was re-scanned at v1.2.0 (no hits). `.env.local` is untracked
  and has always been.
- **No raw SQL over user input.** Everything is SQLAlchemy ORM; the handful of
  `text()` calls are literal DDL fragments and server defaults (`app/models.py`,
  `app/counterparties.py:811`).
- **Route gating is structurally proven, not sampled.**
  `tests/test_permissions.py:114` fails the build if any mutating route lacks a
  cataloged permission — an ungated action cannot ship.
- **OIDC discipline:** algorithm and `kid` pinned, `nonce` checked, PKCE
  enforced, discovery document downgrade refused (`app/auth/oidc.py`, ~30 lines
  of RS256 rather than a JOSE dependency — deliberate, and reviewed).
- **No `python-multipart`.** Uploads are parsed by the app's own bounded reader;
  form bodies go through `parse_qsl`, not Starlette's form parser.
- **CSV exports quote formula-leading cells** (`=`, `+`, `-`, `@`).
- **Uploads are validated by magic bytes, not by filename or declared type, and
  there is no download route** — a stored document cannot be served back.
- **Per-row idempotency keys are stable across a reconciler replay**, including
  the case of two byte-identical rows in one batch (`hash_scope` carries the
  batch id and row number).
- **The Conduit client never logs bodies, headers or the API key**, and refuses
  path traversal or query-splitting in any path it is handed
  (`app/conduit/client.py:337`).
- **The container runs as a non-root user** (`Dockerfile:37,49`) and carries no
  test tooling.
- **`pip-audit` blocks CI** (`.github/workflows/ci.yml:190`).
- **Every contrast ratio is computed, never quoted.** `tests/test_contrast.py`
  parses the shipped hexes straight out of `static/styles.css` and recomputes
  4.5:1 for every text token on every ground it may sit on, and 3:1 for the
  focus ring and the status hues drawn as a line, in both themes.
- **Every reader of a stored operation error goes through the one translation
  boundary.** `tests/test_failure_paths.py` pins that nothing under
  `app/web/*.py` or `app/batches.py` lifts `title` or `detail` straight out of
  an `Operation.error` snapshot instead of going through `web.problem_of` —
  the batch detail cell, the results CSV, the revoke and cancel banners, the
  order-execute banner and the upload chip all route through it; a sixth
  reader that skips it fails the grep pin.

- **The v1.2.0 money-path fixes were reviewed under a fresh context** (fresh context):
  concurrent same-nonce creates arbitrated by the DB constraint; backfill safe
  under the 0005 partial unique index; retry after a rejection is a fresh
  render on every path; `pollBlocked` cannot starve a poller (htmx clears
  `htmx-request` on error/abort/timeout); the seal is HMAC-SHA256 with a
  constant-time compare and cannot pass as a session or CSRF token; the
  revocation SELECT fails closed when the DB is down; purges are idempotent and
  never touch `partially_dispatched` batches or unprocessed events.
---

## (d) Pointers

- `tests/e2e/*.py` are **live scripts, run deliberately, never by CI.** Every
  one of them refuses to run against anything but `api.sandbox.conduit.financial`
  with a `ck_sandbox_` key. They are not part of `pytest`.
- **The stylesheet is mid-revamp.** `docs/ARCA_REVAMP_SPEC.md` is the frozen
  contract for the "Arca" revamp, landing token-by-token and
  template-by-template over several releases — a screenshot taken between
  slices is expected to look inconsistent, not broken.
- The test suites: `uv run pytest -q --ignore=tests/browser` (needs a local
  Postgres `conduit_console_test`) and `uv run pytest -q tests/browser` (needs
  chromium). The browser suite drives the real app in a real browser against a
  stubbed Conduit — it is also what proves the Content-Security-Policy does not
  break htmx.

---

## (e) How to get a copy

**`git clone` it. Never copy the directory.**

The working tree may contain an untracked `.env.local` holding a real
`SESSION_SECRET` and `ENCRYPTION_KEY`. `.gitignore` and `.dockerignore` keep it
out of pushes and images; neither of them stops `cp -r`.

```sh
git clone <this repo> conduit-console
cd conduit-console && git checkout v1.4.0
```

The repository is private and stays that way. `LICENSE` is a proprietary
notice — shared for review only, no licence granted.
