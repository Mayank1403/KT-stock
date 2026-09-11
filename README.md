# Kanhaiya Textile — Tally Dashboard

Rewritten, organized version of the original single-file script.

## What was actually causing the timeout

```
Tally fetch error for Sale Voucher: HTTPConnectionPool(host='localhost', port=9000):
Read timed out. (read timeout=15)
```

The old code asked Tally to export **every Sale Voucher ever entered**, with
no date bound, and gave it only 15 seconds to respond. As the voucher
ledger grows, that export gets big enough that Tally can't serialize and
return it in time — a bigger timeout only delays the same failure.

This rewrite fixes it at the source:

- **Date-windowed fetching** (`tally_client.py`, `config.VOUCHER_WINDOW_DAYS`,
  default 90 days): each voucher export now uses `SVFROMDATE`/`SVTODATE` so
  Tally only has to serialize a recent slice, not the whole history. Raise
  `VOUCHER_WINDOW_DAYS` if you need to see further back by default.
- **Reused, retrying connection**: one pooled `requests.Session` with an
  automatic exponential-backoff retry for connection drops and 5xx
  responses, instead of a fresh connection (and no retry) per call.
- **Realistic, separate connect/read timeouts**: connect fails fast (Tally
  not running), read gets a real budget for a genuine export
  (`TALLY_READ_TIMEOUT`, default 45s).
- **Sale + Purchase fetched concurrently** instead of one after another —
  roughly halves wall-clock time for the vouchers page.
- **Background cache refresh** (`cache.py`): a daemon thread keeps the
  stock/voucher caches warm on its own schedule. A normal page load reads
  memory and essentially never blocks on a live Tally call — only the very
  first request after a cold start does, and even that benefits from the
  windowed export above.

## Project layout

```
config.py            All tunables (env-var driven)
xml_utils.py          XML cleaning + formatting helpers
tally_client.py        Everything that talks to Tally over HTTP
parsers.py              Turns Tally XML into plain Python lists/dicts
cache.py                 Stale-while-revalidate cache + background refresher
app.py                    Flask routes only
templates/                Jinja2 templates (was one giant Python string)
static/style.css           Shared list-page styling
static/detail.css           Voucher detail page styling
```

The UI/UX (search, sort, client-side pagination, voucher drill-down) is
unchanged — it was already reasonably fast client-side. What changed is how
reliably and quickly the server gets data out of Tally in the first place.

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000.

## Tuning via environment variables

| Variable                      | Default | Meaning |
|--------------------------------|---------|---------|
| `TALLY_URL`                    | `http://localhost:9000` | Tally's ODBC/HTTP endpoint |
| `TALLY_CONNECT_TIMEOUT`        | `5`     | Seconds to wait for Tally to accept a connection |
| `TALLY_READ_TIMEOUT`           | `45`    | Seconds to wait for Tally to finish sending a response |
| `TALLY_MAX_RETRIES`            | `2`     | Retries on connection errors / 5xx |
| `VOUCHER_WINDOW_DAYS`          | `90`    | How far back the voucher list page looks by default |
| `VOUCHER_MAX_WINDOW_DAYS`      | `730`   | Lookback window used when opening a single voucher's detail |
| `CACHE_TTL`                    | `1800`  | How long cached data is considered fresh (seconds) |
| `BACKGROUND_REFRESH_ENABLED`   | `1`     | Set to `0` to disable the background refresh thread |
| `BACKGROUND_REFRESH_INTERVAL`  | = `CACHE_TTL` | How often the background thread refreshes each cache |
| `PORT`                         | `5000`  | Flask port |

If your Sale/Purchase ledger is still too large to export within
`TALLY_READ_TIMEOUT` even at 90 days, shrink `VOUCHER_WINDOW_DAYS` (e.g. to
`30`) rather than raising the timeout indefinitely.
