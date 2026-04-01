"""Orchestration pipeline: Seedance → Download → Merge → SRT → TikTok

两套入口：
  - run_pipeline()     旧版单片段接口（向后兼容，app.py v1 可继续使用）
  - run_job_clips()    新版多片段断点续传接口（dashboard v2 使用）

每步产生 (event, data) 事件流，调用方按需渲染进度。

事件类型（run_job_clips）：
  clip_start    {"index": int}
  clip_done     {"index": int, "video_url": str, "local_path": str}
  clip_failed   {"index": int, "error": str}
  clip_skip     {"index": int}          # 已完成，跳过
  stage_start   {"stage": str}
  stage_done    {"stage": str, **extra}
  stage_failed  {"stage": str, "error": str}
  job_done      {}
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

from job_state import (
    JobState,
    CLIP_DONE,
    STATUS_GENERATING,
    STATUS_PENDING_REVIEW,
    STATUS_MERGING,
    STATUS_PENDING_SRT,
    STATUS_SRT_REVIEW,
    STATUS_UPLOADING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)

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
    raw = r.text
    r.raise_for_status()

    body = r.json()
    if not isinstance(body, dict):
        raise ValueError(f"API returned unexpected type {type(body).__name__}: {raw}")

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

        data = data.get("data", data) if isinstance(data.get("data"), dict) else data
        status = data.get("status")
        if status == "succeeded":
            content = data.get("content", {})
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
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_current_project_dir() -> Path:
    pm = _load_module_from_file(
        "project_manager", VIDEO_EDIT_ROOT / "cli" / "project_manager.py"
    )
    ctx = pm.get_context(VIDEO_EDIT_ROOT)
    return Path(ctx.project_dir)


def _format_exception(exc: Exception) -> str:
    msg = str(exc).strip()
    if not msg:
        return f"{type(exc).__name__}: {exc!r}"
    return f"{type(exc).__name__}: {msg}"


def _ensure_windows_proactor_policy() -> None:
    if sys.platform != "win32":
        return
    proactor_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor_cls is None:
        return
    current = asyncio.get_event_loop_policy()
    if isinstance(current, proactor_cls):
        return
    asyncio.set_event_loop_policy(proactor_cls())


def _generate_srt(video_path: Path, audio_path: str | None = None) -> str:
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
            f"TikTok upload reported failures: {failed}. cookies={cookies_file}"
        )


# ─── Public API ───────────────────────────────────────────────────────────────

STEPS = [
    "generate_video",
    "download_video",
    "generate_srt",
    "edit_video",
    "upload_tiktok",
]

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
        song_dir = VIDEO_EDIT_ROOT / "raw_materials" / "song"
    if not song_dir.exists():
        return []
    exts = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
    return sorted(
        str(p) for p in song_dir.iterdir()
        if p.suffix.lower() in exts
    )


# ─── Legacy single-clip pipeline (backward compatible) ────────────────────────

def run_pipeline(
    prompt: str,
    style: str,
    tiktok_account: str,
    audio_path: str | None = None,
    resolution: str = "480p",
    ratio: str = "16:9",
    duration: int = 5,
) -> Generator[tuple[str, str, str | None], None, None]:
    """Orchestrate the full single-clip pipeline (v1 interface).

    Yields (step, status, detail) tuples.
    """
    tmp_dir = Path(__file__).parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    raw_video_path = tmp_dir / f"seedance_{int(time.time())}.mp4"
    mapped_style = STYLE_MAP.get(style, None)

    yield ("generate_video", "running", None)
    try:
        video_url = _seedance_generate(prompt, resolution, ratio, duration)
        yield ("generate_video", "done", video_url)
    except Exception as exc:
        yield ("generate_video", "error", _format_exception(exc))
        return

    yield ("download_video", "running", None)
    try:
        _download(video_url, raw_video_path)
        yield ("download_video", "done", str(raw_video_path))
    except Exception as exc:
        yield ("download_video", "error", _format_exception(exc))
        return

    yield ("generate_srt", "running", None)
    srt_path: str
    try:
        srt_path = _generate_srt(raw_video_path, audio_path)
        yield ("generate_srt", "done", srt_path)
    except Exception as exc:
        yield ("generate_srt", "error", _format_exception(exc))
        return

    yield ("edit_video", "running", None)
    final_output: str
    try:
        final_output = _run_edit_pipeline(raw_video_path, srt_path, mapped_style, audio_path)
        yield ("edit_video", "done", final_output)
    except Exception as exc:
        yield ("edit_video", "error", _format_exception(exc))
        return

    yield ("upload_tiktok", "running", None)
    try:
        _upload_tiktok(final_output, prompt[:150], tiktok_account)
        yield ("upload_tiktok", "done", None)
    except Exception as exc:
        yield ("upload_tiktok", "error", _format_exception(exc))


# ─── New multi-clip pipeline with checkpoint resume ───────────────────────────

Event = tuple[str, dict]


def run_job_clips(
    job: JobState,
    stop_flag: "list[bool] | None" = None,
) -> Generator[Event, None, None]:
    """Generate all pending clips for a job with checkpoint resume.

    stop_flag: pass a mutable list [False]; set stop_flag[0] = True to request
    a graceful pause between clips.

    Yields events: (event_type, data_dict)

    Does NOT run merge/srt/upload — those are separate steps initiated by the UI.
    """
    params = job.params
    resolution = params.get("resolution", "480p")
    ratio      = params.get("ratio", "9:16")
    duration   = int(params.get("duration", 5))

    pending = job.pending_clips()
    if not pending:
        # Nothing to do — all clips already done or skipped
        return

    job.overall_status = STATUS_GENERATING
    job.save()

    for idx in pending:
        # Check stop flag between clips
        if stop_flag and stop_flag[0]:
            break

        clip = job.clips[idx]
        prompt = clip["prompt"]

        # ── Generate ──────────────────────────────────────────────────────────
        yield ("clip_start", {"index": idx, "prompt": prompt})
        job.set_clip_running(idx)

        try:
            video_url = _seedance_generate(prompt, resolution, ratio, duration)
        except Exception as exc:
            err = _format_exception(exc)
            job.set_clip_failed(idx, err)
            yield ("clip_failed", {"index": idx, "error": err, "stage": "generate"})
            continue  # don't abort other clips

        # ── Download ──────────────────────────────────────────────────────────
        local_path = job.job_dir / f"clip_{idx:03d}.mp4"
        try:
            _download(video_url, local_path)
        except Exception as exc:
            err = _format_exception(exc)
            job.set_clip_failed(idx, err)
            yield ("clip_failed", {"index": idx, "error": err, "stage": "download"})
            continue

        # ── Save ──────────────────────────────────────────────────────────────
        job.set_clip_done(idx, video_url, str(local_path))
        yield ("clip_done", {"index": idx, "video_url": video_url, "local_path": str(local_path)})

    # Update overall status based on result
    if job.all_clips_done():
        job.overall_status = STATUS_PENDING_REVIEW
    else:
        # Some clips failed or were interrupted; stay in generating so user can resume
        job.overall_status = STATUS_GENERATING
    job.save()

    yield ("job_clips_finished", {
        "done": len(job.done_clips()),
        "total": len(job.clips),
        "overall_status": job.overall_status,
    })


def run_job_merge(
    job: JobState,
) -> Generator[Event, None, None]:
    """Merge all confirmed clips with FFmpeg using the job's BGM.

    Yields stage_start / stage_done / stage_failed events.
    """
    if job.stage_is_done("merge"):
        yield ("stage_skip", {"stage": "merge"})
        return

    yield ("stage_start", {"stage": "merge"})
    job.set_stage_running("merge")

    bgm_path = job.params.get("bgm_path")
    mapped_style = STYLE_MAP.get(job.params.get("style", "minimal"), None)
    clip_paths = [
        c["local_path"] for c in job.clips
        if c["status"] == CLIP_DONE and c.get("confirmed", False) and c.get("local_path")
    ]

    if not clip_paths:
        err = "No confirmed clips to merge"
        job.set_stage_failed("merge", err)
        job.overall_status = STATUS_FAILED
        job.save()
        yield ("stage_failed", {"stage": "merge", "error": err})
        return

    try:
        output_path = _merge_clips(job, clip_paths, bgm_path, mapped_style)
        job.set_stage_done("merge", output_path=output_path)
        job.overall_status = STATUS_PENDING_SRT
        job.save()
        yield ("stage_done", {"stage": "merge", "output_path": output_path})
    except Exception as exc:
        err = _format_exception(exc)
        job.set_stage_failed("merge", err)
        job.overall_status = STATUS_FAILED
        job.save()
        yield ("stage_failed", {"stage": "merge", "error": err})


def run_job_srt(
    job: JobState,
) -> Generator[Event, None, None]:
    """Run Whisper on merged video and save SRT to job state."""
    if job.stage_is_done("srt"):
        yield ("stage_skip", {"stage": "srt"})
        return

    merge_output = job.stages.get("merge", {}).get("output_path")
    if not merge_output or not Path(merge_output).is_file():
        err = f"Merge output not found: {merge_output}"
        job.set_stage_failed("srt", err)
        yield ("stage_failed", {"stage": "srt", "error": err})
        return

    yield ("stage_start", {"stage": "srt"})
    job.set_stage_running("srt")

    bgm_path = job.params.get("bgm_path")
    try:
        srt_path = _generate_srt(Path(merge_output), bgm_path)
        with open(srt_path, encoding="utf-8") as f:
            srt_content = f.read()
        job.set_stage_done("srt", srt_path=srt_path, content=srt_content)
        job.overall_status = STATUS_SRT_REVIEW
        job.save()
        yield ("stage_done", {"stage": "srt", "srt_path": srt_path, "content": srt_content})
    except Exception as exc:
        err = _format_exception(exc)
        job.set_stage_failed("srt", err)
        yield ("stage_failed", {"stage": "srt", "error": err})


def run_job_upload(
    job: JobState,
    description: str,
) -> Generator[Event, None, None]:
    """Upload the final video to all configured TikTok accounts."""
    yield ("stage_start", {"stage": "upload"})
    job.set_stage_running("upload")
    job.overall_status = STATUS_UPLOADING
    job.save()

    merge_output = job.stages.get("merge", {}).get("output_path")
    if not merge_output or not Path(merge_output).is_file():
        err = f"Final video not found: {merge_output}"
        job.set_stage_failed("upload", err)
        job.overall_status = STATUS_FAILED
        job.save()
        yield ("stage_failed", {"stage": "upload", "error": err})
        return

    accounts = job.params.get("tiktok_accounts", [])
    results: dict[str, str] = {}

    for account in accounts:
        try:
            _upload_tiktok(merge_output, description[:150], account)
            results[account] = "success"
            yield ("upload_account_done", {"account": account})
        except Exception as exc:
            err = _format_exception(exc)
            results[account] = f"failed: {err}"
            yield ("upload_account_failed", {"account": account, "error": err})

    any_success = any(v == "success" for v in results.values())
    if any_success:
        job.set_stage_done("upload", results=results)
        job.overall_status = STATUS_COMPLETED
        job.save()
        yield ("stage_done", {"stage": "upload", "results": results})
    else:
        err = f"All uploads failed: {results}"
        job.set_stage_failed("upload", err)
        job.overall_status = STATUS_FAILED
        job.save()
        yield ("stage_failed", {"stage": "upload", "error": err})


# ─── FFmpeg clip merge (internal) ─────────────────────────────────────────────

def _merge_clips(
    job: JobState,
    clip_paths: list[str],
    bgm_path: str | None,
    style: str | None,
) -> str:
    """Concatenate clips with FFmpeg and overlay BGM. Returns output path."""
    import subprocess
    import shutil

    output_path = str(job.job_dir / "merged.mp4")
    concat_list = job.job_dir / "concat.txt"

    # Write FFmpeg concat file
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in clip_paths:
            # FFmpeg requires forward slashes even on Windows
            safe = Path(p).as_posix()
            f.write(f"file '{safe}'\n")

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    if bgm_path and Path(bgm_path).is_file():
        # Concat clips then mix BGM
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", bgm_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest",
            output_path,
        ]
    else:
        # Concat only, no audio replacement
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            output_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg merge failed:\n{result.stderr[-2000:]}")

    return output_path
