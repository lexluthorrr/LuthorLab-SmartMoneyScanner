"""Minimal Telegram Bot API client (stdlib only). Sends plain text; batches without notification spam."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 3800          # Telegram allows 4096 chars; keep headroom


class Telegram:
    def __init__(self, token: str):
        self._api = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, timeout: float = 70, **params) -> dict:
        data = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"{self._api}/{method}", data=data), timeout=timeout) as r:
            return json.loads(r.read())

    def send(self, chat_id: int, text: str, silent: bool = False) -> None:
        params = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": "true"}
        if silent:
            params["disable_notification"] = "true"
        self.call("sendMessage", **params)

    def send_batch(self, chat_id: int, parts: list[str]) -> None:
        """Send several messages in a row: all silent except the last one (one notification per batch)."""
        msgs = [c for p in parts for c in chunks(p)]
        for i, m in enumerate(msgs):
            self.send(chat_id, m, silent=i < len(msgs) - 1)


def chunks(text: str, limit: int = LIMIT):
    """Split long text on line breaks into Telegram-sized pieces."""
    text = text or "(empty)"
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text.strip():
        yield text


def pack(sections: list[str], limit: int = LIMIT) -> list[str]:
    """Join short sections into as few messages as possible."""
    out, cur = [], ""
    for s in sections:
        if cur and len(cur) + 2 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur}\n\n{s}" if cur else s
    if cur:
        out.append(cur)
    return out
