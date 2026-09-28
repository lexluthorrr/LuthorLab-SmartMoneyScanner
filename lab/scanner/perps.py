"""Perps and whale money: what large accounts are doing right now.

Hyperliquid (public info API + public leaderboard):
  - the largest and riskiest open positions among top leaderboard accounts;
  - daily and weekly leaders and biggest losers by perp P&L.
Ethereum (free public RPC, no key): USDT/USDC transfers of $20M+ over the last ~6 hours,
with public exchange labels.

Read-only public data. collect() -> compact dict.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import time

from lab.net import get, getj, pmap_dict, safe, usd

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_ALIASES = "https://api.hypurrscan.io/globalAliases"   # public display names of Hyperliquid addresses
HL_ADDR = "www.coinglass.com/hyperliquid/"
# system and exchange accounts are not whales (treasuries, deployers, HLP, liquidator, exchanges)
HL_SYSTEM = re.compile(r"treasury|deployer|vault|hlp|liquidator|assistance|fund|bridge|fee|\bdev\b|"
                       r"gate\.io|bitvavo|bitget|kucoin|binance|okx|bybit", re.I)

ETH_RPCS = ["https://ethereum-rpc.publicnode.com", "https://rpc.flashbots.net"]
TOKENS = {"0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC"}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDT_ISSUE = "0xcb8241adb0c3fdb35b70c24ce35c5eb0c17af7431c99f827d44a445ca624176a"   # Issue(uint256)
USDT_REDEEM = "0x702d5967f45f6513a38ffc42d6ba9bf230bd40e8f53b16363c7eb4fd2deb9a44"  # Redeem(uint256)
ZERO = "0x" + "0" * 40
MIN_USD = 20e6
WINDOW_BLOCKS = 1800      # ~6 h at 12 s per block
CHUNK = 150               # public RPCs reject very wide log ranges
ETH_BUDGET_S = 35         # if time runs out the window is honestly shorter (see window_h)

# Public Etherscan name tags (etherscan.io/address/<address>).
LABELS = {
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff": "Binance 2",
    "0x564286362092d8e7936f0549571a803b203aaced": "Binance 3",
    "0x0681d8db095565fe8a346fa0277bffde9c0edbbf": "Binance 4",
    "0xfe9e8709d3215310075d67e3ed32a380ccf451c8": "Binance 5",
    "0x4e9ce36e442e55ecd9025b9a6e0d88485d628a67": "Binance 6",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance 7",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance: Hot Wallet 20",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance 15",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance 16",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance 17",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Binance 18",
    "0x4976a4a02f38326660d17bf34b431dc6e2eb2327": "Binance 20",
    "0x5a52e96bacdabb82fd05763e25335261b270efcb": "Binance 28",
    "0xee7ae85f2fe2239e27d9c1e23fffe168d63b4055": "Binance: Hot Wallet 34",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase 1",
    "0x77696bb39917c91a0c3908d577d5e322095425ca": "Coinbase 3",
    "0x7c195d981abfdc3ddecd2ca0fed0958430488e34": "Coinbase 4",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase 12",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase 23",
    "0x28c5b0445d0728bc25f143f8eba5c5539fae151a": "Coinbase 29",
    "0x3cd751e6b0078be393132286c442345e5dc49699": "Coinbase 33",
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511": "Coinbase 44",
    "0xeb2629a2734e272bcc07bda959863f316f4bd4cf": "Coinbase 54",
    "0x02466e547bfdab679fc49e96bbfc62b9747d997c": "Coinbase 56",
    "0x6b76f8b1e9e59913bfe758821887311ba1805cab": "Coinbase 57",
    "0xa090e606e30bd747d4e6245a1517ebe430f0057e": "Coinbase: Miscellaneous",
    "0xcd531ae9efcce479654c4926dec5f6209531ca7b": "Coinbase Prime 1",
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken 1",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13": "Kraken 2",
    "0xe853c56864a2ebe4576a807d26fdc4a0ada51919": "Kraken 3",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken 4",
    "0x53d284357ec70ce289d6d64134dfac8e511c8a3d": "Kraken 6",
    "0xda9dfa130df4de4673b89022ee50ff26f6ea73cf": "Kraken 13",
    "0xe9f7ecae3a53d2a67105292894676b00d1fab785": "Kraken: Hot Wallet",
    "0x05ff6964d21e5dae3b1010d5ae0465b3c450f381": "Kraken: Hot Wallet 4",
    "0xaa8ba7d4611437141192e7ceced531bc0a133efb": "Kraken: Hot Wallet 5",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX 2",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX 3",
    "0x98ec059dc3adfbdd63429454aeb0c990fba4a128": "OKX 6",
    "0x5041ed759dd4afc3a72b8192c143f72f4724081a": "OKX 7",
    "0x91d40e4818f4d4c57b4578d9eca6afc92ac8debe": "OKX 193",
    "0x445f16314284b43dfa1fd3cd77b9dea4a1bebd97": "OKX 225",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit: Hot Wallet",
    "0xa7a93fd0a276fc1c0197a5b5623ed117786eed06": "Bybit: Hot Wallet 2",
    "0xee5b5b923ffce93a870b3104b7ca09c3db80047a": "Bybit: Hot Wallet 4",
    "0xbaed383ede0e5d9d72430661f3285daa77e9439f": "Bybit: Hot Wallet 6",
    "0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4": "Bybit: Cold Wallet 1",
    "0x88a1493366d48225fc3cefbdae9ebb23e323ade3": "Bybit 2",
    "0x5754284f345afc66a98fbb0a0afe71e0f007b949": "Tether: Treasury",
    "0xc6cde7c39eb2f0f0095f41570af89efc2c1ea828": "Tether: Multisig",
    "0x55fe002aeff02f77364de339a1292923a15844b8": "Circle",
    "0x77134cbc06cb00b66f4c7e623d5fdbf6777635ec": "Bitfinex: Hot Wallet",
    "0x742d35cc6634c0532925a3b844bc454e4438f44e": "Bitfinex 2",
    "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa": "Bitfinex 3",
    "0xab5c66752a9e8167967685f1450532fb96d5d24f": "HTX 1",
    "0x3cc936b795a188f0e246cbb2d74c5bd190aecf18": "MEXC 3",
    "0xc411ab12837f63935b71b6c6643d0f7000e55e4b": "MEXC 22",
    "0x8fca4ade3a517133ff23ca55cdaea29c78c990b8": "Poloniex 7",
    "0x29065a4c1f2f20d1e263930088890d6f49fe715a": "Poloniex 10",
    "0x33566c9d8be6cf0b23795e0d380e112be9d75836": "Galaxy Digital: OTC",
    "0x1157a2076b9bb22a85cc2c162f20fab3898f4101": "FalconX 1",
    "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb": "Morpho: Morpho",
    "0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c": "Aave: USDC V3",
    "0x23878914efe38d27c4d67ab83ed1b93a74d4086a": "Aave: Ethereum USDT",
    "0x37305b1cd40574e4c5ce33f8e8306be057fd7341": "Sky: PSM",
    "0x0a59649758aa4d66e25f08dd01271e891fe52199": "Sky: PSM-USDC-A",
    "0xa188eec8f81263234da3622a406892f3d630f98c": "Spark: Usds Psm Wrapper",
}
CEX = {"Binance", "Coinbase", "Kraken", "OKX", "Bybit", "Bitfinex", "HTX", "MEXC", "Poloniex"}
ISSUER = {"Tether", "Circle"}
DEFI = {"Morpho", "Aave", "Sky", "Spark"}


def short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"


def px(x: float) -> float:
    """Price with 5 significant digits."""
    return float(f"{x:.5g}")


# ---------------------------------------------------------------- Hyperliquid

def hl(body: dict, timeout: int = 15):
    return getj(HL_INFO, timeout, body)


def _position_rows(addr: str, st: dict) -> list[dict]:
    ms = st.get("marginSummary") or {}
    av, ntl = float(ms.get("accountValue") or 0), float(ms.get("totalNtlPos") or 0)
    rows = []
    for ap in st.get("assetPositions") or []:
        p = ap.get("position") or {}
        szi, val = float(p.get("szi") or 0), float(p.get("positionValue") or 0)
        if not szi or not val:
            continue
        mark = val / abs(szi)
        liq = float(p["liquidationPx"]) if p.get("liquidationPx") else None
        rows.append({
            "addr": addr, "coin": p.get("coin"), "side": "long" if szi > 0 else "short", "value": val,
            "lev": (p.get("leverage") or {}).get("value"), "margin": (p.get("leverage") or {}).get("type"),
            "entry": float(p.get("entryPx") or 0), "mark": mark, "liq": liq,
            "to_liq_pct": round(abs(liq - mark) / mark * 100, 1) if liq and liq > 0 else None,
            "upnl": float(p.get("unrealizedPnl") or 0), "acct_value": av,
            "acct_ntl": ntl, "acct_lev": round(ntl / av, 1) if av > 0 else None,
        })
    return rows


def _fill_age(fills: list[dict], coin: str, side: str, now_ms: int) -> dict:
    """When the position was last touched and when it was opened (if the open is within recent fills)."""
    fs = sorted((f for f in fills if f.get("coin") == coin), key=lambda f: f["time"])
    if not fs:
        return {}
    out = {"changed_h": round((now_ms - fs[-1]["time"]) / 3.6e6, 1)}
    sign = 1 if side == "long" else -1
    for f in reversed(fs):
        start = float(f.get("startPosition") or 0)
        if start == 0 or (start > 0) != (sign > 0):
            out["opened_h"] = round((now_ms - f["time"]) / 3.6e6, 1)
            break
    return out


def _pos_summary(p: dict, why: list[str], age: dict) -> str:
    s = f"{p['side'].capitalize()} {p['coin']} {usd(p['value'])} at {p['lev']}x"
    if p["upnl"]:
        s += f", {'down' if p['upnl'] < 0 else 'up'} {usd(abs(p['upnl']))} right now"
    if "near_liq" in why:
        s += f", {p['to_liq_pct']}% price move to liquidation"
    if (age.get("changed_h") or 0) >= 720:
        s += f", untouched for {age['changed_h'] / 24:.0f} days"
    if p["acct_ntl"] > 1.3 * p["value"]:
        s += f"; account total in positions {usd(p['acct_ntl'])}"
    return s


@safe
def hyperliquid():
    t0, now_ms = time.time(), int(time.time() * 1000)
    with cf.ThreadPoolExecutor(2) as ex:
        f_lb = ex.submit(get, HL_LEADERBOARD, 30)
        f_al = ex.submit(get, HL_ALIASES, 10)
        raw = f_lb.result()
        try:
            aliases = {k.lower(): v for k, v in json.loads(f_al.result()).items()}
        except Exception:
            aliases = {}
    rows = []
    for r in json.loads(raw)["leaderboardRows"]:
        wp = {k: float(v.get("pnl") or 0) for k, v in r.get("windowPerformances") or []}
        a = r["ethAddress"].lower()
        name = r.get("displayName") or aliases.get(a)
        if name and HL_SYSTEM.search(name):
            continue
        rows.append({"addr": a, "name": name, "lb_av": float(r.get("accountValue") or 0), **wp})
    del raw

    def by(k, rev):
        return sorted(rows, key=lambda r: r.get(k, 0), reverse=rev)

    lists = {"day_top": by("day", True)[:10], "day_worst": by("day", False)[:10],
             "week_top": by("week", True)[:10], "week_worst": by("week", False)[:10]}
    cand = {r["addr"]: r for r in by("lb_av", True)[:50]}
    for lst in lists.values():
        cand.update({r["addr"]: r for r in lst})

    states = pmap_dict(lambda a: hl({"type": "clearinghouseState", "user": a}), list(cand))

    def perp_dominant(r: dict) -> bool:
        """Spot treasuries and token holders show up on the leaderboard but are not perp traders."""
        ms = (states.get(r["addr"]) or {}).get("marginSummary") or {}
        av, ntl = float(ms.get("accountValue") or 0), float(ms.get("totalNtlPos") or 0)
        return max(av, ntl) >= 0.3 * r["lb_av"]

    # 1) whale positions
    allpos = [p for a, st in states.items() for p in _position_rows(a, st)]
    big = [p for p in allpos if p["value"] >= 1e6]
    picks: dict[tuple, list] = {}

    def add(ps, why, n):
        for p in ps[:n]:
            picks.setdefault((p["addr"], p["coin"]), [p, []])[1].append(why)

    top_per_acct = {}
    for p in sorted(big, key=lambda p: -p["value"]):
        top_per_acct.setdefault(p["addr"], p)
    add(list(top_per_acct.values()), "largest", 3)
    add(sorted((p for p in big if p["to_liq_pct"] is not None and p["to_liq_pct"] <= 25 and p["value"] >= 3e6),
               key=lambda p: p["to_liq_pct"]), "near_liq", 2)
    add([p for p in sorted(big, key=lambda p: p["upnl"]) if p["upnl"] < 0], "big_loss", 1)
    add([p for p in sorted(big, key=lambda p: -p["upnl"]) if p["upnl"] > 0], "big_gain", 1)
    add(sorted((p for p in big if p["value"] >= 5e6 and p["acct_lev"]), key=lambda p: -p["acct_lev"]), "max_acct_lev", 1)
    chosen = list(picks.values())[:6]

    users = list(dict.fromkeys(p["addr"] for p, _ in chosen))[:6]
    fills = pmap_dict(lambda a: hl({"type": "userFills", "user": a, "aggregateByTime": True}), users, 6)

    positions = []
    for p, why in chosen:
        age = _fill_age(fills.get(p["addr"]) or [], p["coin"], p["side"], now_ms)
        row = {"who": cand[p["addr"]].get("name"), "wallet": p["addr"], "coin": p["coin"], "side": p["side"],
               "usd": round(p["value"]), "lev": p["lev"], "entry": px(p["entry"]), "mark": px(p["mark"]),
               "liq": px(p["liq"]) if p["liq"] else None, "to_liq_pct": p["to_liq_pct"], "upnl": round(p["upnl"]),
               "acct_value": round(p["acct_value"]), "acct_ntl": round(p["acct_ntl"]), "why": why, **age,
               "url": HL_ADDR + p["addr"], "summary": _pos_summary(p, why, age)}
        positions.append({k: v for k, v in row.items() if v is not None})

    # 2) leaders and losers: perp accounts only, perp P&L from portfolio (like the Perps tab)
    board = {}
    for key, lst in lists.items():
        window = "perpDay" if key.startswith("day") else "perpWeek"
        top = [r for r in lst if perp_dominant(r)][:4]
        port = pmap_dict(lambda a: dict(hl({"type": "portfolio", "user": a})), [r["addr"] for r in top], 4)
        out = []
        for r in top:
            hist = ((port.get(r["addr"]) or {}).get(window) or {}).get("pnlHistory") or []
            if not hist:
                continue
            pnl = float(hist[-1][1]) - float(hist[0][1])
            lb = r["day" if key.startswith("day") else "week"]
            if (pnl > 0) != (lb > 0):
                continue          # leaderboard is cached ~1 h; the live sign already flipped
            ps = sorted(_position_rows(r["addr"], states.get(r["addr"]) or {}), key=lambda p: -p["value"])
            now_pos = f"{ps[0]['coin']} {ps[0]['side']} {usd(ps[0]['value'])}" if ps else None
            av = float(((states.get(r["addr"]) or {}).get("marginSummary") or {}).get("accountValue") or 0)
            period = "24h" if key.startswith("day") else "7d"
            summary = (f"+{usd(pnl)} on perps in {period} with a {usd(av)} account" if pnl > 0 else
                       f"{usd(pnl)} in {period}, and {now_pos} is still open" if now_pos else
                       f"{usd(pnl)} in {period}, no open positions left")
            row = {"who": r.get("name"), "wallet": r["addr"], "pnl": round(pnl), "acct_value": round(av),
                   "now": now_pos, "url": HL_ADDR + r["addr"], "summary": summary}
            out.append({k: v for k, v in row.items() if v is not None})
        board[key] = sorted(out, key=lambda x: -abs(x["pnl"]))[:2]

    return {
        "rule": "snapshot of api.hyperliquid.xyz/info at collection time; leader pnl = perp P&L over 24h/7d "
                "(portfolio); changed_h/opened_h from the wallet's recent fills",
        "positions": positions,
        "pnl_board": board,
        "scanned_accounts": len(states), "took_s": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- Ethereum

def rpc(method: str, params: list, timeout: int = 25):
    last = None
    for url in ETH_RPCS:
        try:
            d = getj(url, timeout, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in d:
                raise RuntimeError(str(d["error"])[:100])
            return d["result"]
        except Exception as e:
            last = e
    raise last


def label(a: str) -> str:
    return "mint/burn (0x0)" if a == ZERO else LABELS.get(a) or short(a)


def group(a: str) -> str | None:
    return "ZERO" if a == ZERO else LABELS[a].split()[0].rstrip(":") if a in LABELS else None


def _tx_summary(t: dict, gf: str | None, gt: str | None) -> str:
    amt = f"{usd(t['usd'])} {t['token']}"
    if gf == "ZERO":
        return f"{amt} minted in one transaction"
    if gt == "ZERO":
        return f"{amt} burned (removed from circulation)"
    if gf and gf == gt:
        return f"{gf} moving {amt} between its own wallets"
    if t.get("via"):
        return f"{amt} moved from {gf or t['from']} to {gt or t['to']} through an intermediary wallet within minutes"
    if gf in ISSUER:
        return f"{amt} sent from {t['from']} to {t['to']} in one transfer"
    if gf in CEX and not gt:
        return f"{amt} withdrawn from {gf} to an unlabeled wallet"
    if gt in CEX and not gf:
        return f"An unlabeled wallet deposited {amt} to {gt}"
    if gf in DEFI or gt in DEFI:
        return f"{amt} moved through a DeFi protocol ({gf or gt})"
    return f"{amt} moved from {t['from']} to {t['to']} in one transfer"


@safe
def eth_transfers():
    t0 = time.time()
    head = int(rpc("eth_blockNumber", []), 16)
    starts = list(range(head - CHUNK + 1, head - WINDOW_BLOCKS, -CHUNK))    # newest first

    def chunk(a: int):
        if time.time() - t0 > ETH_BUDGET_S:
            raise TimeoutError
        logs = rpc("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(min(a + CHUNK - 1, head)),
                                    "address": list(TOKENS), "topics": [TRANSFER]}])
        big = []
        for lg in logs:
            v = int(lg["data"], 16) / 1e6 if lg["data"] not in ("0x", "") else 0
            if v >= MIN_USD:
                big.append({"token": TOKENS[lg["address"].lower()], "f": "0x" + lg["topics"][1][26:],
                            "t": "0x" + lg["topics"][2][26:], "usd": v, "tx": lg["transactionHash"],
                            "block": int(lg["blockNumber"], 16), "ts": int(lg.get("blockTimestamp") or "0x0", 16)})
        return len(logs), big

    done = pmap_dict(chunk, starts, 6)
    covered = sorted(a for a in starts if a in done)
    n_logs = sum(n for n, _ in done.values())
    raw = [x for _, b in done.values() for x in b]

    # flash loans: A->B and B->A for the same amount in one transaction are not money moving
    by_tx: dict[str, list] = {}
    for x in raw:
        by_tx.setdefault(x["tx"], []).append(x)
    flash, real = set(), []
    for x in raw:
        if any(y["f"] == x["t"] and y["t"] == x["f"] and y["token"] == x["token"] and abs(y["usd"] - x["usd"]) <= 0.001 * x["usd"]
               for y in by_tx[x["tx"]]):
            flash.add(x["tx"])
        else:
            real.append(x)

    # A -> unlabeled intermediary -> B for the same amount within ~1 h is merged into one finding
    real.sort(key=lambda x: x["block"])
    used, chained = set(), []
    for i, x in enumerate(real):
        if i in used:
            continue
        if x["t"] not in LABELS and x["t"] != ZERO:
            for j in range(i + 1, len(real)):
                y = real[j]
                if (j not in used and y["f"] == x["t"] and y["token"] == x["token"] and y["block"] - x["block"] <= 300
                        and abs(y["usd"] - x["usd"]) <= 0.001 * x["usd"]):
                    x = {**x, "t": y["t"], "via": x["t"], "tx2": y["tx"]}
                    used.add(j)
                    break
        chained.append(x)

    now = time.time()
    missing_ts = {x["block"] for x in real if not x["ts"]}
    ts_by_block = pmap_dict(lambda b: int(rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16), list(missing_ts), 4)
    weight = {"ZERO": 1.5, "Tether": 1.5, "Circle": 1.5}
    items = []
    for x in chained:
        gf, gt = group(x["f"]), group(x["t"])
        w = weight.get(gf, weight.get(gt, 1.0))
        if gf and gf == gt:
            w = 0.3
        elif gf in DEFI or gt in DEFI:
            w = 0.5
        elif (gf in CEX) != (gt in CEX):
            w = max(w, 1.2)
        elif not gf and not gt:
            w = 0.8
        ts = x["ts"] or ts_by_block.get(x["block"], 0)
        t = {"token": x["token"], "usd": round(x["usd"]), "from": label(x["f"]), "to": label(x["t"]),
             "age_h": round((now - ts) / 3600, 1) if ts else None}
        if x.get("via"):
            t["via"] = short(x["via"])
        if gf and gf == gt:
            t["internal"] = True
        t["tx"] = "etherscan.io/tx/" + x["tx"]
        if x.get("tx2"):
            t["tx2"] = "etherscan.io/tx/" + x["tx2"]
        t["summary"] = _tx_summary(t, gf, gt)
        items.append((x["usd"] * w, t))
    items = [t for _, t in sorted(items, key=lambda z: -z[0])][:5]

    # USDT issuance uses the Issue event, not Transfer: one cheap query over ~24 h
    mint = []
    try:
        for lg in rpc("eth_getLogs", [{"fromBlock": hex(head - 7200), "toBlock": hex(head),
                                       "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                                       "topics": [[USDT_ISSUE, USDT_REDEEM]]}]):
            ts = int(lg.get("blockTimestamp") or "0x0", 16)
            mint.append({"event": "issue" if lg["topics"][0] == USDT_ISSUE else "redeem",
                         "usd": round(int(lg["data"], 16) / 1e6), "age_h": round((now - ts) / 3600, 1) if ts else None,
                         "tx": "etherscan.io/tx/" + lg["transactionHash"]})
    except Exception as e:
        mint = f"error: {str(e)[:80]}"

    try:
        oldest_ts = int(rpc("eth_getBlockByNumber", [hex(covered[0]), False])["timestamp"], 16) if covered else now
    except Exception:
        oldest_ts = now - (head - covered[0]) * 12
    gaps = len(starts) - len(covered)
    out = {
        "window_h": round((now - oldest_ts) / 3600, 1), "min_usd": int(MIN_USD),
        "note": f"skipped {gaps} of {len(starts)} windows of {CHUNK} blocks (timeout/RPC)" if gaps else None,
        "scanned_logs": n_logs, "big_count": len(real), "big_sum_usd": round(sum(x["usd"] for x in real)),
        "flash_loans_skipped": len(flash),
        "items": items, "usdt_issue_redeem_24h": mint,
        "took_s": round(time.time() - t0, 1),
    }
    return {k: v for k, v in out.items() if v is not None}


SOURCES = [hyperliquid, eth_transfers]


def collect() -> dict:
    t0 = time.time()
    out = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with cf.ThreadPoolExecutor(len(SOURCES)) as ex:
        futs = {fn.__name__: ex.submit(fn) for fn in SOURCES}
        for name, f in futs.items():
            out[name] = f.result()
    out["took_s"] = round(time.time() - t0, 1)
    return out
