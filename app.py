import logging

from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request

from cache import DataCache, BackgroundRefresher
from config import Config
from tally_client import client, TallyError
from parsers import parse_stock_items, parse_voucher_list, parse_voucher_detail
from xml_utils import fmt_amount

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__)

# ── Caches ────────────────────────────────────────────────────────────
stock_cache = DataCache(
    name="stock",
    fetch_fn=client.fetch_stock_items,
    parse_fn=parse_stock_items,
    ttl=Config.CACHE_TTL,
    cache_dir=Config.CACHE_DIR,
)

voucher_cache = DataCache(
    name="voucher",
    fetch_fn=client.fetch_vouchers_window,
    parse_fn=parse_voucher_list,
    ttl=Config.CACHE_TTL,
    cache_dir=Config.CACHE_DIR,
)

refresher = BackgroundRefresher(Config.BACKGROUND_REFRESH_INTERVAL)
refresher.register(stock_cache)
refresher.register(voucher_cache)
if Config.BACKGROUND_REFRESH_ENABLED:
    refresher.start()


def _cached_at(cache: DataCache) -> str:
    if not cache.timestamp:
        return "Never"
    return datetime.fromtimestamp(cache.timestamp, tz=IST).strftime("%d %b %Y, %I:%M %p IST")


# ── Stock routes ─────────────────────────────────────────────────────
@app.route("/")
def index():
    items = stock_cache.get()
    return render_template(
        "stock.html",
        data_json=items,
        total=len(items),
        cached_at=_cached_at(stock_cache),
        data_source=stock_cache.source,
    )


@app.route("/refresh")
def refresh_stock():
    stock_cache.invalidate()
    items = stock_cache.get(force=True)
    return f"\u2705 Refreshed! {len(items)} items. <a href='/'>\u2190 Back</a>"


# ── Voucher routes ───────────────────────────────────────────────────
@app.route("/vouchers")
def vouchers():
    vlist = voucher_cache.get()
    return render_template(
        "vouchers.html",
        data_json=vlist,
        total=len(vlist),
        cached_at=_cached_at(voucher_cache),
        data_source=voucher_cache.source,
        window_days=Config.VOUCHER_WINDOW_DAYS,
    )


@app.route("/vouchers/refresh")
def refresh_vouchers():
    voucher_cache.invalidate()
    vlist = voucher_cache.get(force=True)
    return f"\u2705 Refreshed! {len(vlist)} vouchers. <a href='/vouchers'>\u2190 Back</a>"


@app.route("/voucher")
def voucher_detail():
    vnum = request.args.get("vnum", "").strip()
    vtype = request.args.get("vtype", "").strip()
    if not vnum or not vtype:
        return render_template("voucher_detail.html", d={}, error="Missing voucher number or type.", amt_css="", amt_display="")
    try:
        xml_text = client.fetch_voucher_detail(vtype)
        detail = parse_voucher_detail(xml_text, vnum, vtype)
    except TallyError as e:
        log.warning("Voucher detail fetch failed: %s", e)
        return render_template(
            "voucher_detail.html",
            d={"vnum": vnum, "vtype": vtype}, error="Could not connect to Tally. Please check that Tally is running.",
            amt_css="", amt_display="",
        )
    if not detail:
        return render_template(
            "voucher_detail.html",
            d={"vnum": vnum, "vtype": vtype},
            error=f"Voucher '{vnum}' not found in the current {Config.VOUCHER_MAX_WINDOW_DAYS}-day lookback window.",
            amt_css="", amt_display="",
        )
    css, disp = fmt_amount(detail["amount"])
    return render_template("voucher_detail.html", d=detail, error=None, amt_css=css, amt_display=disp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=Config.DEBUG)
