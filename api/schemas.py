"""Pydantic request/response models for the FastAPI layer."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ─── BGM ──────────────────────────────────────────────────────────────────────

class BGMItem(BaseModel):
    name: str
    path: str
    size_bytes: int
    duration_s: float | None = None
    bpm: float | None = None


class BGMAnalyzeResult(BaseModel):
    name: str
    duration_s: float | None
    bpm: float | None
    error: str | None = None


# ─── Prompts ──────────────────────────────────────────────────────────────────

class PromptExpandRequest(BaseModel):
    song: str = Field(..., description="Song title")
    artist: str = Field(..., description="Artist name")
    n: int = Field(..., ge=1, le=60, description="Number of prompts to generate")
    style: str = Field("", description="Optional MV style / mother prompt")


class PromptExpandResponse(BaseModel):
    prompts: list[str]


# ─── Jobs ─────────────────────────────────────────────────────────────────────

class JobCreateRequest(BaseModel):
    prompts: list[str] = Field(..., min_length=1)
    tiktok_accounts: list[str] = Field(default_factory=list)
    bgm_path: str | None = None
    resolution: str = "480p"
    ratio: str = "9:16"
    duration: int = 5
    beats_per_cut: int = 2
    bpm: float | None = None
    song: str = ""
    artist: str = ""
    subtitle_mode: str = "whisper"
    subtitle_display: str = "word"
    whisper_model: str = "medium"
    whisper_language: str | None = None
    scheduled_at: str | None = None
    tiktok_bgm_song: str = ""
    tiktok_bgm_artist: str = ""


class JobSummary(BaseModel):
    job_id: str
    song: str
    artist: str
    overall_status: str
    created_at: str
    updated_at: str
    clip_counts: dict[str, int]


class JobDetail(BaseModel):
    """Full job state — raw JSON from state.json."""
    data: dict[str, Any]


class JobUpdateRequest(BaseModel):
    """Partial update — any subset of mutable fields."""
    overall_status: str | None = None
    clips: list[dict[str, Any]] | None = None
    stages: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


# ─── Pipeline ─────────────────────────────────────────────────────────────────

class StageTriggerResponse(BaseModel):
    job_id: str
    stage: str
    status: str = "started"
    message: str = "Stage scheduled in background. Poll GET /jobs/{job_id} for progress."
