"""ANALYST command line.

  python -m lab.analyst                         signals from data/digest.json -> data/signals.json
  python -m lab.analyst signals --digest FILE   same, from another digest
  python -m lab.analyst check "text" [--sources FILE]
                                                numbers in the text that are not in the sources
                                                (default sources: data/digest.json)
  python -m lab.analyst demo                    run the number check on a built-in example
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lab import config
from lab.analyst import from_digest, render, unsourced_numbers

DEMO_SOURCES = """{"wallet": "example", "invested_usd": 499699, "profit_usd": 517372,
 "total_pnl_usd": 1127059, "entry_price": 0.625, "trades": 48}"""
DEMO_TEXT = ("This wallet is up $1.1M all time. It put in $499,699 and got $1,017,071 back, "
             "buying at 62.5 cents over 48 trades. Its best month made $2.4M.")


def _digest(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No digest at {path}. Run `python -m lab.scan` first.")
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.analyst")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("signals", help="signals from a digest")
    s.add_argument("--digest", type=Path, default=None)
    s.add_argument("--limit", type=int, default=15)
    c = sub.add_parser("check", help="find numbers without a source")
    c.add_argument("text")
    c.add_argument("--sources", type=Path, default=None,
                   help="any text/JSON file (default data/digest.json). Prefer the narrowest source, e.g. one "
                        "finding: the more numbers in the sources, the more a wrong number can match by chance")
    sub.add_parser("demo", help="number check on a built-in example")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        src = a.sources or config.data_dir() / "digest.json"
        if not src.exists():
            sys.exit(f"No sources file at {src}.")
        bad = unsourced_numbers(a.text, src.read_text())
        print("All numbers trace to the sources." if not bad else "Numbers without a source: " + ", ".join(bad))
        return 1 if bad else 0

    if a.cmd == "demo":
        print("Sources:", DEMO_SOURCES.replace("\n", ""))
        print("Text:   ", DEMO_TEXT)
        bad = unsourced_numbers(DEMO_TEXT, DEMO_SOURCES)
        print("\n$1.1M matches 1,127,059 after rounding; $1,017,071 = 499,699 + 517,372; 62.5 cents = 0.625.")
        print("Numbers without a source:", ", ".join(bad) if bad else "none")
        return 0

    path = getattr(a, "digest", None) or config.data_dir() / "digest.json"
    sigs = from_digest(_digest(path))
    out = config.data_dir() / "signals.json"
    out.write_text(json.dumps(sigs, ensure_ascii=False, indent=1))
    print(render(sigs, getattr(a, "limit", 15)))
    print(f"\n{len(sigs)} signals saved to {out.relative_to(config.ROOT) if out.is_relative_to(config.ROOT) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
