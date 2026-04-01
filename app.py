"""TikTok 自动发布仪表盘 — Streamlit UI v2

模块状态：
  ✅ 模块 4  断点续传系统 (job_state.py + pipeline.py)
  ✅ 模块 5  执行面板（本文件）
  ⬜ 模块 1  BGM 管理器
  ⬜ 模块 2  任务创建面板（当前为简化版）
  ⬜ 模块 3  AI Prompt 扩展
  ⬜ 模块 6  字幕生成
  ⬜ 模块 7  上传调度
  ⬜ 模块 8  历史记录
  ⬜ 模块 9  账号管理
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from job_state import (
    CLIP_DONE,
    CLIP_FAILED,
    CLIP_PENDING,
    CLIP_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_PENDING_REVIEW,
    JobState,
    get_incomplete_jobs,
)
from pipeline import STEPS, STYLE_MAP, list_audio_files, run_job_clips, run_pipeline

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

    elif job.all_clips_confirmed():
        st.success(_t(
            "🎉 所有片段已确认，可以开始合并！",
            "🎉 All clips confirmed — ready to merge!",
        ))
        if st.button(
            _t("🎬 开始合并与字幕生成（模块 6 待实现）", "🎬 Start Merge & Subtitles (Module 6 coming)"),
            type="primary",
            use_container_width=True,
        ):
            st.info(_t(
                "合并与字幕功能将在模块 6 实现。",
                "Merge and subtitle features will be added in Module 6.",
            ))

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

    # ── Multi-clip job creation ───────────────────────────────────────────
    st.subheader(_t("📝 新建多片段任务", "📝 Create Multi-clip Job"))

    with st.form("new_job_form"):
        col_prompts, col_settings = st.columns([2, 1])

        with col_prompts:
            prompts_raw = st.text_area(
                _t("视频 Prompts（每行一条）", "Video Prompts (one per line)"),
                placeholder=_t(
                    "一只橘猫坐在咖啡馆窗边，慵懒地望向窗外\n"
                    "夕阳下的海滩，浪花拍打礁石，超慢动作\n"
                    "繁华都市夜景鸟瞰，霓虹灯倒映在雨后街道",
                    "An orange cat sits by a cafe window gazing outside\n"
                    "Sunset beach with waves crashing in slow motion\n"
                    "Aerial city nightscape, neon reflections on wet streets",
                ),
                height=150,
            )

        with col_settings:
            song    = st.text_input(_t("歌曲名称", "Song Title"))
            artist  = st.text_input(_t("艺术家", "Artist"))
            style   = st.selectbox(_t("视频风格", "Style"), STYLE_OPTIONS, format_func=_style_label)
            account = st.selectbox(_t("TikTok 账号", "Account"), _get_tiktok_accounts())

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            resolution = st.selectbox(_t("分辨率", "Resolution"), ["480p", "720p", "1080p"])
        with col_b:
            ratio = st.selectbox(_t("画面比例", "Ratio"), ["9:16", "16:9", "1:1"])
        with col_c:
            duration = st.slider(_t("片段时长（秒）", "Clip duration (s)"), 5, 15, 5)

        audio_files = list_audio_files()
        bgm_path: str | None = None
        if audio_files:
            bgm_path = st.selectbox(
                _t("背景音乐（可选）", "BGM (optional)"),
                [None, *audio_files],
                format_func=lambda p: _t("无", "None") if p is None else Path(p).name,
            )

        submit_new = st.form_submit_button(
            _t("🚀 创建任务并开始生成", "🚀 Create Job & Start"),
            type="primary",
            use_container_width=True,
        )

    if submit_new:
        prompts = [p.strip() for p in prompts_raw.strip().splitlines() if p.strip()]
        if not prompts:
            st.error(_t("请至少填写一条 Prompt。", "Please enter at least one prompt."))
        else:
            _new_job = JobState.create(
                prompts=prompts,
                style=style,
                tiktok_accounts=[account],
                bgm_path=bgm_path,
                resolution=resolution,
                ratio=ratio,
                duration=duration,
                song=song,
                artist=artist,
            )
            st.session_state["active_job_id"] = _new_job.job_id
            st.session_state["page"] = "execution"
            _start_thread(_new_job.job_id)
            st.rerun()

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

    # Auto-rerun while generation thread is alive.
    # sleep goes AFTER rendering so the page shows current state first.
    if _is_running(_job_id):
        time.sleep(1.5)
        st.rerun()
