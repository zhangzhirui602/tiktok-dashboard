# TikTok Dashboard

**作者：** 张芷睿  
**联系方式：** zhangzhiruizzr@gmail.com  
**版权所有 © 2026 张芷睿。保留所有权利。**

本项目由张芷睿独立开发。

一个基于 Streamlit 的多片段 TikTok 自动化面板，支持任务断点续传。

当前完整流程：

1. 使用 Seedance API 按多个 Prompt 生成片段
2. 逐片段预览并确认
3. 使用 FFmpeg 合并已确认片段
4. 使用 Whisper 生成字幕（逐词/逐句可选），并在上传前编辑 SRT
5. 上传到一个或多个 TikTok 账号

本项目是一个 UI/编排层，依赖以下同级项目能力：

- Video-Editing-FFmpeg-librosa-Whisper-
- tiktok-uploader-mcp

可选的 FastAPI 接口层（`api/`）将相同业务逻辑以 HTTP API 暴露，独立运行在 8001 端口，不影响 Streamlit。详见 `api/README.md`。

## 核心功能

| 功能 | 说明 |
|---|---|
| **多片段视频生成** | 通过 Seedance API，根据文本 Prompt 批量生成视频片段 |
| **字幕流水线** | Whisper 自动生成 SRT（逐词/逐句可选），支持浏览器内编辑并烧录至最终视频 |
| **上传流水线** | Playwright 自动化多账号 TikTok 上传，支持定时发布 |
| **断点续传** | 任务状态持久化到磁盘，中断后可恢复生成，不丢失进度 |
| **BGM 管理器** | 上传、试听、删除本地 BGM；BPM 分析并自动推荐片段数量 |
| **双语界面** | 支持中文/英文一键切换 |
| **历史记录与账号管理** | 查看历史任务、记录 TikTok 帖子链接、管理账号 Cookie |

## 功能特性

- 支持中英文界面切换
- 任务化流程与断点续传（状态持久化在 tmp/jobs）
- 片段级控制：重试、改 Prompt 重生、逐片段确认
- BGM 管理器（assets/bgm）：上传（需点击确认按钮保存）、试听、删除、BPM 分析、建议片段数
- 字幕流程：可选 Whisper 语言/模型、字幕显示模式（逐词/逐句）、字幕预览编辑、可一键重新识别
- 多账号上传，失败信息更可读
- 定时发布：将发布时间传给 tiktok-uploader，由 TikTok 平台原生处理定时调度
- TikTok 帖子链接记录：上传完成后可在历史记录中手动填写并保存帖子链接

## 字幕显示规则

- `word`：逐词显示（类似卡拉 OK）
- `sentence`：逐句显示，按以下规则切分：
  - 标点边界：`,` `，` `.` `。` `?` `？` `!` `！`
  - 无标点时按停顿阈值自动分句
  - 单条字幕最多 12 个词
  - 逗号保留在前一句末尾
- 无论走 Whisper Python API 还是 Whisper CLI 回退路径，行为保持一致

## 模块状态（UI v2）

- 已完成：模块 1 BGM 管理器
- 已完成：模块 2 任务创建面板
- 规划中：模块 3 AI Prompt 扩展
- 已完成：模块 4 断点续传系统
- 已完成：模块 5 执行面板
- 已完成：模块 6 字幕生成与确认
- 已完成：模块 7 上传调度（立即上传 + TikTok 原生定时发布，帖子链接记录）
- 已完成：模块 8 历史记录
- 已完成：模块 9 账号管理

## 技术栈

| 层级 | 技术 |
|---|---|
| **UI 框架** | [Streamlit](https://streamlit.io) |
| **视频生成** | Seedance API（火山引擎 ARK） |
| **视频剪辑** | FFmpeg |
| **音频分析** | librosa（BPM 检测） |
| **语音识别** | OpenAI Whisper（Python API + CLI 回退） |
| **浏览器自动化** | Playwright（Chromium） |
| **开发语言** | Python 3.10+ |
| **状态持久化** | JSON 文件（tmp/jobs/） |

## 运行要求

- Windows（当前工作区推荐）
- Python 3.10+
- 已安装 FFmpeg，并加入 PATH
- 可用的 ARK API Key（Seedance）
- TikTok cookies（Netscape 格式）

## 目录说明

```
tiktok-dashboard/
  app.py                    # Streamlit 页面（dashboard v2）
  pipeline.py               # 生成/合并/字幕/上传编排
  job_state.py              # 任务状态持久化与断点续传逻辑
  modules/bgm_manager.py    # BGM 文件管理与 BPM 分析
  api/                      # FastAPI HTTP 接口层（可选）
  requirements.txt          # 面板依赖
  cookies/                  # 本地 cookies（已加入 gitignore）
  tmp/                      # 任务运行时文件（已加入 gitignore）
  assets/bgm/               # 本地 BGM 库（已加入 gitignore）
```

## 安装与配置

### 1. 创建并激活虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. 安装面板依赖

```powershell
pip install -r requirements.txt
```

### 3. 安装同级项目依赖

```powershell
pip install -r ..\Video_Editing_FFmpeg_librosa_Whisper\Video-Editing-FFmpeg-librosa-Whisper-\requirements.txt
pip install -r ..\mcp-tiktok-uploader-mcp\tiktok-uploader-mcp\requirements.txt
python -m playwright install chromium
```

### 4. 配置环境变量

```powershell
Copy-Item .env.example .env
```

编辑 .env，至少配置：

- ARK_API_KEY
- TIKTOK_COOKIES_<账号名>

示例：

```
TIKTOK_COOKIES_MAIN=cookies/main_account.txt
```

### 5. 确保同级仓库路径可用

默认依赖以下目录结构：

- Video_Editing_FFmpeg_librosa_Whisper/Video-Editing-FFmpeg-librosa-Whisper-
- mcp-tiktok-uploader-mcp/tiktok-uploader-mcp

如果你的路径不同，请修改 pipeline.py 中对应常量。

## 启动

```powershell
streamlit run app.py
```

在浏览器打开 Streamlit 输出的本地地址。

### 启动 API 服务（可选）

```powershell
uvicorn api.main:app --port 8001 --reload
```

浏览器打开 <http://localhost:8001/docs> 查看 Swagger UI。详见 `api/README.md`。

## 常见问题

### 1. 上传步骤出现 NotImplementedError（Windows）

项目已在上传前设置 Windows Proactor 事件循环策略。
若仍报错，请完整重启 Streamlit 后重试。

### 2. 上传步骤提示缺少模块

请确认 Streamlit 当前使用的 Python 环境已安装依赖。

### 3. 上传步骤认证失败

请检查：

- cookies 文件是否存在
- TIKTOK_COOKIES_<账号名> 路径是否正确
- cookies 是否仍包含有效 sessionid/sessionid_ss/sid_tt

### 4. Whisper 或字幕步骤失败，或字幕仍是整句

请检查：

- 当前环境已安装 openai-whisper
- ffmpeg 在 PATH 中可用
- 选用的 BGM 或合并视频中有人声音轨

说明：

- 若未安装 `openai-whisper`，程序会自动回退到 `whisper` CLI。
- `word` 模式输出逐词 SRT。
- `sentence` 模式会将 SRT 归一化为逐句结果（标点分句 + 停顿兜底 + 12 词上限）。

当前流程支持在界面中重新识别，并在确认前手动编辑 SRT。

## 安全说明

- 不要提交 .env 和 cookies 文件
- 本地音频素材放在 assets/bgm（已加入 gitignore）
- 如密钥或 cookies 泄露，请立即更换

## 许可说明

请遵循依赖仓库的开源许可和你的实际使用规范。
