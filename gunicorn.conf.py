import os
import sys

def _cfg_log(msg):
    print(f"[gunicorn.conf] {msg}", file=sys.stderr, flush=True)

# Prefer Render-injected PORT; never hardcode the listen port alone.
port = os.environ.get("PORT") or "10000"
bind = f"0.0.0.0:{port}"
# One worker keeps memory flat and keeps the in-process fallbacks (the
# rate limiter and the JWT revocation blocklist, both of which degrade to
# process memory when Redis is unreachable) consistent -- a second worker
# would silently double every "3 per hour" limit and let a logged-out
# token stay valid on the other worker. Concurrency is therefore bought
# with THREADS, not processes.
#
# Threads matter on the auth path specifically: every OTP send is a
# blocking outbound HTTPS call to Message Central with a (5, 15) timeout,
# and OTP verify retries that up to 3 times. With threads = 1 a single
# slow provider call stalls EVERY other request on the site -- dashboards,
# listings, payments -- for up to ~20s. Threads let those requests keep
# being served while an OTP call is in flight. Safe here because the
# request path holds no shared mutable state: OTP state lives in
# Postgres, and the two in-process fallbacks above are already lock-guarded.
workers = 1
worker_class = "gthread"
# INVARIANT: database/init_db.py's pool_size + max_overflow must comfortably
# exceed this number -- every thread can hold a DB connection concurrently,
# and start.sh must not pass --workers/--worker-class/--threads flags of its
# own (CLI flags override this config file), or this setting silently stops
# being the one actually in effect. tests/test_pool_sizing.py asserts the
# pool-vs-threads half of this; there is no automated guard for the
# start.sh half short of reading it here too, which this comment does by hand.
threads = 4
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

_cfg_log(
    f"PORT env={os.environ.get('PORT')!r} bind={bind!r} workers={workers} "
    f"worker_class={worker_class} threads={threads} timeout={timeout}"
)


def on_starting(server):
    _cfg_log("on_starting — master process beginning")


def when_ready(server):
    _cfg_log(f"when_ready — master listening on {bind} (TCP bind complete)")


def post_worker_init(worker):
    _cfg_log(f"post_worker_init — worker pid={worker.pid} finished booting")


def worker_abort(worker):
    _cfg_log(f"worker_abort — worker pid={worker.pid} aborted (check timeout/boot hang)")
