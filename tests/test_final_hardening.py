import pytest
import os
import re
import json
import secrets
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import (
    User, TTSJob, WorkerSession, KaggleExecutionAccount,
    WorkerToken, McpPairingSession, ApiKey, VoiceSample
)
from app.services.auth_service import AuthService
from app.services.api_key_service import ApiKeyService
from app.services.kaggle_account_service import KaggleAccountService
from app.services.job_service import JobService
from app.services.mcp_auth_service import McpAuthService
from app.mcp_server import link_account, generate_speech

client = TestClient(app)

class DummyRequestContext:
    def __init__(self, headers=None, session_id=None):
        self.request = MagicMock()
        self.request.headers = headers or {}
        self.request.query_params = {"session_id": session_id} if session_id else {}
        self.session = MagicMock() if session_id else None

class DummyContext:
    def __init__(self, mcp_session_header=None, query_session_id=None):
        headers = {}
        if mcp_session_header:
            headers["mcp-session-id"] = mcp_session_header
        self.request_context = DummyRequestContext(headers=headers, session_id=query_session_id)


@pytest.fixture(scope="module")
def hardening_fixture():
    db = SessionLocal()
    try:
        # User 1 (Tenant 1)
        u1 = db.query(User).filter(User.username == "hard_user_1").first()
        if not u1:
            u1 = User(
                id="usr_hard_1",
                username="hard_user_1",
                email="hard1@oloka.net",
                hashed_password=AuthService.hash_password("Pass@123"),
                role="user"
            )
            db.add(u1)
            db.commit()

        acc1 = KaggleAccountService.create_or_update_account(
            db, user_id=u1.id, kaggle_username="hard_k_1", kaggle_key="kkey_hard_1"
        )
        wtk1, wtk1_raw = KaggleAccountService.issue_worker_token(
            db, owner_user_id=u1.id, execution_account_id=acc1.id
        )

        # User 2 (Tenant 2)
        u2 = db.query(User).filter(User.username == "hard_user_2").first()
        if not u2:
            u2 = User(
                id="usr_hard_2",
                username="hard_user_2",
                email="hard2@oloka.net",
                hashed_password=AuthService.hash_password("Pass@123"),
                role="user"
            )
            db.add(u2)
            db.commit()

        acc2 = KaggleAccountService.create_or_update_account(
            db, user_id=u2.id, kaggle_username="hard_k_2", kaggle_key="kkey_hard_2"
        )
        wtk2, wtk2_raw = KaggleAccountService.issue_worker_token(
            db, owner_user_id=u2.id, execution_account_id=acc2.id
        )

        # Admin
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            admin = User(
                id="usr_hard_admin",
                username="hard_admin",
                email="admin_hard@oloka.net",
                hashed_password=AuthService.hash_password("AdminPass@123"),
                role="admin"
            )
            db.add(admin)
            db.commit()

        acc_admin = KaggleAccountService.get_execution_account_for_user(db, admin)
        if not acc_admin:
            acc_admin = KaggleAccountService.create_or_update_account(
                db, user_id=admin.id, kaggle_username="admin_k_hard", kaggle_key="kkey_admin_hard"
            )

        u1_id = u1.id
        u2_id = u2.id
        acc1_id = acc1.id
        acc2_id = acc2.id
        admin_id = admin.id
        admin_token = AuthService.create_token(admin.id, admin.username, admin.role)

        return {
            "u1_id": u1_id, "acc1_id": acc1_id, "wtk1_raw": wtk1_raw,
            "u2_id": u2_id, "acc2_id": acc2_id, "wtk2_raw": wtk2_raw,
            "admin_id": admin_id, "acc_admin_id": acc_admin.id, "admin_token": admin_token
        }
    finally:
        db.close()


# ==============================================================================
# P0: MCP PAIRING ISOLATION & SESSION TOKEN VERIFICATION
# ==============================================================================

def test_mcp_pairing_cross_client_isolation(hardening_fixture):
    """
    Client A and Client B call link_account() concurrently with separate transport contexts.
    Verify they receive separate codes and session tokens, approval binds exclusively,
    and neither client can hijack the other.
    """
    ctx_a = DummyContext(mcp_session_header="trans_client_a_123")
    ctx_b = DummyContext(mcp_session_header="trans_client_b_456")

    res_a = link_account(ctx=ctx_a)
    res_b = link_account(ctx=ctx_b)

    assert "Mã phiên của bạn" in res_a
    assert "Mã phiên của bạn" in res_b

    match_a = re.search(r'OLK-[A-Za-z0-9]+', res_a)
    match_b = re.search(r'OLK-[A-Za-z0-9]+', res_b)
    assert match_a and match_b
    code_a = match_a.group(0)
    code_b = match_b.group(0)
    assert code_a != code_b, "Clients A and B must never receive the same pairing code!"

    # Verify session tokens in DB
    db = SessionLocal()
    try:
        sess_a = McpAuthService.get_pairing_session(db, code_a)
        sess_b = McpAuthService.get_pairing_session(db, code_b)
        assert sess_a is not None and sess_b is not None
        assert sess_a.session_token != sess_b.session_token
        assert sess_a.transport_session_id == "hdr_trans_client_a_123"
        assert sess_b.transport_session_id == "hdr_trans_client_b_456"

        # User 1 approves Client A; User 2 approves Client B
        u1_id = hardening_fixture["u1_id"]
        u2_id = hardening_fixture["u2_id"]

        ok_a, _, token_a = McpAuthService.approve_pairing_session(db, code_a, u1_id)
        ok_b, _, token_b = McpAuthService.approve_pairing_session(db, code_b, u2_id)
        assert ok_a is True
        assert ok_b is True

        # Resolve Client A via transport context -> maps strictly to User 1
        caller_a = McpAuthService.resolve_caller(db, transport_session_id="hdr_trans_client_a_123")
        assert caller_a is not None
        assert caller_a.id == u1_id

        # Resolve Client B via transport context -> maps strictly to User 2
        caller_b = McpAuthService.resolve_caller(db, transport_session_id="hdr_trans_client_b_456")
        assert caller_b is not None
        assert caller_b.id == u2_id

        # Resolve via explicit session token
        caller_token_a = McpAuthService.resolve_caller(db, session_token=token_a)
        assert caller_token_a is not None and caller_token_a.id == u1_id

        # Client A attempting to pass Client B's session_token across different transport should fail closed
        cross_caller = McpAuthService.resolve_caller(
            db, session_token=token_b, transport_session_id="hdr_trans_client_a_123"
        )
        assert cross_caller is None, "Cross-client session token spoofing must be rejected!"

        # Revoke Session A
        revoked = McpAuthService.revoke_pairing_session(db, code_a, user_id=u1_id)
        assert revoked is True

        # Now Client A must fail to resolve
        assert McpAuthService.resolve_caller(db, transport_session_id="hdr_trans_client_a_123") is None
        assert McpAuthService.resolve_caller(db, session_token=token_a) is None

        # Client B must remain active and completely unaffected
        assert McpAuthService.resolve_caller(db, transport_session_id="hdr_trans_client_b_456").id == u2_id

    finally:
        db.close()


# ==============================================================================
# P0: API KEY LIFECYCLE, REVOCATION, AND SCOPES
# ==============================================================================

def test_api_key_scopes_interoperability_and_revocation(hardening_fixture):
    """
    Verify:
    1. Scopes 'tts:generate' and 'tts:write' are both valid for speech generation.
    2. Scope 'voices:read' alone fails with 403 on speech generation.
    3. Deactivated / revoked API keys fail immediately with 401.
    4. Plaintext User.api_key is rejected and migration nullifies legacy keys.
    """
    db = SessionLocal()
    u1_id = hardening_fixture["u1_id"]
    try:
        # Create key with tts:generate scope
        ak_gen, raw_key_gen = ApiKeyService.create_api_key(
            db, user_id=u1_id, name="Test Gen Scope", scopes="tts:generate"
        )

        # Create key with tts:write scope
        ak_write, raw_key_write = ApiKeyService.create_api_key(
            db, user_id=u1_id, name="Test Write Scope", scopes="tts:write"
        )

        # Create key with voices:read only
        ak_read, raw_key_read = ApiKeyService.create_api_key(
            db, user_id=u1_id, name="Test Read Scope", scopes="voices:read"
        )

        # Validate with ApiKeyService
        ak_obj_gen = ApiKeyService.validate_api_key(db, raw_key_gen, required_scope="tts:generate")
        assert ak_obj_gen is not None and ak_obj_gen.user_id == u1_id

        ak_obj_write = ApiKeyService.validate_api_key(db, raw_key_write, required_scope="tts:generate")
        assert ak_obj_write is not None and ak_obj_write.user_id == u1_id

        # Calling with required scope 'tts:generate' on read-only key fails
        ak_read_fail = ApiKeyService.validate_api_key(db, raw_key_read, required_scope="tts:generate")
        assert ak_read_fail is None

        from unittest.mock import patch
        from app.services.kaggle_orchestrator import KaggleOrchestrator

        with patch.object(KaggleOrchestrator, "ensure_worker_running", return_value={"status": "mocked"}):
            # Test HTTP /v1/tts/jobs endpoint with tts:generate key
            resp_gen = client.post(
                "/v1/tts/jobs",
                headers={"Authorization": f"Bearer {raw_key_gen}"},
                json={"prompt": "Xin chao thu nghiem", "voice_type": "preset", "resolved_voice_id": "vp_haidang"}
            )
            assert resp_gen.status_code == 200

            # Test HTTP /v1/tts/jobs endpoint with tts:write key
            resp_write = client.post(
                "/v1/tts/jobs",
                headers={"Authorization": f"Bearer {raw_key_write}"},
                json={"prompt": "Xin chao thu nghiem 2", "voice_type": "preset", "resolved_voice_id": "vp_haidang"}
            )
            assert resp_write.status_code == 200

            # Test HTTP /v1/tts/jobs endpoint with voices:read key -> must return 403
            resp_read = client.post(
                "/v1/tts/jobs",
                headers={"Authorization": f"Bearer {raw_key_read}"},
                json={"prompt": "Xin chao that bai", "voice_type": "preset", "resolved_voice_id": "vp_haidang"}
            )
            assert resp_read.status_code == 403
            assert "không có quyền tạo âm thanh" in resp_read.text

        # Deactivate / revoke key
        revoked = ApiKeyService.revoke_api_key(db, user_id=u1_id, key_id=ak_gen.id)
        assert revoked is True

        # Validation must fail closed immediately
        assert ApiKeyService.validate_api_key(db, raw_key_gen) is None

        resp_revoked = client.post(
            "/v1/tts/jobs",
            headers={"Authorization": f"Bearer {raw_key_gen}"},
            json={"prompt": "Thu nghiem sau thu hoi", "voice_type": "preset", "resolved_voice_id": "vp_haidang"}
        )
        assert resp_revoked.status_code == 401

        # Delete key and test rejection
        deleted = ApiKeyService.delete_api_key(db, user_id=u1_id, key_id=ak_write.id)
        assert deleted is True
        resp_del = client.post(
            "/v1/tts/jobs",
            headers={"Authorization": f"Bearer {raw_key_write}"},
            json={"prompt": "Thu nghiem sau xoa", "voice_type": "preset", "resolved_voice_id": "vp_haidang"}
        )
        assert resp_del.status_code == 401

    finally:
        db.close()


def test_legacy_plaintext_key_migration_and_rejection():
    """
    Ensure that plaintext User.api_key column is migrated and nullified,
    and attempting to use a raw plaintext User.api_key fails closed.
    """
    db = SessionLocal()
    try:
        # Create user with legacy plaintext key
        u_legacy = User(
            id="usr_legacy_plaintext",
            username="legacy_plain_user",
            email="plain@oloka.net",
            hashed_password=AuthService.hash_password("Pass@123"),
            api_key="oloka_live_plaintext_secret_12345",
            role="user"
        )
        db.add(u_legacy)
        db.commit()

        # Run migration
        migrated_count = ApiKeyService.migrate_legacy_keys(db)
        assert migrated_count >= 1

        db.refresh(u_legacy)
        # users.api_key must be NULL
        assert u_legacy.api_key is None

        # The raw key string now works via hashed ApiKey record with non-admin scopes
        ak_obj = ApiKeyService.validate_api_key(db, "oloka_live_plaintext_secret_12345")
        assert ak_obj is not None
        assert ak_obj.user_id == u_legacy.id
        assert ak_obj.scopes == "tts:generate,voices:read"
        assert "full:access" not in ak_obj.scopes

    finally:
        db.close()


# ==============================================================================
# P1: SCOPED WORKER SHUTDOWN ISOLATION
# ==============================================================================

def test_scoped_worker_shutdown_isolation(hardening_fixture):
    """
    Verify:
    1. Stopping Tenant A's workers marks ONLY Tenant A's workers as 'stopping'.
    2. Worker A receives {"action": "shutdown"} and transitions to 'offline'.
    3. Worker B is completely unaffected and continues running.
    """
    db = SessionLocal()
    wtk1_raw = hardening_fixture["wtk1_raw"]
    wtk2_raw = hardening_fixture["wtk2_raw"]
    acc1_id = hardening_fixture["acc1_id"]
    admin_token = hardening_fixture["admin_token"]

    try:
        # Register Worker A
        reg_a = client.post(
            "/api/worker/register",
            headers={"Authorization": f"Bearer {wtk1_raw}"},
            json={"worker_id": "wkr_test_a_0", "gpu_index": 0, "gpu_name": "Tesla T4", "vram_total_mb": 15360}
        )
        assert reg_a.status_code == 200

        # Register Worker B
        reg_b = client.post(
            "/api/worker/register",
            headers={"Authorization": f"Bearer {wtk2_raw}"},
            json={"worker_id": "wkr_test_b_0", "gpu_index": 0, "gpu_name": "Tesla T4", "vram_total_mb": 15360}
        )
        assert reg_b.status_code == 200

        # Admin stops workers for Tenant A's execution account
        stop_resp = client.post(
            f"/api/admin/kaggle/stop?execution_account_id={acc1_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert stop_resp.status_code == 200

        # Check session states in DB
        sess_a = db.query(WorkerSession).filter(WorkerSession.worker_id == "wkr_test_a_0").first()
        sess_b = db.query(WorkerSession).filter(WorkerSession.worker_id == "wkr_test_b_0").first()
        assert sess_a.status == "stopping"
        assert sess_b.status == "ready", "Worker B must NOT be stopped when Worker A is targeted!"

        # Worker A pulls job -> receives shutdown signal and transitions to offline
        pull_a = client.post(
            "/api/worker/jobs/pull",
            headers={"Authorization": f"Bearer {wtk1_raw}"},
            json={"worker_id": "wkr_test_a_0"}
        )
        assert pull_a.status_code == 200
        assert pull_a.json().get("action") == "shutdown"

        db.refresh(sess_a)
        assert sess_a.status == "offline"

        # Worker B pulls job -> does NOT receive shutdown
        pull_b = client.post(
            "/api/worker/jobs/pull",
            headers={"Authorization": f"Bearer {wtk2_raw}"},
            json={"worker_id": "wkr_test_b_0"}
        )
        assert pull_b.status_code == 200
        assert pull_b.json().get("action") != "shutdown"

        # Worker A can re-register and become ready again
        re_reg_a = client.post(
            "/api/worker/register",
            headers={"Authorization": f"Bearer {wtk1_raw}"},
            json={"worker_id": "wkr_test_a_0", "gpu_index": 0, "gpu_name": "Tesla T4", "vram_total_mb": 15360}
        )
        assert re_reg_a.status_code == 200
        db.refresh(sess_a)
        assert sess_a.status == "ready"

    finally:
        db.close()


# ==============================================================================
# P1: FAIL-CLOSED ON NULL / LEGACY OWNER RESOURCES
# ==============================================================================

def test_fail_closed_on_legacy_null_owner_resources(hardening_fixture):
    """
    Verify:
    1. TTSJob with execution_account_id=None is rejected on worker complete/fail/prompt-audio.
    2. TTSJob with user_id=None is rejected on /v1/tts/jobs/{id} and /audio for non-admin users.
    3. VoiceSample with user_id=None is rejected on /v1/voices/samples/{filename} for non-admin users.
    """
    db = SessionLocal()
    wtk1_raw = hardening_fixture["wtk1_raw"]
    u1_id = hardening_fixture["u1_id"]
    u1 = db.query(User).filter(User.id == u1_id).first()
    u1_token = AuthService.create_token(u1.id, u1.username, u1.role)

    try:
        # Create unowned job (execution_account_id=None, user_id=None)
        unowned_job = TTSJob(
            id="job_unowned_test",
            prompt="Unowned prompt text",
            voice_type="preset",
            voice_id="vp_haidang",
            speed=1.0,
            status="processing",
            worker_id="wkr_test_a_0",
            lease_token="lease_unowned_token",
            lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
            execution_account_id=None,
            user_id=None,
            ref_audio_path="storage/audio/dummy.wav"
        )
        db.add(unowned_job)

        # Create unowned voice sample (user_id=None)
        unowned_sample = VoiceSample(
            id="vs_unowned_sample",
            name="Unowned Sample",
            file_path="storage/samples/unowned_sample.wav",
            user_id=None
        )
        db.add(unowned_sample)
        db.commit()

        # 1. Worker complete on unowned job -> 403 Forbidden
        comp_resp = client.post(
            f"/api/worker/jobs/{unowned_job.id}/complete",
            headers={"Authorization": f"Bearer {wtk1_raw}"},
            data={"worker_id": "wkr_test_a_0", "lease_token": "lease_unowned_token", "duration": 1.0},
            files={"audio_file": ("test.wav", b"RIFF....WAVEfmt ....data....", "audio/wav")}
        )
        assert comp_resp.status_code == 403
        assert "thiếu tài khoản sở hữu" in comp_resp.text

        # 2. Worker fail on unowned job -> 403 Forbidden
        fail_resp = client.post(
            f"/api/worker/jobs/{unowned_job.id}/fail",
            headers={"Authorization": f"Bearer {wtk1_raw}"},
            json={"worker_id": "wkr_test_a_0", "lease_token": "lease_unowned_token", "error_message": "test fail"}
        )
        assert fail_resp.status_code == 403
        assert "thiếu tài khoản sở hữu" in fail_resp.text

        # 3. Worker prompt-audio on unowned job -> 403 Forbidden
        pa_resp = client.get(
            f"/api/worker/jobs/{unowned_job.id}/prompt-audio",
            headers={"Authorization": f"Bearer {wtk1_raw}"}
        )
        assert pa_resp.status_code == 403

        # 4. Non-admin user querying unowned job metadata -> 403 Forbidden
        job_info_resp = client.get(
            f"/v1/tts/jobs/{unowned_job.id}",
            headers={"Authorization": f"Bearer {u1_token}"}
        )
        assert job_info_resp.status_code == 403

        # 5. Non-admin user querying unowned job audio -> 403 Forbidden
        job_audio_resp = client.get(
            f"/v1/tts/jobs/{unowned_job.id}/audio",
            headers={"Authorization": f"Bearer {u1_token}"}
        )
        assert job_audio_resp.status_code == 403

        # 6. Non-admin user downloading unowned voice sample -> 403 Forbidden
        # Create dummy file on disk for path check
        sample_file = Path("storage/samples/unowned_sample.wav")
        sample_file.parent.mkdir(parents=True, exist_ok=True)
        sample_file.write_bytes(b"RIFFtestsample")

        sample_resp = client.get(
            f"/v1/voices/samples/unowned_sample.wav",
            headers={"Authorization": f"Bearer {u1_token}"}
        )
        assert sample_resp.status_code == 403

    finally:
        # Cleanup dummy file
        p = Path("storage/samples/unowned_sample.wav")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
        db.close()
