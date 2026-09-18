"""Regression guard: the DB connection pool must be able to serve every
gunicorn thread concurrently.

database/init_db.py's pool (pool_size + max_overflow) is the maximum
number of connections the app can hand out at once. gunicorn.conf.py's
`threads` is the maximum number of requests one worker can be running at
once (workers=1 here, so this is the whole process). If threads ever
exceeds what the pool can serve, some requests block on checkout and,
past pool_timeout, fail with a pool-exhaustion TimeoutError -- exactly
the mechanism this suite exists to catch before it reaches production.

Reads both files as plain text (regex) rather than importing them, so
this test needs no database connection and doesn't execute
gunicorn.conf.py's module-level logging side effects.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _first_int(pattern, text, label):
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"Could not find {label} in source")
    return int(match.group(1))


class TestPoolSizing(unittest.TestCase):
    def test_pool_capacity_covers_gunicorn_threads(self):
        init_db_src = _read("database/init_db.py")
        gunicorn_src = _read("gunicorn.conf.py")

        pool_size = _first_int(r"pool_size\s*=\s*(\d+)", init_db_src, "pool_size")
        max_overflow = _first_int(r"max_overflow\s*=\s*(\d+)", init_db_src, "max_overflow")
        threads = _first_int(r"threads\s*=\s*(\d+)", gunicorn_src, "threads")

        pool_capacity = pool_size + max_overflow
        self.assertGreaterEqual(
            pool_capacity,
            threads,
            f"database/init_db.py's pool (pool_size={pool_size} + "
            f"max_overflow={max_overflow} = {pool_capacity}) cannot cover "
            f"gunicorn.conf.py's threads={threads}. Every gunicorn thread "
            "can hold a DB connection concurrently -- resize the pool "
            "(or reduce threads) so pool_size + max_overflow >= threads.",
        )

    def test_start_sh_does_not_override_worker_settings(self):
        """start.sh must not pass --workers/--worker-class/--threads as CLI
        flags -- gunicorn's CLI-over-config-file precedence would silently
        negate gunicorn.conf.py's worker_class/threads setting, which is
        exactly what let 04f51c1's concurrency change never actually take
        effect in production."""
        start_sh_src = _read("start.sh")
        exec_block = start_sh_src[start_sh_src.index("exec gunicorn"):]
        for flag in ("--workers", "--worker-class", "--threads"):
            self.assertNotIn(
                flag,
                exec_block,
                f"start.sh passes {flag} as a CLI flag, which overrides "
                "gunicorn.conf.py's setting of the same name and silently "
                "reintroduces the pool-vs-threads mismatch this test guards "
                "against.",
            )


if __name__ == "__main__":
    unittest.main()
