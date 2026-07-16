# Dicepunk / d2 Дідів — Podcast Publishing Pipeline

Автоматизація публікації подкасту: транскрипція → метадані → відео → Transistor + YouTube → Telegram.
Один фід, один Transistor/YouTube акаунт, один Telegram-канал — епізоди виходять під двома умовними серіями, **Dicepunk** і **d2 Дідів**, обраними прапорцем `--series` при `prepare`.
Повна специфікація й архітектура — у `podcast-pipeline-spec.md`.

## Встановлення

```bash
# рекомендовано uv:
uv sync
# або pip:
pip install -e .
```

Потрібен системний **ffmpeg**:
- macOS: `brew install ffmpeg`
- Ubuntu: `sudo apt install ffmpeg`

## Налаштування

1. `cp .env.example .env` і заповни ключі.
2. Відредагуй `config.yaml` (ID шоу Transistor, назви серій, канал Telegram, кольори хвилі тощо).
3. Наповни `templates/*.md` СВОЇМ реальним форматом і `games_reference.json` — своїми системами.
4. Для YouTube поклади `client_secret.json` у корінь (як отримати — у спеці, §4).

## Робочий цикл

```bash
# STAGE 1a — підготовка (нічого не публікує); --series: dicepunk | d2didiv
podpub prepare --audio ~/ep042.mp3 --episode 042 --series dicepunk

# → переглянь episodes/042/review.md
#   за потреби відредагуй episodes/042/metadata.json
#   глянь episodes/042/video.mp4

# STAGE 1b — публікація на Transistor + YouTube
podpub publish --episode 042

# STAGE 2 — Telegram (наступного дня, коли лінки платформ розійшлись)
podpub telegram --episode 042              # чернетка → episodes/042/telegram.md
podpub telegram --episode 042 --publish    # відправка в канал

# статус будь-коли
podpub status --episode 042
```

## Що доробити (TODO)

- Наповнити `templates/` і `prompts/` своїм голосом (зараз заготовки).
- Розширити `games_reference.json` повним списком систем.
- Звірити Transistor / YouTube API з актуальними доками (спека §13) — місця позначені `TODO` в коді (`transistor.py`, `youtube.py`).
- За бажання — deep links платформ у `links.py`.
- Замінити `assets/background_default.png` на свій брендовий фон.
