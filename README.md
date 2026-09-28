# Luthor Lab — Smart Money Scanner

A free console scanner that shows where big money is moving in crypto, from public data only.

It finds:
- Hyperliquid whale positions close to liquidation, and the biggest 24h winners and losers
- Fresh Polymarket wallets with big wins, and large bets placed at long odds
- Memecoins whose market cap is 100x+ bigger than their pool, and holders with identical balances
- USDT / USDC transfers of $20M+ on Ethereum, pump.fun launch and dump stats
- Plus a walk-forward tester to check a trading idea before any money goes in

Read-only: no API keys, no wallets, no trading, no posting.

![SCANNER, ANALYST and ORACLE pixel agents](docs/agents.png)

## Requirements

- Python **3.11+** (https://www.python.org/downloads/)
- Git (optional, https://git-scm.com/downloads). You can also download the ZIP.
- A terminal: Terminal on macOS, PowerShell on Windows, any shell on Linux

Optional extras (only for the parts that use them):
- Phone-style profile recording (VERIFIER): Chromium via Playwright + ffmpeg
- Telegram alerts (ORACLE): a bot token from @BotFather
- Second opinion on signals (SKEPTIC): Claude Code installed and logged in

## Run from terminal (step-by-step)

### 1) Clone the repository

```bash
git clone https://github.com/lexluthorrr/LuthorLab-SmartMoneyScanner luthor-lab
cd luthor-lab
```

Alternative (no git):
- Click the green `<> Code` button on GitHub
- Choose `Download ZIP` and extract it
- Open a terminal in the extracted folder

### 2) Create a virtual environment

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

The scanner itself needs no `pip install`: it uses the Python standard library only.

### 3) Check that it works (offline)

```bash
python -m lab.scan --dry
```

You should see `self-check: OK`.

### 4) Run the scanner

```bash
python -m lab.scan
```

Takes about a minute. It prints a summary and saves everything to `data/digest.json`.

Only one part:

```bash
python -m lab.scan --only perps        # or: memes, polymarket, signals
```

### 5) Turn findings into signals

```bash
python -m lab.analyst signals
```

### 6) Test a trading idea before money

```bash
python -m lab.tester.walkforward --demo              # example on synthetic data
python -m lab.tester.walkforward --fetch BTC-USD     # real BTC candles from Coinbase
python -m lab.tester.walkforward your_prices.csv     # your own price file
```

Fees and slippage are flags: `--fee-bps 10 --slippage-bps 5`.

### 7) (Optional) Record a Polymarket profile like on a phone

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m lab.verifier <polymarket-username>
```

Needs ffmpeg installed on your system.

### 8) (Optional) Telegram alerts

```bash
cp .env.example .env
```

Open `.env`, paste your bot token after `TELEGRAM_BOT_TOKEN=` and your chat id after `TELEGRAM_CHAT_ID=`, then:

```bash
python -m lab.oracle
```

The bot answers only your chat. How to get the token and your chat id: [SETUP.md](SETUP.md).

## Or ask your AI to install it

Paste this into Claude Code, Codex or a similar assistant opened in an empty folder:

```text
Clone https://github.com/lexluthorrr/LuthorLab-SmartMoneyScanner into ./luthor-lab and set it up for me.
Read AGENTS.md first and follow it exactly.
1. Check that Python 3.11+ is available and create a virtual environment in .venv.
2. Run `python -m lab.scan --dry`, then `python -m lab.scan`, then `python -m lab.analyst signals`
   and `python -m lab.tester.walkforward --demo`. Show me the output.
3. Do not add API keys, wallets or trading code. Do not enable ORACLE_ALLOW_CLAUDE.
   Do not install system services or cron jobs unless I confirm.
4. If I want the Telegram bot, create .env from .env.example and tell me which two values
   to fill in myself; do not ask me to paste the token into this chat.
Summarize what works and what was skipped.
```

## Safety

- Only public, read-only requests. Nothing is signed, bought, sold or posted.
- No keys or logins are needed for any data source.
- `.env` and `data/` stay on your machine (they are in `.gitignore`).
- The Telegram bot answers only the chat id you set and ignores old messages.
- **Warning:** `ORACLE_ALLOW_CLAUDE=1` forwards your chat messages to Claude Code on your machine.
  It is off by default. `ORACLE_CLAUDE_MODE=full` lets Claude run any command with your user's
  permissions: use it only on a machine you are ready to lose, never on one with keys or funds.

Signals are prompts to check something, not trade ideas. Nothing here is financial advice.
Known limits and troubleshooting: [SETUP.md](SETUP.md).

## Support the project

Luthor Lab is free. If it saved you time:

- ⭐ **Star the repo.** It's the only way others find it.
- **Follow [@mr_luth0r](https://x.com/mr_luth0r) on X.** New agents, test results and write-ups land there first.
- **Share what your scanner finds** in the replies. The best finds get a shout-out.

## License

MIT
