# Conduit Console

A self-hosted operator console for [Conduit Financial](https://conduit.financial)'s
v2 API. One installation serves one Conduit organization, using **your** API key:
onboard customers, request virtual accounts in whatever currencies Conduit's
own discovery names for that customer, send payouts and transfers, convert
between currencies, and follow every one of those in a ledger that survives a
lost response.

It is a small FastAPI + Jinja2 + htmx application with a background worker and a
PostgreSQL database. No JavaScript build step, no SPA, no queue broker, no Redis.

**What it is not.** It is not a bank, a ledger of record, or a second source of
truth: Conduit owns the money and the resources. This console owns exactly two
things — the operator's *intent* (an operations ledger with idempotency keys, so
a mutation happens once) and the operator's *work in progress* (encrypted
onboarding drafts). Everything else is read back from Conduit.

## What is in the box

| | |
|---|---|
| **Onboarding** | The questionnaire, documents and people are whatever `GET /v2/onboarding/requirements` answers for a country. There is no country data in this repository. Drafts autosave, survive a restart, and are encrypted at rest. |
| **Accounts** | Virtual accounts in whatever currencies discovery names for the customer, through the feature-request flow, deposit instructions (including SEPA and nested addresses), balances, deposit history. |
| **Payouts** | Driven end to end by `GET /v2/payouts/requirements`: the form's shape, its document gate and its whitelist gate all come from the response, never from a table in this app. |
| **Transfers & conversions** | Same-currency transfers to whitelisted destinations; quote → option → order for USD↔EUR, with the option's expiry counted down in the browser and re-checked on the server. |
| **Monitoring** | A webhook inbox, a reconciler that repairs whatever the webhooks missed, and an operations ledger where an ambiguous mutation lands as `outcome_unknown` and resolves itself. |
| **Operators** | Reverse-proxy identity or OIDC, three built-in roles (viewer < operator < admin) plus any role your deployment defines over the [permission catalog](PERMISSIONS.md), CSRF on every mutation, sessions in signed cookies. |

## Bring your own API key

There is no hosted service and no shared tenancy. You run the container, you
supply `CONDUIT_API_KEY`, and the console talks to Conduit **as your
organization**. Consequences worth stating plainly:

- The key lives in the process environment and nowhere else. It is never written
  to the database, never rendered, never logged, and there is no browser-facing
  setting that can change it. Every secret also accepts a file instead —
  `CONDUIT_API_KEY_FILE=/run/secrets/…`, and the same for the rest — which is
  how Docker secrets and Kubernetes projected volumes deliver them
  ([deploy/README.md §2](deploy/README.md#secrets-from-files-foo_file)).
- The console can do whatever that key can do. Scope the key to the least
  Conduit access your operators need.
- One installation, one organization. Two organizations means two deployments
  with two databases — the operations ledger and the drafts are per-org data.
- Host selection is a hardcoded allowlist (`app/config.py`), keyed by
  `CONDUIT_ENV`. `CONDUIT_API_BASE`, if set at all, must equal the allowlisted
  origin for that environment or the app refuses to start. The console cannot be
  pointed at an arbitrary host.

## Quickstart — local development

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 14+, and (for
the browser tests) node and chromium.

```sh
createdb conduit_console
cp .env.example .env          # then fill it in, see below
uv sync --all-groups
uv run --env-file .env alembic upgrade head
uv run --env-file .env uvicorn app.main:create_app --factory --reload
```

`--env-file` is not decoration. Settings are read from the process environment
and nowhere else — `Settings` sets `env_file=None` deliberately, so that one
mechanism delivers a secret rather than two disagreeing ones. A `.env` that is
merely *present* is a `.env` that is not read, and the failure is three
`Field required` errors naming `DATABASE_URL`, `SESSION_SECRET` and
`ENCRYPTION_KEY`. `set -a; source .env; set +a` does the same job.

The minimum `.env` for a local run against the Conduit **sandbox**:

```sh
CONDUIT_ENV=sandbox
CONDUIT_API_KEY=<your sandbox key>
DATABASE_URL=postgresql+psycopg://localhost/conduit_console
AUTH_MODE=disabled            # sandbox only; refused outside it
COOKIE_SECURE=false           # sandbox only, for plain http
SESSION_SECRET=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

Run the worker too — without it nothing reconciles and webhooks pile up unread:

```sh
uv run --env-file .env python -m app.worker
```

Against **production** none of the above applies: an open console over real
money is refused at startup, and the local shape is a proxy in front of it.
See [`deploy/README.md` §1D](deploy/README.md#d-one-operator-one-machine).

### Tests

```sh
createdb conduit_console_test
uv run pytest -q                          # everything, incl. browser; no --env-file
uv run pytest -q --ignore=tests/browser   # without chromium
uv run playwright install chromium        # once, before the browser suite
```

`pytest` is the one command on this page that takes **no `--env-file`**.
`tests/conftest.py` honours `DATABASE_URL` and truncates every table before
each test, so pointing the suite at the database you actually use destroys it —
including the encrypted drafts, which have no other copy. The suite defaults to
`conduit_console_test` on its own; leave it that way, or name a test database
explicitly (`DATABASE_URL=…/conduit_console_test uv run pytest -q`).

It also refuses to start unless `DATABASE_URL` names a database that says it is
disposable: `test`, `e2e` or `sweep` as a whole word in the name, or a `cc_…`
phase database. `conduit_console` is not one, so that accident now stops at
collection with a message naming the database. The escape hatch is
`ALLOW_DESTRUCTIVE_TEST_DB=1`, and it is only the right answer if you have
decided to lose that database's contents. (`tests/e2e/07_counterparties.py`
refuses on the same grounds, for the same reason.)

The suites, and what each is for, are in `IMPLEMENTATION_PLAN.md` §9. Two are
worth knowing about here: `tests/test_forms.py -k condition_vectors` runs the
same vectors through the Python evaluator *and* through `static/conditions.js`
under node, so the two cannot drift; and `tests/browser/` drives the real app in
a real chromium against a stubbed Conduit. `tests/e2e/*.py` are the live scripts
— they talk to a real Conduit host and are run deliberately, never by CI.

## Quickstart — Docker

```sh
docker build -t conduit-console .
docker run --env-file .env -e RUN_MIGRATIONS=1 -p 8000:8000 conduit-console web
docker run --env-file .env conduit-console worker
```

One image, two commands. `RUN_MIGRATIONS=1` runs `alembic upgrade head` before
either role starts; leave it off and run the migration as its own one-shot
container. The image runs as a non-root user and contains no test tooling.

For a full stack — Postgres, the migration, both roles, and a commented
reverse-proxy example — see `deploy/docker-compose.yml`, and read
**[`deploy/README.md`](deploy/README.md)** before running anything that is not
sandbox. It has the deployment architectures, the proxy header contract, the
backup/restore procedure and the complete environment reference.

## Environment matrix

`CONDUIT_ENV` picks the host, and with it how strict the startup guards are.

| | `sandbox` | `staging` | `production` |
|---|---|---|---|
| Host | `api.sandbox.conduit.financial` | `api.staging.conduit.financial` | `api.conduit.financial` |
| Money | fake | **real key, real-shaped data** | real |
| `AUTH_MODE=disabled` | allowed | **refused at startup** | **refused at startup** |
| `COOKIE_SECURE=false` | allowed | **refused** | **refused** |
| Weak/placeholder secrets | allowed | **refused** | **refused** |
| `OIDC_REDIRECT_URI` | derived if unset | derived if unset | **must be set, https** |
| `/v2/sandbox/*` simulators | offered in the UI | not offered | not offered |
| Environment badge | green | **amber** | amber |

Three caveats that have bitten this project already:

1. **Staging self-labels as "Production".** Its API responses and its own
   documentation call it production; only the hostname distinguishes it. The
   console shows the host next to the environment badge for exactly this reason.
   Treat a staging key as a production key.
2. **Staging has no sandbox simulators.** `/v2/sandbox/*` does not exist there,
   so approvals, rejections and deposit simulation are manual. The console hides
   those controls outside `CONDUIT_ENV=sandbox`, and the routes re-check the host
   server-side before sending.
3. **A sandbox key is not interchangeable with a staging one**, and neither is
   interchangeable with production. The key and `CONDUIT_ENV` must agree: a
   sandbox key against the staging host authenticates as nobody, and the
   symptoms (401s on reads that "worked yesterday") are unhelpful. If reads fail
   immediately after a deploy, check that pair first.

`EUR` accounts additionally depend on the organization's provider coverage: an
org without a EUR provider gets `422 NO_ELIGIBLE_PROVIDER` from Conduit even
though discovery lists EUR as an allowed value. The console renders that refusal
as Conduit's own problem-detail rather than pretending the request is malformed.

## Where the design lives

| File | What it settles |
|---|---|
| `IMPLEMENTATION_PLAN.md` | Architecture, module boundaries, phases, test matrix |
| `OPERATIONS_SPEC.md` | The operations ledger: idempotency, states, reconciliation, webhooks |
| `FORM_ENGINE_SPEC.md` | The discovery-driven form engine and its condition language |
| `DESIGN.md` | The design system: type scale, spacing, status colours, density — implemented as tokens in `static/styles.css` |
| `deploy/README.md` | Running it |
| `KNOWN_ISSUES.md` | What we are unsure about, accepted, and verified — reviewers start here |
| `RELEASE_NOTES.md` | What changed per tag, and what got stricter |
