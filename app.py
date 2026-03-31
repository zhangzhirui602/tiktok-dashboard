"""TikTok 自动发布仪表盘 — Streamlit UI"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from pipeline import STEPS, STYLE_MAP, list_audio_files, run_pipeline

load_dotenv()

# ─── Constants ────────────────────────────────────────────────────────────────
HISTORY_FILE = Path(__file__).parent / "history.json"

STEP_LABELS: dict[str, str] = {
    "generate_video": "生成视频 (Seedance API)",
    "download_video": "下载视频到本地",
    "generate_srt":   "生成字幕 (Whisper)",
    "edit_video":     "剪辑合成 (FFmpeg)",
    "upload_tiktok":  "上传 TikTok",
}

STEP_LABELS_EN: dict[str, str] = {
    "generate_video": "Generate video (Seedance API)",
    "download_video": "Download video locally",
    "generate_srt": "Generate subtitles (Whisper)",
    "edit_video": "Edit and compose (FFmpeg)",
    "upload_tiktok": "Upload to TikTok",
}

STYLE_OPTIONS = list(STYLE_MAP.keys())  # vintage, neon, cinematic, minimal


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def _append_history(record: dict) -> None:
    history = _load_history()
    history.insert(0, record)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[:100], f, ensure_ascii=False, indent=2)


def _get_tiktok_accounts() -> list[str]:
    accounts = [
        key[len("TIKTOK_COOKIES_"):].lower()
        for key, val in os.environ.items()
        if key.startswith("TIKTOK_COOKIES_") and val
    ]
    return accounts if accounts else ["default"]


def _t(lang: str, zh: str, en: str) -> str:
    return en if lang == "en" else zh


def _style_label(style_key: str, lang: str) -> str:
    labels = {
        "zh": {
            "vintage": "🎞 Vintage（复古）",
            "neon": "🌿 Neon → 清新自然",
            "cinematic": "🎬 Cinematic（电影感）",
            "minimal": "⬜ Minimal（无滤镜）",
        },
        "en": {
            "vintage": "🎞 Vintage",
            "neon": "🌿 Neon → Fresh Natural",
            "cinematic": "🎬 Cinematic",
            "minimal": "⬜ Minimal",
        },
    }
    return labels.get(lang, labels["zh"]).get(style_key, style_key)


# ─── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TikTok Dashboard",
    page_icon="🎬",
    layout="centered",
)

lang = st.selectbox(
    "Language / 语言",
    ["zh", "en"],
    format_func=lambda code: "中文" if code == "zh" else "English",
)

st.title(_t(lang, "🎬 TikTok 自动发布仪表盘", "🎬 TikTok Auto Publishing Dashboard"))
st.caption(_t(lang, "Seedance 视频生成 → 字幕剪辑 → 自动上传", "Seedance video generation -> subtitle editing -> auto upload"))

# ─── Input area ───────────────────────────────────────────────────────────────
st.subheader(_t(lang, "📝 输入参数", "📝 Input Parameters"))

with st.form("pipeline_form"):
    prompt = st.text_area(
        _t(lang, "视频 Prompt", "Video Prompt"),
        placeholder=_t(
            lang,
            "例：一只橘猫坐在咖啡馆窗边，慵懒地望向窗外，窗外是雨后的街道，电影感，暖色调。",
            "Example: An orange cat sits by a cafe window, gazing at the rainy street outside with a warm cinematic tone.",
        ),
        height=120,
    )

    # Audio selector
    audio_files = list_audio_files()
    if audio_files:
        selected_audio = st.selectbox(
            _t(lang, "背景音乐 / BGM", "Background Music / BGM"),
            audio_files,
            format_func=lambda p: Path(p).name,
        )
    else:
        st.warning(
            _t(
                lang,
                "未找到音频文件，请将 MP3/WAV 放入 video editor 的 raw_materials/song/ 目录。",
                "No audio files found. Please put MP3/WAV files into video editor's raw_materials/song/ directory.",
            )
        )
        selected_audio = None

    col1, col2 = st.columns(2)
    with col1:
        style = st.selectbox(
            _t(lang, "视频风格", "Video Style"),
            STYLE_OPTIONS,
            format_func=lambda s: _style_label(s, lang),
        )
        duration = st.slider(
            _t(lang, "视频时长（秒）", "Duration (seconds)"),
            min_value=5,
            max_value=15,
            value=5,
            step=1,
        )

    with col2:
        account = st.selectbox(_t(lang, "TikTok 账号", "TikTok Account"), _get_tiktok_accounts())
        resolution = st.selectbox(_t(lang, "分辨率", "Resolution"), ["480p", "720p", "1080p"], index=0)
        ratio = st.selectbox(_t(lang, "画面比例", "Aspect Ratio"), ["9:16", "16:9", "1:1"], index=0)

    submitted = st.form_submit_button(
        _t(lang, "🚀 开始生成并发布", "🚀 Start Generate & Publish"),
        use_container_width=True,
        type="primary",
    )

# ─── Progress area ────────────────────────────────────────────────────────────

if submitted:
    if not prompt.strip():
        st.error(_t(lang, "请输入视频 Prompt 后再提交。", "Please enter a video prompt before submitting."))
        st.stop()

    st.divider()
    st.subheader(_t(lang, "⚙️ 执行进度", "⚙️ Execution Progress"))

    # One placeholder per step, rendered before the pipeline starts
    step_states: dict[str, str] = {s: "pending" for s in STEPS}
    placeholders = {s: st.empty() for s in STEPS}

    def _render():
        icons = {"pending": "⬜", "running": "⏳", "done": "✅", "error": "❌"}
        labels = STEP_LABELS_EN if lang == "en" else STEP_LABELS
        for step in STEPS:
            state = step_states[step]
            icon = icons[state]
            label = labels[step]
            if state == "running":
                placeholders[step].info(
                    f"{icon} **{label}** {_t(lang, '— 进行中…', '- Running...')}"
                )
            elif state == "done":
                placeholders[step].success(f"{icon} {label}")
            elif state == "error":
                placeholders[step].error(
                    f"{icon} **{label}** {_t(lang, '— 失败', '- Failed')}"
                )
            else:
                placeholders[step].markdown(f"{icon} {label}")

    _render()

    final_status = "success"
    error_detail: str | None = None

    for step, status, detail in run_pipeline(
        prompt=prompt.strip(),
        style=style,
        tiktok_account=account,
        audio_path=selected_audio,
        resolution=resolution,
        ratio=ratio,
        duration=duration,
    ):
        step_states[step] = status
        _render()
        if status == "error":
            final_status = "failed"
            error_detail = detail
            # Mark remaining steps as pending (already pending, no change needed)
            break

    st.divider()
    if final_status == "success":
        st.success(_t(lang, "🎉 全部完成！视频已成功上传到 TikTok。", "🎉 All done! The video was uploaded to TikTok successfully."))
    else:
        labels = STEP_LABELS_EN if lang == "en" else STEP_LABELS
        st.error(
            _t(
                lang,
                f"Pipeline 在步骤 **{labels.get(step, step)}** 失败：{error_detail}",
                f"Pipeline failed at step **{labels.get(step, step)}**: {error_detail}",
            )
        )

    _append_history(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": prompt.strip()[:80],
            "style": style,
            "account": account,
            "status": _t(
                lang,
                "✅ 成功" if final_status == "success" else "❌ 失败",
                "✅ Success" if final_status == "success" else "❌ Failed",
            ),
        }
    )

# ─── History area ─────────────────────────────────────────────────────────────
st.divider()
st.subheader(_t(lang, "📋 历史记录", "📋 History"))

history = _load_history()
if history:
    import pandas as pd

    df = pd.DataFrame(history)
    expected_columns = ["time", "prompt", "style", "account", "status"]
    if all(col in df.columns for col in expected_columns):
        df = df[expected_columns]
    df.columns = _t(
        lang,
        ["时间", "Prompt", "风格", "账号", "状态"],
        ["Time", "Prompt", "Style", "Account", "Status"],
    )
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info(_t(lang, "暂无历史记录，完成第一次任务后将在此显示。", "No history yet. It will appear here after your first run."))
