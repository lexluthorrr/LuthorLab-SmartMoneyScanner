# Claude Code notes for Luthor Lab

Read [AGENTS.md](AGENTS.md) and follow it: it lists what you may do, what you must not do,
and how to verify the installation.

Short version:

- This is a read-only research lab. No trading, no wallets, no keys, no posting.
- Quick check: `python -m lab.scan --dry` must print `self-check: OK`; a real `python -m lab.scan`
  writes `data/digest.json` in under ~90 seconds.
- Never enable `ORACLE_ALLOW_CLAUDE` or `ORACLE_CLAUDE_MODE=full` unless the user explicitly asks
  after reading the warning in README.md.
- Never read or print `.env`; never ask for tokens in chat.
