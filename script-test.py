import os
import json
import time
import re
import requests
import xml.etree.ElementTree as ET
from flask import Flask, render_template_string, request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote

IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__)
TALLY_URL          = os.environ.get("TALLY_URL", "http://localhost:9000")
CACHE_FILE         = "stock_cache.json"
VOUCHER_CACHE_FILE = "voucher_cache.json"
CACHE_TTL          = 3600

_cache         = {"data": [], "timestamp": None, "source": "none"}
_voucher_cache = {"data": [], "timestamp": None, "source": "none"}

# ── Disk cache helpers ─────────────────────────────────────
def load_cache_from_disk():
    global _cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                _cache["data"]      = saved.get("data", [])
                _cache["timestamp"] = saved.get("timestamp")
                _cache["source"]    = "disk"
                print(f"Loaded {len(_cache['data'])} items from disk cache.")
        except Exception as e:
            print(f"Could not load disk cache: {e}")

def save_cache_to_disk():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": _cache["data"], "timestamp": _cache["timestamp"]}, f)
    except Exception as e:
        print(f"Could not save cache: {e}")

def load_voucher_cache_from_disk():
    global _voucher_cache
    if os.path.exists(VOUCHER_CACHE_FILE):
        try:
            with open(VOUCHER_CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                _voucher_cache["data"]      = saved.get("data", [])
                _voucher_cache["timestamp"] = saved.get("timestamp")
                _voucher_cache["source"]    = "disk"
                print(f"Loaded {len(_voucher_cache['data'])} vouchers from disk cache.")
        except Exception as e:
            print(f"Could not load voucher disk cache: {e}")

def save_voucher_cache_to_disk():
    try:
        with open(VOUCHER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": _voucher_cache["data"], "timestamp": _voucher_cache["timestamp"]}, f)
    except Exception as e:
        print(f"Could not save voucher cache: {e}")

def is_cache_fresh():
    return bool(_cache["timestamp"]) and (time.time() - _cache["timestamp"]) < CACHE_TTL

def is_voucher_cache_fresh():
    return bool(_voucher_cache["timestamp"]) and (time.time() - _voucher_cache["timestamp"]) < CACHE_TTL

# ── XML cleaner ────────────────────────────────────────────
def clean_xml(xml_text):
    xml_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', xml_text)
    xml_text = re.sub(r'&#x([0-9A-Fa-f]+);',
                      lambda m: '' if int(m.group(1), 16) < 32 and int(m.group(1), 16) not in (9, 10, 13)
                      else m.group(0), xml_text)
    xml_text = re.sub(r'&#([0-9]+);',
                      lambda m: '' if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13)
                      else m.group(0), xml_text)
    xml_text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', xml_text)
    xml_text = xml_text.replace('\u20b9', 'Rs')
    xml_text = re.sub(r'\s+xmlns(?::\w+)?=\"[^\"]*\"', '', xml_text)
    xml_text = re.sub(r'<(/?)(\w+):(\w)', r'<\1\3', xml_text)
    xml_text = re.sub(r'(\s)(\w+):(\w+)=', r'\1\3=', xml_text)
    return xml_text

def _fmt_tally_date(date_str):
    if date_str and len(date_str) == 8:
        try:
            return datetime.strptime(date_str, "%Y%m%d").strftime("%d/%m/%Y")
        except Exception:
            pass
    return date_str or "\u2014"

# ── Tally fetch — Stock ────────────────────────────────────
def fetch_stock_data():
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
    r = requests.post(TALLY_URL, data=xml_request.encode("utf-8"),
                      headers={"Content-Type": "application/xml"}, timeout=10)
    return r.text

def parse_stock_data(xml_text):
    if not xml_text:
        return []
    try:
        xml_text = clean_xml(xml_text)
        root = ET.fromstring(xml_text)
        items = []
        for item in root.iter("STOCKITEM"):
            name      = item.get("NAME") or item.findtext("NAME", "")
            parent    = item.findtext("PARENT", "\u2014")
            std_price = item.findtext("STANDARDPRICE", "\u2014")
            if name:
                items.append([name, parent, std_price])
        return items
    except ET.ParseError as e:
        print(f"XML Parse Error: {e}")
        with open("tally_raw_response.xml", "w", encoding="utf-8") as f:
            f.write(xml_text)
        return []

def get_stock_data():
    global _cache
    if is_cache_fresh() and _cache["data"]:
        _cache["source"] = "live cache"
        return _cache["data"]
    try:
        xml_text = fetch_stock_data()
        items    = parse_stock_data(xml_text)
        if items:
            _cache["data"]      = items
            _cache["timestamp"] = time.time()
            _cache["source"]    = "live tally"
            save_cache_to_disk()
            print(f"Fetched {len(items)} items from Tally.")
            return items
    except Exception as e:
        print(f"Tally unreachable: {e}")
    if _cache["data"]:
        _cache["source"] = "offline cache"
        return _cache["data"]
    return []

# ── Tally fetch — Voucher list ─────────────────────────────
def _fetch_one_voucher_type(vtype):
    cname = f"KTVch{re.sub(r'[^A-Za-z0-9]', '', vtype)}"
    fname = f"KTFlt{re.sub(r'[^A-Za-z0-9]', '', vtype)}"
    xml_req = f"""<ENVELOPE>
        <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE><ID>{cname}</ID></HEADER>
        <BODY><DESC>
            <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
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
    r = requests.post(TALLY_URL, data=xml_req.encode("utf-8"),
                      headers={"Content-Type": "application/xml"}, timeout=15)
    return r.text

def fetch_voucher_data():
    results = []
    for vt in ["Sale Voucher", "Purchase Voucher"]:
        try:
            xml_text = _fetch_one_voucher_type(vt)
            print(f"Fetched XML for {vt} ({len(xml_text)} bytes)")
            results.append((vt, xml_text))
        except Exception as e:
            print(f"Tally fetch error for {vt}: {e}")
            results.append((vt, ""))
    return results

def parse_voucher_data(results):
    vouchers = []
    for (vtype_label, xml_text) in results:
        if not xml_text:
            continue
        try:
            cleaned = clean_xml(xml_text)
            root    = ET.fromstring(cleaned)
            for v in root.iter("VOUCHER"):
                date   = _fmt_tally_date(v.findtext("DATE", ""))
                vnum   = (v.findtext("VOUCHERNUMBER", "") or
                          v.get("VOUCHERNUMBER", "") or v.get("NAME", "\u2014"))
                vtype  = v.findtext("VOUCHERTYPENAME", "") or vtype_label
                party  = v.findtext("PARTYLEDGERNAME", "\u2014")
                amount = v.findtext("AMOUNT", "\u2014")
                if vnum:
                    vouchers.append([date, vnum, vtype, party, amount])
        except Exception as e:
            print(f"Voucher Parse Error ({vtype_label}): {e}")
            with open(f"tally_raw_voucher_{vtype_label}.xml", "w", encoding="utf-8") as f:
                f.write(xml_text)
    def sort_key(row):
        d = row[0]
        if d and len(d) == 10 and d[2] == '/':
            return d[6:] + d[3:5] + d[:2]
        return d
    vouchers.sort(key=sort_key, reverse=True)
    return vouchers

def get_voucher_data():
    global _voucher_cache
    if is_voucher_cache_fresh() and _voucher_cache["data"]:
        _voucher_cache["source"] = "live cache"
        return _voucher_cache["data"]
    try:
        results  = fetch_voucher_data()
        vouchers = parse_voucher_data(results)
        _voucher_cache["timestamp"] = time.time()
        if vouchers:
            _voucher_cache["data"]   = vouchers
            _voucher_cache["source"] = "live tally"
            save_voucher_cache_to_disk()
            print(f"Fetched {len(vouchers)} vouchers from Tally.")
            return vouchers
        else:
            _voucher_cache["source"] = "live tally"
            print("Tally returned 0 vouchers.")
    except Exception as e:
        print(f"Tally unreachable for vouchers: {e}")
    if _voucher_cache["data"]:
        _voucher_cache["source"] = "offline cache"
        return _voucher_cache["data"]
    return []

# ── Tally fetch — Single Voucher Detail ───────────────────
def fetch_voucher_detail(vnum, vtype):
    """
    Fetch full voucher detail by exporting ALL vouchers of that type,
    then Python-side matching by voucher number.
    This avoids Tally filter bugs with special chars in voucher numbers.
    Uses EXPLODE to get sub-entries (ledger + inventory lines).
    """
    safe_vtype = vtype.replace('"', "").replace("'", "")
    cname = f"KTDetCol{abs(hash(safe_vtype)) % 9999}"
    fname = f"KTDetFlt{abs(hash(safe_vtype)) % 9999}"
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
    r = requests.post(TALLY_URL, data=xml_req.encode("utf-8"),
                      headers={"Content-Type": "application/xml"}, timeout=30)
    return r.text

def parse_voucher_detail(xml_text, vnum, vtype):
    if not xml_text:
        return None
    try:
        with open("tally_raw_detail.xml", "w", encoding="utf-8") as f:
            f.write(xml_text)
    except Exception:
        pass

    try:
        cleaned = clean_xml(xml_text)
        root    = ET.fromstring(cleaned)
    except Exception as e:
        print(f"XML parse failed: {e}")
        return None

    # ── Find the matching VOUCHER by number (Python-side match) ──
    target = vnum.strip().lower()
    matched = None
    all_vouchers = list(root.iter("VOUCHER"))
    print(f"Total <VOUCHER> elements in response: {len(all_vouchers)}")
    for v in all_vouchers:
        n = (v.findtext("VOUCHERNUMBER", "") or v.get("VOUCHERNUMBER", "") or v.get("NAME", "")).strip()
        if n.lower() == target:
            matched = v
            break
    # fallback: pick first voucher if only one returned
    if matched is None and len(all_vouchers) == 1:
        matched = all_vouchers[0]
        print("No exact match — using single voucher in response as fallback")
    if matched is None:
        print(f"No match for vnum='{vnum}' among {len(all_vouchers)} vouchers")
        nums = [(v.findtext("VOUCHERNUMBER","") or v.get("NAME","")) for v in all_vouchers[:10]]
        print(f"Sample voucher numbers in response: {nums}")
        return None

    v = matched
    date      = _fmt_tally_date(v.findtext("DATE", ""))
    found_num = (v.findtext("VOUCHERNUMBER", "") or v.get("VOUCHERNUMBER", "") or v.get("NAME", ""))
    vtype_out = v.findtext("VOUCHERTYPENAME", "") or vtype
    party     = v.findtext("PARTYLEDGERNAME", "\u2014")
    amount    = v.findtext("AMOUNT", "\u2014")
    narration = v.findtext("NARRATION", "").strip()
    reference = v.findtext("REFERENCE", "").strip()
    print(f"Matched: vnum={found_num} date={date} party={party} amount={amount}")

    # ── Log ALL tags inside this voucher to help debug sub-entry names ──
    all_tags = sorted(set(el.tag for el in v.iter()))
    print(f"All tags inside matched VOUCHER: {all_tags}")

    # ── Ledger entries — try every known Tally tag variant ──
    ledgers = []
    for tag in ["ALLLEDGERENTRIES", "LEDGERENTRIES",
                "ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"]:
        entries = list(v.iter(tag))
        if entries:
            print(f"Ledgers: {len(entries)} entries under <{tag}>")
            for le in entries:
                lname  = le.findtext("LEDGERNAME", "") or le.get("NAME", "")
                lamt   = le.findtext("AMOUNT", "\u2014")
                is_pos = (le.findtext("ISDEEMEDPOSITIVE", "") or
                          le.findtext("ISDEEMEDNPOSITIVE", "")).strip().lower()
                ledgers.append({"name": lname or "\u2014", "amount": lamt,
                                "dr_cr": "Dr" if is_pos == "yes" else "Cr"})
            break

    # ── Inventory entries — try every known Tally tag variant ──
    inventory = []
    for tag in ["ALLINVENTORYENTRIES", "INVENTORYENTRIES",
                "ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"]:
        entries = list(v.iter(tag))
        if entries:
            print(f"Inventory: {len(entries)} entries under <{tag}>")
            for ie in entries:
                inventory.append({
                    "name":       ie.findtext("STOCKITEMNAME", "") or ie.get("NAME", "\u2014"),
                    "billed_qty": ie.findtext("BILLEDQTY", "\u2014"),
                    "actual_qty": ie.findtext("ACTUALQTY", "\u2014"),
                    "rate":       ie.findtext("RATE", "\u2014"),
                    "amount":     ie.findtext("AMOUNT", "\u2014"),
                })
            break

    if not ledgers and not inventory:
        print(f"WARNING: no sub-entries found. Tags were: {all_tags}")

    return {"date": date, "vnum": found_num or vnum, "vtype": vtype_out,
            "party": party, "amount": amount, "narration": narration,
            "reference": reference, "ledgers": ledgers, "inventory": inventory}

# ── Helpers ────────────────────────────────────────────────
def fmt_amount(raw):
    """Return (css_class, display_string) for an amount string."""
    if not raw or raw in ("\u2014", "—"):
        return ("", "\u2014")
    try:
        n   = float(str(raw).replace(",", "").strip())
        abs_str = f"{abs(n):,.2f}"
        # Indian number formatting
        parts = abs_str.split(".")
        dec   = parts[1]
        intg  = parts[0].replace(",", "")
        if len(intg) > 3:
            intg = intg[:-3] + "," + intg[-3:]
            i = len(intg) - 7
            while i > 0:
                intg = intg[:i] + "," + intg[i:]
                i -= 2
        sign = "+" if n >= 0 else "\u2212"
        css  = "amount-credit" if n >= 0 else "amount-debit"
        return (css, f"Rs {sign}{intg}.{dec}")
    except Exception:
        return ("", str(raw))

SHARED_STYLE = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f5f5f5; padding: 24px; }
    .nav { display: flex; align-items: center; background: #2c3e50; border-radius: 6px; overflow: hidden; margin-bottom: 20px; width: fit-content; box-shadow: 0 1px 6px rgba(0,0,0,0.15); }
    .nav a { color: #b0bec5; text-decoration: none; padding: 10px 20px; font-size: 13px; font-weight: 600; letter-spacing: 0.3px; transition: background 0.15s, color 0.15s; display: flex; align-items: center; gap: 6px; }
    .nav a:hover  { background: #3d5166; color: #ecf0f1; }
    .nav a.active { background: #1a252f; color: #ffffff; }
    .nav-divider  { width: 1px; background: #3d5166; height: 38px; }
    h2 { color: #2c3e50; margin-bottom: 10px; }
    .status { font-size: 12px; margin-bottom: 14px; padding: 6px 12px; border-radius: 4px; display: inline-block; }
    .live    { background: #e8f5e9; color: #2e7d32; }
    .cached  { background: #fff8e1; color: #f57f17; }
    .offline { background: #fdecea; color: #c62828; }
    .toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
    input[type=text] { width: 320px; padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
    select { padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
    #info, #vinfo { font-size: 13px; color: #666; }
    table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 6px rgba(0,0,0,0.1); border-radius: 6px; overflow: hidden; }
    th { background: #2c3e50; color: white; padding: 11px 14px; text-align: left; font-size: 13px; cursor: pointer; user-select: none; }
    th:hover { background: #3d5166; }
    td { padding: 9px 14px; border-bottom: 1px solid #eee; font-size: 13px; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover td { background: #e8f0fe; }
    .group          { background: #e8f0fe; color: #1a56db; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
    .badge-sale     { background: #e8f5e9; color: #2e7d32; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-purchase { background: #fff3e0; color: #e65100; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge-other    { background: #f3e5f5; color: #6a1b9a; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .amount-credit  { color: #2e7d32; font-weight: 600; }
    .amount-debit   { color: #c62828; font-weight: 600; }
    .empty { text-align: center; padding: 40px; color: #999; font-size: 15px; }
    #pagination, #vpagination { margin-top: 14px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
    #pagination button, #vpagination button { padding: 5px 12px; border: 1px solid #ccc; border-radius: 4px; background: white; cursor: pointer; font-size: 13px; }
    #pagination button.active, #vpagination button.active { background: #2c3e50; color: white; border-color: #2c3e50; }
    #pagination button:hover:not(.active):not(:disabled),
    #vpagination button:hover:not(.active):not(:disabled) { background: #f0f4ff; }
    #pagination button:disabled, #vpagination button:disabled { opacity: 0.4; cursor: default; }
    a.refresh-btn { font-size: 12px; padding: 6px 12px; background: #2c3e50; color: white; border-radius: 4px; text-decoration: none; }
    a.refresh-btn:hover { background: #3d5166; }
    .summary-bar { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
    .summary-pill { padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; display: flex; flex-direction: column; gap: 2px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
    .summary-pill span.label { font-size: 11px; font-weight: 400; opacity: 0.8; }
    .pill-total    { background: #e8f0fe; color: #1a56db; }
    .pill-sale     { background: #e8f5e9; color: #2e7d32; }
    .pill-purchase { background: #fff3e0; color: #e65100; }
"""

# ── Stock Page ─────────────────────────────────────────────
STOCK_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>Stock Master - Kanhaiya Textile</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>""" + SHARED_STYLE + """</style>
</head>
<body>
    <nav class="nav">
        <a href="/" class="active">&#128230; Stock Master</a>
        <div class="nav-divider"></div>
        <a href="/vouchers">&#129534; Sale &amp; Purchase</a>
    </nav>
    <h2>&#128230; Stock Master &mdash; Kanhaiya Textile</h2>
    <div class="status {{ 'live' if data_source == 'live tally' else 'cached' if data_source == 'live cache' else 'offline' }}">
        {% if data_source == 'live tally' %}&#9989; Live data from Tally &mdash; Updated: {{ cached_at }}
        {% elif data_source == 'live cache' %}&#9889; Cached (Tally connected) &mdash; Last updated: {{ cached_at }}
        {% else %}&#9888; Tally offline &mdash; Showing last known data from {{ cached_at }}{% endif %}
    </div>
    <div class="toolbar">
        <input type="text" id="search" placeholder="Search item or group...">
        <span id="info"></span>
        <a href="/refresh" class="refresh-btn">&#128260; Refresh</a>
    </div>
    <table>
        <thead><tr>
            <th style="width:50px">#</th>
            <th onclick="sortTable(0)">Stock Item &#8597;</th>
            <th onclick="sortTable(1)">Stock Group &#8597;</th>
            <th onclick="sortTable(2)">Std. Sell Price &#8597;</th>
        </tr></thead>
        <tbody id="tableBody"></tbody>
    </table>
    <div id="pagination"></div>
    <script>
        const ALL_DATA = {{ data_json|safe }};
        const SEARCH_INDEX = ALL_DATA.map(r => (r[0]+" "+r[1]).toLowerCase());
        const PAGE_SIZE = 100;
        let filtered=[...ALL_DATA], currentPage=1, timer=null, sortCol=-1, sortAsc=true;
        function render() {
            const start=(currentPage-1)*PAGE_SIZE, slice=filtered.slice(start,start+PAGE_SIZE);
            const tbody=document.getElementById("tableBody");
            if (!filtered.length) { tbody.innerHTML='<tr><td colspan="4" class="empty">No items found.</td></tr>'; document.getElementById("info").textContent=""; document.getElementById("pagination").innerHTML=""; return; }
            tbody.innerHTML=slice.map((r,i)=>`<tr><td>${start+i+1}</td><td><strong>${r[0]}</strong></td><td><span class="group">${r[1]}</span></td><td>${r[2]}</td></tr>`).join("");
            document.getElementById("info").textContent=`Showing ${start+1}–${Math.min(start+PAGE_SIZE,filtered.length)} of ${filtered.length} items`;
            renderPages("pagination",currentPage,Math.ceil(filtered.length/PAGE_SIZE),goPage);
        }
        function renderPages(id,cur,total,cb) {
            const el=document.getElementById(id);
            if(total<=1){el.innerHTML="";return;}
            let h=`<button onclick="${cb.name}(${cur-1})" ${cur===1?"disabled":""}>&#8592; Prev</button>`;
            h+=`<button onclick="${cb.name}(1)" ${cur===1?'class="active"':""}>1</button>`;
            if(cur>3)h+=`<span>&hellip;</span>`;
            for(let p=Math.max(2,cur-1);p<=Math.min(total-1,cur+1);p++) h+=`<button onclick="${cb.name}(${p})" ${cur===p?'class="active"':""}>${p}</button>`;
            if(cur<total-2)h+=`<span>&hellip;</span>`;
            if(total>1)h+=`<button onclick="${cb.name}(${total})" ${cur===total?'class="active"':""}>${total}</button>`;
            h+=`<button onclick="${cb.name}(${cur+1})" ${cur===total?"disabled":""}>Next &#8594;</button>`;
            el.innerHTML=h;
        }
        function goPage(p){currentPage=Math.max(1,Math.min(p,Math.ceil(filtered.length/PAGE_SIZE)));render();window.scrollTo(0,0);}
        function sortTable(col){
            if(sortCol===col)sortAsc=!sortAsc;else{sortCol=col;sortAsc=true;}
            filtered.sort((a,b)=>sortAsc?(a[col]||"").toLowerCase().localeCompare((b[col]||"").toLowerCase()):(b[col]||"").toLowerCase().localeCompare((a[col]||"").toLowerCase()));
            currentPage=1;render();
        }
        document.getElementById("search").addEventListener("input",function(){
            clearTimeout(timer);timer=setTimeout(()=>{const q=this.value.toLowerCase().trim();filtered=q?ALL_DATA.filter((_,i)=>SEARCH_INDEX[i].includes(q)):[...ALL_DATA];currentPage=1;render();},250);
        });
        render();
    </script>
</body>
</html>"""

# ── Voucher List Page ──────────────────────────────────────
VOUCHER_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>Sale &amp; Purchase Vouchers - Kanhaiya Textile</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>""" + SHARED_STYLE + """</style>
</head>
<body>
    <nav class="nav">
        <a href="/">&#128230; Stock Master</a>
        <div class="nav-divider"></div>
        <a href="/vouchers" class="active">&#129534; Sale &amp; Purchase</a>
    </nav>
    <h2>&#129534; Sale &amp; Purchase Vouchers &mdash; Kanhaiya Textile</h2>
    <div class="status {{ 'live' if data_source == 'live tally' else 'cached' if data_source == 'live cache' else 'offline' }}">
        {% if data_source == 'live tally' %}&#9989; Live data from Tally &mdash; Updated: {{ cached_at }}
        {% elif data_source == 'live cache' %}&#9889; Cached (Tally connected) &mdash; Last updated: {{ cached_at }}
        {% else %}&#9888; Tally offline &mdash; Showing last known data from {{ cached_at }}{% endif %}
    </div>
    <div class="summary-bar" id="summaryBar"></div>
    <div class="toolbar">
        <input type="text" id="vsearch" placeholder="Search party, voucher no...">
        <select id="vtypeFilter">
            <option value="">All Types</option>
            <option value="sale">Sale Voucher</option>
            <option value="purchase">Purchase Voucher</option>
        </select>
        <span id="vinfo"></span>
        <a href="/vouchers/refresh" class="refresh-btn">&#128260; Refresh</a>
    </div>
    <table>
        <thead><tr>
            <th style="width:44px">#</th>
            <th onclick="sortV(0)">Date &#8597;</th>
            <th onclick="sortV(1)">Voucher No. &#8597;</th>
            <th onclick="sortV(2)">Type &#8597;</th>
            <th onclick="sortV(3)">Party &#8597;</th>
            <th onclick="sortV(4)">Amount &#8597;</th>
        </tr></thead>
        <tbody id="vBody"></tbody>
    </table>
    <div id="vpagination"></div>
    <script>
        const V_ALL  = {{ data_json|safe }};
        const V_IDX  = V_ALL.map(r => (r[1]+" "+r[3]).toLowerCase());
        const V_PAGE = 100;
        let vFiltered=[...V_ALL], vPage=1, vTimer=null, vCol=0, vAsc=false;

        function badge(t) {
            const l=(t||"").toLowerCase();
            if(l.includes("sale"))     return `<span class="badge-sale">${t}</span>`;
            if(l.includes("purchase")) return `<span class="badge-purchase">${t}</span>`;
            return `<span class="badge-other">${t||"&mdash;"}</span>`;
        }
        function fmtAmt(raw) {
            if(!raw||raw==="\u2014"||raw==="—") return "&mdash;";
            const n=parseFloat(String(raw).replace(/[^0-9.-]/g,""));
            if(isNaN(n)) return raw;
            const abs=Math.abs(n).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2});
            return `<span class="${n>=0?'amount-credit':'amount-debit'}">Rs ${n>=0?'+':'&minus;'}${abs}</span>`;
        }
        function buildSummary() {
            const s=V_ALL.filter(r=>(r[2]||"").toLowerCase().includes("sale")).length;
            const p=V_ALL.filter(r=>(r[2]||"").toLowerCase().includes("purchase")).length;
            document.getElementById("summaryBar").innerHTML=
                `<div class="summary-pill pill-total"><span class="label">Total Vouchers</span>${V_ALL.length}</div>
                 <div class="summary-pill pill-sale"><span class="label">Sales</span>${s}</div>
                 <div class="summary-pill pill-purchase"><span class="label">Purchases</span>${p}</div>`;
        }
        function renderV() {
            const start=(vPage-1)*V_PAGE, slice=vFiltered.slice(start,start+V_PAGE);
            const tbody=document.getElementById("vBody");
            if(!vFiltered.length){tbody.innerHTML='<tr><td colspan="6" class="empty">No vouchers found.</td></tr>';document.getElementById("vinfo").textContent="";document.getElementById("vpagination").innerHTML="";return;}
            tbody.innerHTML=slice.map((r,i)=>{
                const idx=start+i;
                return `<tr class="clickable" data-idx="${idx}">
                 <td>${start+i+1}</td>
                 <td>${r[0]||"&mdash;"}</td>
                 <td><strong>${r[1]||"&mdash;"}</strong></td>
                 <td>${badge(r[2])}</td>
                 <td>${r[3]||"&mdash;"}</td>
                 <td>${fmtAmt(r[4])}</td></tr>`;
            }).join("");
            tbody.querySelectorAll("tr.clickable").forEach(tr=>{
                tr.addEventListener("click", function(){
                    const r=vFiltered[parseInt(this.dataset.idx)];
                    if(!r) return;
                    window.location.href="/voucher?vnum="+encodeURIComponent(r[1])+"&vtype="+encodeURIComponent(r[2]);
                });
            });
            document.getElementById("vinfo").textContent=`Showing ${start+1}–${Math.min(start+V_PAGE,vFiltered.length)} of ${vFiltered.length} vouchers`;
            renderVPages();
        }
        function renderVPages() {
            const total=Math.ceil(vFiltered.length/V_PAGE),el=document.getElementById("vpagination");
            if(total<=1){el.innerHTML="";return;}
            let h=`<button onclick="goV(${vPage-1})" ${vPage===1?"disabled":""}>&#8592; Prev</button>`;
            h+=`<button onclick="goV(1)" ${vPage===1?'class="active"':""}>1</button>`;
            if(vPage>3)h+=`<span>&hellip;</span>`;
            for(let p=Math.max(2,vPage-1);p<=Math.min(total-1,vPage+1);p++) h+=`<button onclick="goV(${p})" ${vPage===p?'class="active"':""}>${p}</button>`;
            if(vPage<total-2)h+=`<span>&hellip;</span>`;
            if(total>1)h+=`<button onclick="goV(${total})" ${vPage===total?'class="active"':""}>${total}</button>`;
            h+=`<button onclick="goV(${vPage+1})" ${vPage===total?"disabled":""}>Next &#8594;</button>`;
            el.innerHTML=h;
        }
        function goV(p){vPage=Math.max(1,Math.min(p,Math.ceil(vFiltered.length/V_PAGE)));renderV();window.scrollTo(0,0);}
        function applyFilters(){
            const q=document.getElementById("vsearch").value.toLowerCase().trim();
            const type=document.getElementById("vtypeFilter").value.toLowerCase();
            vFiltered=V_ALL.filter((r,i)=>(q===""||V_IDX[i].includes(q))&&(type===""||( (r[2]||"").toLowerCase().includes(type))));
            vFiltered.sort((a,b)=>{const va=(a[vCol]||"").toLowerCase(),vb=(b[vCol]||"").toLowerCase();return vAsc?va.localeCompare(vb):vb.localeCompare(va);});
            vPage=1;renderV();
        }
        function sortV(col){if(vCol===col)vAsc=!vAsc;else{vCol=col;vAsc=true;}applyFilters();}
        document.getElementById("vsearch").addEventListener("input",()=>{clearTimeout(vTimer);vTimer=setTimeout(applyFilters,250);});
        document.getElementById("vtypeFilter").addEventListener("change",applyFilters);
        buildSummary();
        renderV();
    </script>
</body>
</html>"""

# ── Voucher Detail Page ────────────────────────────────────
DETAIL_STYLE = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f5f5f5; padding: 24px; }
    .nav { display: flex; align-items: center; background: #2c3e50; border-radius: 6px; overflow: hidden; margin-bottom: 20px; width: fit-content; box-shadow: 0 1px 6px rgba(0,0,0,0.15); }
    .nav a { color: #b0bec5; text-decoration: none; padding: 10px 20px; font-size: 13px; font-weight: 600; letter-spacing: 0.3px; transition: background 0.15s, color 0.15s; display: flex; align-items: center; gap: 6px; }
    .nav a:hover { background: #3d5166; color: #ecf0f1; }
    .nav a.active { background: #1a252f; color: #ffffff; }
    .nav-divider { width: 1px; background: #3d5166; height: 38px; }

    .back-btn { display: inline-flex; align-items: center; gap: 6px; margin-bottom: 18px; padding: 7px 14px; background: white; border: 1px solid #d0d0d0; border-radius: 5px; text-decoration: none; color: #2c3e50; font-size: 13px; font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.07); transition: background 0.15s; }
    .back-btn:hover { background: #f0f4ff; }

    .voucher-card { background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; max-width: 900px; }

    .voucher-header { background: #2c3e50; color: white; padding: 20px 24px; display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
    .voucher-header .vnum { font-size: 20px; font-weight: 700; }
    .voucher-header .vtype { font-size: 12px; opacity: 0.7; margin-top: 4px; }
    .badge-sale-lg     { background: #27ae60; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700; }
    .badge-purchase-lg { background: #e67e22; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700; }
    .badge-other-lg    { background: #8e44ad; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700; }

    .info-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0; border-bottom: 1px solid #eee; }
    .info-cell { padding: 14px 20px; border-right: 1px solid #f0f0f0; }
    .info-cell:last-child { border-right: none; }
    .info-cell .lbl { font-size: 11px; color: #999; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
    .info-cell .val { font-size: 14px; color: #2c3e50; font-weight: 600; }

    .section { padding: 20px 24px; border-bottom: 1px solid #f0f0f0; }
    .section:last-child { border-bottom: none; }
    .section-title { font-size: 12px; font-weight: 700; color: #2c3e50; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 2px solid #e8f0fe; }

    .detail-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .detail-table th { background: #f5f7fa; color: #555; padding: 9px 12px; text-align: left; font-weight: 600; border-bottom: 1px solid #e0e0e0; font-size: 12px; }
    .detail-table td { padding: 9px 12px; border-bottom: 1px solid #f5f5f5; color: #333; }
    .detail-table tr:last-child td { border-bottom: none; }
    .detail-table tr:hover td { background: #fafbff; }
    .detail-table .total-row td { font-weight: 700; background: #f5f7fa; border-top: 2px solid #ddd; }
    .dr  { color: #c62828; font-weight: 700; }
    .cr  { color: #2e7d32; font-weight: 700; }
    .amount-credit { color: #2e7d32; font-weight: 600; }
    .amount-debit  { color: #c62828; font-weight: 600; }
    .narration-box { background: #f9f9f9; border: 1px solid #e8e8e8; border-radius: 5px; padding: 12px 14px; font-size: 13px; color: #444; line-height: 1.6; }
    .no-data { color: #bbb; font-size: 13px; font-style: italic; }
    .error-box { background: #fdecea; border: 1px solid #f5c6c6; border-radius: 6px; padding: 20px 24px; color: #c62828; font-size: 14px; max-width: 600px; }
"""

DETAIL_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>{{ d.vnum }} &mdash; Kanhaiya Textile</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>""" + DETAIL_STYLE + """</style>
</head>
<body>
    <nav class="nav">
        <a href="/">&#128230; Stock Master</a>
        <div class="nav-divider"></div>
        <a href="/vouchers" class="active">&#129534; Sale &amp; Purchase</a>
    </nav>

    <a href="/vouchers" class="back-btn">&#8592; Back to Vouchers</a>

    {% if error %}
    <div class="error-box">&#9888; {{ error }}</div>
    {% else %}
    <div class="voucher-card">

        <!-- Header -->
        <div class="voucher-header">
            <div>
                <div class="vnum">{{ d.vnum }}</div>
                <div class="vtype">{{ d.vtype }}</div>
            </div>
            <div>
                {% set lt = d.vtype|lower %}
                {% if 'sale' in lt %}<span class="badge-sale-lg">&#128200; Sale Voucher</span>
                {% elif 'purchase' in lt %}<span class="badge-purchase-lg">&#128201; Purchase Voucher</span>
                {% else %}<span class="badge-other-lg">{{ d.vtype }}</span>{% endif %}
            </div>
        </div>

        <!-- Info grid -->
        <div class="info-grid">
            <div class="info-cell"><div class="lbl">Date</div><div class="val">{{ d.date or '&mdash;' }}</div></div>
            <div class="info-cell"><div class="lbl">Party</div><div class="val">{{ d.party or '&mdash;' }}</div></div>
            <div class="info-cell"><div class="lbl">Total Amount</div>
                <div class="val {{ amt_css }}">{{ amt_display }}</div></div>
            {% if d.reference %}<div class="info-cell"><div class="lbl">Reference</div><div class="val">{{ d.reference }}</div></div>{% endif %}
        </div>

        <!-- Narration -->
        {% if d.narration %}
        <div class="section">
            <div class="section-title">&#128221; Narration</div>
            <div class="narration-box">{{ d.narration }}</div>
        </div>
        {% endif %}

        <!-- Ledger Entries -->
        <div class="section">
            <div class="section-title">&#128196; Ledger Entries</div>
            {% if d.ledgers %}
            <table class="detail-table">
                <thead><tr>
                    <th style="width:50%">Ledger Account</th>
                    <th style="text-align:right">Amount</th>
                    <th style="width:80px;text-align:center">Dr / Cr</th>
                </tr></thead>
                <tbody>
                {% for le in d.ledgers %}
                    {% set n = le.amount|string|replace(',','')|float(default=0) %}
                    <tr>
                        <td>{{ le.name }}</td>
                        <td style="text-align:right">
                            {% if le.amount and le.amount != '—' %}
                                Rs {{ '{:,.2f}'.format(n|abs) }}
                            {% else %}&mdash;{% endif %}
                        </td>
                        <td style="text-align:center">
                            <span class="{{ 'dr' if le.dr_cr == 'Dr' else 'cr' }}">{{ le.dr_cr }}</span>
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="no-data">No ledger entries found.</div>
            {% endif %}
        </div>

        <!-- Inventory / Stock Items -->
        <div class="section">
            <div class="section-title">&#128230; Stock / Inventory Items</div>
            {% if d.inventory %}
            <table class="detail-table">
                <thead><tr>
                    <th>Item Name</th>
                    <th style="text-align:right">Billed Qty</th>
                    <th style="text-align:right">Actual Qty</th>
                    <th style="text-align:right">Rate</th>
                    <th style="text-align:right">Amount</th>
                </tr></thead>
                <tbody>
                {% set ns = namespace(total=0) %}
                {% for it in d.inventory %}
                    {% set n = it.amount|string|replace(',','')|float(default=0) %}
                    {% set ns.total = ns.total + n|abs %}
                    <tr>
                        <td><strong>{{ it.name }}</strong></td>
                        <td style="text-align:right">{{ it.billed_qty }}</td>
                        <td style="text-align:right">{{ it.actual_qty }}</td>
                        <td style="text-align:right">{{ it.rate }}</td>
                        <td style="text-align:right">
                            {% if it.amount and it.amount != '—' %}Rs {{ '{:,.2f}'.format(n|abs) }}
                            {% else %}&mdash;{% endif %}
                        </td>
                    </tr>
                {% endfor %}
                {% if d.inventory|length > 1 %}
                <tr class="total-row">
                    <td colspan="4" style="text-align:right">Total</td>
                    <td style="text-align:right">Rs {{ '{:,.2f}'.format(ns.total) }}</td>
                </tr>
                {% endif %}
                </tbody>
            </table>
            {% else %}
            <div class="no-data">No inventory entries found.</div>
            {% endif %}
        </div>

    </div>
    {% endif %}
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────
@app.route("/")
def index():
    items     = get_stock_data()
    cached_at = datetime.fromtimestamp(_cache["timestamp"], tz=IST).strftime('%d %b %Y, %I:%M %p IST') \
                if _cache["timestamp"] else "Never"
    return render_template_string(STOCK_TEMPLATE, data_json=json.dumps(items),
                                  total=len(items), cached_at=cached_at, data_source=_cache["source"])

@app.route("/refresh")
def refresh():
    global _cache
    _cache["timestamp"] = None
    items = get_stock_data()
    return f"&#9989; Refreshed! {len(items)} items. <a href='/'>&#8592; Back</a>"

@app.route("/vouchers")
def vouchers():
    vlist     = get_voucher_data()
    cached_at = datetime.fromtimestamp(_voucher_cache["timestamp"], tz=IST).strftime('%d %b %Y, %I:%M %p IST') \
                if _voucher_cache["timestamp"] else "Never"
    return render_template_string(VOUCHER_TEMPLATE, data_json=json.dumps(vlist),
                                  total=len(vlist), cached_at=cached_at, data_source=_voucher_cache["source"])

@app.route("/vouchers/refresh")
def refresh_vouchers():
    global _voucher_cache
    _voucher_cache["timestamp"] = None
    vlist = get_voucher_data()
    return f"&#9989; Refreshed! {len(vlist)} vouchers. <a href='/vouchers'>&#8592; Back</a>"

@app.route("/voucher")
def voucher_detail():
    vnum  = request.args.get("vnum", "").strip()
    vtype = request.args.get("vtype", "").strip()
    if not vnum or not vtype:
        return render_template_string(DETAIL_TEMPLATE,
                                      d={}, error="Missing voucher number or type.", amt_css="", amt_display="")
    try:
        xml_text = fetch_voucher_detail(vnum, vtype)
        detail   = parse_voucher_detail(xml_text, vnum, vtype)
        if not detail:
            return render_template_string(DETAIL_TEMPLATE, d={"vnum": vnum, "vtype": vtype},
                                          error=f"Voucher '{vnum}' not found in Tally. Tally may be offline or the voucher no longer exists.",
                                          amt_css="", amt_display="")
        css, disp = fmt_amount(detail["amount"])
        return render_template_string(DETAIL_TEMPLATE, d=detail, error=None,
                                      amt_css=css, amt_display=disp)
    except Exception as e:
        print(f"Voucher detail route error: {e}")
        return render_template_string(DETAIL_TEMPLATE, d={"vnum": vnum, "vtype": vtype},
                                      error="Could not connect to Tally. Please check that Tally is running.",
                                      amt_css="", amt_display="")


@app.route("/voucher/debug")
def voucher_debug():
    """Shows raw XML + all tag names from Tally for a voucher."""
    vnum  = request.args.get("vnum", "").strip()
    vtype = request.args.get("vtype", "").strip()
    if not vnum or not vtype:
        return "Usage: /voucher/debug?vnum=KT/1059/26-27&vtype=Sale+Voucher", 400
    try:
        xml_text = fetch_voucher_detail(vnum, vtype)
        tag_info = ""
        try:
            import xml.etree.ElementTree as ET2
            cleaned = clean_xml(xml_text)
            root2 = ET2.fromstring(cleaned)
            all_tags = sorted(set(el.tag for el in root2.iter()))
            voucher_tags = []
            for v in root2.iter("VOUCHER"):
                voucher_tags = sorted(set(el.tag for el in v.iter()))
                break
            tag_info = (f"<b>All tags in response:</b> {all_tags}<br><br>"
                        f"<b>Tags inside &lt;VOUCHER&gt;:</b> {voucher_tags}<br><br>")
        except Exception as te:
            tag_info = f"<b>Tag parse error:</b> {te}<br><br>"
        import html as html_mod
        raw_escaped = html_mod.escape(xml_text[:80000])
        return (f"<html><body style='font-family:monospace;padding:20px'>"
                f"{tag_info}"
                f"<b>Raw XML:</b><br><pre style='font-size:11px;white-space:pre-wrap;word-break:break-all'>{raw_escaped}</pre>"
                f"</body></html>")
    except Exception as e:
        return f"Error: {e}", 500

# ── Start ───────────────────────────────────────────────────
load_cache_from_disk()
load_voucher_cache_from_disk()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
