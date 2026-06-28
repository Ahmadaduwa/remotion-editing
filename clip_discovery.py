"""
Auto-clip discovery: heuristic scoring for finding high-engagement
clip candidates from a long video's transcript.

Scores candidate segments by:
- Signal word density (superlatives, numbers, strong claims, questions)
- Speech pace delta (excitement = faster speech)
- Information density (unique word ratio)
- Hook quality (starts with question or bold statement)
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Signal words that indicate engaging content
SIGNAL_WORDS_EN = {
    # Superlatives & extremes
    "best", "worst", "most", "least", "biggest", "smallest", "fastest",
    "never", "always", "every", "only", "first", "last", "ultimate",
    # Curiosity triggers
    "secret", "mistake", "wrong", "truth", "lie", "hidden", "actually",
    "surprising", "shocking", "incredible", "insane", "crazy", "wild",
    # Numbers & specifics (handled separately via regex)
    # Strong claims
    "guarantee", "proven", "discovered", "revealed", "changed", "transform",
    "hack", "trick", "strategy", "method", "system", "framework",
    # Emotional
    "love", "hate", "fear", "amazing", "terrible", "beautiful", "ugly",
    "perfect", "impossible", "dangerous", "powerful",
}

SIGNAL_WORDS_TH = {
    "ที่สุด", "เลย", "สุดๆ", "มากๆ", "แรก", "สุดท้าย",
    "ลับ", "ผิด", "จริงๆ", "น่าตกใจ", "บ้ามาก", "เปลี่ยน",
    "เทคนิค", "วิธี", "ระบบ", "ความลับ", "อันตราย",
    "รัก", "เกลียด", "กลัว", "สวย", "สมบูรณ์แบบ",
    "ห้าม", "ต้อง", "เท่านั้น",
}

# Regex for numbers, percentages, dollar amounts
NUMBER_PATTERN = re.compile(r'\b\d+[\d,.]*%?\b|\$\d+')
QUESTION_PATTERN = re.compile(r'\?|ไหม|หรือเปล่า|มั้ย|รึเปล่า')


@dataclass
class ClipCandidate:
    """A scored candidate clip range."""
    start: float
    end: float
    score: float
    hook_sentence: str
    duration: float
    reason: str  # human-readable explanation of why this scored high


def _words_per_minute(words: list[dict], start: float, end: float) -> float:
    """Calculate words-per-minute in a time window."""
    duration = end - start
    if duration <= 0:
        return 0
    count = sum(1 for w in words if start <= w["start"] < end)
    return (count / duration) * 60


def _signal_word_score(text: str) -> float:
    """Score text for signal word density."""
    text_lower = text.lower()
    words_in_text = text_lower.split()
    if not words_in_text:
        return 0

    signal_count = 0
    for w in words_in_text:
        clean = w.strip(".,!?;:'\"()[]")
        if clean in SIGNAL_WORDS_EN or clean in SIGNAL_WORDS_TH:
            signal_count += 1

    # Bonus for numbers
    signal_count += len(NUMBER_PATTERN.findall(text))

    # Bonus for questions
    if QUESTION_PATTERN.search(text):
        signal_count += 2

    return signal_count / max(len(words_in_text), 1)


def _hook_quality(text: str) -> float:
    """Score the opening line for hook potential."""
    score = 0.0
    # Questions make great hooks
    if QUESTION_PATTERN.search(text[:100]):
        score += 0.3
    # Starting with a number
    if re.match(r'^\d', text.strip()):
        score += 0.2
    # Short punchy opening (fewer words = more impact)
    first_sentence_words = text.split()[:10]
    if len(first_sentence_words) <= 8:
        score += 0.1
    # Contains signal words in first 10 words
    first_words = " ".join(first_sentence_words).lower()
    for sw in SIGNAL_WORDS_EN | SIGNAL_WORDS_TH:
        if sw in first_words:
            score += 0.15
            break
    return min(score, 1.0)


def _information_density(words: list[str]) -> float:
    """Ratio of unique words to total words — higher = more information-dense."""
    if not words:
        return 0
    clean = [w.lower().strip(".,!?;:'\"()[]") for w in words]
    clean = [w for w in clean if len(w) > 1]
    if not clean:
        return 0
    return len(set(clean)) / len(clean)


def discover_clips(
    transcript: dict,
    count: int = 5,
    min_duration: float = 15.0,
    max_duration: float = 60.0,
    window_step: float = 5.0,
) -> list[dict]:
    """
    Given a transcript with word-level timestamps, find the top N
    non-overlapping candidate clips ranked by engagement potential.

    Args:
        transcript: Output from transcriber.transcribe_video()
        count: Number of clips to return
        min_duration: Minimum clip duration in seconds
        max_duration: Maximum clip duration in seconds
        window_step: Step size for sliding window in seconds

    Returns:
        List of clip candidates, each with:
        {start, end, score, hook_sentence, duration, reason}
    """
    segments = transcript.get("segments", [])
    if not segments:
        return []

    # Flatten all words with their timestamps
    all_words = []
    for seg in segments:
        for w in seg.get("words", []):
            all_words.append(w)

    if not all_words:
        return []

    total_duration = transcript.get("duration", 0)
    if total_duration <= 0 and all_words:
        total_duration = all_words[-1]["end"]

    # Calculate global average WPM for pace comparison
    global_wpm = _words_per_minute(all_words, 0, total_duration) if total_duration > 0 else 150

    # Sliding window scoring
    candidates = []

    for duration in [min_duration, (min_duration + max_duration) / 2, max_duration]:
        t = 0.0
        while t + duration <= total_duration:
            window_start = t
            window_end = t + duration

            # Get words in this window
            window_words = [w for w in all_words if window_start <= w["start"] < window_end]
            if len(window_words) < 5:  # Skip near-empty windows
                t += window_step
                continue

            # Get segments overlapping this window
            window_text = " ".join(w["word"] for w in window_words)
            word_strings = [w["word"] for w in window_words]

            # --- Scoring ---
            reasons = []

            # 1. Signal word density (0-1, weight: 30%)
            sig_score = _signal_word_score(window_text)
            if sig_score > 0.05:
                reasons.append(f"signal_words={sig_score:.2f}")

            # 2. Speech pace delta (0-1, weight: 25%)
            local_wpm = _words_per_minute(all_words, window_start, window_end)
            pace_ratio = local_wpm / max(global_wpm, 1)
            pace_score = min(max(pace_ratio - 0.8, 0) / 0.8, 1.0)  # Normalized above-average pace
            if pace_score > 0.1:
                reasons.append(f"pace={local_wpm:.0f}wpm(+{(pace_ratio-1)*100:.0f}%)")

            # 3. Information density (0-1, weight: 20%)
            info_score = _information_density(word_strings)
            if info_score > 0.5:
                reasons.append(f"info_density={info_score:.2f}")

            # 4. Hook quality of opening (0-1, weight: 25%)
            # Find the first complete sentence-ish chunk
            first_words = " ".join(w["word"] for w in window_words[:15])
            hook_score = _hook_quality(first_words)
            if hook_score > 0.1:
                reasons.append(f"hook={hook_score:.2f}")

            # Weighted total
            total_score = (
                sig_score * 0.30
                + pace_score * 0.25
                + info_score * 0.20
                + hook_score * 0.25
            )

            # Build hook sentence (first ~10 words)
            hook_sentence = " ".join(w["word"] for w in window_words[:10]).strip()

            candidates.append(ClipCandidate(
                start=round(window_start, 2),
                end=round(window_end, 2),
                score=round(total_score, 4),
                hook_sentence=hook_sentence,
                duration=round(duration, 2),
                reason="; ".join(reasons) if reasons else "baseline",
            ))

            t += window_step

    # Sort by score descending
    candidates.sort(key=lambda c: c.score, reverse=True)

    # Select top N non-overlapping
    selected = []
    for c in candidates:
        if len(selected) >= count:
            break
        # Check overlap with already selected
        overlaps = False
        for s in selected:
            if c.start < s.end and c.end > s.start:
                overlaps = True
                break
        if not overlaps:
            selected.append(c)

    # Sort selected by start time
    selected.sort(key=lambda c: c.start)

    return [
        {
            "start": c.start,
            "end": c.end,
            "score": c.score,
            "hook_sentence": c.hook_sentence,
            "duration": c.duration,
            "reason": c.reason,
        }
        for c in selected
    ]
