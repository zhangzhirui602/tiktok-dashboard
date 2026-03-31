"""Orchestration pipeline: Seedance → Video Editor → TikTok

Each step yields (step_name, status, detail) tuples:
  status: "running" | "done" | "error"
  detail: optional string (URL, path, error message)
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import urllib.request
import asyncio
from pathlib import Path
from typing import Generator

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Sibling repo paths ────────────────────────────────────────────────────────
_DESKTOP = Path(__file__).resolve().parent.parent.parent  # …/Desktop

VIDEO_EDIT_ROOT = (
    _DESKTOP
    / "Video_Editing_FFmpeg_librosa_Whisper"
    / "Video-Editing-FFmpeg-librosa-Whisper-"
)
TIKTOK_SRC = (
    _DESKTOP
    / "mcp-tiktok-uploader-mcp"
    / "tiktok-uploader-mcp"
    / "src"
)

# ─── Seedance API ──────────────────────────────────────────────────────────────
_SEEDANCE_CREATE = (
    "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
)
_SEEDANCE_QUERY = (
    "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}"
)
_DEFAULT_MODEL = "doubao-seedance-1-5-pro-251215"


def _seedance_headers() -> dict:
    key = os.environ.get("ARK_API_KEY", "")
    if not key:
        raise EnvironmentError("ARK_API_KEY not set in environment")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _seedance_generate(
    prompt: str, resolution: str, ratio: str, duration: int
) -> str:
    """Submit a text-to-video task and poll until completion. Returns video URL."""
    payload = {
        "model": _DEFAULT_MODEL,
        "content": [{"type": "text", "text": prompt}],
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "watermark": False,
        "generate_audio": False,
    }
    r = requests.post(
        _SEEDANCE_CREATE, headers=_seedance_headers(), json=payload, timeout=60
    )
    raw = r.text  # keep raw text for debugging before parsing
    r.raise_for_status()

    body = r.json()
    if not isinstance(body, dict):
        raise ValueError(f"API returned unexpected type {type(body).__name__}: {raw}")

    # Volcengine may wrap response in {"data": {...}} or return id at top level
    data_obj = body.get("data", body)
    task_id = data_obj.get("id") if isinstance(data_obj, dict) else None
    if not task_id:
        raise ValueError(f"No task ID in response (HTTP {r.status_code}): {raw}")

    for _ in range(30):  # max ~5 minutes
        time.sleep(10)
        qr = requests.get(
            _SEEDANCE_QUERY.format(task_id=task_id),
            headers=_seedance_headers(),
            timeout=60,
        )
        qr.raise_for_status()
        data = qr.json()
        if not isinstance(data, dict):
            raise ValueError(f"Poll returned unexpected type: {qr.text}")

        # Support both top-level and data-wrapped responses
        data = data.get("data", data) if isinstance(data.get("data"), dict) else data
        status = data.get("status")
        if status == "succeeded":
            content = data.get("content", {})
            # API returns content as a dict, not a list
            if isinstance(content, dict):
                url = content.get("video_url") or content.get("url")
                if url:
                    return url
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        url = item.get("video_url") or item.get("url")
                        if url:
                            return url
            raise ValueError(f"Task succeeded but no video URL found: {data}")
        if status in {"failed", "expired"}:
            raise RuntimeError(f"Seedance task {status}: {data}")

    raise TimeoutError("Seedance task timed out after 5 minutes")


# ─── Video downloader ─────────────────────────────────────────────────────────

def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)


# ─── Video editing ────────────────────────────────────────────────────────────

def _ensure_video_edit_path() -> None:
    p = str(VIDEO_EDIT_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_module_from_file(name: str, file_path: Path):
    """Load a Python module directly from file, bypassing package __init__.py."""
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    # Python 3.13 dataclass processing expects the module to be registered.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_current_project_dir() -> Path:
    """Resolve the active video-editor project directory."""
    pm = _load_module_from_file(
        "project_manager", VIDEO_EDIT_ROOT / "cli" / "project_manager.py"
    )
    ctx = pm.get_context(VIDEO_EDIT_ROOT)
    return Path(ctx.project_dir)


def _format_exception(exc: Exception) -> str:
    """Return a user-facing error string that is never empty."""
    msg = str(exc).strip()
    if not msg:
        return f"{type(exc).__name__}: {exc!r}"
    return f"{type(exc).__name__}: {msg}"


def _ensure_windows_proactor_policy() -> None:
    """Playwright on Windows requires Proactor loop for subprocess support."""
    if sys.platform != "win32":
        return
    proactor_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor_cls is None:
        return
    current = asyncio.get_event_loop_policy()
    if isinstance(current, proactor_cls):
        return
    asyncio.set_event_loop_policy(proactor_cls())


def _generate_srt(video_path: Path, audio_path: str | None = None) -> str:  # noqa: ARG001
    """Transcribe the project's audio file to SRT. Returns SRT file path."""
    _ensure_video_edit_path()
    from src.config import load_config  # type: ignore[import]
    from src.transcriber import ensure_srt  # type: ignore[import]

    pm = _load_module_from_file(
        "project_manager", VIDEO_EDIT_ROOT / "cli" / "project_manager.py"
    )

    ctx = pm.get_context(VIDEO_EDIT_ROOT)
    cfg = load_config(
        project_dir=ctx.project_dir, verbose=False, require_videos=False
    )

    effective_audio = audio_path or cfg["audio_path"]
    if not Path(effective_audio).is_file():
        raise FileNotFoundError(f"Audio file not found: {effective_audio}")

    # Keep subtitle naming aligned with the selected audio file.
    srt_path = str(
        Path(ctx.project_dir)
        / "raw_materials"
        / "lyric"
        / f"{Path(effective_audio).stem}.srt"
    )

    return ensure_srt(
        effective_audio,
        srt_path,
        cfg["whisper_model"],
        cfg["language"],
        cfg.get("split_mode", "word"),
        cfg["temp_dir"],
        verbose=False,
    )


def _run_edit_pipeline(
    video_path: Path, srt_path: str, style: str | None, audio_path: str | None = None
) -> str:
    """Copy video into the video editor project and run the pipeline.
    Returns the final output file path."""
    _ensure_video_edit_path()
    import shutil
    from src.config import load_config  # type: ignore[import]
    from src.pipeline import run as pipeline_run  # type: ignore[import]

    pm = _load_module_from_file(
        "project_manager", VIDEO_EDIT_ROOT / "cli" / "project_manager.py"
    )

    ctx = pm.get_context(VIDEO_EDIT_ROOT)
    project_dir = ctx.project_dir

    videos_dir = project_dir / "raw_materials" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    dest = videos_dir / video_path.name
    if dest.resolve() != video_path.resolve():
        shutil.copy2(video_path, dest)

    # Temporarily override AUDIO_PATH so load_config inside pipeline_run picks it up
    _prev = os.environ.get("AUDIO_PATH")
    if audio_path:
        os.environ["AUDIO_PATH"] = audio_path
    try:
        cfg = load_config(project_dir=project_dir, verbose=False)
        pipeline_run(
            project_dir=project_dir,
            prepared_srt_path=srt_path,
            quiet=True,
            style=style,
        )
    finally:
        if _prev is None:
            os.environ.pop("AUDIO_PATH", None)
        else:
            os.environ["AUDIO_PATH"] = _prev

    return cfg["final_output"]


# ─── TikTok uploader ──────────────────────────────────────────────────────────

def _upload_tiktok(
    video_path: str, description: str, tiktok_account: str
) -> None:
    _ensure_windows_proactor_policy()

    if not Path(video_path).is_file():
        raise FileNotFoundError(f"Upload video not found: {video_path}")

    p = str(TIKTOK_SRC)
    if p not in sys.path:
        sys.path.insert(0, p)
    from tiktok_uploader.upload import upload_video  # type: ignore[import]

    account_key = f"TIKTOK_COOKIES_{tiktok_account.upper()}"
    configured = os.environ.get(account_key)
    cookies_path = configured if configured else tiktok_account

    # Resolve relative cookies paths against the dashboard root.
    cookies_file = Path(cookies_path)
    if not cookies_file.is_absolute():
        cookies_file = (Path(__file__).parent / cookies_file).resolve()

    if configured and not cookies_file.is_file():
        raise FileNotFoundError(
            f"Configured cookies file not found for {account_key}: {cookies_file}"
        )

    if not configured and not cookies_file.is_file():
        available = [
            key[len("TIKTOK_COOKIES_"):].lower()
            for key, val in os.environ.items()
            if key.startswith("TIKTOK_COOKIES_") and val
        ]
        raise ValueError(
            f"No cookies configured for account '{tiktok_account}'. "
            f"Available accounts: {available or ['default']}"
        )

    try:
        failed = upload_video(
            filename=video_path,
            description=description,
            cookies=str(cookies_file),
        )
    except Exception as exc:
        raise RuntimeError(
            f"TikTok uploader crashed. cookies={cookies_file} | {_format_exception(exc)}"
        ) from exc

    if failed:
        raise RuntimeError(
            f"TikTok upload reported failures: {failed}. "
            f"cookies={cookies_file}"
        )


# ─── Public API ───────────────────────────────────────────────────────────────

STEPS = [
    "generate_video",
    "download_video",
    "generate_srt",
    "edit_video",
    "upload_tiktok",
]

# Map dashboard style names → video editor STYLE_PRESETS keys (None = no filter)
STYLE_MAP: dict[str, str | None] = {
    "vintage": "vintage_film",
    "neon": "fresh_natural",
    "cinematic": "cinematic",
    "minimal": None,
}


def list_audio_files() -> list[str]:
    """Return absolute paths of audio files in the active project's song directory."""
    try:
        project_dir = _get_current_project_dir()
        song_dir = project_dir / "raw_materials" / "song"
    except Exception:
        # Fallback to root-level folder if project context is unavailable.
        song_dir = VIDEO_EDIT_ROOT / "raw_materials" / "song"
    if not song_dir.exists():
        return []
    exts = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
    return sorted(
        str(p) for p in song_dir.iterdir()
        if p.suffix.lower() in exts
    )


def run_pipeline(
    prompt: str,
    style: str,
    tiktok_account: str,
    audio_path: str | None = None,
    resolution: str = "480p",
    ratio: str = "16:9",
    duration: int = 5,
) -> Generator[tuple[str, str, str | None], None, None]:
    """Orchestrate the full pipeline.

    Yields (step, status, detail) tuples.
    """
    tmp_dir = Path(__file__).parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    raw_video_path = tmp_dir / f"seedance_{int(time.time())}.mp4"
    mapped_style = STYLE_MAP.get(style, None)

    # ── 1. Generate video with Seedance ────────────────────────────────────────
    yield ("generate_video", "running", None)
    try:
        video_url = _seedance_generate(prompt, resolution, ratio, duration)
        yield ("generate_video", "done", video_url)
    except Exception as exc:
        yield ("generate_video", "error", _format_exception(exc))
        return

    # ── 2. Download video ──────────────────────────────────────────────────────
    yield ("download_video", "running", None)
    try:
        _download(video_url, raw_video_path)
        yield ("download_video", "done", str(raw_video_path))
    except Exception as exc:
        yield ("download_video", "error", _format_exception(exc))
        return

    # ── 3. Generate SRT ────────────────────────────────────────────────────────
    yield ("generate_srt", "running", None)
    srt_path: str
    try:
        srt_path = _generate_srt(raw_video_path, audio_path)
        yield ("generate_srt", "done", srt_path)
    except Exception as exc:
        yield ("generate_srt", "error", _format_exception(exc))
        return

    # ── 4. Edit video ──────────────────────────────────────────────────────────
    yield ("edit_video", "running", None)
    final_output: str
    try:
        final_output = _run_edit_pipeline(raw_video_path, srt_path, mapped_style, audio_path)
        yield ("edit_video", "done", final_output)
    except Exception as exc:
        yield ("edit_video", "error", _format_exception(exc))
        return

    # ── 5. Upload to TikTok ────────────────────────────────────────────────────
    yield ("upload_tiktok", "running", None)
    try:
        _upload_tiktok(final_output, prompt[:150], tiktok_account)
        yield ("upload_tiktok", "done", None)
    except Exception as exc:
        yield ("upload_tiktok", "error", _format_exception(exc))
