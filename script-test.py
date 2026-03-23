import os
import json
import time
import re
import requests
import xml.etree.ElementTree as ET
from flask import Flask, render_template_string

app = Flask(__name__)
TALLY_URL  = os.environ.get("TALLY_URL", "http://localhost:9000")
CACHE_FILE = "stock_cache.json"
CACHE_TTL  = 3600

_cache = {
    "data"      : [],
    "timestamp" : None,
    "source"    : "none"
}

# ── Cache helpers ──────────────────────────────────────────
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
        print("Cache saved to disk.")
    except Exception as e:
        print(f"Could not save cache: {e}")

def is_cache_fresh():
    if not _cache["timestamp"]:
        return False
    return (time.time() - _cache["timestamp"]) < CACHE_TTL

# ── Tally fetch ────────────────────────────────────────────
def fetch_stock_data():
    xml_request = """
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>MyStockItems</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="MyStockItems" ISINTERNAL="No">
                            <TYPE>StockItem</TYPE>
                            <FETCH>Name, Parent, StandardPrice</FETCH>
                        </COLLECTION>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>
    """
    response = requests.post(
        TALLY_URL,
        data=xml_request.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        timeout=10
    )
    return response.text

def clean_xml(xml_text):
    xml_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', xml_text)
    xml_text = re.sub(r'&#x([0-9A-Fa-f]+);', lambda m: '' if int(m.group(1), 16) < 32 and int(m.group(1), 16) not in (9, 10, 13) else m.group(0), xml_text)
    xml_text = re.sub(r'&#([0-9]+);', lambda m: '' if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13) else m.group(0), xml_text)
    xml_text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', xml_text)
    xml_text = xml_text.replace('₹', 'Rs')
    return xml_text

def parse_stock_data(xml_text):
    if not xml_text:
        return []
    try:
        xml_text = clean_xml(xml_text)
        root     = ET.fromstring(xml_text)
        items    = []
        for item in root.iter("STOCKITEM"):
            name      = item.get("NAME") or item.findtext("NAME", "")
            parent    = item.findtext("PARENT", "—")
            std_price = item.findtext("STANDARDPRICE", "—")
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
        print("Serving from memory cache.")
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
        print("Serving stale disk cache.")
        _cache["source"] = "offline cache"
        return _cache["data"]

    return []

# ── HTML Template ──────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Stock Master - Kanhaiya Textile</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body  { font-family: Arial, sans-serif; background: #f5f5f5; padding: 24px; }
        h2    { color: #2c3e50; margin-bottom: 10px; }
        .status {
            font-size: 12px; margin-bottom: 14px; padding: 6px 12px;
            border-radius: 4px; display: inline-block;
        }
        .live    { background: #e8f5e9; color: #2e7d32; }
        .cached  { background: #fff8e1; color: #f57f17; }
        .offline { background: #fdecea; color: #c62828; }
        .toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
        input {
            width: 320px; padding: 8px 12px;
            border: 1px solid #ccc; border-radius: 4px; font-size: 14px;
        }
        #info { font-size: 13px; color: #666; }
        table {
            width: 100%; border-collapse: collapse; background: white;
            box-shadow: 0 1px 6px rgba(0,0,0,0.1); border-radius: 6px; overflow: hidden;
        }
        th {
            background: #2c3e50; color: white; padding: 11px 14px;
            text-align: left; font-size: 13px; cursor: pointer; user-select: none;
        }
        th:hover { background: #3d5166; }
        td    { padding: 9px 14px; border-bottom: 1px solid #eee; font-size: 13px; }
        tr:hover td { background: #f0f4ff; }
        .group {
            background: #e8f0fe; color: #1a56db;
            padding: 2px 8px; border-radius: 10px; font-size: 11px;
        }
        .empty { text-align: center; padding: 40px; color: #999; font-size: 15px; }
        #pagination { margin-top: 14px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        #pagination button {
            padding: 5px 12px; border: 1px solid #ccc; border-radius: 4px;
            background: white; cursor: pointer; font-size: 13px;
        }
        #pagination button.active { background: #2c3e50; color: white; border-color: #2c3e50; }
        #pagination button:hover:not(.active):not(:disabled) { background: #f0f4ff; }
        #pagination button:disabled { opacity: 0.4; cursor: default; }
        a.refresh-btn {
            font-size: 12px; padding: 6px 12px; background: #2c3e50; color: white;
            border-radius: 4px; text-decoration: none;
        }
        a.refresh-btn:hover { background: #3d5166; }
    </style>
</head>
<body>
    <h2>📦 Stock Master — Kanhaiya Textile</h2>

    <!-- Line 1: status div class -->
<div class="status {{ 'live' if data_source == 'live tally' else 'cached' if data_source == 'live cache' else 'offline' }}">

<!-- Line 2: condition block -->
    {% if data_source == 'live tally' %}
        ✅ Live data from Tally — Updated: {{ cached_at }}
    {% elif data_source == 'live cache' %}
        ⚡ Cached (Tally connected) — Last updated: {{ cached_at }}
    {% else %}
        ⚠️ Tally offline — Showing last known data from {{ cached_at }}
    {% endif %}
</div>

    <div class="toolbar">
        <input type="text" id="search" placeholder="🔍 Search item or group...">
        <span id="info"></span>
        <a href="/refresh" class="refresh-btn">🔄 Refresh</a>
    </div>

    <table id="stockTable">
        <thead>
            <tr>
                <th style="width:50px">#</th>
                <th onclick="sortTable(0)">Stock Item ↕</th>
                <th onclick="sortTable(1)">Stock Group ↕</th>
                <th onclick="sortTable(2)">Std. Sell Price ↕</th>
            </tr>
        </thead>
        <tbody id="tableBody"></tbody>
    </table>
    <div id="pagination"></div>

    <script>
        const ALL_DATA     = {{ data_json|safe }};
        const SEARCH_INDEX = ALL_DATA.map(r => (r[0] + " " + r[1]).toLowerCase());
        const PAGE_SIZE    = 100;

        let filtered      = [...ALL_DATA];
        let currentPage   = 1;
        let debounceTimer = null;
        let sortCol       = -1;
        let sortAsc       = true;

        function renderTable() {
            const start = (currentPage - 1) * PAGE_SIZE;
            const slice = filtered.slice(start, start + PAGE_SIZE);
            const tbody = document.getElementById("tableBody");

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="empty">⚠️ No items found.</td></tr>';
                document.getElementById("info").textContent = "";
                document.getElementById("pagination").innerHTML = "";
                return;
            }

            tbody.innerHTML = slice.map((row, i) => `
                <tr>
                    <td>${start + i + 1}</td>
                    <td><strong>${row[0]}</strong></td>
                    <td><span class="group">${row[1]}</span></td>
                    <td>${row[2]}</td>
                </tr>
            `).join("");

            document.getElementById("info").textContent =
                `Showing ${start + 1}–${Math.min(start + PAGE_SIZE, filtered.length)} of ${filtered.length} items`;

            renderPagination();
        }

        function renderPagination() {
            const total = Math.ceil(filtered.length / PAGE_SIZE);
            const el    = document.getElementById("pagination");
            if (total <= 1) { el.innerHTML = ""; return; }

            let pages = [];
            pages.push(`<button onclick="goPage(1)" ${currentPage===1?'class="active"':''}>1</button>`);
            if (currentPage > 3) pages.push(`<span>…</span>`);
            for (let p = Math.max(2, currentPage - 1); p <= Math.min(total - 1, currentPage + 1); p++) {
                pages.push(`<button onclick="goPage(${p})" ${currentPage===p?'class="active"':''}>${p}</button>`);
            }
            if (currentPage < total - 2) pages.push(`<span>…</span>`);
            if (total > 1) pages.push(`<button onclick="goPage(${total})" ${currentPage===total?'class="active"':''}>${total}</button>`);

            el.innerHTML =
                `<button onclick="goPage(${currentPage - 1})" ${currentPage===1?'disabled':''}>← Prev</button>` +
                pages.join("") +
                `<button onclick="goPage(${currentPage + 1})" ${currentPage===total?'disabled':''}>Next →</button>`;
        }

        function goPage(p) {
            const total = Math.ceil(filtered.length / PAGE_SIZE);
            currentPage = Math.max(1, Math.min(p, total));
            renderTable();
            window.scrollTo(0, 0);
        }

        function sortTable(col) {
            if (sortCol === col) sortAsc = !sortAsc;
            else { sortCol = col; sortAsc = true; }
            filtered.sort((a, b) => {
                const va = (a[col] || "").toLowerCase();
                const vb = (b[col] || "").toLowerCase();
                return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
            });
            currentPage = 1;
            renderTable();
        }

        document.getElementById("search").addEventListener("input", function () {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                const q = this.value.toLowerCase().trim();
                filtered = q === "" ? [...ALL_DATA] : ALL_DATA.filter((_, i) => SEARCH_INDEX[i].includes(q));
                currentPage = 1;
                renderTable();
            }, 250);
        });

        renderTable();
    </script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    items     = get_stock_data()
    data_json = json.dumps(items)
    cached_at = time.strftime('%d %b %Y, %I:%M %p', time.localtime(_cache["timestamp"])) if _cache["timestamp"] else "Never"
    return render_template_string(HTML_TEMPLATE,
                                  data_json=data_json,
                                  total=len(items),
                                  cached_at=cached_at,
                                  data_source=_cache["source"])


@app.route("/refresh")
def refresh():
    global _cache
    _cache["timestamp"] = None
    items = get_stock_data()
    return f"✅ Refreshed! {len(items)} items loaded from [{_cache['source']}]. <a href='/'>← Back</a>"

# ── Start ─────────────────────────────────────────────────
load_cache_from_disk()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
