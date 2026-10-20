#!/bin/bash
# Подвійний клік у Finder — запускає веб-інтерфейс podpub і відкриває його в браузері.
# Поки працюєш з інтерфейсом, це вікно терміналу має лишатися відкритим.
cd "$(dirname "$0")" || exit 1
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
source .venv/bin/activate
exec podpub web
