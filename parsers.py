"""
Turns raw Tally XML into plain Python data structures. Kept free of any
networking or caching concerns so it's easy to unit test on saved XML.
"""
import logging
import re
import xml.etree.ElementTree as ET

from xml_utils import clean_xml, fmt_tally_date

log = logging.getLogger("parsers")


def parse_stock_items(xml_text: str):
    if not xml_text:
        return []
    try:
        root = ET.fromstring(clean_xml(xml_text))
    except ET.ParseError as e:
        log.warning("Stock XML parse error: %s", e)
        return []
    items = []
    for item in root.iter("STOCKITEM"):
        name = item.get("NAME") or item.findtext("NAME", "")
        if not name:
            continue
        parent = item.findtext("PARENT", "\u2014")
        std_price = item.findtext("STANDARDPRICE", "\u2014")
        items.append([name, parent, std_price])
    return items


def _voucher_sort_key(row):
    """Newest first: primary key is the voucher date, secondary is the
    numeric part of the voucher number (handles formats like KT/1059/26-27)."""
    date_str = row[0]
    date_key = (date_str[6:] + date_str[3:5] + date_str[:2]) \
        if (date_str and len(date_str) == 10 and date_str[2] == '/') else (date_str or "")
    m = re.search(r'(\d+)(?:/[^/]*)?$', row[1] or "")
    num_key = int(m.group(1)) if m else 0
    return (date_key, num_key)


def parse_voucher_list(results):
    """`results` is a list of (vtype_label, xml_text_or_None)."""
    vouchers = []
    for vtype_label, xml_text in results:
        if not xml_text:
            continue
        try:
            root = ET.fromstring(clean_xml(xml_text))
        except ET.ParseError as e:
            log.warning("Voucher XML parse error (%s): %s", vtype_label, e)
            continue
        for v in root.iter("VOUCHER"):
            vnum = (v.findtext("VOUCHERNUMBER", "") or v.get("VOUCHERNUMBER", "") or v.get("NAME", "\u2014"))
            if not vnum:
                continue
            vouchers.append([
                fmt_tally_date(v.findtext("DATE", "")),
                vnum,
                v.findtext("VOUCHERTYPENAME", "") or vtype_label,
                v.findtext("PARTYLEDGERNAME", "\u2014"),
                v.findtext("AMOUNT", "\u2014"),
            ])
    vouchers.sort(key=_voucher_sort_key, reverse=True)
    return vouchers


_LEDGER_TAGS = ["ALLLEDGERENTRIES", "LEDGERENTRIES", "ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"]
_INVENTORY_TAGS = ["ALLINVENTORYENTRIES", "INVENTORYENTRIES", "ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"]


def parse_voucher_detail(xml_text: str, vnum: str, vtype: str):
    if not xml_text:
        return None
    try:
        root = ET.fromstring(clean_xml(xml_text))
    except ET.ParseError as e:
        log.warning("Voucher detail XML parse failed: %s", e)
        return None

    target = vnum.strip().lower()
    all_vouchers = list(root.iter("VOUCHER"))
    matched = next(
        (v for v in all_vouchers
         if (v.findtext("VOUCHERNUMBER", "") or v.get("VOUCHERNUMBER", "") or v.get("NAME", "")).strip().lower() == target),
        None,
    )
    if matched is None and len(all_vouchers) == 1:
        matched = all_vouchers[0]  # single result — trust it even if the number format differs slightly
    if matched is None:
        log.info("No match for vnum=%r among %d vouchers in window", vnum, len(all_vouchers))
        return None

    v = matched
    ledgers = []
    for tag in _LEDGER_TAGS:
        entries = list(v.iter(tag))
        if entries:
            for le in entries:
                is_pos = (le.findtext("ISDEEMEDPOSITIVE", "") or le.findtext("ISDEEMEDNPOSITIVE", "")).strip().lower()
                ledgers.append({
                    "name": le.findtext("LEDGERNAME", "") or le.get("NAME", "") or "\u2014",
                    "amount": le.findtext("AMOUNT", "\u2014"),
                    "dr_cr": "Dr" if is_pos == "yes" else "Cr",
                })
            break

    inventory = []
    for tag in _INVENTORY_TAGS:
        entries = list(v.iter(tag))
        if entries:
            for ie in entries:
                inventory.append({
                    "name": ie.findtext("STOCKITEMNAME", "") or ie.get("NAME", "\u2014"),
                    "billed_qty": ie.findtext("BILLEDQTY", "\u2014"),
                    "actual_qty": ie.findtext("ACTUALQTY", "\u2014"),
                    "rate": ie.findtext("RATE", "\u2014"),
                    "amount": ie.findtext("AMOUNT", "\u2014"),
                })
            break

    return {
        "date": fmt_tally_date(v.findtext("DATE", "")),
        "vnum": (v.findtext("VOUCHERNUMBER", "") or v.get("VOUCHERNUMBER", "") or v.get("NAME", "")) or vnum,
        "vtype": v.findtext("VOUCHERTYPENAME", "") or vtype,
        "party": v.findtext("PARTYLEDGERNAME", "\u2014"),
        "amount": v.findtext("AMOUNT", "\u2014"),
        "narration": v.findtext("NARRATION", "").strip(),
        "reference": v.findtext("REFERENCE", "").strip(),
        "ledgers": ledgers,
        "inventory": inventory,
    }
