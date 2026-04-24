"""BGM router — thin wrapper over modules/bgm_manager.py."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

from api.deps import BGM_DIR
from api.schemas import BGMItem, BGMAnalyzeResult
from modules.bgm_manager import (
    AUDIO_EXTS,
    analyze_bgm,
    delete_bgm,
    ensure_bgm_dir,
    list_bgm_files,
)

router = APIRouter(prefix="/bgm", tags=["bgm"])


def _resolve(name: str) -> Path:
    """Resolve a BGM name to a safe path inside BGM_DIR (prevents traversal)."""
    ensure_bgm_dir()
    candidate = (BGM_DIR / name).resolve()
    try:
        candidate.relative_to(BGM_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid BGM name")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"BGM not found: {name}")
    return candidate


@router.get("", response_model=list[BGMItem])
def list_bgm():
    """List all BGM files in assets/bgm/."""
    items: list[BGMItem] = []
    for p in list_bgm_files():
        items.append(BGMItem(
            name=p.name,
            path=str(p),
            size_bytes=p.stat().st_size,
        ))
    return items


@router.post("", response_model=BGMItem, status_code=201)
async def upload_bgm(file: UploadFile = File(...)):
    """Upload a BGM file (multipart/form-data)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required")

    ext = Path(file.filename).suffix.lower()
    if ext not in AUDIO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format {ext}. Allowed: {sorted(AUDIO_EXTS)}",
        )

    ensure_bgm_dir()
    dest = BGM_DIR / Path(file.filename).name  # strip any path
    try:
        content = await file.read()
        dest.write_bytes(content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {exc}")

    return BGMItem(name=dest.name, path=str(dest), size_bytes=dest.stat().st_size)


@router.get("/{name}/metadata", response_model=BGMItem)
def get_metadata(name: str):
    """Analyze BGM and return name/path/size/duration/bpm."""
    p = _resolve(name)
    result = analyze_bgm(p)
    return BGMItem(
        name=p.name,
        path=str(p),
        size_bytes=p.stat().st_size,
        duration_s=result.get("duration_s"),
        bpm=result.get("bpm"),
    )


@router.post("/{name}/analyze", response_model=BGMAnalyzeResult)
def analyze(name: str):
    """Trigger BPM/duration analysis and return the raw analysis dict."""
    p = _resolve(name)
    result = analyze_bgm(p)
    return BGMAnalyzeResult(
        name=p.name,
        duration_s=result.get("duration_s"),
        bpm=result.get("bpm"),
        error=result.get("error"),
    )


@router.delete("/{name}", status_code=204)
def delete(name: str):
    p = _resolve(name)
    delete_bgm(p)
    return None
