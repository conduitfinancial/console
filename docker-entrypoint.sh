#!/bin/sh
# Dispatch the image's two runtime roles, and optionally migrate first.
#
#   docker run … conduit-console web      # uvicorn on $PORT (default 8000)
#   docker run … conduit-console worker   # python -m app.worker
#   docker run … conduit-console <cmd…>   # anything else runs verbatim
#
# RUN_MIGRATIONS=1 runs `alembic upgrade head` before either role. Leave it off
# and run the migration as its own one-shot container (deploy/README.md) when
# more than one replica starts at once — Alembic takes a lock, so concurrent
# upgrades are safe but pointless.
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
	echo "entrypoint: alembic upgrade head" >&2
	alembic upgrade head
fi

# `exec` so the role becomes PID 1 and receives SIGTERM directly: uvicorn drains
# its connections, and the worker's own SIGTERM handler stops both of its loops.
case "${1:-web}" in
web)
	exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}"
	;;
worker)
	exec python -m app.worker
	;;
*)
	exec "$@"
	;;
esac
