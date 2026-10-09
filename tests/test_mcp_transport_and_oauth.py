import hashlib
import base64
import secrets
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User, McpPairingSession, KaggleExecutionAccount
from app.services.auth_service import AuthService
from app.services.api_key_service import ApiKeyService
from app.services.mcp_auth_service import McpAuthService
from app.services.kaggle_account_service import KaggleAccountService
from app.services.job_service import JobService

client = TestClient(app)

@pytest.fixture
def setup_mcp_users():
    db = SessionLocal()
    try:
        user_a = db.query(User).filter(User.username == "mcp_usr_a").first()
        if not user_a:
            user_a = User(
                id="usr_mcp_a",
                username="mcp_usr_a",
                email="mcp_a@oloka.net",
                hashed_password=AuthService.hash_password("Pass123!"),
                role="user",
                kaggle_username="user_a_kaggle",
                kaggle_key="key_a_12345",
                is_active=True
            )
            db.add(user_a)

        user_b = db.query(User).filter(User.username == "mcp_usr_b").first()
        if not user_b:
            user_b = User(
                id="usr_mcp_b",
                username="mcp_usr_b",
                email="mcp_b@oloka.net",
                hashed_password=AuthService.hash_password("Pass123!"),
                role="user",
                kaggle_username="user_b_kaggle",
                kaggle_key="key_b_12345",
                is_active=True
            )
            db.add(user_b)
        db.commit()

        # Ensure execution accounts exist for both
        KaggleAccountService.get_execution_account_for_user(db, user_a)
        KaggleAccountService.get_execution_account_for_user(db, user_b)

        return {"user_a_id": user_a.id, "user_b_id": user_b.id}
    finally:
        db.close()


def test_mcp_link_account_idempotent_per_transport_session():
    """Test A: Same Client - verify calling link_account multiple times from same transport reuses code without bloat."""
    db = SessionLocal()
    try:
        transport_id = "hdr_test_transport_idempotent_123"
        db.query(McpPairingSession).filter(McpPairingSession.transport_session_id == transport_id).delete()
        db.commit()

        # 1. First call creates session
        sess1 = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id=transport_id)
        assert sess1.code.startswith("OLK-")
        assert sess1.status == "pending"

        # 2. Second call with same transport reuses existing pending session
        sess2 = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id=transport_id)
        assert sess2.id == sess1.id
        assert sess2.code == sess1.code
    finally:
        db.close()


def test_mcp_link_account_cross_client_transport_isolation():
    """Test B: Two Clients - verify distinct MCP clients receive isolated sessions and distinct codes."""
    db = SessionLocal()
    try:
        t_id_a = "hdr_client_alice_999"
        t_id_b = "hdr_client_bob_888"

        db.query(McpPairingSession).filter(McpPairingSession.transport_session_id.in_([t_id_a, t_id_b])).delete()
        db.commit()

        sess_a = McpAuthService.create_pairing_session(db, client_name="ChatGPT-Alice", transport_session_id=t_id_a)
        sess_b = McpAuthService.create_pairing_session(db, client_name="ChatGPT-Bob", transport_session_id=t_id_b)

        assert sess_a.id != sess_b.id
        assert sess_a.code != sess_b.code
        assert sess_a.transport_session_id == t_id_a
        assert sess_b.transport_session_id == t_id_b

        lookup_a = McpAuthService.get_pending_session_for_transport(db, t_id_a)
        lookup_b = McpAuthService.get_pending_session_for_transport(db, t_id_b)
        assert lookup_a.id == sess_a.id
        assert lookup_b.id == sess_b.id
    finally:
        db.close()


def test_session_token_hashing_and_masking():
    """Verify long-term session credentials are saved as SHA-256 hashes, not plaintext secrets."""
    db = SessionLocal()
    try:
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id="hdr_test_hash")
        raw_token = getattr(sess, "_raw_token", None)
        assert raw_token is not None
        assert raw_token.startswith("mcp_tok_")

        expected_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
        assert sess.session_token_hash == expected_hash
        assert sess.session_token.endswith("...") or len(sess.session_token) < len(raw_token)

        found = McpAuthService.get_session_by_token(db, raw_token)
        assert found is not None
        assert found.id == sess.id
    finally:
        db.close()


def test_ambient_transport_resolution_after_browser_approval(setup_mcp_users):
    """Test C1: Ambient Transport Binding - once approved, transport resolves caller automatically."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_a_id"]
        transport_id = "hdr_transport_auto_resolve_555"

        db.query(McpPairingSession).filter(McpPairingSession.transport_session_id == transport_id).delete()
        db.commit()

        # Step 1: AI Client connects, gets pending pairing session
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id=transport_id)
        assert sess.status == "pending"

        # Step 2: Before approval, transport resolution returns None
        caller_before = McpAuthService.resolve_caller(db, transport_session_id=transport_id)
        assert caller_before is None

        # Step 3: User approves pairing code in browser
        ok, msg, _ = McpAuthService.approve_pairing_session(db, sess.code, user_id)
        assert ok is True

        # Step 4: Subsequent tool call from same client (no parameters passed by LLM)
        caller_after = McpAuthService.resolve_caller(db, transport_session_id=transport_id)
        assert caller_after is not None
        assert caller_after.id == user_id
    finally:
        db.close()


def test_pair_code_resolution_across_dynamic_transport_ids(setup_mcp_users):
    """Test C1b: Dynamic MCP Transports (e.g. ChatGPT cloud workers) - pair_code resolves across transport changes."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_a_id"]
        t_id_turn1 = "hdr_chatgpt_worker_turn1_abc111"
        t_id_turn2 = "hdr_chatgpt_worker_turn2_def222"

        db.query(McpPairingSession).filter(McpPairingSession.transport_session_id.in_([t_id_turn1, t_id_turn2])).delete()
        db.commit()

        # Step 1: Turn 1 creates pairing session under transport 1
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id=t_id_turn1)
        code = sess.code

        # Step 2: User approves pairing code in browser
        ok, _, _ = McpAuthService.approve_pairing_session(db, code, user_id)
        assert ok is True

        # Step 3: Turn 2 arrives from dynamic transport 2 with explicit pair_code
        # MUST resolve successfully even though t_id_turn2 != t_id_turn1
        caller_turn2 = McpAuthService.resolve_caller(
            db,
            pair_code=code,
            transport_session_id=t_id_turn2
        )
        assert caller_turn2 is not None
        assert caller_turn2.id == user_id

        # Step 4: Verify transport session was updated to transport 2
        refreshed_sess = db.query(McpPairingSession).filter(McpPairingSession.code == code).first()
        assert refreshed_sess.transport_session_id == t_id_turn2

        # Step 5: Turn 3 from transport 2 arrives with NO parameters, resolves ambiently
        caller_turn3 = McpAuthService.resolve_caller(db, transport_session_id=t_id_turn2)
        assert caller_turn3 is not None
        assert caller_turn3.id == user_id
    finally:
        db.close()


def test_approved_session_multi_tenant_job_isolation(setup_mcp_users):
    """Test C2: User B approved -> JobService associates strictly with User B and Account B, zero admin fallback."""
    db = SessionLocal()
    try:
        user_b_id = setup_mcp_users["user_b_id"]
        user_b = db.query(User).filter(User.id == user_b_id).first()
        acc_b = KaggleAccountService.get_execution_account_for_user(db, user_b)
        assert acc_b is not None

        # Create job with resolved User B
        job = JobService.create_job(
            db=db,
            prompt="Kiểm tra cách ly đa người dùng",
            voice_type="preset",
            voice_id="Hải Đăng",
            user_id=user_b.id
        )

        assert job.user_id == user_b.id
        assert job.execution_account_id == acc_b.id
        assert job.execution_account_id != "admin"
        assert job.status in ("queued", "booting_kaggle")
    finally:
        db.close()


def test_missing_authentication_fails_closed():
    """Test E: Missing authentication fails closed, refuses to create GPU job or boot Kaggle."""
    db = SessionLocal()
    try:
        # Resolving anonymous caller returns None
        caller = McpAuthService.resolve_caller(db, api_key=None, session_token=None, transport_session_id=None, pair_code=None)
        assert caller is None

        # JobService without execution account / user fails closed
        job = JobService.create_job(
            db=db,
            prompt="Test unauthenticated fail closed",
            voice_type="preset",
            voice_id="Hải Đăng",
            user_id=None
        )
        assert job.status == "failed"
        assert "Kaggle" in job.error_message
    finally:
        db.close()


def test_token_revocation_disables_access(setup_mcp_users):
    """Test D: Revoking pairing session or token disables future access."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_a_id"]
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT-Revoke")
        McpAuthService.approve_pairing_session(db, sess.code, user_id)

        # Before revocation: valid
        caller1 = McpAuthService.resolve_caller(db, pair_code=sess.code)
        assert caller1 is not None
        assert caller1.id == user_id

        # Revoke session
        revoked = McpAuthService.revoke_pairing_session(db, sess.code, user_id=user_id)
        assert revoked is True

        # After revocation: rejected
        caller2 = McpAuthService.resolve_caller(db, pair_code=sess.code)
        assert caller2 is None
    finally:
        db.close()


def test_oauth2_rfc_full_suite_with_pkce(setup_mcp_users):
    """Test F: Full OAuth 2.1 RFC 9728, RFC 8414, RFC 7591, and RFC 7636 PKCE flow."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_b_id"]
        user = db.query(User).filter(User.id == user_id).first()

        # 1. Protected Resource Metadata (RFC 9728)
        res_pr = client.get("/.well-known/oauth-protected-resource")
        assert res_pr.status_code == 200
        pr_meta = res_pr.json()
        assert "authorization_servers" in pr_meta
        assert "resource" in pr_meta

        # 2. Authorization Server Metadata (RFC 8414)
        res_as = client.get("/.well-known/oauth-authorization-server")
        assert res_as.status_code == 200
        as_meta = res_as.json()
        assert "authorization_endpoint" in as_meta
        assert "token_endpoint" in as_meta
        assert "registration_endpoint" in as_meta
        assert "S256" in as_meta["code_challenge_methods_supported"]

        # OpenID configuration alias
        res_oidc = client.get("/.well-known/openid-configuration")
        assert res_oidc.status_code == 200

        # 3. Dynamic Client Registration (RFC 7591)
        res_reg = client.post("/oauth/register", json={
            "client_name": "ChatGPT Remote Connector",
            "redirect_uris": ["https://chatgpt.com/api/mcp/oauth/callback"]
        })
        assert res_reg.status_code == 201
        reg_data = res_reg.json()
        client_id = reg_data["client_id"]
        redirect_uri = "https://chatgpt.com/api/mcp/oauth/callback"

        # 4. PKCE Setup (RFC 7636)
        code_verifier = secrets.token_urlsafe(40)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        # 5. Authorization Page (renders consent form when logged in)
        auth_cookie_token = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_cookie_token)

        res_page = client.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "test_state_123",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        })
        assert res_page.status_code == 200
        assert "Cấp Quyền" in res_page.text

        # 6. User submits Consent Form -> receives authorization code via redirect
        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "test_state_123",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": "mcp:all speech:generate"
        }, follow_redirects=False)
        assert res_consent.status_code == 303
        redirect_target = res_consent.headers["location"]
        assert redirect_target.startswith(redirect_uri)
        assert "code=olk_auth_" in redirect_target

        import urllib.parse
        parsed = urllib.parse.urlparse(redirect_target)
        q_params = urllib.parse.parse_qs(parsed.query)
        auth_code = q_params["code"][0]

        # 7. PKCE Mismatch rejection
        res_token_bad = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": "wrong_code_verifier_123"
        })
        assert res_token_bad.status_code == 400
        assert "PKCE" in res_token_bad.json()["error_description"]

        # 8. Token Exchange with valid code_verifier
        res_token = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier
        })
        assert res_token.status_code == 200
        token_payload = res_token.json()
        assert "access_token" in token_payload
        assert "refresh_token" in token_payload
        access_tok = token_payload["access_token"]
        refresh_tok = token_payload["refresh_token"]

        # Validate that access_token resolves to User B
        caller = McpAuthService.resolve_caller(db, api_key=access_tok)
        assert caller is not None
        assert caller.id == user.id

        # 9. Refresh Token Exchange
        res_refresh = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok
        })
        assert res_refresh.status_code == 200
        refreshed_data = res_refresh.json()
        assert "access_token" in refreshed_data
        new_access_tok = refreshed_data["access_token"]
        caller_refreshed = McpAuthService.resolve_caller(db, api_key=new_access_tok)
        assert caller_refreshed is not None
        assert caller_refreshed.id == user.id

        # 10. Token Revocation
        res_revoke = client.post("/oauth/revoke", data={"token": new_access_tok})
        assert res_revoke.status_code == 200

    finally:
        client.cookies.clear()
        db.close()


@pytest.mark.anyio
async def test_tools_list_schema_matches_implementation():
    """Test G: Schema Compatibility - verify tools/list schema contains expected parameters."""
    from app.mcp_server import mcp
    tools = await mcp.list_tools()
    tool_map = {t.name: t for t in tools}

    assert "list_voices" in tool_map
    assert "link_account" in tool_map
    assert "generate_speech" in tool_map
    assert "get_system_status" in tool_map
    assert "estimate_speech_duration" in tool_map

    # Check generate_speech schema
    gen_props = tool_map["generate_speech"].inputSchema.get("properties", {})
    assert "prompt" in gen_props
    assert "pair_code" in gen_props
    assert "voice" in gen_props
    assert "speed" in gen_props
    assert "temperature" in gen_props
    assert "api_key" in gen_props

    # Check link_account schema
    link_props = tool_map["link_account"].inputSchema.get("properties", {})
    assert "pair_code" in link_props
