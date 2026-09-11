"""
Low-level helpers for dealing with Tally's (often malformed) XML output.
"""
import re
from datetime import datetime

_CTRL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_BAD_HEX_ENTITY_RE = re.compile(r'&#x([0-9A-Fa-f]+);')
_BAD_DEC_ENTITY_RE = re.compile(r'&#([0-9]+);')
_BARE_AMP_RE = re.compile(r'&(?!amp;|lt;|gt;|quot;|apos;|#)')
_XMLNS_RE = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')
_NS_TAG_RE = re.compile(r'<(/?)(\w+):(\w)')
_NS_ATTR_RE = re.compile(r'(\s)(\w+):(\w+)=')


def clean_xml(xml_text: str) -> str:
    """Strip the control characters, bad entities, and namespace prefixes
    that Tally's export commonly emits and that trip up ElementTree."""
    xml_text = _CTRL_CHARS_RE.sub('', xml_text)
    xml_text = _BAD_HEX_ENTITY_RE.sub(
        lambda m: '' if int(m.group(1), 16) < 32 and int(m.group(1), 16) not in (9, 10, 13) else m.group(0),
        xml_text,
    )
    xml_text = _BAD_DEC_ENTITY_RE.sub(
        lambda m: '' if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13) else m.group(0),
        xml_text,
    )
    xml_text = _BARE_AMP_RE.sub('&amp;', xml_text)
    xml_text = xml_text.replace('\u20b9', 'Rs')
    xml_text = _XMLNS_RE.sub('', xml_text)
    xml_text = _NS_TAG_RE.sub(r'<\1\3', xml_text)
    xml_text = _NS_ATTR_RE.sub(r'\1\3=', xml_text)
    return xml_text


def fmt_tally_date(date_str: str) -> str:
    """Tally dates come as YYYYMMDD; render as DD/MM/YYYY."""
    if date_str and len(date_str) == 8:
        try:
            return datetime.strptime(date_str, "%Y%m%d").strftime("%d/%m/%Y")
        except ValueError:
            pass
    return date_str or "\u2014"


def to_tally_date(dt: datetime) -> str:
    """Python datetime -> Tally's YYYYMMDD request format."""
    return dt.strftime("%Y%m%d")


def fmt_amount(raw):
    """Return (css_class, display_string) for an amount string, with
    Indian-style digit grouping."""
    if not raw or raw in ("\u2014", "—"):
        return ("", "\u2014")
    try:
        n = float(str(raw).replace(",", "").strip())
        abs_str = f"{abs(n):,.2f}"
        intg, dec = abs_str.replace(",", "").split(".")
        if len(intg) > 3:
            intg = intg[:-3] + "," + intg[-3:]
            i = len(intg) - 7
            while i > 0:
                intg = intg[:i] + "," + intg[i:]
                i -= 2
        sign = "+" if n >= 0 else "\u2212"
        css = "amount-credit" if n >= 0 else "amount-debit"
        return (css, f"Rs {sign}{intg}.{dec}")
    except (ValueError, TypeError):
        return ("", str(raw))
