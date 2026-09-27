"""Polymarket signals plus stocks and AI trends.

Sections of collect():
  fresh_wallets     - Polymarket wallets younger than 14 days with $50K+ realized profit;
  timed_bets        - $25K+ bets in the last 72 h at <=35c, after which the outcome hit 80c+ within
                      12 h or the market resolved in their favor (sports and "up or down" excluded);
  losers            - biggest daily and weekly losses (profile numbers, not the leaderboard);
  celebrity_markets - celebrity markets with the highest 24h volume (excluding decided ones: <=1% / >=99%);
  stocks            - daily moves of +-5% or more (Yahoo chart API);
  ai                - trending Hugging Face models and AI stories on Hacker News in the last 24 h.

Free public APIs without keys, read-only. Every number comes from an API response.
Wallet numbers come from polymarket.profile: exactly what a reader sees on the profile page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

from lab.net import getj_retry as _j, left, pmap_list as _pmap, safe, usd as _m
from lab.scanner.polymarket import CLOB, DATA_API, GAMMA, SPORT, profile, wallet_url

FRESH_DAYS, FRESH_MIN_REALIZED = 14, 50_000
BET_MIN_USD, BET_MAX_PRICE, BET_WINDOW_H, JUMP_H, JUMP_TO = 25_000, 0.35, 72, 12, 0.80
STOCK_MOVE_PCT = 5.0

# Sports and "up or down" markets are decided live or by minute-level prices: precise timing there is not a finding.
SPORT_SLUG = re.compile(r"^(nfl|nba|mlb|nhl|cfb|cbb|ncaa|wnba|epl|ucl|uel|uecl|mls|unl|lal|bun|sea|fl1|ere|por|tur|"
                        r"bra|arg|mex|jpn|kor|kbo|npb|atp|wta|ufc|box|pga|f1|nascar|cs2|lol|dota2?|val|ow|r6|cod|"
                        r"cricket|ipl|t20|odi|test)-", re.I)
UPDOWN = re.compile(r"up or down|up-or-down|updown", re.I)
AI_RE = re.compile(r"\b(ai|agi|agents?|agentic|llms?|gpt[\w.-]*|chatgpt|openai|anthropic|claude|gemini|deepmind|mistral|"
                   r"llama|qwen|deepseek|grok|xai|copilot|codex|hugging ?face|ollama|nvidia|neural|transformers?|"
                   r"diffusion|inference)\b", re.I)
CELEBS = [(n, re.compile(p, re.I)) for n, p in (
    ("Trump", r"\btrump\b"), ("Elon Musk", r"\belon\b|\bmusk\b"), ("Taylor Swift", r"taylor swift"),
    ("Travis Kelce", r"\bkelce\b"), ("Kanye West", r"\bkanye\b"), ("MrBeast", r"\bmr\.? ?beast\b"),
    ("Drake", r"\bdrake\b"), ("Kardashian", r"kardashian"), ("Kylie Jenner", r"kylie jenner"),
    ("Beyonce", r"beyonc"), ("Rihanna", r"rihanna"), ("Justin Bieber", r"bieber"), ("Zuckerberg", r"zuckerberg"),
    ("Bezos", r"\bbezos\b"), ("Sam Altman", r"\baltman\b"), ("Diddy", r"\bdiddy\b|sean combs"),
    ("Jake Paul", r"jake paul"), ("Logan Paul", r"logan paul"), ("Andrew Tate", r"andrew tate"),
    ("Joe Rogan", r"\brogan\b"), ("Bad Bunny", r"bad bunny"), ("Sabrina Carpenter", r"sabrina carpenter"),
    ("Lady Gaga", r"lady gaga"), ("Selena Gomez", r"selena gomez"), ("Nicki Minaj", r"nicki minaj"),
    ("Cardi B", r"cardi b\b"), ("Snoop Dogg", r"snoop dogg"), ("Oprah", r"\boprah\b"))]
TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX", "ORCL",                     # megacaps
           "NVDA", "AMD", "AVGO", "TSM", "SMCI", "PLTR", "ARM", "MU", "INTC", "QCOM", "ASML",   # AI and chips
           "MRVL", "DELL", "CRWV", "NBIS", "IONQ", "RGTI", "SOUN", "ANET", "VRT",
           "COIN", "MSTR", "HOOD", "MARA", "RIOT", "CRCL", "CLSK", "HUT", "BMNR", "GLXY",       # crypto stocks
           "IREN", "CIFR"]
REL = {"quantized": "quantized version of", "finetune": "fine-tune of", "adapter": "adapter for", "merge": "merge of"}

_cache: dict = {}
_locks: dict = defaultdict(threading.Lock)
_stats: dict = {}


# ---------- helpers ----------

def _once(key: str, fn):
    """Shared fetches (trade feed, gamma events) load once per collect()."""
    with _locks[key]:
        if key not in _cache:
            _cache[key] = fn()
        return _cache[key]


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _c(p: float | None) -> float | None:
    """Share price 0..1 -> cents."""
    return None if p is None else round(p * 100, 1)


def _prices(m: dict) -> list[float]:
    try:
        return [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return []


def _is_sport_event(e: dict) -> bool:
    return bool(e.get("sport") or e.get("gameId") or SPORT.search(e.get("title") or "")
                or SPORT_SLUG.search(e.get("slug") or ""))


def _is_noise_market(title: str, slug: str, m: dict | None = None) -> bool:
    """Sports/esports or "up or down"."""
    return bool(SPORT.search(title) or SPORT.search(slug) or SPORT_SLUG.search(slug) or UPDOWN.search(title)
                or UPDOWN.search(slug) or (m and (m.get("sportsMarketType") or m.get("gameStartTime"))))


def _big_trades() -> list:
    """Trades of $25K+ (maker and taker): one page is ~5 days of feed."""
    return _once("big", lambda: _j(f"{DATA_API}/trades?limit=1000&filterType=CASH&filterAmount=25000&takerOnly=false"))


def _events() -> list:
    """Top 500 active gamma events by 24h volume (shared by timed_bets and celebrity_markets)."""
    def load():
        pages = _pmap(_j, [f"{GAMMA}/events?active=true&closed=false&order=volume24hr&ascending=false"
                           f"&limit=100&offset={o}" for o in range(0, 500, 100)], 5)
        seen, out = set(), []
        for p in pages:
            for e in p or []:
                if e.get("id") not in seen:
                    seen.add(e.get("id"))
                    out.append(e)
        return out
    return _once("events", load)


def _gamma_markets(conds: list[str]) -> dict:
    """conditionId -> gamma market, open and closed (gamma serves them via separate queries)."""
    urls = []
    for i in range(0, len(conds), 20):
        q = "&".join(f"condition_ids={c}" for c in conds[i:i + 20])
        urls += [f"{GAMMA}/markets?limit=100&{q}", f"{GAMMA}/markets?limit=100&closed=true&{q}"]
    out = {}
    for page in _pmap(_j, urls, 6):
        for m in page or []:
            out[m.get("conditionId")] = m
    return out


def _realized(wallet: str) -> tuple[float, bool]:
    """Net realizedPnl over closed positions: gains from the top, losses from the bottom (up to 4 pages each).

    Second value is True if there were more positions than read (the sum is then partial)."""
    total, partial = 0.0, False
    for direction, sign in (("DESC", 1), ("ASC", -1)):
        for page in range(4):
            rows = _j(f"{DATA_API}/closed-positions?user={wallet}&limit=50&offset={page * 50}"
                      f"&sortBy=REALIZEDPNL&sortDirection={direction}")
            vals = [r.get("realizedPnl") or 0 for r in rows]
            total += sum(v for v in vals if v * sign > 0)
            if len(rows) < 50 or any(v * sign <= 0 for v in vals):
                break
        else:
            partial = True
    return total, partial


def _created(wallet: str) -> str | None:
    return _j(f"{GAMMA}/public-profile?address={wallet}", 10).get("createdAt")


def _first_trade(wallet: str) -> dict | None:
    return (_j(f"{DATA_API}/activity?user={wallet}&limit=1&type=TRADE&sortBy=TIMESTAMP&sortDirection=ASC") or [None])[0]


def _born(created: str | None, first: dict | None) -> float | None:
    """Wallet age: profile createdAt can be later than the first trade (profile renamed later), take the earlier."""
    ts = [t for t in (_ts(created), (first or {}).get("timestamp")) if t]
    return min(ts) if ts else None


def _age_txt(days: float) -> str:
    return f"{int(days * 24)} h" if days < 1 else f"{int(days)} days"   # floor: 13.9 days is "13", not "14"


def _pnl_view(prof: dict, age_days: float | None) -> dict:
    """Profile numbers. For a wallet younger than the period the P&L curve does not start at zero,
    so "last minus first point" is wrong; use the all-time total instead."""
    out = {k: prof.get(k) for k in ("total_pnl_usd", "pnl_7d_usd", "pnl_24h_usd", "open_unrealized_usd")}
    if age_days is not None and age_days < 7:
        out["pnl_7d_usd"] = out["total_pnl_usd"]
    if age_days is not None and age_days < 1:
        out["pnl_24h_usd"] = out["total_pnl_usd"]
    return out


# ---------- fresh wallets with a big win ----------

@safe
def fresh_wallets():
    now = time.time()
    cands: dict[str, dict] = {}
    lb = [(p, o) for p in ("DAY", "WEEK") for o in (0, 50)]
    pages = _pmap(lambda x: _j(f"{DATA_API}/v1/leaderboard?timePeriod={x[0]}&orderBy=PNL&limit=50&offset={x[1]}"), lb)
    for (period, _), rows in zip(lb, pages):
        for r in rows or []:
            w = (r.get("proxyWallet") or "").lower()
            if w and w not in cands:
                cands[w] = {"name": r.get("userName"), "via": f"leaderboard {period.lower()} #{r.get('rank')}"}
    for t in _big_trades():
        w = (t.get("proxyWallet") or "").lower()
        if w and w not in cands and t.get("timestamp", 0) >= now - 86400:
            cands[w] = {"name": t.get("name") or None, "via": f"{_m(t['size'] * t['price'])} trade in 24h"}
    wallets = list(cands)
    created = dict(zip(wallets, _pmap(_created, wallets, 12)))
    young = [w for w in wallets if _ts(created[w]) and now - _ts(created[w]) < FRESH_DAYS * 86400]
    first = dict(zip(young, _pmap(_first_trade, young, 8)))
    born = {w: _born(created[w], first[w]) for w in young}
    fresh = [w for w in young if born[w] and now - born[w] < FRESH_DAYS * 86400]
    realized = dict(zip(fresh, _pmap(_realized, fresh, 6)))
    rich = [w for w in fresh if realized[w] and realized[w][0] >= FRESH_MIN_REALIZED]
    _stats["fresh_wallets"] = (f"wallets {len(wallets)}; profile younger than {FRESH_DAYS} days: {len(young)}; "
                               f"first trade too: {len(fresh)}; realized >= $50K: {len(rich)}")

    rows = []
    for w, prof in zip(rich, _pmap(lambda w: profile(w, cands[w]["name"]), rich, 6)):
        if not prof:
            continue
        age = (now - born[w]) / 86400
        f1 = first[w]
        win = prof.get("biggest_win_resolved") or prof.get("biggest_exit_7d_sold_before_resolution")
        row = {"name": cands[w]["name"], "wallet": w, "age_days": round(age, 1), "profile_created": (created[w] or "")[:10],
               "via": cands[w]["via"], "realized_pnl_usd": round(realized[w][0]),
               "realized_partial": realized[w][1] or None,
               "profile": _pnl_view(prof, age)}
        if win:
            row["biggest_win"] = {"market": win["market"], "side": win["side"], "entry_c": _c(win["avg_price"]),
                                  "invested_usd": win["stake_usd"], "received_usd": win["stake_usd"] + win["profit_usd"],
                                  "profit_usd": win["profit_usd"],
                                  "resolved": bool(prof.get("biggest_win_resolved"))}
        if f1:
            row["first_trade_at"] = _utc(f1["timestamp"])
            if (f1.get("usdcSize") or 0) >= 1000 or not win:
                row["first_bet"] = {"market": f1.get("title"), "side": f1.get("outcome"), "buy_or_sell": f1.get("side"),
                                    "entry_c": _c(f1.get("price")), "usd": round(f1.get("usdcSize") or 0)}
        row["url"] = prof["profile_url"]
        s = f"Wallet is {_age_txt(age)} old and has already realized +{_m(realized[w][0])}"
        if win:
            s += f"; biggest win +{_m(win['profit_usd'])} from an entry at {_c(win['avg_price']):g}c"
        unreal = prof.get("open_unrealized_usd") or 0
        if unreal <= -0.5 * realized[w][0]:
            s += f"; open positions are at {_m(unreal)} right now"
        row["summary"] = s
        rows.append({k: v for k, v in row.items() if v is not None})
    return sorted(rows, key=lambda r: -r["realized_pnl_usd"])[:4]


# ---------- precisely timed bets ----------

def _mid_trades() -> list:
    """Taker trades of $5K+: several feed pages (big bets are often split)."""
    pages = _pmap(_j, [f"{DATA_API}/trades?limit=1000&offset={o}&filterType=CASH&filterAmount=5000&takerOnly=true"
                       for o in (0, 1000, 2000, 3000)], 4)
    return [t for p in pages if p for t in p]


def _jump_market_trades() -> list:
    """Non-sports markets where an outcome gained 45+ pp in a week and trades at 80c+: read their $1K+ feed."""
    jumps = []
    for e in _events():
        if _is_sport_event(e):
            continue
        for m in e.get("markets") or []:
            px, wk = _prices(m), m.get("oneWeekPriceChange")
            if m.get("closed") or not px or wk is None:
                continue
            if (wk >= 0.45 and px[0] >= JUMP_TO) or (wk <= -0.45 and px[0] <= 1 - JUMP_TO):
                jumps.append((float(m.get("volume24hr") or 0), m["conditionId"]))
    conds = [c for _, c in sorted(jumps, reverse=True)[:15]]
    _stats["timed_bets_jump_markets"] = len(conds)
    pages = _pmap(lambda c: _j(f"{DATA_API}/trades?market={c}&limit=1000&filterType=CASH&filterAmount=1000"
                               f"&takerOnly=false"), conds, 6)
    return [t for p in pages if p for t in p]


@safe
def timed_bets():
    now = time.time()
    since = now - BET_WINDOW_H * 3600
    raw = list(_big_trades()) + _mid_trades() + _jump_market_trades()
    seen, agg = set(), defaultdict(list)
    for t in raw:
        key = (t.get("transactionHash"), t.get("proxyWallet"), t.get("asset"), t.get("size"), t.get("price"))
        if key in seen:
            continue
        seen.add(key)
        if t.get("side") == "BUY" and (t.get("price") or 1) <= BET_MAX_PRICE and t.get("timestamp", 0) >= since:
            agg[(t["proxyWallet"], t["asset"])].append(t)
    cands, noise = [], 0
    for (w, asset), fills in agg.items():
        cost = sum(f["size"] * f["price"] for f in fills)
        f0 = min(fills, key=lambda f: f["timestamp"])
        if cost >= BET_MIN_USD and _is_noise_market(f0.get("title") or "", f0.get("slug") or ""):
            noise += 1
        elif cost >= BET_MIN_USD:
            shares = sum(f["size"] for f in fills)
            cands.append({"wallet": w, "asset": asset, "cond": f0["conditionId"], "idx": f0.get("outcomeIndex"),
                          "cost": cost, "shares": shares, "avg": cost / shares, "t0": f0["timestamp"],
                          "fills": len(fills), "f0": f0})
    markets = _gamma_markets(sorted({c["cond"] for c in cands}))
    kept = [c for c in cands if not _is_noise_market("", "", markets.get(c["cond"]))]
    noise += len(cands) - len(kept)
    cands = kept
    _stats["timed_bets"] = (f"trades {len(seen)}; bets >= $25K at <=35c in {BET_WINDOW_H} h: {len(cands) + noise}, "
                            f"sports/up-down filtered out: {noise}; jump markets: {_stats.get('timed_bets_jump_markets')}")
    _stats.pop("timed_bets_jump_markets", None)

    def path(c):
        h = _j(f"{CLOB}/prices-history?market={c['asset']}&startTs={int(c['t0']) - 600}&fidelity=10").get("history") or []
        return [p for p in h if c["t0"] <= p["t"] <= c["t0"] + JUMP_H * 3600]

    rows = []
    for c, h in zip(cands, _pmap(path, cands, 8)):
        m = markets.get(c["cond"]) or {}
        px = _prices(m)
        now_p = px[c["idx"]] if c["idx"] is not None and c["idx"] < len(px) else None
        resolved_for = bool(m.get("closed") and now_p is not None and now_p >= 0.99)
        hit = next((p for p in (h or []) if p["p"] >= JUMP_TO), None)
        if not hit and not resolved_for:
            continue
        peak = max(h, key=lambda p: p["p"]) if h else None
        f0 = c["f0"]
        name = f0.get("name") or None
        row = {"market": f0.get("title"), "event": ((m.get("events") or [{}])[0].get("title") or None),
               "side": f0.get("outcome"), "who": name or f0.get("pseudonym"), "wallet": c["wallet"],
               "bet_at": _utc(c["t0"]), "fills": c["fills"], "stake_usd": round(c["cost"]), "entry_c": _c(c["avg"]),
               "hit_80c_after_h": round((hit["t"] - c["t0"]) / 3600, 1) if hit else None,
               "peak_12h_c": _c(peak["p"]) if peak else None,
               "resolved_in_their_favor": resolved_for or None,
               "resolved_at": ((m.get("closedTime") or "")[:16] or None) if resolved_for else None,
               "now_c": _c(now_p), "paper_profit_now_usd": round(c["shares"] * now_p - c["cost"]) if now_p is not None else None,
               "url": f"polymarket.com/event/{f0.get('eventSlug') or f0.get('slug')}",
               "wallet_url": wallet_url(c["wallet"], name)}
        if hit:
            s = (f"Bought {_m(c['cost'])} at {_c(c['avg']):g}c; {row['hit_80c_after_h']:g} h later the outcome "
                 f"traded at {_c(hit['p']):g}c")
        else:
            s = f"Bought {_m(c['cost'])} at {_c(c['avg']):g}c; the market resolved in their favor"
        if row["paper_profit_now_usd"] is not None:
            s += f", paper profit {_m(row['paper_profit_now_usd'])}"
        row["summary"] = s
        rows.append({k: v for k, v in row.items() if v is not None})
    return sorted(rows, key=lambda r: -(r.get("paper_profit_now_usd") or 0))[:4]


# ---------- biggest losers ----------

@safe
def losers():
    """The leaderboard does not sort by loss, so look for negatives among the top 150 by volume and check the profile."""
    def period(args):
        period_, key, label = args
        pages = _pmap(_j, [f"{DATA_API}/v1/leaderboard?timePeriod={period_}&orderBy=VOL&limit=50&offset={o}"
                           for o in (0, 50, 100)], 3)
        cands = sorted((r for p in pages for r in (p or []) if (r.get("pnl") or 0) < 0), key=lambda r: r["pnl"])[:6]
        since = time.time() - (86400 if period_ == "DAY" else 7 * 86400)

        def detail(r):
            w = r["proxyWallet"]
            closed = _j(f"{DATA_API}/closed-positions?user={w}&limit=50&sortBy=REALIZEDPNL&sortDirection=ASC")
            loss = next((p for p in closed if (p.get("timestamp") or 0) >= since and (p.get("realizedPnl") or 0) < 0), None)
            try:
                created = _created(w)
            except Exception:
                created = None
            born = _born(created, _first_trade(w))
            return profile(w, r.get("userName")), loss, (time.time() - born) / 86400 if born else None

        rows = []
        for r, d in zip(cands, _pmap(detail, cands, 6)):
            if not d:
                continue
            prof, loss, age = d
            view = _pnl_view(prof, age)
            pnl = view[key]
            if pnl is None or pnl >= 0:
                continue
            row = {"name": r.get("userName"), "wallet": r["proxyWallet"],
                   "age_days": round(age, 1) if age is not None else None,
                   "profile": {"pnl_period_usd": pnl, "total_pnl_usd": view["total_pnl_usd"],
                               "open_unrealized_usd": view["open_unrealized_usd"]},
                   "url": prof["profile_url"]}
            s = f"{_m(pnl)} over the {label} per the profile"
            if loss:
                inv = (loss.get("totalBought") or 0) * (loss.get("avgPrice") or 0)
                row["biggest_loss_in_period"] = {"market": loss.get("title"), "side": loss.get("outcome"),
                                                 "entry_c": _c(loss.get("avgPrice")), "invested_usd": round(inv),
                                                 "lost_usd": round(loss["realizedPnl"])}
                s += f"; most expensive miss {_m(loss['realizedPnl'])} on \"{loss.get('title')}\""
            if age is not None and age < FRESH_DAYS:
                s += f" (wallet is {_age_txt(age)} old)"
            row["summary"] = s
            rows.append({k: v for k, v in row.items() if v is not None})
        return sorted(rows, key=lambda x: x["profile"]["pnl_period_usd"])[:3]

    day, week = _pmap(period, [("DAY", "pnl_24h_usd", "last 24h"), ("WEEK", "pnl_7d_usd", "last 7 days")], 2)
    return {"day": day or [], "week": week or []}


# ---------- celebrity markets ----------

def _celeb(text: str) -> str | None:
    return next((n for n, rx in CELEBS if rx.search(text)), None)


@safe
def celebrity_markets():
    rows = []
    for e in _events():
        if _is_sport_event(e):
            continue
        # markets at 0-1% or 99-100% are effectively decided
        live = [m for m in e.get("markets") or [] if not m.get("closed") and _prices(m) and 0.01 < _prices(m)[0] < 0.99]
        who = _celeb(e.get("title") or "")
        if who:
            pool = live
        else:
            pool = [m for m in live if _celeb(m.get("question") or "")]
            who = _celeb(pool[0]["question"]) if pool else None
        if not pool:
            continue
        # in a "who/how many" event show the leader; otherwise the market about the celebrity
        by_title = bool(_celeb(e.get("title") or ""))
        m = (max(pool, key=lambda x: _prices(x)[0]) if by_title and len(pool) > 1
             else max(pool, key=lambda x: float(x.get("volume24hr") or 0)))
        if not by_title and m.get("groupItemTitle"):
            who = m["groupItemTitle"]
        vol = float((e if by_title else m).get("volume24hr") or 0)
        yes, chg = _prices(m)[0], m.get("oneDayPriceChange")
        label = m.get("groupItemTitle") if len(live) > 1 and m.get("groupItemTitle") else "Yes"
        row = {"who": who, "event": e.get("title"), "question": m.get("question"), "yes_pct": round(yes * 100, 1),
               "chg_24h_pp": round(chg * 100, 1) if chg is not None else None, "vol24_usd": round(vol),
               "url": f"polymarket.com/event/{e.get('slug')}"}
        pct = f"{yes * 100:.1f}" if yes < 0.01 else f"{yes * 100:.0f}"
        s = f"\"{e.get('title')}\": {_m(vol)} traded in 24h; \"{label}\" at {pct}%"
        if chg:
            s += f" ({chg * 100:+.0f} pp in 24h)"
        row["summary"] = s
        rows.append({k: v for k, v in row.items() if v is not None})
    rows.sort(key=lambda r: -r["vol24_usd"])
    _stats["celebrity_markets"] = f"events {len(_events())}, celebrity: {len(rows)}"
    out, series, per = [], set(), defaultdict(int)
    for r in rows:  # the same series over different date windows counts once
        key = re.sub(r"\d+|january|february|march|april|may|june|july|august|september|october|november|december|"
                     r"[^a-z]", "", r["event"].lower())
        if key in series or per[r["who"]] >= 2:
            continue
        series.add(key)
        per[r["who"]] += 1
        out.append(r)
    return out[:5]


# ---------- stocks ----------

@safe
def stocks():
    """Daily move from Yahoo closes; volume relative to the mean of the previous ~20 sessions."""
    def one(t):
        r = _j(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=1mo&interval=1d")["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        bars = [(ts, c, v) for ts, c, v in zip(r["timestamp"], q["close"], q["volume"]) if c is not None]
        if len(bars) < 3:
            return None
        (ts, close, vol), prev = bars[-1], bars[-2][1]
        base = [v for _, _, v in bars[:-1] if v]
        return {"ticker": t, "name": r["meta"].get("shortName"), "chg_pct": round((close / prev - 1) * 100, 2),
                "price": round(close, 2), "rel_volume": round(vol / (sum(base) / len(base)), 1) if base and vol else None,
                "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                "url": f"finance.yahoo.com/quote/{t}"}

    got = [x for x in _pmap(one, TICKERS, 10) if x]
    _stats["stocks"] = f"tickers {len(TICKERS)}, answered {len(got)}, session {got[0]['date'] if got else '?'}"
    rows = sorted((x for x in got if abs(x["chg_pct"]) >= STOCK_MOVE_PCT), key=lambda x: -abs(x["chg_pct"]))
    for x in rows:
        x["summary"] = (f"{x['ticker']} {x['chg_pct']:+.1f}% on the day"
                        + (f" on {x['rel_volume']:g}x average volume" if x.get("rel_volume") else ""))
    return [{k: v for k, v in x.items() if v is not None} for x in rows[:6]]


# ---------- AI releases ----------

def _hf() -> list:
    d = _j("https://huggingface.co/api/models?sort=trendingScore&limit=15&expand[]=safetensors&expand[]=pipeline_tag"
           "&expand[]=likes&expand[]=trendingScore&expand[]=createdAt&expand[]=downloads&expand[]=cardData")
    rows, bases = [], set()
    for m in d:
        card = m.get("cardData") or {}
        base = card.get("base_model")
        base = base[0] if isinstance(base, list) and base else base
        if base and base in bases | {r["name"] for r in rows}:
            continue  # a quant/repack of something already listed
        params = (m.get("safetensors") or {}).get("total")
        what = [(m.get("pipeline_tag") or "model").replace("-", " ")]
        if params:
            what.append(f"{params / 1e9:.1f}B params" if params >= 1e9 else f"{params / 1e6:.0f}M params")
        if base and card.get("base_model_relation") in REL:
            what.append(f"{REL[card['base_model_relation']]} {base}")
        age = (time.time() - _ts(m.get("createdAt"))) / 86400 if _ts(m.get("createdAt")) else None
        rows.append({"name": m["id"], "what": ", ".join(what), "likes": m.get("likes"), "downloads": m.get("downloads"),
                     "age_days": round(age) if age is not None else None, "url": f"huggingface.co/{m['id']}",
                     "summary": (f"{m.get('likes')} likes in {age:.0f} days, trending on Hugging Face"
                                 if age is not None else f"{m.get('likes')} likes, trending on Hugging Face")})
        if base:
            bases.add(base)
        if len(rows) >= 5:
            break
    return [{k: v for k, v in r.items() if v is not None} for r in rows]


def _hn() -> list:
    """Hacker News stories from the last 24 h by points (Algolia), filtered to AI topics."""
    now = time.time()
    hits = _j(f"https://hn.algolia.com/api/v1/search?tags=story&numericFilters=created_at_i>{int(now - 86400)},"
              f"points>20&hitsPerPage=500")["hits"]
    rows = []
    for h in sorted(hits, key=lambda h: -(h.get("points") or 0)):
        if not AI_RE.search(h.get("title") or ""):
            continue
        age_h = (now - h["created_at_i"]) / 3600
        rows.append({"title": h["title"], "points": h["points"], "comments": h.get("num_comments"),
                     "url": h.get("url") or f"news.ycombinator.com/item?id={h['objectID']}",
                     "hn": f"news.ycombinator.com/item?id={h['objectID']}",
                     "summary": f"{h['points']} points and {h.get('num_comments') or 0} comments on Hacker News in {age_h:.0f} h"})
        if len(rows) >= 5:
            break
    return rows


@safe
def ai_releases():
    hf, hn = _pmap(lambda f: f(), [_hf, _hn], 2)
    return {"hf_trending": hf or [], "hn_24h": hn or []}


# ---------- assembly ----------

SECTIONS = {"fresh_wallets": fresh_wallets, "timed_bets": timed_bets, "losers": losers,
            "celebrity_markets": celebrity_markets, "stocks": stocks, "ai": ai_releases}


def collect(only: set[str] | None = None) -> dict:
    t0 = time.time()
    _cache.clear()
    _stats.clear()
    sections = {n: fn for n, fn in SECTIONS.items() if not only or n in only}
    ex = ThreadPoolExecutor(max(1, len(sections)))
    futs = {n: ex.submit(fn) for n, fn in sections.items()}
    wait(list(futs.values()), timeout=max(1.0, min(left(), 120)))
    ex.shutdown(wait=False, cancel_futures=True)
    out = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "rule": "all numbers from API responses; wallet numbers as on the Polymarket profile; empty = nothing found"}
    for n, f in futs.items():
        out[n] = f.result() if f.done() else {"error": "did not finish within the time budget"}
    out["checked"] = dict(_stats)
    out["took_s"] = round(time.time() - t0, 1)
    return out
