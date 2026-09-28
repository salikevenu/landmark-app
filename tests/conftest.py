"""Suite-wide test environment.

app.py refuses to start without JWT_SECRET_KEY (no hardcoded fallback), so
set a test-only secret before any test module imports it. Modules that set
their own value with os.environ.setdefault keep working unchanged.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
