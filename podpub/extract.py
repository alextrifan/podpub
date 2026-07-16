from __future__ import annotations

import json

from .config import ROOT, Config
from .llm import call_claude_json


def _load_prompt() -> str:
    return (ROOT / "prompts" / "extract_games.md").read_text(encoding="utf-8")


def _load_reference() -> list[dict]:
    return json.loads((ROOT / "games_reference.json").read_text(encoding="utf-8"))


def extract_games(transcript: str, config: Config) -> dict:
    """Повертає {"matched": [...], "uncertain": [...]}. 'uncertain' іде в review для перевірки."""
    system = _load_prompt()
    reference = _load_reference()
    user = (
        "ДОВІДНИК ВІДОМИХ СИСТЕМ (JSON):\n"
        f"{json.dumps(reference, ensure_ascii=False)}\n\n"
        "ТРАНСКРИПТ:\n"
        f"{transcript}"
    )
    return call_claude_json(system, user, config)
