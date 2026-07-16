from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Config

# Whisper API обмежує запит 25MB. Ріжемо довші епізоди на chunk'и (mono, низький
# бітрейт — якості достатньо для transcription) і склеюємо транскрипти по порядку.
MAX_CHUNK_SECONDS = 600
CHUNK_BITRATE = "64k"


class Transcriber(Protocol):
    def transcribe(self, audio_path: str, language: str = "uk") -> str: ...


def _split_audio(audio_path: str, out_dir: Path) -> list[Path]:
    pattern = str(out_dir / "chunk_%03d.mp3")
    cmd = [
        "ffmpeg", "-v", "error", "-i", audio_path,
        "-ac", "1", "-b:a", CHUNK_BITRATE,
        "-f", "segment", "-segment_time", str(MAX_CHUNK_SECONDS),
        pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("chunk_*.mp3"))


def _chunk_duration(chunk_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(chunk_path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _format_ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class WhisperAPITranscriber:
    """OpenAI Whisper API (whisper-1). Найпростіший дефолт.

    Довші епізоди (>~10 хв) перевищують ліміт запиту в 25MB — перед відправкою
    ріжемо на chunk'и через ffmpeg і склеюємо транскрипти по порядку.
    Транскрипт виходить з інлайн-мітками [HH:MM:SS] на межах сегментів Whisper
    (абсолютний час у епізоді), щоб Claude міг будувати реальні таймкоди YouTube.
    """

    def transcribe(self, audio_path: str, language: str = "uk") -> str:
        with tempfile.TemporaryDirectory() as tmp:
            chunks = _split_audio(audio_path, Path(tmp))
            lines: list[str] = []
            offset = 0.0
            for chunk_path in chunks:
                for seg_start, text in self._transcribe_chunk(chunk_path, language):
                    text = text.strip()
                    if text:
                        lines.append(f"[{_format_ts(offset + seg_start)}] {text}")
                offset += _chunk_duration(chunk_path)
            return "\n".join(lines)

    @retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(4))
    def _transcribe_chunk(self, chunk_path: Path, language: str) -> list[tuple[float, str]]:
        from openai import OpenAI

        client = OpenAI(api_key=Config.secret("OPENAI_API_KEY"))
        with open(chunk_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1", file=f, language=language, response_format="verbose_json"
            )
        segments = getattr(resp, "segments", None) or []
        return [(seg.start, seg.text) for seg in segments]


class DeepgramTranscriber:
    """TODO: реалізувати через Deepgram (Nova підтримує українську)."""

    def transcribe(self, audio_path: str, language: str = "uk") -> str:
        raise NotImplementedError("DeepgramTranscriber ще не реалізовано")


class FasterWhisperLocal:
    """Локально, без марж. вартості. Потребує `pip install faster-whisper` (+ бажано GPU)."""

    def __init__(self, model_size: str = "large-v3"):
        self.model_size = model_size

    def transcribe(self, audio_path: str, language: str = "uk") -> str:
        from faster_whisper import WhisperModel

        model = WhisperModel(self.model_size, device="auto", compute_type="auto")
        segments, _ = model.transcribe(audio_path, language=language)
        return " ".join(s.text.strip() for s in segments)


def get_transcriber(config: Config) -> Transcriber:
    provider = config.get("transcription", "provider", default="whisper_api")
    if provider == "whisper_api":
        return WhisperAPITranscriber()
    if provider == "deepgram":
        return DeepgramTranscriber()
    if provider == "faster_whisper_local":
        return FasterWhisperLocal()
    raise ValueError(f"Unknown transcription provider: {provider}")
