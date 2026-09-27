"""Polymarket helpers: wallet numbers exactly as the public profile page shows them.

Rule: numbers about a wallet come from the profile endpoints (the same data a reader sees at
polymarket.com/@name), not from the leaderboard. The leaderboard computes P&L differently and is
used only to find wallets and name their rank.
"""
from __future__ import annotations

import re
import time

from lab.net import getj, safe

DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PNL_API = "https://user-pnl-api.polymarket.com/user-pnl"

SPORT = re.compile(r"\bvs\.?\b|O/U|Spread|Championship|League|Cup\b|Open\b|Grand Prix|^(nfl|nba|mlb|nhl|cfb|"
                   r"cbb|ncaa|epl|ucl|uel|mls|atp|wta|ufc|f1|pga|cs2|lol|dota|val)-", re.I)


def wallet_url(wallet: str, name: str | None) -> str:
    return (f"polymarket.com/@{name.lower()}" if name and not name.startswith("0x")
            else f"polymarket.com/profile/{wallet}")


def _pnl(wallet: str, interval: str) -> list[dict]:
    """P&L curve of a profile: the same points as the chart on the profile page."""
    return getj(f"{PNL_API}?user_address={wallet}&interval={interval}&fidelity=1d") or []


def profile(wallet: str, name: str | None = None) -> dict:
    """Wallet numbers as shown on its Polymarket profile.

    A "win" is a position in a market that resolved in its favor (curPrice = 1). A position sold
    before resolution is an exit in profit, not a win.
    """
    allp, wk, day = _pnl(wallet, "all"), _pnl(wallet, "1w"), _pnl(wallet, "1d")
    closed = getj(f"{DATA_API}/closed-positions?user={wallet}&limit=50&sortBy=REALIZEDPNL&sortDirection=DESC")
    pos = getj(f"{DATA_API}/positions?user={wallet}&limit=100&sizeThreshold=1")

    def pos_row(p: dict, now_key: str) -> dict:
        stake = (p.get("totalBought") or p.get("size") or 0) * (p.get("avgPrice") or 0)
        return {"market": p.get("title"), "side": p.get("outcome"), "avg_price": round(p.get("avgPrice") or 0, 4),
                "now_price": round(p.get(now_key) or 0, 4), "stake_usd": round(stake)}

    wins = [p for p in closed if (p.get("curPrice") or 0) >= 0.999]
    exits_7d = [p for p in closed if (p.get("curPrice") or 0) < 0.999 and p.get("timestamp", 0) >= time.time() - 7 * 86400]
    top = sorted(pos, key=lambda p: -(p.get("currentValue") or 0))[:3]
    return {
        "profile_url": wallet_url(wallet, name),
        "total_pnl_usd": round(allp[-1]["p"]) if allp else None,
        # wallet younger than the window: the curve does not start at zero, so use the all-time total
        "pnl_7d_usd": (round(allp[-1]["p"]) if allp and wk and allp[0]["t"] >= wk[0]["t"] else
                       round(wk[-1]["p"] - wk[0]["p"]) if len(wk) > 1 else None),
        "pnl_24h_usd": (round(allp[-1]["p"]) if allp and day and allp[0]["t"] >= day[0]["t"] else
                        round(day[-1]["p"] - day[0]["p"]) if len(day) > 1 else None),
        "biggest_win_resolved": ({**pos_row(wins[0], "curPrice"), "profit_usd": round(wins[0]["realizedPnl"])} if wins else None),
        "biggest_exit_7d_sold_before_resolution": ({**pos_row(exits_7d[0], "curPrice"), "profit_usd": round(exits_7d[0]["realizedPnl"])}
                                                   if exits_7d else None),
        "open_positions": len(pos),
        "open_value_usd": round(sum(p.get("currentValue") or 0 for p in pos)),
        "open_unrealized_usd": round(sum(p.get("cashPnl") or 0 for p in pos)),
        "open_sides": {o: sum(1 for p in pos if p.get("outcome") == o) for o in {p.get("outcome") for p in pos}},
        "top_open": [{**pos_row(p, "curPrice"), "value_usd": round(p.get("currentValue") or 0),
                      "unrealized_usd": round(p.get("cashPnl") or 0)} for p in top],
    }


@safe
def whales() -> dict:
    """Daily and weekly leaders (overall / crypto), their profile numbers, and big bets in 24h."""
    out = {"rule": "wallet numbers come from the profile (as on the wallet page); rank comes from the leaderboard"}
    for key, period, cat in (("top_day_overall", "DAY", "OVERALL"), ("top_week_crypto", "WEEK", "CRYPTO")):
        rows = getj(f"{DATA_API}/v1/leaderboard?timePeriod={period}&orderBy=PNL&limit=8&category={cat}")
        rows = [r for r in rows if (r.get("vol") or 0) > 0][:5]
        out[key] = []
        for i, r in enumerate(rows):
            row = {"name": r.get("userName"), "wallet": r.get("proxyWallet"), "leaderboard_rank": r.get("rank"),
                   "leaderboard": f"{period.lower()} / {cat.lower()}"}
            if i < 3:
                try:
                    row["profile"] = profile(r["proxyWallet"], r.get("userName"))
                except Exception as e:
                    row["profile_error"] = str(e)[:120]
            out[key].append(row)
    trades = getj(f"{DATA_API}/trades?limit=200&filterType=CASH&filterAmount=25000&takerOnly=true")
    big = [{"market": t["title"], "side": t.get("outcome"), "buy_or_sell": t.get("side"),
            "usd": round(t["size"] * t["price"]), "price": round(t["price"], 3),
            "who": t.get("name") or t.get("pseudonym"), "wallet": t.get("proxyWallet"),
            "hours_ago": round((time.time() - t["timestamp"]) / 3600, 1)}
           for t in trades if not SPORT.search(t.get("title", "")) and not SPORT.search(t.get("slug", ""))
           and t["timestamp"] >= time.time() - 86400]
    out["big_bets_24h"] = sorted(big, key=lambda x: -x["usd"])[:8]
    return out


def collect() -> dict:
    return whales()
