"""Memecoins: what is pumping, how real the market cap is, and who holds the supply.

Free public APIs without keys, read-only:
- DexScreener (api.dexscreener.com): token boosts/profiles/community takeovers, search, and
  tokens/v1/<chain>/<up to 30 addresses>. There is no free "top gainers" list, so candidates are
  gathered from boosts, profiles, takeovers, search, GeckoTerminal trending pools and pump.fun;
  every number about a coin comes from DexScreener.
- pump.fun frontend-api-v3 /coins: fresh launches, bonding curve, graduated coins.
- Solana RPC (api.mainnet-beta.solana.com): token supply and mint authorities. The free tier
  disables getTokenLargestAccounts, so holders fall back to a public keyless gateway, then RugCheck.
- GeckoTerminal: trending pools (candidates only) and holder distribution for Base.

collect() -> dict.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from lab.net import UA, getj, left, safe, usd

DS = "https://api.dexscreener.com"
PF = "https://frontend-api-v3.pump.fun"
GT = "https://api.geckoterminal.com/api/v2"
RPC_MAIN = "https://api.mainnet-beta.solana.com"
RPC_FALLBACK = "https://solana-mainnet.gateway.tatum.io"   # public keyless gateway, rate limited
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
CHAINS = ("solana", "base")
MIN_LIQ = 20_000                   # below this a % move means nothing: a few hundred dollars move the pool
MIN_VOL24, MIN_VOL1 = 50_000, 5_000
MIN_TX24, MIN_TX1 = 100, 50        # fewer trades means a "pump" made by a handful of trades on an empty pair
PAPER_RATIO = 100                  # market cap 100x+ the pool is "paper", reported separately
# not memes: base and quote assets that also appear in trending pools
NOT_MEME = {"SOL", "WSOL", "USDC", "USDT", "ETH", "WETH", "CBBTC", "WBTC", "BTC", "JUP", "JITOSOL", "MSOL",
            "USDS", "DAI", "EURC", "CBETH", "WSTETH", "AERO", "VIRTUAL", "USD1", "PYUSD", "BNB"}
# AMM vault authorities: such a "holder" is a liquidity pool, not a person
AMM_AUTH = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium AMM v4
            "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",   # Raydium CPMM
            "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh"}    # Raydium LaunchLab
PF_CURVE_TOKENS = 793_100_000 * 10**6   # tokens on a standard pump.fun curve (30 SOL virtual reserve)

_status: dict[str, str] = {}
_fallback_blocked = False


def rpc(url: str, method: str, params: list, timeout: int = 10):
    t = min(timeout, left())
    if t < 1:
        raise TimeoutError("scan time budget exhausted")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=t) as r:
        d = json.loads(r.read())
    if d.get("error"):
        raise RuntimeError(f"{method}: {str(d['error'])[:100]}")
    return d["result"]


# ---------------------------------------------------------------- formatting

def _num(x, nd=0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if nd else round(v)


def _sig(x, n=4):
    """4 significant digits: a price like 0.00000123 must not become 0."""
    try:
        return float(f"{float(x):.{n}g}")
    except (TypeError, ValueError):
        return None


def _pct(x):
    return None if x is None else (_sig(x, 2) if abs(x) < 1 else round(x, 1))


def _int(v) -> str:
    return f"{int(v):,}"


def _x(x) -> str:
    return _int(x) if x >= 100 else f"{x:g}"


def _age_txt(h) -> str:
    return "<1 min" if h * 60 < 1 else f"{h * 60:.0f} min" if h < 1 else f"{h:.0f} h"


def _age_h(ms) -> float | None:
    return round((time.time() * 1000 - ms) / 3.6e6, 1) if ms else None


# ---------------------------------------------------------------- pump.fun

@safe
def pumpfun_raw() -> dict:
    """Raw pump.fun lists: fresh launches, bonding curve, graduated, largest caps."""
    out, errs = {}, []
    for key, q in (("new", "sort=created_timestamp&limit=50"),
                   ("grad_new", "sort=created_timestamp&limit=50&complete=true"),
                   ("grad_active", "sort=last_trade_timestamp&limit=50&complete=true"),
                   ("curve_active", "sort=last_trade_timestamp&limit=50&complete=false"),
                   ("mcap_top", "sort=market_cap&limit=50")):
        try:
            out[key] = [c for c in getj(f"{PF}/coins?{q}&order=DESC&offset=0&includeNsfw=false", 12)
                        if not c.get("is_banned")]
        except Exception as e:
            out[key] = []
            errs.append(f"{key}: {type(e).__name__}")
    ok = sum(1 for v in out.values() if v)
    _status["pumpfun"] = (f"frontend-api-v3 /coins ok ({ok}/5)" if ok else "unavailable") + (f"; errors {errs}" if errs else "")
    return out


def _curve_progress(c: dict):
    """Share of curve tokens already bought, %. Only for the standard curve (30 SOL virtual reserve)."""
    vs, rs, rt = c.get("virtual_sol_reserves"), c.get("real_sol_reserves"), c.get("real_token_reserves")
    if None in (vs, rs, rt) or abs(vs - rs - 30 * 10**9) > 10**6:
        return None
    p = 100 - rt / PF_CURVE_TOKENS * 100
    return round(p, 1) if 0 <= p <= 100 else None


def pumpfun_view(pf: dict) -> dict:
    """Launch rate, who is closest to graduating to a DEX, and what happened to the freshly graduated."""
    out = {}
    ts = sorted(c["created_timestamp"] for c in pf.get("new") or [] if c.get("created_timestamp"))
    if len(ts) >= 10 and ts[-1] > ts[0]:
        span = (ts[-1] - ts[0]) / 60000
        per_h = round(len(ts) / span * 60)
        out["launches"] = {"last_n": len(ts), "span_min": round(span, 1), "per_hour": per_h,
                           "summary": f"a new pump.fun token every {span * 60 / len(ts):.0f} s, ~{_int(per_h)} per hour"}

    def card(c: dict) -> dict:
        d = {"sym": c.get("symbol"), "mcap": _num(c.get("usd_market_cap")), "ath": _num(c.get("ath_market_cap")),
             "age_h": _age_h(c.get("created_timestamp")), "url": f"pump.fun/coin/{c['mint']}"}
        if (c.get("name") or "").strip().lower() != (c.get("symbol") or "").strip().lower():
            d["name"] = (c.get("name") or "")[:32]
        return d

    curve = {c["mint"]: c for c in (pf.get("curve_active") or []) + (pf.get("mcap_top") or [])
             if not c.get("complete") and not c.get("nsfw") and (_age_h(c.get("created_timestamp")) or 99) < 24}
    out["near_graduation"] = []
    for c in sorted(curve.values(), key=lambda c: -(c.get("usd_market_cap") or 0))[:2]:
        d, prog = card(c), _curve_progress(c)
        d["curve_pct"] = prog
        d["summary"] = (f"bonding curve {prog:g}% sold (at 100% the coin moves to a DEX); "
                        if prog is not None else "") + f"cap {usd(d['mcap'])}, coin age {_age_txt(d['age_h'])}"
        out["near_graduation"].append(d)

    grad = [c for c in pf.get("grad_new") or [] if c.get("ath_market_cap")]
    if grad:
        n80 = sum(1 for c in grad if (c.get("usd_market_cap") or 0) <= 0.2 * c["ath_market_cap"])
        oldest = max(_age_h(c.get("created_timestamp")) or 0 for c in grad)
        g = {"n": len(grad), "created_within_h": oldest, "down80_from_ath": n80,
             "summary": f"of the {len(grad)} most recent coins that reached a DEX (all created within "
                        f"{_age_txt(oldest)}), {n80} are already 80%+ below their peak"}
        worst = sorted((c for c in grad if not c.get("nsfw") and c["ath_market_cap"] >= 100_000),
                       key=lambda c: (c.get("usd_market_cap") or 0) / c["ath_market_cap"])[:1]
        for c in worst:
            d = card(c)
            d["from_ath_pct"] = round(-100 * (1 - (c.get("usd_market_cap") or 0) / c["ath_market_cap"]), 1)
            d["summary"] = (f"peak {usd(d['ath'])}, now {usd(d['mcap'])} ({d['from_ath_pct']:g}%), "
                            f"coin age {_age_txt(d['age_h'])}")
            g["example"] = d
        out["graduated"] = g
    return out


# ---------------------------------------------------------------- DexScreener

@safe
def dex_candidates(pf: dict) -> dict:
    """Solana/Base token addresses to search for sharp moves."""
    cand = {ch: set() for ch in CHAINS}
    errs = []
    for path in ("token-boosts/top/v1", "token-boosts/latest/v1", "token-profiles/latest/v1",
                 "community-takeovers/latest/v1"):
        try:
            for x in getj(f"{DS}/{path}", 12):
                if x.get("chainId") in cand and x.get("tokenAddress"):
                    cand[x["chainId"]].add(x["tokenAddress"])
        except Exception as e:
            errs.append(f"{path.split('/')[0]}: {type(e).__name__}")
    for q in ("pump", "clanker", "zora", "base"):
        try:
            for p in getj(f"{DS}/latest/dex/search?q={q}", 12).get("pairs") or []:
                if p.get("chainId") in cand:
                    cand[p["chainId"]].add(p["baseToken"]["address"])
        except Exception as e:
            errs.append(f"search {q}: {type(e).__name__}")
    for ch, dur in (("base", "1h"), ("base", "24h"), ("solana", "1h")):   # sequential: GeckoTerminal rate limits
        try:
            for pl in getj(f"{GT}/networks/{ch}/trending_pools?duration={dur}", 12)["data"]:
                cand[ch].add(pl["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
        except Exception as e:
            errs.append(f"geckoterminal {ch} {dur}: {type(e).__name__}")
    for key in ("grad_active", "grad_new", "mcap_top"):
        for c in pf.get(key) or []:
            cand["solana"].add(c["mint"])
    return {"cand": {k: sorted(v) for k, v in cand.items()}, "errors": errs}


@safe
def dex_pairs(cand: dict) -> dict:
    """tokens/v1 in batches of 30: all pairs per token. Main pair = highest liquidity."""
    pairs, calls, fails = {}, 0, 0
    for ch, addrs in cand.items():
        for i in range(0, len(addrs), 30):
            try:
                for p in getj(f"{DS}/tokens/v1/{ch}/" + ",".join(addrs[i:i + 30]), 12):
                    pairs.setdefault((p["chainId"], p["baseToken"]["address"]), []).append(p)
                calls += 1
            except Exception:
                fails += 1
    _status["dexscreener"] = ((f"ok: {sum(len(v) for v in cand.values())} candidates -> {len(pairs)} tokens with pairs "
                               f"({calls} batch calls" + (f", {fails} failed)" if fails else ")")) if calls else
                              f"unavailable: 0 batch calls succeeded, {fails} failed")
    return pairs


def _liq(p: dict) -> float:
    return (p.get("liquidity") or {}).get("usd") or 0


def _main_pair(ps: list) -> dict:
    return max(ps, key=_liq)


def _ratio(p: dict):
    m, liq = p.get("marketCap") or p.get("fdv"), _liq(p)
    return m / liq if m and liq else None


def _coin(p: dict) -> dict:
    """Coin card: DexScreener fields only (x24 is priceChange.h24 expressed as a multiple)."""
    pc, tx, vol = p.get("priceChange") or {}, p.get("txns") or {}, p.get("volume") or {}
    h24, h1 = pc.get("h24"), pc.get("h1")
    age = _age_h(p.get("pairCreatedAt"))
    t24, t1 = tx.get("h24") or {}, tx.get("h1") or {}
    r = _ratio(p)
    c = {"sym": p["baseToken"].get("symbol"), "chain": p.get("chainId"), "dex": p.get("dexId"),
         "token": p["baseToken"].get("address"), "price": _sig(p.get("priceUsd")),
         "x24": _sig(1 + h24 / 100, 3) if h24 is not None and h24 > -100 else None, "pct24": h24, "pct1": h1,
         "vol24": _num(vol.get("h24")), "liq": _num(_liq(p)), "mcap": _num(p.get("marketCap") or p.get("fdv")),
         "mcap_liq": _sig(r, 3) if r else None, "age_h": age,
         "buys24": t24.get("buys"), "sells24": t24.get("sells"),
         "url": f"dexscreener.com/{p['chainId']}/{p.get('pairAddress')}"}
    c["summary"] = _coin_summary(c, b1=t1.get("buys") or 0, s1=t1.get("sells") or 0)
    return c


def _coin_summary(c: dict, b1: int = 0, s1: int = 0) -> str:
    f = []
    r, x, age, h1 = c.get("mcap_liq"), c.get("x24"), c.get("age_h"), c.get("pct1")
    b24, s24 = c.get("buys24") or 0, c.get("sells24") or 0
    fresh = age is not None and age < 24   # pair younger than a day: DexScreener counts h24 from the first trade
    if r and r >= 50:
        f.append(f"market cap {usd(c.get('mcap'))} on paper vs {usd(c.get('liq'))} in the pool ({r:.0f}x)")
    when = f"in {_age_txt(age)} since the pair launched" if fresh else "in 24h"
    if x and x >= 5:
        f.append(f"x{_x(x)} {when}, volume {usd(c.get('vol24'))}")
    elif (c.get("pct24") or 0) >= 50:
        f.append(f"+{c['pct24']:.0f}% {when}")
    if b24 >= 1000 and s24 and b24 / s24 >= 5:
        f.append(f"{_int(b24)} buys vs {_int(s24)} sells in 24h")
    if h1 is not None and h1 <= -25 and (c.get("pct24") or 0) > 100:
        f.append(f"but {h1:.0f}% in the last hour")
    elif h1 is not None and h1 >= 50 and not (fresh and age is not None and age <= 1):
        f.append(f"+{h1:.0f}% in the last hour")
    if s1 >= 30 and s1 > 1.5 * b1:
        f.append(f"{s1} sells vs {b1} buys in the last hour")
    return "; ".join(f[:3]) or f"24h volume {usd(c.get('vol24'))} with a {usd(c.get('liq'))} pool"


def _rows(pairs: dict) -> list:
    rows = []
    for ps in pairs.values():
        p = _main_pair(ps)
        if (p["baseToken"].get("symbol") or "").upper() not in NOT_MEME and _liq(p) >= MIN_LIQ:
            rows.append(p)
    return rows


def gainers(rows: list) -> dict:
    """Sharpest 24h and 1h moves on Solana and Base (paper caps excluded, see paper_mcap)."""
    rows = [p for p in rows if (_ratio(p) or 0) < PAPER_RATIO]
    seen: set = set()

    def ntx(p: dict, key: str) -> int:
        t = (p.get("txns") or {}).get(key) or {}
        return (t.get("buys") or 0) + (t.get("sells") or 0)

    def top(key: str, min_vol: float, min_pct: float, per_chain: dict) -> list:
        out = []
        min_tx = MIN_TX24 if key == "h24" else MIN_TX1
        for ch, n in per_chain.items():
            cand = [p for p in rows if p["chainId"] == ch and ((p.get("volume") or {}).get(key) or 0) >= min_vol
                    and ntx(p, key) >= min_tx
                    and ((p.get("priceChange") or {}).get(key) or 0) >= min_pct
                    and p["baseToken"]["address"] not in seen]
            for p in sorted(cand, key=lambda p: -p["priceChange"][key])[:n]:
                out.append(p)
                seen.add(p["baseToken"]["address"])
        return out

    g24 = top("h24", MIN_VOL24, 20, {"solana": 2, "base": 1})
    g1 = top("h1", MIN_VOL1, 20, {"solana": 2, "base": 1})
    return {"rule": f"pool>=${MIN_LIQ // 1000}k, volume 24h>=${MIN_VOL24 // 1000}k / 1h>=${MIN_VOL1 // 1000}k, "
                    f"trades>={MIN_TX24}/{MIN_TX1}, cap<{PAPER_RATIO}x pool; {len(rows)} candidates; "
                    f"age_h<24 -> pct24/x24 counted from the pair's first trade",
            "g24": [_coin(p) for p in g24], "g1": [_coin(p) for p in g1], "_raw": (g24, g1)}


def paper_mcap(rows: list) -> dict:
    """Young coins whose paper market cap is 100x+ what actually sits in the pool."""
    young = [p for p in rows if (_ratio(p) or 0) >= PAPER_RATIO and (_age_h(p.get("pairCreatedAt")) or 99) < 48]
    young.sort(key=lambda p: -(p.get("marketCap") or 0))
    out = []
    for p in young[:2]:
        c = _coin(p)
        if p.get("dexId") in ("pumpswap", "pumpfun"):
            c["pf_url"] = f"pump.fun/coin/{p['baseToken']['address']}"
        twins = sum(1 for q in rows if q["baseToken"].get("symbol") == c["sym"])
        if twins > 1:
            c["same_ticker_tokens"] = twins
            c["summary"] += f"; {twins} different tokens use the ticker {c['sym']}"
        out.append(c)
    return {"rule": f"pair <48 h, cap >= {PAPER_RATIO}x pool; found {len(young)}",
            "coins": out, "_raw": young[:2]}


# ---------------------------------------------------------------- holders

def _largest_rpc(mint: str) -> tuple[list, str]:
    """getTokenLargestAccounts: main RPC, then the public gateway. A failure disables a source for the run."""
    global _fallback_blocked
    if not _status.get("_main_off"):
        try:
            return rpc(RPC_MAIN, "getTokenLargestAccounts", [mint])["value"], "mainnet-beta"
        except Exception as e:
            _status["_main_off"] = f"{type(e).__name__} {str(e)[:40]}"
    if not _fallback_blocked:
        try:
            return rpc(RPC_FALLBACK, "getTokenLargestAccounts", [mint])["value"], "public-gateway"
        except Exception:
            _fallback_blocked = True
    raise RuntimeError("getTokenLargestAccounts unavailable")


def _hold_stats(bal: list[tuple[str, float]], supply: float, pools: set, creator: str | None,
                pool_tokens: float = 0) -> dict:
    """bal = [(owner, amount)] descending. Liquidity pools are not holders; the pool share comes from
    DexScreener liquidity.base (the pool may be outside the top 20)."""
    holders = [(o, a) for o, a in bal if o not in pools]
    top = [a / supply * 100 for _, a in holders[:10]]
    d = {"top10_pct": _pct(sum(top)), "top1_pct": _pct(top[0]) if top else None,
         "pool_pct": _pct(max(pool_tokens, sum(a for o, a in bal if o in pools)) / supply * 100)}
    amts = [a for _, a in holders if a > 0]
    best = max(amts, key=lambda a: sum(1 for b in amts if abs(b - a) <= a * 0.001), default=None)
    n_same = sum(1 for b in amts if best and abs(b - best) <= best * 0.001)
    if n_same >= 3:   # many top-20 addresses holding the same balance (+-0.1%)
        d["same_n"], d["same_of"] = n_same, len(amts)
        d["same_tokens"], d["same_pct"] = _sig(best, 10), _pct(best / supply * 100)
    if creator:
        cr = sum(a for o, a in bal if o == creator)
        if cr:
            d["creator_pct"] = _pct(cr / supply * 100)
    return d


def _holders_rpc(mint: str, pools: set, creator: str | None, pool_tokens: float) -> dict:
    supply = float(rpc(RPC_MAIN, "getTokenSupply", [mint])["value"]["uiAmountString"])
    largest, src = _largest_rpc(mint)
    largest = largest[:20]
    info = rpc(RPC_MAIN, "getMultipleAccounts", [[x["address"] for x in largest], {"encoding": "jsonParsed"}])["value"]
    owners = [((i or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("owner") for i in info]
    bal = [(o or x["address"], float(x["uiAmountString"])) for o, x in zip(owners, largest)]
    return {**_hold_stats(bal, supply, pools, creator, pool_tokens), "src": f"rpc:{src}"}


def _holders_rugcheck(mint: str, pools: set, creator: str | None, pool_tokens: float) -> dict:
    r = getj(f"{RUGCHECK}/{mint}/report", 10)
    pools = pools | {m.get("pubkey") for m in r.get("markets") or []}
    bal = [(h.get("owner") or h.get("address"), float(h.get("uiAmount") or 0)) for h in (r.get("topHolders") or [])[:20]]
    supply = float((r.get("token") or {}).get("supply") or 0) / 10 ** int((r.get("token") or {}).get("decimals") or 0)
    d = _hold_stats(bal, supply, pools, creator or r.get("creator"), pool_tokens)
    if r.get("totalHolders"):
        d["holders"] = r["totalHolders"]
    return {**d, "src": "rugcheck"}


def _mint_authorities(mints: list[str]) -> dict:
    """Can someone mint more or freeze other wallets? Read straight from the mint account."""
    out = {}
    try:
        info = rpc(RPC_MAIN, "getMultipleAccounts", [mints, {"encoding": "jsonParsed"}])["value"]
        for m, i in zip(mints, info):
            p = ((i or {}).get("data") or {}).get("parsed", {}).get("info", {})
            out[m] = {k: True for k, v in (("mint_authority_live", p.get("mintAuthority")),
                                           ("freeze_authority_live", p.get("freezeAuthority"))) if v}
    except Exception:
        pass
    return out


def _holders_summary(d: dict) -> str:
    f = []
    if d.get("same_n"):
        f.append(f"{d['same_n']} of the top {d['same_of']} addresses hold the same {_int(d['same_tokens'])} "
                 f"tokens ({d['same_pct']:g}% each)")
    if d.get("top10_pct") is not None:
        f.append(f"top 10 excluding the pool hold {d['top10_pct']:g}%, the pool holds {d['pool_pct']:g}%")
    if d.get("creator_pct"):
        f.append(f"creator holds {d['creator_pct']:g}%")
    if d.get("freeze_authority_live"):
        f.append("freeze authority not revoked: the issuer can freeze other holders' tokens")
    if d.get("mint_authority_live"):
        f.append("mint authority not revoked: more tokens can be minted")
    return "; ".join(f[:3])


@safe
def holders(hot: list[dict], pairs: dict, creators: dict) -> list:
    """3-5 hottest Solana coins: top 10 without the pool, largest holder, same-balance clusters."""
    sol = [p for p in hot if p["chainId"] == "solana"][:5]
    mints = [p["baseToken"]["address"] for p in sol]
    auth = _mint_authorities(mints) if mints else {}
    out, n_rpc, n_rc, errs = [], 0, 0, []
    for p, mint in zip(sol, mints):
        if left() < 4:
            errs.append("time budget exhausted")
            break
        mp = pairs.get(("solana", mint)) or []
        pools = {q.get("pairAddress") for q in mp} | AMM_AUTH
        pool_tokens = sum(float((q.get("liquidity") or {}).get("base") or 0) for q in mp)
        try:
            d = _holders_rpc(mint, pools, creators.get(mint), pool_tokens)
            n_rpc += 1
        except Exception:
            try:
                d = _holders_rugcheck(mint, pools, creators.get(mint), pool_tokens)
                n_rc += 1
            except Exception as e:
                errs.append(f"{p['baseToken'].get('symbol')}: {type(e).__name__}")
                continue
        d.update(auth.get(mint) or {})
        out.append({"sym": p["baseToken"].get("symbol"), "token": mint, **d,
                    "url": f"solscan.io/token/{mint}#holders", "summary": _holders_summary(d)})
    main = "getTokenLargestAccounts closed" if _status.get("_main_off") else "getTokenLargestAccounts ok"
    _status["solana_rpc"] = ("not checked: no hot Solana coins" if not mints else
                             f"mainnet-beta: {main}; gateway {'rate-limited/error' if _fallback_blocked else 'ok'}; "
                             f"holders via RPC {n_rpc}, via RugCheck {n_rc} of {len(mints)}"
                             + (f"; {errs}" if errs else ""))
    return out


@safe
def holders_base(hot: list[dict]) -> list:
    """Base: EVM has no getTokenLargestAccounts, use GeckoTerminal distribution (top 10 may include the pool)."""
    out = []
    for p in [p for p in hot if p["chainId"] == "base"][:2]:
        a = p["baseToken"]["address"]
        for attempt in (0, 1):
            try:
                h = getj(f"{GT}/networks/base/tokens/{a}/info", 8)["data"]["attributes"].get("holders") or {}
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt or left() < 8:
                    raise
                time.sleep(3)
        dist = h.get("distribution_percentage") or {}
        if dist.get("top_10") is None:
            continue
        row = {"sym": p["baseToken"].get("symbol"), "token": a, "holders": h.get("count"),
               "top10_pct_incl_pool": _num(dist.get("top_10"), 1), "src": "geckoterminal",
               "url": f"basescan.org/token/{a}#balances"}
        row["summary"] = f"{_int(row['holders'] or 0)} holders; top 10 addresses incl. the pool hold {row['top10_pct_incl_pool']:g}%"
        out.append(row)
    return out


# ---------------------------------------------------------------- assembly

def collect() -> dict:
    global _fallback_blocked
    t0 = time.time()
    _fallback_blocked = False
    _status.clear()
    d = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    pf = pumpfun_raw()
    if "error" in pf:
        _status["pumpfun"], pf = pf["error"], {}
    cands = dex_candidates(pf)
    pairs = dex_pairs(cands["cand"]) if "cand" in cands else {"error": cands.get("error")}
    if "error" in pairs:
        _status["dexscreener"], pairs = str(pairs["error"]), {}
    elif cands.get("errors"):
        _status["dexscreener"] += f"; partial: {cands['errors'][:3]}"

    rows = _rows(pairs)
    g, pm = safe(gainers)(rows), safe(paper_mcap)(rows)
    raw24, raw1 = g.pop("_raw", ([], [])) if "error" not in g else ([], [])
    raw_p = pm.pop("_raw", []) if "error" not in pm else []
    d["gainers"], d["paper_mcap"], d["pumpfun"] = g, pm, safe(pumpfun_view)(pf)

    # "hot" coins for the holder check: 2 top 24h + 2 top 1h on Solana + the largest paper cap
    sol24 = [p for p in raw24 if p["chainId"] == "solana"][:2]
    sol1 = [p for p in raw1 if p["chainId"] == "solana"][:2]
    hot, seen = [], set()
    for p in sol24 + sol1 + raw_p[:1] + [p for p in raw24 + raw1 if p["chainId"] == "base"]:
        if p["baseToken"]["address"] not in seen:
            seen.add(p["baseToken"]["address"])
            hot.append(p)
    creators = {c["mint"]: c.get("creator") for v in pf.values() for c in v}
    d["holders"] = holders(hot, pairs, creators)
    d["holders_base"] = holders_base(hot)
    _status.pop("_main_off", None)
    d["status"] = dict(_status)
    d["took_s"] = round(time.time() - t0, 1)
    return d
