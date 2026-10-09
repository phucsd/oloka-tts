"""
Unit and Integration Tests for OlokaTTS Pause Tags, Audio Composition,
and VieNeu-TTS Worker Compatibility.
Covers Test A through Test I as mandated by the critical fix specification.
"""

import pytest
import numpy as np
import io
import soundfile as sf
from datetime import datetime, timedelta

from app.services.tts_script_parser import TtsScriptParser, ParsedScript, ScriptSegment
from app.services.audio_composer import AudioComposer
from app.services.job_service import JobService
from app.models import TTSJob, KaggleExecutionAccount, User


# ==============================================================================
# TEST A — PARSER BASIC TEST
# ==============================================================================

def test_a_parser_basic():
    """
    Test A: 'Xin chào [0.2] các bạn'
    Expected:
      - 2 speech segments: 'Xin chào', 'các bạn'
      - 1 pause segment: 200 ms
      - No literal '[0.2]' in speech text
      - Preserves words 'Xin chào', 'các bạn'
    """
    text = "Xin chào [0.2] các bạn"
    parsed = TtsScriptParser.parse(text)

    assert parsed.has_pauses is True
    assert parsed.total_pause_ms == 200
    assert len(parsed.segments) == 3

    seg0 = parsed.segments[0]
    assert seg0.type == "speech"
    assert seg0.text == "Xin chào"

    seg1 = parsed.segments[1]
    assert seg1.type == "pause"
    assert seg1.duration_ms == 200
    assert seg1.duration_s == 0.2

    seg2 = parsed.segments[2]
    assert seg2.type == "speech"
    assert seg2.text == "các bạn"

    # Verify no '[0.2]' inside speech texts
    speech_texts = [s.text for s in parsed.get_speech_segments()]
    for st in speech_texts:
        assert "[0.2]" not in st
        assert "0.2" not in st


# ==============================================================================
# TEST B — MULTIPLE PAUSES & ALIASES
# ==============================================================================

def test_b_multiple_pauses_and_ordering():
    """
    Test B: 'Một [0.2] hai [0.5] ba [1.0] bốn.'
    Expected:
      - 4 speech segments, 3 pause segments
      - Proper sequential ordering
      - Total pause duration: 200 + 500 + 1000 = 1700 ms
    """
    text = "Một [0.2] hai [0.5] ba [1.0] bốn."
    parsed = TtsScriptParser.parse(text)

    assert parsed.has_pauses is True
    assert parsed.total_pause_ms == 1700
    assert len(parsed.segments) == 7

    types = [s.type for s in parsed.segments]
    assert types == ["speech", "pause", "speech", "pause", "speech", "pause", "speech"]

    durations = [s.duration_ms for s in parsed.segments if s.type == "pause"]
    assert durations == [200, 500, 1000]

    texts = [s.text for s in parsed.segments if s.type == "speech"]
    assert texts == ["Một", "hai", "ba", "bốn."]


def test_b2_pause_aliases():
    """Verify [0.2], [0.2s], [pause:0.2s], [1s], [pause:1s] behave identically."""
    p1 = TtsScriptParser.parse("Câu 1 [0.2] Câu 2")
    p2 = TtsScriptParser.parse("Câu 1 [0.2s] Câu 2")
    p3 = TtsScriptParser.parse("Câu 1 [pause:0.2s] Câu 2")

    assert p1.total_pause_ms == 200
    assert p2.total_pause_ms == 200
    assert p3.total_pause_ms == 200
    assert p1.segments[1].duration_ms == p2.segments[1].duration_ms == p3.segments[1].duration_ms == 200

    p4 = TtsScriptParser.parse("Câu 1 [1.0] Câu 2")
    p5 = TtsScriptParser.parse("Câu 1 [1s] Câu 2")
    p6 = TtsScriptParser.parse("Câu 1 [pause:1s] Câu 2")
    assert p4.total_pause_ms == 1000
    assert p5.total_pause_ms == 1000
    assert p6.total_pause_ms == 1000


# ==============================================================================
# TEST C — EMOTION CUES PRESERVATION
# ==============================================================================

def test_c_emotion_preservation():
    """
    Test C: '[cười] Xin chào [0.2] [thở dài] Hôm nay tôi rất mệt.'
    Expected:
      - '[cười]' and '[thở dài]' stay intact in speech segments
      - '[0.2]' is parsed as pause
    """
    text = "[cười] Xin chào [0.2] [thở dài] Hôm nay tôi rất mệt."
    parsed = TtsScriptParser.parse(text)

    assert parsed.has_pauses is True
    assert parsed.total_pause_ms == 200

    speeches = [s.text for s in parsed.get_speech_segments()]
    assert len(speeches) == 2
    assert speeches[0] == "[cười] Xin chào"
    assert speeches[1] == "[thở dài] Hôm nay tôi rất mệt."


# ==============================================================================
# TEST D — NON-PAUSE BRACKETS (CITATIONS & ESCAPES)
# ==============================================================================

def test_d_non_pause_brackets_citations():
    """
    Test D: Reference brackets [1] and [12] must not be treated as pauses.
    """
    text = "Tham khảo tài liệu [1] và [12]."
    parsed = TtsScriptParser.parse(text)

    assert parsed.has_pauses is False
    assert parsed.total_pause_ms == 0
    assert len(parsed.segments) == 1
    assert parsed.segments[0].type == "speech"
    assert parsed.segments[0].text == "Tham khảo tài liệu [1] và [12]."


def test_d2_escaped_brackets():
    """Escaped brackets like \\[0.2] or \\[0.5\\] must be preserved as literal speech text."""
    text = r"Mã hiệu \[0.2\] cần giữ nguyên và [0.5] khoảng dừng."
    parsed = TtsScriptParser.parse(text)

    assert parsed.has_pauses is True
    assert parsed.total_pause_ms == 500

    speeches = [s.text for s in parsed.get_speech_segments()]
    assert "[0.2]" in speeches[0]


# ==============================================================================
# TEST E — INVALID INPUT VALIDATION & LIMITS
# ==============================================================================

def test_e_negative_pause_rejected():
    with pytest.raises(ValueError, match="không được là số âm|lớn hơn 0"):
        TtsScriptParser.parse("Câu 1 [-0.2] Câu 2")

    with pytest.raises(ValueError, match="không được là số âm|lớn hơn 0"):
        TtsScriptParser.parse("Câu 1 [pause:-0.5s] Câu 2")


def test_e_excessive_pause_duration_rejected():
    with pytest.raises(ValueError, match="tối đa là 5.0s"):
        TtsScriptParser.parse("Câu 1 [6.0] Câu 2")

    with pytest.raises(ValueError, match="tối đa là 5.0s"):
        TtsScriptParser.parse("Câu 1 [pause:10s] Câu 2")


def test_e_zero_pause_rejected():
    with pytest.raises(ValueError, match="phải lớn hơn 0"):
        TtsScriptParser.parse("Câu 1 [0.0] Câu 2")


def test_e_too_many_pauses_rejected():
    many_pauses = " ".join(f"Từ {i} [0.1]" for i in range(55))
    with pytest.raises(ValueError, match="vượt quá giới hạn tối đa"):
        TtsScriptParser.parse(many_pauses)


def test_e_total_pause_exceeded_rejected():
    pauses_32s = " ".join(f"Từ {i} [4.0]" for i in range(9))  # 9 * 4.0 = 36s > 30s
    with pytest.raises(ValueError, match="Tổng thời gian tạm dừng"):
        TtsScriptParser.parse(pauses_32s)


def test_e_pure_pauses_no_speech_rejected():
    with pytest.raises(ValueError, match="không chứa đoạn lời thoại nào"):
        TtsScriptParser.parse("[0.5] [1.0]")


def test_e_empty_text_rejected():
    with pytest.raises(ValueError, match="không được để trống"):
        TtsScriptParser.parse("   ")


# ==============================================================================
# TEST F — WAVEFORM COMPOSITION & PRECISION
# ==============================================================================

def test_f_waveform_sample_counts_and_concatenation():
    """
    Test F: Verify exact sample insertion at 48kHz:
      - 200 ms = 9,600 samples
      - 500 ms = 24,000 samples
      - 1,000 ms = 48,000 samples
    Check dtype float32, mono 1D, no NaN, no clipping.
    """
    sr = 48000
    # Mock speech audio: 1 second of 440Hz tone
    speech1 = np.sin(2 * np.pi * 440 * np.linspace(0, 1, sr, endpoint=False, dtype=np.float32)) * 0.5
    # Mock speech audio: 0.5 second of 880Hz tone
    speech2 = np.sin(2 * np.pi * 880 * np.linspace(0, 0.5, sr // 2, endpoint=False, dtype=np.float32)) * 0.5

    parsed = TtsScriptParser.parse("Đoạn một [0.2] Đoạn hai [0.5] Đoạn ba [1.0] Hết.")
    speech_audios = [speech1, speech2, speech1, speech2]

    composed = AudioComposer.compose_from_parsed_script(
        segments=parsed.segments,
        speech_audios=speech_audios,
        sample_rate=sr
    )

    expected_speech_samples = len(speech1) + len(speech2) + len(speech1) + len(speech2)
    expected_pause_samples = int(0.2 * sr) + int(0.5 * sr) + int(1.0 * sr)  # 9600 + 24000 + 48000 = 81600
    expected_total_samples = expected_speech_samples + expected_pause_samples

    assert composed.dtype == np.float32
    assert composed.ndim == 1
    assert len(composed) == expected_total_samples

    # Check that silence regions contain exact zeros
    # Region 1: after speech1 (len 48000) for 9600 samples
    silence_region_1 = composed[48000:48000 + 9600]
    assert np.all(silence_region_1 == 0.0)

    # Check audio amplitude range
    assert np.max(np.abs(composed)) <= 1.0


def test_f2_audio_composer_wav_encoding():
    """Verify soundfile writes and reads the composed audio cleanly."""
    sr = 48000
    silence_200ms = AudioComposer.generate_silence(0.2, sr)
    assert len(silence_200ms) == 9600

    tone = np.ones(24000, dtype=np.float32) * 0.3
    composed = AudioComposer.compose_audio([tone, silence_200ms, tone], sr)

    buf = io.BytesIO()
    sf.write(buf, composed, sr, format="WAV")
    buf.seek(0)

    read_audio, read_sr = sf.read(buf, dtype="float32")
    assert read_sr == sr
    assert len(read_audio) == 24000 + 9600 + 24000
    np.testing.assert_allclose(read_audio, composed, atol=1e-4)


# ==============================================================================
# TEST G — SHORT SPEECH SEGMENTS
# ==============================================================================

def test_g_short_speech_segments():
    """
    Test G: 'Tôi [0.2] nghĩ [0.2] vậy.'
    Short words must not be dropped and must preserve order without merging across pause tags.
    """
    text = "Tôi [0.2] nghĩ [0.2] vậy."
    parsed = TtsScriptParser.parse(text)

    speeches = [s.text for s in parsed.get_speech_segments()]
    assert speeches == ["Tôi", "nghĩ", "vậy."]

    pauses = [s.duration_ms for s in parsed.segments if s.type == "pause"]
    assert pauses == [200, 200]


# ==============================================================================
# TEST H — WORKER COMPATIBILITY & PROTOCOL VERSIONING
# ==============================================================================

def test_h_worker_capabilities_isolation():
    """
    Test H:
    1. A job with pause tags must NOT be pulled by a legacy worker lacking 'pause_tags_v1'.
    2. A modern worker with 'pause_tags_v1' successfully pulls the job with 'segments' and 'has_pauses=True'.
    3. A job without pause tags can be pulled by any worker.
    """
    from app.database import SessionLocal
    db_session = SessionLocal()
    try:
        # Create test account
        acc = db_session.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == "kacc_test_h").first()
        if not acc:
            acc = KaggleExecutionAccount(
                id="kacc_test_h",
                owner_user_id="user_test_h",
                kaggle_username="user_test_h",
                kaggle_key="key",
                kernel_ref="user/ref",
                is_enabled=True
            )
            db_session.add(acc)
            db_session.commit()

        # Create job with pause tags
        job_with_pauses = JobService.create_job(
            db=db_session,
            prompt="Xin chào [0.5] các bạn.",
            voice_id="Hải Đăng",
            user_id="user_test_h",
            execution_account_id=acc.id
        )

        # 1. Legacy worker pulls without capabilities
        legacy_pulled = JobService.pull_pending_job(
            db=db_session,
            worker_id="worker_legacy",
            execution_account_id=acc.id,
            worker_capabilities=[]  # Legacy worker
        )
        # Must NOT give job with pause tags to legacy worker!
        assert legacy_pulled is None

        # 2. Modern worker pulls with 'pause_tags_v1'
        modern_pulled = JobService.pull_pending_job(
            db=db_session,
            worker_id="worker_modern",
            execution_account_id=acc.id,
            worker_capabilities=["pause_tags_v1"]
        )
        assert modern_pulled is not None
        assert modern_pulled["id"] == job_with_pauses.id
        assert modern_pulled["has_pauses"] is True
        assert modern_pulled["script_version"] == 1
        assert len(modern_pulled["segments"]) == 3
        assert modern_pulled["segments"][1]["duration_ms"] == 500

        # 3. Create job WITHOUT pauses
        job_no_pauses = JobService.create_job(
            db=db_session,
            prompt="Xin chào các bạn không có khoảng nghỉ.",
            voice_id="Hải Đăng",
            user_id="user_test_h",
            execution_account_id=acc.id
        )

        # Legacy worker CAN pull this standard job safely
        legacy_pulled_std = JobService.pull_pending_job(
            db=db_session,
            worker_id="worker_legacy_2",
            execution_account_id=acc.id,
            worker_capabilities=[]
        )
        assert legacy_pulled_std is not None
        assert legacy_pulled_std["id"] == job_no_pauses.id
        assert legacy_pulled_std["has_pauses"] is False
    finally:
        db_session.close()


# ==============================================================================
# TEST I — ENDPOINT CONSISTENCY & ESTIMATE SPEECH DURATION
# ==============================================================================

def test_i_estimate_speech_duration_with_pauses():
    """
    Test I: estimate_speech_duration must parse pause tags, exclude them from word count,
    and add explicit pause seconds to the duration.
    """
    from app.mcp_server import estimate_speech_duration

    # 1. Pure speech (3 words) at 1.0x (~1s speech)
    est_pure = estimate_speech_duration("Một hai ba", speed=1.0)
    assert "Số từ lời thoại: **3**" in est_pure
    assert "Khoảng ngắt nghỉ" not in est_pure

    # 2. Speech with 2 pause tags (1.5s total pause)
    est_with_pause = estimate_speech_duration("Một [0.5] hai [1.0] ba", speed=1.0)
    assert "Số từ lời thoại: **3**" in est_with_pause
    assert "+1.5s" in est_with_pause
    assert "Khoảng ngắt nghỉ chỉ định (2 thẻ)" in est_with_pause
