# TikTok Dashboard

A Streamlit dashboard that orchestrates an end-to-end short-video pipeline:

1. Generate a video with Seedance API
2. Download the generated video locally
3. Generate subtitles with Whisper
4. Edit and compose video with FFmpeg-based tooling
5. Upload to TikTok with browser automation

This dashboard is a lightweight UI layer on top of two sibling projects:

- Video-Editing-FFmpeg-librosa-Whisper-
- tiktok-uploader-mcp

## Features

- Bilingual UI: Chinese and English switch in the page
- One-click pipeline execution with step-by-step progress
- Select TikTok account from environment variables
- Audio selection from active video-editor project
- History panel for recent runs
- Improved upload diagnostics for easier debugging

## Requirements

- Windows (recommended for this workspace setup)
- Python 3.10+
- FFmpeg installed and available in PATH
- A valid ARK API key for Seedance
- TikTok cookies exported in Netscape format

## Project Structure

```
tiktok-dashboard/
	app.py              # Streamlit UI
	pipeline.py         # Orchestration logic
	.env.example        # Environment template
	requirements.txt    # Dashboard dependencies
	cookies/            # Local cookies files (ignored by git)
	tmp/                # Temporary files (ignored by git)
```

## Setup

### 1. Create and activate virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

The upload step also requires dependencies from the TikTok uploader stack:

```powershell
pip install pydantic playwright pytz toml
python -m playwright install chromium
```

### 3. Configure environment

```powershell
Copy-Item .env.example .env
```

Edit .env and set:

- ARK_API_KEY
- TIKTOK_COOKIES_<ACCOUNT_NAME> entries

Example:

```
TIKTOK_COOKIES_MAIN=cookies/main_account.txt
```

### 4. Prepare sibling repositories

This dashboard expects sibling folders under Desktop:

- Video_Editing_FFmpeg_librosa_Whisper/Video-Editing-FFmpeg-librosa-Whisper-
- mcp-tiktok-uploader-mcp/tiktok-uploader-mcp

If your folder names differ, update path constants in pipeline.py.

## Run

```powershell
streamlit run app.py
```

Open the local URL shown by Streamlit in your browser.

## Troubleshooting

### Upload step fails with NotImplementedError on Windows

The pipeline includes a Windows event-loop policy fix before Playwright upload.
If you still see this error, fully restart Streamlit and run again.

### Upload step fails with missing package errors

Install missing packages in the same virtual environment used by Streamlit.
Common ones: pydantic, playwright.

### Upload step fails with authentication errors

Check that:

- cookies file exists
- cookies path in .env is correct
- cookies still include valid sessionid/sessionid_ss/sid_tt

### Whisper or subtitle step fails

Check dependencies and configuration in the video-editor project:

- librosa
- openai-whisper
- valid audio file in active project raw_materials/song

## Security Notes

- Never commit .env or cookies files
- Rotate API keys and cookies if accidentally exposed

## License

Follow the licenses of the integrated upstream repositories and your internal usage policy.