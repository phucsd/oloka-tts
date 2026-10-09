import hashlib
import time
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User, McpPairingSession
from app.services.auth_service import AuthService
from app.services.api_key_service import ApiKeyService
from app.services.mcp_auth_service import McpAuthService

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
                is_active=True
            )
            db.add(user_b)
        db.commit()
        return {"user_a_id": user_a.id, "user_b_id": user_b.id}
    finally:
        db.close()


def test_mcp_link_account_idempotent_per_transport_session():
    """Verify that calling link_account multiple times from the same MCP transport returns the exact same pairing code."""
    db = SessionLocal()
    try:
        transport_id = "hdr_test_transport_idempotent_123"
        # Clean previous sessions
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
    """Verify that different MCP clients receive completely isolated sessions and distinct codes."""
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

        # Looking up pending session for transport A never returns transport B's session
        lookup_a = McpAuthService.get_pending_session_for_transport(db, t_id_a)
        lookup_b = McpAuthService.get_pending_session_for_transport(db, t_id_b)
        assert lookup_a.id == sess_a.id
        assert lookup_b.id == sess_b.id
    finally:
        db.close()


def test_session_token_hashing_and_masking():
    """Verify that long-term session credentials are saved as SHA-256 hashes, not plaintext secrets."""
    db = SessionLocal()
    try:
        sess = McpAuthService.create_pairing_session(db, client_name="ChatGPT", transport_session_id="hdr_test_hash")
        raw_token = getattr(sess, "_raw_token", None)
        assert raw_token is not None
        assert raw_token.startswith("mcp_tok_")

        # Session in DB has session_token_hash matching SHA-256 of raw_token
        expected_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
        assert sess.session_token_hash == expected_hash

        # Stored session_token column is masked, not the full secret
        assert sess.session_token.endswith("...") or len(sess.session_token) < len(raw_token)

        # Lookup by raw token finds the session via hash match
        found = McpAuthService.get_session_by_token(db, raw_token)
        assert found is not None
        assert found.id == sess.id
    finally:
        db.close()


def test_ambient_transport_resolution_after_browser_approval(setup_mcp_users):
    """Verify that once approved in browser, subsequent calls from that same transport automatically resolve without any parameters."""
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


def test_bearer_api_key_and_jwt_resolution(setup_mcp_users):
    """Verify that Oloka API Key and JWT Auth Token work at the MCP transport layer."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_a_id"]
        user = db.query(User).filter(User.id == user_id).first()

        # 1. Oloka API Key resolution
        api_key_obj, raw_key = ApiKeyService.create_api_key(db, user_id=user_id, name="MCP Test Key")
        caller_from_key = McpAuthService.resolve_caller(db, api_key=raw_key)
        assert caller_from_key is not None
        assert caller_from_key.id == user_id

        # 2. JWT Auth Token resolution
        jwt_token = AuthService.create_token(user.id, user.username, user.role, expires_in_days=1)
        caller_from_jwt = McpAuthService.resolve_caller(db, api_key=jwt_token)
        assert caller_from_jwt is not None
        assert caller_from_jwt.id == user_id

        # 3. Invalid token returns None
        caller_invalid = McpAuthService.resolve_caller(db, api_key="invalid_token_xyz")
        assert caller_invalid is None
    finally:
        db.close()


def test_oauth2_endpoints_flow(setup_mcp_users):
    """Verify RFC 6749 and RFC 8414 OAuth 2.0 flow for ChatGPT and remote AI agents."""
    db = SessionLocal()
    try:
        user_id = setup_mcp_users["user_b_id"]
        user = db.query(User).filter(User.id == user_id).first()

        # 1. Check OAuth 2.0 metadata endpoint
        res_meta = client.get("/.well-known/oauth-authorization-server")
        assert res_meta.status_code == 200
        meta = res_meta.json()
        assert "authorization_endpoint" in meta
        assert "token_endpoint" in meta
        assert "code" in meta["response_types_supported"]

        # 2. Authorization request without login -> redirects to login
        res_auth_unlogged = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "chatgpt_mcp",
                "redirect_uri": "https://chatgpt.com/oauth/callback",
                "state": "state123"
            },
            follow_redirects=False
        )
        assert res_auth_unlogged.status_code == 303
        assert "/login" in res_auth_unlogged.headers["location"]

        # 3. Authorization request with user session cookie (cookie name is 'session_token')
        auth_cookie_token = AuthService.create_token(user.id, user.username, user.role)
        client.cookies.set("session_token", auth_cookie_token)

        res_auth_logged = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "chatgpt_mcp",
                "redirect_uri": "https://chatgpt.com/oauth/callback",
                "state": "state123"
            },
            follow_redirects=False
        )
        assert res_auth_logged.status_code == 303
        redirect_url = res_auth_logged.headers["location"]
        assert redirect_url.startswith("https://chatgpt.com/oauth/callback")
        assert "code=olk_auth_" in redirect_url
        assert "state=state123" in redirect_url

        # Extract code
        import urllib.parse
        parsed = urllib.parse.urlparse(redirect_url)
        q_params = urllib.parse.parse_qs(parsed.query)
        auth_code = q_params["code"][0]

        # 4. Exchange code for access token via POST /oauth/token
        res_token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "chatgpt_mcp",
                "redirect_uri": "https://chatgpt.com/oauth/callback"
            }
        )
        assert res_token.status_code == 200
        token_data = res_token.json()
        assert "access_token" in token_data
        assert token_data["token_type"].lower() == "bearer"
        assert token_data["expires_in"] == 2592000

        # Validate that access_token resolves to User B
        access_tok = token_data["access_token"]
        caller_resolved = McpAuthService.resolve_caller(db, api_key=access_tok)
        assert caller_resolved is not None
        assert caller_resolved.id == user.id

        # 5. Code cannot be reused (one-time code protection)
        res_token_reused = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "chatgpt_mcp"
            }
        )
        assert res_token_reused.status_code == 400
        assert res_token_reused.json()["error"] == "invalid_grant"

    finally:
        client.cookies.clear()
        db.close()
