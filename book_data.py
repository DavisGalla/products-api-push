"""Small helpers shared by the pushers for reading the products JSON."""
import math
import re

_CURRENCY_WORDS = re.compile(r"(?i)\b(eur|euro|euros|gbp|chf|pln|usd|sek|dkk|nok|czk|huf|ron|zł|zl|kr)\b")


def _grouped(text, sep):
    """True for 1-3 digits followed by groups of exactly 3 digits, e.g. 1.234 or 12.345.678."""
    groups = text.split(sep)
    return (groups[0].isdigit() and 1 <= len(groups[0]) <= 3
            and all(g.isdigit() and len(g) == 3 for g in groups[1:]))


def clean_price(value):
    """A price as a number, or None when there is no usable one.

    Positive numbers come back unchanged. Text such as "12,50", "12.50 €", "€ 1.234,56" or
    "1,234.56" is converted. Anything else (empty, "abc", "1e3", True, NaN, zero or a
    negative amount, a stray extra separator like "12,50,") gives None, so the book is
    skipped as "no price" instead of crashing the run, going out with a negative price, or
    being priced 100 times too high.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    s = _CURRENCY_WORDS.sub("", value)
    s = re.sub(r"[€$£\s ]", "", s)
    if not re.fullmatch(r"-?[0-9.,]+", s):
        return None
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("-")
    if "," in s and "." in s:                      # 1.234,56 or 1,234.56: the last one is the decimal point
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        whole, _, fraction = s.rpartition(decimal)
        if not (_grouped(whole, thousands) and fraction.isdigit()):
            return None
        s = whole.replace(thousands, "") + "." + fraction
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2:                         # 1.234.567: only valid as thousands separators
            if not _grouped(s, sep):
                return None
            s = "".join(parts)
        elif not (parts[0].isdigit() and parts[1].isdigit()):
            return None                            # ".5", "5." and similar
        elif len(parts[1]) == 3 and 1 <= len(parts[0]) <= 3 and parts[0] != "0":
            s = "".join(parts)                     # 1.234 / 1,234: a thousands separator
        else:
            s = parts[0] + "." + parts[1]          # 12,50 / 12.5: a decimal point
    try:
        number = sign * float(s)
    except ValueError:
        return None
    return number if math.isfinite(number) and number > 0 else None
