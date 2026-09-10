# Conduit Console — implementation plan (v2, rebaselined)

v2 of 2026-08-27, superseding the same-day v1 after an external plan review that day (factual claims verified against the production OpenAPI spec — see §11 for the two places reality differs from the review). The review document itself is not in this repository. This is the master plan; component contracts (`FORM_ENGINE_SPEC.md`) point here.

## 0. Product scope

**v1 delivers:** a single-tenant, self-hosted, production-connected internal operator console. One Conduit organization and API key per installation (colleagues each run their own installation with their own key — configured at deployment, never in the browser). Business onboarding via requirements discovery; application monitoring, rejection correction, RFIs, IDV links; virtual accounts in whatever currencies discovery names per customer, deposit instructions, balances, transaction history; external USD/EUR payouts on the rails Conduit makes eligible per route; same-currency transfers between eligible customer accounts; **USD↔EUR conversions** between a customer's virtual accounts; pluggable operator authentication.

**Out of v1:** crypto wallets/transactions, Travel Rule flows, shared multi-tenant deployment, customer-facing self-service, two-person payout approval, conversion currencies beyond USD↔EUR, house-account prefunding and markup, browser-configurable API hosts or keys, combined cross-customer-cross-currency transfers (same-customer conversion + same-currency A→B ship first).

## 1. Architecture

Modular monolith, server-rendered (FastAPI + Jinja2 + htmx — unchanged), but production-shaped: one codebase, one container image, one PostgreSQL database, two runtime roles (`web`, `worker`).

```
Operator browser → auth boundary (verified Actor) → FastAPI web ⇄ Conduit API
                                                      ↓↑                ↑
                                                  PostgreSQL            | reconciliation
                                        (drafts, operations, audit,     |
                                         webhook inbox, projections)    |
                                                      ↓↑                |
                                                background worker ←— signed webhooks
```

```
app/
  auth/             # IdentityProvider contract + adapters (proxy, oidc, disabled)
  conduit/          # typed client, problem-detail errors, retry policy, OpenAPI pin
  onboarding/       # requirements, drafts, submissions, RFIs, IDV
  accounts/         # discovery-driven feature requests + virtual accounts
  payments/         # recipients, payouts, transfers
  conversions/      # quotes, orders, execution
  operations/       # durable idempotency + unknown-outcome recovery
  webhooks/         # signature verification + inbox ingestion
  reconciliation/   # repair jobs over non-terminal operations
  audit/            # actor-attributed events
  web/              # routes, templates, form presentation
forms.py stays the form-engine module (contract: FORM_ENGINE_SPEC.md)
```

Money-movement state transitions, webhook processing, authorization, and operation recovery are explicit service boundaries — not incidental route code.

## 2. Configuration, authentication, security (v1 requirements, not later phases)

- **No browser key/host entry.** `CONDUIT_ENV=sandbox|staging|production` selects from a hardcoded host allowlist; the API key comes from an environment secret or mounted secret file. Arbitrary URLs are rejected at startup. The header badge still shows the active environment prominently.
- **`IdentityProvider.authenticate(request) -> Actor(id, email, display_name, roles)`** with three adapters:
  1. `proxy` (default): verified identity headers from an upstream auth proxy, gated by a shared proxy-verification secret; app port not publicly reachable.
  2. `oidc`: issuer, client id/secret, callback, role claim via env.
  3. `disabled`: development only — **the app refuses to start with `disabled` + production host.**
- Roles: `viewer` (read), `operator` (submit/act), `admin` (config-sensitive actions). Every sensitive action and audit event carries the verified Actor.
- TLS at the ingress/reverse proxy (documented reference config); secure session cookie with env-provided secret; **CSRF token on every browser mutation** (htmx `hx-headers`).
- Structured logs with secret and personal-data redaction; the API key never appears in logs, HTML, or errors.

## 3. Data model (PostgreSQL; Conduit stays the financial source of truth)

Migrations via Alembic from day one.

- `drafts` — minimal onboarding/form drafts + the requirements snapshot used to render them (a draft is always validated/assembled against its own pinned snapshot). Encrypted at rest (app-level, key from env), retention period, sensitive fields purged after successful submission.
- `operations` — one row per logical mutation: action type, actor, request hash, idempotency key, Conduit resource id, state (`created → submitted → confirmed | failed | outcome_unknown`), timestamps. **Persisted before the Conduit call.** The idempotency key lives here, not in a hidden form field — a restart, lost response, double-click, or second tab reuses the same operation row and key, so a duplicate payout cannot be created. `outcome_unknown` (mutation timeout / ambiguous response) is a first-class state the reconciler resolves; the UI shows an explicit "result being confirmed" state, never a bare retry button.
- `webhook_events` — inbox: raw event, unique event id/hash (dedupe), processing attempts, status.
- `audit_events` — attempted/confirmed/retried/cancelled/failed operator actions.
- Read projections (applications, customers, accounts, transactions, payouts, orders) — optional, populated by the worker, used for list views; detail views may still read Conduit live.

## 4. Conduit client (`app/conduit/`)

- **Pinned OpenAPI snapshot checked into the repo**; typed response/error models generated or validated from it. CI job diffs the pin against the current production spec and reports material drift.
- Error parsing is problem-detail, centrally: `ProblemDetailDto {type, title, status, detail, resolution, docs, instance, correlationId, timestamp}`, `ValidationErrorDto` (extends it with `errors[] {pointer, detail, category, allowedValues}`), `RateLimitedErrorDto`. **`resolution` and `correlationId` are displayed** (correlation id on every error surface, for support cases). Fixture: `tests/fixtures/problem_detail_422_no_eligible_provider.json`.
- One shared `httpx.AsyncClient` per process, created at startup, closed at shutdown (connection pooling). Never per-request clients.
- Reads: honor `Retry-After` on 429, bounded exponential backoff with jitter. Mutations: never auto-retried; a timeout marks the operation `outcome_unknown` for the reconciler.
- Pagination: pass one cursor page through to the UI; never eagerly walk all cursors.

## 5. Monitoring: webhooks + reconciliation (polling demoted to manual refresh)

- `POST /v2/webhooks/endpoints` registered per installation (deployment config supplies the public URL). Handler: verify `X-Conduit-Signature` (HMAC-SHA256 over `{t}.{rawBody}`, keep `whsec_` prefix, 300s window, dual-`v1` during rotation) **over the raw request body**, insert into `webhook_events`, ack promptly. Worker processes the inbox: dedupe by event id/hash, idempotent handlers, tolerate out-of-order delivery (compare event timestamps/versions against projection state), update projections + audit.
- Relevant events: `application.approved/rejected`, `customer.created/restricted`, `virtual_account.activated`, `whitelist_recipient.*`, `transaction.*` (payouts emit these — there are no `payout.*` events), `order.*`.
- **Reconciler** (worker, periodic): queries Conduit for every non-terminal `operations` row and non-terminal projection, repairs missed/late/out-of-order events, resolves `outcome_unknown` by matching request hash / clientReferenceId / idempotent replay semantics.
- UI: manual refresh buttons + slow fallback refresh on detail pages; the state engine is webhooks+reconciler, not browser tabs.

## 6. Form engine

Contract unchanged in core: `FORM_ENGINE_SPEC.md` (updated alongside this plan). Deltas from v1:
- Server 422s arrive as `ValidationErrorDto` in problem-detail form; the client layer parses centrally, the engine's mapper consumes `errors[]`.
- Dialect B responses carry route metadata the engine surfaces to the route: `whitelist {required, reason}`, `documentation {required, reason, acceptedDocumentTypes}`, `blockedJurisdictions` — **payout gating is discovery-driven, never hardcoded** (fixtures confirm: fedwire/goods requires documentation; intercompany requires whitelist, not documentation).
- JS and Python condition evaluators both execute the shared vector file **in CI** (Python via pytest, JS via node); disagreement fails the build. Python stays authoritative at runtime.
- Requirements responses cached briefly per (context, country, asset, schemaVersion); every active draft pins the exact snapshot it was rendered from.

## 7. Feature modules

**Onboarding** (`app/onboarding/`): as v1 (country → requirements + policy-subject catalogs → grouped dynamic form → persons → documents → submit with operation-backed idempotency), plus: durable encrypted drafts (survive restarts and session expiry; rejected-application correction re-opens the draft, not a session blob); RFI list/acknowledge/respond; IDV links per person; application lifecycle driven by webhooks/reconciler.

**Accounts** (`app/accounts/`): virtual accounts in whatever currencies discovery names for the customer. Asset picker is `allowedValues` from `GET /customers/{id}/features/requirements` with `asset` omitted, verbatim — no local currency list — and even then handle runtime `422 NO_ELIGIBLE_PROVIDER` gracefully (verified live: the schema advertises EUR while the provider check can still refuse it; show Conduit's `resolution` text). Deposit-instruction cards for us_domestic (ach/fedwire/rtp), swift, and **sepa** variants. Balances + deposit history per account.

**Payments** (`app/payments/`): payout form driven end-to-end by `GET /v2/payouts/requirements` — fields, `whitelist.required`, `documentation.required` + `acceptedDocumentTypes`, `blockedJurisdictions` all from the response for the chosen purpose/rail/recipientType/destinationCountry. USD rails: ach/fedwire/rtp/fednow/swift as eligible; EUR rails: sepa/swift as eligible; never assume a rail exists for a route. Quotes for direct payouts are **indicative only**: clearly labeled, refreshed immediately before confirmation, with `expiresAt` shown; stale confirmations disabled. Whitelist-recipients page (incl. the "whitelist another customer's account" `group_entity` shortcut) and same-currency transfers via intercompany payouts, both per v1 design but operation-backed. Submission timeout → explicit `outcome_unknown` recovery state.

**Conversions** (`app/conversions/`) — new, first-class Convert workflow:
1. Pick customer, source VA, destination VA (opposite currency), amount + `lockSide`.
2. `POST /v2/quotes` (conversion quote: differing source/destination assets, no destination country) → present `options[]` with rate, fees, `expiresAt`.
3. Operator selects an option → stored on the operation row (option id + expiry) → confirm screen with countdown; **confirmation after expiry is rejected and re-quoted**.
4. `POST /v2/orders` with `QuoteRedemptionOrderDto {quoteOptionId, source, destination, autoExecute?}`; explicit `POST /v2/orders/{id}/execute` when not auto-executing.
5. Follow order status `pending → succeeded | failed | cancelled` (per spec — quote *options* expire; orders don't have an `expired` status) via webhooks/reconciler; handle insufficient funds, cancellation, and ambiguous execution (`outcome_unknown`).
6. Order list/detail views; `fiat_conversion` transactions in the ledger.

**Transactions ledger**: as v1 (unified list + typed detail), fed by projections, including conversions and linked orders.

**Unknown statuses** (all modules): render `Unknown: <raw value>` neutrally, never infer terminality, never enable state-dependent actions on an unknown state, emit a structured log alert with resource + raw value. Contract-drift test feeds synthetic unknown values through every status renderer.

## 8. Packaging & distribution (self-hosted)

- Versioned container image; `web` and `worker` commands from the same image; Docker Compose reference deployment (app, worker, PostgreSQL, reverse proxy example).
- Alembic migrations with documented startup/upgrade procedure; `/health/live` + `/health/ready`.
- Reverse-proxy + OIDC config examples; complete environment/secret reference; backup/restore/upgrade docs.
- CI: unit + integration + browser tests, condition-vector parity, OpenAPI drift check, image build, dependency/container vulnerability scan.

## 9. Test matrix

1. **Pure unit**: form parsing/coercion/conditions/assembly, problem-detail parsing, operation state machine, webhook signature, authorization decisions.
2. **Contract**: pinned OpenAPI + captured fixtures (now incl. EUR/SEPA payout requirements and a live ProblemDetailDto; quote/order fixtures captured during the first sandbox run).
3. **Integration** (app + Postgres): operation/idempotency lifecycle, duplicate + out-of-order webhooks, reconciliation repair, draft encryption/retention.
4. **Browser e2e** (Playwright): onboarding, account, payout, conversion, rejection, stale-quote, timeout, error-recovery journeys.
5. **Sandbox acceptance**: both currencies, all promised route classes, both conversion directions, cancellation, documentation gates, returned/failed outcomes.
6. **Security**: proxy-header spoofing, CSRF, host allowlist, secret redaction, upload limits, role enforcement, production-startup safeguards.

Critical interaction tests: rapid double-submit, two tabs on one draft, navigation away mid-request, session expiry, 10s upstream latency, lost mutation response, restart before reconciliation.

## 10. Phases

| Phase | Scope | Acceptance gate |
|---|---|---|
| **0 — rebaseline** | This plan; spec/protocol updates; EUR + problem-detail fixtures | Done 2026-08-27 (quote/order fixtures deferred to the first run) |
| **1 — production foundation** | Deployment config + secrets, auth adapters + roles + CSRF, Postgres schema + migrations, typed client + problem-detail + retry policy, operations/idempotency service + audit, webhook inbox + worker + reconciler, health endpoints | Unit + integration suites green; security tests for startup guards, CSRF, allowlist; a scripted lost-response drill ends in `outcome_unknown` → reconciled |
| **2 — onboarding** | Dynamic forms + drafts (encrypted, durable), documents, submit, applications, rejection correction, RFIs, IDV, application webhooks | Sandbox: onboard→approve, reject→correct→resubmit; restart-mid-draft recovery; parity gate in CI |
| **3 — accounts** | discovery-driven feature requests, activation, deposit instructions (incl. SEPA), balances, deposit history, NO_ELIGIBLE_PROVIDER handling | Sandbox: USD account funded; EUR path exercised (or NO_ELIGIBLE_PROVIDER surfaced correctly if the org lacks a EUR provider — see §11) |
| **4 — money movement** | Discovery-driven payouts (USD + EUR rails), whitelist + same-currency transfers, **conversions** (quote→order→execute, expiry, cancellation), unified monitoring | Sandbox: USD payout (doc gate on + off per discovery), EUR/SEPA payout, A→B transfer, USD→EUR and EUR→USD conversions, stale-quote rejection, cancel |
| **5 — packaging & verification** | Container, compose reference, migrations docs, full test matrix, browser e2e suite, ops docs, CI complete | Fresh-machine deploy from docs alone; full matrix green |

Once the foundations stabilize, the later milestones can proceed as parallel workstreams against the same contracts and fixtures.

## 11. Where reality already diverges from the review (verified 2026-08-27)

- **Order status enum** is `pending|succeeded|failed|cancelled` — no `expired`, no `completed`. Expiry is a quote-option property; the stale-confirmation guard lives at the confirm step.
- **EUR virtual accounts on the current staging org 422 with `NO_ELIGIBLE_PROVIDER`** even though discovery's `/asset/code` allowedValues includes EUR. Acceptance depends on the org's provider coverage — raise with Conduit (or use a sandbox org with EUR coverage) before the accounts milestone; the app must handle the 422 either way.
- The `whitelist.required` / `documentation.required` flags the review demanded are confirmed present in our previously captured payout fixtures (fedwire/goods: documentation required; intercompany: whitelist required) — the engine surfaces them, routes obey them.
