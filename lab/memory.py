"""MEMORY: what the lab has already seen, so a scan can say what is new and later check what happened.

Design rule: no model gets memory. State lives in code and in one small SQLite file,
<data dir>/lab.db (LAB_DATA_DIR is respected). The console, the bot and any model only ever get
fresh facts plus plain flags computed here: new or not, first seen, times seen, change since last time.

  python -m lab.memory report       hit-rates of past findings, always with n
  python -m lab.memory outcomes     check findings flagged >= 24 h / >= 7 d ago (same public keyless APIs)

Tables:
  runs      one row per scan (sources that answered / failed)
  findings  one row per finding per scan: kind, stable key, title, a few metrics, url
  seen      one row per key: first and last time seen, how many scans saw it
  outcomes  one row per key, flag time and horizon (24h / 7d): what the public data says now
Rows older than LAB_MEMORY_DAYS (default 30) are deleted at the start of every run; VACUUM runs
at most once a week. Read-only toward the outside world: outcome checks are public GETs / read calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lab import config, net

DB_NAME = "lab.db"
HORIZONS = {"24h": 86400, "7d": 7 * 86400}
HORIZON_TXT = {"24h": "~1 day", "7d": "~7 days"}
MISSED_AFTER = 3              # a check more than 3x its horizon late (24 h check after 72 h) is not measured
VACUUM_EVERY_S = 7 * 86400
TINY = 5                      # below this many checked findings a share is an anecdote, not a rate
DEFAULT_MAX_CHECKS = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, took_s REAL,
                                 ok_sources TEXT, failed_sources TEXT);
CREATE TABLE IF NOT EXISTS findings (run_id INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                                     title TEXT, metrics_json TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, kind TEXT, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
                                 times INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS outcomes (key TEXT NOT NULL, kind TEXT, flagged_ts REAL NOT NULL, horizon TEXT NOT NULL,
                                     checked_ts REAL NOT NULL, result_json TEXT, verdict TEXT,
                                     UNIQUE (key, flagged_ts, horizon));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS runs_ts ON runs (ts);
CREATE INDEX IF NOT EXISTS findings_ts ON findings (ts);
CREATE INDEX IF NOT EXISTS findings_key_ts ON findings (key, ts);
CREATE INDEX IF NOT EXISTS seen_first_ts ON seen (first_ts);
CREATE INDEX IF NOT EXISTS seen_last_ts ON seen (last_ts);
CREATE INDEX IF NOT EXISTS outcomes_flagged_ts ON outcomes (flagged_ts);
CREATE INDEX IF NOT EXISTS outcomes_checked_ts ON outcomes (checked_ts);
"""


# ---------------------------------------------------------------- database

def keep_days() -> int:
    try:
        return max(1, int(os.environ.get("LAB_MEMORY_DAYS") or 30))
    except ValueError:
        return 30


def db_path() -> Path:
    return config.data_dir() / DB_NAME


def connect(path: Path | str | None = None, now: float | None = None) -> sqlite3.Connection:
    """Open (and create) the memory file; old rows are deleted on every open."""
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.executescript(SCHEMA)
    cleanup(con, now)
    return con


def cleanup(con: sqlite3.Connection, now: float | None = None) -> int:
    """Retention: delete rows older than LAB_MEMORY_DAYS; VACUUM at most once a week."""
    now = now or time.time()
    cut = now - keep_days() * 86400
    with con:
        n = sum(con.execute(f"DELETE FROM {t} WHERE {c} < ?", (cut,)).rowcount
                for t, c in (("runs", "ts"), ("findings", "ts"), ("seen", "last_ts"), ("outcomes", "checked_ts")))
        last = con.execute("SELECT v FROM meta WHERE k = 'last_vacuum'").fetchone()
        if not last:
            con.execute("INSERT INTO meta (k, v) VALUES ('last_vacuum', ?)", (str(now),))
    if last and now - float(last[0]) >= VACUUM_EVERY_S:
        con.execute("VACUUM")
        with con:
            con.execute("UPDATE meta SET v = ? WHERE k = 'last_vacuum'", (str(now),))
    return n


def iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def day(ts_or_iso) -> str:
    """'Sep 28' from a unix time or an ISO string (UTC)."""
    if isinstance(ts_or_iso, str):
        try:
            return time.strftime("%b %d", time.strptime(ts_or_iso[:10], "%Y-%m-%d"))
        except ValueError:
            return ts_or_iso[:10]
    return time.strftime("%b %d", time.gmtime(ts_or_iso)) if ts_or_iso else "?"


# ---------------------------------------------------------------- findings and stable keys

def _h(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def _l(node, *keys) -> list:
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    return node if isinstance(node, list) else []


def _after(url: str | None, marker: str) -> str | None:
    """'pump.fun/coin/<mint>' -> '<mint>' for marker 'coin/'."""
    if not url or marker not in url:
        return None
    return url.split(marker, 1)[1].split("?")[0].split("#")[0].split("/")[0] or None


def _get(item: dict, path: str):
    node = item
    for k in path.split("."):
        node = node.get(k) if isinstance(node, dict) else None
    return node


def _chain(name: str | None) -> str:
    return {"solana": "sol"}.get(name or "", name or "?")


def coin_key(x: dict) -> str | None:
    """sol:<mint> / base:<token>; falls back to the pair url."""
    tok = x.get("token")
    url = x.get("url") or ""
    if tok:
        ch = x.get("chain") or ("base" if "basescan" in url else "solana")
        return f"{_chain(ch)}:{tok}"
    mint = _after(url, "/coin/")
    if mint:
        return f"sol:{mint}"
    return f"pair:{url}" if url else None


def _tx_key(url: str | None) -> str | None:
    h = _after(url, "/tx/")
    return f"tx:{h.lower()}" if h else None


# kind -> metric fields kept per snapshot ("a.b>name" stores a nested field under a new name)
FIELDS = {
    "hl_position": ["usd", "upnl", "lev", "to_liq_pct", "liq", "mark", "entry", "acct_value", "why"],
    "hl_pnl_board": ["acct_value"],
    "stable_transfer": ["usd", "token", "from", "to"],
    "usdt_issue": ["usd", "event"],
    "meme_move": ["mcap", "liq", "price", "mcap_liq", "pct24"],
    "meme_paper": ["mcap", "liq", "price", "mcap_liq", "pct24"],
    "meme_holders": ["top10_pct", "top1_pct", "same_n", "same_pct", "creator_pct", "holders", "top10_pct_incl_pool",
                     "mint_authority_live", "freeze_authority_live"],
    "pumpfun_curve": ["mcap", "ath", "curve_pct"],
    "pumpfun_graduated": ["mcap", "ath", "from_ath_pct"],
    "pm_leader": ["leaderboard_rank", "profile.total_pnl_usd>total_pnl_usd", "profile.pnl_24h_usd>pnl_24h_usd",
                  "profile.pnl_7d_usd>pnl_7d_usd", "profile.open_value_usd>open_value_usd"],
    "pm_big_bet": ["usd", "price"],
    "pm_fresh_wallet": ["realized_pnl_usd", "age_days", "profile.total_pnl_usd>total_pnl_usd",
                        "profile.open_unrealized_usd>open_unrealized_usd"],
    "pm_timed_bet": ["stake_usd", "entry_c", "now_c", "paper_profit_now_usd"],
    "pm_loser": ["profile.total_pnl_usd>total_pnl_usd"],
    "pm_celebrity": ["yes_pct", "vol24_usd", "chg_24h_pp"],
    "stock_move": ["chg_pct", "price", "rel_volume"],
    "ai_model": ["likes", "downloads"],
    "ai_story": ["points", "comments"],
}


def _metrics(kind: str, item: dict, extra: dict | None = None) -> dict:
    out = {}
    for spec in FIELDS.get(kind, []):
        path, _, name = spec.partition(">")
        v = _get(item, path)
        if v is not None:
            out[name or path] = v
    out.update({k: v for k, v in (extra or {}).items() if v is not None})
    return out


def _title(item: dict, fallback: str = "") -> str:
    lab = next((item[k] for k in ("sym", "ticker", "who", "name", "title") if isinstance(item.get(k), str) and item[k]), "")
    s = item.get("summary") or fallback
    return (f"{lab}: {s}" if lab and lab not in s else s or lab)[:200]


def extract(d: dict) -> list[dict]:
    """Every trackable finding in a digest: {item, kind, key, title, metrics, url}. Aggregates are skipped."""
    out: list[dict] = []

    def add(item, kind: str, key: str | None, title: str = "", extra: dict | None = None) -> None:
        if isinstance(item, dict) and key:
            out.append({"item": item, "kind": kind, "key": key, "title": _title(item, title),
                        "metrics": _metrics(kind, item, extra), "url": item.get("url") or item.get("tx")})

    p = d.get("perps") or {}
    for x in _l(p, "hyperliquid", "positions"):
        w = (x.get("wallet") or "").lower()
        add(x, "hl_position", w and f"hl:{w}:{x.get('coin')}:{x.get('side')}")
    for board, rows in ((p.get("hyperliquid") or {}).get("pnl_board") or {}).items():
        for x in rows if isinstance(rows, list) else []:
            w = (x.get("wallet") or "").lower()
            period = "pnl_24h_usd" if board.startswith("day") else "pnl_7d_usd"
            add(x, "hl_pnl_board", w and f"hl:{w}", extra={period: x.get("pnl")})
    for x in _l(p, "eth_transfers", "items"):
        add(x, "stable_transfer", _tx_key(x.get("tx")))
    for x in _l(p, "eth_transfers", "usdt_issue_redeem_24h"):
        add(x, "usdt_issue", _tx_key(x.get("tx")), f"USDT {x.get('event')} {net.usd(x.get('usd'))}")

    m = d.get("memes") or {}
    for x in _l(m, "gainers", "g24") + _l(m, "gainers", "g1"):
        add(x, "meme_move", coin_key(x))
    for x in _l(m, "paper_mcap", "coins"):
        add(x, "meme_paper", coin_key(x))
    for x in _l(m, "holders") + _l(m, "holders_base"):
        add(x, "meme_holders", coin_key(x))
    pf = m.get("pumpfun") or {}
    for x in pf.get("near_graduation") or []:
        add(x, "pumpfun_curve", coin_key(x))
    add((pf.get("graduated") or {}).get("example"), "pumpfun_graduated", coin_key((pf.get("graduated") or {}).get("example") or {}))

    w = d.get("polymarket") or {}
    for board in ("top_day_overall", "top_week_crypto"):
        for x in _l(w, board):
            add(x, "pm_leader", x.get("wallet") and f"pm:{x['wallet'].lower()}",
                f"{x.get('name') or (x.get('wallet') or '')[:10]} is #{x.get('leaderboard_rank')} on the Polymarket "
                f"{x.get('leaderboard')} leaderboard")
    for x in _l(w, "big_bets_24h"):
        add(x, "pm_big_bet", x.get("wallet") and f"pmbet:{x['wallet'].lower()}:{_h(x.get('market'), x.get('side'), x.get('buy_or_sell'), x.get('usd'))}",
            f"{x.get('who') or 'wallet'} {str(x.get('buy_or_sell') or '').lower()} {x.get('side')} for "
            f"{net.usd(x.get('usd'))} at {x.get('price')} on \"{x.get('market')}\"")

    s = d.get("signals") or {}
    for x in _l(s, "fresh_wallets"):
        add(x, "pm_fresh_wallet", x.get("wallet") and f"pm:{x['wallet'].lower()}")
    for x in _l(s, "timed_bets"):
        add(x, "pm_timed_bet", x.get("wallet") and f"pmbet:{x['wallet'].lower()}:{_h(x.get('market'), x.get('side'), x.get('bet_at'))}")
    for period, key in (("day", "pnl_24h_usd"), ("week", "pnl_7d_usd")):
        for x in _l(s, "losers", period):
            add(x, "pm_loser", x.get("wallet") and f"pm:{x['wallet'].lower()}",
                extra={key: (x.get("profile") or {}).get("pnl_period_usd")})
    for x in _l(s, "celebrity_markets"):
        slug = _after(x.get("url"), "/event/")
        add(x, "pm_celebrity", slug and f"pmevent:{slug}")
    for x in _l(s, "stocks"):
        add(x, "stock_move", x.get("ticker") and f"stock:{x['ticker']}:{x.get('date')}")
    for x in _l(s, "ai", "hf_trending"):
        add(x, "ai_model", x.get("name") and f"hf:{x['name']}")
    for x in _l(s, "ai", "hn_24h"):
        hid = (x.get("hn") or "").rsplit("id=", 1)[-1] if "id=" in (x.get("hn") or "") else None
        add(x, "ai_story", f"hn:{hid}" if hid else x.get("url") and f"url:{x['url']}")
    return out


def sources(d: dict) -> tuple[list[str], list[str]]:
    """(sources that answered, sources that failed) as 'module.section'."""
    from lab.scanner import MODULES
    ok, failed = [], []
    for m in MODULES:
        node = d.get(m)
        if node is None:
            continue
        if not isinstance(node, dict) or isinstance(node.get("error"), str):
            failed.append(m)
            continue
        for k, v in node.items():
            if isinstance(v, dict) and isinstance(v.get("error"), str):
                failed.append(f"{m}.{k}")
            elif isinstance(v, (dict, list)) and k not in ("status", "checked"):
                ok.append(f"{m}.{k}")
    return ok, failed


# ---------------------------------------------------------------- change since last time

# kind -> [(field, label, style, show if change >=, material if change >=)]
# styles: usd (signed $ change), usd_to ($a -> $b), rel (% change), pp (a% -> b%), count (signed number)
# the change is relative for usd/usd_to/rel/count and in points for pp
DELTAS = {
    "hl_position": [("usd", "size", "usd", 0.02, 0.2), ("upnl", "uPnL", "usd_to", 0.05, 0.5),
                    ("to_liq_pct", "to liquidation", "pp", 0.5, 3)],
    "hl_pnl_board": [("pnl_24h_usd", "24h P&L", "usd_to", 0.05, 0.3), ("pnl_7d_usd", "7d P&L", "usd_to", 0.05, 0.3),
                     ("acct_value", "account", "usd", 0.05, 0.3)],
    "meme_move": [("mcap", "cap", "rel", 0.05, 0.3), ("liq", "pool", "rel", 0.05, 0.3)],
    "meme_paper": [("mcap", "cap", "rel", 0.05, 0.3), ("liq", "pool", "rel", 0.05, 0.3)],
    "meme_holders": [("top10_pct", "top 10", "pp", 1, 5), ("same_n", "same-balance holders", "count", 0.01, 0.3)],
    "pumpfun_curve": [("mcap", "cap", "rel", 0.05, 0.3), ("curve_pct", "curve", "pp", 1, 10)],
    "pumpfun_graduated": [("mcap", "cap", "rel", 0.05, 0.3)],
    "pm_leader": [("total_pnl_usd", "profile P&L", "usd", 0.02, 0.2)],
    "pm_fresh_wallet": [("total_pnl_usd", "profile P&L", "usd", 0.02, 0.2),
                        ("realized_pnl_usd", "realized", "usd", 0.02, 0.2)],
    "pm_loser": [("total_pnl_usd", "profile P&L", "usd", 0.02, 0.2)],
    "pm_timed_bet": [("now_c", "price now (c)", "count", 0.02, 0.2)],
    "pm_celebrity": [("yes_pct", "Yes", "pp", 1, 5), ("vol24_usd", "24h volume", "rel", 0.1, 0.5)],
    "ai_model": [("likes", "likes", "count", 0.05, 0.5)],
    "ai_story": [("points", "points", "count", 0.05, 0.5)],
}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def delta(kind: str, prev: dict, cur: dict) -> dict | None:
    """{'text': 'size +$20.0M, to liquidation 8% -> 4%', 'material': bool} or None if nothing moved."""
    parts, material = [], False
    for field, label, style, show, mat in DELTAS.get(kind, []):
        a, b = _num(prev.get(field)), _num(cur.get(field))
        if a is None or b is None or a == b:
            continue
        if style == "pp":
            change = abs(b - a)
        elif style == "rel":
            change = abs(b / a - 1) if a else float("inf")
        else:
            change = abs(b - a) / max(abs(a), abs(b), 1e-9)
        if change < show:
            continue
        material |= change >= mat
        if style == "usd":
            parts.append(f"{label} {'+' if b > a else '-'}{net.usd(abs(b - a))}")
        elif style == "usd_to":
            parts.append(f"{label} {net.usd(a)} -> {net.usd(b)}")
        elif style == "rel":
            parts.append(f"{label} {(b / a - 1) * 100:+.0f}%" if a else f"{label} {net.usd(a)} -> {net.usd(b)}")
        elif style == "pp":
            parts.append(f"{label} {a:g}% -> {b:g}%")
        else:
            parts.append(f"{label} {b - a:+,.0f}" if abs(b - a) >= 1 else f"{label} {a:g} -> {b:g}")
    return {"text": ", ".join(parts[:3]), "material": material} if parts else None


# ---------------------------------------------------------------- remember a scan

def remember(d: dict, path: Path | str | None = None, now: float | None = None) -> dict:
    """Store the scan, update `seen`, and flag every finding in the digest in place:
    new, first_seen, times_seen and (for repeats) delta vs the previous snapshot of the same key."""
    now = round(now or time.time(), 3)
    groups: dict[tuple, dict] = {}
    for f in extract(d):
        g = groups.setdefault((f["kind"], f["key"]), {**f, "items": [], "metrics": {}})
        g["items"].append(f["item"])
        g["metrics"].update({k: v for k, v in f["metrics"].items() if k not in g["metrics"]})
    con = connect(path, now)
    try:
        prev_run = con.execute("SELECT MAX(ts) FROM runs").fetchone()[0]
        ok, failed = sources(d)
        keys: dict[str, tuple] = {}
        n_changed = 0
        with con:
            run_id = con.execute("INSERT INTO runs (ts, took_s, ok_sources, failed_sources) VALUES (?, ?, ?, ?)",
                                 (now, d.get("took_s"), json.dumps(ok), json.dumps(failed))).lastrowid
            for (kind, key), g in groups.items():
                if key not in keys:
                    row = con.execute("SELECT first_ts, times FROM seen WHERE key = ?", (key,)).fetchone()
                    if row:
                        keys[key] = (False, row[0], row[1] + 1)
                        con.execute("UPDATE seen SET last_ts = ?, times = ? WHERE key = ?", (now, row[1] + 1, key))
                    else:
                        keys[key] = (True, now, 1)
                        con.execute("INSERT INTO seen (key, kind, first_ts, last_ts, times) VALUES (?, ?, ?, ?, 1)",
                                    (key, kind, now, now))
                prev = con.execute("SELECT metrics_json FROM findings WHERE key = ? AND kind = ? ORDER BY ts DESC LIMIT 1",
                                   (key, kind)).fetchone()
                dl = delta(kind, json.loads(prev[0] or "{}"), g["metrics"]) if prev else None
                n_changed += bool(dl and dl["material"])
                con.execute("INSERT INTO findings (run_id, ts, kind, key, title, metrics_json, url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, now, kind, key, g["title"], json.dumps(g["metrics"], ensure_ascii=False), g["url"]))
                new, first, times = keys[key]
                for item in g["items"]:
                    item.update({"new": new, "first_seen": iso(first), "times_seen": times})
                    if dl and not new:
                        item["delta"] = dl["text"]
    finally:
        con.close()
    n_new = sum(1 for v in keys.values() if v[0])
    summary = {"db": DB_NAME, "keep_days": keep_days(), "tracked": len(keys), "new": n_new,
               "seen_before": len(keys) - n_new, "changed": n_changed, "previous_scan": iso(prev_run)}
    d["memory"] = summary
    return summary


# ---------------------------------------------------------------- outcome checks

OUTCOME_KINDS = {"hl_position": "hl", "meme_move": "meme", "meme_paper": "meme", "pumpfun_curve": "meme",
                 "pumpfun_graduated": "meme", "pm_fresh_wallet": "pm"}     # one-off transfers get no outcome


def pending(con: sqlite3.Connection, now: float | None = None) -> tuple[list[dict], list[dict]]:
    """(to check, missed): keys old enough for a horizon without an outcome yet. The flag is the key's
    earliest snapshot of an outcome kind; a check far past its window is recorded as missed."""
    now = now or time.time()
    kinds = list(OUTCOME_KINDS)
    rows = con.execute(f"SELECT key, kind, MIN(ts), metrics_json FROM findings WHERE kind IN ({','.join('?' * len(kinds))}) "
                       "GROUP BY key", kinds).fetchall()
    todo, missed = [], []
    for key, kind, ts, mj in rows:
        for hz, secs in HORIZONS.items():
            age = now - ts
            if age < secs or con.execute("SELECT 1 FROM outcomes WHERE key = ? AND horizon = ? AND ABS(flagged_ts - ?) < 1",
                                         (key, hz, ts)).fetchone():
                continue
            p = {"key": key, "kind": kind, "group": OUTCOME_KINDS[kind], "flagged_ts": ts, "horizon": hz,
                 "flag": json.loads(mj or "{}"), "after_h": round(age / 3600, 1)}
            (missed if age > MISSED_AFTER * secs else todo).append(p)
    todo.sort(key=lambda p: p["flagged_ts"])
    return todo, missed


def judge_hl(flag: dict, pos: dict | None, fills: list | None, ledger: list | None, addr: str, coin: str,
             side: str, since_ms: int) -> tuple[str, dict]:
    """A flagged Hyperliquid position now: open / liquidated / closed_loss / closed_profit / closed (P&L unknown).
    pos = the wallet's current position in that coin (lab.scanner.perps row) or None."""
    res = {"usd_then": flag.get("usd"), "upnl_then": flag.get("upnl"), "to_liq_then": flag.get("to_liq_pct"),
           "why": flag.get("why")}
    if pos and pos.get("side") == side:
        res.update(usd_now=round(pos["value"]), upnl_now=round(pos["upnl"]), to_liq_now=pos.get("to_liq_pct"))
        return "open", res
    addr = addr.lower()
    fs = [f for f in fills or [] if f.get("coin") == coin and f.get("time", 0) >= since_ms]
    liq_fill = any(isinstance(f.get("liquidation"), dict)
                   and (f["liquidation"].get("liquidatedUser") or addr).lower() == addr for f in fs)
    liq_ledger = any((u.get("delta") or {}).get("type") == "liquidation" and u.get("time", 0) >= since_ms
                     and (not (u["delta"].get("liquidatedPositions")) or
                          any(x.get("coin") == coin for x in u["delta"]["liquidatedPositions"]))
                     for u in ledger or [])
    res["flipped"] = bool(pos)
    if fs:
        res["closed_pnl"] = round(sum(float(f.get("closedPnl") or 0) for f in fs))
        res["closed_after_h"] = round((fs[-1]["time"] - since_ms) / 3.6e6, 1)
    if liq_fill or liq_ledger:
        return "liquidated", res
    if not fs:
        return "closed", res
    return ("closed_loss" if res["closed_pnl"] < 0 else "closed_profit"), res


def judge_meme(flag: dict, pair: dict | None) -> tuple[str, dict]:
    """Market cap and pool now vs when flagged; dead = either one down 90% or more (or no pool left)."""
    res = {"mcap_then": flag.get("mcap"), "liq_then": flag.get("liq"), "mcap_liq_then": flag.get("mcap_liq")}
    if not pair:
        return "dead", {**res, "no_pairs": True}
    cap = pair.get("marketCap") or pair.get("fdv")
    liq = (pair.get("liquidity") or {}).get("usd")
    res.update(mcap_now=round(cap) if cap is not None else None, liq_now=round(liq) if liq is not None else None)
    ch = [now / then - 1 for now, then in ((cap, flag.get("mcap")), (liq, flag.get("liq"))) if now is not None and then]
    if not ch:
        return "unknown", res
    if cap is not None and flag.get("mcap"):
        res["cap_change_pct"] = round((cap / flag["mcap"] - 1) * 100, 1)
    if min(ch) <= -0.9:
        return "dead", res
    return ("down" if ch[0] < 0 else "up"), res


def judge_pm(flag: dict, pnl_now: float | None) -> tuple[str, dict]:
    """Profile P&L now vs when flagged."""
    then = flag.get("total_pnl_usd")
    then = flag.get("realized_pnl_usd") if then is None else then
    res = {"pnl_then": then, "pnl_now": None if pnl_now is None else round(pnl_now)}
    if then is None or pnl_now is None:
        return "unknown", res
    res["change_usd"] = round(pnl_now - then)
    if then > 0 and pnl_now <= then / 2:
        return "gave_back_half", res
    return ("down" if pnl_now < then else "up"), res


def _fills_since(addr: str, since_ms: int, pages: int = 3) -> list:
    """Fills from since_ms forward (2000 per page); if that is not enough, add the latest 2000."""
    from lab.scanner.perps import hl
    out, start = [], since_ms
    for _ in range(pages):
        page = hl({"type": "userFillsByTime", "user": addr, "startTime": start, "aggregateByTime": True}) or []
        out += page
        if len(page) < 2000:
            return out
        start = page[-1]["time"] + 1
    tids = {f.get("tid") for f in out}
    latest = hl({"type": "userFills", "user": addr, "aggregateByTime": True}) or []
    return sorted(out + [f for f in latest if f.get("tid") not in tids], key=lambda f: f.get("time", 0))


def _check_hl(ps: list[dict]) -> list[tuple]:
    from lab.scanner.perps import _position_rows, hl
    addrs = sorted({p["key"].split(":")[1] for p in ps})
    states = net.pmap_dict(lambda a: hl({"type": "clearinghouseState", "user": a}), addrs, 6)
    need = {}
    for p in ps:
        _, a, coin, side = p["key"].split(":", 3)
        if a in states and not any(r["coin"] == coin and r["side"] == side for r in _position_rows(a, states[a])):
            need[a] = min(need.get(a, 1e18), int(p["flagged_ts"] * 1000))
    fills = net.pmap_dict(lambda a: _fills_since(a, need[a]), list(need), 4)
    ledgers = net.pmap_dict(lambda a: hl({"type": "userNonFundingLedgerUpdates", "user": a, "startTime": need[a]}),
                            list(need), 4)
    out = []
    for p in ps:
        _, a, coin, side = p["key"].split(":", 3)
        if a not in states or (a in need and a not in fills):
            continue                                 # source did not answer: try again next time
        rows = [r for r in _position_rows(a, states[a]) if r["coin"] == coin]
        out.append((p, *judge_hl(p["flag"], rows[0] if rows else None, fills.get(a), ledgers.get(a), a, coin, side,
                                 int(p["flagged_ts"] * 1000))))
    return out


def _check_meme(ps: list[dict], extra: dict) -> list[tuple]:
    from lab.scanner.memes import DS, _main_pair
    chains = {"sol": "solana"}
    by_chain: dict[str, list] = {}
    for p in ps:
        ch, _, tok = p["key"].partition(":")
        if ch != "pair" and tok:
            by_chain.setdefault(chains.get(ch, ch), []).append(tok)
    batches = [(ch, sorted(set(toks))[i:i + 30]) for ch, toks in by_chain.items() for i in range(0, len(set(toks)), 30)]
    got = net.pmap_list(lambda b: (b, net.getj(f"{DS}/tokens/v1/{b[0]}/" + ",".join(b[1]), 15)), batches, 4)
    pairs, answered = {}, set()
    for r in got:
        if not r:
            continue
        (ch, toks), rows = r
        answered.update((ch, t) for t in toks)
        for q in rows or []:
            pairs.setdefault((ch, q["baseToken"]["address"]), []).append(q)
    out = []
    for p in ps:
        ch, _, tok = p["key"].partition(":")
        ch = chains.get(ch, ch)
        if (ch, tok) not in answered:
            continue
        v, res = judge_meme(p["flag"], _main_pair(pairs[(ch, tok)]) if pairs.get((ch, tok)) else None)
        out.append((p, v, {**res, **extra.get(p["key"], {})}))
    return out


def _check_pm(ps: list[dict]) -> list[tuple]:
    from lab.scanner.polymarket import _pnl
    wallets = sorted({p["key"].split(":")[1] for p in ps})
    curves = net.pmap_dict(lambda w: _pnl(w, "all"), wallets, 6)
    out = []
    for p in ps:
        w = p["key"].split(":")[1]
        if w in curves:
            curve = curves[w]
            out.append((p, *judge_pm(p["flag"], curve[-1]["p"] if curve else None)))
    return out


def _meme_context(con: sqlite3.Connection, keys: list[str]) -> dict:
    """What else was flagged about each coin (for report groups): kinds and the largest same-balance cluster."""
    out = {}
    for k in keys:
        kinds, same = set(), 0
        for kind, mj in con.execute("SELECT kind, metrics_json FROM findings WHERE key = ?", (k,)):
            kinds.add(kind)
            same = max(same, (json.loads(mj or "{}").get("same_n") or 0))
        out[k] = {"kinds": sorted(kinds), "same_n": same or None}
    return out


def check_outcomes(path: Path | str | None = None, max_checks: int = DEFAULT_MAX_CHECKS,
                   now: float | None = None) -> dict:
    """Check pending findings against public data (respects the lab/net.py time budget)."""
    now = now or time.time()
    con = connect(path, now)
    try:
        todo, missed = pending(con, now)
        with con:
            for p in missed:
                _store(con, p, "missed", {"after_h": p["after_h"]}, now)
        batch = todo[:max_checks]
        extra = _meme_context(con, [p["key"] for p in batch if p["group"] == "meme"])
        jobs = {"hl": lambda ps: _check_hl(ps), "meme": lambda ps: _check_meme(ps, extra), "pm": lambda ps: _check_pm(ps)}
        results: list[tuple] = []
        with ThreadPoolExecutor(3) as ex:
            futs = [ex.submit(fn, [p for p in batch if p["group"] == g]) for g, fn in jobs.items()
                    if any(p["group"] == g for p in batch)]
            for f in futs:
                try:
                    results += f.result()
                except Exception:
                    pass                          # one source down: its findings stay pending
        with con:
            for p, verdict, res in results:
                _store(con, p, verdict, {**res, "after_h": p["after_h"]}, now)
    finally:
        con.close()
    return {"checked": len(results), "not_answered": len(batch) - len(results), "left": len(todo) - len(results),
            "missed": len(missed), "lines": [f"{p['horizon']:>3} {p['key'][:60]}: {v}" for p, v, _ in results]}


def _store(con: sqlite3.Connection, p: dict, verdict: str, res: dict, now: float) -> None:
    con.execute("INSERT OR REPLACE INTO outcomes (key, kind, flagged_ts, horizon, checked_ts, result_json, verdict) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (p["key"], p["kind"], p["flagged_ts"], p["horizon"], now, json.dumps(res, ensure_ascii=False), verdict))


# ---------------------------------------------------------------- report

def share(k: int, n: int) -> str:
    """'17 of 40 (42%, n=40)'; below 5 the sample is called tiny and no percentage is printed."""
    if n == 0:
        return "nothing checked yet"
    if n < TINY:
        return f"{k} of {n} (n={n}, tiny sample: not a rate)"
    return f"{k} of {n} ({k / n:.0%}, n={n})"


def report_text(path: Path | str | None = None) -> str:
    con = connect(path)
    try:
        runs, first = con.execute("SELECT COUNT(*), MIN(ts) FROM runs").fetchone()
        n_find, n_keys = con.execute("SELECT COUNT(*), COUNT(DISTINCT key) FROM findings").fetchone()
        todo, _ = pending(con)
        rows = [(k, kind, hz, v, json.loads(r or "{}"))
                for k, kind, hz, v, r in con.execute("SELECT key, kind, horizon, verdict, result_json FROM outcomes "
                                                     "WHERE verdict NOT IN ('missed', 'unknown')")]
    finally:
        con.close()
    out = [f"Luthor Lab memory: {runs} scan(s) since {iso(first) or '-'}, {n_find} findings, {n_keys} distinct; "
           f"{len(todo)} check(s) pending.",
           "Each finding is checked about 1 day and about 7 days after it was first flagged, with the same public "
           f"data. n = findings checked; under {TINY} is a tiny sample, not a rate."]
    if not runs:
        return "\n".join(out[:1] + ["No scans remembered yet. Run `python -m lab.scan`."])

    def group(g: str) -> dict:
        return {hz: [r for r in rows if OUTCOME_KINDS.get(r[1]) == g and r[2] == hz] for hz in HORIZONS}

    out.append("\nHyperliquid whale positions (liquidated or closed at a loss, from public fills and ledger)")
    for hz, rs in group("hl").items():
        bad = lambda xs: sum(1 for x in xs if x[3] in ("liquidated", "closed_loss"))
        out.append(f"  checked {HORIZON_TXT[hz]} later: {len(rs)} position(s); liquidated or closed at a loss: "
                   f"{share(bad(rs), len(rs))}")
        if rs:
            c = {v: sum(1 for x in rs if x[3] == v) for v in ("liquidated", "closed_loss", "closed_profit", "closed", "open")}
            out.append(f"    liquidated {c['liquidated']}, closed at a loss {c['closed_loss']}, closed in profit "
                       f"{c['closed_profit']}, closed (P&L not found) {c['closed']}, still open {c['open']}")
        for lim in (5, 10, 25):
            sub = [x for x in rs if (x[4].get("to_liq_then") is not None and x[4]["to_liq_then"] <= lim)]
            if sub:
                out.append(f"    flagged within {lim}% of liquidation: {len(sub)}; liquidated or closed at a loss: "
                           f"{share(bad(sub), len(sub))}")

    out.append("\nMemecoins (DexScreener market cap and pool now vs when flagged; dead = either down 90%+)")
    for hz, rs in group("meme").items():
        dead = lambda xs: sum(1 for x in xs if x[3] == "dead")
        out.append(f"  checked {HORIZON_TXT[hz]} later: {len(rs)} coin(s); dead: {share(dead(rs), len(rs))}; "
                   f"lower cap than at the flag: {share(sum(1 for x in rs if x[3] in ('dead', 'down')), len(rs))}")
        for label, cond in (("flagged with market cap >= 100x the pool", lambda r: (r.get("mcap_liq_then") or 0) >= 100),
                            ("flagged with 5+ top holders at an identical balance", lambda r: (r.get("same_n") or 0) >= 5)):
            sub = [x for x in rs if cond(x[4])]
            if sub:
                out.append(f"    {label}: {len(sub)}; dead: {share(dead(sub), len(sub))}")

    out.append("\nFresh Polymarket wallets with big wins (profile P&L now vs when flagged)")
    for hz, rs in group("pm").items():
        out.append(f"  checked {HORIZON_TXT[hz]} later: {len(rs)} wallet(s); P&L lower than at the flag: "
                   f"{share(sum(1 for x in rs if x[3] in ('down', 'gave_back_half')), len(rs))}; gave back half or more: "
                   f"{share(sum(1 for x in rs if x[3] == 'gave_back_half'), len(rs))}")
    out.append("\nOne-off events (stablecoin transfers, single bets) get no outcome check.")
    return "\n".join(out)


# ---------------------------------------------------------------- offline self-test

def self_test() -> tuple[bool, str]:
    """Offline: remember two synthetic scans in a temp dir, check flags, delta, retention, verdicts, report."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / DB_NAME
            t0 = time.time() - 30 * 3600

            def digest(size: float, cap: float) -> dict:
                return {"took_s": 1.0,
                        "perps": {"hyperliquid": {"positions": [{"wallet": "0xAbC", "coin": "BTC", "side": "long", "usd": size,
                                                                 "upnl": -1e6, "to_liq_pct": 4.0, "summary": "Long BTC"}]},
                                  "eth_transfers": {"items": [{"usd": 5e7, "tx": "etherscan.io/tx/0xFF", "summary": "t"}]}},
                        "memes": {"gainers": {"g24": [{"sym": "X", "chain": "solana", "token": "Mint1", "mcap": cap,
                                                       "liq": 1e5, "summary": "x"}], "g1": []}},
                        "signals": {"fresh_wallets": [{"wallet": "0xW", "realized_pnl_usd": 9e4,
                                                       "profile": {"total_pnl_usd": 8e4}, "summary": "w"}]}}
            d1, d2 = digest(5e7, 1e6), digest(7e7, 1.5e6)
            m1 = remember(d1, db, t0)
            m2 = remember(d2, db, t0 + 3600)
            pos1, pos2 = d1["perps"]["hyperliquid"]["positions"][0], d2["perps"]["hyperliquid"]["positions"][0]
            assert m1["new"] == 4 and m2["new"] == 0 and m2["seen_before"] == 4, (m1, m2)
            assert pos1["new"] is True and pos2["new"] is False and pos2["times_seen"] == 2, pos2
            assert "size +$20.0M" in pos2.get("delta", ""), pos2
            con = connect(db, t0 + 25 * 3600)
            todo, missed = pending(con, t0 + 25 * 3600)
            assert {p["key"] for p in todo} == {"hl:0xabc:BTC:long", "sol:Mint1", "pm:0xw"} and not missed, todo
            with con:
                con.execute("INSERT INTO runs (ts) VALUES (?)", (t0 - 40 * 86400,))
            assert cleanup(con, t0 + 25 * 3600) == 1
            liq = [{"coin": "BTC", "time": int(t0 * 1000) + 5, "closedPnl": "-2000000",
                    "liquidation": {"liquidatedUser": "0xabc", "markPx": "1", "method": "market"}}]
            verdicts = [judge_hl({}, None, liq, [], "0xAbC", "BTC", "long", int(t0 * 1000))[0],
                        judge_meme({"mcap": 1e6, "liq": 1e5}, {"marketCap": 5e4, "liquidity": {"usd": 2e4}})[0],
                        judge_pm({"total_pnl_usd": 8e4}, 3e4)[0],
                        judge_hl({}, {"side": "long", "value": 1, "upnl": 0}, None, None, "0xabc", "BTC", "long", 0)[0]]
            assert verdicts == ["liquidated", "dead", "gave_back_half", "open"], verdicts
            with con:
                for p, v in zip(sorted(todo, key=lambda p: p["group"]), ("liquidated", "dead", "gave_back_half")):
                    _store(con, p, v, {"to_liq_then": 4.0}, t0 + 25 * 3600)
            con.close()
            rep = report_text(db)
            assert "tiny sample" in rep and "within 5% of liquidation: 1" in rep, rep
        return True, "remember/new/seen/delta/retention/verdicts/report ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.memory", description="What the lab remembers between runs")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("report", help="hit-rates of past findings with n")
    o = sub.add_parser("outcomes", help="check findings flagged >= 24 h / 7 d ago against public data")
    o.add_argument("--max", type=int, default=DEFAULT_MAX_CHECKS, help="most checks in one run (default 40)")
    o.add_argument("--budget", type=float, default=90, help="time budget in seconds (default 90)")
    sub.add_parser("self-test", help="offline test in a temp dir")
    ap.add_argument("--db", type=Path, default=None, help="memory file (default <data dir>/lab.db)")
    a = ap.parse_args(argv)
    if a.cmd == "self-test":
        ok, msg = self_test()
        print("memory self-test:", "OK" if ok else "FAILED", msg)
        return 0 if ok else 1
    if a.cmd == "outcomes":
        net.set_deadline(time.time() + a.budget)
        try:
            r = check_outcomes(a.db, a.max)
        finally:
            net.set_deadline(None)
        print(f"Checked {r['checked']} finding(s); {r['not_answered']} got no answer (retry next time); "
              f"{r['left']} still pending; {r['missed']} past their window (recorded as missed).")
        for line in r["lines"]:
            print("  " + line)
        print("\nReport: python -m lab.memory report")
        return 0
    print(report_text(a.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
