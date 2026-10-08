"""
Central configuration for the Tally Dashboard.
Everything here can be overridden with environment variables, so the app
can be tuned per-deployment without touching code.
"""
import os


class Config:
    # ── Tally connection ────────────────────────────────────────────
    TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")

    # requests supports a (connect_timeout, read_timeout) tuple. The old
    # code used a single flat timeout (10-30s) for requests that could
    # legitimately take longer once the voucher ledger grows — that's
    # what produced the "Read timed out" error. Connect stays short
    # (Tally is local, if it's not listening we want to fail fast);
    # read is generous because large exports genuinely take a while.
    TALLY_CONNECT_TIMEOUT = float(os.environ.get("TALLY_CONNECT_TIMEOUT", 5))
    TALLY_READ_TIMEOUT = float(os.environ.get("TALLY_READ_TIMEOUT", 45))

    # Automatic retry on transient failures (connection refused, 5xx,
    # or a timeout on a retryable request). Exponential backoff.
    TALLY_MAX_RETRIES = int(os.environ.get("TALLY_MAX_RETRIES", 2))
    TALLY_RETRY_BACKOFF = float(os.environ.get("TALLY_RETRY_BACKOFF", 1.0))

    # ── Voucher fetch windowing (this is the actual timeout fix) ────
    # Instead of asking Tally for "every Sale/Purchase voucher ever
    # entered", we fetch a rolling window and let the UI page further
    # back in time on demand. This keeps each XML export small and fast.
    VOUCHER_WINDOW_DAYS = int(os.environ.get("VOUCHER_WINDOW_DAYS", 7))
    VOUCHER_MAX_WINDOW_DAYS = int(os.environ.get("VOUCHER_MAX_WINDOW_DAYS", 15))

    # ── Caching ──────────────────────────────────────────────────────
    CACHE_DIR = os.environ.get("CACHE_DIR", os.path.dirname(os.path.abspath(__file__)))
    CACHE_TTL = int(os.environ.get("CACHE_TTL", 1800))  # 30 min

    # Background refresh keeps the cache warm so real user requests
    # almost never wait on Tally directly (stale-while-revalidate).
    BACKGROUND_REFRESH_ENABLED = os.environ.get("BACKGROUND_REFRESH_ENABLED", "1") == "1"
    BACKGROUND_REFRESH_INTERVAL = int(os.environ.get("BACKGROUND_REFRESH_INTERVAL", CACHE_TTL))

    # ── Misc ─────────────────────────────────────────────────────────
    PORT = int(os.environ.get("PORT", 5000))
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
