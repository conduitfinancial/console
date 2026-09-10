# Deploying Conduit Console

Everything needed to run this in earnest: the shapes a deployment can take, the
secrets it needs, the header contract that makes proxy auth safe, and the two
procedures you will actually be woken up for — upgrade and restore.

Read [the environment reference](#environment-reference) before the first run.
`app/config.py` is the source of truth for every setting; the table is checked
against it by `tests/test_docs.py`, so it cannot silently fall behind.

---

## 1. Architectures

All three run the same image with two commands, `web` and `worker`. Run exactly
one `worker`; run as many `web` replicas as you like.

### A. Single host, compose, behind a reverse proxy — the default

```
internet → Caddy/nginx (TLS) → oauth2-proxy (identity) → web ⇄ postgres
                                                          worker ⇄ postgres
```

`docker-compose.yml` in this directory is that stack minus the proxy, which is
included commented out. The app port is bound to `127.0.0.1` so nothing but the
proxy can reach it. This is the recommended shape: TLS, identity and rate
limiting are the proxy's job, and the console does not reimplement any of them.

### B. Single host, no proxy, OIDC in the app

```
internet → TLS terminator → web (AUTH_MODE=oidc) ⇄ postgres
                            worker ⇄ postgres
```

The app runs the authorization-code flow with PKCE itself. You still need TLS in
front. Set `OIDC_REDIRECT_URI` to the public https URL — with
`CONDUIT_ENV=production` the app refuses to start without it, because deriving
the callback from the request means trusting forwarded headers for login.

### C. Kubernetes / Nomad / ECS

Two workloads from one image: a `web` Deployment (N replicas, liveness
`/health/live`, readiness `/health/ready`) and a `worker` Deployment with
**`replicas: 1`**. Migrations go in an init container or a Job running
`alembic upgrade head`; do not set `RUN_MIGRATIONS=1` on a multi-replica
Deployment — Alembic locks, so it is safe, but every replica pays for it.

> **One worker.** Two workers do not corrupt anything: the inbox claims rows
> `FOR UPDATE SKIP LOCKED`, reconciliation is idempotent, and every step commits
> on its own. They simply double the Conduit request budget for no benefit, and
> make "why are we rate limited?" harder to answer.

### D. One operator, one machine

The top-level README's quickstart — `uvicorn`, a browser, no proxy — is a
**sandbox** shape and does not survive the move to a real key. Point it at
production and every page answers 401, because in proxy mode this console
implements no login at all: `login_url` is `None`, identity is the proxy's job,
and the startup guards refuse both settings that would let you skip it
(`AUTH_MODE=disabled` and `COOKIE_SECURE=false` are rejected outside
`CONDUIT_ENV=sandbox`). In-app OIDC is not the way around it either — with
`CONDUIT_ENV=production` the app requires an https `OIDC_REDIRECT_URI`, which a
workstation does not have.

That is the intended behaviour, stated once so nobody goes looking for the flag:
**there is no configuration in which this console serves an operator over real
money with nobody having logged in.**

So one operator on one machine runs **architecture A, shrunk** — the compose
stack in this directory with the proxy uncommented and every port bound to
`127.0.0.1`. It needs no public hostname and no certificate: Google, Okta and
Entra all accept an `http://localhost:PORT/oauth2/callback` redirect URI, so the
authorization round-trip completes against the real IdP. Two adjustments to the
snippets in §3:

- oauth2-proxy needs `--cookie-secure=false` for plain http on loopback. The
  console's own `COOKIE_SECURE` stays `true`; that one is not negotiable.
- Use Chrome or Firefox. The console's session and CSRF cookies carry the
  `__Host-` prefix, which requires `Secure`; both browsers treat
  `http://localhost` as a trustworthy origin and store them anyway. **Safari
  does not**, and the symptom is a login loop with nothing in the log.

Set `MONEY_CEILING` while you are here. It is the one guard indifferent to how
the console is deployed, and a workstation is precisely where a wrong amount
gets typed.

> **Do not swap the IdP for a shim that injects the identity headers.** It is
> about fifteen lines and it works, which is the trap: it is oauth2-proxy with
> the authentication deleted, and it hands admin over real money to anything
> that can open the port — including any other process on that machine. As a
> throwaway for reading production data on a host you alone control, fine. Never
> in something a second person runs, and never in a repository, where its next
> reader will assume it was reviewed.

---

## 2. Secrets

Generate each one fresh, per deployment. Never copy a value out of
`.env.example` — outside `CONDUIT_ENV=sandbox` the app refuses to start on a
secret that is short or contains placeholder text, and that guard exists because
a copied example file is exactly how a published value reaches production.

```sh
# SESSION_SECRET and PROXY_SHARED_SECRET (and any other opaque shared secret)
openssl rand -hex 32

# ENCRYPTION_KEY — a Fernet key, not an arbitrary string
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# POSTGRES_PASSWORD for the compose reference
openssl rand -hex 24
```

`CONDUIT_API_KEY` comes from Conduit. `CONDUIT_WEBHOOK_SECRET` is returned once
by the endpoint registration call — see [§6](#6-webhooks).

The compose reference reads `deploy/.env`; that file must never be committed
(the repository `.gitignore` covers `.env` at any depth).

### Secrets from files: `FOO_FILE`

Every secret-bearing setting also accepts its value from a **file**, which is
the shape Docker secrets, Kubernetes projected volumes and systemd credentials
all deliver: set `FOO_FILE=/path` instead of `FOO`. A file is not inherited by
every child process and does not show up in `docker inspect`.

```yaml
services:
  web:
    environment:
      SESSION_SECRET_FILE: /run/secrets/session_secret
      CONDUIT_API_KEY_FILE: /run/secrets/conduit_api_key
    secrets: [session_secret, conduit_api_key]

secrets:
  session_secret: {file: ./secrets/session_secret}
  conduit_api_key: {file: ./secrets/conduit_api_key}
```

- Supported for `CONDUIT_API_KEY`, `SESSION_SECRET`, `ENCRYPTION_KEY`,
  `PROXY_SHARED_SECRET`, `CONDUIT_WEBHOOK_SECRET`, `OIDC_CLIENT_SECRET` and
  `DATABASE_URL` (its DSN carries a password). The list is derived from the
  settings model, not hand-kept, so a new secret gets the file form for free.
- **One trailing newline is stripped** — `echo secret > file` is fine. Nothing
  else is: a secret may end in a space.
- **Setting both `FOO` and `FOO_FILE` is refused at startup.** Any precedence
  rule would surprise somebody, and the situation means two deployment
  mechanisms disagree about a secret. The error names the setting; it never
  prints the value, and never the path.

---

## 3. The proxy-mode header contract

`AUTH_MODE=proxy` is the default and the recommended mode. It is safe **only**
if the proxy holds up its half of the following contract.

The app trusts identity headers on a request **if and only if** that same
request carries the shared secret:

| Header (configurable) | Default name | Meaning |
|---|---|---|
| `PROXY_SECRET_HEADER` | `X-Proxy-Auth` | Must equal `PROXY_SHARED_SECRET`, or the request is refused outright. |
| `PROXY_USER_HEADER` | `X-Auth-Request-User` | Stable user id. |
| `PROXY_EMAIL_HEADER` | `X-Auth-Request-Email` | Shown in the header bar and recorded on every operation. |
| `PROXY_GROUPS_HEADER` | `X-Auth-Request-Groups` | Comma-separated groups, mapped to roles by `AUTH_ROLE_MAP`. |

The proxy must therefore do **both** of these:

1. **Set** `X-Proxy-Auth: <PROXY_SHARED_SECRET>` on every request it forwards.
2. **Strip any inbound copy** of all four headers before adding its own. A
   visitor who can set `X-Auth-Request-Groups` *and* reach a proxy that forwards
   it has granted themselves whatever role that value maps to.

And the app port must not be reachable from the network — bind it to loopback or
to an internal network only. The shared secret is a second lock, not the first.

A group not listed in `AUTH_ROLE_MAP` maps to the role of the *same name*
(`operator` → operator); anything else grants nothing at all, and a user with no
role gets a 403 page rather than a default role. Roles are ordered
`viewer < operator < admin`: viewers read, operators move money, admins
additionally hold the operations release valve (`Mark abandoned`).

### Custom roles

The three built-ins are bundles over a **permission catalog** — every gated
action in the console has a name, and routes are gated on the action, not on the
role. [PERMISSIONS.md](../PERMISSIONS.md) is the full grid, generated from the
code that enforces it; it is the document to hand to a security review.

To define your own role, point `ROLES_FILE` at a JSON file and map a group onto
it:

```json
{
  "payments-clerk": ["console.view", "payout.create", "contact.edit"],
  "batch-dispatcher": ["console.view", "batch.dispatch"]
}
```

```
ROLES_FILE=/etc/conduit-console/roles.json
AUTH_ROLE_MAP=finance-team=payments-clerk,ops-batch=batch-dispatcher
```

Roles are deployment config on purpose: they belong in your change control,
where a diff is reviewed before it ships, and there is no runtime screen that
can grant somebody a permission between two audits.

The file is read once, at startup, and **the app refuses to start** rather than
run with a role file it cannot honour. Each refusal names the role and the
permission, and suggests the nearest real name:

```
ROLES_FILE role 'payments-clerk' lists unknown permission 'payout.send' —
did you mean 'payout.create'? (the catalog is PERMISSIONS.md)

ROLES_FILE role 'payments-clerk' has 'payout.create' without 'console.view',
which it requires — the pages that offer 'payout.create' are 'console.view' pages

ROLES_FILE defines 'operator', which is a built-in role — the built-ins are
fixed so that an upgrade cannot quietly change what an existing role means;
give this one another name
```

Two rules are worth knowing before you write the file: a permission that acts
needs `console.view` alongside it (the button that fires it lives on a page),
and the **admin-class** permissions — `contact.delete`, `operation.abandon`,
`onboarding.access_any`, plus `sandbox.simulate` off the sandbox host — may only
be granted on top of the whole operator base. Both are checked at startup.

### Caddy + oauth2-proxy

```caddyfile
console.example.com {
    # `route` runs these in the order written. Caddy's global directive order
    # would otherwise put `request_header` AFTER `forward_auth` and delete the
    # identity headers it had just installed — leaving nobody able to log in.
    route {
        # 1. The webhook receiver authenticates by HMAC signature, not by
        #    identity (§6). It skips forward_auth entirely, and must never carry
        #    identity headers a caller supplied.
        handle /webhooks/conduit {
            request_header -X-Auth-Request-User
            request_header -X-Auth-Request-Email
            request_header -X-Auth-Request-Groups
            request_header -X-Proxy-Auth
            reverse_proxy web:8000
        }

        # 2. Strip what the CLIENT sent, before anyone decides who they are.
        #    `copy_headers` only overwrites headers the auth response actually
        #    sets — so a header the IdP leaves unset (Groups, very commonly)
        #    would otherwise survive from the request and pick its own role.
        request_header -X-Auth-Request-User
        request_header -X-Auth-Request-Email
        request_header -X-Auth-Request-Groups
        request_header -X-Proxy-Auth

        # 3. oauth2-proxy's own endpoints — the login redirect lands here.
        handle /oauth2/* {
            reverse_proxy oauth2-proxy:4180
        }

        # 4. Authenticate. `copy_headers` copies from the auth RESPONSE onto the
        #    request that carries on to the app.
        forward_auth oauth2-proxy:4180 {
            uri /oauth2/auth
            copy_headers X-Auth-Request-User X-Auth-Request-Email X-Auth-Request-Groups
        }

        # 5. Add the shared secret and hand it over. NOTHING is stripped here:
        #    step 2 removed the client's copies, and the headers present now are
        #    the ones step 4 installed. Deleting them here — the obvious-looking
        #    `header_up -X-Auth-Request-*` — makes every request anonymous and
        #    the console unusable.
        reverse_proxy web:8000 {
            header_up X-Proxy-Auth {env.PROXY_SHARED_SECRET}
        }
    }
}
```

Caddy resolves `{env.PROXY_SHARED_SECRET}` from **its own** process environment,
so the proxy container needs the variable too — not just the app:

```yaml
caddy:
  environment:
    PROXY_SHARED_SECRET: ${PROXY_SHARED_SECRET:?set it in .env}
```

Unset, the placeholder resolves to an empty string and the app refuses every
request. oauth2-proxy needs `--set-xauthrequest` (so `/oauth2/auth` returns
those headers) and a scope that actually yields groups from your IdP.

### nginx equivalent

```nginx
# The webhook receiver first, and exactly: `location = ` is an exact match, so
# no other path can fall into this block. Signature is its authentication (§6).
location = /webhooks/conduit {
    proxy_set_header X-Auth-Request-User   "";   # overwrite whatever came in
    proxy_set_header X-Auth-Request-Email  "";
    proxy_set_header X-Auth-Request-Groups "";
    proxy_set_header X-Proxy-Auth          "";
    proxy_pass http://web:8000;
}

location / {
    auth_request /oauth2/auth;
    auth_request_set $user   $upstream_http_x_auth_request_user;
    auth_request_set $email  $upstream_http_x_auth_request_email;
    auth_request_set $groups $upstream_http_x_auth_request_groups;

    proxy_set_header X-Auth-Request-User   $user;      # overwrites the client's
    proxy_set_header X-Auth-Request-Email  $email;
    proxy_set_header X-Auth-Request-Groups $groups;
    proxy_set_header X-Proxy-Auth          "<PROXY_SHARED_SECRET>";
    proxy_pass http://web:8000;
}
```

`proxy_set_header` replaces the inbound value, which is the stripping step — but
only for headers you name. Name all four, in **both** blocks. And note the
subtle one: if the IdP returns no groups, `$groups` is empty and the header is
sent empty, which is what you want; leaving the directive out instead would pass
the client's own value through.

### OIDC instead

```sh
AUTH_MODE=oidc
OIDC_ISSUER=https://idp.example.com          # https, or a loopback for local dev
OIDC_CLIENT_ID=conduit-console
OIDC_CLIENT_SECRET=…                          # 32+ chars outside sandbox
OIDC_REDIRECT_URI=https://console.example.com/auth/callback
OIDC_CALLBACK_PATH=/auth/callback             # register this with the IdP
OIDC_ROLE_CLAIM=roles
AUTH_ROLE_MAP=conduit-ops=operator,conduit-admins=admin
```

Authorization code + PKCE (S256), RS256 ID tokens, discovery required. The
issuer and every endpoint its discovery document names must be https.

### Signing one operator out (`AUTH_MODE=oidc`)

A session cookie is signed and self-contained, so nothing is consulted when it is
presented. To end the sessions one operator is currently holding — a lost laptop,
a leaver, a cookie you think was taken:

```sh
docker compose exec web python scripts/revoke_sessions.py --sub <sub>
```

`<sub>` is the operator's subject as this console knows it: the IdP's `sub`
claim, which is also the `actor_id` column of `audit_events` — look it up by
email there if that is all you have. Every cookie issued before you run it stops
working on that operator's next request; there is nothing to restart.

**It ends sessions, it does not ban the account.** The operator can sign in
again straight away and gets a working session. To stop them signing in, remove
them in the IdP or take their role away (`AUTH_ROLE_MAP` / `ROLES_FILE`); do both
if you mean both. Re-running moves the cut-off forward, so a second incident
after a first sign-out takes effect.

The cut-off is the instant the script ran, compared against the instant each
cookie was minted, and a cookie minted in that same second is refused. Those two
instants are read from two clocks — the script's host and the web host — so host
clock skew moves the boundary by the skew; keep both on NTP, and note that either
direction is fixed by the operator signing in again.

In `proxy` mode the script refuses: this console issues no session there, the
proxy asserts identity on every request, and sign-out is the proxy's to do.

---

## 4. Migrations and upgrades

The schema is Alembic; `alembic upgrade head` is the whole procedure. Every
migration is additive through v1.1.0; v1.2.0's 0013 needs the step below. For
every other migration, a `web` replica of the previous version keeps working
against the new schema for the length of a rolling deploy.

```sh
# compose: the migration is its own service and runs before web/worker start
docker compose up -d --build

# elsewhere: one-shot container, then roll the app
docker run --rm --env-file .env conduit-console:<new> alembic upgrade head
```

**Concurrent runners are safe, but still not the pattern to copy.** `alembic
upgrade head` takes a Postgres *advisory* lock for the whole run
(`alembic/env.py`), so if a cold start brings up several replicas with
`RUN_MIGRATIONS=1` at once, the second waits and then finds the schema already
at head and does nothing. Without that lock both would read "current is X", both
would run the same revision, and the loser would die on `relation already
exists` — on the one day the whole fleet restarts. Prefer the one-shot `migrate`
service anyway: it makes "the schema moved" a thing you can see in one place.

Upgrade order, and why:

1. **Back up the database** (§5). Thirty seconds now, or an evening later.
2. **Run the migration** to `head`, once. **v1.2.0 only:** 0013 is not
   rolling-safe — see below before you roll `web`.
3. **Roll `web`.** `/health/ready` is the gate — it proves the process is up
   *and* the database is reachable; `/health/live` only proves the former.
4. **Restart the `worker` last.** It is the component that would act on a
   half-migrated schema unattended.

Rolling back a *migration* is not supported as a routine operation
(`alembic downgrade` exists, but data written by the newer version is the
problem, not the DDL). Roll back the image and leave the schema forward.

### v1.2.0: migration 0013 is not rolling-safe

`0013` adds `operation_intents` and backfills it from `operations.intent`, and
it is the one migration on this list that a rolling deploy can break. While the
roll is in progress, a `web` replica still running v1.1.0 creates operations
the old way: it writes `operations.intent` and never touches
`operation_intents`. A v1.2.0 replica's `by_intent` lookup reads
`operation_intents` alone, so any operation the old replica created during the
roll is invisible to the nonce guard — a lost-redirect re-POST against it can
mint a second idempotency key, which is exactly what 0013 exists to prevent.

For v1.2.0, do one of the two:

- **Deploy non-rolling:** stop `web`, migrate, start `web`. No window where the
  two replica versions disagree about where a nonce lives.
- **Or, if you rolled anyway:** once every `web` replica is on v1.2.0, re-run
  the backfill once — it is idempotent, so running it again only picks up what
  the old replicas wrote during the roll:

  ```sql
  insert into operation_intents (intent, operation_id, created_at)
  select intent, id, created_at from operations where intent is not null
  on conflict do nothing;
  ```

### Health endpoints

| Endpoint | Auth | Answers | Use as |
|---|---|---|---|
| `GET /health/live` | none | `200 {"status":"ok"}` | liveness probe; restart on failure |
| `GET /health/ready` | none | `200 {"status":"ready"}` or `503 {"status":"unready"}` | readiness / load-balancer gate |

Both are deliberately unauthenticated — probes have no credentials — and both
are cheap. `/health/ready` runs `select 1`; its 503 body carries no exception
text, because a DSN carries credentials.

The worker has no endpoint, so it reports liveness two other ways.

**A heartbeat file.** Set `WORKER_HEARTBEAT_PATH` and the worker touches that
file after every successful loop pass; a healthcheck compares its mtime to now.
The compose reference does exactly this, with a staleness threshold of 3× the
reconcile interval:

```yaml
worker:
  environment:
    WORKER_HEARTBEAT_PATH: /tmp/worker.heartbeat
  healthcheck:
    test: ["CMD", "python", "-c", "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime(os.environ['WORKER_HEARTBEAT_PATH']) < 3 * float(os.environ.get('RECONCILE_INTERVAL_SECONDS', 60)) else 1)"]
```

Leave the setting unset and no file is written — the feature costs nothing when
it is not wanted.

**A non-zero exit.** After `WORKER_MAX_FAILED_PASSES` (default 10) *consecutive*
failures of one loop, the worker stops and exits 1, so `restart: unless-stopped`
or a Kubernetes `restartPolicy` acts instead of a queue growing quietly behind a
process that is technically up. One successful pass resets the count, so an
upstream blip does not trip it.

The functional check is still worth knowing: operations stuck in
`outcome_unknown` that never become `confirmed`, `rejected` or `stalled` mean
nothing is reconciling.

---

## 5. Backup and restore

### What is in the database

| Table | What is lost if it goes | Encrypted at rest |
|---|---|---|
| `operations` | The idempotency ledger. Losing it means a resubmitted mutation can pay twice. | `request_body` only |
| `drafts` | Operators' unsubmitted onboarding answers and their document references. | `payload` |
| `document_blobs` | Uploaded files awaiting submission/replay. | yes |
| `audit_events` | Who did what, and the price they agreed to on a conversion. | no (no secrets in it) |
| `webhook_events` | Undelivered-work queue; Conduit will not re-send. | `raw_body` only |
| `projections` | A cache of Conduit's own state. Rebuilt by the reconciler. | no |
| `session_revocations` | Which operators have been signed out, and when. Losing it un-revokes them. | no (no secrets in it) |

Nothing here is the source of truth for money. Conduit is. But the operations
ledger is the source of truth for *whether we already asked*, which is why it is
worth backing up.

### What ages out, and when

Every column above holding a payee's bank coordinates, postal address or a
customer's own answers is emptied by the worker on a schedule. Nothing else goes:
a purge NULLs the sensitive column and leaves the row, its status, its
timestamps and everything projected from it — the ledger stays complete, the
personal data does not.

| What | Window | When the clock starts | Never purged |
|---|---|---|---|
| `operations.request_body` and `.error` | `OP_BODY_RETENTION_DAYS` | the operation resolved | while `stalled` — it is retryable and a replay needs the exact body |
| `document_blobs` — bytes, filename, name, digest | `OP_BODY_RETENTION_DAYS` | its operation resolved | same rule as the body it belongs to |
| `payout_batch_rows.payload` (the assembled destination) | `OP_BODY_RETENTION_DAYS` | the batch was last touched | while the batch is `validating`, `ready` or `partially_dispatched` — those rows can still be dispatched |
| `webhook_events.raw_body` (the delivery verbatim) | `WEBHOOK_RAW_RETENTION_DAYS` | the worker processed the event | `pending`, `processing` and **`failed`** |
| `drafts.payload` (the operator's answers) | unsubmitted: `DRAFT_TTL_DAYS` untouched. Submitted: as soon as the application settles, and `OP_BODY_RETENTION_DAYS` after submission regardless | last edit / submission | a rejection still correctable, until that backstop — the alternative destroys the only copy of the operator's work |

**The one deliberate hole is a `failed` webhook event.** Its raw body is the only
evidence of why it is poison, and a human is expected to read it — so it is kept
at any age. If your inbox has failed rows, deal with them; they are not a queue
that drains itself, and each one is holding a delivery's payload indefinitely:

```sh
docker compose exec -T postgres psql -U console console \
  -c "select event_id, event_type, received_at, error from webhook_events where status = 'failed'"
```

The purges run in the worker's `RECONCILE_INTERVAL_SECONDS` tick. **A deployment
running only the `web` role never purges anything** — the worker is what makes
these windows real.

### Backup

```sh
# compose
docker compose exec -T postgres pg_dump -U console -Fc console > console-$(date +%F).dump

# anywhere
pg_dump "$DATABASE_URL_WITHOUT_THE_DRIVER_SUFFIX" -Fc > console-$(date +%F).dump
```

`pg_dump` does not take `postgresql+psycopg://` — strip the `+psycopg`.

### Restore

```sh
createdb console
pg_restore -d console --clean --if-exists console-2026-08-28.dump
docker run --rm --env-file .env conduit-console alembic upgrade head   # if the dump is older than the code
```

### ⚠ The encryption-key caveat

**A database backup without `ENCRYPTION_KEY` is a partial backup.**
`drafts.payload`, `operations.request_body` and `document_blobs.data` are
Fernet-encrypted with a key that lives only in the environment. Restore the dump
with a different key and:

- every unsubmitted draft is unreadable — the operators' work is gone;
- every stored request body is unreadable, so the reconciler cannot replay a
  `stalled` operation, and a `document_upload` cannot be retried at all;
- the rows themselves are still there and still count against the double-submit
  guard, so the failure looks like "the console is broken", not "the key is
  wrong".

Back the key up **separately from the dump**, in your secret store, and treat
losing it as data loss. Everything else — customers, accounts, transactions,
applications — is re-readable from Conduit and will repopulate.

### Key rotation

`ENCRYPTION_KEY` is a comma-separated list, **newest first**. The first key
encrypts; every listed key decrypts (`MultiFernet`). So:

1. Generate a new Fernet key.
2. Set `ENCRYPTION_KEY=<new>,<old>` and redeploy `web` and `worker`. New writes
   use the new key; old rows still decrypt.
3. Wait out the retention windows above — `OP_BODY_RETENTION_DAYS` (default 30)
   for request bodies, blobs and batch-row destinations,
   `WEBHOOK_RAW_RETENTION_DAYS` (default 30) for raw deliveries,
   `DRAFT_TTL_DAYS` (default 30) for abandoned drafts. Submitted drafts are
   purged once their application settles. A `failed` webhook event is never
   purged, so clear those first or its body will not decrypt after step 4.
4. Drop `<old>` from the list and redeploy.

There is no re-encrypt-in-place job, and deliberately so: the data ages out
faster than a rotation cadence needs to, and a rewrite job that touches every
encrypted row is a much bigger thing to get wrong than a list of two keys.
`SESSION_SECRET` rotation is simpler and harsher: change it and every operator
is logged out.

---

## 6. Webhooks

Webhooks are how the console learns that an application was approved or a payout
settled without polling. They are optional — the reconciler repairs everything
eventually — but without them "eventually" is `RECONCILE_INTERVAL_SECONDS`.

**The receiver is not mounted at all unless `CONDUIT_WEBHOOK_SECRET` is set.**
An unverifiable POST gets a 404, not a route that accepts anything.

1. Deploy first, with the console reachable at a public https URL. The path is
   **`POST /webhooks/conduit`**.
2. Register the endpoint with Conduit (`POST /v2/webhooks/endpoints`) for the
   event types the console consumes: `application.*`, `transaction.*`,
   `order.*`, `virtual_account.*`, `whitelist_recipient.*`, `customer.*`.
3. Copy the returned signing secret **verbatim, `whsec_` prefix included** into
   `CONDUIT_WEBHOOK_SECRET`, and redeploy. The prefix is part of the HMAC key;
   stripping it makes every valid delivery look tampered with.
4. Confirm: a delivery appears in `webhook_events` and moves to `processed`
   within a second or two. `processed_ignored` means the event was understood
   and had nothing to project — that is fine.

The reverse proxy must let `/webhooks/conduit` through **without** the identity
headers and without the auth check: it authenticates by signature. Do not put it
behind oauth2-proxy — behind a forward-auth gate, Conduit's unauthenticated POST
is answered with a login redirect and the delivery is lost. Both examples in
[§3](#3-the-proxy-mode-header-contract) do this as their *first* rule (Caddy:
`handle /webhooks/conduit` inside the `route`; nginx: `location = /webhooks/conduit`),
and both still strip the four identity headers on the way through — a caller
does not get to claim an identity just because a route ignores identity.

Bodies over 1 MiB are refused before they are read. Deliveries are verified,
stored, then processed by the worker — so a slow projection can never make
Conduit's delivery time out.

---

## 7. Logs

Plain `logging` to stdout, at whatever level your runtime sets (`INFO` for the
worker's `__main__`). Ship them however you ship container logs.

**What is guaranteed not to appear in them**, by construction rather than by a
scrubbing filter:

- **The Conduit API key.** It is only ever a request header built inside the
  client; nothing formats it, and the `Settings` object that holds it uses
  pydantic `SecretStr` and is never logged.
- **Session, CSRF, proxy and OIDC secrets.** Same. Startup-guard failures name
  the *setting* and the *reason* ("is shorter than 32 characters"), never the
  value — there is a test that asserts the rejected value does not appear in the
  message.
- **Draft answers, request bodies and uploaded files.** They live encrypted in
  the database and are never passed to a log call. Conduit request logging is
  `method`, `path`, and the exception *type* — not the body, not the response.
- **Database credentials.** `/health/ready`'s 503 body and its log line carry no
  exception text, because the DSN would be in it.

What *is* logged, and is worth alerting on: `webhook rejected: signature did not
verify`, `proxy auth rejected: …`, `csrf rejected: …`, `unknown … status …
rendered neutrally` (Conduit shipped an enum value this build does not know),
and `worker … pass failed`.

One honest caveat: a webhook event that fails to process logs the exception's
own string (`webhook event <id> failed …`). A malformed payload's decoder error
can quote a fragment of that payload. It is a webhook envelope, not a draft or a
key, but it is the one place log content is not fully under this app's control.

---

## 8. Fonts

Nothing to do here in a normal deployment — the two OFL faces the console needs
(**DM Sans**, **JetBrains Mono**) are in the image, at `static/fonts/`, with
their licences beside them.

One optional step exists, and only Conduit's licence holder can take it. The
console's heading stack is

    --font-heading: "Founders Grotesk", "DM Sans", system-ui, …

and **Founders Grotesk is Klim Type Foundry's commercial face, so it is not in
this repo** and never will be. Two `@font-face` rules in `static/styles.css`
already point at it. To turn headings from DM Sans into Conduit's real type,
drop these two files in:

    static/fonts/founders-grotesk-regular.woff2     (weight 400 — h1)
    static/fonts/founders-grotesk-medium.woff2      (weight 500 — h2, brand mark, stat figures)

**No code change follows.** No stylesheet edit, no rebuild step beyond baking
the two files into the image, no configuration. The rules are already there;
the browser starts using them the moment the files answer. Conduit's own site
also ships a Light (300) face — this console has no consumer for it, so leaving
it out changes nothing.

If the files are absent (the default), every heading renders in DM Sans. That is
the fallback working, not a fault, and `tests/test_fonts.py` asserts it stays
that way so a commercial face is never quietly vendored into this repo.

`jetbrains-mono.woff2` is **subset** — it is not upstream's file byte for byte.
Upstream ships Cyrillic, Greek, APL and box-drawing glyphs plus the code
ligatures, and this console sets nothing in mono but machine values, so it is
cut to Latin and the punctuation the templates emit: 111 KB → 32 KB. The exact
`pyftsubset` command is recorded in the `@font-face` comment in
`static/styles.css`, and `tests/test_fonts.py` holds a size ceiling so a
well-meaning re-download cannot silently undo it. `dm-sans.woff2` is upstream
whole — it is the body face and it takes customer names.

One more static file worth knowing about, though it is not a font:
`static/theme.js`. It is a small, blocking script in `<head>` — it has to run
before the first paint to stamp `System`/`Light`/`Dark` onto the page, so it
cannot be deferred or async'd the way `app.js` at the foot of the body is.
Same-origin, served from this app like every other file under `static/`, so
the `script-src 'self'` Content-Security-Policy already covers it; there is
nothing to allowlist.

---

## Validation runs

A first run against **production** keys — the small-money exercise that proves
the wiring, not a load test — sets `MONEY_CEILING` to the largest single amount
that run is allowed to move, e.g. `MONEY_CEILING=25.00`.

What that buys, and what it does not:

- **Buys:** every operation this console creates — a single payout, a
  virtual-account transfer, each row of a batch *and* the batch's own total, and
  a conversion order's source amount — is refused locally before it becomes an
  operation. A slipped decimal point is a form error, not a wire.
- **Does not buy:** a spend limit. It is per operation, not per day: ten
  payouts of `25.00` are ten payouts. Conduit's own limits are the ones that
  bound the total.
- **Does not buy:** a re-check on an operation that already exists. The guard
  runs where the amount is held, which is at creation, so `POST
  /orders/{id}/execute` and `POST /operations/{id}/retry` act on a body that was
  written before the ceiling existed. Set it before the run, not partway
  through.
- The operator sees it: the number is on the environment strip beside the host,
  so a refusal is never a mystery.

Unset it again — or leave it set — once the run is done; empty is the behaviour
every deployment had before it existed.

The environment badge itself only names the environment and the ceiling now —
`Sandbox · ceiling 5000`, `Staging`, `Production`. The host and `AUTH_MODE`
haven't left the UI; they moved into the badge's `title` tooltip, so hover it
when you need to confirm which host or auth mode an operator is looking at.

## Environment reference

Every setting `app/config.py` reads, plus the two the entrypoint reads. Required
means "the app will not start without it".

### Conduit connection

| Variable | Default | Notes |
|---|---|---|
| `CONDUIT_ENV` | `sandbox` | `sandbox` \| `staging` \| `production`. Picks the host from a hardcoded allowlist and sets how strict the startup guards are (see the matrix in the root README). |
| `CONDUIT_API_KEY` | *(empty)* | Your organization's key for that environment. Empty starts, but every call 401s. |
| `CONDUIT_API_BASE` | unset | Optional explicit origin. Must equal the allowlist entry for `CONDUIT_ENV` or the app refuses to start. Normally leave unset. |
| `CONDUIT_WEBHOOK_SECRET` | *(empty)* | `whsec_` + 64 hex, verbatim from Conduit. Empty = the receiver route is not mounted. |

### Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | **required** | `postgresql+psycopg://…`. Serves both the async app and Alembic. |

### Security

| Variable | Default | Notes |
|---|---|---|
| `SESSION_SECRET` | **required** | Signs session and CSRF cookies. 32+ chars, no placeholder text, outside sandbox. Rotating it logs everyone out. |
| `ENCRYPTION_KEY` | **required** | Comma-separated Fernet keys, **newest first**. First encrypts, all decrypt. See [key rotation](#key-rotation) and the [caveat](#-the-encryption-key-caveat). |
| `AUTH_MODE` | `proxy` | `proxy` \| `oidc` \| `disabled`. `disabled` is refused outside `CONDUIT_ENV=sandbox` — staging carries a live key against a host that self-labels "Production". |
| `MONEY_CEILING` | *(empty)* | A decimal string. When set, **every operation this console creates** is refused locally if its amount is above it — single payout, virtual-account transfer, each batch row, the batch total, and a conversion order's source amount. The refusal is a form error naming the ceiling, and it happens before the operations ledger is written, so a refused submit is not an operation. Asset-agnostic: one number, shown on the environment strip beside the host. Empty (the default) means no ceiling. A non-positive or unparseable value refuses to start. See [validation runs](#validation-runs). |
| `SESSION_MAX_AGE_SECONDS` | `43200` | Session lifetime (12 h). |
| `COOKIE_SECURE` | `true` | `false` accepted only with `CONDUIT_ENV=sandbox`. When on, cookies carry the `__Host-` prefix, so no sibling subdomain can plant one. |
| `AUTH_ROLE_MAP` | *(empty)* | `group=role` pairs, comma-separated, e.g. `conduit-ops=operator,conduit-admins=admin`. Unlisted values map to a role of the same name; anything else grants nothing. |
| `ROLES_FILE` | *(empty)* | Path to a JSON file of custom roles — `{"role name": ["permission", ...]}`. Merged with the three built-ins, which it may not redefine. Every name must be in [PERMISSIONS.md](../PERMISSIONS.md); anything else refuses to start. See [custom roles](#custom-roles). |

### `AUTH_MODE=proxy`

| Variable | Default | Notes |
|---|---|---|
| `PROXY_SHARED_SECRET` | *(empty)* | **Required in this mode** — the app will not start without it. Identity headers are trusted only alongside this value. |
| `PROXY_SECRET_HEADER` | `X-Proxy-Auth` | Header carrying the shared secret. |
| `PROXY_USER_HEADER` | `X-Auth-Request-User` | User id. |
| `PROXY_EMAIL_HEADER` | `X-Auth-Request-Email` | Email; recorded on every operation. |
| `PROXY_GROUPS_HEADER` | `X-Auth-Request-Groups` | Comma-separated groups. |

### `AUTH_MODE=oidc`

| Variable | Default | Notes |
|---|---|---|
| `OIDC_ISSUER` | *(empty)* | **Required in this mode.** https, or an http loopback for local dev only. |
| `OIDC_CLIENT_ID` | *(empty)* | **Required in this mode.** |
| `OIDC_CLIENT_SECRET` | *(empty)* | **Required in this mode.** 32+ chars, no placeholder text, outside sandbox. |
| `OIDC_CALLBACK_PATH` | `/auth/callback` | Register this path with the IdP. Must be an absolute path of at least two segments with no trailing slash — the callback is mounted as an anonymous prefix, so a shorter value would make every path anonymous, and the app refuses to start on one. |
| `OIDC_REDIRECT_URI` | *(empty)* | Absolute callback URL, used verbatim. **Required and https with `CONDUIT_ENV=production`**; otherwise derived from the request, which is wrong behind a TLS-terminating proxy. |
| `OIDC_ROLE_CLAIM` | `roles` | ID-token claim holding the group/role values. |
| `OIDC_SCOPES` | `openid email profile` | Scopes requested. |

### Operations engine (`OPERATIONS_SPEC.md` §6)

| Variable | Default | Notes |
|---|---|---|
| `OP_CLIENT_TIMEOUT` | `30.0` | Seconds per Conduit call. |
| `OP_STALE_INFLIGHT_FACTOR` | `2.0` | An `in_flight` operation older than timeout × this is stale and gets reconciled. |
| `OP_CREATED_TTL_SECONDS` | `86400` | Never-sent operations become `abandoned` after this. |
| `OP_BODY_RETENTION_DAYS` | `30` | Purge `request_body`, document blobs and finished batches' row destinations this long after a terminal state. Also the window a rotated encryption key must outlive. |
| `WEBHOOK_RAW_RETENTION_DAYS` | `30` | Purge a webhook delivery's `raw_body` this long after the worker processed it. Unprocessed and `failed` events keep theirs at any age — see § 5. |
| `DRAFT_TTL_DAYS` | `30` | Unsubmitted drafts discarded after this long untouched. |
| `RECONCILE_MAX_ATTEMPTS` | `5` | Then the operation is `stalled` and a human is asked. |
| `RECONCILE_INTERVAL_SECONDS` | `60` | Worker sweep interval. The webhook inbox drains every second regardless. |
| `PROJECTION_STALE_SECONDS` | `900` | Re-read a non-terminal projection untouched for longer than this. |
| `RECONCILE_REQUEST_BUDGET` | `100` | Conduit requests one reconciler pass may spend, so it cannot starve interactive traffic into rate limiting. |
| `WORKER_HEARTBEAT_PATH` | *(empty)* | File the worker touches after every successful loop pass, for a container healthcheck to stat. Empty writes nothing. |
| `WORKER_MAX_FAILED_PASSES` | `10` | Consecutive failed passes of one loop before the worker exits non-zero and lets the restart policy act. One good pass resets the count. |

**Tuning under load — guidance, not benchmarks.** The three that matter move
together: lowering `RECONCILE_INTERVAL_SECONDS` and raising
`RECONCILE_REQUEST_BUDGET` resolve `outcome_unknown` operations sooner, and buy
that with Conduit requests the operators' own pages then compete for — 429s from
the reconciler are the signal you have gone too far. Raise
`PROJECTION_STALE_SECONDS` instead if the pressure is coming from re-reads of
resources that are simply still settling. Change one at a time and watch the
reconciler's request count; none of these numbers has been measured at volume.

### Conduit read policy

Reads retry; **mutations never do** — an ambiguous one becomes `outcome_unknown`
and is resolved by reading the resource back.

| Variable | Default | Notes |
|---|---|---|
| `READ_MAX_ATTEMPTS` | `4` | GET attempts on 429 (honouring `Retry-After`), 5xx and transport errors. |
| `READ_BACKOFF_BASE_SECONDS` | `0.25` | Initial backoff. |
| `READ_BACKOFF_MAX_SECONDS` | `8.0` | Backoff ceiling. |

### Container entrypoint

Read by `docker-entrypoint.sh`, not by `app/config.py`.

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `8000` | Port the `web` command binds (always on `0.0.0.0` inside the container). |
| `RUN_MIGRATIONS` | `0` | `1` runs `alembic upgrade head` before the role starts. Prefer a one-shot migration container for multi-replica deployments. |
