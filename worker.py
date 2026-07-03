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


async def shutdown_worker() -> None:
    """Shutdown background workers by cancelling tasks."""
    global _workers
    logger.info("Stopping background workers...")
    for t in _workers:
        t.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
        _workers = []
    logger.info("All background workers stopped.")


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

        # Generate automatic edit plan when the job does not already include one.
        generated_plan = None
        if not params.get("clip_ranges") or not params.get("overlays") or not params.get("bgm_settings"):
            generated_plan = await asyncio.to_thread(
                ai_corrector.orchestrate_video_edit,
                transcript,
                params.get("mode", "short"),
                max(1, len(params.get("clip_ranges", [])) or 3),
            )
            if not params.get("clip_ranges"):
                params["clip_ranges"] = generated_plan.get("clip_ranges", [])
            if not params.get("overlays"):
                params["overlays"] = generated_plan.get("overlays", [])
            if not params.get("bgm_settings"):
                params["bgm_settings"] = generated_plan.get("bgm_settings", {})
            if not params.get("subtitle_style"):
                params["subtitle_style"] = generated_plan.get("subtitle_style", "karaoke")
            if not params.get("subtitle_style_settings"):
                params["subtitle_style_settings"] = generated_plan.get("subtitle_style_settings")
            if params.get("trim_silence") is None:
                params["trim_silence"] = generated_plan.get("trim_silence", True)
            if params.get("auto_zoom") is None:
                params["auto_zoom"] = generated_plan.get("auto_zoom", True)
            await db.update_job(
                task_id,
                progress_percent=36,
            )

        # ── Phase 2: Pre-process clips with ffmpeg (30% → 50%) ──
        clip_ranges = params.get("clip_ranges", []) or (generated_plan or {}).get("clip_ranges", [])
        trim_silence = params.get("trim_silence", True)
        smart_crop = params.get("smart_crop", True)
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
                smart_crop,
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

            # Download any remote HTTP assets in overlays (e.g. from Pixabay)
            overlays_list = params.get("overlays", []) or (generated_plan or {}).get("overlays", [])
            _download_remote_assets(overlays_list)

            bgm_settings = params.get("bgm_settings") or (generated_plan or {}).get("bgm_settings")

            input_props = {
                "src": f"job_{task_id}.mp4",
                "fps": fps,
                "durationInFrames": total_frames,
                "width": width,
                "height": height,
                "mode": mode,
                "subtitleStyle": params.get("subtitle_style", "karaoke"),
                "subtitleStyleSettings": params.get("subtitle_style_settings"),
                "bgmSettings": bgm_settings,
                "subtitles": subtitles,
                "overlays": overlays_list,
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
                "--concurrency=2",
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


def _get_video_resolution(path: str) -> tuple[int, int]:
    """Get video width and height using ffprobe."""
    import subprocess
    import json
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 1920, 1080  # fallback
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return int(stream.get("width")), int(stream.get("height"))
    except Exception:
        pass
    return 1920, 1080


def _build_crop_scale_filter(in_w: int, in_h: int, target_w: int, target_h: int) -> str:
    """Build FFmpeg crop & scale filter to fill the target screen completely."""
    in_aspect = in_w / in_h
    target_aspect = target_w / target_h

    if abs(in_aspect - target_aspect) < 0.05:
        return f"scale={target_w}:{target_h}:flags=lanczos"

    if in_aspect > target_aspect:
        # Input is wider than target
        crop_h = in_h
        crop_w = int(in_h * target_aspect)
        crop_x = (in_w - crop_w) // 2
        crop_y = 0
    else:
        # Input is narrower than target
        crop_w = in_w
        crop_h = int(in_w / target_aspect)
        crop_x = 0
        crop_y = (in_h - crop_h) // 2

    return f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={target_w}:{target_h}:flags=lanczos"


def _build_smart_crop_filter(
    input_path: str,
    clip_start: float,
    clip_duration: float,
    in_w: int,
    in_h: int,
    target_w: int,
    target_h: int,
    tmpdir: str,
    clip_idx: int,
) -> Optional[str]:
    """
    Attempts to build a dynamic face-tracking crop filter using OpenCV Haar Cascades.
    Falls back to None if cv2 is missing or no face is detected.
    """
    try:
        import cv2
    except ImportError:
        logger.info("cv2 not available, using center crop fallback")
        return None

    # If video is already vertical, skip crop
    in_aspect = in_w / in_h
    target_aspect = target_w / target_h
    if abs(in_aspect - target_aspect) < 0.1:
        return None

    # Calculate crop dimensions
    crop_w = int(in_h * target_aspect)
    crop_h = in_h
    center_x = in_w // 2

    # Open video capture
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        return None

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    start_frame = int(clip_start * fps)
    end_frame = int((clip_start + clip_duration) * fps)
    
    # Load OpenCV Haar Cascade for frontal face
    cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
    face_cascade = cv2.CascadeClassifier(cascade_path)
    if face_cascade.empty():
        logger.warning(f"Failed to load Haar Cascade from {cascade_path}")
        cap.release()
        return None

    interval = 10  # check every 10 frames
    keyframes = []  # (frame_offset, face_center_x)
    last_known_x = center_x

    logger.info(f"[{clip_idx}] Running Haar Cascade face tracking from {clip_start:.2f}s to {clip_start+clip_duration:.2f}s (fps={fps})...")

    for f_idx in range(start_frame, min(end_frame, total_frames), interval):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if not ret:
            break

        # Convert to grayscale for detection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Detect faces
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(int(in_h * 0.05), int(in_h * 0.05)) # min face size 5% of height
        )

        if len(faces) > 0:
            # Largest face by area (w * h)
            largest_face = max(faces, key=lambda f: f[2] * f[3])
            fx, fy, fw, fh = largest_face
            face_x = int(fx + fw // 2)
            last_known_x = face_x
            keyframes.append((f_idx - start_frame, face_x))
        else:
            # Persist last known position to prevent sudden snaps back to center
            keyframes.append((f_idx - start_frame, last_known_x))

    cap.release()

    if not keyframes:
        logger.info(f"[{clip_idx}] No frames processed for face tracking.")
        return None

    # Check if face was ever detected away from center
    face_detected_anywhere = any(x != center_x for _, x in keyframes)
    logger.info(f"[{clip_idx}] Face tracked. Detected anywhere: {face_detected_anywhere}")
    
    if not face_detected_anywhere:
        logger.info(f"[{clip_idx}] Face always centered or none detected. Falling back to center crop.")
        return None

    # Smooth positions using a gentle, slow low-pass EMA filter (alpha=0.06)
    alpha = 0.06
    smoothed = [keyframes[0][1]]
    for _, x in keyframes[1:]:
        prev_smoothed = smoothed[-1]
        val = int((1.0 - alpha) * prev_smoothed + alpha * x)
        smoothed.append(val)

    # Write sendcmd script
    sendcmd_lines = []
    for (f_offset, _), smooth_x in zip(keyframes, smoothed):
        ts = f_offset / fps
        target_crop_x = smooth_x - crop_w // 2
        final_crop_x = max(0, min(in_w - crop_w, target_crop_x))
        sendcmd_lines.append(f"{ts:.3f} crop x {final_crop_x};")

    sendcmd_path = os.path.join(tmpdir, f"sendcmd_clip_{clip_idx:03d}.txt")
    with open(sendcmd_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sendcmd_lines))

    logger.info(f"[{clip_idx}] Generated sendcmd crop script at {sendcmd_path} with {len(sendcmd_lines)} instructions.")

    cmd_path_escaped = sendcmd_path.replace("\\", "/").replace(":", "\\:")
    initial_crop_x = max(0, min(in_w - crop_w, smoothed[0] - crop_w // 2))

    return (
        f"sendcmd=f='{cmd_path_escaped}',"
        f"crop={crop_w}:{crop_h}:x='if(eq(n\\,0)\\,{initial_crop_x}\\,x)':y=0,"
        f"scale={target_w}:{target_h}:flags=lanczos"
    )


def _preprocess_video(
    input_path: str,
    output_path: str,
    clip_ranges: list[dict],
    trim_silence: bool,
    transcript: dict,
    width: int,
    height: int,
    smart_crop: bool = True,
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

            # Determine crop/scale filters dynamically (center crop vs smart crop)
            in_w, in_h = _get_video_resolution(input_path)
            crop_filter = None
            if smart_crop and width == 1080 and height == 1920:  # Portrait mode
                crop_filter = _build_smart_crop_filter(
                    input_path=input_path,
                    clip_start=clip_start,
                    clip_duration=clip_duration,
                    in_w=in_w,
                    in_h=in_h,
                    target_w=width,
                    target_h=height,
                    tmpdir=tmpdir,
                    clip_idx=i,
                )
            
            if crop_filter:
                filters.append(crop_filter)
            else:
                filters.append(_build_crop_scale_filter(in_w, in_h, width, height))

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


def _get_keep_ranges(transcript: dict, clip_start: float, clip_end: float) -> list[tuple[float, float]]:
    keep_ranges = []
    if not transcript:
        return keep_ranges

    # Get all non-filler word timestamps in clip range
    words_in_range = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            if w["start"] >= clip_start and w["end"] <= clip_end:
                if not w.get("is_filler", False):
                    words_in_range.append(w)

    if not words_in_range:
        return keep_ranges

    # Build keep ranges from non-filler words with small padding
    for w in words_in_range:
        rel_start = w["start"] - clip_start
        rel_end = w["end"] - clip_start
        # Merge with previous range if gap is small
        if keep_ranges and rel_start - keep_ranges[-1][1] < 0.15:
            keep_ranges[-1] = (keep_ranges[-1][0], rel_end)
        else:
            keep_ranges.append((max(0, rel_start - 0.05), rel_end + 0.05))
            
    return keep_ranges


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
        clip_ranges = [{"start": 0, "end": transcript.get("duration", 30.0)}]

    # Map words to output timeline
    output_words = []
    output_offset = 0.0

    for clip in clip_ranges:
        clip_start = clip.get("start", 0)
        clip_end = clip.get("end", clip_start + 30)

        keep_ranges = []
        if trim_silence:
            keep_ranges = _get_keep_ranges(transcript, clip_start, clip_end)

        clip_words = []
        if trim_silence and keep_ranges:
            # Map words using keep_ranges
            for seg in transcript.get("segments", []):
                for w in seg.get("words", []):
                    if w["start"] >= clip_start and w["end"] <= clip_end:
                        if w.get("is_filler", False):
                            continue
                        
                        # Find which keep range contains this word's center
                        w_center = (w["start"] + w["end"]) / 2.0 - clip_start
                        found_range_idx = -1
                        for idx, (ks, ke) in enumerate(keep_ranges):
                            if ks <= w_center <= ke:
                                found_range_idx = idx
                                break
                        
                        if found_range_idx != -1:
                            ks, ke = keep_ranges[found_range_idx]
                            prev_durs = sum(r[1] - r[0] for r in keep_ranges[:found_range_idx])
                            
                            new_start = prev_durs + (w["start"] - clip_start - ks)
                            new_end = prev_durs + (w["end"] - clip_start - ks)
                            
                            clip_words.append({
                                "word": w["word"],
                                "start": round(new_start + output_offset, 3),
                                "end": round(new_end + output_offset, 3),
                            })
            
            # Increment offset by the total duration of the kept ranges for this clip
            clip_duration = sum(r[1] - r[0] for r in keep_ranges)
            output_offset += clip_duration + 0.1
        else:
            # No trimming, or keep_ranges is empty (no trimming was actually applied)
            for seg in transcript.get("segments", []):
                for w in seg.get("words", []):
                    if w["start"] >= clip_start and w["end"] <= clip_end:
                        if not w.get("is_filler", False) or not trim_silence:
                            clip_words.append({
                                "word": w["word"],
                                "start": round(w["start"] - clip_start + output_offset, 3),
                                "end": round(w["end"] - clip_start + output_offset, 3),
                            })
            
            # Increment offset by the original clip duration
            output_offset += (clip_end - clip_start) + 0.1

        if clip_words:
            output_words.extend(clip_words)

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


def _download_remote_assets(overlays: list[dict]) -> None:
    """Downloads remote HTTP assets in overlays to local public/assets/visuals/ folder."""
    import urllib.request
    import os
    import hashlib
    from pathlib import Path

    visuals_dir = PROJECT_DIR / "public" / "assets" / "visuals"
    visuals_dir.mkdir(parents=True, exist_ok=True)

    for o in overlays:
        asset = o.get("asset", "")
        if asset.startswith("http://") or asset.startswith("https://"):
            # Determine extension
            ext = ".jpg"
            if ".png" in asset.lower(): ext = ".png"
            elif ".mp4" in asset.lower(): ext = ".mp4"
            elif ".gif" in asset.lower(): ext = ".gif"
            elif ".webp" in asset.lower(): ext = ".webp"

            # Clean filename from URL
            h = hashlib.md5(asset.encode()).hexdigest()[:12]
            local_filename = f"pixabay_{h}{ext}"
            local_path = visuals_dir / local_filename

            if not local_path.exists():
                try:
                    logger.info(f"Downloading remote asset {asset} to {local_path}")
                    # Download using urllib with user-agent to prevent blocks
                    req = urllib.request.Request(
                        asset,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                    )
                    with urllib.request.urlopen(req, timeout=15) as response:
                        with open(local_path, "wb") as f:
                            f.write(response.read())
                except Exception as e:
                    logger.error(f"Failed to download remote asset {asset}: {e}")
                    continue

            # Update asset path in overlay object to be relative to public/
            o["asset"] = f"assets/visuals/{local_filename}"

