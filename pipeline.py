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
import datetime
import time
import urllib.request
import asyncio
import re
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


def _ms_to_srt_time(ms: int) -> str:
    ms = max(0, ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_time_to_ms(ts: str) -> int:
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", ts.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {ts}")
    h, mm, s, ms = (int(g) for g in m.groups())
    return (((h * 60) + mm) * 60 + s) * 1000 + ms


def _split_words_for_srt(text: str) -> list[str]:
    # Use whitespace tokenization first; for CJK text without spaces, fall back to per-char.
    words = [w for w in text.split() if w]
    if words:
        return words
    chars = [c for c in text.strip() if not c.isspace()]
    return chars


def _expand_srt_to_word_level(srt_text: str) -> str:
    blocks = re.split(r"\r?\n\r?\n", srt_text.strip())
    lines: list[str] = []
    idx = 1

    for block in blocks:
        raw_lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(raw_lines) < 2:
            continue

        # Accept both with/without numeric index on first line.
        if re.fullmatch(r"\d+", raw_lines[0]):
            ts_line = raw_lines[1]
            text_lines = raw_lines[2:]
        else:
            ts_line = raw_lines[0]
            text_lines = raw_lines[1:]

        if "-->" not in ts_line:
            continue
        start_s, end_s = (p.strip() for p in ts_line.split("-->", 1))
        try:
            seg_s_ms = _srt_time_to_ms(start_s)
            seg_e_ms = _srt_time_to_ms(end_s)
        except ValueError:
            continue

        if seg_e_ms <= seg_s_ms:
            seg_e_ms = seg_s_ms + 1

        text = " ".join(text_lines).strip()
        if not text:
            continue

        words = _split_words_for_srt(text)
        if not words:
            continue

        duration = seg_e_ms - seg_s_ms
        per_word_ms = max(1, duration // len(words))

        for wi, word in enumerate(words):
            w_s = seg_s_ms + wi * per_word_ms
            w_e = w_s + per_word_ms if wi < len(words) - 1 else seg_e_ms
            lines += [str(idx), f"{_ms_to_srt_time(w_s)} --> {_ms_to_srt_time(w_e)}", word, ""]
            idx += 1

    if not lines:
        return srt_text.rstrip() + "\n"
    return "\n".join(lines).rstrip() + "\n"


def _normalize_srt_to_sentence_level(srt_text: str, max_words_per_sentence: int = 12) -> str:
    """Normalize SRT text to sentence-level subtitles.

    This is primarily used for Whisper CLI outputs where sentence mode may still
    arrive as long segment-level lines.
    """

    # First expand to synthetic word-level timing so we can reuse sentence grouping.
    expanded = _expand_srt_to_word_level(srt_text)
    blocks = re.split(r"\r?\n\r?\n", expanded.strip())

    word_entries: list[tuple[int, int, str]] = []
    for block in blocks:
        raw_lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(raw_lines) < 2:
            continue

        if re.fullmatch(r"\d+", raw_lines[0]):
            ts_line = raw_lines[1]
            text_lines = raw_lines[2:]
        else:
            ts_line = raw_lines[0]
            text_lines = raw_lines[1:]

        if "-->" not in ts_line:
            continue

        start_s, end_s = (p.strip() for p in ts_line.split("-->", 1))
        try:
            s_ms = _srt_time_to_ms(start_s)
            e_ms = _srt_time_to_ms(end_s)
        except ValueError:
            continue

        text = " ".join(text_lines).strip()
        if not text:
            continue
        word_entries.append((s_ms, max(s_ms + 1, e_ms), text))

    if not word_entries:
        return srt_text.rstrip() + "\n"

    sentences = _group_words_into_sentences(
        word_entries,
        max_words_per_sentence=max_words_per_sentence,
    )

    lines: list[str] = []
    for idx, (s_ms, e_ms, text) in enumerate(sentences, start=1):
        lines += [str(idx), f"{_ms_to_srt_time(s_ms)} --> {_ms_to_srt_time(e_ms)}", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def _group_words_into_sentences(
    word_entries: list[tuple[int, int, str]],
    pause_threshold_ms: int = 350,
    max_words_per_sentence: int = 12,
) -> list[tuple[int, int, str]]:
    """Group word-level (start_ms, end_ms, text) entries into sentence-level entries.

    Boundaries are determined by:
    - sentence punctuation (comma/period/question/exclamation, Chinese+English),
    - pause between words >= pause_threshold_ms,
    - max_words_per_sentence limit,
    - or end of input.
    """

    def _word_ends_with_boundary_punct(word: str) -> bool:
        trimmed = re.sub(r"[\s\"'”’)\]\}）】》」]+$", "", word)
        return bool(trimmed) and trimmed[-1] in ",，.。!?！？"

    def _join_sentence_words(words: list[str]) -> str:
        # Keep punctuation attached to previous token in spaced languages.
        joined = " ".join(words).strip()
        return re.sub(r"\s+([,，.。!?！？])", r"\1", joined)

    sentences: list[tuple[int, int, str]] = []
    if not word_entries:
        return sentences

    sent_start = word_entries[0][0]
    sent_words: list[str] = []

    for i, (s_ms, e_ms, word) in enumerate(word_entries):
        sent_words.append(word)
        is_last = i == len(word_entries) - 1
        should_split = False

        if _word_ends_with_boundary_punct(word):
            should_split = True

        if not should_split and len(sent_words) >= max_words_per_sentence:
            should_split = True

        if not should_split and not is_last:
            next_s_ms = word_entries[i + 1][0]
            if next_s_ms - e_ms >= pause_threshold_ms:
                should_split = True

        if is_last:
            should_split = True

        if should_split:
            sentence_text = _join_sentence_words(sent_words)
            if sentence_text:
                sentences.append((sent_start, e_ms, sentence_text))
            if not is_last:
                sent_start = word_entries[i + 1][0]
            sent_words = []

    return sentences


def _split_text_into_sentences(text: str, max_words_per_sentence: int = 12) -> list[str]:
    """Split plain text into sentence-like chunks by punctuation and max length.

    Used when Whisper does not provide per-word timestamps.
    """

    tokens = _split_words_for_srt(text)
    if not tokens:
        return []

    def _ends_with_boundary_punct(token: str) -> bool:
        trimmed = re.sub(r"[\s\"'”’)\]\}）】》」]+$", "", token)
        return bool(trimmed) and trimmed[-1] in ",，.。!?！？"

    def _join_tokens(parts: list[str]) -> str:
        joined = " ".join(parts).strip()
        return re.sub(r"\s+([,，.。!?！？])", r"\1", joined)

    chunks: list[str] = []
    current: list[str] = []
    for token in tokens:
        current.append(token)
        if _ends_with_boundary_punct(token) or len(current) >= max_words_per_sentence:
            chunk = _join_tokens(current)
            if chunk:
                chunks.append(chunk)
            current = []

    if current:
        chunk = _join_tokens(current)
        if chunk:
            chunks.append(chunk)

    return chunks


def _run_whisper(
    audio_path: str,
    out_srt: Path,
    model_name: str = "medium",
    language: str | None = None,
    subtitle_display: str = "word",
) -> None:
    """Transcribe audio with Whisper and write SRT to out_srt.
    Tries Python API first, falls back to CLI. No sibling-repo dependency.

    Args:
        model_name: Whisper model size (tiny/base/small/medium/large). Default: medium.
        language: ISO language code (e.g. "ko", "ja", "en") or None for auto-detect.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    out_srt.parent.mkdir(parents=True, exist_ok=True)

    try:
        import whisper as _whisper  # type: ignore[import]
        model = _whisper.load_model(model_name)
        transcribe_kwargs: dict = {
            "fp16": False,
            "condition_on_previous_text": False,
            "temperature": 0,
        }
        if language:
            transcribe_kwargs["language"] = language
        transcribe_kwargs["word_timestamps"] = True
        result = model.transcribe(str(audio_path), **transcribe_kwargs)

        # Collect all words with per-word timestamps
        all_words: list[dict] = []
        for seg in result.get("segments", []):
            all_words.extend(seg.get("words", []))

        lines: list[str] = []
        idx = 1

        if all_words:
            # Collect word-level entries: (start_ms, end_ms, text)
            word_entries: list[tuple[int, int, str]] = []
            for i, w in enumerate(all_words):
                word = w.get("word", "").strip()
                if not word:
                    continue
                s_ms = int(round(float(w["start"]) * 1000))
                if i + 1 < len(all_words):
                    next_s_ms = int(round(float(all_words[i + 1]["start"]) * 1000))
                    e_ms = max(s_ms + 1, min(int(round(float(w["end"]) * 1000)), next_s_ms - 50))
                else:
                    e_ms = max(s_ms + 1, int(round(float(w["end"]) * 1000)))

                # Some Whisper outputs may place multiple words in one "word" token.
                # Expand and spread timing so sentence splitting rules can still apply.
                sub_words = _split_words_for_srt(word)
                if len(sub_words) <= 1:
                    word_entries.append((s_ms, e_ms, word))
                    continue

                duration = max(1, e_ms - s_ms)
                per_sub_ms = max(1, duration // len(sub_words))
                for si, sub_word in enumerate(sub_words):
                    sw_s = s_ms + si * per_sub_ms
                    sw_e = sw_s + per_sub_ms if si < len(sub_words) - 1 else e_ms
                    word_entries.append((sw_s, sw_e, sub_word))

            if subtitle_display == "sentence":
                entries = _group_words_into_sentences(word_entries)
            else:
                entries = word_entries

            for s_ms, e_ms, text in entries:
                lines += [str(idx), f"{_ms_to_srt_time(s_ms)} --> {_ms_to_srt_time(e_ms)}", text, ""]
                idx += 1
        else:
            # Fallback: Whisper returned no per-word timestamps (common with music/BGM).
            for seg in result.get("segments", []):
                text = seg["text"].strip()
                if not text:
                    continue
                seg_s_ms = int(round(float(seg["start"]) * 1000))
                seg_e_ms = max(seg_s_ms + 1, int(round(float(seg["end"]) * 1000)))
                if subtitle_display == "sentence":
                    sentence_chunks = _split_text_into_sentences(text, max_words_per_sentence=12)
                    if not sentence_chunks:
                        continue

                    seg_duration = seg_e_ms - seg_s_ms
                    token_counts = [max(1, len(_split_words_for_srt(chunk))) for chunk in sentence_chunks]
                    total_tokens = max(1, sum(token_counts))
                    consumed_tokens = 0
                    cur_start = seg_s_ms

                    for si, chunk in enumerate(sentence_chunks):
                        consumed_tokens += token_counts[si]
                        if si == len(sentence_chunks) - 1:
                            cur_end = seg_e_ms
                        else:
                            ratio_end = seg_s_ms + int(round(seg_duration * (consumed_tokens / total_tokens)))
                            cur_end = max(cur_start + 1, min(ratio_end, seg_e_ms))

                        lines += [str(idx), f"{_ms_to_srt_time(cur_start)} --> {_ms_to_srt_time(cur_end)}", chunk, ""]
                        idx += 1
                        cur_start = cur_end
                else:
                    # Split into words and distribute time proportionally.
                    words = [w for w in text.split() if w]
                    if not words:
                        continue
                    duration = seg_e_ms - seg_s_ms
                    per_word_ms = duration // len(words)
                    for wi, word in enumerate(words):
                        w_s = seg_s_ms + wi * per_word_ms
                        w_e = w_s + per_word_ms if wi < len(words) - 1 else seg_e_ms
                        lines += [str(idx), f"{_ms_to_srt_time(w_s)} --> {_ms_to_srt_time(w_e)}", word, ""]
                        idx += 1

        out_srt.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return
    except ImportError:
        pass  # fall through to CLI
    except Exception as exc:
        raise RuntimeError(f"Whisper API failed: {exc}") from exc

    # CLI fallback
    whisper_cmd = _shutil.which("whisper") or "whisper"
    cli_cmd = [
        whisper_cmd, str(audio_path),
        "--model", model_name,
        "--output_dir", str(out_srt.parent),
        "--output_format", "srt",
        "--condition_on_previous_text", "False",
        "--word_timestamps", "True",
    ]
    if language:
        cli_cmd += ["--language", language]
    r = _subprocess.run(cli_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"whisper CLI failed: {(r.stderr or r.stdout).strip()}")

    # CLI names the file after the audio stem; move to our target path
    cli_srt = out_srt.parent / f"{Path(audio_path).stem}.srt"
    if cli_srt.exists() and cli_srt.resolve() != out_srt.resolve():
        cli_srt.replace(out_srt)  # replace() overwrites on Windows; rename() does not
    elif not out_srt.exists():
        srts = sorted(out_srt.parent.glob("*.srt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if srts:
            srts[0].replace(out_srt)
        else:
            raise FileNotFoundError(f"Whisper CLI completed but no SRT found in {out_srt.parent}")

    # Normalize CLI output so sentence/word modes are consistent with API path.
    try:
        original = out_srt.read_text(encoding="utf-8")
        if subtitle_display == "sentence":
            normalized = _normalize_srt_to_sentence_level(original, max_words_per_sentence=12)
        else:
            normalized = _expand_srt_to_word_level(original)
        out_srt.write_text(normalized, encoding="utf-8")
    except Exception:
        # Keep original CLI output if normalization fails.
        pass


def _run_edit_pipeline(
    video_path: Path, srt_path: str, audio_path: str | None = None
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
        )
    finally:
        if _prev is None:
            os.environ.pop("AUDIO_PATH", None)
        else:
            os.environ["AUDIO_PATH"] = _prev

    return cfg["final_output"]


# ─── TikTok uploader ──────────────────────────────────────────────────────────

def _upload_tiktok(
    video_path: str,
    description: str,
    tiktok_account: str,
    schedule: datetime.datetime | None = None,
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
            schedule=schedule,
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
        final_output = _run_edit_pipeline(raw_video_path, srt_path, audio_path)
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

    # Update overall status based on result.
    # Reload from disk first: the UI may have written STATUS_FAILED (Cancel button)
    # while the thread was finishing the current clip.  If so, preserve it.
    fresh = JobState.load(job.job_id)
    if fresh.overall_status == STATUS_FAILED:
        # Job was externally cancelled — keep FAILED, do not overwrite.
        job = fresh
    elif job.all_clips_done():
        job.overall_status = STATUS_PENDING_REVIEW
        job.save()
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
        output_path = _merge_clips(job, clip_paths, bgm_path)
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

    # If no BGM path, extract audio from the merged video so Whisper
    # always transcribes the actual audio the viewer will hear.
    audio_for_srt: str | None = bgm_path
    _extracted: Path | None = None
    if not audio_for_srt or not Path(audio_for_srt).is_file():
        import shutil, subprocess
        extracted = Path(merge_output).with_suffix(".wav")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        r = subprocess.run(
            [ffmpeg, "-y", "-i", merge_output, "-vn", "-ar", "16000", "-ac", "1", str(extracted)],
            capture_output=True,
        )
        if r.returncode == 0 and extracted.is_file():
            audio_for_srt = str(extracted)
            _extracted = extracted

    whisper_model = job.params.get("whisper_model", "medium")
    whisper_language = job.params.get("whisper_language") or None  # None = auto-detect
    subtitle_display = job.params.get("subtitle_display", "word")

    try:
        out_srt = job.job_dir / "subtitle.srt"
        _run_whisper(audio_for_srt or merge_output, out_srt, model_name=whisper_model, language=whisper_language, subtitle_display=subtitle_display)
        srt_path = str(out_srt)
        srt_content = out_srt.read_text(encoding="utf-8")
        job.set_stage_done(
            "srt",
            srt_path=srt_path,
            content=srt_content,
            audio_used=audio_for_srt or "unknown",
        )
        job.overall_status = STATUS_SRT_REVIEW
        job.save()
        yield ("stage_done", {"stage": "srt", "srt_path": srt_path, "content": srt_content})
    except Exception as exc:
        err = _format_exception(exc)
        job.set_stage_failed("srt", err)
        yield ("stage_failed", {"stage": "srt", "error": err})
    finally:
        if _extracted and _extracted.exists():
            _extracted.unlink(missing_ok=True)


def _srt_to_ass(srt_path: str, ass_path: str) -> None:
    """Convert SRT to ASS with explicit centering and styling."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 384\n"
        "PlayResY: 288\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Alignment=5 → numpad center (horizontally and vertically centered)
        # Outline=0, Shadow=0 → no border/shadow
        # PrimaryColour=&H00FFFFFF → white (ASS uses AABBGGRR order, 00=opaque)
        "Style: Default,Times New Roman,11,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _srt_time_to_ass(t: str) -> str:
        # SRT:  00:00:01,500  →  ASS:  0:00:01.50
        t = t.strip().replace(",", ".")
        h, m, rest = t.split(":", 2)
        s, ms = rest.split(".")
        return f"{int(h)}:{m}:{s}.{ms[:2]}"

    srt_text = Path(srt_path).read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    dialogues: list[str] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            start, end = lines[1].split(" --> ")
            start_ass = _srt_time_to_ass(start)
            end_ass   = _srt_time_to_ass(end)
            text = r"\N".join(line.strip() for line in lines[2:])
            dialogues.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}")
        except Exception:
            continue

    Path(ass_path).write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")


def burn_subtitles(video_path: str, srt_path: str, output_path: str) -> None:
    """Burn SRT subtitles into video.

    Converts SRT → ASS first (reliable styling/positioning), then uses the
    FFmpeg `ass` filter. Alignment=5 centers the subtitle both horizontally
    and vertically on screen.
    """
    import shutil, subprocess
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    ass_path = str(Path(srt_path).with_suffix(".ass"))
    _srt_to_ass(srt_path, ass_path)

    # Escape path for FFmpeg filter (Windows: C:\path → C\:/path)
    ass_escaped = str(Path(ass_path).resolve()).replace("\\", "/").replace(":", "\\:")
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-vf", f"ass='{ass_escaped}'",
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg subtitle burn failed:\n{result.stderr[-2000:]}")


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

    # Use pre-burned final.mp4 if available (burned at confirmation time),
    # otherwise burn now as fallback.
    srt_stage = job.stages.get("srt", {})
    srt_path = srt_stage.get("srt_path")
    final_path = str(job.job_dir / "final.mp4")
    upload_video = merge_output
    if Path(final_path).is_file():
        upload_video = final_path
    elif srt_path and Path(srt_path).is_file():
        try:
            burn_subtitles(merge_output, srt_path, final_path)
            upload_video = final_path
        except Exception as exc:
            yield ("subtitle_burn_warning", {"error": _format_exception(exc)})
            # Fall back to uploading without subtitles

    # Parse scheduled_at ISO string → datetime (naive, local time)
    schedule_dt: datetime.datetime | None = None
    scheduled_at_str = job.params.get("scheduled_at")
    if scheduled_at_str:
        try:
            schedule_dt = datetime.datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            pass

    accounts = job.params.get("tiktok_accounts", [])
    results: dict[str, str] = {}

    for account in accounts:
        try:
            _upload_tiktok(upload_video, description[:150], account, schedule=schedule_dt)
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
) -> str:
    """Concatenate clips with FFmpeg and overlay BGM. Returns output path.

    When bpm and beats_per_cut are available in job.params, each clip is
    trimmed to the beat interval (cut_s = 60/bpm * beats_per_cut) so that
    cuts land on the beat. The total duration is capped to the BGM length
    via -shortest.

    Without bpm info, falls back to simple concat (clips play at full length).
    """
    import subprocess
    import shutil

    output_path = str(job.job_dir / "merged.mp4")
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    bpm: float | None = job.params.get("bpm")
    beats_per_cut: int = int(job.params.get("beats_per_cut", 2))
    has_bgm = bgm_path and Path(bgm_path).is_file()

    if bpm and bpm > 0 and beats_per_cut > 0:
        # ── Beat-aware merge: trim each clip to the cut interval ──────────────
        cut_s = (60.0 / bpm) * beats_per_cut
        n = len(clip_paths)

        # Build filter_complex: trim + setpts for each clip, then concat
        filter_parts: list[str] = []
        for i in range(n):
            filter_parts.append(
                f"[{i}:v]trim=duration={cut_s:.6f},setpts=PTS-STARTPTS[v{i}]"
            )
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vout]")
        filter_complex = ";".join(filter_parts)

        # All clip inputs
        cmd: list[str] = [ffmpeg, "-y"]
        for p in clip_paths:
            cmd += ["-i", p]

        if has_bgm:
            cmd += ["-i", bgm_path]
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", f"{n}:a:0",
                "-c:v", "libx264", "-c:a", "aac",
                "-shortest",   # cap total at BGM length
                output_path,
            ]
        else:
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-c:v", "libx264",
                output_path,
            ]
    else:
        # ── Fallback: simple concat (no bpm info) ─────────────────────────────
        concat_list = job.job_dir / "concat.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in clip_paths:
                safe = Path(p).as_posix()
                f.write(f"file '{safe}'\n")

        if has_bgm:
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
