"""
All direct communication with Tally lives here.

Key differences from the original script (these are what actually fix the
"Read timed out" error, not just paper over it):

1. A single `requests.Session` with a `Retry`-backed `HTTPAdapter` is reused
   across calls instead of opening a fresh connection every time, and
   transient failures (timeouts, connection resets, 5xx) are retried with
   backoff automatically.
2. `timeout` is a (connect, read) tuple — connect fails fast, read gets a
   realistic budget for a large export.
3. Voucher exports are windowed by date (`SVFROMDATE`/`SVTODATE`) instead of
   asking Tally for the entire voucher history in one shot. This is the
   actual cause of the timeout: an unbounded `<COLLECTION>` export over
   years of vouchers can take Tally well past 15s to serialize. A rolling
   window (default 90 days, extendable) keeps each request small and fast,
   and the app can page further back on demand.
4. Sale and Purchase vouchers are fetched concurrently instead of
   sequentially, roughly halving wall-clock time.
"""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config
from xml_utils import to_tally_date

log = logging.getLogger("tally_client")

VOUCHER_TYPES = ["Sale Voucher", "Purchase Voucher"]


class TallyError(Exception):
    """Raised when Tally can't be reached or returns nothing usable."""


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=Config.TALLY_MAX_RETRIES,
        backoff_factor=Config.TALLY_RETRY_BACKOFF,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class TallyClient:
    """Thread-safe, connection-pooled client for Tally's XML HTTP API."""

    def __init__(self, base_url: str = None):
        self.base_url = base_url or Config.TALLY_URL
        self._session = _build_session()
        self._lock = threading.Lock()

    @property
    def timeout(self):
        return (Config.TALLY_CONNECT_TIMEOUT, Config.TALLY_READ_TIMEOUT)

    def _post(self, xml_request: str, timeout=None) -> str:
        try:
            resp = self._session.post(
                self.base_url,
                data=xml_request.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=timeout or self.timeout,
            )
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            raise TallyError(f"Tally request failed: {e}") from e

    # ── Stock items ──────────────────────────────────────────────────
    def fetch_stock_items(self) -> str:
        xml_request = """<ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE><ID>MyStockItems</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <COLLECTION NAME="MyStockItems" ISINTERNAL="No">
                        <TYPE>StockItem</TYPE>
                        <FETCH>Name, Parent, StandardPrice</FETCH>
                    </COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""
        return self._post(xml_request)

    # ── Voucher list (date-windowed) ────────────────────────────────
    @staticmethod
    def _voucher_list_xml(vtype: str, from_date: str, to_date: str) -> str:
        safe = re.sub(r'[^A-Za-z0-9]', '', vtype)
        cname, fname = f"KTVch{safe}", f"KTFlt{safe}"
        return f"""<ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE><ID>{cname}</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVFROMDATE>{from_date}</SVFROMDATE>
                    <SVTODATE>{to_date}</SVTODATE>
                </STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <COLLECTION NAME="{cname}" ISINTERNAL="No">
                        <TYPE>Voucher</TYPE>
                        <FETCH>Date, VoucherNumber, VoucherTypeName, PartyLedgerName, Amount</FETCH>
                        <FILTER>{fname}</FILTER>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="{fname}">
                        $VoucherTypeName = "{vtype}"
                    </SYSTEM>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""

    def fetch_voucher_type(self, vtype: str, from_date: str, to_date: str) -> str:
        return self._post(self._voucher_list_xml(vtype, from_date, to_date))

    def fetch_vouchers_window(self, window_days: int = None):
        """Fetch Sale + Purchase vouchers for the last `window_days`,
        concurrently. Returns a list of (vtype, xml_text_or_None) tuples;
        a None means that one type failed but the other may have
        succeeded, so a partial Tally hiccup doesn't blank the whole page.
        """
        window_days = window_days or Config.VOUCHER_WINDOW_DAYS
        to_date = to_tally_date(datetime.now())
        from_date = to_tally_date(datetime.now() - timedelta(days=window_days))

        results = {}
        with ThreadPoolExecutor(max_workers=len(VOUCHER_TYPES)) as pool:
            futures = {
                pool.submit(self.fetch_voucher_type, vt, from_date, to_date): vt
                for vt in VOUCHER_TYPES
            }
            for fut in as_completed(futures):
                vt = futures[fut]
                try:
                    results[vt] = fut.result()
                except TallyError as e:
                    log.warning("Voucher fetch failed for %s: %s", vt, e)
                    results[vt] = None
        return [(vt, results[vt]) for vt in VOUCHER_TYPES]

    # ── Single voucher detail ───────────────────────────────────────
    def fetch_voucher_detail(self, vtype: str) -> str:
        """Full detail export (ledger + inventory sub-entries) for every
        voucher of `vtype` within the configured window; the caller
        matches the specific voucher number Python-side, same approach
        as the original script (Tally's own filter is unreliable with
        special characters in voucher numbers)."""
        safe_vtype = vtype.replace('"', "").replace("'", "")
        cname = f"KTDetCol{abs(hash(safe_vtype)) % 9999}"
        fname = f"KTDetFlt{abs(hash(safe_vtype)) % 9999}"
        to_date = to_tally_date(datetime.now())
        from_date = to_tally_date(datetime.now() - timedelta(days=Config.VOUCHER_MAX_WINDOW_DAYS))
        xml_req = f"""<ENVELOPE>
            <HEADER>
                <VERSION>1</VERSION>
                <TALLYREQUEST>Export</TALLYREQUEST>
                <TYPE>Collection</TYPE>
                <ID>{cname}</ID>
            </HEADER>
            <BODY>
                <DESC>
                    <STATICVARIABLES>
                        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                        <SVFROMDATE>{from_date}</SVFROMDATE>
                        <SVTODATE>{to_date}</SVTODATE>
                    </STATICVARIABLES>
                    <TDL>
                        <TDLMESSAGE>
                            <COLLECTION NAME="{cname}" ISINTERNAL="No">
                                <TYPE>Voucher</TYPE>
                                <FETCH>Date, VoucherNumber, VoucherTypeName, PartyLedgerName,
                                       Amount, Narration, Reference,
                                       AllLedgerEntries.LedgerName,
                                       AllLedgerEntries.Amount,
                                       AllLedgerEntries.IsDeemedPositive,
                                       AllInventoryEntries.StockItemName,
                                       AllInventoryEntries.BilledQty,
                                       AllInventoryEntries.ActualQty,
                                       AllInventoryEntries.Rate,
                                       AllInventoryEntries.Amount</FETCH>
                                <FILTER>{fname}</FILTER>
                            </COLLECTION>
                            <SYSTEM TYPE="Formulae" NAME="{fname}">
                                $VoucherTypeName = "{safe_vtype}"
                            </SYSTEM>
                        </TDLMESSAGE>
                    </TDL>
                </DESC>
            </BODY>
        </ENVELOPE>"""
        return self._post(xml_req, timeout=(Config.TALLY_CONNECT_TIMEOUT, max(Config.TALLY_READ_TIMEOUT, 60)))


# Module-level singleton — one pooled session for the whole app.
client = TallyClient()
