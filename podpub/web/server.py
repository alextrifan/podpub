"""Локальний веб-інтерфейс podpub (лише 127.0.0.1 — працює тільки на цьому Mac).

Довгі дії (підготовка, публікація, Telegram) виконуються у фоні як «завдання»;
сторінка опитує /api/job і показує живий журнал. Одночасно — лише одне завдання.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import pipeline
from ..config import Config
from ..pipeline import PipelineError
from ..store import EpisodeStore

STATIC = Path(__file__).parent / "static"
EPISODE_ID = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
MEDIA = {"video.mp4": "video/mp4", "thumbnail.jpg": "image/jpeg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


# ── фонові завдання ─────────────────────────────────────────────────────────
class Job:
    def __init__(self, kind: str, episode: str | None, label: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.episode = episode
        self.label = label
        self.status = "running"
        self.error: str | None = None
        self.lines: list[dict] = []
        self.started = time.time()
        self.finished: float | None = None

    def log(self, message: str, kind: str = "info") -> None:
        print(message, flush=True)
        self.lines.append({"t": time.time(), "kind": kind, "text": message})

    def snapshot(self, since: int = 0) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "episode": self.episode,
            "label": self.label,
            "status": self.status,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
            "total": len(self.lines),
            "lines": self.lines[since:],
        }


def _friendly_error(e: BaseException) -> str:
    """Перетворює технічний виняток на пояснення, з яким можна щось зробити."""
    name = type(e).__name__
    if name == "RetryError" and hasattr(e, "last_attempt"):
        inner = e.last_attempt.exception()
        if inner is not None:
            return _friendly_error(inner)
    if isinstance(e, httpx.HTTPStatusError):
        host, code = e.request.url.host, e.response.status_code
        if "transistor" in host and code == 401:
            return "Transistor відхилив API-ключ. Перевір TRANSISTOR_API_KEY у файлі .env."
        if "telegram" in host:
            return f"Telegram відхилив повідомлення ({code}): {e.response.text[:300]}"
        if "anthropic" in host:
            return f"Claude тимчасово недоступний ({code}). Спробуй ще раз за хвилину."
        return f"{host} відповів помилкою {code}. Спробуй ще раз трохи згодом."
    text = str(e)
    if "credit balance is too low" in text:
        return (
            "На рахунку Anthropic (Claude) закінчилися кредити. Поповни баланс на "
            "console.anthropic.com → Plans & Billing і натисни «Спробувати ще раз»."
        )
    if "insufficient_quota" in text or "exceeded your current quota" in text:
        return (
            "На рахунку OpenAI (розшифровка аудіо) закінчилися кошти. Поповни баланс на "
            "platform.openai.com → Billing і натисни «Спробувати ще раз»."
        )
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return f"Сервіс відхилив API-ключ — перевір ключі у файлі .env. ({text[:200]})"
    if name == "HttpError":
        return (
            "YouTube тимчасово відповів помилкою. Натисни «Спробувати ще раз» — "
            f"публікація продовжиться з того ж місця. ({str(e)[:200]})"
        )
    if name == "CalledProcessError":
        return "ffmpeg не зміг зрендерити відео. Деталі — у вікні терміналу, з якого запущено інтерфейс."
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout"):
        return "Немає з'єднання з інтернетом або сервіс не відповідає. Перевір мережу й спробуй ще раз."
    return f"{name}: {str(e)[:300]}"


class JobRunner:
    def __init__(self) -> None:
        self.current: Job | None = None
        self._lock = threading.Lock()

    def start(self, kind: str, episode: str | None, label: str, fn: Callable[[Callable], None]) -> Job:
        with self._lock:
            if self.current and self.current.status == "running":
                raise HTTPException(
                    409, f"Зараз уже виконується «{self.current.label}». Дочекайся завершення."
                )
            job = Job(kind, episode, label)
            self.current = job
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    def _run(self, job: Job, fn: Callable[[Callable], None]) -> None:
        # caffeinate не дає Mac заснути, поки триває довга дія (рендер, аплоад).
        try:
            awake = subprocess.Popen(["caffeinate", "-i"])
        except OSError:
            awake = None
        try:
            fn(job.log)
            job.status = "done"
        except PipelineError as e:
            job.error = str(e)
            job.status = "error"
        except Exception as e:  # noqa: BLE001 — показуємо людині, деталі в терміналі
            traceback.print_exc()
            job.error = _friendly_error(e)
            job.status = "error"
        finally:
            job.finished = time.time()
            if awake:
                awake.terminate()


runner = JobRunner()


# ── застосунок ──────────────────────────────────────────────────────────────
app = FastAPI(title="podpub", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Захист від запитів з чужих сайтів: лише localhost-хост, а змінні запити —
    # тільки з нашим заголовком (браузер не дасть чужій сторінці його надіслати).
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"detail": "Доступ лише з цього комп'ютера."}, status_code=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("x-podpub") != "1":
        return JSONResponse({"detail": "Запит відхилено."}, status_code=403)
    return await call_next(request)


@app.exception_handler(PipelineError)
async def pipeline_error(_: Request, exc: PipelineError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _episode(episode: str, must_exist: bool = True) -> str:
    if not EPISODE_ID.match(episode):
        raise HTTPException(400, "Номер випуску може містити лише цифри й латинські літери.")
    if must_exist and not pipeline.episode_exists(episode):
        raise HTTPException(404, f"Випуску {episode} немає.")
    return episode


def _local_datetime(value: str) -> str:
    """'2026-10-27T09:00' (з поля datetime-local) → '2026-10-27 09:00:00'."""
    value = value.strip().replace("T", " ")
    return value + ":00" if len(value) == 16 else value


def _next_number(ids: list[str]) -> str:
    numbers = [int(e) for e in ids if e.isdigit()]
    return f"{(max(numbers) + 1) if numbers else 1:03d}"


@app.get("/api/ping")
def ping():
    return {"app": "podpub"}


@app.get("/api/state")
def state():
    cfg = Config.load()
    ids = pipeline.list_episodes()
    return {
        "series": [{"key": k, "name": v} for k, v in pipeline.series_options(cfg).items()],
        "channel": cfg.get("telegram", "channel_id", default=""),
        "episodes": [pipeline.episode_status(e) for e in ids],
        "next_number": _next_number(ids),
        "job": runner.current.snapshot(len(runner.current.lines)) if runner.current else None,
    }


@app.get("/api/job")
def job(since: int = 0, id: str = ""):
    current = runner.current
    if current is None:
        return {"job": None}
    # Якщо сторінка знає інше (старіше) завдання — віддаємо журнал нового з початку.
    return {"job": current.snapshot(since if id == current.id else 0)}


@app.get("/api/episodes/{episode}")
def episode_detail(episode: str):
    _episode(episode)
    store = EpisodeStore(episode)
    cfg = Config.load()
    status = pipeline.episode_status(episode)
    playlists = cfg.get("youtube", "playlists", default={}) or {}
    return {
        **status,
        "metadata": store.load_json("metadata.json") if store.exists("metadata.json") else None,
        "games": store.load_json("games.json") if store.exists("games.json") else None,
        "telegram": store.read_text("telegram.md") if store.exists("telegram.md") else None,
        "playlist": playlists.get(status["series"] or ""),
        "media_version": int(store.path("video.mp4").stat().st_mtime) if store.exists("video.mp4") else 0,
        "metadata_stale": _newer(store, "games.json", "metadata.json"),
    }


def _newer(store: EpisodeStore, a: str, b: str) -> bool:
    """Чи змінювався файл a після b (напр. список ігор після генерації описів)."""
    if not (store.exists(a) and store.exists(b)):
        return False
    return store.path(a).stat().st_mtime > store.path(b).stat().st_mtime


@app.post("/api/episodes")
def create_episode(
    episode: str = Form(...),
    series: str = Form(...),
    audio: UploadFile = File(...),
):
    episode = _episode(episode.strip(), must_exist=False)
    if pipeline.episode_exists(episode) and pipeline.episode_status(episode)["steps"]["audio"]:
        raise HTTPException(409, f"Випуск {episode} уже існує — відкрий його зі списку.")
    if series not in pipeline.series_options(Config.load()):
        raise HTTPException(400, "Обери серію.")
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise HTTPException(400, "Потрібен аудіофайл: mp3, wav, m4a, aac, flac або ogg.")
    if runner.current and runner.current.status == "running":
        raise HTTPException(409, f"Зараз уже виконується «{runner.current.label}». Дочекайся завершення.")

    store = EpisodeStore(episode)
    # Пишемо в тимчасовий файл і перейменовуємо лише після повного копіювання —
    # обірване завантаження не повинне виглядати як готовий аудіофайл.
    partial = store.path(".upload.partial")
    with partial.open("wb") as out:
        shutil.copyfileobj(audio.file, out, length=8 * 1024 * 1024)
    dest = store.path(f"audio{suffix}")
    partial.replace(dest)

    runner.start(
        "prepare", episode, f"Підготовка випуску {episode}",
        lambda log: pipeline.prepare(episode, series, audio=str(dest), log=log),
    )
    return {"episode": episode}


@app.post("/api/episodes/{episode}/prepare")
def resume_prepare(episode: str):
    _episode(episode)
    series = pipeline.episode_status(episode)["series"]
    if not series:
        raise HTTPException(400, "Невідома серія епізоду — створи його заново.")
    runner.start(
        "prepare", episode, f"Підготовка випуску {episode}",
        lambda log: pipeline.prepare(episode, series, log=log),
    )
    return {"ok": True}


@app.put("/api/episodes/{episode}/metadata")
def save_metadata(episode: str, meta: dict):
    _episode(episode)
    pipeline.save_metadata(episode, meta)
    return {"ok": True}


@app.post("/api/episodes/{episode}/regenerate-metadata")
def regenerate_metadata(episode: str):
    _episode(episode)
    runner.start(
        "regenerate", episode, f"Оновлення описів випуску {episode}",
        lambda log: pipeline.regenerate_metadata(episode, log=log),
    )
    return {"ok": True}


class GameFix(BaseModel):
    original: str
    canonical: str = ""


@app.post("/api/episodes/{episode}/games/resolve")
def resolve_game(episode: str, body: GameFix):
    _episode(episode)
    pipeline.resolve_uncertain_game(episode, body.original, body.canonical)
    return {"ok": True}


@app.post("/api/episodes/{episode}/games/dismiss")
def dismiss_game(episode: str, body: GameFix):
    _episode(episode)
    pipeline.dismiss_uncertain_game(episode, body.original)
    return {"ok": True}


class PublishRequest(BaseModel):
    schedule: str = ""


@app.post("/api/episodes/{episode}/publish")
def publish(episode: str, body: PublishRequest):
    _episode(episode)
    schedule = _local_datetime(body.schedule) if body.schedule else ""
    if schedule:
        pipeline.parse_schedule(schedule)  # помилку з часом показуємо одразу, ще до старту
    runner.start(
        "publish", episode, f"Публікація випуску {episode}",
        lambda log: pipeline.publish(episode, schedule=schedule, log=log),
    )
    return {"ok": True}


@app.post("/api/episodes/{episode}/youtube-public")
def youtube_public(episode: str):
    _episode(episode)
    runner.start(
        "youtube-public", episode, "Відкриття відео на YouTube",
        lambda log: pipeline.youtube_go_public(episode, log=log),
    )
    return {"ok": True}


@app.post("/api/youtube/login")
def youtube_login():
    runner.start("youtube-login", None, "Вхід у YouTube", lambda log: pipeline.youtube_login(log=log))
    return {"ok": True}


class DraftRequest(BaseModel):
    regenerate: bool = False


@app.post("/api/episodes/{episode}/telegram/draft")
def telegram_draft(episode: str, body: DraftRequest):
    _episode(episode)
    runner.start(
        "telegram-draft", episode, "Чернетка поста в Telegram",
        lambda log: pipeline.telegram_draft(episode, regenerate=body.regenerate, log=log),
    )
    return {"ok": True}


class TelegramText(BaseModel):
    text: str


@app.put("/api/episodes/{episode}/telegram")
def telegram_save(episode: str, body: TelegramText):
    _episode(episode)
    pipeline.save_telegram(episode, body.text)
    return {"ok": True}


@app.post("/api/episodes/{episode}/telegram/send")
def telegram_send(episode: str):
    _episode(episode)
    runner.start(
        "telegram-send", episode, "Відправка поста в Telegram",
        lambda log: pipeline.telegram_send(episode, log=log),
    )
    return {"ok": True}


class ScheduleRequest(BaseModel):
    at: str


@app.post("/api/episodes/{episode}/telegram/schedule")
def telegram_schedule(episode: str, body: ScheduleRequest):
    _episode(episode)
    pipeline.telegram_schedule(episode, _local_datetime(body.at), log=lambda *_: None)
    return {"ok": True}


@app.delete("/api/episodes/{episode}/telegram/schedule")
def telegram_cancel(episode: str):
    _episode(episode)
    pipeline.telegram_cancel_schedule(episode, log=lambda *_: None)
    return {"ok": True}


@app.get("/media/{episode}/{name}")
def media(episode: str, name: str):
    _episode(episode)
    if name not in MEDIA:
        raise HTTPException(404)
    path = EpisodeStore(episode).path(name)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=MEDIA[name])


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ── запуск ──────────────────────────────────────────────────────────────────
def _already_running(url: str) -> bool:
    try:
        return httpx.get(url + "api/ping", timeout=1).json().get("app") == "podpub"
    except Exception:  # noqa: BLE001
        return False


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if _already_running(url):
        print(f"Інтерфейс уже працює: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(
        f"\n  podpub працює: {url}\n"
        "  Не закривай це вікно, поки працюєш з інтерфейсом — інакше він зупиниться.\n"
        "  Щоб вийти — просто закрий вікно.\n",
        flush=True,
    )
    if open_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
