import re
import unicodedata
from decimal import Decimal, InvalidOperation


def text(value):
    if value is None:
        return ""
    return str(value).strip()


def key(value):
    raw = text(value).upper()
    return "".join(c for c in unicodedata.normalize("NFD", raw) if unicodedata.category(c) != "Mn")


def identifier(value):
    return re.sub(r"[^A-Z0-9]", "", key(value))


def lot(value):
    return identifier(value)


def document(value):
    raw = text(value)
    if raw.endswith(".0"):
        raw = raw[:-2]
    return raw.zfill(9) if raw.isdigit() else identifier(raw)


def unit(value):
    value = key(value)
    aliases = {"L": "L", "LT": "L", "LITRO": "L", "LITROS": "L", "KG": "KG", "KILO": "KG", "QUILOS": "KG", "UN": "UN", "UNIDADE": "UN"}
    return aliases.get(value, value)


def number(value):
    raw = text(value).replace(".", "").replace(",", ".")
    try:
        return float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None
