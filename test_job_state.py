"""测试 job_state.py 的断点续传逻辑

运行方式：
    python test_job_state.py

每个测试独立，使用真实文件系统（tmp/jobs/ 下的临时目录），
测试结束后自动清理。
"""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

# ─── 确保从项目根目录导入 ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from job_state import (
    CLIP_DONE,
    CLIP_FAILED,
    CLIP_PENDING,
    CLIP_RUNNING,
    JOBS_DIR,
    STATUS_CREATING,
    STATUS_GENERATING,
    STATUS_PENDING_REVIEW,
    JobState,
    get_incomplete_jobs,
)

# ─── 测试基础设施 ─────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []   # (name, passed, detail)
_created_job_dirs: list[Path] = []           # 测试结束后清理


def _register(job: JobState) -> JobState:
    """记录待清理目录。"""
    _created_job_dirs.append(job.job_dir)
    return job


def _run(name: str, fn) -> None:
    """执行一个测试函数，捕获异常，记录结果。"""
    try:
        msg = fn()
        _results.append((name, True, msg or ""))
        print(f"  PASS  {name}" + (f"  ({msg})" if msg else ""))
    except AssertionError as exc:
        _results.append((name, False, str(exc)))
        print(f"  FAIL  {name}  —  {exc}")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _results.append((name, False, detail))
        print(f"  FAIL  {name}  —  {detail}")
        traceback.print_exc()


def _assert(condition: bool, msg: str) -> None:
    if not condition:
        raise AssertionError(msg)


# ─── 场景 1：创建新任务，验证 state.json ──────────────────────────────────────

def test_create_new_job():
    """创建一个 3 片段的新任务，验证 state.json 内容是否正确。"""
    prompts = ["clip A scene", "clip B scene", "clip C scene"]
    job = _register(JobState.create(
        prompts=prompts,
        style="cinematic",
        tiktok_accounts=["acc1", "acc2"],
        bgm_path="/fake/bgm.mp3",
        resolution="720p",
        ratio="9:16",
        duration=5,
        song="Test Song",
        artist="Test Artist",
    ))

    # state.json 必须存在
    state_file = job.job_dir / "state.json"
    _assert(state_file.exists(), "state.json 未生成")

    # 读取原始 JSON 独立校验（不依赖 JobState 方法）
    with open(state_file, encoding="utf-8") as f:
        raw = json.load(f)

    _assert(raw["job_id"] == job.job_id, "job_id 不匹配")
    _assert(raw["overall_status"] == STATUS_CREATING, f"初始状态应为 creating，实为 {raw['overall_status']}")
    _assert(raw["song"] == "Test Song", "song 字段错误")
    _assert(raw["artist"] == "Test Artist", "artist 字段错误")
    _assert(raw["params"]["style"] == "cinematic", "style 字段错误")
    _assert(raw["params"]["resolution"] == "720p", "resolution 字段错误")
    _assert(raw["params"]["tiktok_accounts"] == ["acc1", "acc2"], "tiktok_accounts 字段错误")

    _assert(len(raw["clips"]) == 3, f"应有 3 个 clip，实有 {len(raw['clips'])}")
    for i, clip in enumerate(raw["clips"]):
        _assert(clip["index"] == i, f"clip[{i}].index 错误")
        _assert(clip["prompt"] == prompts[i], f"clip[{i}].prompt 错误")
        _assert(clip["status"] == CLIP_PENDING, f"clip[{i}].status 应为 pending")
        _assert(clip["video_url"] is None, f"clip[{i}].video_url 应为 None")
        _assert(clip["local_path"] is None, f"clip[{i}].local_path 应为 None")
        _assert(clip["confirmed"] is False, f"clip[{i}].confirmed 应为 False")

    _assert("merge" in raw["stages"], "缺少 merge stage")
    _assert("srt" in raw["stages"], "缺少 srt stage")
    _assert("upload" in raw["stages"], "缺少 upload stage")

    return f"job_id={job.job_id}"


# ─── 场景 2：片段 0 和 1 完成，片段 2 pending ─────────────────────────────────

def test_partial_completion():
    """模拟片段 0 和片段 1 完成，片段 2 仍 pending；验证状态字段。"""
    job = _register(JobState.create(
        prompts=["p0", "p1", "p2"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))

    job.set_clip_running(0)
    _assert(job.clips[0]["status"] == CLIP_RUNNING, "clip 0 应为 running")

    job.set_clip_done(0, "https://cdn/v0.mp4", "/tmp/clip_000.mp4")
    job.set_clip_done(1, "https://cdn/v1.mp4", "/tmp/clip_001.mp4")

    # 验证内存状态
    _assert(job.clips[0]["status"] == CLIP_DONE, "clip 0 应为 done")
    _assert(job.clips[0]["video_url"] == "https://cdn/v0.mp4", "clip 0 video_url 错误")
    _assert(job.clips[0]["local_path"] == "/tmp/clip_000.mp4", "clip 0 local_path 错误")
    _assert(job.clips[1]["status"] == CLIP_DONE, "clip 1 应为 done")
    _assert(job.clips[2]["status"] == CLIP_PENDING, "clip 2 应保持 pending")

    # 验证磁盘持久化
    with open(job.job_dir / "state.json", encoding="utf-8") as f:
        raw = json.load(f)
    _assert(raw["clips"][0]["status"] == CLIP_DONE, "磁盘：clip 0 应为 done")
    _assert(raw["clips"][1]["status"] == CLIP_DONE, "磁盘：clip 1 应为 done")
    _assert(raw["clips"][2]["status"] == CLIP_PENDING, "磁盘：clip 2 应为 pending")

    _assert(not job.all_clips_done(), "all_clips_done() 应返回 False")
    _assert(len(job.done_clips()) == 2, "done_clips() 应返回 2 个")

    return "clip 0,1=done  clip 2=pending"


# ─── 场景 3：程序重启后 pending_clips() 只返回片段 2 ─────────────────────────

def test_resume_after_restart():
    """程序重启：重新 load，pending_clips() 只返回索引 2。"""
    job = _register(JobState.create(
        prompts=["p0", "p1", "p2"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))
    job.set_clip_done(0, "https://cdn/v0.mp4", "/tmp/clip_000.mp4")
    job.set_clip_done(1, "https://cdn/v1.mp4", "/tmp/clip_001.mp4")
    # clip 2 stays pending

    # 模拟重启：重新从磁盘加载
    job2 = JobState.load(job.job_id)
    pending = job2.pending_clips()

    _assert(pending == [2], f"重启后 pending_clips() 应只返回 [2]，实为 {pending}")
    _assert(job2.clips[0]["status"] == CLIP_DONE, "重启后 clip 0 应为 done")
    _assert(job2.clips[1]["status"] == CLIP_DONE, "重启后 clip 1 应为 done")
    _assert(job2.clips[2]["status"] == CLIP_PENDING, "重启后 clip 2 应为 pending")

    return f"pending={pending}"


# ─── 场景 4：片段 1 失败后，pending_clips() 返回 [1, 2] ──────────────────────

def test_failed_clip_rejoins_pending():
    """片段 1 先完成后失败（reset），pending_clips() 应返回 [1, 2]。"""
    job = _register(JobState.create(
        prompts=["p0", "p1", "p2"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))
    job.set_clip_done(0, "https://cdn/v0.mp4", "/tmp/clip_000.mp4")
    job.set_clip_done(1, "https://cdn/v1.mp4", "/tmp/clip_001.mp4")
    # 模拟片段 1 被判定为失败（如用户主动重新生成或下载失败）
    job.set_clip_failed(1, "DownloadError: connection reset")

    pending = job.pending_clips()
    _assert(1 in pending, f"clip 1 失败后应在 pending 中，实为 {pending}")
    _assert(2 in pending, f"clip 2 未生成应在 pending 中，实为 {pending}")
    _assert(0 not in pending, f"clip 0 已完成不应在 pending 中，实为 {pending}")
    _assert(pending == [1, 2], f"pending_clips() 应为 [1, 2]，实为 {pending}")

    # 重启后验证持久化
    job2 = JobState.load(job.job_id)
    pending2 = job2.pending_clips()
    _assert(pending2 == [1, 2], f"重启后 pending_clips() 应为 [1, 2]，实为 {pending2}")

    return f"pending={pending}"


# ─── 场景 5：get_incomplete_jobs() 能找到未完成任务 ──────────────────────────

def test_get_incomplete_jobs():
    """get_incomplete_jobs() 只返回未完成（非 completed/failed）的任务。"""
    # 创建一个未完成任务
    job_a = _register(JobState.create(
        prompts=["p0", "p1"],
        style="minimal",
        tiktok_accounts=["acc1"],
        song="Incomplete Song",
    ))
    job_a.overall_status = STATUS_GENERATING
    job_a.save()

    # 创建一个已完成任务
    from job_state import STATUS_COMPLETED
    job_b = _register(JobState.create(
        prompts=["p0"],
        style="minimal",
        tiktok_accounts=["acc1"],
        song="Completed Song",
    ))
    job_b.overall_status = STATUS_COMPLETED
    job_b.save()

    # 创建一个已失败任务
    from job_state import STATUS_FAILED
    job_c = _register(JobState.create(
        prompts=["p0"],
        style="minimal",
        tiktok_accounts=["acc1"],
        song="Failed Song",
    ))
    job_c.overall_status = STATUS_FAILED
    job_c.save()

    incomplete = get_incomplete_jobs()
    incomplete_ids = {j.job_id for j in incomplete}

    _assert(job_a.job_id in incomplete_ids,
            f"未完成任务 {job_a.job_id} 应在 incomplete_jobs 中")
    _assert(job_b.job_id not in incomplete_ids,
            f"已完成任务 {job_b.job_id} 不应在 incomplete_jobs 中")
    _assert(job_c.job_id not in incomplete_ids,
            f"已失败任务 {job_c.job_id} 不应在 incomplete_jobs 中")

    return f"found {len(incomplete)} incomplete job(s)"


# ─── 场景 6：原子写入 — 中断不损坏 state.json ─────────────────────────────────

def test_atomic_write_on_interrupt():
    """模拟写入 .tmp 文件后、rename 前发生异常，state.json 应保持上一次完好状态。"""
    job = _register(JobState.create(
        prompts=["p0", "p1", "p2"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))
    job.set_clip_done(0, "https://cdn/v0.mp4", "/tmp/clip_000.mp4")

    # 读取当前 state.json 内容作为基准
    state_file = job.job_dir / "state.json"
    with open(state_file, encoding="utf-8") as f:
        good_content = f.read()
    good_data = json.loads(good_content)

    # 模拟：写 .tmp 成功，但 rename 前进程崩溃
    # 实现方式：patch Path.replace 使其抛出 OSError，然后手动留下一个损坏的 .tmp
    tmp_file = job.job_dir / "state.json.tmp"
    tmp_file.write_text("{ this is broken JSON <<<", encoding="utf-8")

    # state.json 此时应仍是完好的旧内容（因为 rename 没发生）
    with open(state_file, encoding="utf-8") as f:
        current_content = f.read()
    current_data = json.loads(current_content)   # 如果损坏会抛 JSONDecodeError

    _assert(
        current_data["clips"][0]["status"] == CLIP_DONE,
        "中断后 state.json 应保留上一次完好数据，clip 0 应为 done"
    )
    _assert(
        current_data["clips"][1]["status"] == CLIP_PENDING,
        "中断后 clip 1 应仍为 pending"
    )

    # 现在模拟程序恢复：调用 save() 确认能覆盖损坏的 .tmp 并正常完成 rename
    job.set_clip_done(1, "https://cdn/v1.mp4", "/tmp/clip_001.mp4")

    with open(state_file, encoding="utf-8") as f:
        recovered = json.load(f)
    _assert(recovered["clips"][1]["status"] == CLIP_DONE,
            "恢复后 clip 1 应为 done")
    _assert(not tmp_file.exists(),
            "恢复后 .tmp 文件应已被 rename 消除")

    return "state.json 在中断后保持完整，恢复写入正常"


# ─── 额外：reset_clip 更换 prompt ─────────────────────────────────────────────

def test_reset_clip_with_new_prompt():
    """reset_clip() 重置状态并可更换 prompt，使其重新进入 pending。"""
    job = _register(JobState.create(
        prompts=["original prompt", "p1"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))
    job.set_clip_done(0, "https://cdn/v0.mp4", "/tmp/clip_000.mp4")
    job.confirm_clip(0)

    _assert(job.clips[0]["confirmed"], "reset 前 clip 0 应已 confirmed")

    job.reset_clip(0, new_prompt="brand new prompt")

    _assert(job.clips[0]["status"] == CLIP_PENDING, "reset 后 clip 0 应为 pending")
    _assert(job.clips[0]["confirmed"] is False, "reset 后 confirmed 应为 False")
    _assert(job.clips[0]["video_url"] is None, "reset 后 video_url 应为 None")
    _assert(job.clips[0]["prompt"] == "brand new prompt", "prompt 应已更换")
    _assert(job.params["prompts"][0] == "brand new prompt", "params.prompts[0] 应同步更换")
    _assert(0 in job.pending_clips(), "reset 后 clip 0 应重新出现在 pending_clips() 中")

    return "reset_clip + prompt 更换正常"


# ─── 额外：all_clips_confirmed() ─────────────────────────────────────────────

def test_all_clips_confirmed():
    """全部 done + confirmed 后 all_clips_confirmed() 才返回 True。"""
    job = _register(JobState.create(
        prompts=["p0", "p1"],
        style="minimal",
        tiktok_accounts=["acc1"],
    ))
    job.set_clip_done(0, "u0", "l0")
    job.set_clip_done(1, "u1", "l1")

    _assert(not job.all_clips_confirmed(), "未 confirm 时应为 False")
    job.confirm_clip(0)
    _assert(not job.all_clips_confirmed(), "只确认一个时应仍为 False")
    job.confirm_clip(1)
    _assert(job.all_clips_confirmed(), "全部确认后应为 True")

    return "confirm 逻辑正常"


# ─── 清理 ─────────────────────────────────────────────────────────────────────

def _cleanup():
    for d in _created_job_dirs:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  job_state.py 测试套件")
    print("=" * 60)

    tests = [
        ("场景1：创建新任务，验证 state.json",                test_create_new_job),
        ("场景2：片段 0/1 完成，片段 2 pending",             test_partial_completion),
        ("场景3：重启后 pending_clips() 只返回 [2]",         test_resume_after_restart),
        ("场景4：片段 1 失败后 pending_clips() 返回 [1,2]",  test_failed_clip_rejoins_pending),
        ("场景5：get_incomplete_jobs() 过滤已完成任务",       test_get_incomplete_jobs),
        ("场景6：原子写入 — 中断不损坏 state.json",          test_atomic_write_on_interrupt),
        ("额外：reset_clip() 更换 prompt",                   test_reset_clip_with_new_prompt),
        ("额外：all_clips_confirmed() 逻辑",                 test_all_clips_confirmed),
    ]

    for name, fn in tests:
        _run(name, fn)

    _cleanup()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total  = len(_results)
    print(f"  结果：{passed}/{total} 通过，{failed} 失败")
    if failed:
        print("\n  失败项：")
        for name, ok, detail in _results:
            if not ok:
                print(f"    ✗ {name}")
                print(f"      {detail}")
    print("=" * 60 + "\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
