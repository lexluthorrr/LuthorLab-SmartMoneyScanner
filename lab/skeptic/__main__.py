"""python -m lab.skeptic [--top N] [--finding FILE --analysis TEXT]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lab import config
from lab.skeptic import review


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.skeptic", description="Second opinion: find the weakest point.")
    ap.add_argument("--top", type=int, default=3, help="review the N strongest signals from data/signals.json")
    ap.add_argument("--finding", type=Path, help="a JSON file with one finding (instead of signals.json)")
    ap.add_argument("--analysis", default="", help="your short analysis of the finding")
    a = ap.parse_args(argv)
    if not config.claude_bin():
        print("SKEPTIC skipped: Claude Code CLI not found. Install it or set CLAUDE_BIN; everything else works without it.")
        return 0
    if a.finding:
        items = [(json.loads(a.finding.read_text()), a.analysis)]
    else:
        path = config.data_dir() / "signals.json"
        if not path.exists():
            sys.exit("No data/signals.json. Run `python -m lab.scan` and `python -m lab.analyst` first.")
        sigs = json.loads(path.read_text())[: a.top]
        items = [(s, s.get("why_it_matters", "")) for s in sigs]
    results = []
    for finding, analysis in items:
        title = finding.get("headline") if isinstance(finding, dict) else str(finding)[:80]
        print(f"- {title}", flush=True)
        res = review(finding, analysis)
        results.append({"finding": finding, "skeptic": res})
        print("  " + json.dumps(res, ensure_ascii=False))
    out = config.data_dir() / "skeptic.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\nSaved {len(results)} reviews to {out.name} in the data folder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
