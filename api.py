import asyncio
import json
import logging
import logging.handlers
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import shutil

import db
import worker
import transcriber
import clip_discovery
import thai_fixer
import ai_corrector

# ─── Logging setup ───
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/app"))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(PROJECT_DIR / "data" / "logs")))
LOG_DIR.mkdir(exist_ok=True, parents=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("api")

# Rotate logs at 10MB, keep 5 backups
file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "api.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
)
logging.getLogger().addHandler(file_handler)

# ─── Paths ───
INPUT_DIR = Path(os.environ.get("INPUT_DIR", str(PROJECT_DIR / "input")))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(PROJECT_DIR / "output")))
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", str(PROJECT_DIR / "data" / "uploads")))
UPLOADS_DIR.mkdir(exist_ok=True, parents=True)
PUBLIC_DIR = Path(os.environ.get("PUBLIC_DIR", str(PROJECT_DIR / "public")))


# ─── Pydantic Models ───

class ClipRange(BaseModel):
    start: float
    end: float


class Overlay(BaseModel):
    type: str = "text"                  # "text" | "watermark"
    content: str = ""
    asset: str = ""
    position: str = "top"               # "top"|"center"|"bottom"|"bottom-right"
    start: float = 0
    end: float = -1                     # -1 = entire duration
    style: str = "default"              # "hook"|"cta"|"default"
    volume: float = 1.0
    width: str = "50%"


class JobRequest(BaseModel):
    input_video: str
    output_name: str
    mode: str = "short"                 # "short" (9:16) | "long" (16:9)
    clip_ranges: list[ClipRange] = Field(default_factory=list)
    subtitle_style: str = "karaoke"     # "karaoke"|"static"|"none"
    overlays: list[Overlay] = Field(default_factory=list)
    trim_silence: bool = True
    auto_zoom: bool = True
    language: Optional[str] = "th"      # optional transcription language, default "th"
    bgm_settings: Optional[dict] = None
    subtitle_style_settings: Optional[dict] = None
    auto_edit: bool = True


class AutoClipRequest(BaseModel):
    input_video: str
    count: int = 5
    min_duration: float = 15.0
    max_duration: float = 60.0


# ─── App lifecycle ───

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB and worker system on startup."""
    await db.init_db()
    logger.info("Database initialized")
    
    # Ensure public/assets is symlinked to /app/assets so staticFile() works
    public_dir = PUBLIC_DIR
    public_dir.mkdir(exist_ok=True)
    public_assets = public_dir / "assets"
    if not public_assets.exists():
        try:
            os.symlink(str(Path(os.environ.get("ASSET_ROOT", str(PROJECT_DIR / "assets")))), str(public_assets))
            logger.info("Symlinked /app/assets to public/assets")
        except Exception as e:
            logger.error(f"Failed to symlink assets: {e}")

    await worker.init_worker(num_workers=4)
    logger.info("Worker system started")
    yield
    logger.info("Shutting down workers and database connection...")
    await worker.shutdown_worker()
    await db.close_db()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Remotion Auto-Edit API",
    description="Async video editing pipeline for Hermes Agent",
    version="2.0.0",
    lifespan=lifespan,
)

# Enable CORS for all routes (production best practice)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───

@app.get("/v1/health")
async def health():
    """Health check with job queue status for Hermes to poll."""
    counts = await db.count_jobs_by_status()
    return {
        "status": "ok",
        "active_jobs": counts.get("processing", 0),
        "queued_jobs": counts.get("queued", 0),
        "completed_jobs": counts.get("completed", 0),
        "failed_jobs": counts.get("failed", 0),
    }


# ─── Jobs ───

@app.post("/v1/jobs")
async def create_job(req: JobRequest):
    """
    Submit a render job. Returns immediately with task_id and status='queued'.
    Hermes should poll GET /v1/jobs/{task_id} for progress.
    """
    # Validate input file exists
    input_path = INPUT_DIR / req.input_video
    if not input_path.exists():
        raise HTTPException(404, f"Input file not found: {req.input_video}")
    if not input_path.is_file():
        raise HTTPException(400, f"Not a file: {req.input_video}")

    # Validate mode
    if req.mode not in ("short", "long"):
        raise HTTPException(400, f"Invalid mode: {req.mode}. Must be 'short' or 'long'")

    # Validate subtitle_style
    if req.subtitle_style not in ("karaoke", "static", "none"):
        raise HTTPException(400, f"Invalid subtitle_style: {req.subtitle_style}")

    # Validate clip_ranges only when the caller provides them explicitly.
    if req.clip_ranges:
        for i, cr in enumerate(req.clip_ranges):
            if cr.end <= cr.start:
                raise HTTPException(400, f"clip_ranges[{i}]: end must be > start")
    elif not req.auto_edit:
        raise HTTPException(400, "At least one clip_range is required when auto_edit is disabled")

    # Generate task ID
    task_id = str(uuid.uuid4())
    output_path = str(OUTPUT_DIR / req.output_name)

    # Store job in database
    params = req.model_dump()
    if not params.get("clip_ranges") and req.auto_edit:
        params["clip_ranges"] = []
    await db.create_job(
        task_id=task_id,
        input_path=str(input_path),
        output_path=output_path,
        params=params,
    )

    # Enqueue for background processing
    await worker.enqueue_job(task_id)

    logger.info(f"Job {task_id} queued: {req.input_video} → {req.output_name}")
    return {"task_id": task_id, "status": "queued"}
@app.get("/v1/jobs/{task_id}")
async def get_job(task_id: str):
    """Get job status, progress, and output path."""
    job = await db.get_job(task_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {task_id}")

    return {
        "task_id": job["task_id"],
        "status": job["status"],
        "progress_percent": job["progress_percent"],
        "backend": job.get("backend"),
        "error": job.get("error_message"),
        "output_path": job.get("output_path"),
        "params": job.get("params", {}),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


# ─── File exploration ───
@app.get("/v1/inputs")
async def list_inputs():
    """List all input video files with metadata (size, duration, resolution)."""
    def get_inputs():
        if not INPUT_DIR.exists():
            return []
        files = []
        for f in sorted(INPUT_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
                meta = _get_file_metadata(f)
                files.append(meta)
        return files
    files = await asyncio.to_thread(get_inputs)
    return {"files": files}


@app.get("/v1/outputs")
async def list_outputs():
    """List all rendered output files with metadata."""
    def get_outputs():
        if not OUTPUT_DIR.exists():
            return []
        files = []
        for f in sorted(OUTPUT_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
                meta = _get_file_metadata(f)
                files.append(meta)
        return files
    files = await asyncio.to_thread(get_outputs)
    return {"files": files}


@app.get("/v1/assets")
async def list_assets():
    """List available local assets for AI planning and rendering."""
    def scan_assets():
        asset_root = Path("/app/assets")
        result = {"bgm": [], "sfx": [], "visual": []}

        scan_specs = [
            ("bgm", "bgm", (".mp3", ".wav", ".m4a", ".aac", ".ogg")),
            ("sfx", "sfx", (".mp3", ".wav", ".m4a", ".aac", ".ogg")),
            ("Images", "visual", (".png", ".jpg", ".jpeg", ".webp", ".gif")),
            ("videos", "visual", (".mp4", ".mov", ".webm", ".mkv")),
        ]

        for subdir, key, exts in scan_specs:
            root = asset_root / subdir
            if not root.exists():
                continue
            for file_path in sorted(root.rglob("*")):
                if file_path.is_file() and file_path.suffix.lower() in exts:
                    try:
                        rel = file_path.relative_to(Path("/app"))
                        asset_path = str(rel)
                    except ValueError:
                        asset_path = str(file_path)
                    result[key].append({"name": file_path.stem, "asset": asset_path})
        return result

    return await asyncio.to_thread(scan_assets)
@app.get("/v1/inputs/{filename}/transcript")
async def get_transcript(filename: str):
    """
    Return word-level transcript for an input video.
    If cached, returns immediately. If not cached, triggers transcription
    and returns {status: 'transcribing'} — Hermes should poll.
    """
    input_path = INPUT_DIR / filename
    if not input_path.exists():
        raise HTTPException(404, f"Input file not found: {filename}")

    # Check cache
    cached = transcriber.get_cached_transcript(str(input_path))
    if cached is not None:
        return {"status": "ready", "transcript": cached}

    # Trigger async transcription
    asyncio.create_task(_transcribe_background(str(input_path)))
    return {"status": "transcribing", "message": "Transcription started. Poll this endpoint again."}


async def _transcribe_background(video_path: str):
    """Run transcription in background thread."""
    try:
        await asyncio.to_thread(transcriber.transcribe_video, video_path)
        logger.info(f"Background transcription completed: {video_path}")
    except Exception as e:
        logger.error(f"Background transcription failed: {e}", exc_info=True)


# ─── Auto-clip discovery ───

@app.post("/v1/auto-clips")
async def auto_clips(req: AutoClipRequest):
    """
    Given an input video, discover the top N candidate clip ranges
    scored by engagement potential. Requires transcript (will trigger
    transcription if not cached).
    """
    input_path = INPUT_DIR / req.input_video
    if not input_path.exists():
        raise HTTPException(404, f"Input file not found: {req.input_video}")

    # Get or generate transcript
    transcript = transcriber.get_cached_transcript(str(input_path))
    if transcript is None:
        # Run transcription synchronously for this endpoint
        # since the results are needed immediately
        try:
            transcript = await asyncio.to_thread(
                transcriber.transcribe_video, str(input_path)
            )
        except Exception as e:
            raise HTTPException(500, f"Transcription failed: {str(e)[:500]}")

    # Discover clips
    clips = clip_discovery.discover_clips(
        transcript=transcript,
        count=req.count,
        min_duration=req.min_duration,
        max_duration=req.max_duration,
    )

    return {
        "input_video": req.input_video,
        "total_duration": transcript.get("duration", 0),
        "language": transcript.get("language", "unknown"),
        "candidates": clips,
    }


# ─── Helpers ───

def _get_file_metadata(path: Path) -> dict:
    """Get file metadata including ffprobe info."""
    meta = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
    }

    # Use ffprobe for duration and resolution
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)

            # Duration
            fmt = data.get("format", {})
            duration = fmt.get("duration")
            if duration:
                meta["duration_seconds"] = round(float(duration), 2)

            # Resolution from first video stream
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    meta["width"] = stream.get("width")
                    meta["height"] = stream.get("height")
                    meta["codec"] = stream.get("codec_name")
                    fps_str = stream.get("r_frame_rate", "")
                    if "/" in fps_str:
                        num, den = fps_str.split("/")
                        if int(den) > 0:
                            meta["fps"] = round(int(num) / int(den), 2)
                    break
    except Exception as e:
        logger.warning(f"ffprobe failed for {path}: {e}")

    return meta


# ─── Project Pipeline Endpoints ───

class CreateProjectRequest(BaseModel):
    video_name: str
    source_type: str = "input" # "input" | "upload"


class EditSegment(BaseModel):
    text: str
    start: float
    end: float


class ApplyEditsRequest(BaseModel):
    segments: list[EditSegment]


class ProjectSettingsRequest(BaseModel):
    subtitle_style_settings: Optional[dict] = None
    bgm_settings: Optional[dict] = None


class RenderProjectRequest(BaseModel):
    mode: str = "short"  # "short" (9:16) | "long" (16:9)
    subtitle_style: str = "karaoke"
    subtitle_style_settings: Optional[dict] = None
    bgm_settings: Optional[dict] = None
    overlays: list[Overlay] = Field(default_factory=list)
    clip_ranges: list[ClipRange] = Field(default_factory=list)
    trim_silence: bool = True
    auto_zoom: bool = True
    smart_crop: bool = True


class AutoEditRequest(BaseModel):
    input_video: str
    output_name: Optional[str] = None
    mode: str = "short"
    language: Optional[str] = "th"
    source_type: str = "input"
    clip_count: int = 3


def _overlay_models(values: list[dict] | list[Overlay] | None) -> list[Overlay]:
    if not values:
        return []
    return [value if isinstance(value, Overlay) else Overlay(**value) for value in values]


def _clip_range_models(values: list[dict] | list[ClipRange] | None) -> list[ClipRange]:
    if not values:
        return []
    return [value if isinstance(value, ClipRange) else ClipRange(**value) for value in values]


def _project_job_payload(
    project: dict,
    render_plan: Optional[dict],
    req: Optional[RenderProjectRequest] = None,
    output_name: Optional[str] = None,
) -> dict:
    """Build a render job payload from request fields and AI render plan."""
    payload = {
        "mode": req.mode if req else (render_plan.get("mode") if render_plan else "short"),
        "subtitle_style": req.subtitle_style if req else None,
        "subtitle_style_settings": req.subtitle_style_settings if req else None,
        "bgm_settings": req.bgm_settings if req else None,
        "overlays": [o.model_dump() for o in _overlay_models(req.overlays)] if req else [],
        "clip_ranges": [c.model_dump() for c in _clip_range_models(req.clip_ranges)] if req else [],
        "trim_silence": req.trim_silence if req else None,
        "auto_zoom": req.auto_zoom if req else None,
        "smart_crop": req.smart_crop if req else None,
    }

    if render_plan:
        if not payload["subtitle_style"]:
            payload["subtitle_style"] = render_plan.get("subtitle_style", "karaoke")
        if not payload["subtitle_style_settings"]:
            payload["subtitle_style_settings"] = render_plan.get("subtitle_style_settings")
        if not payload["bgm_settings"]:
            payload["bgm_settings"] = render_plan.get("bgm_settings")
        if not payload["overlays"]:
            payload["overlays"] = render_plan.get("overlays", [])
        if not payload["clip_ranges"]:
            payload["clip_ranges"] = render_plan.get("clip_ranges", [])
        if payload["trim_silence"] is None:
            payload["trim_silence"] = render_plan.get("trim_silence", True)
        if payload["auto_zoom"] is None:
            payload["auto_zoom"] = render_plan.get("auto_zoom", True)

    if not payload["subtitle_style"]:
        payload["subtitle_style"] = "karaoke"
    if payload["subtitle_style_settings"] is None:
        payload["subtitle_style_settings"] = None
    if payload["bgm_settings"] is None:
        payload["bgm_settings"] = None
    if payload["trim_silence"] is None:
        payload["trim_silence"] = True
    if payload["auto_zoom"] is None:
        payload["auto_zoom"] = True
    if payload["smart_crop"] is None:
        payload["smart_crop"] = True
    if output_name is None:
        output_name = f"project_{project['project_id']}_{uuid.uuid4().hex[:8]}.mp4"

    payload["output_name"] = output_name
    payload["input_video"] = project["video_name"]
    return payload


async def _transcribe_and_plan(video_name: str, language: Optional[str], mode: str = "short") -> tuple[dict, dict, dict]:
    """Transcribe a source video and build an AI render plan."""
    path1 = INPUT_DIR / video_name
    path2 = UPLOADS_DIR / video_name
    video_path = str(path1) if path1.exists() else str(path2)

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_name}")

    transcript = await asyncio.to_thread(transcriber.transcribe_video, video_path, language=language or "th")
    fixed_transcript = await asyncio.to_thread(thai_fixer.fix_transcript, transcript)
    orchestrated = await asyncio.to_thread(ai_corrector.orchestrate_transcript, fixed_transcript)
    render_plan = orchestrated.get("render_plan") or {}

    return fixed_transcript, orchestrated, render_plan


async def _queue_render_job(project_id: str, input_path: str, payload: dict) -> tuple[str, str]:
    """Create a render job and enqueue it."""
    task_id = str(uuid.uuid4())
    output_name = payload["output_name"]
    output_path = str(OUTPUT_DIR / output_name)

    await db.create_job(
        task_id=task_id,
        input_path=input_path,
        output_path=output_path,
        params=payload,
    )
    await worker.enqueue_job(task_id)
    logger.info("Queued render job %s for project %s", task_id, project_id)
    return task_id, output_path


@app.post("/v1/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload a video to the writeable uploads folder."""
    if not file.filename:
        raise HTTPException(400, "Missing filename")
        
    ext = Path(file.filename).suffix.lower()
    if ext not in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
        raise HTTPException(400, f"Unsupported file format: {ext}")
        
    filename = f"{uuid.uuid4()}{ext}"
    dest_path = UPLOADS_DIR / filename
    
    def save_file():
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    await asyncio.to_thread(save_file)
        
    meta = await asyncio.to_thread(_get_file_metadata, dest_path)
    meta["source_type"] = "upload"
    return meta


@app.get("/v1/videos")
async def list_videos():
    """List all available videos from both INPUT_DIR and UPLOADS_DIR."""
    def scan_videos():
        videos = []
        if INPUT_DIR.exists():
            for f in sorted(INPUT_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
                    meta = _get_file_metadata(f)
                    meta["source_type"] = "input"
                    videos.append(meta)
                    
        if UPLOADS_DIR.exists():
            for f in sorted(UPLOADS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
                    meta = _get_file_metadata(f)
                    meta["source_type"] = "upload"
                    videos.append(meta)
        return videos
    videos = await asyncio.to_thread(scan_videos)
    return {"videos": videos}


@app.post("/v1/projects")
async def create_project_route(req: CreateProjectRequest):
    """Create a new project."""
    project_id = str(uuid.uuid4())
    proj = await db.create_project(project_id, req.video_name)
    return proj


@app.get("/v1/projects")
async def list_projects_route():
    """List all projects."""
    return await db.list_projects()


@app.get("/v1/projects/{project_id}")
async def get_project_route(project_id: str):
    """Get project by ID."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    return proj


@app.delete("/v1/projects/{project_id}")
async def delete_project_route(project_id: str):
    """Delete a project by ID, including associated files on disk."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")

    deleted_files = []

    def remove_files():
        # 1. Delete rendered output video(s) matching project_id prefix
        if OUTPUT_DIR.exists():
            for f in OUTPUT_DIR.iterdir():
                if f.is_file() and f.name.startswith(f"project_{project_id}_"):
                    f.unlink(missing_ok=True)
                    deleted_files.append(f.name)

        # 2. Delete uploaded source video if applicable
        video_name = proj.get("video_name", "")
        if proj.get("source_type") == "upload" or (UPLOADS_DIR / video_name).exists():
            upload_path = UPLOADS_DIR / video_name
            if upload_path.exists() and upload_path.is_file():
                upload_path.unlink(missing_ok=True)
                deleted_files.append(video_name)

    await asyncio.to_thread(remove_files)

    # 3. Delete from database
    await db.delete_project(project_id)

    logger.info(f"Deleted project {project_id}, files: {deleted_files}")
    return {"status": "deleted", "project_id": project_id, "files_deleted": deleted_files}


@app.post("/v1/projects/{project_id}/transcribe")
async def transcribe_project_route(project_id: str):
    """Start asynchronous Whisper + Thai fix + AI correct pipeline."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
        
    await db.update_project(project_id, status="transcribing")
    asyncio.create_task(_run_project_transcription(project_id, proj["video_name"]))
    return {"status": "transcribing"}


async def _run_project_transcription(project_id: str, video_name: str):
    """Run full transcription & AI fixer in background."""
    try:
        raw_transcript, orchestrated, render_plan = await _transcribe_and_plan(video_name, "th", mode="short")
        aligned_transcript = orchestrated["aligned_transcript"]
        overlays = render_plan.get("overlays", orchestrated.get("overlays", []))

        lines = [seg.get("text", "") for seg in aligned_transcript.get("segments", [])]
        corrected_text = "\n".join(lines)

        await db.update_project(
            project_id,
            status="ready",
            raw_transcript=raw_transcript,
            corrected_text=corrected_text,
            aligned_transcript=aligned_transcript,
            overlays=overlays,
            render_plan=render_plan,
            subtitle_style=render_plan.get("subtitle_style_settings"),
            bgm_settings=render_plan.get("bgm_settings"),
        )
        logger.info("Project %s transcription and orchestration complete.", project_id)
    except Exception as e:
        logger.error(f"Project {project_id} transcription failed: {e}", exc_info=True)
        await db.update_project(project_id, status="failed")


@app.post("/v1/projects/{project_id}/apply-edits")
async def apply_edits_route(project_id: str, req: ApplyEditsRequest):
    """Align edited subtitle strings and timings to the original timestamps."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
        
    raw_transcript = proj.get("raw_transcript")
    if not raw_transcript:
        raise HTTPException(400, "Project does not have a raw transcript to edit.")
        
    # Update raw transcript timings from request
    for idx, s in enumerate(req.segments):
        if idx < len(raw_transcript.get("segments", [])):
            raw_transcript["segments"][idx]["start"] = s.start
            raw_transcript["segments"][idx]["end"] = s.end
            
    corrected_segments = [s.text for s in req.segments]
    aligned = await asyncio.to_thread(ai_corrector.align_text_to_timestamps, raw_transcript, corrected_segments)
    corrected_text = "\n".join(corrected_segments)
    
    await db.update_project(
        project_id,
        corrected_text=corrected_text,
        raw_transcript=raw_transcript,
        aligned_transcript=aligned,
        status="ready"
    )
    return {"status": "ready", "aligned_transcript": aligned}


@app.post("/v1/projects/{project_id}/suggest-overlays")
async def suggest_overlays_route(project_id: str):
    """Generate suggested text and SFX overlays."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
        
    aligned_transcript = proj.get("aligned_transcript")
    if not aligned_transcript:
        raise HTTPException(400, "Project does not have subtitles ready yet.")
        
    overlays = await asyncio.to_thread(ai_corrector.suggest_overlays, aligned_transcript)
    await db.update_project(project_id, overlays=overlays)
    return {"overlays": overlays}


@app.post("/v1/projects/{project_id}/orchestrate")
async def orchestrate_project_route(project_id: str):
    """Trigger unified orchestration pipeline returning spelling corrected subtitles and visual/SFX edit plan."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
        
    # Get raw or aligned transcript to feed the orchestrator
    aligned_transcript = proj.get("raw_transcript") or proj.get("aligned_transcript")
    if not aligned_transcript:
        raise HTTPException(400, "Project does not have transcript ready.")
        
    orchestrated = await asyncio.to_thread(ai_corrector.orchestrate_transcript, aligned_transcript)
    render_plan = orchestrated.get("render_plan") or {}
    
    # Save the updated transcript and overlays to db
    await db.update_project(
        project_id,
        aligned_transcript=orchestrated["aligned_transcript"],
        overlays=orchestrated["overlays"],
        render_plan=render_plan,
        subtitle_style=render_plan.get("subtitle_style_settings"),
        bgm_settings=render_plan.get("bgm_settings"),
        corrected_text="\n".join([seg.get("text", "") for seg in orchestrated["aligned_transcript"].get("segments", [])]),
        status="ready"
    )
    
    return {
        "corrected_subtitles": orchestrated["corrected_subtitles"],
        "edit_plan": orchestrated["edit_plan"],
        "render_plan": render_plan,
    }



@app.get("/v1/projects/{project_id}/sfx")
async def list_sfx_route(project_id: str):
    """List available sound effects."""
    def get_sfx():
        sfx_dir = Path("/app/assets/sfx")
        sfx_files = []
        if sfx_dir.exists():
            for f in sfx_dir.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".wav", ".mp3"):
                    try:
                        rel = f.relative_to(Path("/app"))
                        asset_path = str(rel)
                    except ValueError:
                        asset_path = f"assets/sfx/{f.name}"
                    sfx_files.append({
                        "name": f.stem,
                        "asset": asset_path
                    })
        return sfx_files
    sfx_files = await asyncio.to_thread(get_sfx)
    if not sfx_files:
        sfx_files = [
            {"name": "ding", "asset": "assets/sfx/Whoosh (Soft/ding.mp3"},
            {"name": "whoosh", "asset": "assets/sfx/Whoosh (Soft/Whoosh.mp3"},
            {"name": "pop", "asset": "assets/sfx/Whoosh (Soft/pop.mp3"},
            {"name": "boom", "asset": "assets/sfx/Whoosh (Soft/cinematic_hit.mp3"}
        ]
    return {"sfx": sfx_files}


@app.post("/v1/projects/{project_id}/settings")
async def save_project_settings(project_id: str, req: ProjectSettingsRequest):
    """Save project style and BGM settings."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    await db.update_project(
        project_id,
        subtitle_style=req.subtitle_style_settings,
        bgm_settings=req.bgm_settings
    )
    return {"status": "success"}


@app.post("/v1/projects/{project_id}/render")
async def render_project_route(project_id: str, req: RenderProjectRequest):
    """Submit project render job."""
    proj = await db.get_project(project_id)
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
        
    aligned_transcript = proj.get("aligned_transcript")
    if not aligned_transcript:
        raise HTTPException(400, "Project is not ready for rendering (missing subtitles).")
    render_plan = proj.get("render_plan") or {}
    video_name = proj["video_name"]
    path1 = INPUT_DIR / video_name
    path2 = UPLOADS_DIR / video_name
    video_path = str(path1) if path1.exists() else str(path2)
    output_name = f"project_{project_id}_{uuid.uuid4().hex[:8]}.mp4"

    payload = _project_job_payload(proj, render_plan, req=req, output_name=output_name)
    payload["transcript_override"] = aligned_transcript
    payload["is_uploaded_video"] = path2.exists()

    await db.update_project(
        project_id,
        subtitle_style=payload.get("subtitle_style_settings"),
        bgm_settings=payload.get("bgm_settings"),
        overlays=payload.get("overlays"),
        render_plan=render_plan or None,
    )

    task_id, _ = await _queue_render_job(project_id, video_path, payload | {"input_video": os.path.basename(video_path)})
    return {"task_id": task_id, "status": "queued", "output_name": output_name}


@app.post("/v1/auto-edit")
async def auto_edit_route(req: AutoEditRequest):
    """One-shot input -> subtitle draft -> AI edit plan.

    Rendering is intentionally left for the human-in-loop review step.
    """
    if req.source_type not in ("input", "upload"):
        raise HTTPException(400, f"Invalid source_type: {req.source_type}")

    source_root = INPUT_DIR if req.source_type == "input" else UPLOADS_DIR
    input_path = source_root / req.input_video
    if not input_path.exists():
        raise HTTPException(404, f"Input file not found: {req.input_video}")

    project_id = str(uuid.uuid4())
    await db.create_project(project_id, req.input_video)
    await db.update_project(project_id, status="transcribing")

    raw_transcript, orchestrated, render_plan = await _transcribe_and_plan(req.input_video, req.language, mode=req.mode)
    aligned_transcript = orchestrated["aligned_transcript"]
    overlays = render_plan.get("overlays", orchestrated.get("overlays", []))
    corrected_text = "\n".join(seg.get("text", "") for seg in aligned_transcript.get("segments", []))

    await db.update_project(
        project_id,
        status="ready",
        raw_transcript=raw_transcript,
        corrected_text=corrected_text,
        aligned_transcript=aligned_transcript,
        overlays=overlays,
        render_plan=render_plan,
        subtitle_style=render_plan.get("subtitle_style_settings"),
        bgm_settings=render_plan.get("bgm_settings"),
    )
    return {
        "project_id": project_id,
        "status": "ready",
        "needs_human_review": True,
        "render_plan": render_plan,
        "project": await db.get_project(project_id),
    }


@app.get("/v1/download-asset")
async def download_asset(url: str = ""):
    """
    Download a remote asset (e.g. from Pixabay) to the local public/assets/visuals/ folder.
    Returns the local path so the frontend can use it.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "Invalid or missing URL parameter")
    
    import hashlib
    import urllib.request
    
    visuals_dir = PROJECT_DIR / "public" / "assets" / "visuals"
    visuals_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine extension
    ext = ".jpg"
    if ".png" in url.lower(): ext = ".png"
    elif ".mp4" in url.lower(): ext = ".mp4"
    elif ".gif" in url.lower(): ext = ".gif"
    elif ".webp" in url.lower(): ext = ".webp"
    elif ".jpeg" in url.lower(): ext = ".jpeg"
    
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    local_filename = f"pixabay_{h}{ext}"
    local_path = visuals_dir / local_filename
    
    if not local_path.exists():
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                with open(local_path, "wb") as f:
                    f.write(response.read())
            logger.info(f"Downloaded remote asset {url} → {local_path}")
        except Exception as e:
            raise HTTPException(500, f"Failed to download asset: {str(e)[:500]}")
    
    return {"local_path": f"assets/visuals/{local_filename}", "filename": local_filename}


# ─── Expose folders for browser streaming & playing ───
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
app.mount("/input", StaticFiles(directory=str(INPUT_DIR)), name="input")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# ─── Serve public directory last ───
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
