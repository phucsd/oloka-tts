"""
OlokaTTS Forced Aligner Service.
Provides millisecond-accurate word-level timestamps and subtitle generation
using Stable-Whisper (faster-whisper engine) for Vietnamese audio.
"""

import os
import re
import time
import json
from typing import List, Dict, Any, Optional, Union
import numpy as np


class ForcedAlignerService:
    _model = None
    _model_name = "base"

    # Regex to clean emotion tags and pause syntax
    EMOTION_TAG_RE = re.compile(r"\[(?:cười|thở dài|hắng giọng|khóc|ngáp|thì thầm|nói to|vui vẻ|buồn|tức giận|ngạc nhiên|hét|nói nhỏ|vỗ tay|ho|thở dốc)\]", re.IGNORECASE)
    PAUSE_TAG_RE = re.compile(r"\[(?:pause:\s*)?[0-9]+(?:\.[0-9]+)?\s*(?:seconds?|sec|s)?\]", re.IGNORECASE)
    CITATION_TAG_RE = re.compile(r"\[\d+\]")

    @classmethod
    def clean_text_for_alignment(cls, text: str) -> str:
        """
        Strips non-spoken bracketed cues (emotion tags, pause tags, citation numbers)
        leaving only actual spoken words and natural punctuation.
        """
        if not text:
            return ""
        
        # Remove known tags
        cleaned = cls.EMOTION_TAG_RE.sub(" ", text)
        cleaned = cls.PAUSE_TAG_RE.sub(" ", cleaned)
        cleaned = cls.CITATION_TAG_RE.sub(" ", cleaned)

        # Remove any remaining standalone bracketed tokens like [pause: ...]
        cleaned = re.sub(r"\[[^\]\r\n]+\]", " ", cleaned)

        # Normalize spaces before punctuation (e.g. "bạn , hôm nay" -> "bạn, hôm nay")
        cleaned = re.sub(r"\s+([,.:;?!])", r"\1", cleaned)

        # Normalize multiple spaces and trim
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @classmethod
    def get_model(cls, model_name: str = "base"):
        """
        Lazily loads the stable-whisper faster-whisper model.
        Cached in memory across requests.
        """
        if cls._model is None or cls._model_name != model_name:
            import stable_whisper
            print(f"[ForcedAligner] Loading faster-whisper '{model_name}' model...")
            try:
                cls._model = stable_whisper.load_faster_whisper(model_name, device="cpu", compute_type="int8")
            except TypeError:
                cls._model = stable_whisper.load_faster_whisper(model_name)
            cls._model_name = model_name
            print(f"[ForcedAligner] Model '{model_name}' loaded successfully!")
        return cls._model

    @classmethod
    def align(
        cls,
        audio: Union[str, np.ndarray, bytes],
        text: str,
        language: str = "vi",
        model_name: str = "base"
    ) -> Dict[str, Any]:
        """
        Performs forced alignment between given audio and Vietnamese text prompt.
        
        Returns:
            {
                "success": bool,
                "text": str,
                "clean_text": str,
                "duration": float,
                "execution_time_ms": float,
                "words_count": int,
                "words": [
                    {
                        "index": int,
                        "word": str,
                        "start": float,
                        "end": float,
                        "duration": float,
                        "probability": float
                    },
                    ...
                ],
                "srt": str,
                "vtt": str
            }
        """
        t0 = time.time()
        clean_text = cls.clean_text_for_alignment(text)
        if not clean_text:
            return {
                "success": False,
                "error": "Văn bản sau khi loại bỏ thẻ tag bị rỗng.",
                "text": text,
                "clean_text": "",
                "duration": 0.0,
                "execution_time_ms": 0.0,
                "words_count": 0,
                "words": [],
                "srt": "",
                "vtt": ""
            }

        model = cls.get_model(model_name=model_name)

        try:
            # Run forced alignment
            # Using language='vi' for Vietnamese speech
            result = model.align(
                audio,
                clean_text,
                language=language,
                word_dur_factor=2.0,
                max_word_dur=3.5,
                nonspeech_skip=5.0
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"Lỗi trong quá trình căn chỉnh âm học: {str(e)}",
                "text": text,
                "clean_text": clean_text,
                "duration": 0.0,
                "execution_time_ms": round((time.time() - t0) * 1000, 2),
                "words_count": 0,
                "words": [],
                "srt": "",
                "vtt": ""
            }

        if not result:
            return {
                "success": False,
                "error": "Không thể căn chỉnh âm thanh với văn bản đã cho.",
                "text": text,
                "clean_text": clean_text,
                "duration": 0.0,
                "execution_time_ms": round((time.time() - t0) * 1000, 2),
                "words_count": 0,
                "words": [],
                "srt": "",
                "vtt": ""
            }

        words_data: List[Dict[str, Any]] = []
        idx = 0
        for seg in result.segments:
            for w in getattr(seg, "words", []):
                w_str = w.word.strip()
                if not w_str:
                    continue
                start_s = round(float(w.start), 3)
                end_s = round(float(w.end), 3)
                # Ensure end is at least start
                if end_s < start_s:
                    end_s = start_s
                dur_s = round(end_s - start_s, 3)
                prob = round(float(getattr(w, "probability", 1.0)), 2)

                words_data.append({
                    "index": idx,
                    "word": w_str,
                    "start": start_s,
                    "end": end_s,
                    "duration": dur_s,
                    "probability": prob
                })
                idx += 1

        total_duration = round(float(getattr(result, "duration", 0.0)), 3)
        words_data = cls.post_process_word_timings(words_data, total_duration)

        if total_duration == 0.0 and words_data:
            total_duration = words_data[-1]["end"]

        exec_ms = round((time.time() - t0) * 1000, 2)

        # Generate subtitles
        srt_content = cls.generate_srt(words_data)
        vtt_content = cls.generate_vtt(words_data)

        return {
            "success": True,
            "text": text,
            "clean_text": clean_text,
            "duration": total_duration,
            "execution_time_ms": exec_ms,
            "words_count": len(words_data),
            "words": words_data,
            "srt": srt_content,
            "vtt": vtt_content
        }

    @classmethod
    def post_process_word_timings(cls, words: List[Dict[str, Any]], audio_duration: float = 0.0) -> List[Dict[str, Any]]:
        """
        Cleans and refines word boundaries:
        - Resolves 0-duration words by distributing shared spans based on character lengths.
        - Ensures strictly non-decreasing timestamps.
        - Guarantees minimum word duration (0.04s) for clean click-to-seek and highlighting.
        """
        if not words:
            return words

        n = len(words)
        i = 0
        while i < n:
            # Check if this word has zero duration or shares start with next word
            if words[i]["end"] <= words[i]["start"] or (i + 1 < n and words[i+1]["start"] <= words[i]["start"]):
                cluster = [i]
                j = i + 1
                while j < n and words[j]["start"] <= words[i]["start"]:
                    cluster.append(j)
                    if words[j]["end"] > words[i]["start"]:
                        break
                    j += 1

                span_start = words[cluster[0]]["start"]
                span_end = max(words[k]["end"] for k in cluster)
                if span_end <= span_start:
                    if j < n:
                        span_end = words[j]["start"]
                    else:
                        span_end = span_start + 0.1 * len(cluster)

                total_span = max(0.06 * len(cluster), span_end - span_start)
                weights = [max(1, len(words[k]["word"])) for k in cluster]
                total_w = sum(weights)
                cur_t = span_start

                for idx_c, k in enumerate(cluster):
                    w_dur = round(total_span * (weights[idx_c] / total_w), 3)
                    words[k]["start"] = round(cur_t, 3)
                    words[k]["end"] = round(cur_t + w_dur, 3)
                    words[k]["duration"] = round(words[k]["end"] - words[k]["start"], 3)
                    cur_t = words[k]["end"]

                i = cluster[-1] + 1
                continue
            i += 1

        # Enforce sanity minimum duration and monotonic progression
        for idx, w in enumerate(words):
            if w["duration"] <= 0.02:
                w["end"] = round(w["start"] + 0.05, 3)
                w["duration"] = 0.05
            if audio_duration > 0 and w["end"] > audio_duration:
                w["end"] = audio_duration
                w["duration"] = round(max(0.02, w["end"] - w["start"]), 3)

        return words

    @staticmethod
    def _format_timestamp(seconds: float, srt: bool = True) -> str:
        """
        Formats float seconds into SRT (HH:MM:SS,mmm) or VTT (HH:MM:SS.mmm).
        """
        total_ms = int(round(seconds * 1000))
        hrs = total_ms // 3600000
        mins = (total_ms % 3600000) // 60000
        secs = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        sep = "," if srt else "."
        return f"{hrs:02d}:{mins:02d}:{secs:02d}{sep}{ms:03d}"

    @classmethod
    def generate_srt(cls, words: List[Dict[str, Any]], words_per_chunk: int = 5, max_gap_s: float = 0.8) -> str:
        """
        Generates readable SRT subtitle blocks grouped by reasonable word count or pauses.
        """
        if not words:
            return ""

        chunks: List[List[Dict[str, Any]]] = []
        current_chunk: List[Dict[str, Any]] = []

        for w in words:
            if not current_chunk:
                current_chunk.append(w)
            else:
                last_end = current_chunk[-1]["end"]
                time_gap = w["start"] - last_end
                # Split chunk if too many words, long pause, or sentence-ending punctuation
                last_word = current_chunk[-1]["word"]
                is_sentence_end = last_word.endswith((".", "!", "?"))
                if len(current_chunk) >= words_per_chunk or time_gap > max_gap_s or is_sentence_end:
                    chunks.append(current_chunk)
                    current_chunk = [w]
                else:
                    current_chunk.append(w)

        if current_chunk:
            chunks.append(current_chunk)

        srt_lines = []
        for i, chunk in enumerate(chunks, 1):
            start_str = cls._format_timestamp(chunk[0]["start"], srt=True)
            end_str = cls._format_timestamp(chunk[-1]["end"], srt=True)
            text_str = " ".join(item["word"] for item in chunk)
            srt_lines.append(f"{i}\n{start_str} --> {end_str}\n{text_str}\n")

        return "\n".join(srt_lines)

    @classmethod
    def generate_vtt(cls, words: List[Dict[str, Any]], words_per_chunk: int = 5, max_gap_s: float = 0.8) -> str:
        """
        Generates standard WebVTT subtitles.
        """
        if not words:
            return "WEBVTT\n"

        srt_body = cls.generate_srt(words, words_per_chunk=words_per_chunk, max_gap_s=max_gap_s)
        # WebVTT uses . instead of , for millisecond separator
        vtt_body = srt_body.replace(",", ".")
        return "WEBVTT\n\n" + vtt_body
