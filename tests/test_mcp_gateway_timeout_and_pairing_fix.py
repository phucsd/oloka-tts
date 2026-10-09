import time
from datetime import datetime, timedelta
import pytest
from app.database import SessionLocal
from app.models import User, McpPairingSession, TTSJob, WorkerSession
from app.services.auth_service import AuthService
from app.services.mcp_auth_service import McpAuthService
from app.services.job_service import JobService
from app.services.db_sync_service import DbSyncService
from app.mcp_server import get_system_status

def test_pairing_session_resolves_despite_legacy_sess_transport_id():
    """Verify that authorized sessions with legacy/transient memory 'sess_' IDs resolve properly."""
    db = SessionLocal()
    try:
        # Create test user
        user = db.query(User).filter(User.username == "test_pairing_usr").first()
        if not user:
            user = User(
                id="usr_test_pairing",
                username="test_pairing_usr",
                email="test_pairing@oloka.net",
                hashed_password=AuthService.hash_password("Pass123!"),
                role="user",
                is_active=True
            )
            db.add(user)
            db.commit()

        # Create session with simulated legacy transient memory address ID
        code = "OLK-TSTPAR"
        session_token = "mcp_tok_test_legacy_address"
        legacy_transport_id = "sess_140128513300880"

        db.query(McpPairingSession).filter(McpPairingSession.code == code).delete()
        db.commit()

        sess = McpPairingSession(
            code=code,
            session_token=session_token,
            transport_session_id=legacy_transport_id,
            status="authorized",
            user_id=user.id,
            client_name="ChatGPT",
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=30)
        )
        db.add(sess)
        db.commit()

        # 1. Resolve with different memory address ID (simulating next stateless HTTP request)
        resolved_1 = McpAuthService.resolve_caller(
            db,
            pair_code=code,
            transport_session_id="sess_140127699283536"
        )
        assert resolved_1 is not None, "Failed to resolve user when memory address ID differed"
        assert resolved_1.id == user.id

        # 2. Resolve with None transport_session_id (stateless HTTP client)
        resolved_2 = McpAuthService.resolve_caller(
            db,
            pair_code=code,
            transport_session_id=None
        )
        assert resolved_2 is not None, "Failed to resolve user with None transport_session_id"
        assert resolved_2.id == user.id

        # 3. Resolve using session_token
        resolved_3 = McpAuthService.resolve_caller(
            db,
            session_token=session_token,
            transport_session_id="sess_different_memory_addr"
        )
        assert resolved_3 is not None, "Failed to resolve user using session_token"
        assert resolved_3.id == user.id
    finally:
        db.close()


def test_get_system_status_in_process_speed_and_zero_timeout():
    """Verify get_system_status executes in-process in milliseconds with zero network deadlocks."""
    t0 = time.time()
    status_text = get_system_status()
    elapsed = time.time() - t0

    assert elapsed < 0.5, f"get_system_status took too long ({elapsed:.3f}s), expected in-process < 0.5s"
    assert "Trạng Thái Hệ Thống OlokaTTS" in status_text
    assert "GPU Worker online" in status_text
    assert "Job đang chờ" in status_text


def test_cleanup_stale_jobs_marks_abandoned_jobs_failed():
    """Verify stale jobs in 'booting_kaggle' or 'queued' older than 5m are automatically cleaned up."""
    db = SessionLocal()
    try:
        # Create an abandoned job from 15 minutes ago
        job_id = "job_stale_test_123"
        db.query(TTSJob).filter(TTSJob.id == job_id).delete()
        db.commit()

        stale_time = datetime.utcnow() - timedelta(minutes=15)
        stale_job = TTSJob(
            id=job_id,
            user_id="usr_test_pairing",
            prompt="Stale test prompt",
            voice_id="Hải Đăng",
            status="booting_kaggle",
            created_at=stale_time,
            updated_at=stale_time
        )
        db.add(stale_job)
        db.commit()

        # Run cleanup with 5 minute threshold
        JobService.cleanup_stale_jobs(db, max_age_minutes=5)

        db.refresh(stale_job)
        assert stale_job.status == "failed"
        assert "Hết hạn chờ xử lý" in (stale_job.error_message or "")
    finally:
        db.close()


def test_db_sync_non_blocking_cooldown():
    """Verify backup_database returns in sub-millisecond time without blocking the caller."""
    t0 = time.time()
    for _ in range(10):
        DbSyncService.backup_database(immediate=False)
    elapsed = time.time() - t0

    assert elapsed < 0.05, f"backup_database was blocking! Took {elapsed:.3f}s"
