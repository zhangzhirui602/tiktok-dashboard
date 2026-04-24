"""Pipeline router — trigger stage functions as FastAPI BackgroundTasks.

The underlying stage functions (run_job_clips / run_job_srt / run_job_merge)
are generators that yield Event tuples. The BackgroundTask driver simply
exhausts the generator — the generator itself persists progress to state.json
via JobState, so the client polls GET /jobs/{id} to observe progress.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from api.schemas import StageTriggerResponse
from job_state import JobState
from pipeline import run_job_clips, run_job_merge, run_job_srt

router = APIRouter(prefix="/jobs", tags=["pipeline"])


def _drain(gen) -> None:
    """Exhaust a stage Generator. Events are ignored; state persists via JobState."""
    for _ in gen:
        pass


def _load_or_404(job_id: str) -> JobState:
    try:
        return JobState.load(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@router.post(
    "/{job_id}/generate",
    response_model=StageTriggerResponse,
    status_code=202,
)
def generate(job_id: str, background: BackgroundTasks):
    """Trigger Seedance clip generation for all pending clips."""
    job = _load_or_404(job_id)
    background.add_task(_drain, run_job_clips(job))
    return StageTriggerResponse(job_id=job_id, stage="clips")


@router.post(
    "/{job_id}/whisper",
    response_model=StageTriggerResponse,
    status_code=202,
)
def whisper(job_id: str, background: BackgroundTasks):
    """Trigger Whisper subtitle generation for the merged video."""
    job = _load_or_404(job_id)
    background.add_task(_drain, run_job_srt(job))
    return StageTriggerResponse(job_id=job_id, stage="srt")


@router.post(
    "/{job_id}/merge",
    response_model=StageTriggerResponse,
    status_code=202,
)
def merge(job_id: str, background: BackgroundTasks):
    """Trigger FFmpeg merge of confirmed clips with the job's BGM."""
    job = _load_or_404(job_id)
    background.add_task(_drain, run_job_merge(job))
    return StageTriggerResponse(job_id=job_id, stage="merge")
