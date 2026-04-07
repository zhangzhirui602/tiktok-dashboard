# TikTok Dashboard

A Streamlit dashboard for multi-clip TikTok video production with checkpoint resume.

Current end-to-end flow:

1. Generate multiple clips from prompts via Seedance API
2. Review and confirm clips one by one
3. Merge confirmed clips with FFmpeg
4. Generate subtitles with Whisper, edit SRT, preview burned result before upload
5. Upload to one or more TikTok accounts

This dashboard is a UI/orchestration layer on top of sibling repositories:

- Video-Editing-FFmpeg-librosa-Whisper-
- tiktok-uploader-mcp

## Features

- Bilingual UI (Chinese and English)
- Job-based workflow with persistent checkpoint resume (state stored in tmp/jobs)
- Clip-level controls: retry, regenerate with edited prompt, per-clip confirmation
- BGM Manager (assets/bgm): upload, preview, delete, BPM analysis, suggested clip count
- Subtitle workflow: Whisper model/language options, SRT preview/edit, re-run recognition
- Multi-account TikTok upload with clearer failure diagnostics

## Module Status (UI v2)

- Done: Module 1 BGM Manager
- Done: Module 2 Job Creation Panel
- Planned: Module 3 AI Prompt Expansion
- Done: Module 4 Checkpoint Resume System
- Done: Module 5 Execution Panel
- Done: Module 6 Subtitle Generation and Review
- Done: Module 7 Upload (basic immediate upload; scheduled upload not yet implemented)
- Planned: Module 8 History Panel
- Planned: Module 9 Account Management

## Requirements

- Windows (recommended for this workspace setup)
- Python 3.10+
- FFmpeg installed and available in PATH
- Valid ARK API key for Seedance
- TikTok cookies exported in Netscape format

## Project Structure

```
tiktok-dashboard/
	app.py                    # Streamlit UI (dashboard v2)
	pipeline.py               # Clip generation / merge / SRT / upload orchestration
	job_state.py              # Persistent job state and checkpoint logic
	modules/bgm_manager.py    # BGM file management and BPM analysis
	requirements.txt          # Dashboard dependencies
	cookies/                  # Local cookies files (ignored by git)
	tmp/                      # Job runtime files and outputs (ignored by git)
	assets/bgm/               # Local BGM library (ignored by git)
```

## Setup

### 1. Create and activate virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dashboard dependencies

```powershell
pip install -r requirements.txt
```

### 3. Install sibling stack dependencies

```powershell
pip install -r ..\Video_Editing_FFmpeg_librosa_Whisper\Video-Editing-FFmpeg-librosa-Whisper-\requirements.txt
pip install -r ..\mcp-tiktok-uploader-mcp\tiktok-uploader-mcp\requirements.txt
python -m playwright install chromium
```

### 4. Configure environment variables

```powershell
Copy-Item .env.example .env
```

Set at least:

- ARK_API_KEY
- TIKTOK_COOKIES_<ACCOUNT_NAME>

Example:

```
TIKTOK_COOKIES_MAIN=cookies/main_account.txt
```

### 5. Verify sibling repository paths

Expected sibling folders under Desktop:

- Video_Editing_FFmpeg_librosa_Whisper/Video-Editing-FFmpeg-librosa-Whisper-
- mcp-tiktok-uploader-mcp/tiktok-uploader-mcp

If your paths differ, update constants in pipeline.py.

## Run

```powershell
streamlit run app.py
```

Open the local URL shown by Streamlit.

## Troubleshooting

### Upload fails with NotImplementedError on Windows

The pipeline sets Windows Proactor event-loop policy before Playwright upload.
If the error persists, restart Streamlit and retry.

### Upload fails with missing package errors

Install dependencies in the same Python environment used by Streamlit.

### Upload authentication fails

Check:

- cookies file exists
- TIKTOK_COOKIES_<ACCOUNT_NAME> path is correct
- cookies still include valid sessionid/sessionid_ss/sid_tt

### Whisper/subtitle stage fails or subtitles are sentence-level

Check:

- openai-whisper is installed in the active environment
- ffmpeg is available in PATH
- selected BGM or merged video contains audible vocals

The app writes word-level SRT for subtitle review and allows re-running recognition.

## Security Notes

- Never commit .env or cookies files
- Keep local media under assets/bgm (ignored by git)
- Rotate API keys/cookies immediately if exposed

## License

Follow licenses of upstream repositories and your internal usage policy.