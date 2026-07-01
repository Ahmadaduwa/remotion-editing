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
LOG_DIR = Path("/app/data/logs")
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
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/app/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
UPLOADS_DIR = Path("/app/data/uploads")
UPLOADS_DIR.mkdir(exist_ok=True, parents=True)


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


class JobRequest(BaseModel):
    input_video: str
    output_name: str
    mode: str = "short"                 # "short" (9:16) | "long" (16:9)
    clip_ranges: list[ClipRange]
    subtitle_style: str = "karaoke"     # "karaoke"|"static"|"none"
    overlays: list[Overlay] = Field(default_factory=list)
    trim_silence: bool = True
    auto_zoom: bool = True
    language: Optional[str] = "th"      # optional transcription language, default "th"


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
    public_dir = Path("/app/public")
    public_dir.mkdir(exist_ok=True)
    public_assets = public_dir / "assets"
    if not public_assets.exists():
        try:
            os.symlink("/app/assets", str(public_assets))
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

    # Validate clip_ranges
    if not req.clip_ranges:
        raise HTTPException(400, "At least one clip_range is required")

    for i, cr in enumerate(req.clip_ranges):
        if cr.end <= cr.start:
            raise HTTPException(400, f"clip_ranges[{i}]: end must be > start")

    # Generate task ID
    task_id = str(uuid.uuid4())
    output_path = str(OUTPUT_DIR / req.output_name)

    # Store job in database
    params = req.model_dump()
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
    overlays: list[Overlay]
    clip_ranges: list[ClipRange]
    trim_silence: bool = True
    auto_zoom: bool = True
    smart_crop: bool = True


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
        path1 = INPUT_DIR / video_name
        path2 = UPLOADS_DIR / video_name
        video_path = str(path1) if path1.exists() else str(path2)
        
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_name}")
            
        # 1. Transcribe
        transcript = await asyncio.to_thread(transcriber.transcribe_video, video_path, language="th")
        
        # 2. Thai Fixer
        transcript = await asyncio.to_thread(thai_fixer.fix_transcript, transcript)
        
        # 3. AI Corrector
        transcript = await asyncio.to_thread(ai_corrector.correct_transcript, transcript)
        
        # Build initial plain text (one segment per line)
        lines = [seg.get("text", "") for seg in transcript.get("segments", [])]
        corrected_text = "\n".join(lines)
        
        await db.update_project(
            project_id,
            status="transcribed",
            raw_transcript=transcript,
            corrected_text=corrected_text,
            aligned_transcript=transcript
        )
        logger.info(f"Project {project_id} transcription and initial correction complete.")
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


@app.get("/v1/projects/{project_id}/sfx")
async def list_sfx_route(project_id: str):
    """List available sound effects."""
    def get_sfx():
        sfx_dir = Path("/app/assets/sfx")
        sfx_files = []
        if sfx_dir.exists():
            for f in sfx_dir.iterdir():
                if f.is_file() and f.suffix.lower() in (".wav", ".mp3"):
                    sfx_files.append({
                        "name": f.stem,
                        "asset": f"assets/sfx/{f.name}"
                    })
        return sfx_files
    sfx_files = await asyncio.to_thread(get_sfx)
    if not sfx_files:
        sfx_files = [
            {"name": "ding", "asset": "assets/sfx/ding.wav"},
            {"name": "whoosh", "asset": "assets/sfx/whoosh.wav"},
            {"name": "pop", "asset": "assets/sfx/pop.wav"},
            {"name": "boom", "asset": "assets/sfx/boom.wav"}
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
        
    video_name = proj["video_name"]
    path1 = INPUT_DIR / video_name
    path2 = UPLOADS_DIR / video_name
    video_path = str(path1) if path1.exists() else str(path2)
    
    task_id = str(uuid.uuid4())
    output_name = f"project_{project_id}_{task_id[:8]}.mp4"
    output_path = str(OUTPUT_DIR / output_name)
    
    # Save the settings to DB first
    await db.update_project(
        project_id,
        subtitle_style=req.subtitle_style_settings,
        bgm_settings=req.bgm_settings
    )
    
    params = {
        "input_video": os.path.basename(video_path),
        "output_name": output_name,
        "mode": req.mode,
        "clip_ranges": [cr.model_dump() for cr in req.clip_ranges],
        "subtitle_style": req.subtitle_style,
        "subtitle_style_settings": req.subtitle_style_settings,
        "bgm_settings": req.bgm_settings,
        "overlays": [o.model_dump() for o in req.overlays],
        "trim_silence": req.trim_silence,
        "auto_zoom": req.auto_zoom,
        "smart_crop": req.smart_crop,
        "transcript_override": aligned_transcript,
        "is_uploaded_video": path2.exists()
    }
    
    await db.create_job(
        task_id=task_id,
        input_path=video_path,
        output_path=output_path,
        params=params
    )
    
    await worker.enqueue_job(task_id)
    return {"task_id": task_id, "status": "queued", "output_name": output_name}


# ─── Expose folders for browser streaming & playing ───
app.mount("/output", StaticFiles(directory="/app/output"), name="output")
app.mount("/input", StaticFiles(directory="/app/input"), name="input")
app.mount("/uploads", StaticFiles(directory="/app/data/uploads"), name="uploads")

# ─── Serve public directory last ───
app.mount("/", StaticFiles(directory="/app/public", html=True), name="public")
