#!/usr/bin/env python3
"""
Unit test for book_data.clean_price, the helper the pushers use to read a price from the
products JSON. Numbers must pass through untouched; text prices must be converted; anything
unreadable must become None (the book is then skipped as "no price", not a crash).

    python tests/test_book_data.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from book_data import clean_price  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          -> {detail}" if detail and not ok else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n=== book_data.clean_price ===")
    for v in (9, 9.5, 28.0, 0.01, 99999999.99):
        check(f"positive number {v!r} is returned unchanged (same value and type)", clean_price(v) == v and type(clean_price(v)) is type(v))
    cases = {
        "12,50": 12.5, "12.50": 12.5, "12.5": 12.5, "12": 12.0, "0,99": 0.99, "0.999": 0.999,
        "12.50 €": 12.5, "€ 12,50": 12.5, "12,50 EUR": 12.5, "EUR 12.50": 12.5, "12,50 €": 12.5, " 12,50 ": 12.5,
        "1.234,56": 1234.56, "1,234.56": 1234.56, "1.234": 1234.0, "1,234": 1234.0, "1.234.567": 1234567.0,
        "1.234,5": 1234.5, "1,234,567.89": 1234567.89, "1234,56": 1234.56, "1234.56": 1234.56,
    }
    for text, want in cases.items():
        got = clean_price(text)
        check(f"text {text!r} -> {want}", got is not None and math.isclose(got, want), f"got {got!r}")
    for bad in (None, "", "   ", "abc", "gratis", "1e3", "12,50,", "1.2.3,4,5", "--5", 0, 0.0, -5, -0.01, "0", "0,00", "-5", "-5,50", "12,5.3", ".5", "5.", "12.34.56", "1,23,456", True, False, float("nan"), float("inf"), [], {}, "€"):
        check(f"unreadable {bad!r} -> None", clean_price(bad) is None, f"got {clean_price(bad)!r}")
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAIL  {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
