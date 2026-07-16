from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(4))
def send_message(text: str, config: Config, preview_url: str = "") -> dict:
    token = Config.secret("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("telegram", "channel_id")
    parse_mode = config.get("telegram", "parse_mode", default="HTML")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        # link_preview_options.url фіксує, яке саме посилання показувати в прев'ю —
        # інакше Telegram бере перше-ліпше посилання з тексту (не завжди YouTube).
        "link_preview_options": {"url": preview_url, "prefer_large_media": True}
        if preview_url
        else {"is_disabled": False},
    }
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()
