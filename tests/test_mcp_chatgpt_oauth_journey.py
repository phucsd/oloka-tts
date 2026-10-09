"""ChatGPT MCP OAuth acceptance tests: no network, no real Kaggle or production data.

Run: python -m pytest tests/test_mcp_chatgpt_oauth_journey.py -v
Tests use the isolated SQLite database provided by tests/conftest.py.
"""
import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import User, OAuthToken, TTSJob
from app.services.auth_service import AuthService
from app.services.mcp_auth_service import McpAuthService
from app.services.kaggle_account_service import KaggleAccountService
from app.services.job_service import JobService

REDIRECT = "https://chatgpt.com/api/mcp/oauth/callback"
AUDIENCE = "https://tts.oloka.net/mcp"


@pytest.fixture
def two_tenants():
    """Create two disposable ordinary users with independent fake Kaggle credentials."""
    suffix = secrets.token_hex(5)
    db = SessionLocal()
    try:
        for tag in ("a", "b"):
            uname = f"journey_{tag}_{suffix}"
            user = User(
                username=uname,
                email=f"{uname}@example.test",
                hashed_password=AuthService.hash_password("IntegrationPass123!"),
                role="user",
                is_active=True,
                kaggle_username=f"kaggle_{uname}",
                kaggle_key=f"not_a_real_kaggle_key_{tag}",
            )
            db.add(user)
        db.commit()
        users = {tag: db.query(User).filter(
            User.username == f"journey_{tag}_{suffix}"
        ).one() for tag in ("a", "b")}
        accounts = {tag: KaggleAccountService.get_execution_account_for_user(
            db, users[tag]
        ) for tag in ("a", "b")}
        assert accounts["a"].id != accounts["b"].id
        yield {
            "users": {k: {"id": v.id, "username": v.username, "role": v.role} for k, v in users.items()},
            "accounts": {k: v.id for k, v in accounts.items()},
        }
    finally:
        db.close()


def _register_client(browser):
    response = browser.post("/oauth/register", json={
        "client_name": "ChatGPT E2E test",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    })
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _issue_oauth_tokens(browser, user, client_id, scope="speech:generate"):
    """Real auth page -> CSRF-protected consent -> PKCE token exchange (test DB only)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    browser.cookies.set(
        "session_token",
        AuthService.create_token(user["id"], user["username"], user["role"]),
    )
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": secrets.token_hex(12),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    page = browser.get("/oauth/authorize", params=query, follow_redirects=False)
    assert page.status_code == 200, page.text[:400]
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match and match.group(1), "Consent form must supply a CSRF token"
    consent = browser.post(
        "/oauth/authorize/consent",
        data={**{k: v for k, v in query.items() if k != "response_type"},
              "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert consent.status_code == 303, consent.text[:400]
    uri = consent.headers["location"]
    assert uri.startswith(REDIRECT + "?")
    params = parse_qs(urlparse(uri).query)
    assert params["state"] == [query["state"]]
    code = params["code"][0]
    exchange = browser.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code": code,
        "code_verifier": verifier,
        "resource": AUDIENCE,
    })
    assert exchange.status_code == 200, exchange.text[:400]
    body = exchange.json()
    assert body["access_token"].startswith("olk_atk_")
    assert body["refresh_token"].startswith("olk_rtk_")
    browser.cookies.clear()
    return body


def _mcp_link_account(browser, bearer_token):
    """New MCP transport/session each time, mimicking separate ChatGPT turns."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {bearer_token}",
    }
    init = browser.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "ChatGPT Journey Test", "version": "1"}},
    })
    assert init.status_code == 200, init.text[:400]
    session_id = init.headers.get("mcp-session-id")
    assert session_id, "Stateful transport must return mcp-session-id"
    headers["mcp-session-id"] = session_id
    response = browser.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "link_account", "arguments": {}},
    })
    assert response.status_code == 200, response.text[:400]
    payload = response.json() if "application/json" in response.headers.get("content-type", "") else next(
        __import__("json").loads(line[5:].strip()) for line in response.text.splitlines()
        if line.startswith("data:")
    )
    result = payload["result"]
    return " ".join(part.get("text", "") for part in result.get("content", []))


def test_oauth_recognizes_same_user_across_fresh_mcp_sessions(two_tenants):
    user = two_tenants["users"]["a"]
    with TestClient(app) as browser:
        cli = _register_client(browser)
        token = _issue_oauth_tokens(browser, user, cli)["access_token"]
        for _ in range(3):
            message = _mcp_link_account(browser, token)
            assert user["username"] in message
            assert "TÀI KHOẢN ĐÃ ĐƯỢC XÁC THỰC" in message
            assert "OLK-" not in message


def test_refresh_preserves_identity_and_revoke_invalidates_family(two_tenants):
    user = two_tenants["users"]["a"]
    with TestClient(app) as browser:
        cli = _register_client(browser)
        issued = _issue_oauth_tokens(browser, user, cli)
        first = issued["access_token"]
        ref = browser.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": cli,
        })
        assert ref.status_code == 200, ref.text
        second = ref.json()["access_token"]
        assert second != first
        assert user["username"] in _mcp_link_account(browser, second)
        # Revoking the rotated access token must invalidate the entire token family.
        revoked = browser.post("/oauth/revoke", data={"token": second})
        assert revoked.status_code == 200
        db = SessionLocal()
        try:
            for raw in (first, second):
                resolved, _, _ = McpAuthService.validate_bearer_token(
                    db, raw, expected_audience=AUDIENCE
                )
                assert resolved is None
        finally:
            db.close()
        replay = browser.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": ref.json()["refresh_token"],
            "client_id": cli,
        })
        assert replay.status_code == 400


def test_token_expiry_rejected_at_real_mcp_http_boundary(two_tenants):
    user = two_tenants["users"]["a"]
    with TestClient(app) as browser:
        cli = _register_client(browser)
        token = _issue_oauth_tokens(browser, user, cli)["access_token"]
        db = SessionLocal()
        try:
            record = db.query(OAuthToken).filter(
                OAuthToken.token_hash == McpAuthService.hash_token(token)
            ).one()
            record.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()
        response = browser.post("/mcp", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 3, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "expired", "version": "1"}}})
        assert response.status_code == 401
        assert "invalid_token" in response.headers.get("www-authenticate", "")


def test_two_oauth_users_cannot_see_or_claim_each_others_kaggle_jobs(two_tenants):
    """OAuth user identity -> corresponding account -> isolated queue, with GPU boot mocked."""
    a, b = two_tenants["users"]["a"], two_tenants["users"]["b"]
    aid, bid = two_tenants["accounts"]["a"], two_tenants["accounts"]["b"]
    with TestClient(app) as browser:
        cli = _register_client(browser)
        ta = _issue_oauth_tokens(browser, a, cli)["access_token"]
        tb = _issue_oauth_tokens(browser, b, cli)["access_token"]
        db = SessionLocal()
        try:
            ua, oa, err_a = McpAuthService.validate_bearer_token(db, ta, AUDIENCE)
            ub, ob, err_b = McpAuthService.validate_bearer_token(db, tb, AUDIENCE)
            assert ua and ub and oa and ob
            assert err_a is None and err_b is None
            assert ua.id == a["id"] and ub.id == b["id"]
            assert oa.client_id == ob.client_id == cli
            with patch("app.services.job_service.KaggleOrchestrator.has_live_worker", return_value=True), \
                 patch("app.services.job_service.KaggleOrchestrator.ensure_worker_running",
                       side_effect=AssertionError("Real Kaggle startup must not occur")):
                ja = JobService.create_job(db, prompt="Tenant A only", user_id=ua.id)
                jb = JobService.create_job(db, prompt="Tenant B only", user_id=ub.id)
            assert ja.execution_account_id == aid and jb.execution_account_id == bid
            assert ja.user_id == ua.id and jb.user_id == ub.id
            pulled_a = JobService.pull_pending_job(db, "fake_worker_a", aid)
            assert pulled_a and pulled_a["id"] == ja.id
            assert JobService.pull_pending_job(db, "fake_worker_a", aid) is None
            pulled_b = JobService.pull_pending_job(db, "fake_worker_b", bid)
            assert pulled_b and pulled_b["id"] == jb.id
            assert JobService.pull_pending_job(db, "fake_worker_b", bid) is None
        finally:
            db.close()


def test_revoking_a_does_not_affect_b(two_tenants):
    with TestClient(app) as browser:
        cli = _register_client(browser)
        ta = _issue_oauth_tokens(browser, two_tenants["users"]["a"], cli)["access_token"]
        tb = _issue_oauth_tokens(browser, two_tenants["users"]["b"], cli)["access_token"]
        assert browser.post("/oauth/revoke", data={"token": ta}).status_code == 200
        db = SessionLocal()
        try:
            aa, _, _ = McpAuthService.validate_bearer_token(db, ta, AUDIENCE)
            bb, _, _ = McpAuthService.validate_bearer_token(db, tb, AUDIENCE)
            assert aa is None
            assert bb is not None and bb.id == two_tenants["users"]["b"]["id"]
        finally:
            db.close()
        assert two_tenants["users"]["b"]["username"] in _mcp_link_account(browser, tb)


def test_missing_auth_does_not_create_tts_job_or_launch_gpu(two_tenants):
    with TestClient(app) as browser, \
         patch("app.services.job_service.JobService.create_job",
               side_effect=AssertionError("No job permitted without OAuth token")):
        response = browser.post("/mcp", headers={
            "Accept": "application/json, text/event-stream"
        }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "anonymous", "version": "1"}}})
        assert response.status_code == 200
        session_id = response.headers["mcp-session-id"]
        request = browser.post("/mcp", headers={
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id
        }, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "generate_speech", "arguments": {"prompt": "no GPU"}}})
        assert request.status_code in (200, 401)
        assert "www-authenticate" in request.headers
        assert "OLK-" not in request.text
