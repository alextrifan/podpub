from __future__ import annotations

import json
import re

from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


@retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(4))
def call_claude_text(system: str, user: str, config: Config) -> str:
    client = Anthropic(api_key=Config.secret("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=config.get("claude", "model", default="claude-sonnet-5"),
        max_tokens=config.get("claude", "max_tokens", default=4000),
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "disabled"},
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def call_claude_json(system: str, user: str, config: Config) -> dict:
    raw = call_claude_text(system, user, config)
    # strict=False: Claude інколи повертає буквальні переноси рядків усередині
    # JSON string-значень (напр. у description) замість екранованих \n.
    return json.loads(_strip_fences(raw), strict=False)
