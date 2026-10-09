import pytest
import os
import json
import secrets
import subprocess
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import (
    User, TTSJob, WorkerSession, KaggleExecutionAccount,
    WorkerToken, McpPairingSession, ApiKey
)
from app.services.auth_service import AuthService
from app.services.kaggle_account_service import KaggleAccountService
from app.services.kaggle_orchestrator import KaggleOrchestrator
from app.services.kaggle_notebook_builder import KaggleNotebookBuilder
from app.services.job_service import JobService
from app.services.mcp_auth_service import McpAuthService
from app.mcp_server import mcp

client = TestClient(app)

@pytest.fixture(scope="module")
def setup_tenants():
    """
    Creates isolated test fixtures:
    - User Admin + Kaggle Account Admin + Worker Token Admin
    - User A + Kaggle Account A + Worker Token A
    - User B + Kaggle Account B + Worker Token B
    - User C (Unconfigured, No Kaggle credentials)
    """
    db = SessionLocal()
    try:
        # 1. Admin
        admin = db.query(User).filter(User.email == "admin_test@oloka.net").first()
        if not admin:
            admin = User(
                id="usr_test_admin",
                username="admin_test",
                email="admin_test@oloka.net",
                hashed_password=AuthService.hash_password("AdminPass@123"),
                role="admin",
                kaggle_username="admin_kaggle",
                kaggle_key="admin_kkey_12345"
            )
            db.add(admin)
            db.commit()

        acc_admin = KaggleAccountService.get_execution_account_for_user(db, admin)
        wtk_admin, token_admin_raw = KaggleAccountService.issue_worker_token(
            db, owner_user_id=admin.id, execution_account_id=acc_admin.id
        )

        # 2. User A
        user_a = db.query(User).filter(User.email == "user_a@oloka.net").first()
        if not user_a:
            user_a = User(
                id="usr_test_a",
                username="user_a",
                email="user_a@oloka.net",
                hashed_password=AuthService.hash_password("PassA@123"),
                role="user",
                kaggle_username="user_a_kaggle",
                kaggle_key="user_a_kkey_67890"
            )
            db.add(user_a)
            db.commit()

        acc_a = KaggleAccountService.get_execution_account_for_user(db, user_a)
        wtk_a, token_a_raw = KaggleAccountService.issue_worker_token(
            db, owner_user_id=user_a.id, execution_account_id=acc_a.id
        )

        # 3. User B
        user_b = db.query(User).filter(User.email == "user_b@oloka.net").first()
        if not user_b:
            user_b = User(
                id="usr_test_b",
                username="user_b",
                email="user_b@oloka.net",
                hashed_password=AuthService.hash_password("PassB@123"),
                role="user",
                kaggle_username="user_b_kaggle",
                kaggle_key="user_b_kkey_abcde"
            )
            db.add(user_b)
            db.commit()

        acc_b = KaggleAccountService.get_execution_account_for_user(db, user_b)
        wtk_b, token_b_raw = KaggleAccountService.issue_worker_token(
            db, owner_user_id=user_b.id, execution_account_id=acc_b.id
        )

        # 4. User C (Unconfigured - No Kaggle credentials)
        user_c = db.query(User).filter(User.email == "user_c@oloka.net").first()
        if not user_c:
            user_c = User(
                id="usr_test_c",
                username="user_c",
                email="user_c@oloka.net",
                hashed_password=AuthService.hash_password("PassC@123"),
                role="user",
                kaggle_username=None,
                kaggle_key=None
            )
            db.add(user_c)
            db.commit()

        context = {
            "admin_id": admin.id,
            "acc_admin_id": acc_admin.id,
            "token_admin": token_admin_raw,
            "user_a_id": user_a.id,
            "acc_a_id": acc_a.id,
            "token_a": token_a_raw,
            "user_b_id": user_b.id,
            "acc_b_id": acc_b.id,
            "user_b_kaggle_username": acc_b.kaggle_username,
            "user_b_kernel_slug": acc_b.kernel_slug,
            "user_b_kernel_title": acc_b.kernel_title,
            "token_b": token_b_raw,
            "user_c_id": user_c.id
        }
        return context
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 01 & 02: Worker A cannot pull Job B, and Worker B cannot pull Job A
# -----------------------------------------------------------------------------
def test_01_and_02_strict_worker_pull_cross_tenant_isolation(setup_tenants):
    """Verify that Worker A pulls ONLY Job A and never Job B; Worker B pulls ONLY Job B."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean up any leftover jobs
        db.query(TTSJob).filter(TTSJob.execution_account_id.in_([ctx["acc_a_id"], ctx["acc_b_id"]])).delete()
        db.commit()

        # Create Job A for User A
        job_a = JobService.create_job(
            db=db,
            prompt="Job của User A",
            user_id=ctx["user_a_id"],
            execution_account_id=ctx["acc_a_id"]
        )

        # Create Job B for User B
        job_b = JobService.create_job(
            db=db,
            prompt="Job của User B",
            user_id=ctx["user_b_id"],
            execution_account_id=ctx["acc_b_id"]
        )

        assert job_a.execution_account_id == ctx["acc_a_id"]
        assert job_b.execution_account_id == ctx["acc_b_id"]

        # Worker A attempts to pull with Token A
        resp_a = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "worker_a_gpu0"},
            headers={"Authorization": f"Bearer {ctx['token_a']}"}
        )
        assert resp_a.status_code == 200
        pulled_a = resp_a.json()
        assert pulled_a.get("id") == job_a.id
        assert pulled_a.get("prompt") == "Job của User A"
        assert pulled_a.get("lease_token") is not None

        # Worker A attempts to pull again -> queue for Account A is empty, must NOT receive Job B!
        resp_a_empty = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "worker_a_gpu0"},
            headers={"Authorization": f"Bearer {ctx['token_a']}"}
        )
        assert resp_a_empty.status_code == 200
        assert resp_a_empty.json() == {}

        # Worker B pulls with Token B -> receives Job B
        resp_b = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "worker_b_gpu0"},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert resp_b.status_code == 200
        pulled_b = resp_b.json()
        assert pulled_b.get("id") == job_b.id
        assert pulled_b.get("prompt") == "Job của User B"
        assert pulled_b.get("lease_token") is not None

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id.in_([job_a.id, job_b.id])).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 03 & 04: Admin worker cannot pull Tenant Job, and vice versa
# -----------------------------------------------------------------------------
def test_03_and_04_admin_worker_tenant_job_isolation(setup_tenants):
    """Admin worker must NEVER pull jobs belonging to User B, and User B worker cannot pull Admin job."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean up existing jobs
        db.query(TTSJob).filter(TTSJob.execution_account_id.in_([ctx["acc_admin_id"], ctx["acc_b_id"]])).delete()
        db.commit()

        job_b = JobService.create_job(
            db=db,
            prompt="Job cá nhân của User B",
            user_id=ctx["user_b_id"],
            execution_account_id=ctx["acc_b_id"]
        )

        # Admin worker polls
        resp_admin = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "admin_worker_0"},
            headers={"Authorization": f"Bearer {ctx['token_admin']}"}
        )
        assert resp_admin.status_code == 200
        assert resp_admin.json() == {}  # Empty! Admin worker didn't touch User B's job

        # Admin queues a job
        job_admin = JobService.create_job(
            db=db,
            prompt="Job của Admin",
            user_id=ctx["admin_id"],
            execution_account_id=ctx["acc_admin_id"]
        )

        # Worker B polls
        resp_b = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "worker_b_gpu0"},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert resp_b.status_code == 200
        # Worker B gets Job B, NOT Job Admin
        pulled = resp_b.json()
        assert pulled.get("id") == job_b.id

        # Worker B polls again -> empty, must NOT get Job Admin
        resp_b_second = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "worker_b_gpu0"},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert resp_b_second.status_code == 200
        assert resp_b_second.json() == {}

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id.in_([job_b.id, job_admin.id])).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 05: Offline worker B triggers only Kaggle B push (not Admin push)
# -----------------------------------------------------------------------------
def test_05_offline_worker_triggers_only_tenant_push(setup_tenants):
    """When User B submits a job with no live worker, ensure_worker_running boots ONLY User B's kernel."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Mark all worker sessions for Account B as offline
        db.query(WorkerSession).filter(WorkerSession.execution_account_id == ctx["acc_b_id"]).update({"status": "offline"})
        db.commit()

        with patch.object(KaggleOrchestrator, "trigger_push") as mock_push:
            # Check worker alive for User B
            assert not KaggleOrchestrator.has_live_worker(db, execution_account_id=ctx["acc_b_id"])

            # Creating job triggers ensure_worker_running for User B
            job = JobService.create_job(
                db=db,
                prompt="Cần GPU của B",
                user_id=ctx["user_b_id"]
            )

            # Verify trigger_push called for account B, not admin
            mock_push.assert_called_once()
            called_kwargs = mock_push.call_args.kwargs
            assert called_kwargs.get("execution_account_id") == ctx["acc_b_id"]

            # Cleanup
            db.query(TTSJob).filter(TTSJob.id == job.id).delete()
            db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 06: User without Kaggle credentials fails cleanly with 400/failed
# -----------------------------------------------------------------------------
def test_06_unconfigured_user_fails_cleanly(setup_tenants):
    """User C without Kaggle keys cannot boot GPU and fails with informative message without touching Admin."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        with patch.object(KaggleOrchestrator, "trigger_push") as mock_push:
            job = JobService.create_job(
                db=db,
                prompt="Thử nghiệm không có key",
                user_id=ctx["user_c_id"]
            )

            assert job.status == "failed"
            assert "Bạn chưa cài đặt Kaggle API Key" in job.error_message
            assert job.execution_account_id is None
            mock_push.assert_not_called()

            # Cleanup
            db.query(TTSJob).filter(TTSJob.id == job.id).delete()
            db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 07: User B quota exceeded fails job without admin fallback
# -----------------------------------------------------------------------------
def test_07_quota_exceeded_fails_job_without_admin_fallback(setup_tenants):
    """When Kaggle returns quota exceeded for Tenant B, job fails cleanly without fallback to admin."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean existing jobs
        db.query(TTSJob).filter(TTSJob.execution_account_id == ctx["acc_b_id"]).delete()
        db.commit()

        job = JobService.create_job(
            db=db,
            prompt="Job kiểm tra quota",
            user_id=ctx["user_b_id"],
            execution_account_id=ctx["acc_b_id"]
        )
        assert job.status in ("queued", "booting_kaggle")

        # Mock subprocess.run simulating Kaggle Quota error
        fake_res = subprocess.CompletedProcess(
            args=["kaggle", "kernels", "push"],
            returncode=1,
            stdout="",
            stderr="400 - Bad Request: GPU quota exceeded. You have used all available GPU hours this week."
        )

        with patch("subprocess.run", return_value=fake_res):
            KaggleOrchestrator.trigger_push(
                db=db,
                execution_account_id=ctx["acc_b_id"],
                force=True,
                sync=True
            )

        db.refresh(job)
        assert job.status == "failed"
        assert "hết hạn mức GPU" in job.error_message or "Quota Exceeded" in job.error_message

        # Verify execution account last status
        acc_b = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == ctx["acc_b_id"]).first()
        assert acc_b.last_status == "quota_exceeded"

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id == job.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 08: User B unauthorized Kaggle key fails job without admin fallback
# -----------------------------------------------------------------------------
def test_08_unauthorized_key_fails_job_without_admin_fallback(setup_tenants):
    """When Kaggle returns 401 Unauthorized for Tenant B, job fails with unauthorized error."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean existing jobs
        db.query(TTSJob).filter(TTSJob.execution_account_id == ctx["acc_b_id"]).delete()
        db.commit()

        job = JobService.create_job(
            db=db,
            prompt="Job kiểm tra 401",
            user_id=ctx["user_b_id"],
            execution_account_id=ctx["acc_b_id"]
        )

        fake_res = subprocess.CompletedProcess(
            args=["kaggle", "kernels", "push"],
            returncode=1,
            stdout="",
            stderr="401 - Unauthorized: Invalid API Key"
        )

        with patch("subprocess.run", return_value=fake_res):
            KaggleOrchestrator.trigger_push(
                db=db,
                execution_account_id=ctx["acc_b_id"],
                force=True,
                sync=True
            )

        db.refresh(job)
        assert job.status == "failed"
        assert "Kaggle API Key không hợp lệ" in job.error_message

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id == job.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 09: Isolated workspace, metadata, and worker script generation
# -----------------------------------------------------------------------------
def test_09_isolated_workspace_and_metadata_generation(setup_tenants):
    """Verify kernel-metadata.json and worker.py are generated with tenant credentials in isolated directory."""
    ctx = setup_tenants
    acc_id = ctx["acc_b_id"]
    target_dir = Path("storage/test_workspaces") / f"account_{acc_id}"

    # Generate metadata
    meta = KaggleNotebookBuilder.generate_kernel_metadata(
        output_dir=str(target_dir),
        kaggle_username=ctx["user_b_kaggle_username"],
        kernel_slug=ctx["user_b_kernel_slug"],
        kernel_title=ctx["user_b_kernel_title"]
    )

    assert meta["id"] == f"{ctx['user_b_kaggle_username']}/{ctx['user_b_kernel_slug']}"
    meta_path = target_dir / "kernel-metadata.json"
    assert meta_path.exists()
    saved_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved_meta["id"] == f"{ctx['user_b_kaggle_username']}/{ctx['user_b_kernel_slug']}"

    # Generate worker script
    worker_script = KaggleNotebookBuilder.generate_worker_script(
        output_dir=str(target_dir),
        gateway_url="https://tts.oloka.net",
        worker_token="raw_token_xyz_123",
        worker_prefix=f"kw_{acc_id}"
    )

    script_path = target_dir / "worker.py"
    assert script_path.exists()
    content = script_path.read_text(encoding="utf-8")
    assert "raw_token_xyz_123" in content
    assert f"kw_{acc_id}" in content
    assert "lease_token" in content

    # Clean up test files
    if script_path.exists(): script_path.unlink()
    if meta_path.exists(): meta_path.unlink()
    if target_dir.exists(): target_dir.rmdir()


# -----------------------------------------------------------------------------
# TEST 10: Concurrent submissions maintain tenant isolation
# -----------------------------------------------------------------------------
def test_10_concurrent_submissions_maintain_isolation(setup_tenants):
    """Submitting multiple jobs concurrently for Tenant A and Tenant B assigns correct execution accounts."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        jobs_a = []
        jobs_b = []

        for i in range(3):
            ja = JobService.create_job(
                db=db,
                prompt=f"A prompt #{i}",
                user_id=ctx["user_a_id"]
            )
            jb = JobService.create_job(
                db=db,
                prompt=f"B prompt #{i}",
                user_id=ctx["user_b_id"]
            )
            jobs_a.append(ja)
            jobs_b.append(jb)

        for ja in jobs_a:
            assert ja.execution_account_id == ctx["acc_a_id"]
            assert ja.user_id == ctx["user_a_id"]
        for jb in jobs_b:
            assert jb.execution_account_id == ctx["acc_b_id"]
            assert jb.user_id == ctx["user_b_id"]

        # Cleanup
        all_ids = [j.id for j in jobs_a + jobs_b]
        db.query(TTSJob).filter(TTSJob.id.in_(all_ids)).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 11: Dual GPU of same account atomically pull distinct jobs
# -----------------------------------------------------------------------------
def test_11_dual_gpu_atomic_pull(setup_tenants):
    """Dual GPUs of Account B pull distinct jobs atomically without race condition or duplication."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean any preexisting jobs for account B
        db.query(TTSJob).filter(TTSJob.execution_account_id == ctx["acc_b_id"]).delete()
        db.commit()

        j1 = JobService.create_job(db=db, prompt="Dual GPU test 1", user_id=ctx["user_b_id"])
        j2 = JobService.create_job(db=db, prompt="Dual GPU test 2", user_id=ctx["user_b_id"])

        # Worker GPU 0 pulls
        p1 = JobService.pull_pending_job(
            db=db,
            worker_id="kw_b_t4_0",
            execution_account_id=ctx["acc_b_id"]
        )

        # Worker GPU 1 pulls
        p2 = JobService.pull_pending_job(
            db=db,
            worker_id="kw_b_t4_1",
            execution_account_id=ctx["acc_b_id"]
        )

        assert p1 is not None
        assert p2 is not None
        assert p1["id"] != p2["id"]
        assert p1["lease_token"] != p2["lease_token"]

        # 3rd pull should be empty
        p3 = JobService.pull_pending_job(
            db=db,
            worker_id="kw_b_t4_0",
            execution_account_id=ctx["acc_b_id"]
        )
        assert p3 is None

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id.in_([j1.id, j2.id])).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 12: Worker spoofing other tenant's worker_id rejected (403)
# -----------------------------------------------------------------------------
def test_12_worker_spoofing_worker_id_rejected(setup_tenants):
    """Worker registered by Tenant B cannot be hijacked by Tenant A."""
    ctx = setup_tenants

    # Tenant B registers worker_b_1
    reg_b = client.post(
        "/api/worker/register",
        json={"worker_id": "worker_b_registered", "gpu_index": 0, "gpu_name": "T4", "vram_total_mb": 15360},
        headers={"Authorization": f"Bearer {ctx['token_b']}"}
    )
    assert reg_b.status_code == 200

    # Tenant A attempts to register or heartbeat with Tenant B's worker_id
    reg_a_spoof = client.post(
        "/api/worker/register",
        json={"worker_id": "worker_b_registered", "gpu_index": 0, "gpu_name": "T4", "vram_total_mb": 15360},
        headers={"Authorization": f"Bearer {ctx['token_a']}"}
    )
    assert reg_a_spoof.status_code == 403
    assert "belongs to another tenant" in reg_a_spoof.json()["detail"]

    # Spoofed pull
    pull_spoof = client.post(
        "/api/worker/jobs/pull",
        json={"worker_id": "worker_b_registered"},
        headers={"Authorization": f"Bearer {ctx['token_a']}"}
    )
    assert pull_spoof.status_code == 403


# -----------------------------------------------------------------------------
# TEST 13: Worker with invalid or revoked token rejected (401/403)
# -----------------------------------------------------------------------------
def test_13_worker_invalid_or_revoked_token_rejected(setup_tenants):
    """Revoked worker token is immediately rejected with 403."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Issue a new token for testing revocation
        wtk, raw_token = KaggleAccountService.issue_worker_token(
            db, owner_user_id=ctx["user_a_id"], execution_account_id=ctx["acc_a_id"]
        )

        # Before revocation: valid
        res_valid = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "temp_worker"},
            headers={"Authorization": f"Bearer {raw_token}"}
        )
        assert res_valid.status_code == 200

        # Revoke the token
        KaggleAccountService.revoke_worker_token(db, wtk.id)

        # After revocation: rejected
        res_revoked = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "temp_worker"},
            headers={"Authorization": f"Bearer {raw_token}"}
        )
        assert res_revoked.status_code == 403

        # Fake token: rejected
        res_fake = client.post(
            "/api/worker/jobs/pull",
            json={"worker_id": "temp_worker"},
            headers={"Authorization": "Bearer fake_token_9999"}
        )
        assert res_fake.status_code == 403
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 14: Worker A cannot complete or fail Job B
# -----------------------------------------------------------------------------
def test_14_worker_a_cannot_complete_or_fail_job_b(setup_tenants):
    """Worker A attempting to complete or fail Job B is rejected with 403."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        job_b = JobService.create_job(
            db=db,
            prompt="Job bảo mật B",
            user_id=ctx["user_b_id"],
            execution_account_id=ctx["acc_b_id"]
        )

        pulled_b = JobService.pull_pending_job(
            db=db,
            worker_id="worker_b_legit",
            execution_account_id=ctx["acc_b_id"]
        )
        assert pulled_b is not None

        # Worker A tries to complete Job B
        dummy_audio = b"RIFF....WAVEfmt ...."
        resp_complete = client.post(
            f"/api/worker/jobs/{job_b.id}/complete",
            data={
                "worker_id": "worker_b_legit",
                "duration": 2.5,
                "execution_time": 0.8,
                "lease_token": pulled_b["lease_token"]
            },
            files={"audio_file": ("out.wav", dummy_audio, "audio/wav")},
            headers={"Authorization": f"Bearer {ctx['token_a']}"}  # Worker A token!
        )
        assert resp_complete.status_code == 403
        assert "tài khoản thực thi khác" in resp_complete.json()["detail"]

        # Worker A tries to fail Job B
        resp_fail = client.post(
            f"/api/worker/jobs/{job_b.id}/fail",
            json={
                "worker_id": "worker_b_legit",
                "error_message": "Fake failure",
                "lease_token": pulled_b["lease_token"]
            },
            headers={"Authorization": f"Bearer {ctx['token_a']}"}
        )
        assert resp_fail.status_code == 403

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id == job_b.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 15: Expired or missing lease token rejected (403)
# -----------------------------------------------------------------------------
def test_15_expired_or_missing_lease_rejected(setup_tenants):
    """Completing a job with missing or expired lease token returns 403."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        job = JobService.create_job(db=db, prompt="Lease test", user_id=ctx["user_b_id"])
        pulled = JobService.pull_pending_job(db=db, worker_id="worker_b_0", execution_account_id=ctx["acc_b_id"])

        dummy_audio = b"RIFF....WAVE"

        # Missing lease token
        resp_missing = client.post(
            f"/api/worker/jobs/{job.id}/complete",
            data={"worker_id": "worker_b_0", "duration": 1.0, "execution_time": 0.5},
            files={"audio_file": ("out.wav", dummy_audio, "audio/wav")},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert resp_missing.status_code == 403

        # Expired lease
        db_job = db.query(TTSJob).filter(TTSJob.id == job.id).first()
        db_job.lease_expires_at = datetime.utcnow() - timedelta(minutes=10)
        db.commit()

        resp_expired = client.post(
            f"/api/worker/jobs/{job.id}/complete",
            data={
                "worker_id": "worker_b_0",
                "duration": 1.0,
                "execution_time": 0.5,
                "lease_token": pulled["lease_token"]
            },
            files={"audio_file": ("out.wav", dummy_audio, "audio/wav")},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert resp_expired.status_code == 403
        assert "hết hạn" in resp_expired.json()["detail"]

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id == job.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 16: Lease token replay attack rejected
# -----------------------------------------------------------------------------
def test_16_lease_replay_attack_rejected(setup_tenants):
    """Once a job is completed, replaying the lease token is rejected."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        job = JobService.create_job(db=db, prompt="Replay test", user_id=ctx["user_b_id"])
        pulled = JobService.pull_pending_job(db=db, worker_id="worker_b_0", execution_account_id=ctx["acc_b_id"])

        dummy_audio = b"RIFF....WAVE"
        # First completion: SUCCESS
        r1 = client.post(
            f"/api/worker/jobs/{job.id}/complete",
            data={
                "worker_id": "worker_b_0",
                "duration": 1.0,
                "execution_time": 0.5,
                "lease_token": pulled["lease_token"]
            },
            files={"audio_file": ("out.wav", dummy_audio, "audio/wav")},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert r1.status_code == 200

        # Replay attempt: REJECTED
        r2 = client.post(
            f"/api/worker/jobs/{job.id}/complete",
            data={
                "worker_id": "worker_b_0",
                "duration": 1.0,
                "execution_time": 0.5,
                "lease_token": pulled["lease_token"]
            },
            files={"audio_file": ("out.wav", dummy_audio, "audio/wav")},
            headers={"Authorization": f"Bearer {ctx['token_b']}"}
        )
        assert r2.status_code in (400, 403)

        # Cleanup
        db.query(TTSJob).filter(TTSJob.id == job.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 17: MCP generate_speech & status check enforces tenant identity
# -----------------------------------------------------------------------------
def test_17_mcp_generate_speech_tenant_isolation(setup_tenants):
    """MCP link_account and generate_speech accurately identify tenant and report BYOK status."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        user_b = db.query(User).filter(User.id == ctx["user_b_id"]).first()
        user_c = db.query(User).filter(User.id == ctx["user_c_id"]).first()

        # 1. Check GPU status description for configured User B
        status_b = McpAuthService.get_user_gpu_status_description(db, user_b)
        assert "BYOK" in status_b
        assert user_b.kaggle_username in status_b

        # 2. Check GPU status for User C (unconfigured)
        status_c = McpAuthService.get_user_gpu_status_description(db, user_c)
        assert "Chưa cấu hình" in status_c

        # 3. Test link_account without arguments (verifying get_latest_pending_session does not crash)
        from app.mcp_server import link_account
        link_res1 = link_account()
        assert "LIÊN KẾT TÀI KHOẢN OLOKATTS" in link_res1
        # Second call reuses existing pending session
        link_res2 = link_account()
        assert "LIÊN KẾT TÀI KHOẢN OLOKATTS" in link_res2

        # 4. Pair User B with MCP session
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT-Test")
        McpAuthService.approve_pairing_session(db, sess.code, user_b.id)

        # link_account with approved code returns authenticated status with BYOK
        link_res3 = link_account(pair_code=sess.code)
        assert "TÀI KHOẢN ĐÃ ĐƯỢC XÁC THỰC THÀNH CÔNG" in link_res3
        assert "BYOK" in link_res3
        assert user_b.kaggle_username in link_res3

        # Caller resolved from pair code
        caller = McpAuthService.resolve_caller(db, pair_code=sess.code)
        assert caller.id == ctx["user_b_id"]

        # Call MCP generate_speech via in-process tool
        tool_func = mcp._tool_manager.get_tool("generate_speech")
        assert tool_func is not None

        # Call generate_speech without auth -> should request pairing
        async def _run_tool():
            return await tool_func.run(
                {"prompt": "Xin chào thế giới", "voice": "Hải Đăng"}
            )
        unauth_res = asyncio.run(_run_tool())

        # MCP OAuth now returns a structured error with mcp/www_authenticate.
        # tool.run may serialize CallToolResult to text depending on SDK version.
        text_out = unauth_res[0].text if isinstance(unauth_res, list) else str(unauth_res)
        assert ("mcp/www_authenticate" in text_out or
                "Authentication required" in text_out or
                "YÊU CẦU XÁC THỰC" in text_out)

        # Cleanup
        db.query(McpPairingSession).filter(McpPairingSession.id == sess.id).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 17b: End-to-end MCP generate_speech with paired User B & BYOK Worker
# -----------------------------------------------------------------------------
def test_17b_mcp_end_to_end_byok_speech_generation(setup_tenants):
    """End-to-end test: Paired User B calls generate_speech via MCP, job is strictly assigned to User B's execution account, admin worker cannot pull it, User B's worker completes it, and MCP returns BYOK status."""
    db = SessionLocal()
    try:
        ctx = setup_tenants
        # Clean jobs for User B and Admin
        db.query(TTSJob).filter(TTSJob.execution_account_id.in_([ctx["acc_admin_id"], ctx["acc_b_id"]])).delete()
        db.commit()

        # 1. Create and approve pairing session for User B
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT-Client")
        McpAuthService.approve_pairing_session(db, sess.code, ctx["user_b_id"])

        tool_func = mcp._tool_manager.get_tool("generate_speech")
        assert tool_func is not None

        async def _run_e2e():
            # Run generate_speech as background task
            gen_task = asyncio.create_task(
                tool_func.run({
                    "prompt": "Xin chào từ ChatGPT MCP tới User B",
                    "voice": "Hải Đăng",
                    "pair_code": sess.code,
                    "save_to_file": False
                })
            )

            # Wait briefly for job to be created and committed
            await asyncio.sleep(0.5)

            # Check that Admin Worker CANNOT pull User B's job
            resp_admin = client.post(
                "/api/worker/jobs/pull",
                json={"worker_id": "admin_worker_0"},
                headers={"Authorization": f"Bearer {ctx['token_admin']}"}
            )
            assert resp_admin.status_code == 200
            assert resp_admin.json() == {}

            # Worker B polls with Token B -> gets the job
            resp_b = client.post(
                "/api/worker/jobs/pull",
                json={"worker_id": "worker_b_0"},
                headers={"Authorization": f"Bearer {ctx['token_b']}"}
            )
            assert resp_b.status_code == 200
            pulled_b = resp_b.json()
            assert pulled_b.get("id") is not None
            job_id = pulled_b["id"]
            lease_token = pulled_b["lease_token"]

            # Worker B completes the job
            dummy_audio = b"RIFF....WAVE"
            resp_comp = client.post(
                f"/api/worker/jobs/{job_id}/complete",
                data={
                    "worker_id": "worker_b_0",
                    "duration": 2.5,
                    "execution_time": 1.2,
                    "lease_token": lease_token
                },
                files={"audio_file": ("test.wav", dummy_audio, "audio/wav")},
                headers={"Authorization": f"Bearer {ctx['token_b']}"}
            )
            assert resp_comp.status_code == 200

            # Wait for generate_speech to finish
            result = await gen_task
            return result

        result = asyncio.run(_run_e2e())
        text_out = result[0].text if isinstance(result, list) else str(result)

        assert "ĐÃ TẠO GIỌNG NÓI THÀNH CÔNG" in text_out
        assert "Kaggle Cá Nhân BYOK" in text_out
        assert ctx["user_b_kaggle_username"] in text_out
        assert "Master Admin" not in text_out

        # Cleanup
        db.query(McpPairingSession).filter(McpPairingSession.id == sess.id).delete()
        db.query(TTSJob).filter(TTSJob.execution_account_id == ctx["acc_b_id"]).delete()
        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# TEST 18: OpenAI and Web Studio endpoints enforce tenant isolation
# -----------------------------------------------------------------------------
def test_18_openai_and_studio_tenant_isolation(setup_tenants):
    """OpenAI endpoint /v1/audio/speech requires valid user token and never defaults to admin."""
    ctx = setup_tenants

    # Unauthenticated /v1/audio/speech returns 401 (NOT admin fallback!)
    resp_anon = client.post(
        "/v1/audio/speech",
        json={"input": "Hello test", "voice": "Hải Đăng"}
    )
    assert resp_anon.status_code == 401

    # Fake token returns 401
    resp_fake = client.post(
        "/v1/audio/speech",
        json={"input": "Hello test", "voice": "Hải Đăng"},
        headers={"Authorization": "Bearer fake_token_abc"}
    )
    assert resp_fake.status_code == 401


# -----------------------------------------------------------------------------
# TEST 19: Ambiguous legacy records are deactivated and never assigned to admin
# -----------------------------------------------------------------------------
def test_19_legacy_records_safe_handling(setup_tenants):
    """Legacy unowned worker sessions must be offline and ignored."""
    db = SessionLocal()
    try:
        # Reset existing worker sessions to ensure clean state
        db.query(WorkerSession).update({"status": "offline"})
        db.commit()

        legacy_id = f"legacy_worker_{secrets.token_hex(4)}"
        # Create unowned legacy worker session even if marked ready
        legacy_ws = WorkerSession(
            worker_id=legacy_id,
            owner_user_id=None,
            execution_account_id=None,
            status="ready",
            last_heartbeat_at=datetime.utcnow()
        )
        db.add(legacy_ws)
        db.commit()

        # has_live_worker for User B should NOT see this legacy worker
        assert not KaggleOrchestrator.has_live_worker(db, execution_account_id=setup_tenants["acc_b_id"])
        # has_live_worker for Admin should NOT see this legacy worker
        assert not KaggleOrchestrator.has_live_worker(db, execution_account_id=setup_tenants["acc_admin_id"])

        # Cleanup
        db.delete(legacy_ws)
        db.commit()
    finally:
        db.close()
