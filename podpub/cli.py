from __future__ import annotations

from typing import Callable

import typer

from . import pipeline
from .pipeline import PipelineError
from .store import EpisodeStore

app = typer.Typer(add_completion=False, help="Dicepunk / d2 Дідів — podcast publishing pipeline")

_COLORS = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW}


def _log(message: str, kind: str = "info") -> None:
    typer.echo(typer.style(message, fg=_COLORS[kind]) if kind in _COLORS else message)


def _run(fn: Callable, *args, **kwargs):
    try:
        return fn(*args, log=_log, **kwargs)
    except PipelineError as e:
        typer.echo(typer.style(f"✗ {e}", fg=typer.colors.RED))
        raise typer.Exit(1)


def _hint(text: str) -> None:
    typer.echo(typer.style(text, fg=typer.colors.GREEN))


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
    _run(pipeline.prepare, episode, series, audio=audio, title_hint=title_hint, bg=bg, force=force)
    store = EpisodeStore(episode)
    _hint(
        f"\n  Переглянь: {store.path('review.md')}\n"
        f"  За потреби відредагуй {store.path('metadata.json')}, тоді:\n"
        f"  podpub publish --episode {episode}"
    )


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
    published = _run(
        pipeline.publish, episode, only=only, youtube_privacy=youtube_privacy, schedule=schedule
    )
    if published.get("youtube_privacy") in ("unlisted", "private"):
        _hint(f"  Коли переглянеш відео: podpub youtube-go-public --episode {episode}")


@app.command("youtube-go-public")
def youtube_go_public(episode: str = typer.Option(..., "--episode")):
    """Переводить уже завантажене відео YouTube з unlisted/private у public (без переаплоаду)."""
    _run(pipeline.youtube_go_public, episode)


@app.command("youtube-login")
def youtube_login():
    """Новий вхід у YouTube (якщо протух токен)."""
    _run(pipeline.youtube_login)


# ── STAGE 2 ─────────────────────────────────────────────────────────────────
@app.command()
def telegram(
    episode: str = typer.Option(..., "--episode"),
    publish: bool = typer.Option(False, "--publish", help="Відправити в канал (інакше лише draft)"),
):
    """Резолвінг лінків → текст поста (Claude) → draft, або відправка з --publish."""
    _run(pipeline.telegram_draft, episode)
    if not publish:
        store = EpisodeStore(episode)
        _hint(
            f"\n  Чернетка: {store.path('telegram.md')}\n"
            f"  Перевір, за потреби відредагуй, тоді:\n"
            f"  podpub telegram --episode {episode} --publish"
        )
        return
    _run(pipeline.telegram_send, episode)


@app.command("schedule-telegram")
def schedule_telegram(
    episode: str = typer.Option(..., "--episode"),
    at: str = typer.Option(..., "--at", help="Локальний час відправки, напр. '2026-07-20 09:00:00'"),
):
    """Планує автоматичну відправку вже готового telegram.md на майбутній час (launchd, без sudo).

    Bot API не підтримує заплановані повідомлення — тому це через локальний
    одноразовий launchd-агент цієї машини (має лишатись увімкненою й залогіненою
    до моменту спрацювання).
    """
    _run(pipeline.telegram_schedule, episode, at)


@app.command()
def status(episode: str = typer.Option(..., "--episode")):
    """Показати, які кроки виконані."""
    if not pipeline.episode_exists(episode):
        typer.echo(f"Епізоду {episode} ще немає.")
        raise typer.Exit(1)
    info = pipeline.episode_status(episode)
    typer.echo(f"Епізод {episode}:")
    if info["series_name"]:
        typer.echo(f"  Серія: {info['series_name']}")
    for step, done in info["steps"].items():
        typer.echo(f"  {'✔' if done else '·'} {step}")
    typer.echo(f"  {'✔' if info['telegram_draft'] else '·'} telegram")
    urls = {k: v for k, v in info["published"].items() if k.endswith("url") and v}
    if urls:
        typer.echo("\nОпубліковано:")
        for k, v in urls.items():
            typer.echo(f"  {k}: {v}")


@app.command()
def web(
    port: int = typer.Option(8765, "--port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Не відкривати браузер автоматично"),
):
    """Запускає веб-інтерфейс (локально, лише на цьому Mac) і відкриває його в браузері."""
    from .web.server import serve

    serve(port=port, open_browser=not no_browser)


if __name__ == "__main__":
    app()
