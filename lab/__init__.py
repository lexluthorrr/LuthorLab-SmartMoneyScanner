"""Luthor Lab: a read-only crypto market research lab built from small AI-agent roles.

SCANNER  - pulls public market data (no keys) into data/digest.json
ANALYST  - turns findings into plain-language signals and flags unsourced numbers
TESTER   - walk-forward template and the 6-filter checklist for trading ideas
VERIFIER - records a public Polymarket profile and (optionally) has a model check the frames
SKEPTIC  - (optional) a second model call that looks for the weak spot in a finding
ORACLE   - (optional) a Telegram bot locked to one chat that sends scan summaries
"""

__version__ = "0.1.0"
