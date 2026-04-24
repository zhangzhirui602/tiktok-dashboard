"""Shared path/env constants for the FastAPI layer.

Re-exports from the existing tiktok-dashboard modules so we never duplicate
path logic. If a path changes in pipeline.py / bgm_manager.py, the API picks
it up automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.bgm_manager import BGM_DIR  # noqa: E402
from job_state import JOBS_DIR  # noqa: E402

__all__ = ["PROJECT_ROOT", "BGM_DIR", "JOBS_DIR"]

PROJECT_ROOT = _PROJECT_ROOT
