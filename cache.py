"""
A small stale-while-revalidate cache.

The original script's caching only saved a request from re-hitting Tally if
the SAME request happened to land inside the TTL window — the very first
request after expiry (or after every restart, since disk-load didn't
count as "fresh") still had to wait on a live Tally call synchronously,
which is exactly the request that timed out.

This version separates "keeping data fresh" from "serving a request":
a background thread refreshes each cache on its own schedule, so a user
request almost always just reads memory. If Tally is genuinely down, the
last good data is served with source="offline cache" instead of failing.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("cache")


class DataCache:
    def __init__(self, name: str, fetch_fn, parse_fn, ttl: int, cache_dir: str):
        self.name = name
        self.fetch_fn = fetch_fn
        self.parse_fn = parse_fn
        self.ttl = ttl
        self.file = os.path.join(cache_dir, f"{name}_cache.json")
        self._lock = threading.RLock()
        self.data = []
        self.timestamp = None
        self.source = "none"
        self._load_disk()

    # ── disk persistence ─────────────────────────────────────────────
    def _load_disk(self):
        if not os.path.exists(self.file):
            return
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            with self._lock:
                self.data = saved.get("data", [])
                self.timestamp = saved.get("timestamp")
                self.source = "disk"
            log.info("%s: loaded %d items from disk cache", self.name, len(self.data))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("%s: could not load disk cache: %s", self.name, e)

    def _save_disk(self):
        try:
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"data": self.data, "timestamp": self.timestamp}, f)
            os.replace(tmp, self.file)  # atomic — avoids a half-written cache file
        except OSError as e:
            log.warning("%s: could not save disk cache: %s", self.name, e)

    # ── freshness ────────────────────────────────────────────────────
    def is_fresh(self) -> bool:
        with self._lock:
            return bool(self.timestamp) and (time.time() - self.timestamp) < self.ttl

    # ── core refresh (called by both requests and the background thread) ──
    def _do_refresh(self):
        try:
            raw = self.fetch_fn()
            items = self.parse_fn(raw)
        except Exception as e:
            log.warning("%s: refresh failed: %s", self.name, e)
            return False
        if items:
            with self._lock:
                self.data = items
                self.timestamp = time.time()
                self.source = "live tally"
            self._save_disk()
            log.info("%s: refreshed, %d items", self.name, len(items))
            return True
        log.info("%s: Tally returned 0 items, keeping previous data", self.name)
        return False

    # ── public API ───────────────────────────────────────────────────
    def get(self, force: bool = False):
        """Serve from memory if fresh; otherwise refresh synchronously
        (only happens on cold start or if the background thread is
        disabled/behind) and fall back to stale data if Tally fails."""
        if not force and self.is_fresh():
            with self._lock:
                self.source = "live cache"
                return self.data
        if self._do_refresh():
            with self._lock:
                return self.data
        with self._lock:
            if self.data:
                self.source = "offline cache"
            return self.data

    def refresh_in_background(self):
        """Fire-and-forget refresh; never raises."""
        try:
            self._do_refresh()
        except Exception:
            log.exception("%s: background refresh crashed", self.name)

    def invalidate(self):
        with self._lock:
            self.timestamp = None


class BackgroundRefresher:
    """Runs `cache.refresh_in_background()` for each registered cache on
    its own timer, off the request path entirely."""

    def __init__(self, interval: int):
        self.interval = interval
        self._caches = []
        self._thread = None
        self._stop = threading.Event()

    def register(self, cache: DataCache):
        self._caches.append(cache)

    def _loop(self):
        while not self._stop.wait(self.interval):
            for c in self._caches:
                c.refresh_in_background()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="cache-refresher")
        self._thread.start()
        log.info("Background refresher started (interval=%ss)", self.interval)

    def stop(self):
        self._stop.set()
