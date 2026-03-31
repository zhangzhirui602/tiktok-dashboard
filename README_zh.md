# TikTok Dashboard

一个基于 Streamlit 的可视化面板，用于串联短视频全流程：

1. 使用 Seedance API 生成视频
2. 下载生成的视频到本地
3. 使用 Whisper 生成字幕
4. 使用 FFmpeg 流程进行剪辑与合成
5. 使用浏览器自动化上传到 TikTok

本项目是一个轻量 UI 层，依赖以下同级项目能力：

- Video-Editing-FFmpeg-librosa-Whisper-
- tiktok-uploader-mcp

## 功能特性

- 页面支持中英文切换
- 一键执行完整流程，并显示分步进度
- 从环境变量自动识别 TikTok 账号
- 从当前视频工程读取可用音频
- 提供历史记录面板
- 上传失败时提供更详细的错误信息

## 运行要求

- Windows（当前工作区推荐）
- Python 3.10+
- 已安装 FFmpeg，并加入 PATH
- 可用的 ARK API Key（Seedance）
- TikTok cookies（Netscape 格式）

## 目录说明

```
tiktok-dashboard/
  app.py              # Streamlit 页面
  pipeline.py         # 流程编排逻辑
  .env.example        # 环境变量模板
  requirements.txt    # 依赖列表
  cookies/            # 本地 cookies（已加入 gitignore）
  tmp/                # 临时文件（已加入 gitignore）
```

## 安装与配置

### 1. 创建并激活虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. 安装依赖

```powershell
pip install -r requirements.txt
```

上传步骤还需要 TikTok uploader 相关依赖：

```powershell
pip install pydantic playwright pytz toml
python -m playwright install chromium
```

### 3. 配置环境变量

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

### 4. 确保同级仓库路径可用

当前 pipeline.py 默认依赖以下目录结构：

- Video_Editing_FFmpeg_librosa_Whisper/Video-Editing-FFmpeg-librosa-Whisper-
- mcp-tiktok-uploader-mcp/tiktok-uploader-mcp

如果你的路径不同，请修改 pipeline.py 中对应常量。

## 启动

```powershell
streamlit run app.py
```

在浏览器打开 Streamlit 输出的本地地址即可。

## 常见问题

### 1. 上传步骤出现 NotImplementedError（Windows）

项目已在上传前加入 Windows 事件循环策略修复。
若仍报错，请完整重启一次 Streamlit 进程后重试。

### 2. 上传步骤提示缺少模块

请确认当前 Streamlit 使用的虚拟环境中已安装依赖。
常见缺失：pydantic、playwright。

### 3. 上传步骤认证失败

请检查：

- cookies 文件是否存在
- .env 中路径是否正确
- cookies 是否仍包含有效 sessionid/sessionid_ss/sid_tt

### 4. Whisper 或字幕步骤失败

请检查视频编辑项目中的依赖和配置：

- librosa
- openai-whisper
- 当前项目 raw_materials/song 中存在可用音频

## 安全说明

- 不要提交 .env 和 cookies 文件
- 如密钥或 cookies 泄露，请立即更换

## 许可说明

请遵循依赖仓库的开源许可和你的实际使用规范。
