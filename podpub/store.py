from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

EPISODES_DIR = ROOT / "episodes"


class EpisodeStore:
    """Робоча тека епізоду = механізм чекпоінтів. Кожен крок пише свій файл;
    повторний запуск пропускає крок, якщо файл уже є (крім --force)."""

    def __init__(self, episode_id: str):
        self.id = episode_id
        self.dir = EPISODES_DIR / episode_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, filename: str) -> Path:
        return self.dir / filename

    def exists(self, filename: str) -> bool:
        return self.path(filename).exists()

    def read_text(self, filename: str) -> str:
        return self.path(filename).read_text(encoding="utf-8")

    def write_text(self, filename: str, content: str) -> Path:
        p = self.path(filename)
        p.write_text(content, encoding="utf-8")
        return p

    def load_json(self, filename: str) -> dict:
        return json.loads(self.read_text(filename))

    def save_json(self, filename: str, data: dict) -> Path:
        return self.write_text(filename, json.dumps(data, ensure_ascii=False, indent=2))
