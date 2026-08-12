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
exec gunicorn app:app \
  --config gunicorn.conf.py \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --worker-class sync \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --log-level info \
  --access-logfile - \
  --error-logfile -
