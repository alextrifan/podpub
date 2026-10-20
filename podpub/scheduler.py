"""Одноразові відкладені дії на цьому Mac (напр. запланований пост у Telegram).

Як це працює: user LaunchAgent (без sudo) у потрібний час відкриває вікно Terminal
зі згенерованим скриптом. Саме через Terminal — бо фонові процеси launchd не мають
доступу до теки Documents (захист приватності macOS), а Terminal має: з нього
запускається podpub. Вікно лишається відкритим із результатом.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SCRIPTS_DIR = Path.home() / "Library" / "Application Support" / "podpub" / "scheduled"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def _script_path(label: str) -> Path:
    return SCRIPTS_DIR / f"{label}.command"


def schedule_local_command(label: str, when: datetime, command: str) -> Path:
    """Запускає `command` (shell) у вікні Terminal у вказаний локальний час.
    Повторний виклик з тим самим label переплановує завдання."""
    cancel_local_command(label)
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    script = _script_path(label)
    script.write_text(f'#!/bin/bash\n{command}\nrm -f "$0"\n', encoding="utf-8")
    script.chmod(0o700)

    plist_path = _plist_path(label)
    # launchd не знає про рік: календарне завдання, пропущене через вимкнений Mac,
    # спрацювало б через рік того ж дня. Тому відкриваємо скрипт лише протягом доби
    # після запланованого часу (сон Mac — ок: launchd запускає пропущене при пробудженні).
    deadline = int(when.timestamp()) + 24 * 3600
    q_script, q_plist = shlex.quote(str(script)), shlex.quote(str(plist_path))
    launcher = (
        f'if [ "$(date +%s)" -lt {deadline} ]; then /usr/bin/open -a Terminal {q_script}; '
        f"else rm -f {q_script}; fi; "
        # rm МУСИТЬ бути перед bootout: bootout себе самого миттєво вбиває процес.
        f"rm -f {q_plist}; launchctl bootout {_domain()}/{label} 2>/dev/null"
    )
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "-c", launcher],
        "StartCalendarInterval": {
            "Month": when.month,
            "Day": when.day,
            "Hour": when.hour,
            "Minute": when.minute,
        },
    }
    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", _domain(), str(plist_path)], check=True)
    return plist_path


def cancel_local_command(label: str) -> bool:
    """Скасовує заплановане завдання, якщо воно є. Повертає True, якщо було що скасовувати."""
    plist_path = _plist_path(label)
    existed = plist_path.exists()
    # Сам скрипт може саме зараз виконуватися в Terminal — видалення файлу йому не
    # заважає (bash тримає його відкритим).
    _script_path(label).unlink(missing_ok=True)
    plist_path.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{label}"], capture_output=True)
    return existed
