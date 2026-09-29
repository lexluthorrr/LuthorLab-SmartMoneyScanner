"""Run the SCANNER: `python -m lab.scan` -> data/digest.json + a short summary in the console.

Read-only: every request is a public GET (or a read-only JSON-RPC call). No keys, no accounts.

  python -m lab.scan                 full scan (about 60-90 s)
  python -m lab.scan --only memes    one module (perps, memes, polymarket, signals)
  python -m lab.scan --dry           offline self-check: imports, data dir, optional tools, memory

After a scan, lab.memory remembers every finding in data/lab.db: each one is flagged new or seen before
(with the change since last time), and a few outcome checks run if budget is left.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from lab import config, net
from lab.scanner import MODULES

DEFAULT_BUDGET_S = 75
OUTCOME_MIN_LEFT_S = 8        # outcome checks after a scan run only if at least this much budget is left
OUTCOME_MAX_CHECKS = 12


def run(only: list[str] | None = None, budget_s: float = DEFAULT_BUDGET_S) -> dict:
    """Run scanner modules in parallel within a time budget and return the digest."""
    t0 = time.time()
    net.set_deadline(t0 + budget_s)
    mods = [m for m in MODULES if not only or m in only]
    results: dict[str, dict] = {}

    def one(name: str) -> None:
        try:
            results[name] = importlib.import_module(f"lab.scanner.{name}").collect()
        except Exception as e:                      # one module must not break the whole scan
            results[name] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}

    threads = [threading.Thread(target=one, args=(m,), daemon=True) for m in mods]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(0.0, t0 + budget_s + 10 - time.time()))
    digest = {"collected_at": net.utc_now(), "read_only": True,
              "note": "public data snapshot; every number comes from an API response; not financial advice"}
    for m in mods:
        digest[m] = results.get(m) or {"error": "did not finish within the time budget"}
    digest["took_s"] = round(time.time() - t0, 1)
    net.set_deadline(None)
    return digest


def save(digest: dict, path: Path | None = None) -> Path:
    path = path or config.data_dir() / "digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(digest, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- human summary

def _items(node, *keys) -> list:
    for k in keys:
        node = node.get(k) if isinstance(node, dict) else None
    return node if isinstance(node, list) else []


def _label(x: dict) -> str:
    for k in ("sym", "ticker", "who", "name", "title"):
        v = x.get(k)
        if isinstance(v, str) and v and v not in (x.get("summary") or ""):
            return f"{v[:60]}: "
    return ""


def _seen_tag(x: dict) -> str:
    """' [seen 3x since Sep 28; size +$20.0M since last seen]' for repeats, '' otherwise."""
    if x.get("new") is not False:
        return ""
    from lab.memory import day
    tag = f"seen {x.get('times_seen')}x since {day(x.get('first_seen'))}"
    return f" [{tag}; {x['delta']} since last seen]" if x.get("delta") else f" [{tag}]"


def _line(x: dict, tag_new: bool = True) -> str:
    url = x.get("url") or x.get("tx") or ""
    new = "NEW " if tag_new and x.get("new") is True else ""   # in an only-new summary every line is new
    return f"  - {new}{_label(x)}{x.get('summary', '')}{_seen_tag(x)}" + (f"\n    {url}" if url else "")


def summarize(d: dict, per_section: int = 3, only_new: bool = False) -> list[tuple[str, list[str]]]:
    """[(section title, lines)] with the most readable findings of a digest.

    only_new: keep only findings memory flagged as new (if memory did not run, keep everything)."""
    out: list[tuple[str, list[str]]] = []
    only_new = only_new and isinstance(d.get("memory"), dict)

    def add(title: str, rows: list, n: int = per_section) -> None:
        rows = [x for x in rows if isinstance(x, dict) and (not only_new or x.get("new") is True)]
        lines = [_line(x, tag_new=not only_new) for x in rows[:n] if isinstance(x, dict) and x.get("summary")]
        if lines:
            out.append((title, lines))

    p = d.get("perps") or {}
    add("Hyperliquid whale positions", _items(p, "hyperliquid", "positions"))
    board = _items(p, "hyperliquid", "pnl_board", "day_top")[:1] + _items(p, "hyperliquid", "pnl_board", "day_worst")[:1]
    add("Hyperliquid perp P&L, 24h (top / worst)", board)
    add("Stablecoin transfers >= $20M (Ethereum)", _items(p, "eth_transfers", "items"))

    m = d.get("memes") or {}
    add("Memecoins: sharpest 24h moves", _items(m, "gainers", "g24"))
    add("Memecoins: sharpest 1h moves", _items(m, "gainers", "g1"), 2)
    add("Paper market caps (cap >= 100x pool)", _items(m, "paper_mcap", "coins"), 2)
    add("Holder concentration (Solana)", _items(m, "holders"))
    pf = m.get("pumpfun") or {}
    pf_rows = [x for x in [pf.get("launches"), pf.get("graduated")] if isinstance(x, dict)]
    add("pump.fun", pf_rows + (pf.get("near_graduation") or []))

    s = d.get("signals") or {}
    add("Polymarket: fresh wallets with big wins", s.get("fresh_wallets") or [])
    add("Polymarket: precisely timed bets", s.get("timed_bets") or [])
    add("Polymarket: biggest losers (24h)", _items(s, "losers", "day"), 2)
    add("Polymarket: celebrity markets", s.get("celebrity_markets") or [])
    add("Stocks: daily moves >= 5%", s.get("stocks") or [])
    add("AI: trending models", _items(s, "ai", "hf_trending"))
    add("AI: Hacker News 24h", _items(s, "ai", "hn_24h"))

    w = d.get("polymarket") or {}
    bets = [{**b, "summary": f"{b.get('who') or 'wallet'} {b.get('buy_or_sell', '').lower()} {b.get('side')} "
                             f"for {net.usd(b.get('usd'))} at {b.get('price')} on \"{b.get('market')}\" "
                             f"({b.get('hours_ago')} h ago)",
             "url": f"polymarket.com/profile/{b.get('wallet')}"} for b in (w.get("big_bets_24h") or [])]
    add("Polymarket: big bets in 24h", bets)
    return out


def errors(d: dict) -> list[str]:
    """Sources that failed, as 'module.section: error'."""
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            if "error" in node and isinstance(node["error"], str):
                out.append(f"{path}: {node['error']}")
            for k, v in node.items():
                if k != "error" and isinstance(v, dict):
                    walk(v, f"{path}.{k}")
    for m in MODULES:
        if m in d:
            walk(d[m], m)
    return out


def memory_line(d: dict) -> str | None:
    m = d.get("memory")
    if not isinstance(m, dict):
        return None
    prev = f" (previous scan {m['previous_scan'][:16].replace('T', ' ')} UTC)" if m.get("previous_scan") else " (first scan)"
    return (f"Memory: {m.get('new')} new, {m.get('seen_before')} seen before, {m.get('changed')} changed materially"
            + prev)


def render(d: dict, per_section: int = 3) -> str:
    head = f"Luthor Lab scan - {d.get('collected_at')} - {d.get('took_s')} s - read-only public data"
    parts = [head, "=" * len(head)]
    if memory_line(d):
        parts.append(memory_line(d))
    for title, lines in summarize(d, per_section):
        parts.append(f"\n{title}")
        parts.extend(lines)
    errs = errors(d)
    if errs:
        parts.append("\nSources that failed or timed out:")
        parts.extend(f"  - {e}" for e in errs[:12])
    return "\n".join(parts)


# ---------------------------------------------------------------- memory

def remember(d: dict) -> dict | None:
    """Flag findings new / seen before and store them in data/lab.db. Never breaks a scan."""
    try:
        from lab import memory
        return memory.remember(d)
    except Exception as e:
        print(f"warning: memory not updated ({type(e).__name__}: {str(e)[:150]})", file=sys.stderr)
        return None


def outcome_pass(deadline: float) -> dict | None:
    """A few outcome checks with whatever is left of the scan budget. Never breaks a scan."""
    if deadline - time.time() < OUTCOME_MIN_LEFT_S:
        return None
    try:
        from lab import memory
        net.set_deadline(deadline)
        return memory.check_outcomes(max_checks=OUTCOME_MAX_CHECKS)
    except Exception as e:
        print(f"warning: outcome checks skipped ({type(e).__name__}: {str(e)[:150]})", file=sys.stderr)
        return None
    finally:
        net.set_deadline(None)


# ---------------------------------------------------------------- self-check

def dry() -> int:
    """Offline self-check. No network calls."""
    ok = True
    print(f"Python {sys.version.split()[0]}", "(ok)" if sys.version_info >= (3, 11) else "(need 3.11+)")
    ok &= sys.version_info >= (3, 11)
    for m in MODULES:
        try:
            importlib.import_module(f"lab.scanner.{m}")
            print(f"scanner.{m}: import ok")
        except Exception as e:
            ok = False
            print(f"scanner.{m}: import FAILED {type(e).__name__}: {e}")
    for m in ("lab.analyst", "lab.skeptic", "lab.tester.walkforward", "lab.oracle.bot", "lab.verifier.proof"):
        try:
            importlib.import_module(m)
            print(f"{m}: import ok")
        except Exception as e:
            print(f"{m}: import failed ({type(e).__name__}: {str(e)[:80]}) - optional module")
    d = config.data_dir()
    probe = d / ".write_test"
    try:
        probe.write_text("ok")
        probe.unlink()
        print(f"data dir writable: {d}")
    except Exception as e:
        ok = False
        print(f"data dir NOT writable: {d} ({e})")
    import shutil
    try:
        import playwright  # noqa: F401
        pw = "installed"
    except ImportError:
        pw = "not installed (only needed for VERIFIER)"
    print(f"optional: playwright {pw}")
    print(f"optional: ffmpeg {'found' if shutil.which('ffmpeg') else 'not found (only needed for VERIFIER video)'}")
    print(f"optional: claude CLI {'found' if config.claude_bin() else 'not found (VERIFIER check and SKEPTIC are skipped)'}")
    tg = "set" if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") else "not set (ORACLE disabled)"
    print(f"optional: Telegram settings {tg}")
    print(f"ORACLE_ALLOW_CLAUDE={'1 (Claude execution ENABLED in the bot)' if config.flag('ORACLE_ALLOW_CLAUDE') else '0 (off)'}")
    try:
        from lab import memory
        mem_ok, msg = memory.self_test()
    except Exception as e:
        mem_ok, msg = False, f"{type(e).__name__}: {e}"
    ok &= mem_ok
    print(f"memory (temp dir, offline): {'ok' if mem_ok else 'FAILED'} - {msg}")
    print("self-check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.scan", description="Read-only public market scan -> data/digest.json")
    ap.add_argument("--only", default="", help=f"comma-separated modules: {','.join(MODULES)}")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("LAB_SCAN_BUDGET_S") or DEFAULT_BUDGET_S),
                    help="time budget in seconds (default 75)")
    ap.add_argument("--out", type=Path, default=None, help="output path (default data/digest.json)")
    ap.add_argument("--dry", action="store_true", help="offline self-check, no network")
    ap.add_argument("--quiet", action="store_true", help="do not print the summary")
    a = ap.parse_args(argv)
    if a.dry:
        return dry()
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    bad = [x for x in only if x not in MODULES]
    if bad:
        ap.error(f"unknown module(s): {', '.join(bad)}; choose from {', '.join(MODULES)}")
    if not a.quiet:
        print(f"Scanning public sources ({', '.join(only or MODULES)}), budget {a.budget:.0f} s...", flush=True)
    t0 = time.time()
    d = run(only or None, a.budget)
    remember(d)
    path = save(d, a.out)
    checked = outcome_pass(t0 + a.budget)
    if not a.quiet:
        print(render(d))
        if checked and (checked["checked"] or checked["missed"]):
            print(f"\nOutcome checks: {checked['checked']} done, {checked['left']} pending "
                  f"(python -m lab.memory report)")
        print(f"\nSaved: {os.path.relpath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
