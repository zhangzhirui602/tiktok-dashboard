"""BGM Manager — audio file scanning, BPM/duration analysis, clip count calculation.

BGM files live in  assets/bgm/  (auto-created).
BPM analysis uses librosa (already a transitive dependency via the sibling
Video-Editing-FFmpeg-librosa-Whisper repo).  If librosa is not importable,
duration and BPM fall back gracefully.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BGM_DIR = _PROJECT_ROOT / "assets" / "bgm"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}


# ─── File helpers ─────────────────────────────────────────────────────────────

def ensure_bgm_dir() -> Path:
    BGM_DIR.mkdir(parents=True, exist_ok=True)
    return BGM_DIR


def list_bgm_files() -> list[Path]:
    """Return sorted list of audio Paths in assets/bgm/."""
    ensure_bgm_dir()
    return sorted(
        p for p in BGM_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )


def save_uploaded_bgm(uploaded_file) -> Path:
    """Save a Streamlit UploadedFile to assets/bgm/. Returns destination Path."""
    ensure_bgm_dir()
    dest = BGM_DIR / uploaded_file.name
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return dest


def delete_bgm(path: Path) -> None:
    """Delete a BGM file from assets/bgm/."""
    path.unlink(missing_ok=True)


# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyze_bgm(path: Path) -> dict:
    """Analyse an audio file and return duration_s, bpm, error.

    Uses librosa for BPM estimation (loads first 60 s only, for speed).
    Falls back to mutagen for duration-only if librosa is unavailable.

    Returns:
        {
            "duration_s": float | None,
            "bpm":        float | None,
            "error":      str | None,
        }
    """
    try:
        import librosa  # type: ignore[import]
        import numpy as np  # type: ignore[import]

        # Duration from file metadata — fast, no full decode
        duration_s = float(librosa.get_duration(path=str(path)))

        # BPM from first 60 s
        y, sr = librosa.load(str(path), sr=None, mono=True, duration=60.0)
        raw_tempo = librosa.beat.beat_track(y=y, sr=sr)[0]
        # beat_track returns ndarray in older librosa; scalar in newer
        bpm = float(np.atleast_1d(raw_tempo)[0])

        return {"duration_s": duration_s, "bpm": bpm, "error": None}

    except Exception as exc:
        import sys, traceback
        full_tb = traceback.format_exc()
        full_tb += f"\n\nStreamlit Python: {sys.executable}"
        dur = _duration_fallback(path)
        return {
            "duration_s": dur,
            "bpm": None,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": full_tb,
        }


def _duration_fallback(path: Path) -> Optional[float]:
    """Try to read duration via mutagen, then ffprobe (both optional)."""
    # 1. mutagen — lightweight, no binary needed
    try:
        from mutagen import File as MFile  # type: ignore[import]
        f = MFile(str(path))
        if f and hasattr(f, "info") and f.info:
            return float(f.info.length)
    except Exception:
        pass

    # 2. ffprobe — available whenever ffmpeg is installed
    try:
        import subprocess, json, shutil
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            dur = data.get("format", {}).get("duration")
            if dur is not None:
                return float(dur)
    except Exception:
        pass

    return None


# ─── Clip count calculation ───────────────────────────────────────────────────

def calc_clip_count(duration_s: float, bpm: float, beats_per_cut: int) -> int:
    """Calculate how many 5-second Seedance clips are needed to cover the BGM.

    Each "cut" happens every `beats_per_cut` beats.
    cut_interval_s = (60 / bpm) * beats_per_cut
    clip_count     = ceil(duration_s / cut_interval_s)

    The result is clamped to [1, 60] to avoid runaway requests.
    """
    if bpm <= 0 or beats_per_cut <= 0 or duration_s <= 0:
        return 1
    beat_s = 60.0 / bpm
    cut_s  = beat_s * beats_per_cut
    return max(1, min(60, math.ceil(duration_s / cut_s)))


def format_duration(seconds: float) -> str:
    """Return a human-readable mm:ss string."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"
