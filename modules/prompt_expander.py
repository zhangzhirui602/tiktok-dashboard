"""ARK API (Doubao) — AI Prompt 自动扩展

调用字节跳动火山引擎 ARK 文本生成模型，根据歌曲名、艺术家名、MV 风格描述，
自动生成 N 条 Seedance 英文片段 prompt。

依赖环境变量：
  ARK_API_KEY       — 火山引擎 ARK API Key（与 Seedance 共用）
  ARK_TEXT_ENDPOINT — 豆包文字模型推理接入点 ID，格式 ep-xxxx
"""

from __future__ import annotations

import json
import os

import requests

_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"

_SYSTEM_PROMPT = """\
You are a professional MV storyboard writer for Seedance AI video generation.

Seedance prompt structure (always follow this order):
  [subject + action], [environment/setting], [camera movement], [lighting], [mood/atmosphere], [visual style]

Example of a well-formed prompt:
  "A young woman dancing slowly on a rain-soaked rooftop, wide shot pulling back to \
reveal the city skyline at dusk, warm golden sidelight, melancholic yet hopeful mood, \
cinematic 35mm film aesthetic"

Instructions:
- Generate exactly N self-contained English prompts, each describing one 5-second clip
- Scenes must have narrative continuity: intro → development → climax → resolution
- If an MV style description is provided, use it as the visual anchor — all scenes \
must feel cohesive with that vision
- Each prompt: 50-100 words, vivid and specific
- Return a JSON array of N strings only — no labels, no numbering, no extra text\
"""


def expand_prompts(song: str, artist: str, n: int, style: str = "") -> list[str]:
    """Generate N Seedance prompts via ARK Doubao.

    Args:
        song:   Song title.
        artist: Artist name.
        n:      Number of prompts to generate (must match clip count).
        style:  Optional MV style description / mother prompt.

    Returns:
        List of N English prompt strings.

    Raises:
        KeyError:     If ARK_API_KEY or ARK_TEXT_ENDPOINT is not set.
        requests.HTTPError: If the API call fails.
        ValueError:   If the response cannot be parsed or has wrong count.
    """
    api_key = os.environ["ARK_API_KEY"]
    model = os.environ["ARK_TEXT_ENDPOINT"]

    user_parts = [f'Song: "{song}" by {artist}.']
    if style.strip():
        user_parts.append(f"MV Style: {style.strip()}")
    user_parts.append(f"Generate {n} MV scene prompts.")
    user_msg = "\n".join(user_parts)

    resp = requests.post(
        f"{_ARK_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.9,
        },
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if the model wraps output in ```json ... ```
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first and last fence lines
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        raw = "\n".join(inner).strip()

    prompts = json.loads(raw)

    if not isinstance(prompts, list):
        raise ValueError(f"Expected a JSON array, got: {type(prompts).__name__}")
    if len(prompts) != n:
        raise ValueError(f"Expected {n} prompts, got {len(prompts)}")

    return [str(p).strip() for p in prompts]
