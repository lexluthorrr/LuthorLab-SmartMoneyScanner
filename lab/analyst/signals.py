"""Turn scanner findings into plain-language signals.

A signal is a finding plus the one number that matters and why it matters. Signals are not
trade ideas: they are things a careful reader should check before believing a headline.

Kinds:
  cap_vs_pool       market cap vs what actually sits in the liquidity pool
  liquidation       how far price must move to liquidate a perp position
  wallet_age        how young a wallet with a big result is
  win_vs_total      biggest single win vs the wallet's all-time profile total
  holder_cluster    many top holders with identical balances / concentrated supply
  token_authority   mint or freeze authority not revoked
  timed_bet         a big cheap bet followed by a sharp price jump
  stablecoin_flow   very large USDT/USDC mints, burns and exchange flows
"""
from __future__ import annotations

from lab.net import usd


def _sig(kind: str, subject: str, headline: str, why: str, strength: int, source: str | None, **numbers) -> dict:
    return {"kind": kind, "subject": subject, "headline": headline, "why_it_matters": why,
            "strength": strength, "numbers": {k: v for k, v in numbers.items() if v is not None},
            "source": source}


def _l(node, *keys) -> list:
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    return node if isinstance(node, list) else []


def cap_vs_pool(coin: dict) -> dict | None:
    r, mcap, liq = coin.get("mcap_liq"), coin.get("mcap"), coin.get("liq")
    if not r or r < 20 or not mcap or not liq:
        return None
    strength = 3 if r >= 100 else 2 if r >= 50 else 1
    return _sig("cap_vs_pool", coin.get("sym") or "?",
                f"{coin.get('sym')}: market cap {usd(mcap)} is {r:.0f}x the {usd(liq)} liquidity pool",
                "The cap is price x supply, not money anyone can take out. Only the pool can absorb selling; "
                "selling a small slice of the 'paper' cap would move the price hard.",
                strength, coin.get("url"), mcap_usd=mcap, pool_usd=liq, ratio=r)


def liquidation(p: dict) -> dict | None:
    d = p.get("to_liq_pct")
    if d is None or d > 50:
        return None
    strength = 3 if d <= 10 else 2 if d <= 25 else 1
    direction = "rise" if p.get("side") == "short" else "fall"
    return _sig("liquidation", p.get("who") or p.get("wallet", "")[:10],
                f"{p.get('side', '').capitalize()} {p.get('coin')} {usd(p.get('usd'))} at {p.get('lev')}x: "
                f"a {d}% price {direction} liquidates it",
                "Distance to liquidation is the fragility of the position. A forced close of a large "
                "position is itself a market order that can push price further.",
                strength, p.get("url"), position_usd=p.get("usd"), to_liq_pct=d, leverage=p.get("lev"),
                entry=p.get("entry"), mark=p.get("mark"), liq_price=p.get("liq"), unrealized_usd=p.get("upnl"))


def wallet_age(w: dict) -> dict | None:
    age, realized = w.get("age_days"), w.get("realized_pnl_usd")
    if age is None or age >= 14 or not realized:
        return None
    strength = 3 if age < 3 else 2 if age < 7 else 1
    return _sig("wallet_age", w.get("name") or w.get("wallet", "")[:10],
                f"{w.get('name') or 'Wallet'}: {age:g} days old, realized {'+' if realized > 0 else ''}{usd(realized)}",
                "A new wallet with an outsized result can be a skilled trader's fresh account, someone with "
                "information, or one lucky bet. Check the first trade and whether it is all one market.",
                strength, w.get("url"), age_days=age, realized_usd=realized,
                first_trade_at=w.get("first_trade_at"))


def win_vs_total(name: str, win: dict | None, prof: dict | None, url: str | None) -> dict | None:
    if not win or not prof:
        return None
    profit, total = win.get("profit_usd"), prof.get("total_pnl_usd")
    if profit is None or total is None or profit <= 0:
        return None
    if total >= 0.5 * profit:
        return None
    strength = 3 if total < 0 else 2
    return _sig("win_vs_total", name,
                f"{name}: biggest win +{usd(profit)}, but the all-time profile total is {usd(total)}",
                "One winning trade says nothing about the trader. The profile total includes every other "
                "trade and open position; screenshots of a single win hide it.",
                strength, url, biggest_win_usd=profit, profile_total_usd=total,
                open_unrealized_usd=prof.get("open_unrealized_usd"), market=win.get("market"))


def holder_signals(h: dict) -> list[dict]:
    out = []
    sym = h.get("sym") or "?"
    if (h.get("same_n") or 0) >= 5:
        n, of, pct = h["same_n"], h.get("same_of"), h.get("same_pct")
        out.append(_sig("holder_cluster", sym,
                        f"{sym}: {n} of the top {of} holders hold an identical balance "
                        f"({pct:g}% each, {n * (pct or 0):.3g}% combined)",
                        "Identical balances across many wallets usually mean one buyer split across wallets "
                        "(a bundle). Together they own far more than any single holder shows, and can sell together.",
                        3 if n >= 10 else 2, h.get("url"), wallets=n, of_top=of, pct_each=pct,
                        combined_pct=round(n * (pct or 0), 2)))
    elif (h.get("top10_pct") or 0) >= 50:
        out.append(_sig("holder_cluster", sym, f"{sym}: top 10 holders (excluding the pool) own {h['top10_pct']:g}%",
                        "Concentrated supply means a few wallets decide the price.", 2, h.get("url"),
                        top10_pct=h["top10_pct"]))
    if h.get("mint_authority_live") or h.get("freeze_authority_live"):
        what = " and ".join(x for x, k in (("mint", "mint_authority_live"), ("freeze", "freeze_authority_live")) if h.get(k))
        out.append(_sig("token_authority", sym, f"{sym}: {what} authority is not revoked",
                        "Mint authority lets the issuer print more tokens; freeze authority lets it lock holders' "
                        "tokens. Either one means you trust the issuer, not the code.", 3, h.get("url")))
    return out


def timed_bet(b: dict) -> dict | None:
    h = b.get("hit_80c_after_h")
    if h is None and not b.get("resolved_in_their_favor"):
        return None
    strength = 3 if h is not None and h <= 3 else 2
    when = f"{h:g} h later it traded at 80c+" if h is not None else "the market resolved in their favor"
    return _sig("timed_bet", b.get("who") or b.get("wallet", "")[:10],
                f"{b.get('who') or 'Wallet'} bought {usd(b.get('stake_usd'))} of \"{b.get('side')}\" at "
                f"{b.get('entry_c')}c; {when}",
                "A large, cheap, well-timed bet can be information or luck. One bet is an anecdote; "
                "look at the wallet's history before calling it insider trading.",
                strength, b.get("url"), stake_usd=b.get("stake_usd"), entry_c=b.get("entry_c"),
                bet_at=b.get("bet_at"), hours_to_80c=h, wallet_url=b.get("wallet_url"))


def stablecoin_flow(t: dict) -> dict | None:
    if (t.get("usd") or 0) < 50e6 or t.get("internal"):
        return None
    return _sig("stablecoin_flow", t.get("token", ""), t.get("summary") or "",
                "Very large stablecoin mints, burns and exchange deposits show where dollar liquidity is "
                "moving. Direction matters more than size; internal exchange shuffles are excluded.",
                2, t.get("tx"), usd=t.get("usd"), frm=t.get("from"), to=t.get("to"), age_h=t.get("age_h"))


def from_digest(d: dict) -> list[dict]:
    """All signals found in a digest, strongest first."""
    out: list[dict] = []
    memes = d.get("memes") or {}
    coins = _l(memes, "gainers", "g24") + _l(memes, "gainers", "g1") + _l(memes, "paper_mcap", "coins")
    seen = set()
    for c in coins:
        if c.get("url") in seen:
            continue
        seen.add(c.get("url"))
        out.append(cap_vs_pool(c))
    for h in _l(memes, "holders"):
        out.extend(holder_signals(h))

    perps = d.get("perps") or {}
    out.extend(liquidation(p) for p in _l(perps, "hyperliquid", "positions"))
    out.extend(stablecoin_flow(t) for t in _l(perps, "eth_transfers", "items"))

    sig = d.get("signals") or {}
    for w in _l(sig, "fresh_wallets"):
        out.append(wallet_age(w))
        out.append(win_vs_total(w.get("name") or w.get("wallet", "")[:10], w.get("biggest_win"), w.get("profile"), w.get("url")))
    for b in _l(sig, "timed_bets"):
        out.append(timed_bet(b))

    wh = d.get("polymarket") or {}
    for key in ("top_day_overall", "top_week_crypto"):
        for r in _l(wh, key):
            prof = r.get("profile") or {}
            out.append(win_vs_total(r.get("name") or (r.get("wallet") or "")[:10], prof.get("biggest_win_resolved"),
                                    prof, prof.get("profile_url")))

    uniq, keys = [], set()
    for s in out:
        if s and (s["kind"], s["subject"], s["headline"]) not in keys:
            keys.add((s["kind"], s["subject"], s["headline"]))
            uniq.append(s)
    return sorted(uniq, key=lambda s: -s["strength"])


def render(signals: list[dict], limit: int = 15) -> str:
    if not signals:
        return "No signals in this digest."
    lines = []
    for s in signals[:limit]:
        lines.append(f"[{'*' * s['strength']:<3}] {s['kind']}: {s['headline']}")
        lines.append(f"      why: {s['why_it_matters']}")
        if s.get("source"):
            lines.append(f"      source: {s['source']}")
    if len(signals) > limit:
        lines.append(f"... and {len(signals) - limit} more in the JSON output")
    return "\n".join(lines)
