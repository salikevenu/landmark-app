import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

try:
    raise ValueError("test")
except Exception as e:
    logger.error(f"Error: {e}")
