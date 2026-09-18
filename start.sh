#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-10000}"

echo "===================================="
echo "LANDMARK start.sh"
echo "PORT=${PORT}"
echo "PWD=$(pwd)"
echo "Python=$(command -v python || true)"
echo "Gunicorn=$(command -v gunicorn || true)"
ls -la app.py gunicorn.conf.py || true
echo "===================================="

# exec replaces this shell — process does NOT background with & and exit early.
# Module path is app:app (Flask instance in app.py).
#
# workers/worker-class/threads are deliberately NOT repeated here as CLI
# flags: gunicorn's precedence is CLI > config file, so a flag here would
# silently override gunicorn.conf.py's worker_class="gthread"/threads
# setting back to the old single-threaded "sync" default -- which is
# exactly what happened before this fix (this file said --worker-class
# sync while gunicorn.conf.py said "gthread", and the CLI flag always
# won, so the gthread/threads config had never actually taken effect).
# One source of truth: gunicorn.conf.py, loaded via --config below.
exec gunicorn app:app \
  --config gunicorn.conf.py \
  --bind "0.0.0.0:${PORT}" \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --log-level info \
  --access-logfile - \
  --error-logfile -
