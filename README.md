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

## Найпростіше: веб-інтерфейс

Подвійний клік по **`Podpub.command`** у Finder → відкриється вікно Terminal (не закривай його, поки працюєш)
і браузер з інтерфейсом `http://127.0.0.1:8765`. Далі все кнопками: новий випуск → перевірка описів і відео →
публікація (зараз або за розкладом) → пост у Telegram (зараз або за розкладом).
Інтерфейс доступний лише з цього Mac. Те саме з терміналу: `podpub web`.

Запланований пост у Telegram надсилає сам Mac: у потрібний час відкриється вікно Terminal і відправить пост.
Mac має бути увімкненим і залогіненим; якщо він спить — пост піде після пробудження (протягом доби).

## Робочий цикл (термінал)

```bash
# STAGE 1a — підготовка (нічого не публікує); --series: dicepunk | d2didiv
podpub prepare --audio ~/ep042.mp3 --episode 042 --series dicepunk

# → переглянь episodes/042/review.md
#   за потреби відредагуй episodes/042/metadata.json
#   глянь episodes/042/video.mp4

# STAGE 1b — публікація на Transistor (live) + YouTube (unlisted за замовчуванням — config.youtube.privacy_status)
podpub publish --episode 042

# → переглянь unlisted відео на YouTube, тоді:
podpub youtube-go-public --episode 042      # переводить у public, без переаплоаду

# STAGE 2 — Telegram (наступного дня, коли лінки платформ розійшлись)
podpub telegram --episode 042              # чернетка → episodes/042/telegram.md
podpub telegram --episode 042 --publish    # відправка в канал

# статус будь-коли
podpub status --episode 042
```

## Як влаштовано

- `podpub/pipeline.py` — уся логіка кроків (одна для CLI й інтерфейсу); кожен крок ідемпотентний і
  пише свій результат у `episodes/<id>/`, тож обірвана дія продовжується з місця зупинки.
- `podpub/cli.py` — термінальні команди; `podpub/web/` — локальний веб-інтерфейс (FastAPI + статичні файли).
- `podpub/scheduler.py` — відкладені дії через `launchd` (запланований Telegram-пост).
