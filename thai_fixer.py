"""
Thai text post-processing: fix word segmentation artifacts from Whisper.
Whisper often splits Thai words with spaces where there shouldn't be (e.g. "มาพูดถ  ึง" → "มาพูดถึง").
This module re-joins broken Thai words using PyThaiNLP's tokenizer.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Regex to detect Thai characters
THAI_CHAR = re.compile(r'[\u0E00-\u0E7F]')

# Cache the tokenizer
_thai_word_dict = None
_thai_tokenizer = None


def _get_tokenizer():
    global _thai_tokenizer
    if _thai_tokenizer is None:
        try:
            from pythainlp.tokenize import word_tokenize as thai_tokenize
            _thai_tokenizer = thai_tokenize
            logger.info("PyThaiNLP tokenizer loaded successfully")
        except ImportError:
            logger.warning("PyThaiNLP not installed, Thai text fixing disabled")
            _thai_tokenizer = False
    return _thai_tokenizer


def fix_thai_text(words: list[dict]) -> list[dict]:
    """
    Fix Thai word segmentation in Whisper output.
    
    Strategy:
    1. Group consecutive Thai words
    2. Re-tokenize the grouped text using PyThaiNLP
    3. Map back to original timestamps
    
    Args:
        words: List of word dicts with 'word', 'start', 'end' keys
    
    Returns:
        Fixed list of word dicts with corrected segmentation
    """
    tokenizer = _get_tokenizer()
    if tokenizer is False:
        return words  # Can't fix without PyThaiNLP
    
    if not words:
        return words
    
    # Check if this is Thai text at all
    thai_word_count = sum(1 for w in words if THAI_CHAR.search(w.get('word', '')))
    if thai_word_count < 2:
        return words  # Not enough Thai to need fixing
    
    fixed_words = []
    
    i = 0
    while i < len(words):
        w = words[i]
        word_text = w.get('word', '').strip()
        
        # Check if this word is a Thai fragment that might be broken
        # Look for patterns like short fragments followed by more fragments
        if (THAI_CHAR.search(word_text) and 
            i + 1 < len(words) and 
            THAI_CHAR.search(words[i + 1].get('word', ''))):
            
            # Collect consecutive Thai fragments
            thai_fragments = []
            j = i
            while j < len(words) and THAI_CHAR.search(words[j].get('word', '')):
                thai_fragments.append(words[j])
                j += 1
            
            if len(thai_fragments) > 1:
                # Join all Thai fragments and re-tokenize
                raw_text = ''.join(f['word'].strip() for f in thai_fragments)
                # Remove excess whitespace
                raw_text = re.sub(r'\s+', '', raw_text)
                
                try:
                    # Re-tokenize with PyThaiNLP
                    re_tokenized = tokenizer(raw_text)
                    
                    # Map tokens back to original word timestamps
                    # Distribute time proportionally
                    total_duration = thai_fragments[-1]['end'] - thai_fragments[0]['start']
                    total_chars = len(raw_text)
                    
                    char_idx = 0
                    for tok in re_tokenized:
                        if not tok.strip():
                            continue
                        tok_len = len(tok)
                        if total_chars > 0:
                            tok_start_ratio = char_idx / total_chars
                            tok_end_ratio = (char_idx + tok_len) / total_chars
                        else:
                            tok_start_ratio = 0
                            tok_end_ratio = 1
                        
                        fixed_words.append({
                            'word': tok,
                            'start': round(thai_fragments[0]['start'] + total_duration * tok_start_ratio, 3),
                            'end': round(thai_fragments[0]['start'] + total_duration * tok_end_ratio, 3),
                        })
                        char_idx += tok_len
                except Exception as e:
                    logger.warning(f"Thai re-tokenization failed: {e}, keeping original words")
                    fixed_words.extend(thai_fragments)
                
                i = j
                continue
            else:
                # Only one Thai word, keep as-is
                fixed_words.append({**w, 'word': word_text})
                i += 1
                continue
        
        # Non-Thai or solo Thai word - keep as-is but strip excess spaces
        cleaned_word = re.sub(r'\s+', ' ', word_text).strip()
        fixed_words.append({
            'word': cleaned_word,
            'start': w.get('start', 0),
            'end': w.get('end', 0),
        })
        i += 1
    
    return fixed_words


def fix_transcript(transcript: dict) -> dict:
    """
    Fix Thai segmentation on an entire transcript dict.
    
    Processes all segments' word lists through fix_thai_text().
    Also rebuilds segment-level 'text' from fixed words.
    
    Returns the modified transcript dict.
    """
    if not transcript or not transcript.get("segments"):
        return transcript
    
    total_original = 0
    total_fixed = 0
    
    for segment in transcript["segments"]:
        original_words = segment.get("words", [])
        if not original_words:
            continue
        
        total_original += len(original_words)
        
        # Fix Thai word segmentation
        fixed_words = fix_thai_text(original_words)
        total_fixed += len(fixed_words)
        
        # Update segment with fixed words
        segment["words"] = fixed_words
        
        # Rebuild segment text from fixed words
        segment["text"] = " ".join(w["word"] for w in fixed_words if w.get("word"))
    
    logger.info(f"Thai fixer: {total_original} original words → {total_fixed} fixed words (delta: {total_fixed - total_original})")
    
    return transcript
