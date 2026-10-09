"""
OlokaTTS Audio Composition Engine.
Assembles synthesized speech segments and waveform-domain silence pauses into a unified,
flawless 48kHz audio track with strict float32 mono compliance.
"""

from typing import List, Union, Dict, Any, Optional
import numpy as np


class AudioComposer:
    """
    Dedicated audio composer for waveform-domain pause insertion and segment stitching.
    """

    @staticmethod
    def generate_silence(duration_s: float, sample_rate: int = 48000) -> np.ndarray:
        """
        Generates silence of exact length in waveform samples.
        e.g. at 48kHz:
          0.2s -> 9,600 samples
          0.5s -> 24,000 samples
          1.0s -> 48,000 samples
        """
        if duration_s <= 0:
            return np.array([], dtype=np.float32)
        num_samples = int(round(duration_s * sample_rate))
        return np.zeros(num_samples, dtype=np.float32)

    @classmethod
    def sanitize_waveform(cls, audio: Any) -> np.ndarray:
        """
        Ensures waveform is 1D mono np.float32 array within [-1.0, 1.0].
        """
        if audio is None:
            return np.array([], dtype=np.float32)

        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim == 0:
            return np.array([], dtype=np.float32)
        elif arr.ndim == 2:
            # Multi-channel / stereo: average to mono
            if arr.shape[0] in (1, 2) and arr.shape[1] > arr.shape[0]:
                arr = arr.mean(axis=0)
            elif arr.shape[1] in (1, 2) and arr.shape[0] > arr.shape[1]:
                arr = arr.mean(axis=1)
            else:
                arr = arr.flatten()
        elif arr.ndim > 2:
            arr = arr.flatten()

        # Guard against NaN/Inf
        if np.isnan(arr).any() or np.isinf(arr).any():
            arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)

        # Soft limit / clamp to prevent clipping
        max_val = np.max(np.abs(arr)) if arr.size > 0 else 0.0
        if max_val > 1.0:
            arr = arr / max_val

        return arr.astype(np.float32)

    @classmethod
    def compose_audio(
        cls,
        parts: List[np.ndarray],
        sample_rate: int = 48000
    ) -> np.ndarray:
        """
        Concatenates a list of waveform parts (audio and silence arrays).
        """
        sanitized_parts = [cls.sanitize_waveform(p) for p in parts if p is not None and len(p) > 0]
        if not sanitized_parts:
            return np.array([], dtype=np.float32)
        if len(sanitized_parts) == 1:
            return sanitized_parts[0]
        return np.concatenate(sanitized_parts)

    @classmethod
    def compose_from_parsed_script(
        cls,
        segments: List[Any],
        speech_audios: List[np.ndarray],
        sample_rate: int = 48000
    ) -> np.ndarray:
        """
        Combines parsed script segments and speech audios into a single continuous waveform.
        
        Args:
            segments: List of ScriptSegment objects or dicts with 'type' ('speech'|'pause')
            speech_audios: List of synthesized waveforms corresponding 1:1 to speech segments
            sample_rate: Audio sampling rate (default 48000)
        """
        ordered_parts: List[np.ndarray] = []
        speech_idx = 0

        for seg in segments:
            # Handle dataclass or dict
            seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)

            if seg_type == "speech":
                if speech_idx < len(speech_audios):
                    w = cls.sanitize_waveform(speech_audios[speech_idx])
                    if len(w) > 0:
                        ordered_parts.append(w)
                    speech_idx += 1
                else:
                    # Missing audio for speech segment
                    pass
            elif seg_type == "pause":
                duration_ms = getattr(seg, "duration_ms", None)
                if duration_ms is None and isinstance(seg, dict):
                    duration_ms = seg.get("duration_ms", 0)
                duration_s = (duration_ms or 0) / 1000.0
                if duration_s > 0:
                    silence = cls.generate_silence(duration_s, sample_rate)
                    ordered_parts.append(silence)

        if not ordered_parts:
            return np.array([], dtype=np.float32)
        if len(ordered_parts) == 1:
            return ordered_parts[0]
        return np.concatenate(ordered_parts)
