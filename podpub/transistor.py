from __future__ import annotations

import html
import re
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config

BASE = "https://api.transistor.fm/v1"

_URL_RE = re.compile(r"(https?://[^\s<]+)")
_BULLET_PREFIXES = ("—", "•", "-")

_retry = retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(4))


def _linkify(text: str) -> str:
    return _URL_RE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', text)


def _text_to_html(text: str) -> str:
    """Плейн-текст (абзаци через порожній рядок, буллети через —/•) → проста HTML-розмітка.

    Transistor документує description як HTML-поле — без розмітки текст рендериться
    одним суцільним блоком без переносів/списків.
    """
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    parts = []
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if lines and all(ln.startswith(_BULLET_PREFIXES) for ln in lines):
            items = "".join(
                f"<li>{_linkify(html.escape(ln.lstrip('—•- ').strip()))}</li>" for ln in lines
            )
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append(f"<p>{'<br>'.join(_linkify(html.escape(ln)) for ln in lines)}</p>")
    return "\n".join(parts)


class TransistorClient:
    def __init__(self, config: Config):
        self.config = config
        self.key = Config.secret("TRANSISTOR_API_KEY")
        self.show_id = str(config.get("show", "transistor_show_id", default=""))

    @property
    def _headers(self) -> dict:
        return {"x-api-key": self.key}

    @_retry
    def authorize_upload(self, filename: str) -> dict:
        """1) Отримати pre-signed URL. Очікувано повертає upload_url + audio_url."""
        r = httpx.get(
            f"{BASE}/episodes/authorize_upload",
            params={"filename": filename},
            headers=self._headers,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["data"]["attributes"]

    @_retry
    def upload_audio(self, upload_url: str, audio_path: str) -> None:
        """2) Залити байти аудіо на pre-signed URL."""
        data = Path(audio_path).read_bytes()
        r = httpx.put(upload_url, content=data, headers={"Content-Type": "audio/mpeg"}, timeout=None)
        r.raise_for_status()

    @_retry
    def create_episode(
        self,
        audio_url: str,
        title: str,
        summary: str,
        description: str,
        keywords: str,
        author: str = "",
        number: int | None = None,
        image_url: str = "",
    ) -> dict:
        """3) Створити епізод (чернетку)."""
        payload = {
            "episode[show_id]": self.show_id,
            "episode[audio_url]": audio_url,
            "episode[title]": title,
            "episode[summary]": summary,
            "episode[description]": _text_to_html(description),
            "episode[keywords]": keywords,
        }
        if author:
            payload["episode[author]"] = author
        if number is not None:
            payload["episode[number]"] = number
        if image_url:
            payload["episode[image_url]"] = image_url
        r = httpx.post(f"{BASE}/episodes", data=payload, headers=self._headers, timeout=60)
        r.raise_for_status()
        return r.json()["data"]

    @_retry
    def update_episode(self, episode_id: str, **fields: str) -> dict:
        """PATCH довільних полів епізоду (напр. video_url після завантаження на YouTube)."""
        payload = {f"episode[{k}]": v for k, v in fields.items()}
        r = httpx.patch(f"{BASE}/episodes/{episode_id}", data=payload, headers=self._headers, timeout=60)
        r.raise_for_status()
        return r.json()["data"]

    @_retry
    def publish_episode(self, episode_id: str) -> dict:
        """4) Опублікувати."""
        payload = {"episode[status]": "published"}
        r = httpx.patch(
            f"{BASE}/episodes/{episode_id}/publish", data=payload, headers=self._headers, timeout=60
        )
        r.raise_for_status()
        return r.json()["data"]

    @_retry
    def schedule_episode(self, episode_id: str, published_at: str) -> dict:
        """4b) Запланувати на майбутню дату/час замість негайної публікації.

        published_at — "YYYY-MM-DD HH:MM:SS" у таймзоні шоу (формат з доків Transistor).
        """
        payload = {"episode[status]": "scheduled", "episode[published_at]": published_at}
        r = httpx.patch(
            f"{BASE}/episodes/{episode_id}/publish", data=payload, headers=self._headers, timeout=60
        )
        r.raise_for_status()
        return r.json()["data"]
