from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path = ROOT / "config.yaml") -> "Config":
        load_dotenv(ROOT / ".env")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Зручний доступ: config.get('video', 'resolution')."""
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @staticmethod
    def secret(name: str, required: bool = True) -> str:
        """Секрети — лише з ENV, ніколи з config.yaml."""
        val = os.getenv(name, "")
        if required and not val:
            raise RuntimeError(f"Missing required env var: {name} (див. .env.example)")
        return val
