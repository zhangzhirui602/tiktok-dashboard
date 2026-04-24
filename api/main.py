"""FastAPI entry point for the tiktok-dashboard API layer.

Run locally:
    uvicorn api.main:app --port 8001 --reload

Streamlit (port 8501) keeps running unchanged in a separate process.
"""
from __future__ import annotations

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import deps as _deps  # noqa: F401  # ensures sys.path is set before router imports
from api.routers import bgm, jobs, pipeline, prompts

load_dotenv()

app = FastAPI(
    title="TikTok Dashboard API",
    description=(
        "HTTP wrapper over the tiktok-dashboard business logic "
        "(BGM / prompts / jobs / pipeline stages). Local use only — "
        "no auth, paths are hard-coded to the developer's sibling repos."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"
app.include_router(bgm.router, prefix=API_PREFIX)
app.include_router(prompts.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
app.include_router(jobs.drafts_router, prefix=API_PREFIX)
app.include_router(pipeline.router, prefix=API_PREFIX)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
