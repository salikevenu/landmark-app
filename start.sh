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

# Explicit CLI bind so Render's scanner always sees 0.0.0.0:$PORT
# even if config loading fails for any reason.
exec gunicorn app:app \
  --config gunicorn.conf.py \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --worker-class sync \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --log-level info \
  --access-logfile - \
  --error-logfile -
