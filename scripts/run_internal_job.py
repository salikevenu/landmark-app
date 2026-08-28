"""POST an internal LANDMARK job. Used by Render cron (not a second payout/commission implementation)."""
import os
import sys
import urllib.error
import urllib.request


def main():
    if len(sys.argv) < 2 or not sys.argv[1].startswith("/internal/"):
        raise SystemExit("usage: run_internal_job.py /internal/<job>")
    path = sys.argv[1]
    base = (os.getenv("BASE_URL") or "https://landmarkvts.in").rstrip("/")
    secret = (os.getenv("SATURDAY_PAYOUT_SECRET") or "").strip()
    if not secret:
        raise SystemExit("SATURDAY_PAYOUT_SECRET is required")
    req = urllib.request.Request(
        base + path,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(resp.status, body)
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode("utf-8", errors="replace"))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
