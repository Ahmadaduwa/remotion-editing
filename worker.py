"""
Background worker system for async job processing.
Uses asyncio.Semaphore for concurrency limiting and asyncio.Queue for job dispatch.
"""
import asyncio
import json
import logging
import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional

import db
import transcriber
import thai_fixer
import ai_corrector

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RENDERS = int(os.environ.get("MAX_CONCURRENT_RENDERS", "2"))
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/app/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/app"))

# Global state
_job_queue: Optional[asyncio.Queue] = None
_render_semaphore: Optional[asyncio.Semaphore] = None
_workers: list[asyncio.Task] = []


async def init_worker(num_workers: int = 4) -> None:
    """Initialize the job queue and start background workers."""
    global _job_queue, _render_semaphore, _workers

    _job_queue = asyncio.Queue()
    _render_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)

    # Requeue any jobs stuck in 'processing' from previous crash
    requeued = await db.requeue_stuck_jobs()
    if requeued > 0:
        logger.info(f"Requeued {requeued} stuck jobs from previous run")

    # Load queued jobs from DB into the in-memory queue
    queued_jobs = await db.list_jobs_by_status("queued")
    for job in queued_jobs:
        await _job_queue.put(job["task_id"])
        logger.info(f"Restored queued job {job['task_id']}")

    # Start worker coroutines
    for i in range(num_workers):
        task = asyncio.create_task(_worker_loop(i))
        _workers.append(task)
    logger.info(f"Started {num_workers} workers (max {MAX_CONCURRENT_RENDERS} concurrent renders)")


async def enqueue_job(task_id: str) -> None:
    """Add a job to the processing queue."""
    if _job_queue is None:
        raise RuntimeError("Worker system not initialized")
    await _job_queue.put(task_id)


def get_queue_size() -> int:
    """Return current queue size (approximate)."""
    if _job_queue is None:
        return 0
    return _job_queue.qsize()


async def _worker_loop(worker_id: int) -> None:
    """Main worker loop — pulls jobs from queue and processes them."""
    logger.info(f"Worker {worker_id} started")
    while True:
        try:
            task_id = await _job_queue.get()
            logger.info(f"Worker {worker_id} picked up job {task_id}")

            # Acquire semaphore to limit concurrent renders
            async with _render_semaphore:
                await _process_job(task_id)

            _job_queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Worker {worker_id} cancelled")
            break
        except Exception as e:
            logger.error(f"Worker {worker_id} unexpected error: {e}", exc_info=True)
            _job_queue.task_done()


async def _process_job(task_id: str) -> None:
    """Full job pipeline: transcribe → preprocess → dedupe check → render."""
    job = await db.get_job(task_id)
    if job is None:
        logger.error(f"Job {task_id} not found in database")
        return

    try:
        await db.update_job(task_id, status="processing", progress_percent=5)
        params = job.get("params", {})
        input_video = params.get("input_video", "")
        uploads_path = Path("/app/data/uploads") / input_video
        if uploads_path.is_file():
            input_path = str(uploads_path)
        else:
            input_path = str(INPUT_DIR / input_video)
            
        output_path = str(OUTPUT_DIR / params.get("output_name", ""))

        # Validate input exists
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Check for transcript override (from human loop project flow)
        transcript = params.get("transcript_override")
        if transcript:
            logger.info(f"[{task_id}] Using transcript override from human-loop editing pipeline.")
            await db.update_job(
                task_id,
                progress_percent=35,
                backend="human_override"
            )
        else:
            # ── Phase 1: Transcription (5% → 25%) ──
            await db.update_job(task_id, progress_percent=10)
            lang_override = params.get("language", "th")
            transcript = await asyncio.to_thread(
                transcriber.transcribe_video, input_path, language=lang_override
            )
            # Update database with backend and any CUDA diagnostics/errors
            await db.update_job(
                task_id,
                progress_percent=25,
                backend=transcript.get("backend"),
                error_message=transcript.get("error_message"),
            )
            logger.info(
                f"[{task_id}] Transcription complete using {transcript.get('backend')} backend. "
                f"Segments: {len(transcript.get('segments', []))}"
            )

            # ── Phase 1.5: Fix Thai word segmentation (25% → 30%) ──
            lang = params.get("language", "th")
            if lang == "th" or lang == "thai":
                transcript = thai_fixer.fix_transcript(transcript)
                logger.info(f"[{task_id}] Thai text fixing applied")
            await db.update_job(task_id, progress_percent=30)

            # ── Phase 1.6: AI correction of Thai transcription (30% → 35%) ──
            if lang in ("th", "thai"):
                await db.update_job(task_id, progress_percent=32)
                transcript = await asyncio.to_thread(
                    ai_corrector.correct_transcript, transcript
                )
                logger.info(f"[{task_id}] AI correction applied")
            await db.update_job(task_id, progress_percent=35)

        # ── Phase 2: Pre-process clips with ffmpeg (30% → 50%) ──
        clip_ranges = params.get("clip_ranges", [])
        trim_silence = params.get("trim_silence", True)
        mode = params.get("mode", "short")

        # Determine output dimensions
        if mode == "short":
            width, height = 1080, 1920  # 9:16
        else:
            width, height = 1920, 1080  # 16:9

        # Build the pre-processed video (cut + stitch clips, remove silence/fillers)
        with tempfile.TemporaryDirectory(dir=str(PROJECT_DIR)) as tmpdir:
            preprocessed_path = os.path.join(tmpdir, "preprocessed.mp4")
            await asyncio.to_thread(
                _preprocess_video,
                input_path,
                preprocessed_path,
                clip_ranges,
                trim_silence,
                transcript,
                width,
                height,
            )
            await db.update_job(task_id, progress_percent=50)
            logger.info(f"[{task_id}] Pre-processing complete")

            # Copy preprocessed video to public/ for Remotion's staticFile()
            public_dir = PROJECT_DIR / "public"
            public_dir.mkdir(exist_ok=True)
            public_video = public_dir / f"job_{task_id}.mp4"
            shutil.copy2(preprocessed_path, str(public_video))

            # ── Phase 3: Same-output deduplication check (50% → 55%) ──
            await _dedupe_check(task_id, output_path)
            await db.update_job(task_id, progress_percent=55)

            # ── Phase 4: Remotion render (55% → 90%) ──
            # Prepare inputProps for Remotion
            fps = 30
            video_duration = _get_video_duration(str(public_video))
            total_frames = max(1, int(video_duration * fps))

            # Build subtitle data from transcript, adjusted to preprocessed timeline
            subtitles = _build_subtitle_data(transcript, clip_ranges, trim_silence)

            input_props = {
                "src": f"job_{task_id}.mp4",
                "fps": fps,
                "durationInFrames": total_frames,
                "width": width,
                "height": height,
                "mode": mode,
                "subtitleStyle": params.get("subtitle_style", "karaoke"),
                "subtitleStyleSettings": params.get("subtitle_style_settings"),
                "bgmSettings": params.get("bgm_settings"),
                "subtitles": subtitles,
                "overlays": params.get("overlays", []),
                "autoZoom": params.get("auto_zoom", True),
            }

            props_file = os.path.join(tmpdir, "props.json")
            with open(props_file, "w", encoding="utf-8") as f:
                json.dump(input_props, f, ensure_ascii=False)

            # Determine composition ID
            comp_id = "VideoShort" if mode == "short" else "VideoLong"

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Run Remotion render
            render_cmd = [
                "npx", "remotion", "render",
                "src/index.ts",
                comp_id,
                output_path,
                f"--props={props_file}",
                "--codec=h264",
                "--image-format=jpeg",
                "--log=error",
            ]
            logger.info(f"[{task_id}] Running Remotion: {' '.join(render_cmd)}")

            render_result = await asyncio.to_thread(
                subprocess.run,
                render_cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(PROJECT_DIR),
            )

            if render_result.returncode != 0:
                stderr = render_result.stderr[-2000:] if render_result.stderr else "Unknown error"
                raise RuntimeError(f"Remotion render failed: {stderr}")

            await db.update_job(task_id, progress_percent=90)
            logger.info(f"[{task_id}] Remotion render complete")

            # Clean up public video copy
            if public_video.exists():
                public_video.unlink()

        # ── Phase 5: Verify output (90% → 100%) ──
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(f"Output file is empty or missing: {output_path}")

        file_size = os.path.getsize(output_path)
        await db.update_job(
            task_id,
            status="completed",
            progress_percent=100,
            output_path=output_path,
        )
        logger.info(f"[{task_id}] Job completed: {output_path} ({file_size} bytes)")

    except Exception as e:
        error_msg = str(e)[:2000]
        logger.error(f"[{task_id}] Job failed: {error_msg}", exc_info=True)
        await db.update_job(
            task_id,
            status="failed",
            error_message=error_msg,
        )


def _preprocess_video(
    input_path: str,
    output_path: str,
    clip_ranges: list[dict],
    trim_silence: bool,
    transcript: dict,
    width: int,
    height: int,
) -> None:
    """
    Pre-process video: extract clip ranges, trim silence/fillers, scale to target resolution.
    Uses ffmpeg's complex filter graph.
    """
    if not clip_ranges:
        # If no clip ranges specified, use the entire video
        clip_ranges = [{"start": 0, "end": _get_video_duration(input_path)}]

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_files = []

        for i, clip in enumerate(clip_ranges):
            clip_start = clip.get("start", 0)
            clip_end = clip.get("end", clip_start + 30)
            clip_duration = clip_end - clip_start

            clip_path = os.path.join(tmpdir, f"clip_{i:03d}.mp4")

            # Build ffmpeg filter for this clip
            filters = []

            # Scale and pad to target aspect ratio
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
            )

            # Build the trim/remove filter for silence and fillers
            if trim_silence and transcript:
                select_expr = _build_silence_trim_filter(
                    transcript, clip_start, clip_end
                )
                if select_expr:
                    # Use the select filter with setpts to remove gaps
                    filters_str = ",".join(filters)
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", str(clip_start),
                        "-i", input_path,
                        "-t", str(clip_duration),
                        "-vf", f"{filters_str},{select_expr},setpts=N/FRAME_RATE/TB",
                        "-af", f"{select_expr.replace('select=', 'aselect=')},asetpts=N/SR/TB",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart",
                        clip_path,
                    ]
                else:
                    filters_str = ",".join(filters)
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", str(clip_start),
                        "-i", input_path,
                        "-t", str(clip_duration),
                        "-vf", filters_str,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart",
                        clip_path,
                    ]
            else:
                filters_str = ",".join(filters)
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(clip_start),
                    "-i", input_path,
                    "-t", str(clip_duration),
                    "-vf", filters_str,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    clip_path,
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning(f"ffmpeg clip extraction failed for clip {i}: {result.stderr[-500:]}")
                # Try simpler approach without silence trim
                cmd_simple = [
                    "ffmpeg", "-y",
                    "-ss", str(clip_start),
                    "-i", input_path,
                    "-t", str(clip_duration),
                    "-vf", ",".join(filters),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    clip_path,
                ]
                result = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg clip {i} failed: {result.stderr[-500:]}")

            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                clip_files.append(clip_path)

        if not clip_files:
            raise RuntimeError("No clips were successfully extracted")

        if len(clip_files) == 1:
            shutil.copy2(clip_files[0], output_path)
        else:
            # Concatenate clips
            concat_file = os.path.join(tmpdir, "concat.txt")
            with open(concat_file, "w") as f:
                for cf in clip_files:
                    f.write(f"file '{cf}'\n")

            concat_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                "-movflags", "+faststart",
                output_path,
            ]
            result = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-500:]}")


def _build_silence_trim_filter(
    transcript: dict,
    clip_start: float,
    clip_end: float,
) -> Optional[str]:
    """
    Build an ffmpeg select expression that keeps speech segments
    and removes silence gaps > 0.4s and filler words.
    Returns None if no trimming is needed.
    """
    # Collect time ranges to KEEP (speech without fillers)
    keep_ranges = []
    silence_gaps = transcript.get("silence_gaps", [])
    filler_words = transcript.get("filler_words", [])

    # Get all word timestamps in clip range
    words_in_range = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            if w["start"] >= clip_start and w["end"] <= clip_end:
                if not w.get("is_filler", False):
                    words_in_range.append(w)

    if not words_in_range:
        return None

    # Build keep ranges from non-filler words with small padding
    for w in words_in_range:
        rel_start = w["start"] - clip_start
        rel_end = w["end"] - clip_start
        # Merge with previous range if gap is small
        if keep_ranges and rel_start - keep_ranges[-1][1] < 0.15:
            keep_ranges[-1] = (keep_ranges[-1][0], rel_end)
        else:
            keep_ranges.append((max(0, rel_start - 0.05), rel_end + 0.05))

    if not keep_ranges:
        return None

    # Build select expression
    parts = []
    for start, end in keep_ranges:
        parts.append(f"between(t,{start:.3f},{end:.3f})")

    if not parts:
        return None

    return f"select='{'+'.join(parts)}'"


def _build_subtitle_data(
    transcript: dict,
    clip_ranges: list[dict],
    trim_silence: bool,
) -> list[dict]:
    """
    Build subtitle data adjusted to the preprocessed video timeline.
    Maps word timestamps from original video time to output time.
    """
    if not clip_ranges:
        # Use all words as-is
        words = []
        for seg in transcript.get("segments", []):
            for w in seg.get("words", []):
                if not w.get("is_filler", False) or not trim_silence:
                    words.append({
                        "word": w["word"],
                        "start": w["start"],
                        "end": w["end"],
                    })
        return words

    # Map words to output timeline
    output_words = []
    output_offset = 0.0

    for clip in clip_ranges:
        clip_start = clip.get("start", 0)
        clip_end = clip.get("end", clip_start + 30)

        clip_words = []
        for seg in transcript.get("segments", []):
            for w in seg.get("words", []):
                if w["start"] >= clip_start and w["end"] <= clip_end:
                    if not w.get("is_filler", False) or not trim_silence:
                        clip_words.append({
                            "word": w["word"],
                            "start": round(w["start"] - clip_start + output_offset, 3),
                            "end": round(w["end"] - clip_start + output_offset, 3),
                        })

        if clip_words:
            output_words.extend(clip_words)
            # Update offset for next clip
            output_offset = clip_words[-1]["end"] + 0.1

    return output_words


def _get_video_duration(path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 30.0  # fallback
    try:
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 30.0))
    except (json.JSONDecodeError, ValueError):
        return 30.0


async def _dedupe_check(task_id: str, output_path: str) -> None:
    """
    Same-output deduplication: if a prior completed job wrote to the exact
    same output_path, delete the old file and log the deletion.
    Does NOT delete if output_path differs (even for same input video).
    """
    old_job = await db.find_completed_job_by_output(output_path)
    if old_job is None:
        return

    old_task_id = old_job["task_id"]
    if old_task_id == task_id:
        return  # Same job, no-op

    # Only delete if the old output file actually exists
    if os.path.exists(output_path):
        logger.info(
            f"[{task_id}] Dedup: removing old output {output_path} "
            f"(from job {old_task_id})"
        )
        os.remove(output_path)
        await db.log_deletion(
            old_output_path=output_path,
            old_task_id=old_task_id,
            replaced_by_task_id=task_id,
            reason=f"Same output path re-rendered by job {task_id}",
        )
    else:
        logger.info(
            f"[{task_id}] Dedup: old job {old_task_id} had same output_path "
            f"but file no longer exists, skipping deletion"
        )
