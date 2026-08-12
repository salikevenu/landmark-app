import os
import sys

def _cfg_log(msg):
    print(f"[gunicorn.conf] {msg}", file=sys.stderr, flush=True)

# Prefer Render-injected PORT; never hardcode the listen port alone.
port = os.environ.get("PORT") or "10000"
bind = f"0.0.0.0:{port}"
workers = 1
worker_class = "sync"
threads = 1
# Generous timeout so a slow (but finite) boot is not mistaken for a hang loop
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = False
preload_app = False
reuse_port = False

_cfg_log(f"PORT env={os.environ.get('PORT')!r} bind={bind!r} workers={workers} timeout={timeout}")


def on_starting(server):
    _cfg_log("on_starting — master process beginning")


def when_ready(server):
    _cfg_log(f"when_ready — master listening on {bind} (TCP bind complete)")


def post_worker_init(worker):
    _cfg_log(f"post_worker_init — worker pid={worker.pid} finished booting")


def worker_abort(worker):
    _cfg_log(f"worker_abort — worker pid={worker.pid} aborted (check timeout/boot hang)")
