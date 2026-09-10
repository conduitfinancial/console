#!/usr/bin/env bash
# Local dev launcher: real Conduit sandbox, dev auth, local Postgres.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source ../.env; source .env.local; set +a
export CONDUIT_ENV=sandbox
export CONDUIT_API_KEY="${CONDUIT_SANDBOX_API_KEY:?paste the sandbox key in ../.env}"
export DATABASE_URL="postgresql+psycopg://mc_bot@/conduit_console_local?host=/tmp"
export AUTH_MODE=disabled
export COOKIE_SECURE=false
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn app.main:create_app --factory --port "${PORT:-8900}"
