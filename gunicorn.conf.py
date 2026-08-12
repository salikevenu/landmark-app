import os
import sys

port = os.environ.get("PORT", "10000")
bind = f"0.0.0.0:{port}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "sync"
threads = 1
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = False
preload_app = False
reuse_port = False

print(f"[gunicorn.conf] bind={bind!r} workers={workers}", file=sys.stderr, flush=True)


def when_ready(server):
    print(f"[gunicorn] READY listening on {bind}", file=sys.stderr, flush=True)


def post_worker_init(worker):
    print(f"[gunicorn] worker booted pid={worker.pid}", file=sys.stderr, flush=True)
