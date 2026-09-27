"""Unsourced-number check: which numbers in a text do not appear in the sources.

Accounts for rounding ("1.1M" covers 1,127,059), sums and differences of two source numbers
("$1,017,071 back" = $499,699 invested + $517,372 profit), prices written as percent or cents
(0.625 -> 62.5%), and ignores wallet addresses.
"""
from __future__ import annotations

import re

MULT = {"k": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
NUM = r"(\d[\d,]*\.?\d*)\s?(k|m|b|million|billion)?\b"


def _num(raw: str, suffix: str = "") -> float | None:
    try:
        return float(raw.replace(",", "")) * MULT.get(suffix.lower().strip(), 1)
    except ValueError:
        return None


def source_pool(sources: str) -> tuple[list[float], set[float]]:
    """Numbers found in the sources, plus pairwise sums/differences of the large ones."""
    sources = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", sources)   # "1 234 567" -> "1234567"
    pool = [v for r, sfx in re.findall(NUM, sources, re.I) if (v := _num(r, sfx or "")) is not None]
    pool += [v * 100 for v in pool if 0 < v < 1]          # 0.625 is written as 62.5% or 62.5 cents
    big = sorted({v for v in pool if v >= 1000})[:400]
    derived = ({a + b for i, a in enumerate(big) for b in big[i:]}
               | {abs(a - b) for i, a in enumerate(big) for b in big[i + 1:]})
    return pool, derived


def unsourced_numbers(text: str, sources: str) -> list[str]:
    """Numbers (>= 10) in `text` that cannot be traced to `sources`, allowing for rounding and a+b / a-b."""
    pool, derived = source_pool(sources)
    bad = []
    text = re.sub(r"0x[0-9a-fA-F]+(?:(?:…|\.\.\.)[0-9a-fA-F]+)?", " ", text)   # addresses are not results
    for m in re.finditer(r"\$?" + NUM, text, re.I):
        v = _num(m.group(1), m.group(2) or "")
        if v is None or v < 10:
            continue
        dec = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
        step = 10 ** -dec * MULT.get((m.group(2) or "").lower().strip(), 1)
        tol = max(0.01 * v, step / 2)                        # "1.1M" covers 1.05M-1.15M
        if any(abs(v - s) <= tol for s in pool) or any(abs(v - s) <= step / 2 + 1 for s in derived):
            continue
        bad.append(m.group(0).strip())
    return sorted(set(bad))
