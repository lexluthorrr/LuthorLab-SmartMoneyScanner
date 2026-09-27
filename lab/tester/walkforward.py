"""TESTER: walk-forward template for a simple trading rule on a price CSV.

It runs the same rule through filters 1-5 of the checklist (see lab/tester/README.md):
in-sample -> out-of-sample -> fees -> honest fill -> pessimism. Filter 6 (tiny live) is yours.

  python -m lab.tester.walkforward --demo
      synthetic random walk (no edge by construction): watch the in-sample "edge" disappear
  python -m lab.tester.walkforward --fetch BTC-USD --granularity 3600 --bars 3000
      download public hourly candles from Coinbase Exchange into data/, then test on them
  python -m lab.tester.walkforward data/my_prices.csv --price-col close --fee-bps 10

CSV: one row per bar, oldest first (or any order with a time column), a price column
(close/price auto-detected). Edit `rule()` and `PARAM_GRID` below to test your own idea.
This is a research template, not a trading system: it never places orders.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path

from lab import config

# ------------------------------------------------------------------ EDIT HERE: your rule

PARAM_GRID = {
    "lookback": [1, 2, 3, 5, 10, 20],   # bars to measure the recent move
    "z": [0.0, 0.5, 1.0],               # minimum move size in standard deviations of 1-bar returns
    "mode": ["momentum", "reversal"],   # follow the move or fade it
    "hold": [1, 3],                     # bars to hold the position
}


def rule(prices: list[float], i: int, p: dict, vol: float) -> int:
    """Position to take after bar i closes: +1 long, -1 short, 0 flat. Uses only prices[: i + 1]."""
    lb = p["lookback"]
    if i < lb:
        return 0
    move = prices[i] / prices[i - lb] - 1
    if abs(move) <= p["z"] * vol * math.sqrt(lb):
        return 0
    side = 1 if move > 0 else -1
    return side if p["mode"] == "momentum" else -side

# ------------------------------------------------------------------ engine


def trades(prices: list[float], lo: int, hi: int, p: dict, vol: float, entry_lag: int = 0,
           cost: float = 0.0) -> list[float]:
    """Non-overlapping trade returns for signals in [lo, hi). entry_lag=1 enters one bar after the signal."""
    out, i = [], lo
    while i < hi:
        pos = rule(prices, i, p, vol)
        e, x = i + entry_lag, i + entry_lag + p["hold"]
        if pos and x < len(prices):
            out.append(pos * (prices[x] / prices[e] - 1) - cost)
            i = x                                   # no overlapping positions: trades stay independent
        else:
            i += 1
    return out


def stats(r: list[float]) -> dict:
    n = len(r)
    if n < 2:
        return {"n": n, "win": None, "mean_bps": None, "t": None}
    sd = statistics.stdev(r)
    mean = statistics.fmean(r)
    return {"n": n, "win": 100 * sum(x > 0 for x in r) / n, "mean_bps": mean * 1e4,
            "t": mean / (sd / math.sqrt(n)) if sd > 0 else None}


def walk_forward(prices: list[float], folds: int, fee_bps: float, slip_bps: float, stress: float) -> dict:
    grid = [dict(zip(PARAM_GRID, v)) for v in itertools.product(*PARAM_GRID.values())]
    rets = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    size = len(prices) // (folds + 1)
    rows = {k: [] for k in ("is", "oos", "fees", "fill", "pess")}
    picks = []
    fee = 2 * fee_bps / 1e4                         # round trip
    slip = 2 * slip_bps / 1e4
    for f in range(1, folds + 1):
        tr_lo, tr_hi, te_hi = (f - 1) * size, f * size, (f + 1) * size   # rolling: train window, then the next one to test
        vol = statistics.pstdev(rets[tr_lo:tr_hi - 1]) or 1e-9
        scored = []
        for p in grid:
            s = stats(trades(prices, tr_lo, tr_hi, p, vol))
            if s["t"] is not None and s["n"] >= 20:
                scored.append((s["t"], p, s))
        if not scored:
            continue
        t_best, p_best, s_best = max(scored, key=lambda z: z[0])
        picks.append({"fold": f, "params": p_best, "is_t": round(t_best, 2), "is_n": s_best["n"]})
        rows["is"] += trades(prices, tr_lo, tr_hi, p_best, vol)
        rows["oos"] += trades(prices, tr_hi, te_hi, p_best, vol)
        rows["fees"] += trades(prices, tr_hi, te_hi, p_best, vol, cost=fee)
        rows["fill"] += trades(prices, tr_hi, te_hi, p_best, vol, entry_lag=1, cost=fee + slip)
        rows["pess"] += trades(prices, tr_hi, te_hi, p_best, vol, entry_lag=1, cost=stress * (fee + slip))
    return {"combos": len(grid), "folds": folds, "picks": picks, "rows": {k: stats(v) for k, v in rows.items()}}


def report(res: dict, t_min: float, fee_bps: float, slip_bps: float, stress: float) -> str:
    names = [("is", "1 in-sample (best of grid)"), ("oos", "2 out-of-sample"),
             ("fees", f"3 after fees ({fee_bps:g} bps/side)"),
             ("fill", f"4 honest fill (next bar, +{slip_bps:g} bps/side)"),
             ("pess", f"5 pessimism (costs x{stress:g})")]
    lines = [f"{'filter':<38}{'trades':>7}{'win %':>8}{'mean bps':>10}{'t-stat':>8}  verdict", "-" * 80]
    alive = True
    for key, label in names:
        s = res["rows"][key]
        ok = s["t"] is not None and s["mean_bps"] > 0 and s["t"] >= t_min
        verdict = ("pass" if ok else "DEAD") if alive else "-"
        alive &= ok
        fmt = lambda v, f: "-" if v is None else format(v, f)
        lines.append(f"{label:<38}{s['n']:>7}{fmt(s['win'], '.1f'):>8}{fmt(s['mean_bps'], '.2f'):>10}"
                     f"{fmt(s['t'], '.2f'):>8}  {verdict}")
    lines.append(f"{'6 tiny live':<38}{'':>33}  yours: small real money, compare with row 5")
    n = res["combos"]
    lucky = math.sqrt(2 * math.log(n)) if n > 1 else 0
    lines += ["", f"Tested {n} parameter combos per fold. The best of {n} pure-noise rules is expected to show "
                  f"t ~ {lucky:.1f} in-sample by luck alone.",
              f"Pass bar: mean > 0 and t >= {t_min:g}. Picks per fold: "
              + "; ".join(f"#{p['fold']} {p['params']} (IS t={p['is_t']})" for p in res["picks"][:6])]
    return "\n".join(lines)


# ------------------------------------------------------------------ data


def load_csv(path: Path, price_col: str | None, time_col: str | None) -> list[float]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{path}: empty CSV")
    cols = {c.lower(): c for c in rows[0]}
    pc = price_col or next((cols[c] for c in ("close", "price", "adj close", "last") if c in cols), None)
    if not pc or pc not in rows[0]:
        sys.exit(f"{path}: no price column (tried close/price); pass --price-col. Columns: {list(rows[0])}")
    tc = time_col or next((cols[c] for c in ("time", "timestamp", "date", "ts") if c in cols), None)
    if tc and tc in rows[0]:
        try:
            rows.sort(key=lambda r: float(r[tc]))
        except ValueError:
            rows.sort(key=lambda r: r[tc])          # ISO dates sort correctly as text
    prices = [float(r[pc]) for r in rows if r.get(pc) not in (None, "")]
    if len(prices) < 200:
        sys.exit(f"{path}: only {len(prices)} prices; walk-forward needs a few hundred bars at least")
    return prices


def demo_csv(n: int = 4000, seed: int = 7) -> Path:
    """Synthetic random walk with no edge by construction (fixed seed, reproducible)."""
    rnd = random.Random(seed)
    p, rows = 100.0, []
    for i in range(n):
        p *= math.exp(rnd.gauss(0, 0.01))
        rows.append((i, round(p, 6)))
    out = config.data_dir() / "demo_random_walk.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "close"])
        w.writerows(rows)
    return out


def fetch_coinbase(product: str, granularity: int, bars: int) -> Path:
    """Public Coinbase Exchange candles (no key). Max 300 per request, paged backwards."""
    ua = {"User-Agent": "luthor-lab/0.1 (read-only research)"}
    end, got = int(time.time()), {}
    while len(got) < bars:
        start = end - 300 * granularity
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={granularity}"
               f"&start={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start))}"
               f"&end={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end))}")
        with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=20) as r:
            page = json.loads(r.read())
        if not page:
            break
        for t, _lo, _hi, _op, close, _vol in page:
            got[t] = close
        end = start
        time.sleep(0.35)                            # stay well under the public rate limit
    out = config.data_dir() / f"{product}_{granularity}.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "close"])
        w.writerows(sorted(got.items())[-bars:])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.tester.walkforward", description=__doc__.split("\n")[0])
    ap.add_argument("csv", nargs="?", type=Path)
    ap.add_argument("--demo", action="store_true", help="synthetic random walk (no edge by construction)")
    ap.add_argument("--fetch", metavar="PRODUCT", help="download public Coinbase candles, e.g. BTC-USD")
    ap.add_argument("--granularity", type=int, default=3600, help="candle seconds for --fetch (60,300,900,3600,21600,86400)")
    ap.add_argument("--bars", type=int, default=3000, help="bars to download with --fetch")
    ap.add_argument("--price-col")
    ap.add_argument("--time-col")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee-bps", type=float, default=10.0, help="fee per side in basis points")
    ap.add_argument("--slippage-bps", type=float, default=5.0, help="extra cost per side for honest fill")
    ap.add_argument("--stress", type=float, default=2.0, help="cost multiplier for the pessimism filter")
    ap.add_argument("--t-min", type=float, default=3.0, help="minimum t-stat to pass a filter")
    a = ap.parse_args(argv)

    if a.demo:
        path = demo_csv()
        print(f"Synthetic random walk -> {path.name} (no edge by construction)\n")
    elif a.fetch:
        path = fetch_coinbase(a.fetch, a.granularity, a.bars)
        print(f"Downloaded public candles -> {path.name}\n")
    elif a.csv:
        path = a.csv
    else:
        ap.error("give a CSV path, --demo or --fetch PRODUCT")
    prices = load_csv(path, a.price_col, a.time_col)
    res = walk_forward(prices, a.folds, a.fee_bps, a.slippage_bps, a.stress)
    print(f"{len(prices)} bars, {a.folds} walk-forward folds (rolling: pick the best rule on one window, test it on the next)\n")
    print(report(res, a.t_min, a.fee_bps, a.slippage_bps, a.stress))
    return 0


if __name__ == "__main__":
    sys.exit(main())
