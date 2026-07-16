from __future__ import annotations

import subprocess

from .config import Config


def render_video(audio_path: str, out_path: str, config: Config, bg_path: str | None = None) -> str:
    """Статичне зображення + анімована аудіохвиля → mp4 (ffmpeg)."""
    v = config.raw.get("video", {})
    width, height = str(v.get("resolution", "1920x1080")).split("x")
    fps = str(v.get("fps", 30))
    mode = v.get("waveform_mode", "cline")
    colors = v.get("waveform_colors", "0xE0E0E0")
    wave_h = str(v.get("waveform_height", 260))
    background = bg_path or v.get("default_background", "assets/background_default.png")

    filter_complex = (
        # фон, що не має точних пропорцій 16:9: заповнюємо кадр розмитою обрізаною
        # копією, зверху вписуємо картинку цілою (без спотворення) по центру.
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=30,setsar=1[bgblur];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1[fit];"
        f"[bgblur][fit]overlay=(W-w)/2:(H-h)/2[bg];"
        f"[1:a]showwaves=s={width}x{wave_h}:mode={mode}:colors={colors}:rate={fps}[w];"
        f"[bg][w]overlay=(W-w)/2:H-h-40[v]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", background,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-r", fps,
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def render_thumbnail(bg_path: str, out_path: str, width: int = 1280, height: int = 720) -> str:
    """YouTube-обкладинка (JPEG, ліміт YouTube — 2MB) з того самого фону й тим самим
    fit+blur підходом, що й render_video, — щоб мигцем прев'ю збігалося з відео."""
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=20,setsar=1[bgblur];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1[fit];"
        f"[bgblur][fit]overlay=(W-w)/2:(H-h)/2"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", bg_path,
        "-filter_complex", filter_complex,
        "-frames:v", "1", "-q:v", "3",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path
