"""python -m lab.verifier <username or 0x address> [--out DIR] [--tab Active|Closed] [--no-check]"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from lab import config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.verifier",
                                 description="Record a public Polymarket profile as seen on a phone (read-only).")
    ap.add_argument("handle", help="Polymarket username (without @) or 0x proxy wallet address")
    ap.add_argument("--out", type=Path, default=None, help="output folder (default data/proof/<handle>)")
    ap.add_argument("--tab", choices=["Active", "Closed"], default=None, help="positions tab to show")
    ap.add_argument("--no-check", action="store_true", help="skip the model check of the frames")
    a = ap.parse_args(argv)
    try:
        from lab.verifier import proof
        url = proof.profile_url(a.handle)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("error: Playwright is not installed. Run: pip install playwright && python -m playwright install chromium",
              file=sys.stderr)
        return 2
    out = a.out or config.data_dir() / "proof" / a.handle.lstrip("@").lower()
    print(f"Recording {url} -> {out}")
    try:
        res = asyncio.run(proof.make(a.handle, out, a.tab))
    except Exception as e:
        print(f"error: recording failed: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        if "Executable doesn't exist" in str(e):
            print("hint: run `python -m playwright install chromium`", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=1))
    if not a.no_check:
        print("Frame check:", json.dumps(proof.verify(out, res.get("marks"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
