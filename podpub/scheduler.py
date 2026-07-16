from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def schedule_local_command(label: str, when: datetime, command: str) -> Path:
    """Одноразовий запуск shell-команди в конкретний локальний час через launchd
    (user LaunchAgent — без sudo). Агент сам вивантажується й видаляє свій plist
    одразу після спрацювання, щоб не лишався в системі."""
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"
    log_path = plist_path.with_suffix(".log")
    err_path = plist_path.with_suffix(".err.log")

    # rm МУСИТЬ бути перед bootout: bootout себе самого вбиває процес одразу,
    # обриваючи будь-які наступні команди в тому ж ланцюжку.
    cleanup = f"rm -f {plist_path} {log_path} {err_path}; launchctl bootout gui/{os.getuid()}/{label} 2>/dev/null"
    full_command = f"({command}) ; {cleanup}"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>{full_command}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Year</key><integer>{when.year}</integer>
        <key>Month</key><integer>{when.month}</integer>
        <key>Day</key><integer>{when.day}</integer>
        <key>Hour</key><integer>{when.hour}</integer>
        <key>Minute</key><integer>{when.minute}</integer>
    </dict>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{err_path}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=True
    )
    return plist_path
