# Remotion Auto-Edit API (v2)

Production-grade asynchronous video auto-editing API service designed for autonomous agents (Hermes) over HTTP. 

## Key Architecture Features

1. **Fully Asynchronous pipeline:** All `/v1/jobs` requests respond immediately in `<500ms` with a `task_id` and status `queued`. Renders are executed by background workers.
2. **GPU-First Transcription (Thai Default):** Attempts GPU transcription using the `medium` model (`device="cuda"`, `compute_type="int8_float16"`) on VRAM. If it encounters a CUDA failure or OOM, it automatically falls back to CPU-only transcription using the fast `small` model (`compute_type="int8"`). Language defaults to Thai (`"th"`).
3. **Same-Output Deduplication Check:** Output files are preserved indefinitely. The *only* auto-deletion is when a new job re-renders the exact same target (matching `input_video` and `output_name` paths). The deletion is logged to the SQLite audit log table.
4. **Rich Overlays & Captions:** Burns karaoke-style word-highlighted captions, text overlays (hooks, CTAs), logo watermarks, and Ken Burns auto-zooms into the composition.

---

## Configuration Env Vars

Set these in your environment or `docker-compose.yml`:
* `WHISPER_MODE`: `"gpu"` (force GPU), `"cpu"` (force CPU), or `"auto"` (default: try GPU, fallback to CPU on failure).
* `MAX_CONCURRENT_RENDERS`: Limit parallel Chromium renders (default: `2`).

---

## API Endpoints & Curl Examples

### 1. Health Check
Hermes should poll this endpoint to check queue pressure.
```bash
curl http://localhost:8000/v1/health
# Response:
# {"status":"ok","active_jobs":0,"queued_jobs":0,"completed_jobs":5,"failed_jobs":1}
```

### 2. Submit a Render Job
```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "input_video": "your-raw-video.mp4",
    "output_name": "tiktok_short_001.mp4",
    "mode": "short",
    "clip_ranges": [{"start": 12.4, "end": 28.1}],
    "subtitle_style": "karaoke",
    "overlays": [
      {"type": "text", "content": "WAIT FOR IT", "position": "top", "start": 0, "end": 3, "style": "hook"},
      {"type": "watermark", "asset": "assets/logo.png", "position": "bottom-right"}
    ],
    "trim_silence": true,
    "auto_zoom": true,
    "language": "th"
  }'
# Response:
# {"task_id": "89cf24a6-4447-49d7-897d-cc51db432a21", "status": "queued"}
```

### 3. Poll Job Status
```bash
curl http://localhost:8000/v1/jobs/89cf24a6-4447-49d7-897d-cc51db432a21
# Response:
# {
#   "task_id": "89cf24a6-4447-49d7-897d-cc51db432a21",
#   "status": "completed",
#   "progress_percent": 100,
#   "backend": "gpu",
#   "error": null,
#   "output_path": "/app/output/tiktok_short_001.mp4",
#   "params": {...},
#   "created_at": "2026-06-27T08:49:06Z",
#   "updated_at": "2026-06-27T08:50:12Z"
# }
```
*(Note: If CUDA failed but CPU succeeded, status will be `completed`, `backend` will show `"cpu_fallback"`, and `error` will contain the CUDA traceback).*

### 4. File Exploration
```bash
# List available raw videos in /app/input
curl http://localhost:8000/v1/inputs

# List completed videos in /app/output
curl http://localhost:8000/v1/outputs
```

### 5. Get word-level transcript
```bash
curl http://localhost:8000/v1/inputs/your-raw-video.mp4/transcript
# If processing: {"status":"transcribing","message":"Transcription started..."}
# If ready: {"status":"ready","transcript": {...}}
```

### 6. Auto-Clip Discovery
Given a long video, suggests the top N engagement windows with scoring:
```bash
curl -X POST http://localhost:8000/v1/auto-clips \
  -H "Content-Type: application/json" \
  -d '{"input_video": "long_webinar.mp4", "count": 3}'
```

---

## File Structure & Volume Mounts
* `/app/input` (Read-only): `/home/aduwa/share/videos`
* `/app/output` (Read/Write): `/home/aduwa/share/edited_videos`
* `/app/assets` (Read-only): `/home/aduwa/projects/editing/remotion-blank/assets` (contains `logo.png` etc.)
* `/app/data` (Read/Write persistent): `/home/aduwa/projects/editing/remotion-blank/data` (contains jobs database and model caches)
