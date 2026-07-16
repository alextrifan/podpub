from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(4))
def send_message(text: str, config: Config) -> dict:
    token = Config.secret("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("telegram", "channel_id")
    parse_mode = config.get("telegram", "parse_mode", default="HTML")
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
