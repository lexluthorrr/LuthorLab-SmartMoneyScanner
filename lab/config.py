"""Paths and settings. Everything is relative to the repo or set via environment variables."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    """Minimal .env reader (KEY=VALUE lines). Existing environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


load_env()


def data_dir() -> Path:
    d = Path(os.environ.get("LAB_DATA_DIR") or ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def claude_bin() -> str | None:
    """Path to the Claude Code CLI, if installed. Override with CLAUDE_BIN."""
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def claude_model_args() -> list[str]:
    """Optional --model override (LAB_CLAUDE_MODEL); empty means the CLI default."""
    m = os.environ.get("LAB_CLAUDE_MODEL", "").strip()
    return ["--model", m] if m else []
