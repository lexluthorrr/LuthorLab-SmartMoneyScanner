# Setup, step by step

This guide assumes no prior experience. Every step after step 4 is optional.

## 1. Python 3.11 or newer

```bash
python3 --version
```

If it prints 3.11 or higher, continue. Otherwise install Python from https://www.python.org/downloads/
(macOS: `brew install python@3.12`; Ubuntu/Debian: `sudo apt install python3 python3-venv`).

## 2. Get the code

```bash
git clone <this-repo-url> luthor-lab
cd luthor-lab
```

(Or download the ZIP from the repository page and unpack it.)

## 3. Virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

The scanner, analyst, tester and bot use only the Python standard library, so nothing else is
required for them.

## 4. First run

```bash
python -m lab.scan --dry                       # offline self-check
python -m lab.scan                             # real scan, ~1 minute -> data/digest.json
python -m lab.analyst                          # signals -> data/signals.json
python -m lab.tester.walkforward --demo        # walk-forward on a synthetic random walk
```

`--dry` must end with `self-check: OK`. A real scan prints a summary and `Saved: data/digest.json`.
If a source is down, it is listed under "Sources that failed"; that is normal.

Useful flags: `python -m lab.scan --only memes` (one module), `--budget 60` (time limit in seconds).

## 5. VERIFIER (optional): record a public Polymarket profile

```bash
pip install -r requirements.txt                # installs Playwright
python -m playwright install chromium          # downloads the browser (~150 MB)
```

Install ffmpeg for the video: macOS `brew install ffmpeg`, Ubuntu `sudo apt install ffmpeg`,
Windows https://ffmpeg.org/download.html.

```bash
python -m lab.verifier <polymarket-username-or-0x-address>
# -> data/proof/<name>/proof.mp4 and proof.png
```

## 6. Claude Code (optional): frame check and SKEPTIC

Install Claude Code (https://docs.claude.com/en/docs/claude-code) and log in once by running `claude`.
Then:

```bash
python -m lab.skeptic --top 3          # second opinion on the 3 strongest signals
```

The verifier uses the CLI automatically to check its frames. Without the CLI both steps are skipped.
If `claude` is not on your PATH, set `CLAUDE_BIN=/full/path/to/claude` in `.env`.
`LAB_CLAUDE_MODEL` picks a model; empty means the CLI default.

## 7. Telegram bot ORACLE (optional)

1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username.
   BotFather replies with a token like `123456789:AA...`. Treat it like a password.
2. Copy the settings template and open it in an editor:
   ```bash
   cp .env.example .env
   ```
   Put the token after `TELEGRAM_BOT_TOKEN=`.
3. **Find your chat id.** Send any message (for example `hi`) to your new bot. Then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in your browser and find
   `"chat":{"id":<number>`. Put that number after `TELEGRAM_CHAT_ID=`.
   Use your private chat with the bot, not a group: in a group anyone in it could send commands.
4. Start the bot:
   ```bash
   python -m lab.oracle
   ```
   In Telegram send `/help`, then `/scan`.

Settings in `.env`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | - | bot token from BotFather |
| `TELEGRAM_CHAT_ID` | - | the only chat the bot answers |
| `ORACLE_SCAN_EVERY_MIN` | 240 | scheduled summary interval, 0 = off |
| `ORACLE_ALLOW_CLAUDE` | 0 | 1 = free text goes to Claude Code. Read the warning in README first |
| `ORACLE_CLAUDE_MODE` | readonly | `readonly` = read files only; `full` = any command on this machine |

Without `.env` the bot exits with a short explanation. Messages from any other chat are ignored.

## 8. Run on a schedule (optional)

Replace `/path/to/luthor-lab` with the real folder (`pwd` prints it) and `YOUR_USER` with your
user name. Templates are in `examples/`.

### cron: scan every 2 hours

```bash
crontab -e
```

```cron
0 */2 * * * cd /path/to/luthor-lab && .venv/bin/python -m lab.scan --quiet >> data/scan.log 2>&1
```

### systemd: keep the bot running (Linux)

`/etc/systemd/system/luthor-oracle.service`:

```ini
[Unit]
Description=Luthor Lab ORACLE Telegram bot (read-only)
After=network-online.target
Wants=network-online.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/luthor-lab
ExecStart=/path/to/luthor-lab/.venv/bin/python -m lab.oracle
Restart=on-failure
RestartSec=30
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now luthor-oracle
journalctl -u luthor-oracle -f          # logs
```

A scan timer (`luthor-scan.service` + `luthor-scan.timer`) is in `examples/systemd/`.
The bot already scans on its own schedule, so you need either the bot or the timer, not both.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `self-check: FAILED` on Python version | install Python 3.11+ and recreate `.venv` |
| a source shows `HTTPError 429` | rate limit; run the scan less often |
| everything times out | check internet access / proxy; try `--budget 120` |
| `Executable doesn't exist` in VERIFIER | `python -m playwright install chromium` |
| bot: `HTTP 401` | wrong `TELEGRAM_BOT_TOKEN` |
| bot: `409` in `data/oracle/oracle.log` | another copy of the bot (or a webhook) uses the same token |
| bot does not answer | `TELEGRAM_CHAT_ID` is not your chat; check with `getUpdates` (step 7.3) |
