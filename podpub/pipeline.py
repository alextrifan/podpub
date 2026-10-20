"""Ядро конвеєра: кроки prepare / publish / telegram без прив'язки до інтерфейсу.

Кожна функція приймає `log(message, kind)` — CLI друкує це в термінал, веб-інтерфейс
показує в живому журналі. kind: "info" | "ok" | "warn". Помилки, які людина може
зрозуміти й виправити сама, кидаються як PipelineError з людським поясненням.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import ROOT, Config
from .extract import extract_games
from .links import resolve_links
from .llm import call_claude_text
from .metadata import generate_metadata
from .scheduler import cancel_local_command, schedule_local_command
from .store import EPISODES_DIR, EpisodeStore
from .telegram import send_message
from .transcribe import get_transcriber
from .transistor import TransistorClient
from .video import render_thumbnail, render_video
from .youtube import YouTubeClient

Log = Callable[[str, str], None]

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M:%S"
GAMES_REFERENCE = ROOT / "games_reference.json"
PREPARE_FILES = ["transcript.txt", "games.json", "metadata.json", "video.mp4", "thumbnail.jpg"]


class PipelineError(Exception):
    """Помилка, яку можна пояснити людині без читання traceback."""


def _print_log(message: str, kind: str = "info") -> None:
    print(message)


def _now_iso() -> str:
    return datetime.now().astimezone().strftime(SCHEDULE_FORMAT)


def parse_schedule(value: str) -> tuple[datetime, str, str]:
    """Один локальний час → (datetime, формат для Transistor, ISO8601 UTC для YouTube)."""
    try:
        dt_local = datetime.strptime(value.strip(), SCHEDULE_FORMAT).astimezone()
    except ValueError:
        raise PipelineError(f"Не вдалося прочитати час «{value}». Формат: 2026-07-20 09:00:00")
    if dt_local <= datetime.now().astimezone():
        raise PipelineError(f"Час {value} уже минув — обери момент у майбутньому.")
    transistor_at = dt_local.strftime(SCHEDULE_FORMAT)
    youtube_at = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt_local, transistor_at, youtube_at


def episode_number(episode: str) -> int | None:
    return int(episode) if episode.isdigit() else None


def series_options(cfg: Config) -> dict[str, str]:
    return cfg.get("show", "series", default={}) or {}


def episode_exists(episode: str) -> bool:
    return (EPISODES_DIR / episode).is_dir()


def list_episodes() -> list[str]:
    if not EPISODES_DIR.exists():
        return []
    ids = [p.name for p in EPISODES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]
    numeric = sorted((e for e in ids if e.isdigit()), key=int, reverse=True)
    return numeric + sorted(e for e in ids if not e.isdigit())


def _existing_audio(store: EpisodeStore) -> Path | None:
    if store.exists("state.json"):
        name = store.load_json("state.json").get("audio_filename")
        if name and store.exists(name):
            return store.path(name)
    found = sorted(store.dir.glob("audio.*"))
    return found[0] if found else None


LOCK_FILE = ".lock"


def _cli_process_for(episode: str) -> int | None:
    """PID термінальної команди podpub, що працює з цим епізодом (на випадок, якщо
    вона запущена без файлу-замка, напр. старішою версією коду)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    except OSError:
        return None
    # Шлях до проєкту містить пробіли, тож шукаємо шаблоном, а не розбиттям на аргументи.
    pattern = re.compile(
        rf"(?:^|/)podpub (?:prepare|publish|telegram)\b.*--episode {re.escape(episode)}(?:\s|$)"
    )
    for line in out.splitlines():
        pid_str, _, command = line.strip().partition(" ")
        if pattern.search(command) and pid_str.isdigit() and int(pid_str) != os.getpid():
            return int(pid_str)
    return None


def _lock_owner(store: EpisodeStore) -> int | None:
    """PID процесу, що зараз обробляє епізод (якщо він ще живий)."""
    lock = store.path(LOCK_FILE)
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return _cli_process_for(store.id)
    except PermissionError:
        return pid
    return pid if pid != os.getpid() else _cli_process_for(store.id)


@contextmanager
def _episode_lock(store: EpisodeStore):
    if (pid := _lock_owner(store)) is not None:
        raise PipelineError(
            f"Цей епізод зараз обробляється в іншому вікні (процес {pid}). "
            "Дочекайся, поки там завершиться."
        )
    lock = store.path(LOCK_FILE)
    lock.write_text(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _store(episode: str) -> EpisodeStore:
    """EpisodeStore для вже наявного епізоду (без створення порожньої теки через описку)."""
    if not episode_exists(episode):
        raise PipelineError(f"Випуску «{episode}» немає. Перевір номер.")
    return EpisodeStore(episode)


def _load_published(store: EpisodeStore) -> dict:
    return store.load_json("published.json") if store.exists("published.json") else {}


# ── STAGE 1a: підготовка ────────────────────────────────────────────────────
def _prepare_impl(
    episode: str,
    series: str,
    audio: str | None = None,
    title_hint: str = "",
    bg: str = "",
    force: bool = False,
    log: Log = _print_log,
) -> None:
    """Транскрибація → витяг ігор → метадані → відео → review.md. Нічого не публікує."""
    cfg = Config.load()
    names = series_options(cfg)
    if series not in names:
        raise PipelineError(f"Невідома серія «{series}». Доступні: {', '.join(names) or '—'}")
    series_name = names[series]
    store = EpisodeStore(episode)
    lang = cfg.get("show", "language", default="uk")

    # 0. аудіо в теці епізоду (щоб publish не залежав від оригінального шляху)
    if audio:
        src = Path(audio).expanduser()
        if not src.is_file():
            raise PipelineError(f"Аудіофайл не знайдено: {src}")
        audio_local = store.path(f"audio{src.suffix.lower() or '.mp3'}")
        if src.resolve() != audio_local.resolve() and (force or not audio_local.exists()):
            log("→ Копіюю аудіо в теку епізоду...", "info")
            shutil.copy2(src, audio_local)
    else:
        audio_local = _existing_audio(store)
        if audio_local is None:
            raise PipelineError("Для цього епізоду ще немає аудіофайлу.")

    state = store.load_json("state.json") if store.exists("state.json") else {}
    state.update(
        audio_filename=audio_local.name,
        title_hint=title_hint or state.get("title_hint", ""),
        series=series,
        series_name=series_name,
    )
    store.save_json("state.json", state)

    # 1. транскрипція
    if force or not store.exists("transcript.txt"):
        log("→ Розшифровую аудіо в текст (найдовший крок разом із відео)...", "info")
        store.write_text("transcript.txt", get_transcriber(cfg).transcribe(str(audio_local), lang))
        log("✔ Розшифровка готова", "ok")
    else:
        log("• Розшифровка вже є, пропускаю", "info")
    transcript = store.read_text("transcript.txt")

    # 2. витяг + звірка ігор (Claude)
    if force or not store.exists("games.json"):
        log("→ Шукаю згадані ігри (Claude)...", "info")
        store.save_json("games.json", extract_games(transcript, cfg))
        log("✔ Ігри знайдено", "ok")
    else:
        log("• Список ігор уже є, пропускаю", "info")
    games = store.load_json("games.json")

    # 3. метадані (Claude)
    ep_number = str(int(episode)) if episode.isdigit() else episode
    if force or not store.exists("metadata.json"):
        log("→ Пишу заголовки й описи (Claude)...", "info")
        store.save_json(
            "metadata.json", generate_metadata(transcript, games, series_name, ep_number, cfg)
        )
        log("✔ Описи готові", "ok")
    else:
        log("• Описи вже є, пропускаю", "info")
    meta = store.load_json("metadata.json")

    # 4. відео + обкладинка (ffmpeg)
    series_bg = (cfg.get("video", "backgrounds", default={}) or {}).get(series) or None
    bg_path = bg or series_bg or cfg.get("video", "default_background", default="assets/background_default.png")
    if force or not store.exists("video.mp4"):
        log("→ Рендерю відео (для годинного епізоду це 15–30 хвилин)...", "info")
        # Рендеримо в тимчасовий файл: обірваний рендер не повинен виглядати як готове відео.
        partial = store.path("video.partial.mp4")
        render_video(str(audio_local), str(partial), cfg, bg_path=bg_path)
        partial.replace(store.path("video.mp4"))
        log("✔ Відео готове", "ok")
    else:
        log("• Відео вже є, пропускаю", "info")
    if force or not store.exists("thumbnail.jpg"):
        log("→ Рендерю обкладинку для YouTube...", "info")
        partial = store.path("thumbnail.partial.jpg")
        render_thumbnail(bg_path, str(partial))
        partial.replace(store.path("thumbnail.jpg"))
    else:
        log("• Обкладинка вже є, пропускаю", "info")

    # 5. review.md (гейт погодження)
    write_review(store, meta, games)
    log("✔ Підготовку завершено — час перевірити описи й відео", "ok")


def write_review(store: EpisodeStore, meta: dict, games: dict) -> None:
    pod = meta.get("podcast", {})
    yt = meta.get("youtube", {})
    uncertain = ", ".join(games.get("uncertain", [])) or "—"
    lines = [
        f"# Ревʼю епізоду {store.id}",
        "",
        "## Подкаст (Transistor)",
        f"**Title:** {pod.get('title', '')}",
        "",
        "```",
        pod.get("description", ""),
        "```",
        "",
        f"**Keywords:** {', '.join(pod.get('keywords', []))}",
        "",
        "## YouTube",
        f"**Title:** {yt.get('title', '')}",
        "",
        "```",
        yt.get("description", ""),
        "```",
        "",
        f"**Tags:** {', '.join(yt.get('tags', []))}",
        "",
        "## Ігри",
        f"- Знайдені: {', '.join(games.get('matched', []))}",
        f"- ⚠️ Перевірити (uncertain): {uncertain}",
        "",
        "## Відео",
        f"- {store.path('video.mp4')}",
        f"- Обкладинка YouTube: {store.path('thumbnail.jpg')}",
        "",
        "---",
        "Відредагуй `metadata.json` за потреби, тоді запусти `publish`.",
    ]
    store.write_text("review.md", "\n".join(lines))


def _require_unpublished(store: EpisodeStore) -> None:
    published = _load_published(store)
    if published.get("transistor_id") or published.get("youtube_id"):
        raise PipelineError(
            "Епізод уже опубліковано — зміни в описах тут уже не потраплять на платформи."
        )


def save_metadata(episode: str, meta: dict) -> None:
    """Зберігає відредаговані описи (з інтерфейсу) і оновлює review.md."""
    store = _store(episode)
    _require_unpublished(store)
    for platform, fields in (("podcast", ("title", "description")), ("youtube", ("title", "description"))):
        block = meta.get(platform)
        if not isinstance(block, dict):
            raise PipelineError(f"Не вистачає розділу «{platform}» в описах.")
        for field in fields:
            if not str(block.get(field, "")).strip():
                raise PipelineError(f"Поле «{field}» для {platform} не може бути порожнім.")
    if len(meta["youtube"]["title"]) > 100:
        raise PipelineError("Заголовок YouTube довший за 100 символів — YouTube його не прийме.")
    if len(meta["youtube"]["description"]) > 5000:
        raise PipelineError("Опис YouTube довший за 5000 символів — YouTube його не прийме.")
    store.save_json("metadata.json", meta)
    games = store.load_json("games.json") if store.exists("games.json") else {}
    write_review(store, meta, games)


def regenerate_metadata(episode: str, log: Log = _print_log) -> None:
    """Перегенеровує описи з поточного списку ігор (напр. після виправлення назв)."""
    store = _store(episode)
    _require_unpublished(store)
    state = store.load_json("state.json")
    store.path("metadata.json").unlink(missing_ok=True)
    store.path("review.md").unlink(missing_ok=True)
    prepare(episode, state["series"], log=log)


def _clean_game_name(name: str) -> str:
    return name.strip().rstrip("?").strip()


def _write_games_reference(entries: list[dict]) -> None:
    body = ",\n".join("  " + json.dumps(e, ensure_ascii=False) for e in entries)
    GAMES_REFERENCE.write_text(f"[\n{body}\n]\n", encoding="utf-8")


def add_game_to_reference(canonical: str, alias: str = "") -> None:
    canonical = _clean_game_name(canonical)
    alias = _clean_game_name(alias)
    if not canonical:
        raise PipelineError("Вкажи правильну назву гри.")
    entries = json.loads(GAMES_REFERENCE.read_text(encoding="utf-8"))
    for entry in entries:
        names = [entry["canonical"], *entry.get("aliases", [])]
        if canonical.casefold() in (n.casefold() for n in names):
            if alias and alias.casefold() not in (n.casefold() for n in names):
                entry.setdefault("aliases", []).append(alias)
            break
    else:
        aliases = [alias] if alias and alias.casefold() != canonical.casefold() else []
        entries.append({"canonical": canonical, "aliases": aliases})
    _write_games_reference(entries)


def resolve_uncertain_game(episode: str, original: str, canonical: str) -> None:
    """Сумнівна гра → правильна назва: у довідник (на майбутнє) і в список цього епізоду."""
    store = _store(episode)
    _require_unpublished(store)
    add_game_to_reference(canonical, alias=original)
    games = store.load_json("games.json")
    games["uncertain"] = [g for g in games.get("uncertain", []) if g != original]
    canonical = _clean_game_name(canonical)
    if canonical not in games.get("matched", []):
        games.setdefault("matched", []).append(canonical)
    store.save_json("games.json", games)


def dismiss_uncertain_game(episode: str, original: str) -> None:
    """Прибирає хибне спрацювання (не гра / непотрібна згадка) зі списку цього епізоду."""
    store = _store(episode)
    _require_unpublished(store)
    games = store.load_json("games.json")
    games["uncertain"] = [g for g in games.get("uncertain", []) if g != original]
    store.save_json("games.json", games)


# ── STAGE 1b: публікація ────────────────────────────────────────────────────
def _publish_impl(
    episode: str,
    only: str = "",
    youtube_privacy: str = "",
    schedule: str = "",
    log: Log = _print_log,
) -> dict:
    """Публікація на Transistor + YouTube (метадані з metadata.json). Ідемпотентно:
    кожен під-крок записує свій прапорець у published.json, тож повторний запуск
    продовжує з місця збою й нічого не дублює."""
    cfg = Config.load()
    store = _store(episode)
    for name in ("metadata.json", "video.mp4", "state.json"):
        if not store.exists(name):
            raise PipelineError("Епізод ще не підготовлено — спершу заверши підготовку.")
    meta = store.load_json("metadata.json")
    state = store.load_json("state.json")
    published = _load_published(store)

    def save() -> None:
        store.save_json("published.json", published)

    do_t = only in ("", "transistor")
    do_y = only in ("", "youtube")

    series = state.get("series", "")
    series_name = state.get("series_name", "")
    artwork_urls = cfg.get("transistor", "artwork_urls", default={}) or {}
    image_url = artwork_urls.get(series, "")

    # Якщо попередній запуск із плануванням обірвався — продовжуємо з тим самим часом.
    if not schedule and published.get("scheduled_at"):
        schedule = published["scheduled_at"]
    transistor_at = youtube_at = ""
    if schedule and not (published.get("transistor_status") and published.get("youtube_id")):
        _, transistor_at, youtube_at = parse_schedule(schedule)
        published["scheduled_at"] = transistor_at

    # Transistor: 1) завантаження + створення епізоду
    if do_t and not published.get("transistor_id"):
        log("→ Завантажую аудіо на Transistor...", "info")
        audio_local = store.path(state["audio_filename"])
        t = TransistorClient(cfg)
        pod = meta["podcast"]
        up = t.authorize_upload(audio_local.name)
        t.upload_audio(up["upload_url"], str(audio_local))
        ep = t.create_episode(
            audio_url=up["audio_url"],
            title=pod["title"],
            summary=pod.get("summary", pod["title"]),
            description=pod["description"],
            keywords=", ".join(pod.get("keywords", [])),
            author=series_name,
            number=episode_number(episode),
            image_url=image_url,
        )
        published["transistor_id"] = ep["id"]
        save()
        if not image_url:
            log("⚠ Не задано обкладинку для Transistor (config.transistor.artwork_urls).", "warn")
    elif do_t:
        log("• Аудіо вже на Transistor", "info")

    # Transistor: 2) публікація або планування (окремо — щоб збій тут не створював дублікат)
    # Старі записи (до появи transistor_status) вважаються опублікованими, якщо є transistor_url.
    already_live = published.get("transistor_status") or published.get("transistor_url")
    if do_t and published.get("transistor_id") and not already_live:
        t = TransistorClient(cfg)
        if transistor_at:
            pub = t.schedule_episode(published["transistor_id"], transistor_at)
            published["transistor_status"] = "scheduled"
            published["scheduled_at"] = transistor_at
            log(f"✔ Transistor: заплановано на {transistor_at}", "ok")
        else:
            pub = t.publish_episode(published["transistor_id"])
            published["transistor_status"] = "published"
            log("✔ Transistor: епізод вийшов", "ok")
        published["transistor_url"] = pub.get("attributes", {}).get("share_url", "")
        save()

    # YouTube: завантаження відео
    if do_y and not published.get("youtube_id"):
        log("→ Завантажую відео на YouTube (може тривати кілька хвилин)...", "info")
        y = YouTubeClient(cfg, notify=log)
        ytm = meta["youtube"]
        privacy = youtube_privacy or cfg.get("youtube", "privacy_status", default="public")
        vid = y.upload_video(
            str(store.path("video.mp4")),
            ytm["title"],
            ytm["description"],
            ytm.get("tags", []),
            privacy_status=privacy,
            publish_at=youtube_at or None,
        )
        published["youtube_id"] = vid
        published["youtube_url"] = f"https://youtu.be/{vid}"
        published["youtube_privacy"] = "scheduled" if youtube_at else privacy
        if youtube_at:
            published["scheduled_at"] = transistor_at
        save()
        log(f"✔ YouTube: відео завантажено ({published['youtube_url']})", "ok")
    elif do_y:
        log("• Відео вже на YouTube", "info")

    # Під-кроки YouTube — окремо ідемпотентні, щоб збій одного не пропускав решту
    # назавжди при повторному запуску, коли youtube_id уже існує.
    if do_y and published.get("youtube_id"):
        vid = published["youtube_id"]
        y = YouTubeClient(cfg, notify=log)
        if store.exists("thumbnail.jpg") and not published.get("youtube_thumbnail_set"):
            y.set_thumbnail(vid, str(store.path("thumbnail.jpg")))
            published["youtube_thumbnail_set"] = True
            save()
            log("✔ YouTube: обкладинку встановлено", "ok")
        playlists_cfg = cfg.get("youtube", "playlists", default={}) or {}
        playlist_title = playlists_cfg.get(series, "")
        if playlist_title and not published.get("youtube_playlist"):
            y.add_to_playlist(vid, playlist_title)
            published["youtube_playlist"] = playlist_title
            save()
            log(f"✔ YouTube: додано у плейлист «{playlist_title}»", "ok")
        if published.get("transistor_id") and not published.get("transistor_video_url_set"):
            TransistorClient(cfg).update_episode(
                published["transistor_id"], video_url=published["youtube_url"]
            )
            published["transistor_video_url_set"] = True
            save()
            log("✔ Transistor: додано посилання на YouTube-відео", "ok")
        if published.get("youtube_privacy") in ("unlisted", "private"):
            log(
                f"⚠ Відео на YouTube поки {published['youtube_privacy']} — переглянь його "
                "і зроби публічним.",
                "warn",
            )
    return published


def youtube_go_public(episode: str, log: Log = _print_log) -> None:
    """Переводить уже завантажене відео YouTube у public (без переаплоаду)."""
    cfg = Config.load()
    store = _store(episode)
    published = _load_published(store)
    video_id = published.get("youtube_id")
    if not video_id:
        raise PipelineError("Відео на YouTube ще не завантажено для цього епізоду.")
    if published.get("youtube_privacy") == "scheduled":
        raise PipelineError(
            f"Відео заплановане на {published.get('scheduled_at')} — YouTube відкриє його сам."
        )
    log("→ Роблю відео публічним...", "info")
    YouTubeClient(cfg, notify=log).set_privacy(video_id, "public")
    published["youtube_privacy"] = "public"
    store.save_json("published.json", published)
    log(f"✔ Відео тепер публічне: {published.get('youtube_url', video_id)}", "ok")


def youtube_login(log: Log = _print_log) -> None:
    """Новий вхід у YouTube (коли протух токен або змінились права)."""
    YouTubeClient(Config.load(), notify=log).login()
    log("✔ Вхід у YouTube оновлено", "ok")


# ── STAGE 2: Telegram ───────────────────────────────────────────────────────
def telegram_draft(episode: str, regenerate: bool = False, log: Log = _print_log) -> str:
    """Резолвінг лінків → текст поста (Claude) → telegram.md."""
    cfg = Config.load()
    store = _store(episode)
    published = _load_published(store)
    if not published:
        raise PipelineError("Епізод ще не опубліковано — пост у Telegram складається після публікації.")
    if regenerate:
        if published.get("telegram_sent_at"):
            raise PipelineError("Пост уже надіслано в канал — перегенерація нічого не змінить.")
        store.path("telegram.md").unlink(missing_ok=True)
    if store.exists("telegram.md"):
        return store.read_text("telegram.md")

    meta = store.load_json("metadata.json")
    games = store.load_json("games.json") if store.exists("games.json") else {"matched": []}
    state = store.load_json("state.json")
    ep_number = str(int(episode)) if episode.isdigit() else episode
    links = resolve_links(published, cfg)

    log("→ Пишу пост для Telegram (Claude)...", "info")
    system = (ROOT / "prompts" / "compose_telegram.md").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "telegram_post.md").read_text(encoding="utf-8")
    links_str = "\n".join(f"{k}: {v}" for k, v in links.items())
    games_str = ", ".join(games.get("matched", []))
    user = (
        "СЕРІЯ ЕПІЗОДУ:\n" f"{state.get('series_name', '')}\n\n"
        "НОМЕР ЕПІЗОДУ:\n" f"{ep_number}\n\n"
        "ФОРМАТ ПОСТА (шаблон):\n" f"{template}\n\n"
        "ОПИС ЕПІЗОДУ:\n" f"{meta.get('podcast', {}).get('description', '')}\n\n"
        "ЗГАДАНІ ІГРИ:\n" f"{games_str}\n\n"
        "ПОСИЛАННЯ:\n" f"{links_str}"
    )
    text = call_claude_text(system, user, cfg)
    store.write_text("telegram.md", text)
    log("✔ Чернетку поста готово", "ok")
    return text


def save_telegram(episode: str, text: str) -> None:
    store = _store(episode)
    if _load_published(store).get("telegram_sent_at"):
        raise PipelineError("Пост уже надіслано — правки тут нічого не змінять у каналі.")
    if not text.strip():
        raise PipelineError("Текст поста порожній.")
    store.write_text("telegram.md", text)


def _check_released(published: dict, at: datetime) -> None:
    scheduled_at = published.get("scheduled_at")
    if scheduled_at and published.get("youtube_privacy") == "scheduled":
        release = datetime.strptime(scheduled_at, SCHEDULE_FORMAT).astimezone()
        if at < release:
            raise PipelineError(
                f"Епізод вийде лише {scheduled_at} — пост у Telegram має бути не раніше."
            )


def _telegram_send_impl(episode: str, log: Log = _print_log) -> None:
    cfg = Config.load()
    store = _store(episode)
    published = _load_published(store)
    if published.get("telegram_sent_at"):
        raise PipelineError(f"Пост уже надіслано {published['telegram_sent_at']} — повторно не надсилаю.")
    if not store.exists("telegram.md"):
        raise PipelineError("Ще немає чернетки поста — спершу склади її.")
    _check_released(published, datetime.now().astimezone())
    log("→ Надсилаю пост у канал...", "info")
    send_message(store.read_text("telegram.md"), cfg, preview_url=published.get("youtube_url", ""))
    published["telegram_sent_at"] = _now_iso()
    published.pop("telegram_scheduled_at", None)
    store.save_json("published.json", published)
    cancel_local_command(_telegram_label(episode))
    log("✔ Пост опубліковано в каналі", "ok")


def _telegram_label(episode: str) -> str:
    return f"fm.podpub.telegram.{episode}"


def telegram_schedule(episode: str, at: str, log: Log = _print_log) -> None:
    """Планує автоматичну відправку telegram.md (Bot API не вміє відкладених повідомлень):
    у потрібний час на цьому Mac відкриється вікно Terminal і надішле пост. Mac має бути
    увімкненим і залогіненим; якщо спить — пост піде після пробудження (протягом доби)."""
    store = _store(episode)
    published = _load_published(store)
    if published.get("telegram_sent_at"):
        raise PipelineError("Пост уже надіслано — планувати нічого.")
    if not store.exists("telegram.md"):
        raise PipelineError("Ще немає чернетки поста — спершу склади її.")
    dt_local, at_str, _ = parse_schedule(at)
    _check_released(published, dt_local)
    command = (
        f"cd {shlex.quote(str(ROOT))} || exit 1\n"
        "source .venv/bin/activate\n"
        f'echo "podpub: надсилаю запланований пост випуску {episode} у Telegram…"\n'
        f"podpub telegram --episode {shlex.quote(episode)} --publish"
    )
    schedule_local_command(_telegram_label(episode), dt_local, command)
    published["telegram_scheduled_at"] = at_str
    store.save_json("published.json", published)
    log(f"✔ Пост заплановано на {at_str}", "ok")


def telegram_cancel_schedule(episode: str, log: Log = _print_log) -> None:
    store = _store(episode)
    published = _load_published(store)
    cancel_local_command(_telegram_label(episode))
    published.pop("telegram_scheduled_at", None)
    store.save_json("published.json", published)
    log("✔ Заплановану відправку скасовано", "ok")


# ── стан епізоду (для інтерфейсу й `podpub status`) ─────────────────────────
def episode_status(episode: str) -> dict:
    store = EpisodeStore(episode)
    state = store.load_json("state.json") if store.exists("state.json") else {}
    published = _load_published(store)
    steps = {"audio": _existing_audio(store) is not None}
    steps.update({name.split(".")[0]: store.exists(name) for name in PREPARE_FILES})
    prepared = all(steps.values())
    title = None
    if store.exists("metadata.json"):
        title = store.load_json("metadata.json").get("podcast", {}).get("title")

    is_published = bool(published.get("transistor_id") or published.get("youtube_id"))
    # Записи, створені до веб-інтерфейсу, не мають прапорців під-кроків і позначки
    # відправки в Telegram — їх вважаємо завершеними, щоб нічого не дублювати.
    legacy = is_published and "transistor_status" not in published
    if legacy:
        publish_complete = bool(published.get("transistor_url") and published.get("youtube_id"))
    else:
        playlist = (Config.load().get("youtube", "playlists", default={}) or {}).get(state.get("series"))
        publish_complete = all([
            published.get("transistor_status"),
            published.get("youtube_id"),
            published.get("transistor_video_url_set"),
            published.get("youtube_thumbnail_set") or not steps["thumbnail"],
            published.get("youtube_playlist") or not playlist,
        ])
    needs_go_public = published.get("youtube_privacy") in ("unlisted", "private")
    telegram_draft = store.exists("telegram.md")
    telegram_done = bool(published.get("telegram_sent_at")) or (legacy and telegram_draft)

    if not prepared:
        stage = "prepare"
    elif not is_published:
        stage = "review"
    elif not publish_complete or needs_go_public:
        stage = "publish"
    elif not telegram_done:
        stage = "telegram"
    else:
        stage = "done"

    return {
        "id": episode,
        "number": episode_number(episode),
        "series": state.get("series"),
        "series_name": state.get("series_name"),
        "title": title,
        "steps": steps,
        "prepared": prepared,
        "published": published,
        "publish_complete": bool(publish_complete),
        "legacy": legacy,
        "telegram_draft": telegram_draft,
        "busy": _lock_owner(store) is not None,
        "stage": stage,
    }


def prepare(episode: str, series: str, audio: str | None = None, title_hint: str = "",
            bg: str = "", force: bool = False, log: Log = _print_log) -> None:
    with _episode_lock(EpisodeStore(episode)):
        _prepare_impl(episode, series, audio=audio, title_hint=title_hint, bg=bg, force=force, log=log)


def publish(episode: str, only: str = "", youtube_privacy: str = "", schedule: str = "",
            log: Log = _print_log) -> dict:
    with _episode_lock(_store(episode)):
        return _publish_impl(episode, only=only, youtube_privacy=youtube_privacy,
                             schedule=schedule, log=log)


def telegram_send(episode: str, log: Log = _print_log) -> None:
    with _episode_lock(_store(episode)):
        _telegram_send_impl(episode, log=log)
