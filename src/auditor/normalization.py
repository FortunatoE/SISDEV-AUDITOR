import re
import unicodedata
import math
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
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, Decimal)):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None
    raw = text(value).replace("\u00a0", "").replace(" ", "")
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    raw = re.sub(r"[^0-9,\.\-+]", "", raw)
    if "," in raw and "." in raw:
        # The last separator is the decimal mark; the other one groups thousands.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        parsed = float(Decimal(raw))
        return -parsed if negative else parsed
    except (InvalidOperation, ValueError):
        return None
