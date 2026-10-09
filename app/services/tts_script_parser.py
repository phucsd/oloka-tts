"""
OlokaTTS Script Parser.
Parses natural text containing custom pause tags, citation brackets, and emotion cues.
Produces structured, versioned script segments for neural audio composition.
"""

import re
import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple


@dataclass
class ScriptSegment:
    """Represents a discrete segment of either speech text or explicit pause duration."""
    type: str  # "speech" | "pause"
    text: Optional[str] = None
    duration_ms: Optional[int] = None

    @property
    def duration_s(self) -> float:
        return (self.duration_ms or 0) / 1000.0

    def to_dict(self) -> Dict[str, Any]:
        if self.type == "speech":
            return {"type": "speech", "text": self.text or ""}
        return {"type": "pause", "duration_ms": int(self.duration_ms or 0)}


@dataclass
class ParsedScript:
    """Parsed representation of a TTS script."""
    version: int = 1
    raw_prompt: str = ""
    has_pauses: bool = False
    total_pause_ms: int = 0
    segments: List[ScriptSegment] = field(default_factory=list)

    def to_dict_segments(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.segments]

    def get_speech_segments(self) -> List[ScriptSegment]:
        return [s for s in self.segments if s.type == "speech"]

    def get_total_speech_text(self) -> str:
        return " ".join(s.text.strip() for s in self.get_speech_segments() if s.text)


class TtsScriptParser:
    """
    Shared script parser for OlokaTTS Studio, MCP, OpenAI-compatible API,
    LocalEngine, and Kaggle Workers.
    """

    # Matches escaped brackets e.g. \[0.2] or \\[0.2] OR standard brackets e.g. [0.2], [cười]
    TOKEN_RE = re.compile(r"(\\{1,2}\[[^\]\r\n]+\\?\])|\[([^\]\r\n]+)\]")

    MAX_PAUSE_COUNT = 50
    MAX_TOTAL_PAUSE_S = 30.0
    MAX_SINGLE_PAUSE_S = 5.0
    MIN_SINGLE_PAUSE_S = 0.05

    @classmethod
    def _parse_pause_tag(cls, inner: str) -> Optional[float]:
        """
        Interprets bracket content.
        Returns pause duration in seconds if recognized as a pause tag.
        Returns None if not a pause tag (e.g. emotion cues or reference numbers [1], [12]).
        Raises ValueError if syntax explicitly targets pause but violates constraints (e.g. negative, > 5.0s, NaN).
        """
        s = inner.strip()

        # 1. Explicit pause: prefix e.g. [pause:0.2s], [pause: 1s], [pause:1]
        if s.lower().startswith("pause:"):
            raw = s[6:].strip()
            # Strip trailing units
            raw = re.sub(r"(?:seconds?|sec|s)$", "", raw, flags=re.I).strip()
            try:
                val = float(raw)
            except ValueError:
                raise ValueError(f"Cú pháp thẻ pause không hợp lệ: [{inner}]")
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Thời gian pause không hợp lệ: [{inner}]")
            if val <= 0:
                raise ValueError(f"Thời gian pause phải lớn hơn 0: [{inner}]")
            if val > cls.MAX_SINGLE_PAUSE_S:
                raise ValueError(f"Thời gian pause tối đa là {cls.MAX_SINGLE_PAUSE_S}s: [{inner}]")
            return val

        # 2. Check negative numbers formatted like [-0.2] or [-1s]
        if s.startswith("-"):
            rem = s[1:].strip()
            if re.match(r"^[0-9]+(?:\.[0-9]+)?\s*(?:seconds?|sec|s)?$", rem, re.I):
                raise ValueError(f"Thời gian pause không được là số âm: [{inner}]")

        # 3. Time units e.g. [0.2s], [1s], [2.0s], [1.5 sec]
        m_unit = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:seconds?|sec|s)$", s, re.I)
        if m_unit:
            try:
                val = float(m_unit.group(1))
            except ValueError:
                raise ValueError(f"Cú pháp thẻ pause không hợp lệ: [{inner}]")
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Thời gian pause không hợp lệ: [{inner}]")
            if val <= 0:
                raise ValueError(f"Thời gian pause phải lớn hơn 0: [{inner}]")
            if val > cls.MAX_SINGLE_PAUSE_S:
                raise ValueError(f"Thời gian pause tối đa là {cls.MAX_SINGLE_PAUSE_S}s: [{inner}]")
            return val

        # 4. Decimal float without unit e.g. [0.2], [0.5], [1.0], [2.0]
        # (Must have decimal point to differentiate from reference citations like [1], [12])
        if re.match(r"^[0-9]+\.[0-9]+$", s):
            try:
                val = float(s)
            except ValueError:
                raise ValueError(f"Cú pháp thẻ pause không hợp lệ: [{inner}]")
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Thời gian pause không hợp lệ: [{inner}]")
            if val <= 0:
                raise ValueError(f"Thời gian pause phải lớn hơn 0: [{inner}]")
            if val > cls.MAX_SINGLE_PAUSE_S:
                raise ValueError(f"Thời gian pause tối đa là {cls.MAX_SINGLE_PAUSE_S}s: [{inner}]")
            return val

        # 5. Non-pause tag (e.g. [1], [12], [cười], [thở dài], [hắng giọng], [nói nhanh])
        return None

    @classmethod
    def parse(
        cls,
        text: str,
        max_pause_count: Optional[int] = None,
        max_total_pause_s: Optional[float] = None
    ) -> ParsedScript:
        """
        Parses text into a ParsedScript with speech and pause segments.
        Preserves Vietnamese Unicode, emotion cues, punctuation, and ordering.
        """
        if text is None or not text.strip():
            raise ValueError("Nội dung văn bản không được để trống.")

        max_count = max_pause_count or cls.MAX_PAUSE_COUNT
        max_total_s = max_total_pause_s or cls.MAX_TOTAL_PAUSE_S

        raw_segments: List[Tuple[str, Any]] = []
        last_idx = 0
        pause_count = 0
        total_pause_s = 0.0

        for m in cls.TOKEN_RE.finditer(text):
            start, end = m.span()
            escaped_bracket = m.group(1)
            inner_bracket = m.group(2)

            # Preceding text before this token
            if start > last_idx:
                chunk = text[last_idx:start]
                raw_segments.append(("speech", chunk))

            if escaped_bracket:
                # Unescape: e.g. \[0.2\] -> [0.2] or \[0.2] -> [0.2]
                unescaped = re.sub(r"^\\{1,2}\[", "[", escaped_bracket)
                unescaped = re.sub(r"\\\]$", "]", unescaped)
                raw_segments.append(("speech", unescaped))
            else:
                pause_sec = cls._parse_pause_tag(inner_bracket)
                if pause_sec is not None:
                    pause_count += 1
                    if pause_count > max_count:
                        raise ValueError(
                            f"Số lượng thẻ tạm dừng vượt quá giới hạn tối đa ({max_count} thẻ)"
                        )
                    total_pause_s += pause_sec
                    if total_pause_s > max_total_s:
                        raise ValueError(
                            f"Tổng thời gian tạm dừng ({total_pause_s:.2f}s) vượt quá giới hạn {max_total_s}s"
                        )
                    raw_segments.append(("pause", int(round(pause_sec * 1000))))
                else:
                    # Bracket that is NOT a pause tag (e.g. [1], [12], [cười], [thở dài])
                    raw_segments.append(("speech", m.group(0)))

            last_idx = end

        if last_idx < len(text):
            raw_segments.append(("speech", text[last_idx:]))

        # Coalesce adjacent speech chunks and strip unnecessary padding
        final_segments: List[ScriptSegment] = []
        current_speech: List[str] = []

        def flush_speech():
            if current_speech:
                combined = "".join(current_speech).strip()
                if combined:
                    final_segments.append(ScriptSegment(type="speech", text=combined))
                current_speech.clear()

        for seg_type, seg_val in raw_segments:
            if seg_type == "speech":
                current_speech.append(seg_val)
            else:
                flush_speech()
                final_segments.append(ScriptSegment(type="pause", duration_ms=seg_val))
        flush_speech()

        # Validate that the prompt actually contains readable speech
        speech_segs = [s for s in final_segments if s.type == "speech"]
        if not speech_segs:
            raise ValueError("Nội dung không chứa đoạn lời thoại nào cần đọc.")

        has_pauses = any(s.type == "pause" for s in final_segments)
        total_pause_ms = sum(s.duration_ms for s in final_segments if s.type == "pause")

        return ParsedScript(
            version=1,
            raw_prompt=text,
            has_pauses=has_pauses,
            total_pause_ms=total_pause_ms,
            segments=final_segments
        )
