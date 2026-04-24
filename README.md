# TikTok Dashboard

**Author:** Zhang Zhirui  
**Contact:** zhangzhiruizzr@gmail.com  
**Copyright © 2026 Zhang Zhirui. All rights reserved.**

This project was independently developed by Zhang Zhirui.

A Streamlit dashboard for multi-clip TikTok video production with checkpoint resume.

Current end-to-end flow:

1. Generate multiple clips from prompts via Seedance API
2. Review and confirm clips one by one
3. Merge confirmed clips with FFmpeg
4. Generate subtitles with Whisper (word-by-word or sentence-by-sentence), edit SRT, preview burned result before upload
5. Upload to one or more TikTok accounts

This dashboard is a UI/orchestration layer on top of sibling repositories:

- Video-Editing-FFmpeg-librosa-Whisper-
- tiktok-uploader-mcp

Optional FastAPI layer (`api/`) exposes the same business logic as HTTP endpoints — runs independently on port 8001, does not affect Streamlit. See `api/README.md`.

## Core Features

| Feature | Description |
|---|---|
| **Multi-clip Video Generation** | Generate multiple video clips from text prompts via Seedance API |
| **Subtitle Pipeline** | Auto-generate SRT with Whisper (word-by-word or sentence mode), edit in-browser, burn into final video |
| **Upload Pipeline** | Automated TikTok upload via Playwright across multiple accounts, with scheduled posting support |
| **Checkpoint Resume** | Job state persisted to disk — resume generation after interruption without losing progress |
| **BGM Manager** | Upload, preview, delete local BGM files; BPM analysis with suggested clip count |
| **Bilingual UI** | Full Chinese / English interface toggle |
| **History & Accounts** | View past jobs, track TikTok post URLs, manage account cookies |

## Features

- Bilingual UI (Chinese and English)
- Job-based workflow with persistent checkpoint resume (state stored in tmp/jobs)
- Clip-level controls: retry, regenerate with edited prompt, per-clip confirmation
- BGM Manager (assets/bgm): upload with explicit confirm button, preview, delete, BPM analysis, suggested clip count
- Subtitle workflow: Whisper model/language options, subtitle display mode (word/sentence), SRT preview/edit, re-run recognition
- Multi-account TikTok upload with clearer failure diagnostics
- Scheduled TikTok posting: pass a publish datetime to tiktok-uploader so TikTok handles the scheduling natively
- TikTok post URL tracking: manually record a post link from the History panel after upload

## Subtitle Display Behavior

- `word`: one subtitle entry per word (karaoke-like)
- `sentence`: sentence-level grouping with these boundaries:
	- punctuation: `,` `，` `.` `。` `?` `？` `!` `！`
	- pause fallback when no punctuation is present
	- hard cap: 12 words per subtitle entry
	- comma stays at the end of the previous sentence
- Behavior is consistent for both Whisper Python API and Whisper CLI fallback outputs

## Module Status (UI v2)

- Done: Module 1 BGM Manager
- Done: Module 2 Job Creation Panel
- Planned: Module 3 AI Prompt Expansion
- Done: Module 4 Checkpoint Resume System
- Done: Module 5 Execution Panel
- Done: Module 6 Subtitle Generation and Review
- Done: Module 7 Upload Scheduler (immediate + TikTok-native scheduled posting, post URL entry)
- Done: Module 8 History Panel
- Done: Module 9 Account Management

## Tech Stack

| Layer | Technology |
|---|---|
| **UI Framework** | [Streamlit](https://streamlit.io) |
| **Video Generation** | Seedance API (Volcano Engine ARK) |
| **Video Editing** | FFmpeg |
| **Audio Analysis** | librosa (BPM detection) |
| **Speech Recognition** | OpenAI Whisper (Python API + CLI fallback) |
| **Browser Automation** | Playwright (Chromium) |
| **Language** | Python 3.10+ |
| **State Persistence** | JSON files (tmp/jobs/) |

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
	api/                      # FastAPI HTTP layer (optional)
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

### Run the API (optional)

```powershell
uvicorn api.main:app --port 8001 --reload
```

Open <http://localhost:8001/docs> for Swagger UI. See `api/README.md` for details.

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

Notes:

- If `openai-whisper` is unavailable, the app automatically falls back to `whisper` CLI.
- `word` mode outputs word-level SRT.
- `sentence` mode normalizes SRT into sentence-level entries with punctuation, pause fallback, and 12-word max-length splitting.

The app allows re-running recognition and manual SRT editing before confirmation.

## Security Notes

- Never commit .env or cookies files
- Keep local media under assets/bgm (ignored by git)
- Rotate API keys/cookies immediately if exposed

## License

Follow licenses of upstream repositories and your internal usage policy.