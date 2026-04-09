"""TikTok 自动发布仪表盘 — Streamlit UI v2

模块状态：
  ✅ 模块 4  断点续传系统 (job_state.py + pipeline.py)
  ✅ 模块 5  执行面板（本文件）
  ✅ 模块 1  BGM 管理器
  ✅ 模块 2  任务创建面板
  ⬜ 模块 3  AI Prompt 扩展
  ✅ 模块 6  字幕生成
  ⬜ 模块 7  上传调度
  ✅ 模块 8  历史记录
  ✅ 模块 9  账号管理
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv, set_key

from job_state import (
    CLIP_DONE,
    CLIP_FAILED,
    CLIP_PENDING,
    CLIP_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_MERGING,
    STATUS_PENDING_REVIEW,
    STATUS_PENDING_SRT,
    STATUS_SRT_REVIEW,
    STATUS_UPLOADING,
    JobState,
    get_incomplete_jobs,
)
from modules.bgm_manager import (
    analyze_bgm,
    calc_clip_count,
    delete_bgm,
    format_duration,
    list_bgm_files,
    save_uploaded_bgm,
)
from pipeline import STEPS, STYLE_MAP, burn_subtitles, list_audio_files, run_job_clips, run_job_merge, run_job_srt, run_job_upload, run_pipeline

load_dotenv()

# ─── Constants ────────────────────────────────────────────────────────────────

STYLE_OPTIONS = list(STYLE_MAP.keys())

_STEP_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "generate_video": "生成视频 (Seedance API)",
        "download_video": "下载视频到本地",
        "generate_srt":   "生成字幕 (Whisper)",
        "edit_video":     "剪辑合成 (FFmpeg)",
        "upload_tiktok":  "上传 TikTok",
    },
    "en": {
        "generate_video": "Generate video (Seedance API)",
        "download_video": "Download video locally",
        "generate_srt":   "Generate subtitles (Whisper)",
        "edit_video":     "Edit & compose (FFmpeg)",
        "upload_tiktok":  "Upload to TikTok",
    },
}

CLIP_ICONS = {
    CLIP_PENDING: "⬜",
    CLIP_RUNNING: "⏳",
    CLIP_DONE:    "✅",
    CLIP_FAILED:  "❌",
}

_OVERALL_STATUS_ZH = {
    "creating":       "创建中",
    "generating":     "生成中",
    "pending_review": "待确认",
    "merging":        "合并中",
    "pending_srt":    "字幕生成中",
    "srt_review":     "字幕待确认",
    "uploading":      "上传中",
    "scheduled":      "已计划",
    "completed":      "已完成",
    "failed":         "已失败",
}


# ─── i18n ─────────────────────────────────────────────────────────────────────

def _t(zh: str, en: str) -> str:
    return en if st.session_state.get("lang") == "en" else zh


# ─── Misc helpers ─────────────────────────────────────────────────────────────

def _get_tiktok_accounts() -> list[str]:
    accounts = [
        k[len("TIKTOK_COOKIES_"):].lower()
        for k, v in os.environ.items()
        if k.startswith("TIKTOK_COOKIES_") and v
    ]
    return accounts or ["default"]


def _style_label(key: str) -> str:
    _maps = {
        "zh": {
            "vintage":   "🎞 Vintage（复古）",
            "neon":      "🌿 Neon → 清新自然",
            "cinematic": "🎬 Cinematic（电影感）",
            "minimal":   "⬜ Minimal（无滤镜）",
        },
        "en": {
            "vintage":   "🎞 Vintage",
            "neon":      "🌿 Neon → Fresh Natural",
            "cinematic": "🎬 Cinematic",
            "minimal":   "⬜ Minimal",
        },
    }
    lang = st.session_state.get("lang", "zh")
    return _maps.get(lang, _maps["zh"]).get(key, key)


# ─── Background thread management ────────────────────────────────────────────
#
# Thread objects live in st.session_state (per browser session).
# The thread writes to job state JSON on disk — the UI reads fresh from disk
# on each rerun, so there is no shared mutable state between thread and UI.
# Write operations in button handlers reload fresh from disk to minimise
# the race window between the generation thread and the UI.

def _thread_key(job_id: str) -> str:
    return f"_thread_{job_id}"


def _stop_key(job_id: str) -> str:
    return f"_stop_{job_id}"


def _is_running(job_id: str) -> bool:
    t = st.session_state.get(_thread_key(job_id))
    return t is not None and t.is_alive()


def _start_thread(job_id: str) -> None:
    """Start clip-generation thread for job_id. No-op if already running."""
    if _is_running(job_id):
        return

    stop_flag: list[bool] = st.session_state.get(_stop_key(job_id), [False])
    stop_flag[0] = False
    st.session_state[_stop_key(job_id)] = stop_flag

    def _worker() -> None:
        try:
            job = JobState.load(job_id)
            for _ in run_job_clips(job, stop_flag):
                pass  # state persisted inside run_job_clips after every clip
        except Exception:
            # Mark job failed if something unexpected blows up the thread
            try:
                j = JobState.load(job_id)
                if j.overall_status not in (STATUS_COMPLETED, STATUS_FAILED):
                    j.overall_status = STATUS_FAILED
                    j.save()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True, name=f"gen-{job_id[:8]}")
    t.start()
    st.session_state[_thread_key(job_id)] = t


def _request_stop(job_id: str) -> None:
    """Signal the generation thread to stop after the current clip."""
    flag = st.session_state.get(_stop_key(job_id), [False])
    flag[0] = True
    st.session_state[_stop_key(job_id)] = flag


# ─── BGM Manager ─────────────────────────────────────────────────────────────

def _render_bgm_manager() -> None:
    """Module 1: BGM manager — scan, upload, analyse, compute clip count."""

    st.subheader(_t("🎵 模块 1 — BGM 管理器", "🎵 Module 1 — BGM Manager"))

    # ── Upload new BGM ────────────────────────────────────────────────────────
    with st.expander(_t("⬆️ 上传新 BGM 文件", "⬆️ Upload new BGM file"), expanded=False):
        up = st.file_uploader(
            _t("支持 mp3 / wav / m4a / flac / aac / ogg", "Supports mp3 / wav / m4a / flac / aac / ogg"),
            type=["mp3", "wav", "m4a", "flac", "aac", "ogg"],
            key="bgm_upload",
        )
        if up is not None:
            dest = save_uploaded_bgm(up)
            st.success(_t(f"已保存：{dest.name}", f"Saved: {dest.name}"))
            st.rerun()

    # ── File list ─────────────────────────────────────────────────────────────
    bgm_files = list_bgm_files()

    if not bgm_files:
        st.info(_t(
            "assets/bgm/ 目录下暂无音频文件。请上传一个 BGM 文件开始。",
            "No audio files in assets/bgm/ yet. Upload a BGM file to get started.",
        ))
        return

    selected_path: str | None = st.session_state.get("bgm_selected_path")

    for p in bgm_files:
        is_selected = str(p) == selected_path
        _confirm_key = f"bgm_confirm_del_{p.name}"
        with st.container(border=True):
            col_info, col_preview, col_sel, col_del = st.columns([3, 4, 2, 1])

            with col_info:
                label = f"{'✅ ' if is_selected else ''}{p.name}"
                st.markdown(f"**{label}**")
                size_kb = p.stat().st_size / 1024
                st.caption(f"{size_kb:.0f} KB")

            with col_preview:
                st.audio(str(p))

            with col_sel:
                if is_selected:
                    if st.button(
                        _t("✖ 取消选择", "✖ Deselect"),
                        key=f"bgm_desel_{p.name}",
                        use_container_width=True,
                    ):
                        _prev = st.session_state.pop("bgm_selected_path", None)
                        if _prev:
                            st.session_state.pop(f"bgm_analysis_{_prev}", None)
                        st.session_state.pop("bgm_beats_per_cut", None)
                        st.session_state.pop("job_bgm_path", None)
                        st.rerun()
                else:
                    if st.button(
                        _t("🎵 选择", "🎵 Select"),
                        key=f"bgm_sel_{p.name}",
                        use_container_width=True,
                        type="primary",
                    ):
                        _old = st.session_state.get("bgm_selected_path")
                        if _old:
                            st.session_state.pop(f"bgm_analysis_{_old}", None)
                        st.session_state["bgm_selected_path"] = str(p)
                        st.session_state["job_bgm_path"] = str(p)
                        st.rerun()

            with col_del:
                if st.session_state.get(_confirm_key):
                    # Second click — actually delete
                    if st.button(
                        "✅",
                        key=f"bgm_del_confirm_{p.name}",
                        use_container_width=True,
                        help=_t("确认删除", "Confirm delete"),
                    ):
                        if is_selected:
                            st.session_state.pop("bgm_selected_path", None)
                            st.session_state.pop(f"bgm_analysis_{str(p)}", None)
                        st.session_state.pop(_confirm_key, None)
                        delete_bgm(p)
                        st.rerun()
                else:
                    # First click — ask for confirmation
                    if st.button(
                        "🗑️",
                        key=f"bgm_del_{p.name}",
                        use_container_width=True,
                        help=_t("删除此文件（再次点击确认）", "Delete file (click again to confirm)"),
                    ):
                        st.session_state[_confirm_key] = True
                        st.rerun()

    # ── Analysis panel ────────────────────────────────────────────────────────
    if not selected_path:
        return

    sel_path_obj = Path(selected_path)
    if not sel_path_obj.is_file():
        st.warning(_t("所选文件已不存在，请重新选择。", "Selected file no longer exists. Please re-select."))
        st.session_state.pop("bgm_selected_path", None)
        return

    # Cache analysis in session state to avoid re-running librosa on every rerun
    analysis_key = f"bgm_analysis_{selected_path}"
    if analysis_key not in st.session_state:
        with st.spinner(_t("分析 BPM 中…", "Analysing BPM…")):
            st.session_state[analysis_key] = analyze_bgm(sel_path_obj)
    result = st.session_state[analysis_key]

    st.divider()
    _hdr_col, _rerun_col = st.columns([6, 1])
    with _hdr_col:
        st.markdown(_t(
            f"**分析结果：{sel_path_obj.name}**",
            f"**Analysis: {sel_path_obj.name}**",
        ))
    with _rerun_col:
        if st.button(_t("🔄 重新分析", "🔄 Re-analyse"), key="bgm_reanalyse", use_container_width=True):
            st.session_state.pop(analysis_key, None)
            st.rerun()

    if result["error"] and result["duration_s"] is None:
        st.error(_t(f"分析失败：{result['error']}", f"Analysis failed: {result['error']}"))
        if result.get("traceback"):
            with st.expander(_t("查看详细错误", "Show traceback")):
                st.code(result["traceback"])
        st.caption(_t(
            "请确认 ffmpeg 已安装（`ffmpeg -version`），或安装 librosa：`pip install librosa`",
            "Make sure ffmpeg is installed (`ffmpeg -version`), or install librosa: `pip install librosa`",
        ))
        return

    col_dur, col_bpm, col_cut, col_clips = st.columns(4)

    duration_s: float = result["duration_s"] or 0.0
    bpm: float | None = result["bpm"]

    with col_dur:
        st.metric(_t("时长", "Duration"), format_duration(duration_s) if duration_s else "—")
    with col_bpm:
        st.metric("BPM", f"{bpm:.1f}" if bpm else "—")

    if result["error"]:
        st.caption(_t(f"⚠️ {result['error']}", f"⚠️ {result['error']}"))
        if result.get("traceback"):
            with st.expander(_t("查看详细错误", "Show traceback")):
                st.code(result["traceback"])

    # beats_per_cut slider — stored in session state so it persists
    with col_cut:
        beats_per_cut = st.number_input(
            _t("每几拍切一次", "Beats per cut"),
            min_value=1,
            max_value=16,
            value=st.session_state.get("bgm_beats_per_cut", 2),
            step=1,
            key="bgm_beats_per_cut",
            help=_t(
                "每隔几拍切换一个片段。BPM 越高、值越小，片段越多。",
                "How many beats before switching clip. Higher BPM + smaller value = more clips.",
            ),
        )

    with col_clips:
        if bpm and duration_s:
            n_clips = calc_clip_count(duration_s, bpm, beats_per_cut)
            st.metric(_t("需生成片段数", "Clips needed"), n_clips)
            st.session_state["bgm_suggested_clips"] = n_clips
        else:
            st.metric(_t("需生成片段数", "Clips needed"), "—")
            st.session_state.pop("bgm_suggested_clips", None)

    if bpm and duration_s:
        beat_s = 60.0 / bpm
        cut_s  = beat_s * beats_per_cut
        st.caption(_t(
            f"每段约 {cut_s:.2f} 秒（{beats_per_cut} 拍 × {beat_s:.3f} 秒/拍）",
            f"Each cut ≈ {cut_s:.2f} s  ({beats_per_cut} beats × {beat_s:.3f} s/beat)",
        ))


# ─── Job creation panel (Module 2) ───────────────────────────────────────────

def _sync_prompt_inputs() -> None:
    """Flush text_area widget values back into session_state["job_prompts"]."""
    lst = st.session_state.get("job_prompts", [])
    for i in range(len(lst)):
        v = st.session_state.get(f"prompt_input_{i}")
        if v is not None:
            lst[i] = v
    st.session_state["job_prompts"] = lst


def _clear_prompt_widget_keys() -> None:
    """Remove all prompt_input_* keys so text_areas re-initialize from the list."""
    for k in list(st.session_state.keys()):
        if k.startswith("prompt_input_"):
            del st.session_state[k]


def _render_job_creation_panel() -> None:
    """Module 2: Dynamic task creation — N prompt inputs, multi-account, scheduling."""

    # ── Sync prompt list length with BGM suggestion ───────────────────────
    suggested = st.session_state.get("bgm_suggested_clips")
    if "job_prompts" not in st.session_state:
        st.session_state["job_prompts"] = [""] * (suggested or 3)

    # Grow list if BGM suggests more clips than currently present
    lst: list[str] = st.session_state["job_prompts"]
    _synced_key = "_job_prompts_synced_n"
    if suggested and st.session_state.get(_synced_key) != suggested:
        if len(lst) < suggested:
            lst.extend([""] * (suggested - len(lst)))
            st.session_state["job_prompts"] = lst
        st.session_state[_synced_key] = suggested

    # ── Basic info row ────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        st.text_input(_t("歌曲名称", "Song Title"), key="job_song")
    with c2:
        st.text_input(_t("艺术家", "Artist"), key="job_artist")
    with c3:
        st.selectbox(_t("视频风格", "Style"), STYLE_OPTIONS, format_func=_style_label, key="job_style")

    st.divider()

    # ── Prompt editor ─────────────────────────────────────────────────────
    hdr, add_col = st.columns([5, 1])
    with hdr:
        n = len(st.session_state["job_prompts"])
        st.markdown(_t(f"**片段 Prompts — 共 {n} 条**", f"**Clip Prompts — {n} clips**"))
        if suggested:
            st.caption(_t(
                f"BGM 管理器建议 {suggested} 个片段，可手动增减。",
                f"BGM Manager suggests {suggested} clips — adjust freely.",
            ))
    with add_col:
        if st.button(_t("➕ 添加", "+ Add"), key="add_prompt", use_container_width=True):
            _sync_prompt_inputs()
            st.session_state["job_prompts"].append("")
            st.rerun()

    for i, prompt_val in enumerate(st.session_state["job_prompts"]):
        col_n, col_txt, col_up, col_dn, col_rm = st.columns([0.3, 7, 0.4, 0.4, 0.4])
        with col_n:
            st.markdown(f"<br>**{i + 1}**", unsafe_allow_html=True)
        with col_txt:
            st.text_area(
                f"clip_{i + 1}",
                value=prompt_val,
                key=f"prompt_input_{i}",
                height=68,
                label_visibility="collapsed",
                placeholder=_t(
                    f"片段 {i + 1} 的画面描述（建议用英文）",
                    f"Scene description for clip {i + 1}",
                ),
            )
        with col_up:
            st.markdown("<br>", unsafe_allow_html=True)
            if i > 0:
                if st.button("↑", key=f"up_{i}", use_container_width=True):
                    _sync_prompt_inputs()
                    _l = st.session_state["job_prompts"]
                    _l[i - 1], _l[i] = _l[i], _l[i - 1]
                    _clear_prompt_widget_keys()
                    st.rerun()
        with col_dn:
            st.markdown("<br>", unsafe_allow_html=True)
            if i < len(st.session_state["job_prompts"]) - 1:
                if st.button("↓", key=f"dn_{i}", use_container_width=True):
                    _sync_prompt_inputs()
                    _l = st.session_state["job_prompts"]
                    _l[i], _l[i + 1] = _l[i + 1], _l[i]
                    _clear_prompt_widget_keys()
                    st.rerun()
        with col_rm:
            st.markdown("<br>", unsafe_allow_html=True)
            if len(st.session_state["job_prompts"]) > 1:
                if st.button("✕", key=f"rm_{i}", use_container_width=True):
                    _sync_prompt_inputs()
                    st.session_state["job_prompts"].pop(i)
                    _clear_prompt_widget_keys()
                    st.rerun()

    st.divider()

    # ── Video settings ────────────────────────────────────────────────────
    col_a, col_b, col_c, col_d = st.columns(4)
    with col_a:
        st.selectbox(_t("分辨率", "Resolution"), ["480p", "720p", "1080p"], key="job_resolution")
    with col_b:
        st.selectbox(_t("画面比例", "Ratio"), ["9:16", "16:9", "1:1"], key="job_ratio")
    with col_c:
        st.slider(_t("片段时长（秒）", "Clip duration (s)"), 5, 15, 5, key="job_duration")
    with col_d:
        st.selectbox(
            _t("字幕模式", "Subtitles"),
            ["whisper", "none"],
            format_func=lambda v: _t("Whisper 自动识别", "Whisper auto") if v == "whisper" else _t("不加字幕", "No subtitles"),
            key="job_subtitle_mode",
        )

    col_lang, col_model = st.columns(2)
    with col_lang:
        st.selectbox(
            _t("歌词语言", "Lyrics language"),
            ["auto", "ko", "ja", "en", "zh"],
            format_func=lambda v: {
                "auto": _t("自动检测", "Auto-detect"),
                "ko": "한국어 (Korean)",
                "ja": "日本語 (Japanese)",
                "en": "English",
                "zh": "中文 (Chinese)",
            }.get(v, v),
            key="job_whisper_language",
        )
    with col_model:
        st.selectbox(
            _t("Whisper 模型", "Whisper model"),
            ["medium", "small", "large", "base"],
            format_func=lambda v: {"medium": "medium (推荐)", "small": "small (快)", "large": "large (最准)", "base": "base (极快)"}.get(v, v),
            key="job_whisper_model",
        )

    # ── BGM ───────────────────────────────────────────────────────────────
    _mgr_bgm = st.session_state.get("bgm_selected_path")
    _all_bgm: list[str | None] = [None]
    for _bp in list_bgm_files():
        if str(_bp) not in _all_bgm:
            _all_bgm.append(str(_bp))
    for _bp in list_audio_files():
        if _bp not in _all_bgm:
            _all_bgm.append(_bp)
    # Sync session state with BGM Manager selection (only when not yet set or stale)
    if _mgr_bgm and st.session_state.get("job_bgm_path") != _mgr_bgm:
        st.session_state["job_bgm_path"] = _mgr_bgm
    elif not _mgr_bgm and "job_bgm_path" not in st.session_state:
        st.session_state["job_bgm_path"] = None

    st.selectbox(
        _t("背景音乐（可选）", "BGM (optional)"),
        _all_bgm,
        format_func=lambda p: _t("无", "None") if p is None else Path(p).name,
        key="job_bgm_path",
    )

    st.divider()

    # ── Accounts + publish strategy ───────────────────────────────────────
    _all_accs = _get_tiktok_accounts()
    col_acc, col_pub = st.columns([1, 1])
    with col_acc:
        st.multiselect(
            _t("目标 TikTok 账号（可多选）", "Target TikTok accounts"),
            _all_accs,
            default=_all_accs[:1],
            key="job_accounts",
        )
    with col_pub:
        st.radio(
            _t("发布策略", "Publish strategy"),
            ["immediate", "scheduled"],
            format_func=lambda v: _t("立即上传", "Upload immediately") if v == "immediate" else _t("定时发布", "Schedule"),
            horizontal=True,
            key="job_publish_mode",
        )

    scheduled_at: str | None = None
    if st.session_state.get("job_publish_mode") == "scheduled":
        from datetime import time as _dtime, datetime as _dt
        col_dt, col_tm = st.columns(2)
        with col_dt:
            pub_date = st.date_input(_t("发布日期", "Publish date"), key="job_pub_date")
        with col_tm:
            pub_time = st.time_input(_t("发布时间", "Publish time"), value=_dtime(12, 0), key="job_pub_time")
        scheduled_at = _dt.combine(pub_date, pub_time).isoformat(timespec="minutes")

    # ── TikTok in-app BGM ─────────────────────────────────────────────────
    with st.expander(_t("🎵 TikTok 内置 BGM（可选）", "🎵 TikTok in-app BGM (optional)"), expanded=False):
        st.caption(_t(
            "上传时 Playwright 会在 TikTok 发布页自动搜索并添加此 BGM。",
            "Playwright will search and add this BGM on the TikTok publish page during upload.",
        ))
        col_ts, col_ta = st.columns(2)
        with col_ts:
            st.text_input(_t("TikTok 曲名", "TikTok BGM song name"), key="job_tiktok_bgm_song")
        with col_ta:
            st.text_input(_t("TikTok 曲作者", "TikTok BGM artist"), key="job_tiktok_bgm_artist")

    st.divider()

    # ── Create button ─────────────────────────────────────────────────────
    if st.button(
        _t("🚀 创建任务并开始生成", "🚀 Create Job & Start"),
        type="primary",
        use_container_width=True,
        key="job_create_btn",
    ):
        _sync_prompt_inputs()
        final_prompts = [p.strip() for p in st.session_state["job_prompts"] if p.strip()]
        accounts      = st.session_state.get("job_accounts") or []

        if not final_prompts:
            st.error(_t("请至少填写一条 Prompt。", "Please enter at least one prompt."))
        elif not accounts:
            st.error(_t("请至少选择一个 TikTok 账号。", "Please select at least one TikTok account."))
        else:
            _new_job = JobState.create(
                prompts        = final_prompts,
                style          = st.session_state.get("job_style", "minimal"),
                tiktok_accounts= accounts,
                bgm_path       = st.session_state.get("job_bgm_path"),
                resolution     = st.session_state.get("job_resolution", "480p"),
                ratio          = st.session_state.get("job_ratio", "9:16"),
                duration       = int(st.session_state.get("job_duration", 5)),
                beats_per_cut  = int(st.session_state.get("bgm_beats_per_cut", 2)),
                bpm            = (st.session_state.get(f"bgm_analysis_{st.session_state.get('job_bgm_path', '')}", {}) or {}).get("bpm"),
                subtitle_mode     = st.session_state.get("job_subtitle_mode", "whisper"),
                whisper_model     = st.session_state.get("job_whisper_model", "medium"),
                whisper_language  = (lambda v: None if v == "auto" else v)(st.session_state.get("job_whisper_language", "auto")),
                scheduled_at   = scheduled_at,
                tiktok_bgm_song  = st.session_state.get("job_tiktok_bgm_song", ""),
                tiktok_bgm_artist= st.session_state.get("job_tiktok_bgm_artist", ""),
                song           = st.session_state.get("job_song", ""),
                artist         = st.session_state.get("job_artist", ""),
            )
            st.session_state["active_job_id"] = _new_job.job_id
            st.session_state["page"] = "execution"
            _start_thread(_new_job.job_id)
            st.rerun()


# ─── Merge / SRT thread management ──────────────────────────────────────────

def _post_thread_key(job_id: str) -> str:
    return f"_post_thread_{job_id}"


def _is_post_running(job_id: str) -> bool:
    t = st.session_state.get(_post_thread_key(job_id))
    return t is not None and t.is_alive()


def _start_post_thread(job_id: str, run_srt: bool) -> None:
    """Start merge (+ optional SRT) thread. No-op if already running."""
    if _is_post_running(job_id):
        return

    def _worker() -> None:
        try:
            job = JobState.load(job_id)
            for _ in run_job_merge(job):
                pass
            if run_srt:
                job = JobState.load(job_id)
                if job.stage_is_done("merge"):
                    for _ in run_job_srt(job):
                        pass
        except Exception:
            try:
                j = JobState.load(job_id)
                if j.overall_status not in (STATUS_COMPLETED, STATUS_FAILED):
                    j.overall_status = STATUS_FAILED
                    j.save()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True, name=f"post-{job_id[:8]}")
    t.start()
    st.session_state[_post_thread_key(job_id)] = t


def _burn_thread_key(job_id: str) -> str:
    return f"_burn_thread_{job_id}"


def _burn_error_key(job_id: str) -> str:
    return f"_burn_error_{job_id}"


def _is_burn_running(job_id: str) -> bool:
    t = st.session_state.get(_burn_thread_key(job_id))
    return t is not None and t.is_alive()


def _start_burn_thread(job_id: str, merge_output: str, srt_path: str) -> None:
    """Burn subtitles into final.mp4 in background. No-op if already running."""
    if _is_burn_running(job_id):
        return
    st.session_state.pop(_burn_error_key(job_id), None)
    final_path = str(Path(merge_output).parent / "final.mp4")

    def _worker() -> None:
        try:
            burn_subtitles(merge_output, srt_path, final_path)
        except Exception as exc:
            st.session_state[_burn_error_key(job_id)] = str(exc)

    t = threading.Thread(target=_worker, daemon=True, name=f"burn-{job_id[:8]}")
    t.start()
    st.session_state[_burn_thread_key(job_id)] = t


def _upload_thread_key(job_id: str) -> str:
    return f"_upload_thread_{job_id}"


def _is_upload_running(job_id: str) -> bool:
    t = st.session_state.get(_upload_thread_key(job_id))
    return t is not None and t.is_alive()


def _start_upload_thread(job_id: str, description: str) -> None:
    """Start upload thread. No-op if already running."""
    if _is_upload_running(job_id):
        return

    def _worker() -> None:
        try:
            job = JobState.load(job_id)
            for _ in run_job_upload(job, description):
                pass
        except Exception:
            try:
                j = JobState.load(job_id)
                if j.overall_status not in (STATUS_COMPLETED, STATUS_FAILED):
                    j.overall_status = STATUS_FAILED
                    j.save()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True, name=f"upload-{job_id[:8]}")
    t.start()
    st.session_state[_upload_thread_key(job_id)] = t


# ─── Module 6 — Merge & subtitle panel ───────────────────────────────────────

def _render_merge_srt_panel(job: JobState) -> None:
    """Render merge progress, SRT editor, and confirmation UI."""
    job_id    = job.job_id
    status    = job.overall_status
    post_busy = _is_post_running(job_id)
    subtitle_mode = job.params.get("subtitle_mode", "whisper")

    merge_stage = job.stages.get("merge", {})
    srt_stage   = job.stages.get("srt",   {})

    # ── All clips confirmed → start button ───────────────────────────────
    if job.all_clips_confirmed() and status == "pending_review":
        st.success(_t("🎉 所有片段已确认，可以开始合并！", "🎉 All clips confirmed — ready to merge!"))
        if st.button(
            _t("🎬 开始合并" + ("与字幕生成" if subtitle_mode == "whisper" else ""),
               "🎬 Start Merge" + (" & Subtitles" if subtitle_mode == "whisper" else "")),
            type="primary",
            use_container_width=True,
            key="start_merge_btn",
        ):
            _start_post_thread(job_id, run_srt=(subtitle_mode == "whisper"))
            st.rerun()
        return

    # ── Merge in progress ────────────────────────────────────────────────
    if merge_stage.get("status") == "running" or (post_busy and merge_stage.get("status") != "done"):
        st.info(_t("⏳ 正在合并片段（FFmpeg）…", "⏳ Merging clips (FFmpeg)…"))
        st.caption(_t("合并完成后将自动继续。", "Will proceed automatically after merge."))
        return

    # ── Merge failed ─────────────────────────────────────────────────────
    if merge_stage.get("status") == "failed":
        st.error(_t(
            f"❌ 合并失败：{merge_stage.get('error', '')}",
            f"❌ Merge failed: {merge_stage.get('error', '')}",
        ))
        if st.button(_t("🔄 重新合并", "🔄 Retry merge"), key="retry_merge_btn"):
            job.stages["merge"]["status"] = "pending"
            job.overall_status = "pending_review"
            job.save()
            _start_post_thread(job_id, run_srt=(subtitle_mode == "whisper"))
            st.rerun()
        return

    # ── Merge done ───────────────────────────────────────────────────────
    if merge_stage.get("status") == "done":
        output_path = merge_stage.get("output_path", "")
        st.success(_t("✅ 合并完成", "✅ Merge complete"))
        if output_path and Path(output_path).exists():
            st.video(output_path)

    # ── Upload panel (SRT confirmed or no-subtitle path) ─────────────────
    if status == STATUS_UPLOADING:
        upload_stage = job.stages.get("upload", {})
        upload_busy  = _is_upload_running(job_id)
        accounts     = job.params.get("tiktok_accounts", [])
        upload_status = upload_stage.get("status", "pending")

        st.divider()
        st.subheader(_t("🚀 上传到 TikTok", "🚀 Upload to TikTok"))
        st.caption(_t(f"目标账号：{', '.join(accounts)}", f"Target accounts: {', '.join(accounts)}"))

        if upload_status == "running" or upload_busy:
            st.info(_t("⏳ 正在上传…", "⏳ Uploading…"))
            for acc, res in (upload_stage.get("results") or {}).items():
                icon = "✅" if res == "success" else "❌"
                st.write(f"{icon} `{acc}`: {res}")
            time.sleep(2)
            st.rerun()
            return

        if upload_status == "done":
            results = upload_stage.get("results", {})
            st.success(_t("🎉 上传完成！", "🎉 Upload complete!"))
            for acc, res in results.items():
                icon = "✅" if res == "success" else "❌"
                st.write(f"{icon} `{acc}`: {res}")
            return

        if upload_status == "failed":
            st.error(_t(
                f"❌ 上传失败：{upload_stage.get('error', '')}",
                f"❌ Upload failed: {upload_stage.get('error', '')}",
            ))

        # Pending (or failed retry) — show upload form
        burn_running = _is_burn_running(job_id)
        burn_error   = st.session_state.get(_burn_error_key(job_id))
        final_path   = str(job.job_dir / "final.mp4")
        final_ready  = Path(final_path).is_file()

        # ── Burn progress ────────────────────────────────────────────────
        if burn_running:
            st.info(_t("⏳ 正在烧录字幕，请稍候…", "⏳ Burning subtitles into video…"))
            time.sleep(1.5)
            st.rerun()
            return

        if burn_error:
            st.error(_t(f"❌ 字幕烧录失败：{burn_error}", f"❌ Subtitle burn failed: {burn_error}"))
            merge_out = job.stages.get("merge", {}).get("output_path", "")
            srt_p = srt_stage.get("srt_path", "")
            if st.button(_t("🔄 重新烧录", "🔄 Retry burn"), key="retry_burn_btn"):
                if merge_out and srt_p:
                    _start_burn_thread(job_id, merge_out, srt_p)
                    st.rerun()

        # ── Final video preview ──────────────────────────────────────────
        if final_ready:
            st.subheader(_t("🎬 带字幕预览", "🎬 Preview with subtitles"))
            st.video(final_path)
            if st.button(_t("↩️ 返回编辑字幕", "↩️ Back to edit subtitles"), key="back_to_srt_btn"):
                _j = JobState.load(job_id)
                _j.overall_status = STATUS_SRT_REVIEW
                _j.save()
                st.rerun()
        elif not burn_error:
            # Burn thread not started yet (e.g. page refreshed) — restart it
            merge_out = job.stages.get("merge", {}).get("output_path", "")
            srt_p = srt_stage.get("srt_path", "")
            if merge_out and srt_p:
                _start_burn_thread(job_id, merge_out, srt_p)
            st.info(_t("⏳ 正在烧录字幕，请稍候…", "⏳ Burning subtitles into video…"))
            time.sleep(1.5)
            st.rerun()
            return

        # ── Upload form (only shown after final.mp4 is ready) ───────────
        if not final_ready:
            return

        _desc_key = f"upload_desc_{job_id}"
        song   = job.song
        artist = job.artist
        default_desc = f"{song} - {artist}".strip(" -") if (song or artist) else ""
        description = st.text_area(
            _t("视频描述（TikTok caption）", "Video description (TikTok caption)"),
            value=st.session_state.get(_desc_key, default_desc),
            height=100,
            key=_desc_key,
        )
        scheduled_at = upload_stage.get("scheduled_at") or job.params.get("scheduled_at")
        if scheduled_at:
            st.info(_t(f"定时发布：{scheduled_at}", f"Scheduled: {scheduled_at}"))
        if st.button(
            _t("🚀 立即上传", "🚀 Upload now"),
            type="primary",
            use_container_width=True,
            key="start_upload_btn",
        ):
            _start_upload_thread(job_id, description)
            st.rerun()
        return

    # ── SRT in progress ──────────────────────────────────────────────────
    if subtitle_mode == "whisper":
        if srt_stage.get("status") == "running" or (post_busy and srt_stage.get("status") not in ("done", "failed")):
            st.info(_t("⏳ Whisper 识别字幕中…", "⏳ Whisper transcribing subtitles…"))
            st.caption(_t("识别完成后可在下方编辑字幕内容。", "You can edit the subtitles below once done."))
            time.sleep(2)
            st.rerun()
            return

        # ── SRT failed ───────────────────────────────────────────────────
        if srt_stage.get("status") == "failed":
            st.error(_t(
                f"❌ 字幕生成失败：{srt_stage.get('error', '')}",
                f"❌ Subtitle generation failed: {srt_stage.get('error', '')}",
            ))
            col_retry, col_skip = st.columns(2)
            with col_retry:
                if st.button(_t("🔄 重新生成字幕", "🔄 Retry subtitles"), key="retry_srt_btn", use_container_width=True):
                    job.stages["srt"]["status"] = "pending"
                    job.save()
                    _start_post_thread(job_id, run_srt=True)
                    st.rerun()
            with col_skip:
                if st.button(_t("跳过字幕，直接上传", "Skip subtitles, proceed to upload"), key="skip_srt_btn", use_container_width=True):
                    job.overall_status = STATUS_PENDING_REVIEW  # reuse status as "ready for upload"
                    job.save()
                    st.rerun()
            return

        # ── SRT review & edit ────────────────────────────────────────────
        if srt_stage.get("status") == "done":
            st.divider()
            st.subheader(_t("📝 字幕预览与编辑", "📝 Subtitle Preview & Edit"))
            _audio_used = srt_stage.get("audio_used", "")
            st.caption(_t(
                f"Whisper 识别来源：`{Path(_audio_used).name if _audio_used else '未知'}`  ·  可直接编辑后确认。",
                f"Whisper source: `{Path(_audio_used).name if _audio_used else 'unknown'}`  ·  Edit if needed, then confirm.",
            ))

            _srt_edit_key = f"srt_edit_{job_id}"
            if _srt_edit_key not in st.session_state:
                # Read from disk so we always get the latest Whisper output,
                # not the content cached in state.json from a previous run.
                _srt_file = srt_stage.get("srt_path")
                if _srt_file and Path(_srt_file).is_file():
                    st.session_state[_srt_edit_key] = Path(_srt_file).read_text(encoding="utf-8")
                else:
                    st.session_state[_srt_edit_key] = srt_stage.get("content", "")

            edited_srt = st.text_area(
                _t("字幕内容（SRT 格式）", "Subtitle content (SRT format)"),
                value=st.session_state[_srt_edit_key],
                height=300,
                key=f"srt_textarea_{job_id}",
            )

            col_confirm, col_reset, col_regen = st.columns([3, 1, 1])
            with col_reset:
                if st.button(_t("↩️ 还原原始字幕", "↩️ Reset to original"), key="srt_reset_btn", use_container_width=True):
                    st.session_state[_srt_edit_key] = srt_stage.get("content", "")
                    st.rerun()
            with col_regen:
                if st.button(_t("🔄 重新识别", "🔄 Re-run Whisper"), key="rerun_srt_btn", use_container_width=True):
                    _j = JobState.load(job_id)
                    _j.stages["srt"] = {"status": "pending", "srt_path": None, "content": None}
                    _j.overall_status = "pending_srt"
                    _j.save()
                    st.session_state.pop(_srt_edit_key, None)
                    st.session_state.pop(f"srt_textarea_{job_id}", None)
                    st.session_state.pop(_post_thread_key(job_id), None)  # clear stale thread ref
                    _start_post_thread(job_id, run_srt=True)
                    st.rerun()
            with col_confirm:
                if st.button(
                    _t("✅ 确认字幕，准备上传", "✅ Confirm subtitles & proceed to upload"),
                    type="primary",
                    use_container_width=True,
                    key="confirm_srt_btn",
                ):
                    # Save edited content back to job state
                    _j = JobState.load(job_id)
                    _j.stages["srt"]["content"] = edited_srt
                    # Write edited SRT to file
                    srt_path = srt_stage.get("srt_path")
                    if srt_path:
                        try:
                            Path(srt_path).write_text(edited_srt, encoding="utf-8")
                        except Exception:
                            pass
                    _j.overall_status = STATUS_UPLOADING
                    _j.save()
                    # Start burn thread so final.mp4 is ready before upload
                    _merge_out = _j.stages.get("merge", {}).get("output_path", "")
                    if srt_path and _merge_out:
                        _start_burn_thread(job_id, _merge_out, srt_path)
                    st.rerun()
            return

        # Whisper not yet started (merge just finished)
        if merge_stage.get("status") == "done" and not post_busy:
            if st.button(_t("🎙️ 开始字幕识别 (Whisper)", "🎙️ Start subtitle recognition (Whisper)"), key="start_srt_btn", use_container_width=True):
                _start_post_thread(job_id, run_srt=True)
                st.rerun()
        return

    # ── No subtitles — go straight to upload ─────────────────────────────
    if merge_stage.get("status") == "done" and subtitle_mode == "none":
        if st.button(
            _t("🚀 合并完成，准备上传", "🚀 Merge done — proceed to upload"),
            type="primary",
            use_container_width=True,
            key="proceed_upload_btn",
        ):
            _j = JobState.load(job_id)
            _j.overall_status = STATUS_UPLOADING
            _j.save()
            st.rerun()


# ─── Clip row renderer ────────────────────────────────────────────────────────

def _render_clip_row(clip: dict, job_id: str) -> None:
    """Render one clip's status card, preview, and action buttons."""
    idx       = clip["index"]
    status    = clip["status"]
    icon      = CLIP_ICONS.get(status, "❓")
    confirmed = clip.get("confirmed", False)
    running   = _is_running(job_id)

    with st.container(border=True):
        top_left, top_right = st.columns([5, 3])

        # ── Status + prompt ───────────────────────────────────────────────
        with top_left:
            header = f"{icon} **{_t('片段', 'Clip')} #{idx + 1}**"
            if confirmed:
                header += "  ✓"
            st.markdown(header)

            prompt_text = clip["prompt"]
            if len(prompt_text) > 120:
                prompt_text = prompt_text[:117] + "…"
            st.caption(prompt_text)

            if status == CLIP_RUNNING:
                st.markdown(f"_{_t('生成中，请稍候…', 'Generating, please wait…')}_")
            elif status == CLIP_FAILED:
                err = clip.get("error") or _t("未知错误", "Unknown error")
                st.error(err[:150])
            elif status == CLIP_DONE:
                label = "✅ " + _t("已完成", "Done")
                if confirmed:
                    label += "  ·  ✓ " + _t("已确认", "Confirmed")
                st.success(label)

        # ── Action buttons ────────────────────────────────────────────────
        with top_right:
            if status == CLIP_DONE:
                # Confirm button (only when not yet confirmed)
                if not confirmed:
                    if st.button(
                        _t("✅ 确认此片段", "✅ Confirm"),
                        key=f"confirm_{idx}_{job_id}",
                        use_container_width=True,
                        type="primary",
                    ):
                        # Reload fresh to avoid overwriting concurrent thread writes
                        _j = JobState.load(job_id)
                        _j.confirm_clip(idx)
                        st.rerun()

                if not running:
                    if st.button(
                        _t("🔄 原 Prompt 重新生成", "🔄 Regen (same prompt)"),
                        key=f"regen_same_{idx}_{job_id}",
                        use_container_width=True,
                    ):
                        _j = JobState.load(job_id)
                        _j.reset_clip(idx)
                        _start_thread(job_id)
                        st.rerun()

                    # Toggle inline prompt-edit form
                    _edit_key = f"_show_edit_{idx}_{job_id}"
                    edit_label = (
                        _t("✏️ 收起", "✏️ Collapse")
                        if st.session_state.get(_edit_key)
                        else _t("✏️ 修改 Prompt 重新生成", "✏️ Edit prompt & regen")
                    )
                    if st.button(edit_label, key=f"edit_toggle_{idx}_{job_id}", use_container_width=True):
                        st.session_state[_edit_key] = not st.session_state.get(_edit_key, False)
                        st.rerun()

            elif status in (CLIP_FAILED, CLIP_PENDING):
                if not running:
                    if st.button(
                        _t("🔄 重试", "🔄 Retry"),
                        key=f"retry_{idx}_{job_id}",
                        use_container_width=True,
                    ):
                        _j = JobState.load(job_id)
                        _j.reset_clip(idx)
                        _start_thread(job_id)
                        st.rerun()

        # ── Inline prompt-edit form (shown below when toggled) ────────────
        _edit_key = f"_show_edit_{idx}_{job_id}"
        if status == CLIP_DONE and not running and st.session_state.get(_edit_key, False):
            new_p = st.text_area(
                _t("修改后的 Prompt", "Updated Prompt"),
                value=clip["prompt"],
                key=f"new_prompt_{idx}_{job_id}",
                height=80,
            )
            if st.button(
                _t("🚀 用新 Prompt 重新生成", "🚀 Regen with new prompt"),
                key=f"do_regen_{idx}_{job_id}",
                type="primary",
            ):
                if new_p.strip():
                    _j = JobState.load(job_id)
                    _j.reset_clip(idx, new_prompt=new_p.strip())
                    st.session_state[_edit_key] = False
                    _start_thread(job_id)
                    st.rerun()
                else:
                    st.warning(_t("Prompt 不能为空。", "Prompt cannot be empty."))

        # ── Video preview ─────────────────────────────────────────────────
        if status == CLIP_DONE and clip.get("local_path"):
            vp = Path(clip["local_path"])
            if vp.exists():
                st.video(str(vp))
            else:
                st.caption(_t(f"⚠️ 本地文件不存在：{vp.name}", f"⚠️ File missing: {vp.name}"))


# ─── Execution panel ──────────────────────────────────────────────────────────

def _render_execution_panel(job: JobState) -> None:
    job_id  = job.job_id
    running = _is_running(job_id)
    counts  = job.clip_counts()
    total   = len(job.clips)
    n_done  = counts[CLIP_DONE]
    n_fail  = counts[CLIP_FAILED]

    # ── Header ────────────────────────────────────────────────────────────
    title = job.song or _t(f"任务 {job_id[:8]}", f"Job {job_id[:8]}")
    if job.artist:
        title += f"  —  {job.artist}"
    st.title(f"🎬  {title}")

    status_label = _OVERALL_STATUS_ZH.get(job.overall_status, job.overall_status)
    meta_parts = [
        f"`{job_id}`",
        _t(f"创建于 {job.created_at}", f"Created {job.created_at}"),
        _t(f"状态：**{status_label}**", f"Status: **{job.overall_status}**"),
    ]
    if running:
        meta_parts.append(_t("🔄 生成中", "🔄 Generating"))
    st.caption("  ·  ".join(meta_parts))

    # ── Progress bar ──────────────────────────────────────────────────────
    progress_frac = n_done / total if total > 0 else 0.0
    progress_text = _t(
        f"已完成 {n_done} / 共 {total} 个片段"
        + (f"，{n_fail} 个失败" if n_fail else ""),
        f"{n_done} / {total} clips done"
        + (f", {n_fail} failed" if n_fail else ""),
    )
    st.progress(progress_frac, text=progress_text)

    # ── Control strip ─────────────────────────────────────────────────────
    c_pause, c_cancel, c_home, _ = st.columns([1, 1, 1, 4])

    with c_pause:
        if running:
            if st.button(_t("⏸ 暂停", "⏸ Pause"), use_container_width=True):
                _request_stop(job_id)
                st.toast(_t("已发送暂停信号，当前片段完成后停止。", "Pause signal sent. Will stop after current clip."))
        else:
            # Show Resume button only when there are clips left to generate
            can_resume = (
                job.overall_status in (STATUS_GENERATING, "creating")
                and bool(job.pending_clips())
            )
            if can_resume:
                if st.button(
                    _t("▶️ 继续生成", "▶️ Resume"),
                    use_container_width=True,
                    type="primary",
                ):
                    _start_thread(job_id)
                    st.rerun()

    with c_cancel:
        if st.button(_t("✖ 取消任务", "✖ Cancel"), use_container_width=True):
            _request_stop(job_id)
            _j = JobState.load(job_id)
            _j.overall_status = STATUS_FAILED
            _j.save()
            st.warning(_t("任务已取消。", "Job cancelled."))
            st.rerun()

    with c_home:
        if st.button(_t("🔙 返回主页", "🔙 Home"), use_container_width=True):
            st.session_state["page"] = "home"
            st.rerun()

    if running:
        st.caption(_t("🔄 自动刷新中（每 1.5 秒）…", "🔄 Auto-refreshing every 1.5 s…"))

    st.divider()

    # ── Clip cards ────────────────────────────────────────────────────────
    for clip in job.clips:
        _render_clip_row(clip, job_id)

    # ── Post-generation actions ───────────────────────────────────────────
    st.divider()

    if job.overall_status == STATUS_FAILED:
        st.error(_t("任务已取消或失败。", "Job was cancelled or failed."))

    elif job.overall_status in (
        STATUS_PENDING_REVIEW, "merging", STATUS_PENDING_SRT, STATUS_SRT_REVIEW, STATUS_UPLOADING,
    ) and job.all_clips_confirmed():
        _render_merge_srt_panel(job)

    elif job.all_clips_done() and not running:
        unconfirmed = sum(1 for c in job.clips if not c.get("confirmed"))
        st.info(_t(
            f"🎬 所有片段生成完毕！还有 {unconfirmed} 个片段待确认，"
            "请逐一预览后点击「✅ 确认此片段」。",
            f"🎬 All clips generated! {unconfirmed} clip(s) still need confirmation. "
            "Preview each and click ✅ Confirm.",
        ))

    elif not running and n_fail > 0:
        st.warning(_t(
            f"有 {n_fail} 个片段生成失败，可单独点击「🔄 重试」或整体「▶️ 继续生成」。",
            f"{n_fail} clip(s) failed. Use 🔄 Retry on each, or ▶️ Resume to retry all.",
        ))


# ─── History panel (Module 8) ────────────────────────────────────────────────

def _render_history_panel() -> None:
    st.title(_t("📋 历史记录", "📋 History"))

    all_jobs = JobState.load_all()

    if not all_jobs:
        st.info(_t("暂无任务记录。", "No jobs found."))
        return

    # ── Status filter buttons ─────────────────────────────────────────────
    _FILTER_OPTIONS = [
        ("all",        _t("全部",   "All")),
        ("generating", _t("生成中", "Generating")),
        ("completed",  _t("已完成", "Completed")),
        ("failed",     _t("失败",   "Failed")),
    ]

    if "history_filter" not in st.session_state:
        st.session_state["history_filter"] = "all"

    cols = st.columns(len(_FILTER_OPTIONS))
    for col, (key, label) in zip(cols, _FILTER_OPTIONS):
        count = sum(
            1 for j in all_jobs
            if key == "all"
            or (key == "generating" and j.overall_status not in (STATUS_COMPLETED, STATUS_FAILED))
            or j.overall_status == key
        )
        active = st.session_state["history_filter"] == key
        btn_label = f"**{label}** ({count})" if active else f"{label} ({count})"
        if col.button(btn_label, key=f"hist_filter_{key}", use_container_width=True):
            st.session_state["history_filter"] = key
            st.rerun()

    st.divider()

    # ── Filter jobs ───────────────────────────────────────────────────────
    filt = st.session_state["history_filter"]
    if filt == "all":
        shown = all_jobs
    elif filt == "generating":
        shown = [j for j in all_jobs if j.overall_status not in (STATUS_COMPLETED, STATUS_FAILED)]
    else:
        shown = [j for j in all_jobs if j.overall_status == filt]

    if not shown:
        st.caption(_t("没有符合条件的记录。", "No matching records."))
        return

    # ── Job rows ──────────────────────────────────────────────────────────
    for job in shown:
        status_zh = _OVERALL_STATUS_ZH.get(job.overall_status, job.overall_status)
        counts = job.clip_counts()
        done_n = counts[CLIP_DONE]
        total_n = len(job.clips)

        upload_stage = job.stages.get("upload", {})
        accounts = list((upload_stage.get("results") or {}).keys())
        accounts_str = ", ".join(accounts) if accounts else "—"

        scheduled_at = job.params.get("scheduled_at") or "—"

        header = (
            f"**{job.song or '—'}**"
            f"  ·  {job.artist or '—'}"
            f"  ·  {status_zh}"
            f"  ·  {done_n}/{total_n} 片段"
            f"  ·  {job.created_at[:16]}"
        )

        with st.expander(header, expanded=False):
            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(f"**{_t('创建时间','Created')}**\n\n{job.created_at[:16]}")
            c2.markdown(f"**{_t('上传账号','Account')}**\n\n{accounts_str}")
            c3.markdown(f"**{_t('发布时间','Scheduled')}**\n\n{scheduled_at}")
            c4.markdown(f"**TikTok 链接**\n\n—")

            st.markdown(f"**{_t('片段状态','Clip Status')}**")
            clip_cols = st.columns(min(total_n, 8))
            for i, clip in enumerate(job.clips):
                icon = CLIP_ICONS.get(clip["status"], "❓")
                clip_cols[i % 8].caption(f"{icon} #{i}")

            final_path = (job.stages.get("upload") or {}).get("output_path") or \
                         (job.stages.get("merge") or {}).get("output_path")
            if final_path and Path(final_path).exists():
                st.markdown(f"**{_t('最终视频','Final Video')}**  `{final_path}`")

            st.divider()
            btn_col1, btn_col2 = st.columns([1, 4])
            if job.overall_status not in (STATUS_COMPLETED, STATUS_FAILED):
                if btn_col1.button(
                    _t("▶ 继续执行", "▶ Resume"),
                    key=f"hist_resume_{job.job_id}",
                ):
                    st.session_state["active_job_id"] = job.job_id
                    st.session_state["page"] = "execution"
                    st.rerun()
            else:
                btn_col1.caption(_t(f"状态：{status_zh}", f"Status: {status_zh}"))


# ─── Account management panel (Module 9) ─────────────────────────────────────

_ENV_FILE = Path(__file__).parent / ".env"


def _cookie_validity(path_str: str) -> tuple[str, str]:
    """Return (badge_emoji + label, streamlit color) for a cookies path."""
    p = Path(path_str)
    if not p.exists():
        return "🔴 文件缺失", "error"
    age_days = (datetime.datetime.now() - datetime.datetime.fromtimestamp(p.stat().st_mtime)).days
    if age_days <= 30:
        return f"🟢 有效（{age_days}天前更新）", "success"
    elif age_days <= 60:
        return f"🟡 可能即将过期（{age_days}天前更新）", "warning"
    else:
        return f"🔴 可能已失效（{age_days}天前更新）", "error"


def _last_upload_time(account_name: str, all_jobs: list) -> str:
    """Return the most recent upload time for an account from job history."""
    latest = None
    for job in all_jobs:
        results = (job.stages.get("upload") or {}).get("results") or {}
        if account_name in results and results[account_name] == "success":
            scheduled = job.params.get("scheduled_at") or job.updated_at
            if scheduled and (latest is None or scheduled > latest):
                latest = scheduled
    return latest[:16] if latest else "—"


def _render_accounts_panel() -> None:
    st.title(_t("👤 账号管理", "👤 Account Management"))

    # Read all TIKTOK_COOKIES_* from env
    raw_accounts = {
        k[len("TIKTOK_COOKIES_"):].lower(): v
        for k, v in os.environ.items()
        if k.startswith("TIKTOK_COOKIES_") and k != "TIKTOK_COOKIES_"
    }

    all_jobs = JobState.load_all()

    # ── Existing accounts ─────────────────────────────────────────────────
    if raw_accounts:
        st.subheader(_t("现有账号", "Existing Accounts"))
        for acct_name, cookies_path in raw_accounts.items():
            validity_label, validity_level = _cookie_validity(cookies_path)
            last_upload = _last_upload_time(acct_name, all_jobs)

            with st.expander(f"**{acct_name}**  ·  {validity_label}", expanded=False):
                c1, c2 = st.columns(2)
                c1.markdown(f"**{_t('最近上传', 'Last Upload')}**\n\n{last_upload}")

                if validity_level == "error":
                    c2.error(validity_label)
                elif validity_level == "warning":
                    c2.warning(validity_label)
                else:
                    c2.success(validity_label)

                new_path = st.text_input(
                    _t("Cookies 文件路径", "Cookies file path"),
                    value=cookies_path,
                    key=f"acct_path_{acct_name}",
                )
                if st.button(_t("💾 保存路径", "💾 Save path"), key=f"acct_save_{acct_name}"):
                    env_key = f"TIKTOK_COOKIES_{acct_name.upper()}"
                    set_key(str(_ENV_FILE), env_key, new_path)
                    st.success(_t(
                        "已保存，请重启 Streamlit 使新配置生效。",
                        "Saved. Please restart Streamlit for the change to take effect.",
                    ))
    else:
        st.info(_t("尚未配置任何 TikTok 账号。", "No TikTok accounts configured yet."))

    st.divider()

    # ── Add new account ───────────────────────────────────────────────────
    st.subheader(_t("添加新账号", "Add New Account"))
    with st.form("add_account_form"):
        new_name = st.text_input(
            _t("账号名（英文，不含空格）", "Account name (letters/numbers, no spaces)"),
            placeholder="e.g. myaccount",
        )
        new_cookies = st.text_input(
            _t("Cookies 文件路径", "Cookies file path"),
            placeholder="cookies/myaccount.txt",
        )
        submitted = st.form_submit_button(_t("➕ 添加账号", "➕ Add Account"))

    if submitted:
        name_clean = new_name.strip().replace(" ", "_").upper()
        path_clean = new_cookies.strip()
        if not name_clean:
            st.error(_t("账号名不能为空。", "Account name cannot be empty."))
        elif not path_clean:
            st.error(_t("Cookies 路径不能为空。", "Cookies path cannot be empty."))
        elif name_clean in [a.upper() for a in raw_accounts]:
            st.error(_t("该账号名已存在。", "Account name already exists."))
        else:
            env_key = f"TIKTOK_COOKIES_{name_clean}"
            set_key(str(_ENV_FILE), env_key, path_clean)
            st.success(_t(
                f"账号 {name_clean.lower()} 已添加，请重启 Streamlit 使配置生效。",
                f"Account {name_clean.lower()} added. Please restart Streamlit to apply.",
            ))


# ─── Legacy single-clip form (backward compat) ───────────────────────────────

def _render_legacy_form() -> None:
    lang = st.session_state.get("lang", "zh")
    step_labels = _STEP_LABELS.get(lang, _STEP_LABELS["zh"])

    st.caption(_t(
        "⚠️ 旧版单步 pipeline，不支持断点续传。完整功能请使用上方「新建多片段任务」。",
        "⚠️ Legacy single-step pipeline — no checkpoint resume. Use the multi-clip form above for full features.",
    ))

    with st.form("legacy_form"):
        prompt = st.text_area(
            _t("视频 Prompt", "Video Prompt"),
            placeholder=_t("例：一只橘猫坐在咖啡馆窗边", "Example: An orange cat by a cafe window"),
            height=100,
        )
        audio_files = list_audio_files()
        selected_audio = None
        if audio_files:
            selected_audio = st.selectbox(
                _t("背景音乐", "BGM"),
                audio_files,
                format_func=lambda p: Path(p).name,
            )

        col1, col2 = st.columns(2)
        with col1:
            style    = st.selectbox(_t("视频风格", "Style"), STYLE_OPTIONS, format_func=_style_label)
            duration = st.slider(_t("时长(秒)", "Duration (s)"), 5, 15, 5)
        with col2:
            account    = st.selectbox(_t("TikTok 账号", "Account"), _get_tiktok_accounts())
            resolution = st.selectbox(_t("分辨率", "Resolution"), ["480p", "720p", "1080p"])
            ratio      = st.selectbox(_t("比例", "Ratio"), ["9:16", "16:9", "1:1"])

        submitted = st.form_submit_button(
            _t("🚀 开始（旧版）", "🚀 Start (Legacy)"),
            use_container_width=True,
        )

    if submitted:
        if not prompt.strip():
            st.error(_t("请输入 Prompt。", "Please enter a prompt."))
            return

        step_states: dict[str, str] = {s: "pending" for s in STEPS}
        placeholders = {s: st.empty() for s in STEPS}

        def _render_steps() -> None:
            icons = {"pending": "⬜", "running": "⏳", "done": "✅", "error": "❌"}
            for s in STEPS:
                state = step_states[s]
                ph    = placeholders[s]
                label = step_labels.get(s, s)
                if state == "running":
                    ph.info(f"{icons[state]} **{label}** — {_t('进行中…', 'Running…')}")
                elif state == "done":
                    ph.success(f"{icons[state]} {label}")
                elif state == "error":
                    ph.error(f"{icons[state]} **{label}** — {_t('失败', 'Failed')}")
                else:
                    ph.markdown(f"{icons[state]} {label}")

        _render_steps()
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
            _render_steps()
            if status == "error":
                st.error(_t(
                    f"步骤「{step_labels.get(step, step)}」失败：{detail}",
                    f"Step '{step_labels.get(step, step)}' failed: {detail}",
                ))
                return
        st.success(_t("🎉 全部完成！", "🎉 All done!"))


# ─── Page setup ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TikTok Dashboard v2",
    page_icon="🎬",
    layout="wide",
)

# ─── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.selectbox(
        "Language / 语言",
        ["zh", "en"],
        format_func=lambda c: "中文" if c == "zh" else "English",
        key="lang",
    )

    st.divider()
    st.subheader(_t("⏳ 未完成任务", "⏳ Incomplete Jobs"))

    _incomplete = get_incomplete_jobs()
    if _incomplete:
        for _j in _incomplete[:6]:
            _c      = _j.clip_counts()
            _badge  = " 🔄" if _j.overall_status == STATUS_GENERATING else ""
            _label  = f"{_j.song or _j.job_id[:8]}{_badge}  {_c[CLIP_DONE]}/{len(_j.clips)}"
            if st.button(_label, key=f"sb_{_j.job_id}", use_container_width=True):
                st.session_state["active_job_id"] = _j.job_id
                st.session_state["page"] = "execution"
                st.rerun()
    else:
        st.caption(_t("暂无未完成任务", "No incomplete jobs"))

    st.divider()
    if st.button(_t("🏠 主页", "🏠 Home"), use_container_width=True, key="sb_home"):
        st.session_state["page"] = "home"
        st.rerun()
    if st.button(_t("📋 历史记录", "📋 History"), use_container_width=True, key="sb_history"):
        st.session_state["page"] = "history"
        st.rerun()
    if st.button(_t("👤 账号管理", "👤 Accounts"), use_container_width=True, key="sb_accounts"):
        st.session_state["page"] = "accounts"
        st.rerun()

# ─── Session state defaults ───────────────────────────────────────────────────

if "page" not in st.session_state:
    st.session_state["page"] = "home"
if "active_job_id" not in st.session_state:
    st.session_state["active_job_id"] = None

# ─── Page router ──────────────────────────────────────────────────────────────

_page = st.session_state.get("page", "home")

# ══════════════════════════════════════════════════════════════════════════════
# HOME
# ══════════════════════════════════════════════════════════════════════════════

if _page == "home":
    st.title(_t("🎬 TikTok 自动发布仪表盘", "🎬 TikTok Auto Publishing Dashboard"))
    st.caption(_t(
        "Seedance 多片段生成 → 断点续传 → 字幕 → 自动上传",
        "Multi-clip Seedance → checkpoint → subtitles → auto-upload",
    ))

    # ── BGM Manager (Module 1) ────────────────────────────────────────────
    with st.expander(
        _t("🎵 BGM 管理器", "🎵 BGM Manager"),
        expanded=st.session_state.get("bgm_selected_path") is not None,
    ):
        _render_bgm_manager()

    st.divider()

    # ── Task creation panel (Module 2) ───────────────────────────────────
    st.subheader(_t("📝 新建多片段任务", "📝 Create Multi-clip Job"))
    _render_job_creation_panel()

    # ── Legacy form ───────────────────────────────────────────────────────
    st.divider()
    with st.expander(
        _t("🔧 旧版单片段流程（向后兼容）", "🔧 Legacy Single-clip Flow (backward compat)"),
        expanded=False,
    ):
        _render_legacy_form()

# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

elif _page == "execution":
    _job_id = st.session_state.get("active_job_id")

    if not _job_id:
        st.error(_t("没有活动任务，请返回主页。", "No active job. Please go home."))
        if st.button(_t("🏠 主页", "🏠 Home")):
            st.session_state["page"] = "home"
            st.rerun()
        st.stop()

    try:
        _job = JobState.load(_job_id)
    except FileNotFoundError:
        st.error(_t(f"任务 `{_job_id}` 未找到，可能已被删除。", f"Job `{_job_id}` not found — it may have been deleted."))
        st.stop()

    _render_execution_panel(_job)

    # Auto-rerun while any background thread is alive.
    # sleep goes AFTER rendering so the page shows current state first.
    if _is_running(_job_id) or _is_post_running(_job_id):
        time.sleep(1.5)
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# HISTORY
# ══════════════════════════════════════════════════════════════════════════════

elif _page == "history":
    _render_history_panel()

# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

elif _page == "accounts":
    _render_accounts_panel()
