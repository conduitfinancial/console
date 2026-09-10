# One image, two commands (plan v2 §8): `web` and `worker`.
#
#   docker build -t conduit-console .
#   docker run --env-file .env -p 8000:8000 conduit-console web
#   docker run --env-file .env conduit-console worker
#
# The build stage carries uv and a compiler-free wheel install; the runtime
# stage carries the virtualenv, the app, and pyproject.toml/uv.lock (copied
# along for the record, not read by anything at runtime) — no uv, no dev
# group, no Playwright. `--no-dev` is what keeps the browser test dependency
# out of the image.

# Pinned by digest, tag kept for humans.
#
# Both FROM lines must move together, by hand where dependabot moves only one:
# the venv this stage builds is copied whole into the runtime stage, so a
# mismatch fails on first import rather than at build time. The group in
# .github/dependabot.yml batches whatever updates a run finds; it cannot raise
# one for an image that has none, which is how the stages drifted apart before.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1 AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY static ./static
# The operator-run scripts (`revoke_sessions`, `backfill_accounts`). They are
# levers for a deployed console, so they have to be where the console is —
# documented in deploy/README as `docker compose exec web python scripts/…`.
COPY scripts ./scripts
RUN uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS runtime

# Non-root, no shell. `--create-home` does make /home/console — useradd's
# default, kept rather than fought — but nothing in the app writes there.
RUN useradd --system --create-home --shell /usr/sbin/nologin console

WORKDIR /app
COPY --from=build --chown=root:root /app /app
COPY --chown=root:root docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER console
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["web"]
