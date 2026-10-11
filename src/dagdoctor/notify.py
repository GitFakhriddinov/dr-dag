"""Send reports to Telegram (optional)."""

from __future__ import annotations

import json
import os
import urllib.request

TELEGRAM_LIMIT = 4000


def chunks(text: str, size: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line breaks so no message exceeds Telegram's length limit."""
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None) -> None:
    token = token or os.environ["DAGDOCTOR_TELEGRAM_TOKEN"]
    chat_id = chat_id or os.environ["DAGDOCTOR_TELEGRAM_CHAT_ID"]
    for part in chunks(text):
        body = json.dumps({"chat_id": chat_id, "text": part}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30):
            pass
