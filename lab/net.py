"""Shared read-only HTTP helpers: GET/POST JSON with a global time budget.

Only public endpoints, no API keys, no auth headers. POST is used only for JSON-RPC style
read calls (Hyperliquid info API, Ethereum/Solana RPC) that do not change any state.
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import os
import time
import urllib.request

UA = {"User-Agent": os.environ.get("LAB_USER_AGENT") or "luthor-lab/0.1 (read-only research)"}

_deadline: float | None = None


def set_deadline(ts: float | None) -> None:
    """After this unix time every request fails fast, so a scan never runs past its budget."""
    global _deadline
    _deadline = ts


def left() -> float:
    return float("inf") if _deadline is None else _deadline - time.time()


def get(url: str, timeout: float = 20, body: dict | None = None) -> bytes:
    t = min(timeout, left())
    if t < 1:
        raise TimeoutError("scan time budget exhausted")
    headers = {**UA, "Accept-Encoding": "gzip"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=t) as r:
        raw = r.read()
        return gzip.decompress(raw) if r.headers.get("Content-Encoding") == "gzip" else raw


def getj(url: str, timeout: float = 20, body: dict | None = None):
    return json.loads(get(url, timeout, body))


def getj_retry(url: str, timeout: float = 15):
    """JSON GET with one retry (some public APIs return sporadic 5xx)."""
    try:
        return getj(url, timeout)
    except Exception:
        time.sleep(0.4)
        return getj(url, timeout)


def safe(fn):
    """Wrap a collector so one failing source returns {"error": ...} instead of crashing the scan."""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


def pmap_dict(fn, items, workers: int = 8) -> dict:
    """Parallel fn(item) -> {item: result}; failed items are skipped."""
    out = {}
    items = list(items)
    if not items:
        return out
    with cf.ThreadPoolExecutor(min(workers, len(items))) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for f in cf.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception:
                pass
    return out


def pmap_list(fn, items, workers: int = 8) -> list:
    """Parallel map preserving order; a failed item becomes None."""
    items = list(items)
    if not items:
        return []
    with cf.ThreadPoolExecutor(min(workers, len(items))) as ex:
        futs = [ex.submit(fn, x) for x in items]
    out = []
    for f in futs:
        try:
            out.append(f.result())
        except Exception:
            out.append(None)
    return out


def usd(x: float | None) -> str:
    """$1.6B / $1.6M / $312K / $950."""
    if x is None:
        return "?"
    a, s = abs(x), "-" if x < 0 else ""
    if a >= 1e9:
        return f"{s}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{s}${a / 1e3:.0f}K"
    return f"{s}${a:.0f}"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
