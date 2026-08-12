import os
import redis

_redis_client = None

def get_redis_client():
    """Lazy Redis client — never connects at import time."""
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        # Hard timeouts so a missing/unreachable Redis cannot hang workers
        _redis_client = redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
        )
    return _redis_client
