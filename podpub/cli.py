from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer

from .config import ROOT, Config
from .extract import extract_games
from .links import resolve_links
from .llm import call_claude_text
from .metadata import generate_metadata
from .scheduler import schedule_local_command
from .store import EpisodeStore
from .telegram import send_message
from .transcribe import get_transcriber
from .transistor import TransistorClient
from .video import render_thumbnail, render_video
from .youtube import YouTubeClient

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_schedule(value: str) -> tuple[datetime, str, str]:
    """Один локальний час → (datetime, формат для Transistor, ISO8601 UTC для YouTube)."""
    dt_local = datetime.strptime(value, SCHEDULE_FORMAT).astimezone()
    transistor_at = dt_local.strftime(SCHEDULE_FORMAT)
    youtube_at = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt_local, transistor_at, youtube_at

app = typer.Typer(add_completion=False, help="Dicepunk / d2 Дідів — podcast publishing pipeline")


def _cfg() -> Config:
    return Config.load()


# ── STAGE 1a ────────────────────────────────────────────────────────────────
@app.command()
def prepare(
    audio: str = typer.Option(..., "--audio", help="Шлях до аудіофайлу епізоду"),
    episode: str = typer.Option(..., "--episode", help="ID епізоду, напр. 042"),
    series: str = typer.Option(..., "--series", help="Серія епізоду: dicepunk | d2didiv"),
    title_hint: str = typer.Option("", "--title-hint", help="Підказка для заголовка"),
    bg: str = typer.Option("", "--bg", help="Фон (інакше дефолт із config)"),
    force: bool = typer.Option(False, "--force", help="Перезаписати вже зроблені кроки"),
):
    """Транскрибація → витяг ігор → метадані → відео → review.md (ЗУПИНКА, нічого не публікує)."""
    cfg = _cfg()
    store = EpisodeStore(episode)
    lang = cfg.get("show", "language", default="uk")

    series_names = cfg.get("show", "series", default={}) or {}
    if series not in series_names:
        typer.echo(f"✗ Невідома серія '{series}'. Доступні: {', '.join(series_names) or '(не задано в config.yaml)'}")
        raise typer.Exit(1)
    series_name = series_names[series]

    # 0. копіюємо аудіо в теку епізоду (щоб publish не залежав від оригінального шляху)
    audio_local = store.path(f"audio{Path(audio).suffix}")
    if force or not audio_local.exists():
        shutil.copy2(audio, audio_local)
    store.save_json(
        "state.json",
        {
            "audio_filename": audio_local.name,
            "title_hint": title_hint,
            "series": series,
            "series_name": series_name,
        },
    )

    # 1. транскрипція
    if force or not store.exists("transcript.txt"):
        typer.echo("→ Транскрибація...")
        store.write_text("transcript.txt", get_transcriber(cfg).transcribe(str(audio_local), lang))
    else:
        typer.echo("• transcript.txt вже є, пропускаю")
    transcript = store.read_text("transcript.txt")

    # 2. витяг + звірка ігор (Claude)
    if force or not store.exists("games.json"):
        typer.echo("→ Витяг та звірка ігор (Claude)...")
        store.save_json("games.json", extract_games(transcript, cfg))
    else:
        typer.echo("• games.json вже є, пропускаю")
    games = store.load_json("games.json")

    # 3. метадані (Claude)
    ep_number = str(int(episode)) if episode.isdigit() else episode
    if force or not store.exists("metadata.json"):
        typer.echo("→ Генерація метаданих (Claude)...")
        store.save_json(
            "metadata.json", generate_metadata(transcript, games, series_name, ep_number, cfg)
        )
    else:
        typer.echo("• metadata.json вже є, пропускаю")
    meta = store.load_json("metadata.json")

    # 4. відео + обкладинка (ffmpeg)
    series_bg = (cfg.get("video", "backgrounds", default={}) or {}).get(series) or None
    bg_path = bg or series_bg or cfg.get("video", "default_background", default="assets/background_default.png")
    if force or not store.exists("video.mp4"):
        typer.echo("→ Рендер відео (ffmpeg)...")
        render_video(str(audio_local), str(store.path("video.mp4")), cfg, bg_path=bg_path)
    else:
        typer.echo("• video.mp4 вже є, пропускаю")
    if force or not store.exists("thumbnail.jpg"):
        typer.echo("→ Рендер обкладинки для YouTube (ffmpeg)...")
        render_thumbnail(bg_path, str(store.path("thumbnail.jpg")))
    else:
        typer.echo("• thumbnail.jpg вже є, пропускаю")

    # 5. review.md (гейт погодження)
    _write_review(store, meta, games)
    typer.echo(
        typer.style(
            f"\n✔ Готово. Переглянь: {store.path('review.md')}\n"
            f"  За потреби відредагуй {store.path('metadata.json')}, тоді:\n"
            f"  podpub publish --episode {episode}",
            fg=typer.colors.GREEN,
        )
    )


def _write_review(store: EpisodeStore, meta: dict, games: dict) -> None:
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


# ── STAGE 1b ────────────────────────────────────────────────────────────────
@app.command()
def publish(
    episode: str = typer.Option(..., "--episode"),
    only: str = typer.Option("", "--only", help="transistor | youtube (інакше обидва)"),
    youtube_privacy: str = typer.Option(
        "", "--youtube-privacy", help="Override youtube.privacy_status для цього запуску (напр. unlisted)"
    ),
    schedule: str = typer.Option(
        "", "--schedule", help="Запланувати на майбутній час замість негайної публікації, напр. '2026-07-20 09:00:00' (локальний час цієї машини)"
    ),
):
    """Публікація на Transistor + YouTube (метадані з metadata.json). Ідемпотентно."""
    cfg = _cfg()
    store = EpisodeStore(episode)
    meta = store.load_json("metadata.json")
    state = store.load_json("state.json")
    published = store.load_json("published.json") if store.exists("published.json") else {}

    do_t = only in ("", "transistor")
    do_y = only in ("", "youtube")

    series = state.get("series", "")
    series_name = state.get("series_name", "")
    ep_number = int(episode) if episode.isdigit() else None
    artwork_urls = cfg.get("transistor", "artwork_urls", default={}) or {}
    image_url = artwork_urls.get(series, "")

    transistor_at = youtube_at = ""
    if schedule:
        _, transistor_at, youtube_at = _parse_schedule(schedule)

    if do_t and not published.get("transistor_id"):
        typer.echo("→ Публікація на Transistor...")
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
            number=ep_number,
            image_url=image_url,
        )
        pub = t.schedule_episode(ep["id"], transistor_at) if schedule else t.publish_episode(ep["id"])
        published["transistor_id"] = ep["id"]
        published["transistor_url"] = pub.get("attributes", {}).get("share_url", "")
        store.save_json("published.json", published)
        if schedule:
            typer.echo(f"  ✔ Transistor: заплановано на {transistor_at}")
        else:
            typer.echo(f"  ✔ Transistor: {published.get('transistor_url', '(без share_url)')}")
        if not image_url:
            typer.echo(
                typer.style(
                    "  ⚠ image_url не задано (config.transistor.artwork_urls) — епізод без обкладинки.",
                    fg=typer.colors.YELLOW,
                )
            )
    elif do_t:
        typer.echo("• Transistor уже опубліковано, пропускаю")

    if do_y and not published.get("youtube_id"):
        typer.echo("→ Публікація на YouTube...")
        y = YouTubeClient(cfg)
        ytm = meta["youtube"]
        vid = y.upload_video(
            str(store.path("video.mp4")),
            ytm["title"],
            ytm["description"],
            ytm.get("tags", []),
            privacy_status=youtube_privacy or None,
            publish_at=youtube_at or None,
        )
        published["youtube_id"] = vid
        published["youtube_url"] = f"https://youtu.be/{vid}"
        store.save_json("published.json", published)
        if schedule:
            typer.echo(f"  ✔ YouTube: заплановано на {transistor_at} ({published['youtube_url']})")
        else:
            typer.echo(f"  ✔ YouTube: {published['youtube_url']}")
    elif do_y:
        typer.echo("• YouTube уже опубліковано, пропускаю")

    # Наступні під-кроки — окремо ідемпотентні (кожен зі своїм прапорцем у published.json),
    # щоб falha в одному з них (напр. тимчасова помилка YouTube API) не пропускала решту
    # назавжди при повторному запуску, коли youtube_id уже існує.
    if do_y and published.get("youtube_id"):
        vid = published["youtube_id"]
        y = YouTubeClient(cfg)
        if store.exists("thumbnail.jpg") and not published.get("youtube_thumbnail_set"):
            y.set_thumbnail(vid, str(store.path("thumbnail.jpg")))
            published["youtube_thumbnail_set"] = True
            store.save_json("published.json", published)
            typer.echo("  ✔ YouTube: обкладинку встановлено")
        playlists_cfg = cfg.get("youtube", "playlists", default={}) or {}
        playlist_title = playlists_cfg.get(series, "")
        if playlist_title and not published.get("youtube_playlist"):
            y.add_to_playlist(vid, playlist_title)
            published["youtube_playlist"] = playlist_title
            store.save_json("published.json", published)
            typer.echo(f"  ✔ YouTube: додано у плейлист «{playlist_title}»")
        if published.get("transistor_id") and not published.get("transistor_video_url_set"):
            TransistorClient(cfg).update_episode(
                published["transistor_id"], video_url=published["youtube_url"]
            )
            published["transistor_video_url_set"] = True
            store.save_json("published.json", published)
            typer.echo("  ✔ Transistor: додано посилання на YouTube-відео")
        if not schedule:
            actual_privacy = youtube_privacy or cfg.get("youtube", "privacy_status", default="public")
            if actual_privacy != "public":
                typer.echo(
                    typer.style(
                        f"  ⚠ YouTube-відео опубліковано як {actual_privacy}. Переглянь і тоді:\n"
                        f"    podpub youtube-go-public --episode {episode}",
                        fg=typer.colors.YELLOW,
                    )
                )


@app.command("youtube-go-public")
def youtube_go_public(episode: str = typer.Option(..., "--episode")):
    """Переводить уже завантажене відео YouTube з unlisted/private у public (без переаплоаду)."""
    cfg = _cfg()
    store = EpisodeStore(episode)
    if not store.exists("published.json"):
        typer.echo("✗ Немає published.json — спершу зроби `publish`.")
        raise typer.Exit(1)
    published = store.load_json("published.json")
    video_id = published.get("youtube_id")
    if not video_id:
        typer.echo("✗ Відео на YouTube ще не завантажено для цього епізоду.")
        raise typer.Exit(1)
    YouTubeClient(cfg).set_privacy(video_id, "public")
    typer.echo(f"✔ YouTube-відео тепер public: {published.get('youtube_url', video_id)}")


# ── STAGE 2 ─────────────────────────────────────────────────────────────────
@app.command()
def telegram(
    episode: str = typer.Option(..., "--episode"),
    publish: bool = typer.Option(False, "--publish", help="Відправити в канал (інакше лише draft)"),
):
    """Резолвінг лінків → текст поста (Claude) → draft, або відправка з --publish."""
    cfg = _cfg()
    store = EpisodeStore(episode)
    if not store.exists("published.json"):
        typer.echo("✗ Немає published.json — спершу зроби `publish`.")
        raise typer.Exit(1)

    published = store.load_json("published.json")
    meta = store.load_json("metadata.json")
    games = store.load_json("games.json") if store.exists("games.json") else {"matched": []}
    state = store.load_json("state.json")
    series_name = state.get("series_name", "")
    ep_number = str(int(episode)) if episode.isdigit() else episode
    links = resolve_links(published, cfg)

    if not store.exists("telegram.md"):
        typer.echo("→ Складання поста (Claude)...")
        system = (ROOT / "prompts" / "compose_telegram.md").read_text(encoding="utf-8")
        template = (ROOT / "templates" / "telegram_post.md").read_text(encoding="utf-8")
        links_str = "\n".join(f"{k}: {v}" for k, v in links.items())
        games_str = ", ".join(games.get("matched", []))
        user = (
            "СЕРІЯ ЕПІЗОДУ:\n" f"{series_name}\n\n"
            "НОМЕР ЕПІЗОДУ:\n" f"{ep_number}\n\n"
            "ФОРМАТ ПОСТА (шаблон):\n" f"{template}\n\n"
            "ОПИС ЕПІЗОДУ:\n" f"{meta.get('podcast', {}).get('description', '')}\n\n"
            "ЗГАДАНІ ІГРИ:\n" f"{games_str}\n\n"
            "ПОСИЛАННЯ:\n" f"{links_str}"
        )
        store.write_text("telegram.md", call_claude_text(system, user, cfg))

    if not publish:
        typer.echo(
            typer.style(
                f"\n✔ Чернетка: {store.path('telegram.md')}\n"
                f"  Перевір, за потреби відредагуй, тоді:\n"
                f"  podpub telegram --episode {episode} --publish",
                fg=typer.colors.GREEN,
            )
        )
        return

    typer.echo("→ Відправка в Telegram...")
    send_message(store.read_text("telegram.md"), cfg, preview_url=published.get("youtube_url", ""))
    typer.echo("  ✔ Опубліковано в канал")


@app.command("schedule-telegram")
def schedule_telegram(
    episode: str = typer.Option(..., "--episode"),
    at: str = typer.Option(..., "--at", help="Локальний час відправки, напр. '2026-07-20 09:00:00'"),
):
    """Планує автоматичну відправку вже готового telegram.md на майбутній час (launchd, без sudo).

    Bot API не підтримує заплановані повідомлення — тому це через локальний
    одноразовий launchd-агент цієї машини (має лишатись увімкненою й розлогіненою
    з активним GUI-сеансом до моменту спрацювання).
    """
    store = EpisodeStore(episode)
    if not store.exists("telegram.md"):
        typer.echo(
            "✗ Немає telegram.md — спершу зроби `podpub telegram --episode "
            f"{episode}` (без --publish), щоб згенерувати чернетку."
        )
        raise typer.Exit(1)
    dt_local, _, _ = _parse_schedule(at)
    label = f"fm.podpub.telegram.{episode}"
    command = (
        f"cd {ROOT} && source .venv/bin/activate && podpub telegram --episode {episode} --publish"
    )
    plist_path = schedule_local_command(label, dt_local, command)
    typer.echo(
        typer.style(
            f"✔ Заплановано відправку episode {episode} на {at}.\n"
            f"  launchd-агент: {plist_path}\n"
            f"  (машина має лишатись увімкненою й залогіненою до цього часу)",
            fg=typer.colors.GREEN,
        )
    )


@app.command()
def status(episode: str = typer.Option(..., "--episode")):
    """Показати, які кроки виконані."""
    store = EpisodeStore(episode)
    steps = [
        "audio", "transcript.txt", "games.json", "metadata.json",
        "video.mp4", "review.md", "published.json", "telegram.md",
    ]
    typer.echo(f"Епізод {episode}:")
    if store.exists("state.json"):
        series_name = store.load_json("state.json").get("series_name", "")
        if series_name:
            typer.echo(f"  Серія: {series_name}")
    for s in steps:
        exists = store.exists(s) or (s == "audio" and any(store.dir.glob("audio.*")))
        typer.echo(f"  {'✔' if exists else '·'} {s}")
    if store.exists("published.json"):
        typer.echo("\nОпубліковано:")
        for k, v in store.load_json("published.json").items():
            if k.endswith("url"):
                typer.echo(f"  {k}: {v}")


if __name__ == "__main__":
    app()
