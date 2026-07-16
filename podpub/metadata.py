from __future__ import annotations

import json

from .config import ROOT, Config
from .llm import call_claude_json


def _load_prompt() -> str:
    return (ROOT / "prompts" / "generate_metadata.md").read_text(encoding="utf-8")


def _load_template(name: str) -> str:
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def generate_metadata(
    transcript: str, games: dict, series_name: str, ep_number: str, config: Config
) -> dict:
    system = _load_prompt()
    podcast_tpl = _load_template("podcast_description.md")
    youtube_tpl = _load_template("youtube_description.md")
    games_list = games.get("matched", []) + games.get("uncertain", [])
    user = (
        "СЕРІЯ ЕПІЗОДУ:\n" f"{series_name}\n\n"
        "НОМЕР ЕПІЗОДУ:\n" f"{ep_number}\n\n"
        "ФОРМАТ ОПИСУ ПОДКАСТУ (шаблон):\n" f"{podcast_tpl}\n\n"
        "ФОРМАТ ОПИСУ YOUTUBE (шаблон):\n" f"{youtube_tpl}\n\n"
        "ЗГАДАНІ ІГРИ:\n" f"{json.dumps(games_list, ensure_ascii=False)}\n\n"
        "ТРАНСКРИПТ:\n" f"{transcript}"
    )
    data = call_claude_json(system, user, config)

    # Фіксований блок (підтримка + ведучі) — однаковий у кожному епізоді,
    # тому додається в коді, а не генерується Claude (без ризику дрейфу тексту/лінків).
    boilerplate = _load_template("support_and_hosts.md").strip()
    data["podcast"]["description"] = data["podcast"]["description"].rstrip() + "\n\n" + boilerplate
    data["youtube"]["description"] = data["youtube"]["description"].rstrip() + "\n\n" + boilerplate
    return data
