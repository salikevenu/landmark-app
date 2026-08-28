"""JWT JTI revocation store (logout blocklist).

Uses Redis when REDIS_URL is reachable so revocation is shared across
gunicorn workers; otherwise falls back to process memory (same multi-worker
caveat as the in-memory rate limiter).
"""
import logging
import threading
import time

from redis_client import get_redis_client

logger = logging.getLogger(__name__)

_PREFIX = "jwt_blocklist:"
_lock = threading.Lock()
_memory = {}


def _ttl_seconds(exp):
    if not exp:
        return 7 * 24 * 3600
    try:
        ttl = int(float(exp) - time.time())
    except (TypeError, ValueError):
        return 7 * 24 * 3600
    return max(ttl, 1)


def revoke_jti(jti, exp=None):
    if not jti:
        return
    ttl = _ttl_seconds(exp)
    key = f"{_PREFIX}{jti}"
    client = get_redis_client()
    if client is not None:
        try:
            client.set(key, "1", ex=ttl)
            return
        except Exception:
            logger.exception("jwt blocklist redis set failed; using memory")
    expires_at = time.time() + ttl
    with _lock:
        _memory[jti] = expires_at


def is_revoked(jti):
    if not jti:
        return False
    client = get_redis_client()
    if client is not None:
        try:
            if client.get(f"{_PREFIX}{jti}"):
                return True
        except Exception:
            logger.exception("jwt blocklist redis get failed")
    now = time.time()
    with _lock:
        exp = _memory.get(jti)
        if exp is None:
            return False
        if exp <= now:
            _memory.pop(jti, None)
            return False
        return True


def reset_memory_for_tests():
    with _lock:
        _memory.clear()
