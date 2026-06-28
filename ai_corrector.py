"""
AI-based Thai transcript correction module.
After Whisper + PyThaiNLP fixer, this uses Ollama (local LLM)
to correct misheard Thai words and produce clean, meaningful text.

Key features:
- Batch processes segments to minimize LLM calls
- Returns corrected word-level timestamps preserving timing
- Falls back gracefully if Ollama is unavailable
"""

import json
import logging
import os
import re
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://100.119.233.96:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# Rate limit: don't call LLM more than once per N seconds
_last_llm_call = 0
MIN_CALL_INTERVAL = 1.0


def _call_ollama(prompt: str, system: str = "") -> str | None:
    """Call Ollama with a prompt and return the response text."""
    global _last_llm_call
    import time

    # Rate limiting
    now = time.time()
    wait = MIN_CALL_INTERVAL - (now - _last_llm_call)
    if wait > 0:
        time.sleep(wait)
    _last_llm_call = time.time()

    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 512},
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("response", "").strip()

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ConnectionError, TimeoutError) as e:
        logger.warning(f"Ollama call failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"Ollama unexpected error: {e}")
        return None


def _is_ollama_available() -> bool:
    """Quick check if Ollama is reachable."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def correct_transcript(transcript: dict) -> dict:
    """
    Main entry point: correct Thai transcription errors using AI.
    
    Pipeline:
    1. Check Ollama availability
    2. Group segments into batches (max 3 segments per call)
    3. For each batch, send to Ollama for correction
    4. Map corrected words back to original timestamps
    5. Return updated transcript
    
    Args:
        transcript: Full transcript dict with 'segments' containing 'text' and 'words'
        
    Returns:
        Corrected transcript dict (preserves all structure, only fixes text/words)
    """
    if not transcript or not transcript.get("segments"):
        return transcript

    if not _is_ollama_available():
        logger.warning("Ollama not available at %s, skipping AI correction", OLLAMA_URL)
        return transcript

    logger.info("Ollama available at %s, starting AI correction...", OLLAMA_URL)

    segments = transcript["segments"]
    total_original = sum(len(s.get("words", [])) for s in segments)
    
    # Group segments into batches to minimize LLM calls
    BATCH_SIZE = 3
    corrected_count = 0

    for batch_start in range(0, len(segments), BATCH_SIZE):
        batch = segments[batch_start:batch_start + BATCH_SIZE]

        # Build the raw text for this batch
        raw_texts = [s.get("text", "") for s in batch]
        raw_combined = "\n".join(f"[{i}] {t}" for i, t in enumerate(raw_texts))

        # Only process if there's actual Thai text
        if not re.search(r"[\u0E00-\u0E7F]", raw_combined):
            continue

        system_prompt = (
            "You are a Thai language transcription corrector. "
            "Your task: fix misheard words in Thai speech-to-text output. "
            "Rules:\n"
            "1. Fix only obvious transcription errors (wrong words, missing words)\n"
            "2. Keep filler words like เอ่อ, อ่า, คือ, แบบ as-is (these are natural speech)\n"
            "3. DO NOT change sentence structure, just fix individual words\n"
            "4. Preserve line numbering exactly: [0], [1], [2] etc.\n"
            "5. Respond with ONLY the corrected lines, nothing else\n"
            "6. If a line is already perfect, keep it exactly as-is"
        )

        user_prompt = (
            "Fix transcription errors in these Thai text lines. "
            "Keep line numbering. Only fix obvious misheard words:\n\n"
            f"{raw_combined}"
        )

        result = _call_ollama(user_prompt, system_prompt)
        if not result:
            logger.info("Skipping batch %d (LLM returned nothing)", batch_start // BATCH_SIZE)
            continue

        # Parse corrected lines back
        corrected_lines = {}
        for line in result.strip().split("\n"):
            line = line.strip()
            m = re.match(r"^\[(\d+)\]\s*(.*)", line)
            if m:
                idx = int(m.group(1))
                corrected_text = m.group(2).strip()
                if idx < len(batch) and corrected_text:
                    corrected_lines[idx] = corrected_text

        # Apply corrections
        for local_idx, corrected_text in corrected_lines.items():
            segment = batch[local_idx]
            original_text = segment.get("text", "")
            if corrected_text == original_text:
                continue  # No change needed

            # Update segment text
            segment["text"] = corrected_text

            # Re-map word-level timestamps: preserve timing but update word text
            # Strategy: keep original words, but if LLM changed them, update word text
            original_words = segment.get("words", [])
            if original_words:
                # Join original word texts to compare
                orig_word_text = "".join(w.get("word", "") for w in original_words)
                # Remove spaces from both for comparison
                orig_clean = re.sub(r"\s+", "", orig_word_text)
                corr_clean = re.sub(r"\s+", "", corrected_text)

                if orig_clean != corr_clean:
                    # Words changed — distribute corrected text across same timestamps
                    total_chars = len(corr_clean)
                    total_duration = original_words[-1]["end"] - original_words[0]["start"] if len(original_words) > 1 else 1.0

                    corrected_words = []
                    char_idx = 0
                    for w in original_words:
                        if char_idx >= total_chars:
                            break
                        # Calculate how many characters of corrected text this word gets
                        word_duration = w["end"] - w["start"]
                        char_fraction = word_duration / total_duration if total_duration > 0 else 1.0 / len(original_words)
                        word_chars = max(1, int(char_fraction * total_chars))

                        new_word_text = corr_clean[char_idx:char_idx + word_chars]
                        if new_word_text:
                            corrected_words.append({
                                "word": new_word_text,
                                "start": w["start"],
                                "end": w["end"],
                            })
                            char_idx += word_chars

                    # If we have leftover characters, append to last word
                    if char_idx < total_chars and corrected_words:
                        corrected_words[-1]["word"] += corr_clean[char_idx:]

                    if corrected_words:
                        segment["words"] = corrected_words
                        corrected_count += 1
            else:
                # No word-level timestamps, just update text
                segment["words"] = [{"word": corrected_text, "start": 0, "end": 0}]

    total_final = sum(len(s.get("words", [])) for s in segments)
    logger.info(
        "AI correction: %d segments corrected | words: %d → %d",
        corrected_count, total_original, total_final
    )

    return transcript


def align_text_to_timestamps(original_transcript: dict, corrected_segments: list[str]) -> dict:
    """
    Re-align a list of human-edited segment texts back to the original timestamps.
    Maintains segment durations but distributes words proportionally by character length.
    """
    if not original_transcript or "segments" not in original_transcript:
        return original_transcript

    # Copy transcript to avoid modifying in-place
    import copy
    new_transcript = copy.deepcopy(original_transcript)
    segments = new_transcript["segments"]

    # Ensure length matches or adjust
    num_segs = min(len(segments), len(corrected_segments))

    for idx in range(num_segs):
        segment = segments[idx]
        new_text = corrected_segments[idx].strip()
        
        # If text is identical, skip re-alignment
        if segment.get("text", "").strip() == new_text:
            continue

        segment["text"] = new_text

        # Tokenize segment text
        try:
            from pythainlp.tokenize import word_tokenize as thai_tokenize
            words = [w.strip() for w in thai_tokenize(new_text) if w.strip()]
        except ImportError:
            words = [w.strip() for w in new_text.split() if w.strip()]

        if not words:
            segment["words"] = []
            continue

        start = segment["start"]
        end = segment["end"]
        total_duration = end - start
        total_chars = sum(len(w) for w in words)

        aligned_words = []
        curr_char_idx = 0
        for i, w in enumerate(words):
            w_len = len(w)
            fraction = w_len / total_chars if total_chars > 0 else 1.0 / len(words)
            w_duration = fraction * total_duration
            
            w_start = start + (curr_char_idx / total_chars) * total_duration if total_chars > 0 else start + i * w_duration
            w_end = w_start + w_duration
            
            aligned_words.append({
                "word": w,
                "start": round(w_start, 3),
                "end": round(w_end, 3),
                "is_filler": False
            })
            curr_char_idx += w_len

        segment["words"] = aligned_words

    # Rebuild flat lists of filler words and silence gaps if needed
    all_words_flat = []
    for seg in segments:
        all_words_flat.extend(seg.get("words", []))

    # Recalculate silence gaps
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
    new_transcript["silence_gaps"] = silence_gaps

    return new_transcript


def suggest_overlays(transcript: dict) -> list[dict]:
    """
    Suggest text and SFX overlays for a video transcript using local Ollama LLM.
    """
    if not transcript or not transcript.get("segments"):
        return []
        
    if not _is_ollama_available():
        logger.warning("Ollama not available, returning empty overlay suggestions")
        return []
        
    # Build text representation of segments with timestamps
    segments_text = []
    for seg in transcript["segments"]:
        segments_text.append(f"[{seg['start']:.2f}s - {seg['end']:.2f}s]: {seg['text']}")
    
    transcript_str = "\n".join(segments_text)
    
    system_prompt = (
        "You are an expert video editor. You are given a video subtitle transcript with timestamps.\n"
        "Your task is to suggest visual text overlays and audio sound effects (SFX) to make the video engaging.\n"
        "You have the following sound effects (SFX) available:\n"
        "- assets/sfx/ding.wav (use for key highlights, pops of text, lists)\n"
        "- assets/sfx/whoosh.wav (use for transitions, text animations, slide-ins)\n"
        "- assets/sfx/pop.wav (use for small emphasis, quick words, emojis)\n"
        "- assets/sfx/boom.wav (use for major statements, punchlines, drama)\n\n"
        "You can suggest two types of overlays:\n"
        "1. Text overlays: type='text', style='hook' (for the opening 3 seconds), style='cta' (for the last 3 seconds), or style='default' (for middle punchy words).\n"
        "2. SFX overlays: type='audio', asset='assets/sfx/ding.wav' | 'assets/sfx/whoosh.wav' | 'assets/sfx/pop.wav' | 'assets/sfx/boom.wav', start=timestamp, volume=1.0\n\n"
        "Rules:\n"
        "1. Suggest a few text overlays for key moments (e.g. hook text at start, call to action at end, key words in middle). Keep their content short (1-5 words).\n"
        "2. Suggest SFX overlays at the exact timestamp where the key words are spoken or when text overlays appear.\n"
        "3. Output a valid JSON array of overlay objects, and NOTHING else. No markdown, no explanations, no chat.\n"
        "Format:\n"
        "[\n"
        "  {\"type\": \"text\", \"content\": \"EXCITING HOOK\", \"position\": \"center\", \"start\": 0.5, \"end\": 2.5, \"style\": \"hook\"},\n"
        "  {\"type\": \"audio\", \"asset\": \"assets/sfx/whoosh.wav\", \"start\": 0.5, \"volume\": 0.8},\n"
        "  {\"type\": \"audio\", \"asset\": \"assets/sfx/ding.wav\", \"start\": 4.2, \"volume\": 1.0},\n"
        "  {\"type\": \"text\", \"content\": \"KEY WORD\", \"position\": \"bottom\", \"start\": 4.2, \"end\": 5.5, \"style\": \"default\"},\n"
        "  {\"type\": \"text\", \"content\": \"SUBSCRIBE NOW!\", \"position\": \"bottom\", \"start\": 12.0, \"end\": 14.5, \"style\": \"cta\"},\n"
        "  {\"type\": \"audio\", \"asset\": \"assets/sfx/boom.wav\", \"start\": 12.0, \"volume\": 1.0}\n"
        "]"
    )
    
    user_prompt = (
        "Here is the transcript. Please suggest text and SFX overlays as a JSON array:\n\n"
        f"{transcript_str}"
    )
    
    response = _call_ollama(user_prompt, system_prompt)
    if not response:
        return []
        
    try:
        start_idx = response.find("[")
        end_idx = response.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = response[start_idx:end_idx + 1]
            return json.loads(json_str)
    except Exception as e:
        logger.error(f"Failed to parse Ollama suggested overlays: {e}\nRaw response: {response}")
        
    return []

