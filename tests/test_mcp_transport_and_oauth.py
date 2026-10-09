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
def anyio_backend():
    return "asyncio"

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


# ==============================================================================
# SECTION 10 MANDATORY TEST SUITE (TEST 01 - TEST 08)
# ==============================================================================

def test_01_mcp_discovery_and_metadata():
    """Test 01: MCP Discovery & Metadata (RFC 9728 & RFC 8414, iss parameter, CIMD support)."""
    # 1. Protected Resource Metadata (RFC 9728)
    res_pr = client.get("/.well-known/oauth-protected-resource")
    assert res_pr.status_code == 200
    pr_meta = res_pr.json()
    assert "authorization_servers" in pr_meta
    assert "resource" in pr_meta
    assert pr_meta["resource"].endswith("/mcp")
    assert "speech:generate" in pr_meta["scopes_supported"]

    # Alias /mcp endpoint
    res_pr_mcp = client.get("/.well-known/oauth-protected-resource/mcp")
    assert res_pr_mcp.status_code == 200
    assert res_pr_mcp.json() == pr_meta

    # 2. Authorization Server Metadata (RFC 8414)
    res_as = client.get("/.well-known/oauth-authorization-server")
    assert res_as.status_code == 200
    as_meta = res_as.json()
    assert "issuer" in as_meta
    assert "authorization_endpoint" in as_meta
    assert "token_endpoint" in as_meta
    assert "registration_endpoint" in as_meta
    assert "revocation_endpoint" in as_meta
    assert as_meta["response_types_supported"] == ["code"]
    assert "authorization_code" in as_meta["grant_types_supported"]
    assert "refresh_token" in as_meta["grant_types_supported"]
    assert "S256" in as_meta["code_challenge_methods_supported"]
    # RFC 9207 & CIMD support flags
    assert as_meta.get("authorization_response_iss_parameter_supported") is True
    assert as_meta.get("client_id_metadata_document_supported") is True

    # Alias /mcp endpoint for AS
    res_as_mcp = client.get("/.well-known/oauth-authorization-server/mcp")
    assert res_as_mcp.status_code == 200
    assert res_as_mcp.json() == as_meta


@pytest.mark.anyio
async def test_02_mcp_tools_list_security_schemes():
    """Test 02: MCP tools/list — securitySchemes declared per tool."""
    from app.mcp_server import mcp
    tools = await mcp.list_tools()
    tool_map = {t.name: t for t in tools}

    assert "generate_speech" in tool_map
    gen_tool = tool_map["generate_speech"]
    sec_schemes = getattr(gen_tool, "securitySchemes", None)
    assert sec_schemes is not None
    assert sec_schemes == [{"type": "oauth2", "scopes": ["speech:generate"]}]

    for pub_name in ["list_voices", "estimate_speech_duration", "link_account", "get_system_status"]:
        assert pub_name in tool_map
        pub_schemes = getattr(tool_map[pub_name], "securitySchemes", None)
        assert pub_schemes is not None
        assert pub_schemes == [{"type": "noauth"}]

    # Also test via Streamable HTTP POST /mcp tools/list
    payload = {
        "jsonrpc": "2.0",
        "id": "test_list_tools_http",
        "method": "tools/list",
        "params": {}
    }
    with TestClient(app) as test_c:
        init_res = test_c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "test_init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test_client", "version": "1.0"}
                }
            },
            headers={"content-type": "application/json", "accept": "application/json"}
        )
        assert init_res.status_code == 200
        sid = init_res.headers.get("mcp-session-id")

        headers = {"content-type": "application/json", "accept": "application/json"}
        if sid:
            headers["mcp-session-id"] = sid

        res = test_c.post("/mcp", json=payload, headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert "result" in data
        http_tools = {t["name"]: t for t in data["result"]["tools"]}
        assert "generate_speech" in http_tools
        assert http_tools["generate_speech"].get("securitySchemes") == [
            {"type": "oauth2", "scopes": ["speech:generate"]}
        ]
        assert http_tools["list_voices"].get("securitySchemes") == [{"type": "noauth"}]


@pytest.mark.anyio
async def test_03_mcp_tool_auth_challenge_unauthenticated():
    """Test 03: MCP Tool Auth Challenge (Unauthenticated generate_speech returns challenge without creating job)."""
    db = SessionLocal()
    try:
        from app.models import TTSJob
        initial_job_count = db.query(TTSJob).count()

        # 1. Direct tool call without auth returns CallToolResult with isError=True and meta challenge
        from app.mcp_server import generate_speech
        res = await generate_speech(prompt="Test unauthenticated speech generation")
        from mcp.types import CallToolResult
        assert isinstance(res, CallToolResult)
        assert res.isError is True
        meta_dict = getattr(res, "meta", None) or getattr(res, "_meta", None)
        if meta_dict is None and hasattr(res, "model_dump"):
            meta_dict = res.model_dump(by_alias=True).get("_meta", {})
        assert meta_dict is not None
        assert "mcp/www_authenticate" in meta_dict
        auth_challenges = meta_dict["mcp/www_authenticate"]
        assert any("Bearer resource_metadata=" in c for c in auth_challenges)
        assert any('error="invalid_token"' in c for c in auth_challenges)

        # Confirm zero jobs were created and no Kaggle GPU booted
        assert db.query(TTSJob).count() == initial_job_count

        # 2. HTTP-level challenge via /mcp JSON-RPC tools/call
        with TestClient(app) as test_c:
            init_res = test_c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "test_init_challenge",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test_client", "version": "1.0"}
                    }
                },
                headers={"content-type": "application/json", "accept": "application/json"}
            )
            assert init_res.status_code == 200
            sid = init_res.headers.get("mcp-session-id")
            hdrs = {"content-type": "application/json", "accept": "application/json"}
            if sid:
                hdrs["mcp-session-id"] = sid

            payload = {
                "jsonrpc": "2.0",
                "id": "test_call_unauth",
                "method": "tools/call",
                "params": {
                    "name": "generate_speech",
                    "arguments": {
                        "prompt": "Test unauth via HTTP"
                    }
                }
            }
            resp = test_c.post("/mcp", json=payload, headers=hdrs)
            assert resp.status_code == 200
            assert "www-authenticate" in resp.headers
            assert "oauth-protected-resource" in resp.headers["www-authenticate"]
            res_data = resp.json()
            assert "result" in res_data
            result_obj = res_data["result"]
            assert result_obj.get("isError") is True
            m = result_obj.get("_meta") or result_obj.get("meta", {})
            assert "mcp/www_authenticate" in m
    finally:
        db.close()


def _perform_oauth_flow(setup_mcp_users):
    """Helper for Test 04 and Test 05 to execute full OAuth code exchange."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_b_id"]
        user = db.query(User).filter(User.id == user_id).first()

        cimd_client_id = "https://chatgpt.com/connector/oloka-tts"
        redirect_uri = "https://chatgpt.com/api/mcp/oauth/callback"

        code_verifier = secrets.token_urlsafe(40)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        auth_cookie_token = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_cookie_token)

        res_page = client.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": cimd_client_id,
            "redirect_uri": redirect_uri,
            "state": "state_chatgpt_test",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        })
        assert res_page.status_code == 200
        assert "Cấp Quyền" in res_page.text

        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": cimd_client_id,
            "redirect_uri": redirect_uri,
            "state": "state_chatgpt_test",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": "speech:generate"
        }, follow_redirects=False)
        assert res_consent.status_code == 303
        redirect_target = res_consent.headers["location"]
        assert redirect_target.startswith(redirect_uri)
        assert "code=olk_auth_" in redirect_target
        assert "iss=" in redirect_target

        import urllib.parse
        parsed = urllib.parse.urlparse(redirect_target)
        q_params = urllib.parse.parse_qs(parsed.query)
        auth_code = q_params["code"][0]
        assert "iss" in q_params

        res_tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": cimd_client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "resource": "https://tts.oloka.net/mcp"
        })
        assert res_tok.status_code == 200
        tok_data = res_tok.json()
        assert tok_data.get("token_type") == "Bearer"
        assert tok_data.get("access_token", "").startswith("olk_atk_")
        assert tok_data.get("refresh_token", "").startswith("olk_rtk_")

        access_tok = tok_data["access_token"]
        caller = McpAuthService.resolve_caller(db, api_key=access_tok)
        assert caller is not None
        assert caller.id == user.id

        return {
            "access_token": access_tok,
            "refresh_token": tok_data["refresh_token"],
            "user_id": user.id
        }
    finally:
        client.cookies.clear()
        db.close()


def test_04_full_oauth_flow_with_pkce_and_iss(setup_mcp_users):
    """Test 04: Full OAuth Flow (CIMD, Authorization with PKCE S256, Code Exchange, Token Generation, iss in redirect)."""
    tokens = _perform_oauth_flow(setup_mcp_users)
    assert tokens["access_token"].startswith("olk_atk_")
    assert tokens["refresh_token"].startswith("olk_rtk_")


def test_05_session_persistence_refresh_and_revocation(setup_mcp_users):
    """Test 05: Session Persistence (Token refresh, Token revocation, reuse detection)."""
    tokens = _perform_oauth_flow(setup_mcp_users)
    access_tok = tokens["access_token"]
    refresh_tok = tokens["refresh_token"]
    user_id = tokens["user_id"]

    db = SessionLocal()
    try:
        # Refresh token exchange
        res_refresh = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok
        })
        assert res_refresh.status_code == 200
        ref_data = res_refresh.json()
        new_access_tok = ref_data["access_token"]
        new_refresh_tok = ref_data["refresh_token"]
        assert new_access_tok != access_tok
        assert new_refresh_tok != refresh_tok

        # Verify new access token works
        caller = McpAuthService.resolve_caller(db, api_key=new_access_tok)
        assert caller is not None
        assert caller.id == user_id

        # Token Revocation
        res_revoke = client.post("/oauth/revoke", data={"token": new_access_tok})
        assert res_revoke.status_code == 200

        # Subsequent resolution of revoked token fails
        caller_revoked, _, err = McpAuthService.validate_bearer_token(db, new_access_tok)
        assert caller_revoked is None
        assert err == "token_revoked"

        # Refresh token reuse detection (RFC 6749 Section 10.4)
        res_reuse = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok
        })
        assert res_reuse.status_code == 400
        assert "reuse" in res_reuse.json().get("error_description", "").lower()
    finally:
        db.close()


def test_06_attack_vectors_downgrade_redirect_replay(setup_mcp_users):
    """Test 06: Attack Vectors (PKCE downgrade, invalid redirect_uri, replay attack)."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_a_id"]
        user = db.query(User).filter(User.id == user_id).first()
        auth_cookie_token = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_cookie_token)

        # Register a valid client for testing attack vectors
        res_reg = client.post("/oauth/register", json={
            "client_name": "Attack Vector Test Client",
            "redirect_uris": ["https://app.example.com/oauth/callback"]
        })
        assert res_reg.status_code == 201
        reg_cli_id = res_reg.json()["client_id"]
        valid_redirect = "https://app.example.com/oauth/callback"

        # 1. PKCE Downgrade attempt: code_challenge_method="plain"
        res_plain = client.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg_cli_id,
            "redirect_uri": valid_redirect,
            "code_challenge": "plain_secret_string_1234567890123456789012345678901234567890",
            "code_challenge_method": "plain"
        })
        assert res_plain.status_code == 400
        assert "S256" in res_plain.text or "plain" in res_plain.text

        # 2. Missing code_challenge attempt
        res_no_pkce = client.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg_cli_id,
            "redirect_uri": valid_redirect
        })
        assert res_no_pkce.status_code == 400

        # 3. Invalid / Untrusted redirect_uri attempt (Fail Closed)
        res_bad_uri = client.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg_cli_id,
            "redirect_uri": "https://evil.com/callback",
            "code_challenge": secrets.token_urlsafe(32),
            "code_challenge_method": "S256"
        })
        assert res_bad_uri.status_code == 400

        # 4. Replay attack: Reusing an authorization code
        code_verifier = secrets.token_urlsafe(40)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": reg_cli_id,
            "redirect_uri": valid_redirect,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        }, follow_redirects=False)
        assert res_consent.status_code == 303
        import urllib.parse
        parsed = urllib.parse.urlparse(res_consent.headers["location"])
        code = urllib.parse.parse_qs(parsed.query)["code"][0]

        # First consumption: OK
        res_tok1 = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": reg_cli_id,
            "redirect_uri": valid_redirect,
            "code_verifier": code_verifier
        })
        assert res_tok1.status_code == 200

        # Second consumption (Replay): MUST fail with 400
        res_tok2 = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": reg_cli_id,
            "redirect_uri": valid_redirect,
            "code_verifier": code_verifier
        })
        assert res_tok2.status_code == 400
        assert "invalid_grant" in res_tok2.json().get("error", "")
    finally:
        client.cookies.clear()
        db.close()


def test_07_multitenant_byok_isolation(setup_mcp_users):
    """Test 07: Multi-tenant BYOK Isolation (No fallback to Admin GPU, BYOK enforced)."""
    db = SessionLocal()
    try:
        user_b_id = setup_mcp_users["user_b_id"]
        user_b = db.query(User).filter(User.id == user_b_id).first()
        acc_b = KaggleAccountService.get_execution_account_for_user(db, user_b)
        assert acc_b is not None

        # User B job routes to User B execution account, never admin
        job_b = JobService.create_job(
            db=db,
            prompt="Kiểm tra cách ly đa người dùng User B",
            voice_type="preset",
            voice_id="Hải Đăng",
            user_id=user_b.id
        )
        assert job_b.user_id == user_b.id
        assert job_b.execution_account_id == acc_b.id
        assert job_b.execution_account_id != "admin"

        # User C: No Kaggle BYOK configured
        user_c = db.query(User).filter(User.username == "mcp_usr_c_nobyk").first()
        if not user_c:
            user_c = User(
                id="usr_mcp_c_nobyk",
                username="mcp_usr_c_nobyk",
                email="mcp_c@oloka.net",
                hashed_password=AuthService.hash_password("Pass123!"),
                role="user",
                kaggle_username=None,
                kaggle_key=None,
                is_active=True
            )
            db.add(user_c)
            db.commit()

        # Job creation for User C fails closed without fallback to admin
        job_c = JobService.create_job(
            db=db,
            prompt="Kiểm tra User C không có BYOK",
            voice_type="preset",
            voice_id="Hải Đăng",
            user_id=user_c.id
        )
        assert job_c.status == "failed"
        assert "Kaggle" in job_c.error_message
    finally:
        db.close()


@pytest.mark.anyio
async def test_08_backward_compatibility_link_account(setup_mcp_users):
    """Test 08: Backward Compatibility (link_account tool and OLK pairing code)."""
    db = SessionLocal()
    try:
        user_a_id = setup_mcp_users["user_a_id"]

        # 1. Anonymous client calls link_account tool
        from app.mcp_server import link_account
        link_res = link_account()
        assert "OLK-" in link_res
        assert "/mcp/pair?code=" in link_res

        # Extract generated pairing code
        import re
        match = re.search(r"OLK-[A-Z0-9]+", link_res)
        assert match is not None
        olk_code = match.group(0)

        # 2. User approves pairing session via browser
        ok, msg, token = McpAuthService.approve_pairing_session(db, olk_code, user_a_id)
        assert ok is True

        # 3. Legacy client generates speech with pair_code
        from app.models import TTSJob
        from app.mcp_server import generate_speech
        gen_res = await generate_speech(
            prompt="Kiểm tra tương thích ngược mã OLK",
            pair_code=olk_code
        )
        # Should NOT return an OAuth challenge error
        if hasattr(gen_res, "isError"):
            assert gen_res.isError is False
        else:
            assert isinstance(gen_res, str)
            assert "Tạo tác vụ thành công" in gen_res or "Lỗi xử lý từ GPU worker" in gen_res or "Lỗi khởi chạy Kaggle" in gen_res or "chưa hoàn tất" in gen_res

        # Verify job was created in DB and associated with User A
        job = db.query(TTSJob).filter(TTSJob.user_id == user_a_id).order_by(TTSJob.created_at.desc()).first()
        assert job is not None
    finally:
        db.close()

