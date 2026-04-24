"""Prompts router — thin wrapper over modules/prompt_expander.py (ARK Doubao)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.schemas import PromptExpandRequest, PromptExpandResponse
from modules.prompt_expander import expand_prompts

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.post("/expand", response_model=PromptExpandResponse)
def expand(req: PromptExpandRequest):
    """Generate N Seedance prompts from song/artist/style via ARK Doubao.

    Requires ARK_API_KEY (and ARK_TEXT_ENDPOINT) in the environment.
    """
    try:
        prompts = expand_prompts(
            song=req.song,
            artist=req.artist,
            n=req.n,
            style=req.style,
        )
    except KeyError as exc:
        raise HTTPException(status_code=500, detail=f"Missing env var: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    return PromptExpandResponse(prompts=prompts)
