"""Jobs router — CRUD over job_state.JobState."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.schemas import (
    JobCreateRequest,
    JobDetail,
    JobSummary,
    JobUpdateRequest,
)
from job_state import JobState, STATUS_CREATING

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _summary(job: JobState) -> JobSummary:
    return JobSummary(
        job_id=job.job_id,
        song=job.song,
        artist=job.artist,
        overall_status=job.overall_status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        clip_counts=job.clip_counts(),
    )


@router.post("", response_model=JobDetail, status_code=201)
def create_job(req: JobCreateRequest):
    """Create a new job and persist state.json under tmp/jobs/{id}/."""
    try:
        job = JobState.create(
            prompts=req.prompts,
            tiktok_accounts=req.tiktok_accounts,
            bgm_path=req.bgm_path,
            resolution=req.resolution,
            ratio=req.ratio,
            duration=req.duration,
            beats_per_cut=req.beats_per_cut,
            bpm=req.bpm,
            song=req.song,
            artist=req.artist,
            subtitle_mode=req.subtitle_mode,
            subtitle_display=req.subtitle_display,
            whisper_model=req.whisper_model,
            whisper_language=req.whisper_language,
            scheduled_at=req.scheduled_at,
            tiktok_bgm_song=req.tiktok_bgm_song,
            tiktok_bgm_artist=req.tiktok_bgm_artist,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    return JobDetail(data=job.to_dict())


@router.get("", response_model=list[JobSummary])
def list_jobs():
    """List all jobs (newest first)."""
    return [_summary(j) for j in JobState.load_all()]


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: str):
    try:
        job = JobState.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return JobDetail(data=job.to_dict())


@router.patch("/{job_id}", response_model=JobDetail)
def update_job(job_id: str, req: JobUpdateRequest):
    """Partial update — overwrites any of overall_status / clips / stages / params."""
    try:
        job = JobState.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    data = job.to_dict()
    patch = req.model_dump(exclude_none=True)

    if "overall_status" in patch:
        job.overall_status = patch["overall_status"]
    if "clips" in patch:
        data["clips"] = patch["clips"]
        job._data["clips"] = patch["clips"]  # type: ignore[attr-defined]
    if "stages" in patch:
        job._data["stages"].update(patch["stages"])  # type: ignore[attr-defined]
    if "params" in patch:
        job._data["params"].update(patch["params"])  # type: ignore[attr-defined]

    job.save()
    return JobDetail(data=job.to_dict())


# ─── Drafts (filter view) ────────────────────────────────────────────────────

drafts_router = APIRouter(prefix="/drafts", tags=["drafts"])


@drafts_router.get("", response_model=list[JobSummary])
def list_drafts():
    """Jobs still in 'creating' state (not yet sent to generation)."""
    return [
        _summary(j) for j in JobState.load_all()
        if j.overall_status == STATUS_CREATING
    ]


@drafts_router.post("/{job_id}/approve", response_model=JobDetail)
def approve_draft(job_id: str):
    """Move a draft out of 'creating' — marks it ready for generation."""
    try:
        job = JobState.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if job.overall_status != STATUS_CREATING:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not a draft (status={job.overall_status})",
        )
    # Leave overall_status to be advanced by the first pipeline run;
    # approval here just flags the intent by clearing CREATING.
    from job_state import STATUS_GENERATING
    job.overall_status = STATUS_GENERATING
    job.save()
    return JobDetail(data=job.to_dict())
