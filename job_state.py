"""断点续传状态管理模块

每个任务对应 tmp/jobs/{job_id}/ 目录，状态持久化在 state.json。
支持单片段失败不影响其他片段，重启后自动从未完成处继续。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# ─── Constants ────────────────────────────────────────────────────────────────

JOBS_DIR = Path(__file__).parent / "tmp" / "jobs"

# Overall job status values
STATUS_CREATING        = "creating"
STATUS_GENERATING      = "generating"       # clips being generated
STATUS_PENDING_REVIEW  = "pending_review"   # all clips done, waiting user confirm
STATUS_MERGING         = "merging"
STATUS_PENDING_SRT     = "pending_srt"
STATUS_SRT_REVIEW      = "srt_review"       # waiting user to confirm subtitles
STATUS_UPLOADING       = "uploading"
STATUS_SCHEDULED       = "scheduled"
STATUS_COMPLETED       = "completed"
STATUS_FAILED          = "failed"

# Clip-level status values
CLIP_PENDING   = "pending"
CLIP_RUNNING   = "running"
CLIP_DONE      = "done"
CLIP_FAILED    = "failed"
CLIP_SKIPPED   = "skipped"   # user chose to skip this clip

# Stage-level status values
STAGE_PENDING  = "pending"
STAGE_RUNNING  = "running"
STAGE_DONE     = "done"
STAGE_FAILED   = "failed"


# ─── JobState ─────────────────────────────────────────────────────────────────

class JobState:
    """In-memory view of a job's persistent state with atomic save."""

    def __init__(self, data: dict, job_dir: Path) -> None:
        self._data = data
        self._job_dir = job_dir

    # ── Factory methods ──────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        prompts: list[str],
        style: str,
        tiktok_accounts: list[str],
        bgm_path: str | None = None,
        resolution: str = "480p",
        ratio: str = "9:16",
        duration: int = 5,
        beats_per_cut: int = 2,
        bpm: float | None = None,
        song: str = "",
        artist: str = "",
        subtitle_mode: str = "whisper",
        subtitle_display: str = "word",
        whisper_model: str = "medium",
        whisper_language: str | None = None,
        scheduled_at: str | None = None,
        tiktok_bgm_song: str = "",
        tiktok_bgm_artist: str = "",
    ) -> "JobState":
        """Create a brand-new job and persist it immediately."""
        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        now = _now()
        data: dict[str, Any] = {
            "job_id": job_id,
            "created_at": now,
            "updated_at": now,
            "song": song,
            "artist": artist,
            "overall_status": STATUS_CREATING,
            "params": {
                "prompts": prompts,
                "style": style,
                "tiktok_accounts": tiktok_accounts,
                "bgm_path": bgm_path,
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "beats_per_cut": beats_per_cut,
                "bpm": bpm,
                "subtitle_mode": subtitle_mode,
                "subtitle_display": subtitle_display,
                "whisper_model": whisper_model,
                "whisper_language": whisper_language,
                "scheduled_at": scheduled_at,
                "tiktok_bgm_song": tiktok_bgm_song,
                "tiktok_bgm_artist": tiktok_bgm_artist,
            },
            "clips": [
                _make_clip(i, p) for i, p in enumerate(prompts)
            ],
            "stages": {
                "merge": _make_stage(),
                "srt":   {**_make_stage(), "srt_path": None, "content": None},
                "upload": {**_make_stage(), "scheduled_at": scheduled_at, "results": {}},
            },
        }

        state = cls(data, job_dir)
        state.save()
        return state

    @classmethod
    def load(cls, job_id: str) -> "JobState":
        """Load an existing job from disk. Raises FileNotFoundError if missing."""
        job_dir = JOBS_DIR / job_id
        state_file = job_dir / "state.json"
        if not state_file.exists():
            raise FileNotFoundError(f"Job state not found: {state_file}")
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        return cls(data, job_dir)

    @classmethod
    def load_all(cls) -> list["JobState"]:
        """Load all jobs from disk, newest first."""
        if not JOBS_DIR.exists():
            return []
        jobs = []
        for job_dir in sorted(JOBS_DIR.iterdir(), reverse=True):
            state_file = job_dir / "state.json"
            if state_file.exists():
                try:
                    jobs.append(cls.load(job_dir.name))
                except Exception:
                    pass
        return jobs

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Atomically write state.json (write to .tmp then rename).

        On Windows, Defender/Search may briefly lock the destination file
        right after a write. Retry up to 5 times with short back-off.
        """
        self._data["updated_at"] = _now()
        state_file = self._job_dir / "state.json"
        tmp_file   = self._job_dir / "state.json.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                tmp_file.replace(state_file)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))  # 50ms, 100ms, 150ms, 200ms

    # ── Property accessors ───────────────────────────────────────────────────

    @property
    def job_id(self) -> str:
        return self._data["job_id"]

    @property
    def job_dir(self) -> Path:
        return self._job_dir

    @property
    def overall_status(self) -> str:
        return self._data["overall_status"]

    @overall_status.setter
    def overall_status(self, value: str) -> None:
        self._data["overall_status"] = value

    @property
    def params(self) -> dict:
        return self._data["params"]

    @property
    def clips(self) -> list[dict]:
        return self._data["clips"]

    @property
    def stages(self) -> dict:
        return self._data["stages"]

    @property
    def song(self) -> str:
        return self._data.get("song", "")

    @property
    def artist(self) -> str:
        return self._data.get("artist", "")

    @property
    def created_at(self) -> str:
        return self._data.get("created_at", "")

    @property
    def updated_at(self) -> str:
        return self._data.get("updated_at", "")

    # ── Clip operations ──────────────────────────────────────────────────────

    def set_clip_running(self, index: int) -> None:
        self.clips[index]["status"] = CLIP_RUNNING
        self.clips[index]["error"] = None
        self.save()

    def set_clip_done(self, index: int, video_url: str, local_path: str) -> None:
        self.clips[index]["status"] = CLIP_DONE
        self.clips[index]["video_url"] = video_url
        self.clips[index]["local_path"] = local_path
        self.clips[index]["error"] = None
        self.clips[index]["generated_at"] = _now()
        self.save()

    def set_clip_failed(self, index: int, error: str) -> None:
        self.clips[index]["status"] = CLIP_FAILED
        self.clips[index]["error"] = error
        self.save()

    def reset_clip(self, index: int, new_prompt: str | None = None) -> None:
        """Reset a clip to pending so it gets regenerated."""
        self.clips[index]["status"] = CLIP_PENDING
        self.clips[index]["video_url"] = None
        self.clips[index]["local_path"] = None
        self.clips[index]["error"] = None
        self.clips[index]["confirmed"] = False
        if new_prompt is not None:
            self.clips[index]["prompt"] = new_prompt
            self.params["prompts"][index] = new_prompt
        self.save()

    def confirm_clip(self, index: int) -> None:
        self.clips[index]["confirmed"] = True
        self.save()

    def all_clips_done(self) -> bool:
        return all(c["status"] == CLIP_DONE for c in self.clips)

    def all_clips_confirmed(self) -> bool:
        return all(
            c["status"] == CLIP_DONE and c.get("confirmed", False)
            for c in self.clips
        )

    def pending_clips(self) -> list[int]:
        """Return indices of clips that still need generation."""
        return [
            c["index"] for c in self.clips
            if c["status"] in (CLIP_PENDING, CLIP_FAILED)
        ]

    def done_clips(self) -> list[dict]:
        return [c for c in self.clips if c["status"] == CLIP_DONE]

    # ── Stage operations ─────────────────────────────────────────────────────

    def set_stage_running(self, stage: str) -> None:
        self.stages[stage]["status"] = STAGE_RUNNING
        self.stages[stage]["error"] = None
        self.save()

    def set_stage_done(self, stage: str, **extra) -> None:
        self.stages[stage]["status"] = STAGE_DONE
        self.stages[stage]["error"] = None
        self.stages[stage].update(extra)
        self.save()

    def set_stage_failed(self, stage: str, error: str) -> None:
        self.stages[stage]["status"] = STAGE_FAILED
        self.stages[stage]["error"] = error
        self.save()

    def stage_is_done(self, stage: str) -> bool:
        return self.stages[stage]["status"] == STAGE_DONE

    # ── Summary helpers ──────────────────────────────────────────────────────

    def clip_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {
            CLIP_PENDING: 0, CLIP_RUNNING: 0,
            CLIP_DONE: 0, CLIP_FAILED: 0, CLIP_SKIPPED: 0,
        }
        for c in self.clips:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        return counts

    def to_dict(self) -> dict:
        """Return a shallow copy of the raw data dict."""
        return dict(self._data)

    def is_resumable(self) -> bool:
        """Return True if the job is not yet completed or failed."""
        return self.overall_status not in (STATUS_COMPLETED, STATUS_FAILED)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _make_clip(index: int, prompt: str) -> dict:
    return {
        "index": index,
        "prompt": prompt,
        "status": CLIP_PENDING,
        "video_url": None,
        "local_path": None,
        "error": None,
        "confirmed": False,
        "generated_at": None,
    }


def _make_stage() -> dict:
    return {"status": STAGE_PENDING, "error": None}


# ─── Convenience ──────────────────────────────────────────────────────────────

def get_incomplete_jobs() -> list[JobState]:
    """Return jobs that are in progress and can be resumed."""
    return [j for j in JobState.load_all() if j.is_resumable()]
