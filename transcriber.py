"""
Transcription service using faster-whisper.
Attempts GPU first, then falls back to local CPU transcription.
Provides word-level timestamps with filler word detection and caching.
"""
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TRANSCRIPT_DIR = os.environ.get("TRANSCRIPT_DIR", "/app/data/transcripts")
WHISPER_MODE = os.environ.get("WHISPER_MODE", "gpu").lower()  # gpu | cpu | auto
WHISPER_GPU_MODEL = os.environ.get("WHISPER_GPU_MODEL", "large-v3")
WHISPER_CPU_MODEL = os.environ.get("WHISPER_CPU_MODEL", "large-v3")
WHISPER_MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/app/data/models")

# Filler words to detect and flag
FILLER_WORDS_EN = {"um", "uh", "erm", "like", "you know", "i mean", "sort of", "kind of", "basically", "actually", "literally"}
FILLER_WORDS_TH = {"เอ่อ", "อ่า", "อ้า", "คือ", "แบบ", "ก็คือ", "อะ", "นะ", "ฮะ", "เออ"}

# Singletons for loaded models to avoid reloading
_gpu_model = None
_cpu_model = None


def _get_gpu_model():
    """Load and return the GPU Whisper model."""
    global _gpu_model
    if _gpu_model is None:
        from faster_whisper import WhisperModel
        model_name = WHISPER_GPU_MODEL
        logger.info(f"Loading GPU Whisper model '{model_name}' on cuda (int8_float16)...")
        _gpu_model = WhisperModel(
            model_name,
            device="cuda",
            compute_type="int8_float16",
            download_root=WHISPER_MODEL_DIR,
        )
        logger.info(f"GPU Whisper model '{model_name}' loaded successfully.")
    return _gpu_model


def _get_cpu_model():
    """Load and return the CPU Whisper fallback model."""
    global _cpu_model
    if _cpu_model is None:
        from faster_whisper import WhisperModel
        model_name = WHISPER_CPU_MODEL
        logger.info(f"Loading CPU Whisper model '{model_name}' on cpu (int8)...")
        _cpu_model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            download_root=WHISPER_MODEL_DIR,
        )
        logger.info(f"CPU Whisper model '{model_name}' loaded successfully.")
    return _cpu_model


def _cache_key(video_path: str) -> str:
    """Generate a stable cache key from the video filename."""
    name = os.path.basename(video_path)
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def _cache_path(video_path: str) -> str:
    """Return the filesystem path for a cached transcript."""
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    return os.path.join(TRANSCRIPT_DIR, f"{_cache_key(video_path)}.json")


def get_cached_transcript(video_path: str) -> Optional[dict]:
    """Return the cached transcript if it exists, else None."""
    path = _cache_path(video_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read cached transcript {path}: {e}")
    return None


def _extract_audio(video_path: str, audio_path: str) -> None:
    """Extract audio from video file and apply FFT noise reduction using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",                    # no video
        "-af", "afftdn",          # FFT noise reduction
        "-acodec", "pcm_s16le",   # WAV format for whisper
        "-ar", "16000",           # 16kHz sample rate
        "-ac", "1",               # mono
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction and denoising failed: {result.stderr[-1000:]}")


def _is_filler(word: str) -> bool:
    """Check if a word is a known filler word."""
    w = word.strip().lower()
    return w in FILLER_WORDS_EN or w in FILLER_WORDS_TH


def transcribe_video(video_path: str, language: Optional[str] = "th") -> dict:
    """
    Transcribe a video file, returning word-level timestamps.
    Attempts GPU first (cuda, medium model, int8_float16), falls back to CPU (cpu, small model, int8).
    Results are cached to disk.

    Returns:
        {
            "source": "path/to/video.mp4",
            "language": "th",
            "backend": "gpu" | "cpu_fallback",
            "error_message": null | "exception message",
            "duration": 123.4,
            "segments": [...],
            "filler_words": [...],
            "silence_gaps": [...]
        }
    """
    # Check cache first
    cached = get_cached_transcript(video_path)
    if cached is not None:
        logger.info(f"Using cached transcript for {video_path}")
        return cached

    logger.info(f"Transcribing {video_path} (Whisper Mode: {WHISPER_MODE}, Language: {language or 'auto'})...")

    # Extract audio to temp WAV
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        _extract_audio(video_path, audio_path)

        transcribe_kwargs = {
            "word_timestamps": True,
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 300},
        }
        if language:
            transcribe_kwargs["language"] = language

        model = None
        backend = "gpu"
        cuda_error = None

        # GPU logic
        if WHISPER_MODE in ("gpu", "auto"):
            try:
                model = _get_gpu_model()
                # Run transcription on GPU
                segments_iter, info = model.transcribe(audio_path, **transcribe_kwargs)
                # Force execution of generator inside try block to catch CUDA issues early
                segments_list = list(segments_iter)
            except Exception as e:
                cuda_error = f"CUDA/GPU transcription failed: {e}\n{traceback.format_exc()}"
                logger.error(cuda_error)
                model = None
                backend = "cpu_fallback"

        # CPU fallback logic
        if model is None:
            if WHISPER_MODE == "gpu" and cuda_error:
                # User strictly requested GPU, but it failed
                raise RuntimeError(f"GPU transcription failed and fallback to CPU is disabled: {cuda_error}")
            
            logger.info("Falling back to CPU transcription using 'small' model...")
            model = _get_cpu_model()
            segments_iter, info = model.transcribe(audio_path, **transcribe_kwargs)
            segments_list = list(segments_iter)
            backend = "cpu_fallback"

        segments = []
        all_fillers = []
        all_words_flat = []

        for seg in segments_list:
            words = []
            for w in (seg.words or []):
                is_filler = _is_filler(w.word)
                word_entry = {
                    "word": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "is_filler": is_filler,
                }
                words.append(word_entry)
                all_words_flat.append(word_entry)
                if is_filler:
                    all_fillers.append({
                        "word": w.word.strip(),
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                    })

            segments.append({
                "text": seg.text.strip(),
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "words": words,
            })

        # Detect silence gaps (>0.4s between words)
        silence_gaps = []
        for i in range(1, len(all_words_flat)):
            prev_end = all_words_flat[i - 1]["end"]
            curr_start = all_words_flat[i]["start"]
            gap = curr_start - prev_end
            if gap > 0.4:
                silence_gaps.append({
                    "start": round(prev_end, 3),
                    "end": round(curr_start, 3),
                    "duration": round(gap, 3),
                })

        result = {
            "source": os.path.basename(video_path),
            "language": info.language if info.language else "unknown",
            "language_probability": round(info.language_probability, 3) if info.language_probability else 0,
            "duration": round(info.duration, 3) if info.duration else 0,
            "backend": backend,
            "error_message": cuda_error,  # store CUDA error for diagnostics if we had to fall back
            "segments": segments,
            "filler_words": all_fillers,
            "silence_gaps": silence_gaps,
        }

        # Save to cache
        cache_file = _cache_path(video_path)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"Transcript cached at {cache_file} (using backend: {backend})")

        return result

    finally:
        # Clean up temp audio file
        if os.path.exists(audio_path):
            os.unlink(audio_path)
