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
import random
import re
from pathlib import Path
import urllib.request
import urllib.error
from typing import Any, Optional

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://100.124.101.126:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:2b")
ASSET_ROOT = Path(os.environ.get("ASSET_ROOT", "/app/assets"))
PROJECT_ROOT = Path(os.environ.get("PROJECT_DIR", "/app"))

# Rate limit: don't call LLM more than once per N seconds
_last_llm_call = 0
MIN_CALL_INTERVAL = 1.0


def _asset_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except Exception:
        try:
            return str(path.relative_to(PROJECT_ROOT))
        except Exception:
            return str(path)


def _scan_assets(subdir: str, exts: tuple[str, ...]) -> list[dict]:
    root = ASSET_ROOT / subdir
    assets: list[dict] = []
    if not root.exists():
        return assets

    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.suffix.lower() in exts:
            assets.append({
                "name": file_path.stem,
                "asset": _asset_path(file_path),
            })
    return assets


def _collect_asset_catalogs() -> dict[str, list[dict]]:
    return {
        "bgm": _scan_assets("bgm", (".mp3", ".wav", ".m4a", ".aac", ".ogg")),
        "sfx": _scan_assets("sfx", (".mp3", ".wav", ".m4a", ".aac", ".ogg")),
        "visual": _scan_assets("Images", (".png", ".jpg", ".jpeg", ".webp", ".gif"))
        + _scan_assets("videos", (".mp4", ".mov", ".webm", ".mkv")),
    }


def _resolve_catalog_asset(value: object, catalog: list[dict], fallback_index: int = 0) -> str:
    if catalog:
        fallback = catalog[min(fallback_index, len(catalog) - 1)]["asset"]
    else:
        fallback = ""

    if isinstance(value, dict):
        for key in ("asset", "path", "file", "name"):
            if value.get(key):
                return _resolve_catalog_asset(value[key], catalog, fallback_index)
        return fallback

    if isinstance(value, str):
        selected = value.strip()
        if not selected:
            return fallback
        for item in catalog:
            asset = item.get("asset", "")
            name = item.get("name", "")
            asset_name = os.path.basename(asset)
            if selected in (asset, name, asset_name):
                return asset
        selected_lower = selected.lower()
        for item in catalog:
            asset = item.get("asset", "")
            name = item.get("name", "")
            asset_name = os.path.basename(asset)
            if selected_lower in asset.lower() or selected_lower in name.lower() or selected_lower in asset_name.lower():
                return asset

    return fallback


def _normalize_overlay_asset(overlay: dict, catalogs: dict[str, list[dict]]) -> dict:
    overlay = dict(overlay)
    overlay_type = overlay.get("type", "text")

    if overlay_type in ("audio", "sfx"):
        overlay["type"] = "audio"
        sfx_catalog = catalogs.get("sfx", [])
        asset = overlay.get("asset") or overlay.get("file")
        if not asset:
            category = overlay.get("category", "")
            category_map = {
                "transition": ["swoosh", "whoosh"],
                "emphasis": ["pop", "digital_click", "ding"],
                "impact": ["cinematic_hit", "rise", "glitch"],
                "contextual": ["keyboard", "paper"],
            }
            selected_keywords = category_map.get(category, [])
            for keyword in selected_keywords:
                match = next((item for item in sfx_catalog if keyword.lower() in item["asset"].lower() or keyword.lower() in item["name"].lower()), None)
                if match:
                    asset = match["asset"]
                    break
        overlay["asset"] = _resolve_catalog_asset(asset, sfx_catalog)
        if not overlay["asset"] and sfx_catalog:
            overlay["asset"] = random.choice(sfx_catalog)["asset"]
        overlay.setdefault("volume", 1.0)
        overlay.pop("category", None)

    elif overlay_type in ("watermark", "image", "video"):
        visual_catalog = catalogs.get("visual", [])
        overlay["asset"] = _resolve_catalog_asset(overlay.get("asset"), visual_catalog)

    return overlay


def _normalize_bgm_settings(bgm_settings: object, catalogs: dict[str, list[dict]]) -> dict:
    bgm_catalog = catalogs.get("bgm", [])
    if isinstance(bgm_settings, dict):
        asset = _resolve_catalog_asset(bgm_settings.get("asset"), bgm_catalog)
        if not asset and bgm_catalog:
            asset = bgm_catalog[0]["asset"]
        volume = bgm_settings.get("volume", 0.22)
        try:
            volume = max(0.0, min(float(volume), 1.0))
        except Exception:
            volume = 0.22
        return {
            "asset": asset,
            "volume": volume,
            "enableDucking": bool(bgm_settings.get("enableDucking", True)),
        }

    if bgm_catalog:
        return {
            "asset": bgm_catalog[0]["asset"],
            "volume": 0.22,
            "enableDucking": True,
        }

    return {
        "asset": "",
        "volume": 0.0,
        "enableDucking": True,
    }


def _scan_asset_library() -> dict[str, list[dict[str, str]]]:
    """Discover local BGM and SFX assets from /app/assets."""
    asset_root = ASSET_ROOT
    library = {"bgm": [], "sfx": []}

    for key, directory in ("bgm", asset_root / "bgm"), ("sfx", asset_root / "sfx"):
        if not directory.exists():
            continue
        for file_path in directory.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}:
                continue
            try:
                rel = file_path.relative_to(Path("/app"))
                asset_path = str(rel)
            except ValueError:
                asset_path = f"assets/{key}/{file_path.name}"
            library[key].append(
                {
                    "name": file_path.stem,
                    "filename": file_path.name,
                    "asset": asset_path,
                }
            )

    return library


def _resolve_asset(asset: str, assets: list[dict[str, str]]) -> str:
    """Resolve a suggested asset name/path to an actual local asset path."""
    if not asset:
        return ""

    normalized = asset.strip().lower()
    basename = os.path.basename(normalized)
    stem = Path(basename).stem

    for item in assets:
        candidates = {
            item["asset"].lower(),
            item["filename"].lower(),
            item["name"].lower(),
            os.path.basename(item["asset"]).lower(),
            Path(item["asset"]).stem.lower(),
        }
        if normalized in candidates or basename in candidates or stem in candidates:
            return item["asset"]

    if asset.startswith("http://") or asset.startswith("https://"):
        return asset

    return asset


def _default_subtitle_style_settings() -> dict[str, Any]:
    return {
        "fontFamily": "Inter",
        "color": "#FFFFFF",
        "highlightColor": "#FFD700",
        "fontSize": 54,
        "animation": "pop",
        "backgroundType": "card",
        "backgroundColor": "rgba(0, 0, 0, 0.68)",
    }


def _fallback_render_plan(transcript: dict, mode: str = "short") -> dict:
    """Deterministic edit plan when Ollama is unavailable."""
    import clip_discovery

    assets = _scan_asset_library()
    clips = clip_discovery.discover_clips(
        transcript=transcript,
        count=3 if mode == "short" else 2,
        min_duration=12.0 if mode == "short" else 20.0,
        max_duration=24.0 if mode == "short" else 60.0,
    )
    overlays = suggest_overlays(transcript)
    bgm_asset = assets["bgm"][0]["asset"] if assets["bgm"] else ""

    if not bgm_asset:
        bgm_asset = "assets/bgm/room_tone.mp3"

    return {
        "mode": mode,
        "subtitle_style": "karaoke",
        "subtitle_style_settings": _default_subtitle_style_settings(),
        "bgm_settings": {
            "asset": bgm_asset,
            "volume": 0.22,
            "enableDucking": True,
        },
        "clip_ranges": [
            {"start": c["start"], "end": c["end"]}
            for c in clips
        ] or [
            {"start": 0.0, "end": min(transcript.get("duration", 30.0), 18.0)}
        ],
        "trim_silence": True,
        "auto_zoom": True,
        "overlays": overlays,
        "edit_style": "short-form-hook",
    }


def _fallback_edit_plan(transcript: dict, clip_count: int = 3, mode: str = "short") -> dict:
    """Compatibility wrapper for the automatic edit-plan generator fallback."""
    render_plan = _fallback_render_plan(transcript, mode=mode)
    render_plan["clip_ranges"] = render_plan.get("clip_ranges", [])[:clip_count]
    render_plan["edit_strategy"] = render_plan.get("edit_style", "heuristic_fallback")
    return render_plan


def _parse_json_payload(response: str) -> Any:
    """Extract the outermost JSON object/array from an Ollama response."""
    if not response:
        return None
    candidates = [("{", "}"), ("[", "]")]
    for open_char, close_char in candidates:
        start_idx = response.find(open_char)
        end_idx = response.rfind(close_char)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            payload = response[start_idx:end_idx + 1]
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


def _normalize_overlay_objects(raw_overlays: list[dict[str, Any]], assets: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    """Resolve overlay asset references to local files."""
    import random

    transition_assets = assets["sfx"] or [
        {"asset": "assets/sfx/Whoosh (Soft/swoosh.mp3", "filename": "swoosh.mp3", "name": "swoosh"}
    ]
    emphasis_assets = assets["sfx"] or transition_assets
    impact_assets = assets["sfx"] or transition_assets
    contextual_assets = assets["sfx"] or transition_assets

    def pick_asset(items: list[dict[str, str]]) -> str:
        return random.choice(items)["asset"] if items else ""

    normalized: list[dict[str, Any]] = []
    for overlay in raw_overlays:
        item = dict(overlay)

        if item.get("type") in ("audio", "sfx"):
            item["type"] = "audio"
            category = item.get("category", "")
            if category == "transition":
                item["asset"] = pick_asset(transition_assets)
            elif category == "emphasis":
                item["asset"] = pick_asset(emphasis_assets)
            elif category == "impact":
                item["asset"] = pick_asset(impact_assets)
            elif category == "contextual":
                item["asset"] = pick_asset(contextual_assets)
            elif item.get("asset"):
                item["asset"] = _resolve_asset(str(item["asset"]), assets["sfx"])
            else:
                item["asset"] = pick_asset(emphasis_assets)
        elif item.get("type") == "watermark":
            item["asset"] = _resolve_asset(str(item.get("asset", "")), assets["sfx"] + assets["bgm"])
        normalized.append(item)

    return normalized


def suggest_render_plan(transcript: dict, mode: str = "short") -> dict:
    """
    Ask Ollama to choose the edit style, clips, subtitles, BGM, SFX, and overlays.
    Falls back to heuristics if Ollama is unavailable or returns invalid JSON.
    """
    if not transcript or not transcript.get("segments"):
        return _fallback_render_plan(transcript or {}, mode=mode)

    assets = _scan_asset_library()
    import clip_discovery

    clip_candidates = clip_discovery.discover_clips(
        transcript=transcript,
        count=5 if mode == "short" else 3,
        min_duration=12.0 if mode == "short" else 20.0,
        max_duration=24.0 if mode == "short" else 60.0,
    )

    if not _is_ollama_available():
        logger.warning("Ollama not available, using heuristic render plan")
        return _fallback_render_plan(transcript, mode=mode)

    segment_lines = []
    for seg in transcript["segments"]:
        segment_lines.append(f"[{seg['start']:.2f}s - {seg['end']:.2f}s] {seg.get('text', '').strip()}")

    system_prompt = (
        "You are a senior short-form video editor.\n"
        "Given a transcript, clip candidates, and available assets, design a complete edit plan.\n"
        "Rules:\n"
        "1. Return ONLY valid JSON.\n"
        "2. Choose a short-form strategy: hook first, remove dead air, keep motion, punchy captions.\n"
        "3. Select 1-4 clip_ranges from the provided candidates.\n"
        "4. Pick one local BGM asset from the provided BGM list.\n"
        "5. Pick SFX only from the provided SFX list.\n"
        "6. Keep text overlays short and timed to strong moments.\n"
        "JSON schema:\n"
        "{\n"
        "  \"mode\": \"short\" | \"long\",\n"
        "  \"subtitle_style\": \"karaoke\" | \"static\" | \"none\",\n"
        "  \"subtitle_style_settings\": {\"fontFamily\": \"Inter\", \"color\": \"#FFFFFF\", \"highlightColor\": \"#FFD700\", \"fontSize\": 54, \"animation\": \"pop\", \"backgroundType\": \"card\", \"backgroundColor\": \"rgba(0, 0, 0, 0.68)\"},\n"
        "  \"bgm_settings\": {\"asset\": \"assets/bgm/room_tone.mp3\", \"volume\": 0.22, \"enableDucking\": true},\n"
        "  \"clip_ranges\": [{\"start\": 0.0, \"end\": 12.0}],\n"
        "  \"trim_silence\": true,\n"
        "  \"auto_zoom\": true,\n"
        "  \"overlays\": [{\"type\": \"text\", \"content\": \"HOOK\", \"position\": \"top\", \"start\": 0, \"end\": 2, \"style\": \"hook\"}]\n"
        "}\n"
    )

    user_prompt = (
        "Transcript:\n"
        f"{chr(10).join(segment_lines)}\n\n"
        "Candidate clips:\n"
        f"{json.dumps(clip_candidates, ensure_ascii=False, indent=2)}\n\n"
        "Available BGM assets:\n"
        f"{json.dumps(assets['bgm'], ensure_ascii=False, indent=2)}\n\n"
        "Available SFX assets:\n"
        f"{json.dumps(assets['sfx'], ensure_ascii=False, indent=2)}"
    )

    response = _call_ollama(user_prompt, system_prompt)
    parsed = _parse_json_payload(response or "")
    if not isinstance(parsed, dict):
        logger.warning("Invalid Ollama render plan, using heuristic fallback")
        return _fallback_render_plan(transcript, mode=mode)

    clip_ranges = []
    for clip in parsed.get("clip_ranges", []):
        try:
            start = float(clip["start"])
            end = float(clip["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            clip_ranges.append({"start": round(start, 2), "end": round(end, 2)})

    if not clip_ranges:
        clip_ranges = [
            {"start": c["start"], "end": c["end"]}
            for c in clip_candidates[:3]
        ]

    bgm_settings = parsed.get("bgm_settings") or {}
    bgm_asset = _resolve_asset(str(bgm_settings.get("asset", "")), assets["bgm"])
    if not bgm_asset:
        bgm_asset = assets["bgm"][0]["asset"] if assets["bgm"] else "assets/bgm/room_tone.mp3"

    subtitle_style_settings = dict(_default_subtitle_style_settings())
    subtitle_style_settings.update(parsed.get("subtitle_style_settings") or {})

    overlays = _normalize_overlay_objects(parsed.get("overlays", []), assets)
    if not overlays:
        overlays = suggest_overlays(transcript)

    return {
        "mode": parsed.get("mode", mode),
        "subtitle_style": parsed.get("subtitle_style", "karaoke"),
        "subtitle_style_settings": subtitle_style_settings,
        "bgm_settings": {
            "asset": bgm_asset,
            "volume": float(bgm_settings.get("volume", 0.22)),
            "enableDucking": bool(bgm_settings.get("enableDucking", True)),
        },
        "clip_ranges": clip_ranges,
        "trim_silence": bool(parsed.get("trim_silence", True)),
        "auto_zoom": bool(parsed.get("auto_zoom", True)),
        "overlays": overlays,
        "edit_style": parsed.get("edit_style", "short-form-hook"),
    }


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

        # Build the raw text for this batch and remove spaces between Thai characters
        raw_texts = [s.get("text", "") for s in batch]
        cleaned_raw_texts = []
        for t in raw_texts:
            t_clean = re.sub(r'([\u0E00-\u0E7F])\s+(?=[\u0E00-\u0E7F0-9])', r'\1', t)
            t_clean = re.sub(r'([0-9])\s+(?=[\u0E00-\u0E7F])', r'\1', t_clean)
            cleaned_raw_texts.append(t_clean)

        raw_combined = "\n".join(f"[{i}] {t}" for i, t in enumerate(cleaned_raw_texts))

        # Only process if there's actual Thai text
        if not re.search(r"[\u0E00-\u0E7F]", raw_combined):
            continue

        system_prompt = (
            "You are an expert Thai speech-to-text transcript corrector.\n"
            "Your task: Correct transcription errors, misheard words, and bad spacing in Thai sentences.\n"
            "Guidelines:\n"
            "1. Correct phonetic spelling mistakes to proper Thai words (e.g., 'พระ เภท ซาว แว่' -> 'ประเภทซอฟต์แวร์', 'เวบ สาย' -> 'เว็บไซต์', 'ขอด' -> 'คอร์ส', 'ยุ่บ' -> 'ยุค', 'ปฏิ วัตร' -> 'ปฏิวัติ').\n"
            "2. Keep the meaning and the sentence flow exactly as spoken. Do not paraphrase or change the speaker's style.\n"
            "3. Keep natural filler words like 'เอ่อ', 'อ่า', 'คือ', 'แบบ' as-is.\n"
            "4. Output sentences in natural Thai formatting (no spaces between Thai words, but keep spaces between English words and numbers).\n"
            "5. Preserve the line numbering format exactly: [0], [1], [2], etc.\n"
            "6. Output ONLY the corrected lines. Do not add any chat, introduction, explanation, or markdown formatting (no ```json or ```text)."
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

            # Clean spaces from corrected text if any returned by LLM
            corrected_text = re.sub(r'([\u0E00-\u0E7F])\s+(?=[\u0E00-\u0E7F0-9])', r'\1', corrected_text)
            corrected_text = re.sub(r'([0-9])\s+(?=[\u0E00-\u0E7F])', r'\1', corrected_text)

            # Update segment text
            segment["text"] = corrected_text

            # Re-map word-level timestamps: preserve timing but update word text
            original_words = segment.get("words", [])
            if original_words:
                try:
                    from pythainlp.tokenize import word_tokenize as thai_tokenize
                    corr_words = [w.strip() for w in thai_tokenize(corrected_text) if w.strip()]
                except ImportError:
                    corr_words = [w.strip() for w in corrected_text.split() if w.strip()]

                if corr_words:
                    start_time = original_words[0]["start"]
                    end_time = original_words[-1]["end"]
                    total_duration = end_time - start_time
                    total_chars = sum(len(w) for w in corr_words)

                    corrected_words = []
                    curr_char_idx = 0
                    
                    FILLER_WORDS_EN = {"um", "uh", "erm", "like", "you know", "i mean", "sort of", "kind of", "basically", "actually", "literally"}
                    FILLER_WORDS_TH = {"เอ่อ", "อ่า", "อ้า", "คือ", "แบบ", "ก็คือ", "อะ", "นะ", "ฮะ", "เออ"}

                    for idx, w_text in enumerate(corr_words):
                        w_len = len(w_text)
                        fraction = w_len / total_chars if total_chars > 0 else 1.0 / len(corr_words)
                        w_duration = fraction * total_duration
                        
                        w_start = start_time + (curr_char_idx / total_chars) * total_duration if total_chars > 0 else start_time + idx * w_duration
                        w_end = w_start + w_duration
                        
                        is_filler = w_text.lower() in FILLER_WORDS_EN or w_text in FILLER_WORDS_TH

                        corrected_words.append({
                            "word": w_text,
                            "start": round(w_start, 3),
                            "end": round(w_end, 3),
                            "is_filler": is_filler
                        })
                        curr_char_idx += w_len

                    segment["words"] = corrected_words
                    corrected_count += 1
            else:
                # No word-level timestamps, just update text
                segment["words"] = [{"word": corrected_text, "start": segment.get("start", 0), "end": segment.get("end", 0), "is_filler": False}]

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

    # Recalculate silence gaps (>=0.75s)
    silence_gaps = []
    for i in range(1, len(all_words_flat)):
        prev_end = all_words_flat[i - 1]["end"]
        curr_start = all_words_flat[i]["start"]
        gap = curr_start - prev_end
        if gap > 0.75:
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
    Categorizes SFX and maps them to actual assets.
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
        "You are a professional video editor. Your task is to analyze subtitles and timestamps to generate an edit plan.\n"
        "You must suggest visual text overlays and audio sound effects (SFX) based on three core principles: 1. Pacing, 2. Emphasis, and 3. Seamless Flow.\n\n"
        "SFX Categories available:\n"
        "- transition (change of topic/scene): swoosh or whoosh\n"
        "- emphasis (keywords, text pop-ups, highlights): pop, digital_click\n"
        "- impact (dramatic shifts/conclusions): cinematic_hit, rise, glitch\n"
        "- contextual (text/graphics insertion): keyboard, paper\n\n"
        "You can suggest two types of overlays:\n"
        "1. Text overlays: type='text', style='hook' (opening 3 seconds), style='cta' (last 3 seconds), or style='default' (middle punchy words).\n"
        "2. SFX overlays: type='audio', category='transition' | 'emphasis' | 'impact' | 'contextual', start=timestamp, volume=1.0\n\n"
        "Rules:\n"
        "1. Suggest a few text overlays for key moments (keep content short, 1-5 words).\n"
        "2. Suggest SFX overlays at the exact timestamp where the key words are spoken or when text overlays appear.\n"
        "3. Output a valid JSON array of overlay objects, and NOTHING else. No markdown, no explanations, no chat.\n"
        "Format:\n"
        "[\n"
        "  {\"type\": \"text\", \"content\": \"EXCITING HOOK\", \"position\": \"center\", \"start\": 0.5, \"end\": 2.5, \"style\": \"hook\"},\n"
        "  {\"type\": \"audio\", \"category\": \"transition\", \"start\": 0.5, \"volume\": 0.8},\n"
        "  {\"type\": \"audio\", \"category\": \"emphasis\", \"start\": 4.2, \"volume\": 1.0},\n"
        "  {\"type\": \"text\", \"content\": \"KEY WORD\", \"position\": \"bottom\", \"start\": 4.2, \"end\": 5.5, \"style\": \"default\"},\n"
        "  {\"type\": \"text\", \"content\": \"SUBSCRIBE NOW!\", \"position\": \"bottom\", \"start\": 12.0, \"end\": 14.5, \"style\": \"cta\"},\n"
        "  {\"type\": \"audio\", \"category\": \"impact\", \"start\": 12.0, \"volume\": 1.0}\n"
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
        parsed = _parse_json_payload(response or "")
        if isinstance(parsed, list):
            assets = _scan_asset_library()
            return _normalize_overlay_objects(parsed, assets)
    except Exception as e:
        logger.error(f"Failed to parse Ollama suggested overlays: {e}\nRaw response: {response}")
        
    return []


def _build_transcript_excerpt(transcript: dict, max_segments: int = 18) -> str:
    segments = transcript.get("segments", [])[:max_segments]
    lines = []
    for seg in segments:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        text = str(seg.get("text", "")).strip()
        if text:
            lines.append(f"[{start:.2f}s - {end:.2f}s] {text}")
    return "\n".join(lines)


def _parse_json_block(text: str):
    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    try:
        return json.loads(text[start_idx:end_idx + 1])
    except Exception:
        return None


def _build_edit_plan_prompt(transcript: dict, clip_candidates: list[dict], catalogs: dict[str, list[dict]], mode: str) -> tuple[str, str]:
    clip_summary = []
    for idx, clip in enumerate(clip_candidates[:8]):
        clip_summary.append(
            f"{idx}. {clip.get('start', 0):.2f}-{clip.get('end', 0):.2f}s score={clip.get('score', 0)} hook={clip.get('hook_sentence', '')}"
        )

    bgm_assets = [item["asset"] for item in catalogs.get("bgm", [])]
    sfx_assets = [item["asset"] for item in catalogs.get("sfx", [])]
    visual_assets = [item["asset"] for item in catalogs.get("visual", [])]
    transcript_excerpt = _build_transcript_excerpt(transcript)

    system_prompt = (
        "You are a professional short-form video editor.\n"
        "Your job is to choose the best cut strategy from subtitle/transcript timing, then generate a concise edit plan.\n"
        "You must output only valid JSON and nothing else.\n"
        "Constraints:\n"
        "- Select only assets from the provided catalogs.\n"
        "- Prefer punchy short-form pacing: strong hook first, tighter midsection, call-to-action at the end if appropriate.\n"
        "- Keep overlays short, bold, and timed to key spoken moments.\n"
        "- BGM should be subtle under speech.\n"
        "- SFX should emphasize transitions, punchlines, or scene changes.\n"
        "JSON schema:\n"
        "{\n"
        "  \"clip_ranges\": [{\"start\": 0.0, \"end\": 12.5}],\n"
        "  \"subtitle_style\": \"karaoke\",\n"
        "  \"subtitle_style_settings\": {\"fontFamily\": \"Kanit\", \"color\": \"#FFFFFF\", \"highlightColor\": \"#FFD54A\", \"fontSize\": 54, \"animation\": \"pop\", \"backgroundType\": \"card\", \"backgroundColor\": \"rgba(0,0,0,0.65)\"},\n"
        "  \"bgm_settings\": {\"asset\": \"assets/bgm/room_tone.mp3\", \"volume\": 0.18, \"enableDucking\": true},\n"
        "  \"overlays\": [\n"
        "    {\"type\": \"text\", \"content\": \"WAIT FOR IT\", \"position\": \"top\", \"start\": 0, \"end\": 2.5, \"style\": \"hook\"},\n"
        "    {\"type\": \"audio\", \"asset\": \"assets/sfx/Whoosh (Soft/swoosh.mp3\", \"start\": 1.2, \"volume\": 0.8}\n"
        "  ],\n"
        "  \"trim_silence\": true,\n"
        "  \"auto_zoom\": true\n"
        "}\n"
        "Use 9:16 pacing for short mode and 16:9 pacing for long mode."
    )

    user_prompt = (
        f"Mode: {mode}\n\n"
        f"Subtitle/transcript excerpt:\n{transcript_excerpt}\n\n"
        f"Candidate clip windows:\n" + "\n".join(clip_summary) + "\n\n"
        f"Available BGM assets:\n" + "\n".join(bgm_assets) + "\n\n"
        f"Available SFX assets:\n" + "\n".join(sfx_assets) + "\n\n"
        f"Available visual assets:\n" + "\n".join(visual_assets) + "\n\n"
        "Choose the edit plan JSON now."
    )

    return system_prompt, user_prompt


def generate_edit_plan(transcript: dict, mode: str = "short", clip_count: int = 3) -> dict:
    """
    Generate an automatic short-form edit plan from transcript and local assets.

    Returns JSON-compatible dict with clip_ranges, overlays, bgm_settings,
    subtitle_style, subtitle_style_settings, trim_silence, and auto_zoom.
    """
    if not transcript:
        return {
            "clip_ranges": [],
            "overlays": [],
            "bgm_settings": {"asset": "", "volume": 0.0, "enableDucking": True},
            "subtitle_style": "karaoke",
            "subtitle_style_settings": None,
            "trim_silence": True,
            "auto_zoom": True,
            "edit_strategy": "empty_input",
        }

    catalogs = _collect_asset_catalogs()
    import clip_discovery

    clip_candidates = clip_discovery.discover_clips(
        transcript=transcript,
        count=max(clip_count, 3),
        min_duration=12.0 if mode == "short" else 24.0,
        max_duration=28.0 if mode == "short" else 90.0,
    )

    if not _is_ollama_available():
        logger.warning("Ollama not available, using heuristic fallback edit plan")
        fallback = _fallback_edit_plan(transcript, clip_count=clip_count, mode=mode)
        fallback["clip_ranges"] = fallback.get("clip_ranges", [])[:clip_count]
        return fallback

    system_prompt, user_prompt = _build_edit_plan_prompt(transcript, clip_candidates, catalogs, mode)
    response = _call_ollama(user_prompt, system_prompt)
    if not response:
        logger.warning("Ollama returned empty response, using heuristic fallback edit plan")
        fallback = _fallback_edit_plan(transcript, clip_count=clip_count, mode=mode)
        fallback["clip_ranges"] = fallback.get("clip_ranges", [])[:clip_count]
        return fallback

    parsed = _parse_json_block(response)
    if not isinstance(parsed, dict):
        logger.warning("Failed to parse Ollama edit plan, using heuristic fallback edit plan")
        fallback = _fallback_edit_plan(transcript, clip_count=clip_count, mode=mode)
        fallback["clip_ranges"] = fallback.get("clip_ranges", [])[:clip_count]
        return fallback

    clip_ranges = []
    for item in parsed.get("clip_ranges", [])[:clip_count]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", 0.0))
        except Exception:
            continue
        if end > start >= 0:
            clip_ranges.append({"start": round(start, 2), "end": round(end, 2)})

    if not clip_ranges:
        clip_ranges = clip_candidates[:clip_count]

    overlays = []
    for overlay in parsed.get("overlays", []):
        if isinstance(overlay, dict):
            overlays.append(_normalize_overlay_asset(overlay, catalogs))

    if not overlays:
        overlays = suggest_overlays(transcript)

    bgm_settings = _normalize_bgm_settings(parsed.get("bgm_settings", {}), catalogs)
    if not bgm_settings.get("asset") and catalogs.get("bgm"):
        bgm_settings["asset"] = catalogs["bgm"][0]["asset"]

    subtitle_style = parsed.get("subtitle_style") or "karaoke"
    subtitle_style_settings = parsed.get("subtitle_style_settings")
    trim_silence = bool(parsed.get("trim_silence", True))
    auto_zoom = bool(parsed.get("auto_zoom", True))

    return {
        "clip_ranges": clip_ranges,
        "overlays": overlays,
        "bgm_settings": bgm_settings,
        "subtitle_style": subtitle_style,
        "subtitle_style_settings": subtitle_style_settings,
        "trim_silence": trim_silence,
        "auto_zoom": auto_zoom,
        "edit_strategy": "ollama",
    }


def orchestrate_video_edit(transcript: dict, mode: str = "short", clip_count: int = 3) -> dict:
    """Produce subtitles plus an automatic edit plan for downstream rendering."""
    corrected_transcript = correct_transcript(transcript)
    edit_plan = generate_edit_plan(corrected_transcript, mode=mode, clip_count=clip_count)

    corrected_subtitles = []
    if corrected_transcript and "segments" in corrected_transcript:
        for seg in corrected_transcript["segments"]:
            for word in seg.get("words", []):
                corrected_subtitles.append({
                    "word": word.get("word", ""),
                    "start": word.get("start", 0.0),
                    "end": word.get("end", 0.0),
                })

    return {
        "corrected_subtitles": corrected_subtitles,
        "aligned_transcript": corrected_transcript,
        "edit_plan": edit_plan,
        "clip_ranges": edit_plan.get("clip_ranges", []),
        "overlays": edit_plan.get("overlays", []),
        "bgm_settings": edit_plan.get("bgm_settings", {}),
        "subtitle_style": edit_plan.get("subtitle_style", "karaoke"),
        "subtitle_style_settings": edit_plan.get("subtitle_style_settings"),
        "trim_silence": edit_plan.get("trim_silence", True),
        "auto_zoom": edit_plan.get("auto_zoom", True),
    }


def orchestrate_transcript(transcript: dict) -> dict:
    """
    Unified Orchestrator:
    1. Correct spelling of transcript segments (AI Corrector).
    2. Suggest overlays (AI Suggest SFX & text highlights).
    3. Consolidate into a unified schema:
       {
         "corrected_subtitles": [...],
         "edit_plan": [...]
       }
    """
    # 1. Run spelling corrector
    corrected_transcript = correct_transcript(transcript)

    # 2. Generate edit plan / overlays / render settings
    render_plan = suggest_render_plan(corrected_transcript)
    overlays = render_plan.get("overlays", [])

    # 3. Build unified schema
    corrected_subtitles = []
    if corrected_transcript and "segments" in corrected_transcript:
        for seg in corrected_transcript["segments"]:
            if "words" in seg:
                for w in seg["words"]:
                    corrected_subtitles.append({
                        "word": w.get("word", ""),
                        "start": w.get("start", 0.0),
                        "end": w.get("end", 0.0)
                    })

    edit_plan = []
    for o in overlays:
        if o.get("type") == "audio":
            edit_plan.append({
                "type": "sfx",
                "file": os.path.basename(o.get("asset", "")),
                "time": o.get("start", 0.0)
            })
        elif o.get("type") in ("text", "overlay"):
            edit_plan.append({
                "type": "overlay",
                "text": o.get("content", ""),
                "start": o.get("start", 0.0),
                "end": o.get("end", 0.0)
            })

    return {
        "corrected_subtitles": corrected_subtitles,
        "edit_plan": edit_plan,
        "overlays": overlays,
        "aligned_transcript": corrected_transcript,
        "render_plan": render_plan,
    }

