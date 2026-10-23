"""
Tests for Forced Alignment Service and Admin Alignment Test Studio.
"""

import os
import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import get_db, SessionLocal, engine
from app.models import Base, User, TTSJob, VoicePreset
from app.services.forced_aligner import ForcedAlignerService
from app.services.auth_service import AuthService
from app.services.job_service import JobService


@pytest.fixture(scope="module")
def db_session():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    # Seed presets if needed
    JobService.seed_presets(db)
    yield db
    db.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def admin_user(db_session):
    admin = db_session.query(User).filter(User.username == "admin_test_align").first()
    if not admin:
        admin = User(
            id="usr_admin_test_align",
            username="admin_test_align",
            email="admin_align@example.com",
            hashed_password=AuthService.hash_password("admin123456"),
            role="admin",
            is_active=True
        )
        db_session.add(admin)
        db_session.commit()
    return admin


@pytest.fixture(scope="module")
def admin_token(admin_user):
    return AuthService.create_token(user_id=admin_user.id, username=admin_user.username, role=admin_user.role)


def test_clean_text_for_alignment():
    raw_prompt = "Xin chào các bạn [cười], hôm nay trời đẹp quá! [pause: 0.5s] [thở dài] Cùng bắt đầu nhé [1]."
    clean = ForcedAlignerService.clean_text_for_alignment(raw_prompt)
    assert "[cười]" not in clean
    assert "[pause: 0.5s]" not in clean
    assert "[thở dài]" not in clean
    assert "[1]" not in clean
    assert "Xin chào các bạn, hôm nay trời đẹp quá! Cùng bắt đầu nhé." in clean


def test_align_with_sample_wav():
    wav_path = "app/static/samples/presets/vp_haidang.wav"
    assert os.path.exists(wav_path), "Sample wav should exist"

    text = "Xin chào, [cười] tôi là Hải Đăng."
    result = ForcedAlignerService.align(wav_path, text, model_name="base")

    assert result["success"] is True
    assert result["words_count"] == 6
    assert result["clean_text"] == "Xin chào, tôi là Hải Đăng."
    assert result["duration"] > 1.0

    words = result["words"]
    for i, w in enumerate(words):
        assert "word" in w
        assert "start" in w
        assert "end" in w
        assert w["end"] >= w["start"]
        assert w["duration"] >= 0.02
        if i > 0:
            # Monotonic start progression
            assert w["start"] >= words[i-1]["start"]

    # Check SRT formatting
    srt = result["srt"]
    assert "-->" in srt
    assert "00:00:" in srt


def test_admin_align_test_page_render(client, admin_token):
    headers = {"Cookie": f"session_token={admin_token}"}
    response = client.get("/admin/align-test", headers=headers)
    assert response.status_code == 200
    assert "Kiểm Thử Căn Chỉnh Mốc Thời Gian" in response.text
    assert "wordChipsContainer" in response.text
    assert "runAlignment" in response.text


def test_admin_api_align_test_with_preset(client, admin_token):
    headers = {"Cookie": f"session_token={admin_token}"}
    payload = {
        "sample_voice_id": "Hải Đăng",
        "text": "Xin chào, tôi là Hải Đăng.",
        "model_size": "base"
    }
    response = client.post("/admin/api/align-test", data=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["words_count"] > 0
    assert len(data["words"]) > 0
    assert "srt" in data
    assert "audio_url" in data


def test_job_alignment_and_subtitles_endpoints(client, admin_token, db_session, admin_user):
    wav_path = "app/static/samples/presets/vp_haidang.wav"
    abs_wav = os.path.abspath(wav_path)

    # Create completed dummy job
    job = TTSJob(
        id="job_test_alignment_dummy",
        user_id=admin_user.id,
        prompt="Xin chào, tôi là Hải Đăng.",
        voice_id="Hải Đăng",
        status="completed",
        audio_path=abs_wav,
        audio_url="/v1/tts/jobs/job_test_alignment_dummy/audio",
        duration=2.14
    )
    db_session.merge(job)
    db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Test get job details
    r_job = client.get("/v1/tts/jobs/job_test_alignment_dummy", headers=headers)
    assert r_job.status_code == 200
    job_data = r_job.json()
    assert job_data["status"] == "completed"
    assert job_data["alignment"] is not None
    assert len(job_data["alignment"]) > 0

    # 2. Test get alignment directly
    r_align = client.get("/v1/tts/jobs/job_test_alignment_dummy/alignment", headers=headers)
    assert r_align.status_code == 200
    align_data = r_align.json()
    assert align_data["success"] is True
    assert align_data["words_count"] > 0

    # 3. Test get SRT subtitles
    r_srt = client.get("/v1/tts/jobs/job_test_alignment_dummy/subtitles?format=srt", headers=headers)
    assert r_srt.status_code == 200
    assert "-->" in r_srt.text
    assert "00:00:" in r_srt.text

    # 4. Test get VTT subtitles
    r_vtt = client.get("/v1/tts/jobs/job_test_alignment_dummy/subtitles?format=vtt", headers=headers)
    assert r_vtt.status_code == 200
    assert "WEBVTT" in r_vtt.text


def test_generated_kaggle_worker_script_compiles(tmp_path):
    from app.services.kaggle_notebook_builder import KaggleNotebookBuilder
    script = KaggleNotebookBuilder.generate_worker_script(output_dir=str(tmp_path))
    compiled = compile(script, "worker.py", "exec")
    assert compiled is not None


def test_worker_raw_alignment_enrichment(db_session, admin_user):
    raw_worker_json = json.dumps({
        "words": [
            {"index": 0, "word": "Xin", "start": 0.1, "end": 0.35, "duration": 0.25},
            {"index": 1, "word": "chào", "start": 0.36, "end": 0.72, "duration": 0.36},
        ],
        "duration": 0.8
    }, ensure_ascii=False)

    job = TTSJob(
        id="job_test_worker_raw_align",
        user_id=admin_user.id,
        prompt="Xin chào",
        voice_id="Hải Đăng",
        status="completed",
        duration=0.8,
        alignment_data=raw_worker_json
    )
    db_session.merge(job)
    db_session.commit()

    enriched = JobService.get_or_create_job_alignment(db_session, job)
    assert enriched is not None
    assert enriched["success"] is True
    assert enriched["words_count"] == 2
    assert "-->" in enriched["srt"]
    assert "WEBVTT" in enriched["vtt"]

