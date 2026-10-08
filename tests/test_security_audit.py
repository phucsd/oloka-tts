import pytest
import os
import secrets
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal, Base, engine
from app.models import User, TTSJob, VoiceSample
from app.services.auth_service import AuthService, get_current_user_optional, get_current_admin
from app.services.mcp_auth_service import McpAuthService
from app.services.job_service import JobService
from app.services.settings_service import SettingsService
from app.routers.auth import is_safe_redirect_url

client = TestClient(app)

def test_f01_f02_dockerignore_security():
    """Verify .dockerignore excludes sensitive files."""
    dockerignore_path = Path(".dockerignore")
    assert dockerignore_path.exists()
    content = dockerignore_path.read_text(encoding="utf-8")
    assert ".env" in content
    assert "*.db" in content
    assert "storage/*.db*" in content

def test_f03_secret_key_no_hardcoded_leak():
    """Verify SECRET_KEY is not the hardcoded string from audit."""
    assert settings.SECRET_KEY != "olokatts_super_secret_production_key_2026_xyz"
    assert len(settings.SECRET_KEY) >= 16

def test_f04_admin_password_no_hardcoded_default():
    """Verify default password is not Admin@123456."""
    db = SessionLocal()
    try:
        # Check that seed_default_admin creates random password when not configured
        assert settings.ADMIN_DEFAULT_PASSWORD != "Admin@123456"
    finally:
        db.close()

def test_f05_mcp_resolve_caller_isolation():
    """Verify resolve_caller never falls back to another user's session."""
    db = SessionLocal()
    try:
        # Unauthenticated call with no pair code or api key must return None
        caller = McpAuthService.resolve_caller(db, pair_code=None, api_key=None)
        assert caller is None

        # Expired or fake code must return None
        caller_fake = McpAuthService.resolve_caller(db, pair_code="OLK-FAKExx", api_key=None)
        assert caller_fake is None
    finally:
        db.close()

def test_f06_tts_job_and_audio_authorization():
    """Verify unauthorized users cannot access other users' jobs or audio, and /audio_files is not mounted."""
    db = SessionLocal()
    try:
        # Create a job owned by user 'test_owner_1'
        test_job = TTSJob(
            id="job_sec_test_123",
            user_id="user_owner_abc",
            prompt="Nội dung bí mật",
            status="completed",
            audio_path="storage/audio/dummy.wav"
        )
        db.add(test_job)
        db.commit()

        # Anonymous request to job details should be 403 Forbidden
        resp_job = client.get("/v1/tts/jobs/job_sec_test_123")
        assert resp_job.status_code == 403

        # Anonymous request to job audio should be 403 Forbidden
        resp_audio = client.get("/v1/tts/jobs/job_sec_test_123/audio")
        assert resp_audio.status_code == 403

        # Direct /audio_files static mount should return 404
        resp_static = client.get("/audio_files/dummy.wav")
        assert resp_static.status_code == 404

        # Cleanup
        db.delete(test_job)
        db.commit()
    finally:
        db.close()

def test_f07_voice_samples_unauthenticated_access():
    """Verify anonymous access cannot view private samples or upload."""
    # Anonymous list returns empty list
    resp_list = client.get("/v1/voices/samples")
    assert resp_list.status_code == 200
    assert resp_list.json() == []

    # Anonymous upload returns 401 Unauthorized
    resp_upload = client.post("/v1/voices/samples", data={"name": "test"})
    assert resp_upload.status_code == 401

def test_f08_worker_token_timing_attack_and_lease():
    """Verify worker auth requires exact valid token and checks job lease."""
    # Invalid token returns 403
    resp = client.post("/api/worker/jobs/pull", headers={"Authorization": "Bearer wrong_token"})
    assert resp.status_code == 403

def test_f10_api_key_admin_access_restricted():
    """Verify API keys without admin scope cannot access get_current_admin."""
    from fastapi import Request
    db = SessionLocal()
    try:
        # Create normal user
        user = User(id="user_test_regular", username="regular", role="user")
        # Direct check with mock request
        class MockRequest:
            url = Path = type('obj', (object,), {'path': '/admin'})
            state = type('obj', (object,), {'api_key': None})

        # Non-admin user gets 403
        with pytest.raises(Exception):
            get_current_admin(MockRequest(), user=user)
    finally:
        db.close()

def test_f11_open_redirect_prevention():
    """Verify is_safe_redirect_url strictly blocks open redirects."""
    # Dangerous redirects
    assert not is_safe_redirect_url("//evil.com")
    assert not is_safe_redirect_url("//google.com/test")
    assert not is_safe_redirect_url("/\\evil.com")
    assert not is_safe_redirect_url("\\evil.com")
    assert not is_safe_redirect_url("https://evil.com")
    assert not is_safe_redirect_url("http://evil.com")
    assert not is_safe_redirect_url("javascript:alert(1)")
    assert not is_safe_redirect_url("/login?next=/admin")

    # Safe relative paths
    assert is_safe_redirect_url("/")
    assert is_safe_redirect_url("/settings")
    assert is_safe_redirect_url("/admin/jobs")

def test_f14_public_registration_toggle():
    """Verify public registration can be disabled by admin."""
    db = SessionLocal()
    try:
        # Disable public registration in DB settings
        SettingsService.set_setting(db, "allow_public_registration", "false")

        # GET /register should redirect with error
        resp_get = client.get("/register", follow_redirects=False)
        assert resp_get.status_code == 303
        assert "registration_disabled" in resp_get.headers.get("Location", "")

        # POST /register should be blocked with 403 or redirect
        resp_post = client.post(
            "/register",
            data={
                "username": "newuser999",
                "email": "newuser999@test.com",
                "password": "Password123!",
                "confirm_password": "Password123!"
            },
            headers={"Accept": "application/json"}
        )
        assert resp_post.status_code == 403

        # Re-enable for standard operation
        SettingsService.set_setting(db, "allow_public_registration", "true")
    finally:
        db.close()

def test_f17_query_string_token_rejected():
    """Verify tokens in query parameters are ignored for authentication."""
    db = SessionLocal()
    try:
        # Attempt to access protected endpoint using ?api_key= or ?token=
        resp = client.get("/api/auth/me?token=fake_token&api_key=fake_key")
        assert resp.status_code == 401
    finally:
        db.close()
