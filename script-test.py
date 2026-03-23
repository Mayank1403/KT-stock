import requests
import xml.etree.ElementTree as ET
from flask import Flask, render_template_string
import re
import json
import os
TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")

app = Flask(__name__)

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
    try:
        response = requests.post(
            TALLY_URL,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=10
        )
        return response.text
    except Exception as e:
        print(f"Error connecting to Tally: {e}")
        return None

def clean_xml(xml_text):
    # Remove raw invalid control characters (except tab \x09, newline \x0a, carriage return \x0d)
    xml_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', xml_text)
    # Remove XML character references to invalid chars e.g. &#x4; &#x1F; &#x0;
    xml_text = re.sub(r'&#x([0-9A-Fa-f]+);', lambda m: '' if int(m.group(1), 16) < 32 and int(m.group(1), 16) not in (9, 10, 13) else m.group(0), xml_text)
    xml_text = re.sub(r'&#([0-9]+);', lambda m: '' if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13) else m.group(0), xml_text)
    # Fix unescaped & not part of valid XML entity
    xml_text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', xml_text)
    # Replace rupee symbol
    xml_text = xml_text.replace('₹', 'Rs')
    return xml_text


def parse_stock_data(xml_text):
    if not xml_text:
        return []
    try:
        xml_text = clean_xml(xml_text)
        root = ET.fromstring(xml_text)
        items = []
        for item in root.iter("STOCKITEM"):
            name      = item.get("NAME") or item.findtext("NAME", "")
            parent    = item.findtext("PARENT", "—")
            std_price = item.findtext("STANDARDPRICE", "—")
            if name:
                items.append({
                    "name"      : name,
                    "parent"    : parent,
                    "std_price" : std_price,
                })
        return items
    except ET.ParseError as e:
        print(f"XML Parse Error after cleaning: {e}")
        with open("tally_raw_response.xml", "w", encoding="utf-8") as f:
            f.write(xml_text)
        print("Raw response saved to tally_raw_response.xml")
        return []

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Stock Master - Tally</title>
    <style>
        body  { font-family: Arial, sans-serif; margin: 30px; background: #f5f5f5; }
        h2    { color: #2c3e50; }
        .toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
        input { width: 320px; padding: 8px 12px;
                border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
        #info { font-size: 13px; color: #666; }
        table { width: 100%; border-collapse: collapse; background: white;
                box-shadow: 0 1px 6px rgba(0,0,0,0.1); border-radius: 6px; overflow: hidden; }
        th    { background: #2c3e50; color: white; padding: 11px 14px;
                text-align: left; font-size: 13px; cursor: pointer; user-select: none; }
        th:hover { background: #3d5166; }
        td    { padding: 9px 14px; border-bottom: 1px solid #eee; font-size: 13px; }
        tr:hover td { background: #f0f4ff; }
        .group { background: #e8f0fe; color: #1a56db; padding: 2px 8px;
                 border-radius: 10px; font-size: 11px; }
        .empty { text-align: center; padding: 40px; color: #999; font-size: 15px; }
        #pagination { margin-top: 14px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        #pagination button {
            padding: 5px 12px; border: 1px solid #ccc; border-radius: 4px;
            background: white; cursor: pointer; font-size: 13px; }
        #pagination button.active { background: #2c3e50; color: white; border-color: #2c3e50; }
        #pagination button:hover:not(.active) { background: #f0f4ff; }
    </style>
</head>
<body>
    <h2>📦 Stock Master — TallyPrime</h2>
    <div class="toolbar">
        <input type="text" id="search" placeholder="🔍 Search item or group...">
        <span id="info"></span>
    </div>
    <table id="stockTable">
        <thead>
            <tr>
                <th>#</th>
                <th onclick="sortTable(1)">Stock Item ↕</th>
                <th onclick="sortTable(2)">Stock Group ↕</th>
                <th onclick="sortTable(3)">Std. Sell Price ↕</th>
            </tr>
        </thead>
        <tbody id="tableBody"></tbody>
    </table>
    <div id="pagination"></div>

    <script>
        // ── Raw data injected from Python ──
        const ALL_DATA = {{ data_json|safe }};

        const PAGE_SIZE = 100;
        let filtered = [...ALL_DATA];
        let currentPage = 1;
        let debounceTimer = null;
        let sortCol = -1, sortAsc = true;

        function renderTable() {
            const start = (currentPage - 1) * PAGE_SIZE;
            const slice = filtered.slice(start, start + PAGE_SIZE);
            const tbody = document.getElementById("tableBody");

            tbody.innerHTML = slice.map((row, i) => `
                <tr>
                    <td>${start + i + 1}</td>
                    <td><strong>${row[0]}</strong></td>
                    <td><span class="group">${row[1]}</span></td>
                    <td>${row[2]}</td>
                </tr>
            `).join("");

            document.getElementById("info").textContent =
                `Showing ${Math.min(start+1, filtered.length)}–${Math.min(start+PAGE_SIZE, filtered.length)} of ${filtered.length} items`;

            renderPagination();
        }

        function renderPagination() {
            const total = Math.ceil(filtered.length / PAGE_SIZE);
            const el = document.getElementById("pagination");
            if (total <= 1) { el.innerHTML = ""; return; }

            let pages = [];
            pages.push(`<button onclick="goPage(1)" ${currentPage===1?'class="active"':''}>1</button>`);
            if (currentPage > 3) pages.push(`<span>…</span>`);
            for (let p = Math.max(2, currentPage-1); p <= Math.min(total-1, currentPage+1); p++) {
                pages.push(`<button onclick="goPage(${p})" ${currentPage===p?'class="active"':''}>${p}</button>`);
            }
            if (currentPage < total - 2) pages.push(`<span>…</span>`);
            if (total > 1) pages.push(`<button onclick="goPage(${total})" ${currentPage===total?'class="active"':''}>${total}</button>`);

            el.innerHTML =
                `<button onclick="goPage(${currentPage-1})" ${currentPage===1?'disabled':''}>← Prev</button>` +
                pages.join("") +
                `<button onclick="goPage(${currentPage+1})" ${currentPage===total?'disabled':''}>Next →</button>`;
        }

        function goPage(p) {
            const total = Math.ceil(filtered.length / PAGE_SIZE);
            currentPage = Math.max(1, Math.min(p, total));
            renderTable();
            window.scrollTo(0,0);
        }

        function sortTable(col) {
            if (sortCol === col) sortAsc = !sortAsc;
            else { sortCol = col; sortAsc = true; }
            filtered.sort((a, b) => {
                const va = a[col-1].toLowerCase();
                const vb = b[col-1].toLowerCase();
                return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
            });
            currentPage = 1;
            renderTable();
        }

        // ── Debounced search on pre-built index ──
        const SEARCH_INDEX = ALL_DATA.map(r => (r[0] + " " + r[1]).toLowerCase());

        document.getElementById("search").addEventListener("input", function() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                const q = this.value.toLowerCase().trim();
                if (q === "") {
                    filtered = [...ALL_DATA];
                } else {
                    filtered = ALL_DATA.filter((_, i) => SEARCH_INDEX[i].includes(q));
                }
                currentPage = 1;
                renderTable();
            }, 250);
        });

        // ── Init ──
        renderTable();
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    xml_text  = fetch_stock_data()
    items     = parse_stock_data(xml_text)
    data_json = json.dumps([[i["name"], i["parent"], i["std_price"]] for i in items])
    return render_template_string(HTML_TEMPLATE, data_json=data_json, total=len(items))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
