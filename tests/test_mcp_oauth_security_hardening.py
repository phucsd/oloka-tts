import json
import base64
import hashlib
import secrets
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User, OAuthClient, OAuthToken, McpPairingSession, TTSJob, WorkerSession, WorkerToken
from app.services.auth_service import AuthService
from app.services.mcp_auth_service import McpAuthService
from app.services.kaggle_account_service import KaggleAccountService
from app.services.job_service import JobService

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_and_setup_security_test_db():
    db = SessionLocal()
    try:
        # Create test users
        u_alice = db.query(User).filter(User.username == "sec_alice").first()
        if not u_alice:
            u_alice = User(
                id="usr_sec_alice",
                username="sec_alice",
                email="alice@oloka.net",
                hashed_password=AuthService.hash_password("AlicePass123!"),
                role="user",
                kaggle_username="alice_kaggle",
                kaggle_key="key_alice_999",
                is_active=True
            )
            db.add(u_alice)

        u_bob = db.query(User).filter(User.username == "sec_bob").first()
        if not u_bob:
            u_bob = User(
                id="usr_sec_bob",
                username="sec_bob",
                email="bob@oloka.net",
                hashed_password=AuthService.hash_password("BobPass123!"),
                role="user",
                kaggle_username="bob_kaggle",
                kaggle_key="key_bob_888",
                is_active=True
            )
            db.add(u_bob)
        db.commit()

        # Seed KaggleExecutionAccount for both
        KaggleAccountService.get_execution_account_for_user(db, u_alice)
        KaggleAccountService.get_execution_account_for_user(db, u_bob)

        # Register official test client
        cli = db.query(OAuthClient).filter(OAuthClient.client_id == "mcp_cli_legit_app").first()
        if not cli:
            cli = OAuthClient(
                client_id="mcp_cli_legit_app",
                client_name="Legit ChatGPT Connector",
                redirect_uris=json.dumps([
                    "https://chatgpt.com/api/mcp/oauth/callback",
                    "http://127.0.0.1/callback"
                ]),
                token_endpoint_auth_method="none",
                created_at=datetime.utcnow()
            )
            db.add(cli)
        db.commit()

        yield {"alice": u_alice, "bob": u_bob, "client": cli}
    finally:
        client.cookies.clear()
        db.close()


def generate_pkce_pair():
    verifier = secrets.token_urlsafe(50)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ==============================================================================
# SECTION 2: REDIRECT URI VALIDATION (RFC 9700 / RFC 8252 / RFC 6749)
# ==============================================================================

def test_redirect_uri_exact_match_pass(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    _, challenge = generate_pkce_pair()
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    })
    assert res.status_code == 200
    assert "Cấp Quyền" in res.text


def test_redirect_uri_diff_domain_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    _, challenge = generate_pkce_pair()
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://attacker.com/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    })
    assert res.status_code == 400
    assert "redirect_uri" in res.text


def test_redirect_uri_prefixed_domain_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    _, challenge = generate_pkce_pair()
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com.evil.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    })
    assert res.status_code == 400
    assert "redirect_uri" in res.text


def test_redirect_uri_unregistered_subdomain_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    _, challenge = generate_pkce_pair()
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://sub.chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    })
    assert res.status_code == 400


def test_redirect_uri_change_between_authorize_and_consent_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    _, challenge = generate_pkce_pair()
    # Submit consent form tampering with redirect_uri
    res = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://evil.com/hijack",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp:all"
    })
    assert res.status_code == 400
    assert "invalid_redirect_uri" in res.text or "URI mismatch" in res.text


def test_client_id_nonexistent_reject(clean_and_setup_security_test_db):
    _, challenge = generate_pkce_pair()
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_ghost_client_never_registered",
        "redirect_uri": "https://chatgpt.com/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    })
    assert res.status_code == 400
    assert "invalid_client" in res.text


def test_dcr_invalid_uri_reject():
    # Attempt registering dangerous javascript: or fragment URI
    res1 = client.post("/oauth/register", json={
        "client_name": "Malicious App",
        "redirect_uris": ["javascript:alert(1)"]
    })
    assert res1.status_code == 400
    assert "invalid_redirect_uri" in res1.json()["error"]

    res2 = client.post("/oauth/register", json={
        "client_name": "Fragment App",
        "redirect_uris": ["https://chatgpt.com/callback#token=123"]
    })
    assert res2.status_code == 400

    res3 = client.post("/oauth/register", json={
        "client_name": "Plain HTTP App",
        "redirect_uris": ["http://insecure-domain.com/callback"]
    })
    assert res3.status_code == 400


# ==============================================================================
# SECTION 3: PKCE S256 ENFORCEMENT (RFC 7636)
# ==============================================================================

def test_pkce_missing_challenge_reject(clean_and_setup_security_test_db):
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback"
    })
    assert res.status_code == 400
    assert "code_challenge is required" in res.text


def test_pkce_downgrade_plain_reject(clean_and_setup_security_test_db):
    res = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": "plain_secret_plain_secret_plain_secret_plain_secret",
        "code_challenge_method": "plain"
    })
    assert res.status_code == 400
    assert "S256" in res.text


def test_pkce_missing_verifier_at_token_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    verifier, challenge = generate_pkce_pair()
    res_consent = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp:all"
    }, follow_redirects=False)
    assert res_consent.status_code == 303
    code = res_consent.headers["location"].split("code=")[1].split("&")[0]

    # Call /oauth/token without code_verifier
    res_tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback"
    })
    assert res_tok.status_code == 400
    assert "code_verifier" in res_tok.json()["error_description"]


def test_pkce_invalid_verifier_format_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    verifier, challenge = generate_pkce_pair()
    res_consent = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    }, follow_redirects=False)
    code = res_consent.headers["location"].split("code=")[1].split("&")[0]

    # Too short verifier (< 43 chars)
    res_short = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_verifier": "too_short_123"
    })
    assert res_short.status_code == 400
    assert "format invalid" in res_short.json()["error_description"]


def test_pkce_verifier_mismatch_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    verifier, challenge = generate_pkce_pair()
    wrong_verifier, _ = generate_pkce_pair()

    res_consent = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    }, follow_redirects=False)
    code = res_consent.headers["location"].split("code=")[1].split("&")[0]

    res_tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_verifier": wrong_verifier
    })
    assert res_tok.status_code == 400
    assert "verification failed" in res_tok.json()["error_description"]


# ==============================================================================
# SECTION 4: CODE BINDING & ATOMIC REPLAY PROTECTION
# ==============================================================================

def test_cross_client_code_exchange_reject(clean_and_setup_security_test_db):
    db = SessionLocal()
    try:
        # Create second registered client
        cli_bob = OAuthClient(
            client_id="mcp_cli_bob_app",
            client_name="Bob App",
            redirect_uris=json.dumps(["https://bob.com/callback"]),
            token_endpoint_auth_method="none",
            created_at=datetime.utcnow()
        )
        db.add(cli_bob)
        db.commit()

        user = clean_and_setup_security_test_db["alice"]
        auth_tok = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_tok)

        verifier, challenge = generate_pkce_pair()
        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": "mcp_cli_legit_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }, follow_redirects=False)
        code = res_consent.headers["location"].split("code=")[1].split("&")[0]

        # Bob's client attempts exchanging Alice's code
        res_exchange = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "mcp_cli_bob_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_verifier": verifier
        })
        assert res_exchange.status_code == 400
        assert "Client ID mismatch" in res_exchange.json()["error_description"]
    finally:
        db.close()


def test_redirect_uri_mismatch_at_token_reject(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    verifier, challenge = generate_pkce_pair()
    res_consent = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    }, follow_redirects=False)
    code = res_consent.headers["location"].split("code=")[1].split("&")[0]

    # Provide different redirect_uri at token exchange
    res_tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "http://127.0.0.1/callback",
        "code_verifier": verifier
    })
    assert res_tok.status_code == 400
    assert "redirect_uri mismatch" in res_tok.json()["error_description"]


def test_code_replay_atomic_rejection(clean_and_setup_security_test_db):
    user = clean_and_setup_security_test_db["alice"]
    auth_tok = AuthService.create_token(user.id, user.username, user.role)
    client.cookies.set("session_token", auth_tok)

    verifier, challenge = generate_pkce_pair()
    res_consent = client.post("/oauth/authorize/consent", data={
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256"
    }, follow_redirects=False)
    code = res_consent.headers["location"].split("code=")[1].split("&")[0]

    # First exchange: SUCCESS
    res_first = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_verifier": verifier
    })
    assert res_first.status_code == 200
    assert "access_token" in res_first.json()

    # Replay attack: REJECTED ATOMICALLY
    res_second = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "mcp_cli_legit_app",
        "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
        "code_verifier": verifier
    })
    assert res_second.status_code == 400
    assert "already been consumed" in res_second.json()["error_description"] or "invalid" in res_second.json()["error_description"]


# ==============================================================================
# SECTION 5: ACCESS TOKEN REVOCATION & REFRESH TOKEN ROTATION
# ==============================================================================

def test_access_token_revocation_instant_invalidation(clean_and_setup_security_test_db):
    db = SessionLocal()
    try:
        user = clean_and_setup_security_test_db["alice"]
        auth_tok = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_tok)

        verifier, challenge = generate_pkce_pair()
        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": "mcp_cli_legit_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }, follow_redirects=False)
        code = res_consent.headers["location"].split("code=")[1].split("&")[0]

        res_tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "mcp_cli_legit_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_verifier": verifier
        })
        access_tok = res_tok.json()["access_token"]

        # Valid before revocation
        resolved_before = McpAuthService.resolve_caller(db, api_key=access_tok)
        assert resolved_before is not None
        assert resolved_before.id == user.id

        # Revoke access token
        res_rev = client.post("/oauth/revoke", data={"token": access_tok})
        assert res_rev.status_code == 200

        # Invalid immediately after revocation
        resolved_after = McpAuthService.resolve_caller(db, api_key=access_tok)
        assert resolved_after is None
    finally:
        db.close()


def test_refresh_token_rotation_and_reuse_detection_revokes_family(clean_and_setup_security_test_db):
    db = SessionLocal()
    try:
        user = clean_and_setup_security_test_db["bob"]
        auth_tok = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_tok)

        verifier, challenge = generate_pkce_pair()
        res_consent = client.post("/oauth/authorize/consent", data={
            "client_id": "mcp_cli_legit_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }, follow_redirects=False)
        code = res_consent.headers["location"].split("code=")[1].split("&")[0]

        res_tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "mcp_cli_legit_app",
            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
            "code_verifier": verifier
        })
        tok_data = res_tok.json()
        atk_1 = tok_data["access_token"]
        rtk_1 = tok_data["refresh_token"]

        # Legitimate rotation 1: Use rtk_1 -> receives atk_2 & rtk_2
        res_rot1 = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": rtk_1
        })
        assert res_rot1.status_code == 200
        tok_data2 = res_rot1.json()
        atk_2 = tok_data2["access_token"]
        rtk_2 = tok_data2["refresh_token"]
        assert atk_2 != atk_1
        assert rtk_2 != rtk_1

        # atk_2 is currently valid
        assert McpAuthService.resolve_caller(db, api_key=atk_2) is not None

        # ATTACK: Reusing old rtk_1!
        res_reuse = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": rtk_1
        })
        assert res_reuse.status_code == 400
        assert "reuse detected" in res_reuse.json()["error_description"].lower()

        # Entire token family must be terminated: atk_2 is now revoked!
        assert McpAuthService.resolve_caller(db, api_key=atk_2) is None
    finally:
        db.close()


def test_web_jwt_not_accepted_as_mcp_oauth_token(clean_and_setup_security_test_db):
    db = SessionLocal()
    try:
        user = clean_and_setup_security_test_db["alice"]
        # Generic web login JWT
        web_jwt = AuthService.create_token(user.id, user.username, user.role, expires_in_days=1)

        # resolve_caller must NOT treat generic web JWT as an MCP Bearer token
        resolved = McpAuthService.resolve_caller(db, api_key=web_jwt)
        assert resolved is None
    finally:
        db.close()


# ==============================================================================
# SECTION 6: MCP PROTOCOL AUTH CHALLENGE & DISCOVERY
# ==============================================================================

def test_mcp_discovery_public_access():
    with TestClient(app) as tc:
        # 1. initialize should succeed publicly (HTTP 200)
        res_init = tc.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "TestClient", "version": "1.0"}
                }
            }
        )
        assert res_init.status_code == 200
        assert "serverInfo" in res_init.text

        sess_id = res_init.headers.get("mcp-session-id")

        # 2. tools/list should succeed publicly (HTTP 200)
        headers = {"Accept": "application/json, text/event-stream"}
        if sess_id:
            headers["mcp-session-id"] = sess_id
        res_tools = tc.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            }
        )
        assert res_tools.status_code == 200
        assert "generate_speech" in res_tools.text


def test_mcp_protected_tool_401_challenge_with_www_authenticate():
    """Public initialize + protected tool returns MCP OAuth challenge without launching GPU."""
    # A context-managed TestClient starts FastMCP's session manager correctly.
    with TestClient(app) as tc:
        headers = {"Accept": "application/json, text/event-stream"}
        init = tc.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 9, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "OAuthTest", "version": "1.0"}}
        })
        assert init.status_code == 200
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
        res = tc.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "generate_speech", "arguments": {"prompt": "Xin chào thế giới"}}
        })
        # Current transport sends a valid MCP CallToolResult (HTTP 200 + tool error).
        assert res.status_code == 200
        auth_hdr = res.headers.get("www-authenticate", "")
        assert "Bearer" in auth_hdr
        assert "resource_metadata=" in auth_hdr
        assert "speech:generate" in auth_hdr
        result = res.json()["result"]
        assert result["isError"] is True
        assert "mcp/www_authenticate" in result.get("_meta", {})


def test_mcp_invalid_token_401_challenge():
    # Request with invalid / expired / fake Bearer token must return HTTP 401
    res = client.post("/mcp", headers={"Authorization": "Bearer olk_atk_completely_fake_token_123"}, json={
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "generate_speech",
            "arguments": {"prompt": "Xin chào"}
        }
    })
    assert res.status_code == 401
    assert "www-authenticate" in res.headers
    auth_hdr = res.headers["www-authenticate"]
    assert 'error="invalid_token"' in auth_hdr


# ==============================================================================
# SECTION 9: MCP SCOPES & BYOK MULTI-TENANT ISOLATION
# ==============================================================================

def test_mcp_scope_restriction_enforcement(clean_and_setup_security_test_db):
    db = SessionLocal()
    try:
        user = clean_and_setup_security_test_db["alice"]
        # Create token with only 'voices:read' scope
        tok_raw = f"olk_atk_{secrets.token_urlsafe(32)}"
        restricted_tok = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="access_token",
            token_hash=McpAuthService.hash_token(tok_raw),
            token_masked="olk_atk_...",
            user_id=user.id,
            client_id="mcp_cli_legit_app",
            scope="voices:read",  # Lacks speech:generate!
            family_id="fam_restricted",
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        db.add(restricted_tok)
        db.commit()

        from app.mcp_server import generate_speech
        import asyncio

        # Calling generate_speech with restricted token must reject due to scope
        res = asyncio.run(generate_speech(prompt="Xin chào", api_key=tok_raw))
        # MCP OAuth errors are CallToolResult objects, not plain strings.
        assert res.isError is True
        assert "speech:generate" in str(res.structuredContent)
        assert "mcp/www_authenticate" in res.meta
        assert "insufficient_scope" in str(res.meta["mcp/www_authenticate"])
    finally:
        db.close()


def test_mcp_oauth_byok_tenant_isolation(clean_and_setup_security_test_db, monkeypatch):
    import asyncio
    from app.services.kaggle_orchestrator import KaggleOrchestrator

    # Mock Kaggle CLI kernel push so unit test doesn't call real Kaggle API
    monkeypatch.setattr(KaggleOrchestrator, "ensure_worker_running", lambda *args, **kwargs: True)

    # Mock asyncio.sleep in generate_speech to mark job completed immediately
    orig_sleep = asyncio.sleep
    async def mock_sleep(secs):
        sim_db = SessionLocal()
        try:
            j = sim_db.query(TTSJob).order_by(TTSJob.created_at.desc()).first()
            if j and j.status != "completed":
                j.status = "completed"
                j.audio_path = "./static/samples/presets/vp_haidang.wav"
                j.duration = 2.5
                sim_db.commit()
        finally:
            sim_db.close()
        await orig_sleep(0.01)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    db = SessionLocal()
    try:
        user_alice = clean_and_setup_security_test_db["alice"]
        user_bob = clean_and_setup_security_test_db["bob"]

        # Alice's OAuth token
        tok_alice_raw = f"olk_atk_{secrets.token_urlsafe(32)}"
        tok_alice = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="access_token",
            token_hash=McpAuthService.hash_token(tok_alice_raw),
            token_masked="olk_atk_...",
            user_id=user_alice.id,
            client_id="mcp_cli_legit_app",
            scope="mcp:all speech:generate",
            family_id="fam_alice",
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        db.add(tok_alice)
        db.commit()

        # Alice generates speech via OAuth token
        from app.mcp_server import generate_speech
        res = asyncio.run(generate_speech(prompt="Xin chào từ Alice", api_key=tok_alice_raw))
        assert "ĐÃ TẠO GIỌNG NÓI THÀNH CÔNG" in res
        assert "@alice_kaggle" in res

        # Check latest job created: MUST have Alice's execution_account_id
        acc_alice = KaggleAccountService.get_execution_account_for_user(db, user_alice)
        acc_bob = KaggleAccountService.get_execution_account_for_user(db, user_bob)
        assert acc_alice.id != acc_bob.id

        latest_job = db.query(TTSJob).order_by(TTSJob.created_at.desc()).first()
        assert latest_job is not None
        assert latest_job.execution_account_id == acc_alice.id
        assert latest_job.execution_account_id != acc_bob.id

        # Verify Bob's worker CANNOT pull Alice's job
        token_bob_record = db.query(WorkerToken).filter(WorkerToken.execution_account_id == acc_bob.id).first()
        if not token_bob_record:
            token_bob_record, raw_wtk_bob = KaggleAccountService.issue_worker_token(db, owner_user_id=user_bob.id, execution_account_id=acc_bob.id)
        else:
            raw_wtk_bob = "test_bob_worker_token"
            token_bob_record.token_hash = KaggleAccountService.hash_token(raw_wtk_bob)
            db.commit()

        pulled = JobService.pull_pending_job(db, worker_id="bob_t4_0", execution_account_id=acc_bob.id)
        # Bob has no pending jobs, so pulled must be None (never pulls Alice's job)
        assert pulled is None
    finally:
        db.close()
