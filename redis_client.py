import os
import redis

_redis_client = None

def get_redis_client():
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        _redis_client = redis.from_url(url)
    return _redis_client