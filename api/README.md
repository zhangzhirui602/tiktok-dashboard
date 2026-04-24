# tiktok-dashboard API

HTTP wrapper around the existing tiktok-dashboard business logic
(`modules/bgm_manager.py`, `modules/prompt_expander.py`, `job_state.py`,
`pipeline.py`). The Streamlit app (`app.py`) keeps working unchanged — the
API is an **additional** consumer of the same modules, not a replacement.

**Scope**: local/intranet only. No auth, CORS wide-open, paths are hard-coded
to sibling repos on the developer's machine (see `pipeline.py` line 52).

---

## Install

```bash
pip install -r requirements.txt
```

(Adds `fastapi`, `uvicorn[standard]`, `python-multipart` on top of the
existing Streamlit deps.)

## Run

From the `tiktok-dashboard/` project root:

```bash
uvicorn api.main:app --port 8001 --reload
```

Streamlit can run in parallel on its usual port:

```bash
streamlit run app.py     # port 8501
```

## Docs

- Swagger UI: <http://localhost:8001/docs>
- OpenAPI JSON: <http://localhost:8001/openapi.json>
- Health check: <http://localhost:8001/health>

All endpoints are mounted under `/api/v1`.

---

## Endpoints

### BGM — `modules/bgm_manager.py`
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/bgm` | List audio files in `assets/bgm/` |
| POST | `/api/v1/bgm` | Upload (multipart form field `file`) |
| GET | `/api/v1/bgm/{name}/metadata` | Name/size/duration/bpm |
| POST | `/api/v1/bgm/{name}/analyze` | Re-run librosa analysis |
| DELETE | `/api/v1/bgm/{name}` | Delete |

### Prompts — `modules/prompt_expander.py`
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/prompts/expand` | ARK Doubao prompt expansion (`song` / `artist` / `n` / `style`) |

Requires `ARK_API_KEY` (and `ARK_TEXT_ENDPOINT`) in `.env`.

### Jobs — `job_state.JobState`
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Create (see `JobCreateRequest` in `schemas.py`) |
| GET | `/api/v1/jobs` | List summaries, newest first |
| GET | `/api/v1/jobs/{id}` | Full state.json |
| PATCH | `/api/v1/jobs/{id}` | Partial update (status / clips / stages / params) |

### Drafts
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/drafts` | Jobs still in `creating` status |
| POST | `/api/v1/drafts/{id}/approve` | Advance to `generating` |

### Pipeline stages (async)
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs/{id}/generate` | Kick off Seedance clip generation |
| POST | `/api/v1/jobs/{id}/merge` | FFmpeg merge of confirmed clips |
| POST | `/api/v1/jobs/{id}/whisper` | Whisper SRT on the merged video |

All three return **202 Accepted** immediately and run in FastAPI
`BackgroundTasks`. Progress is persisted by the stage functions themselves
to `tmp/jobs/{id}/state.json` — poll `GET /api/v1/jobs/{id}` and watch
`stages.<name>.status` transition pending → running → done/failed.

### Not exposed
- **TikTok upload** — relies on local browser cookies, not API-friendly.
- **Scheduling** — in-memory only in `app.py`, would not survive a restart.

---

## curl example

```bash
# Expand prompts
curl -X POST http://localhost:8001/api/v1/prompts/expand \
  -H "Content-Type: application/json" \
  -d '{"song":"Blinding Lights","artist":"The Weeknd","n":6,"style":"neon retro"}'

# Create job, then trigger generation
JOB_ID=$(curl -s -X POST http://localhost:8001/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"prompts":["a","b","c"],"song":"Demo","artist":"Me","bgm_path":"/abs/path/to.mp3"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['data']['job_id'])")

curl -X POST http://localhost:8001/api/v1/jobs/$JOB_ID/generate
curl http://localhost:8001/api/v1/jobs/$JOB_ID   # poll for progress
```
