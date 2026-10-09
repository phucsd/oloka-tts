from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional, List
import re
import json
import secrets
import hashlib
import hmac
import base64
import urllib.parse
from datetime import datetime, timedelta
from app.config import settings
from app.database import get_db
from app.models import User, McpPairingSession, OAuthClient, OAuthToken
from app.services.auth_service import AuthService, get_current_user, get_current_user_optional
from app.services.mcp_auth_service import McpAuthService

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["MCP Pairing & Authentication"])

@router.get("/mcp/pair", response_class=HTMLResponse)
def mcp_pair_page(
    request: Request,
    code: Optional[str] = None,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    if not code or not code.strip():
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "session": None,
                "error": None,
                "success": False
            }
        )

    clean_code = code.strip().upper()

    # If user not logged in, prompt login first and redirect back
    if not user:
        return RedirectResponse(
            url=f"/login?next=/mcp/pair?code={clean_code}",
            status_code=303
        )

    session = McpAuthService.get_pairing_session(db, clean_code)
    if not session:
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "session": None,
                "error": f"Mã ghép đôi '{clean_code}' không tồn tại trên hệ thống.",
                "success": False
            },
            status_code=404
        )

    if session.status == "authorized":
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "code": clean_code,
                "session": session,
                "error": None,
                "success": True
            }
        )

    if session.status == "expired":
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "session": None,
                "error": f"Mã ghép đôi '{clean_code}' đã quá hạn (30 phút). Vui lòng gửi lại yêu cầu từ ChatGPT để nhận mã mới.",
                "success": False
            },
            status_code=400
        )

    if session.status == "revoked":
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "session": None,
                "error": f"Mã ghép đôi '{clean_code}' này đã bị thu hồi trước đó.",
                "success": False
            },
            status_code=400
        )

    return templates.TemplateResponse(
        request=request,
        name="mcp_pair.html",
        context={
            "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
            "user": user,
            "session": session,
            "code": clean_code,
            "error": None,
            "success": False
        }
    )

@router.post("/mcp/pair/approve")
def mcp_pair_approve(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    clean_code = code.strip().upper()
    success, msg, sess_token = McpAuthService.approve_pairing_session(db, clean_code, user.id)

    if not success:
        return templates.TemplateResponse(
            request=request,
            name="mcp_pair.html",
            context={
                "page_title": "Xác Thực Phiên ChatGPT - OlokaTTS",
                "user": user,
                "session": None,
                "error": msg,
                "success": False
            },
            status_code=400
        )

    AuthService.log_audit(
        db,
        action="mcp_pair_approved",
        message=f"Đã cấp quyền cho phiên AI Agent ({clean_code})",
        user_id=user.id
    )

    return templates.TemplateResponse(
        request=request,
        name="mcp_pair.html",
        context={
            "page_title": "Cấp Quyền Thành Công - OlokaTTS",
            "user": user,
            "code": clean_code,
            "session": None,
            "error": None,
            "success": True
        }
    )

@router.post("/api/mcp/pair/generate")
def create_preapproved_pair_code(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Allows a logged-in user to generate a pair code directly from web settings."""
    sess = McpAuthService.create_pairing_session(db, client_name="Web Dashboard")
    McpAuthService.approve_pairing_session(db, sess.code, user.id)
    return {
        "status": "success",
        "code": sess.code,
        "expires_at": sess.expires_at.isoformat() if sess.expires_at else None,
        "message": f"Mã ghép đôi {sess.code} đã sẵn sàng sử dụng trong ChatGPT."
    }

@router.get("/api/mcp/pair/status")
def check_pairing_status(code: str, db: Session = Depends(get_db)):
    """API endpoint for polling session status."""
    clean_code = code.strip().upper()
    sess = McpAuthService.get_pairing_session(db, clean_code)
    if not sess:
        return {"status": "not_found", "is_authorized": False}
    return {
        "status": sess.status,
        "code": sess.code,
        "is_authorized": (sess.status == "authorized"),
        "client_name": sess.client_name
    }


# ==============================================================================
# OAUTH 2.1 SECURITY UTILITIES & VALIDATORS (RFC 6749, RFC 7636, RFC 8252, RFC 9700)
# ==============================================================================

def validate_redirect_uri_format(uri: str) -> bool:
    """Validates that a redirect URI is a valid absolute URI without fragments."""
    if not uri or not isinstance(uri, str):
        return False
    parsed = urllib.parse.urlsplit(uri)
    if parsed.fragment:
        return False  # RFC 6749 Section 3.1.2: URI MUST NOT contain a fragment
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    elif parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        return False
    return False

def validate_redirect_uri_against_client(client: OAuthClient, redirect_uri: str) -> bool:
    """
    Validates redirect_uri strictly against registered URIs in OAuthClient.
    Enforces exact matching (no wildcards, no subdomains, no prefixes).
    RFC 8252 loopback exception: if client registered loopback (e.g. http://127.0.0.1/callback),
    allow varying port if host, path, and scheme match exactly.
    """
    if not client or not redirect_uri:
        return False
    try:
        registered_list = json.loads(client.redirect_uris) if client.redirect_uris else []
    except Exception:
        registered_list = []
    if not isinstance(registered_list, list):
        registered_list = [str(registered_list)]

    target_parsed = urllib.parse.urlsplit(redirect_uri)

    for reg in registered_list:
        if reg == redirect_uri:
            return True
        # RFC 8252 Section 7.3 loopback port exception
        reg_parsed = urllib.parse.urlsplit(reg)
        if reg_parsed.scheme == "http" and target_parsed.scheme == "http":
            reg_host = (reg_parsed.hostname or "").lower()
            target_host = (target_parsed.hostname or "").lower()
            if reg_host in ("127.0.0.1", "localhost") and target_host in ("127.0.0.1", "localhost"):
                if reg_parsed.path == target_parsed.path and reg_parsed.query == target_parsed.query:
                    return True
    return False

def validate_pkce_challenge(challenge: str, method: str) -> bool:
    """Validates PKCE code_challenge and code_challenge_method."""
    if not challenge or not isinstance(challenge, str):
        return False
    if (method or "").upper() != "S256":
        return False
    clean = challenge.rstrip("=")
    if len(clean) < 43 or len(clean) > 128:
        return False
    return bool(re.match(r"^[A-Za-z0-9\-_~]+$", clean))

def validate_pkce_verifier(verifier: str) -> bool:
    """Validates PKCE code_verifier according to RFC 7636 Section 4.1."""
    if not verifier or not isinstance(verifier, str):
        return False
    if len(verifier) < 43 or len(verifier) > 128:
        return False
    return bool(re.match(r"^[A-Za-z0-9\-._~]+$", verifier))

def generate_csrf_token(user_id: str, client_id: str, redirect_uri: str, challenge: str) -> str:
    """Generates a secure HMAC token to protect the consent form from CSRF."""
    secret = settings.SECRET_KEY or "oloka_default_secret_2026"
    raw = f"{user_id}:{client_id}:{redirect_uri}:{challenge}"
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()

def verify_csrf_token(token: str, user_id: str, client_id: str, redirect_uri: str, challenge: str) -> bool:
    expected = generate_csrf_token(user_id, client_id, redirect_uri, challenge)
    return hmac.compare_digest(token or "", expected)


# ==============================================================================
# OAUTH 2.1 ENDPOINTS (RFC 6749, RFC 7591, RFC 7636, RFC 8414, RFC 9728)
# Standard protocol for ChatGPT / Remote AI Agents & Protected MCP Resources
# ==============================================================================

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource_metadata(request: Request):
    """
    RFC 9728 OAuth 2.0 Protected Resource Metadata.
    Probed by ChatGPT and MCP clients to discover authorization servers.
    """
    from app.mcp_server import get_base_url
    base = get_base_url()
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp:all", "speech:generate", "voices:read", "status:read"]
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def oauth_authorization_server_metadata(request: Request):
    """
    RFC 8414 OAuth 2.0 Authorization Server Metadata (and OpenID Connect Discovery alias).
    Enforces PKCE S256 only.
    """
    from app.mcp_server import get_base_url
    base = get_base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp:all", "speech:generate", "voices:read", "status:read"]
    }


@router.post("/oauth/register")
@router.post("/register")
async def oauth_dynamic_client_registration(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    RFC 7591 Dynamic Client Registration (DCR) for MCP Clients (ChatGPT, etc.).
    Server assigns unique client_id. Validates redirect_uris strictly.
    """
    client_data = {}
    try:
        client_data = await request.json()
    except Exception:
        pass

    raw_uris = client_data.get("redirect_uris") or []
    if not isinstance(raw_uris, list) or len(raw_uris) == 0:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_redirect_uri", "error_description": "At least one valid redirect_uri is required"}
        )

    # Validate each redirect_uri strictly
    valid_uris = []
    for u in raw_uris:
        if not isinstance(u, str) or not validate_redirect_uri_format(u.strip()):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_redirect_uri",
                    "error_description": f"Redirect URI '{u}' is invalid. Must be an absolute HTTPS URI (or HTTP loopback) without fragments."
                }
            )
        valid_uris.append(u.strip())

    # Rate-limiting / anti-abuse check (max 500 active dynamic clients)
    count = db.query(OAuthClient).count()
    if count > 500:
        return JSONResponse(
            status_code=429,
            content={"error": "too_many_requests", "error_description": "Registration rate limit reached. Please try again later."}
        )

    # Server generates unforgeable unique client_id
    client_id = f"mcp_cli_{secrets.token_urlsafe(16)}"
    client_name = str(client_data.get("client_name") or "ChatGPT Remote MCP Client")[:128]

    auth_method = client_data.get("token_endpoint_auth_method", "none")
    is_confidential = auth_method in ("client_secret_post", "client_secret_basic")
    client_secret = secrets.token_urlsafe(32) if is_confidential else None

    new_cli = OAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        client_name=client_name,
        redirect_uris=json.dumps(valid_uris),
        is_confidential=is_confidential,
        token_endpoint_auth_method=auth_method if is_confidential else "none",
        created_at=datetime.utcnow()
    )
    db.add(new_cli)
    db.commit()

    resp_content = {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": valid_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": new_cli.token_endpoint_auth_method
    }
    if client_secret:
        resp_content["client_secret"] = client_secret

    return JSONResponse(status_code=201, content=resp_content)


@router.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize_page(
    request: Request,
    response_type: str = "code",
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = "S256",
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    """
    RFC 6749 / RFC 7636 OAuth 2.0 Authorization Endpoint with Mandatory PKCE S256.
    Validates client_id, redirect_uri, and PKCE BEFORE redirecting to login.
    Prevents Open Redirect and PKCE Downgrade attacks.
    """
    # 1. Validate response_type
    if response_type != "code":
        raise HTTPException(
            status_code=400,
            detail="unsupported_response_type: Only response_type='code' is supported"
        )

    # 2. Validate client_id
    if not client_id or not client_id.strip():
        raise HTTPException(status_code=400, detail="invalid_request: Missing required client_id parameter")

    clean_client_id = client_id.strip()
    client_obj = db.query(OAuthClient).filter(OAuthClient.client_id == clean_client_id).first()
    if not client_obj:
        raise HTTPException(status_code=400, detail="invalid_client: Unregistered or unknown client_id")

    # 3. Validate redirect_uri strictly against registered URIs
    if not redirect_uri or not redirect_uri.strip():
        raise HTTPException(status_code=400, detail="invalid_request: Missing required redirect_uri parameter")

    clean_redirect_uri = redirect_uri.strip()
    if not validate_redirect_uri_against_client(client_obj, clean_redirect_uri):
        # DO NOT redirect to an untrusted URI! Fail closed immediately.
        raise HTTPException(
            status_code=400,
            detail="invalid_request: redirect_uri does not match any registered redirect URI for this client"
        )

    # 4. Mandatory PKCE S256 enforcement (P0)
    if not code_challenge or not code_challenge.strip():
        raise HTTPException(
            status_code=400,
            detail="invalid_request: code_challenge is required. Plain or unauthenticated flows are prohibited."
        )

    clean_challenge = code_challenge.strip()
    clean_method = (code_challenge_method or "").strip().upper()
    if clean_method != "S256":
        raise HTTPException(
            status_code=400,
            detail="invalid_request: code_challenge_method must be 'S256'. Method 'plain' is rejected."
        )

    if not validate_pkce_challenge(clean_challenge, clean_method):
        raise HTTPException(
            status_code=400,
            detail="invalid_request: code_challenge format invalid (must be RFC 7636 base64url, 43-128 chars)"
        )

    # 5. Require user login if not authenticated
    if not user:
        full_query = str(request.url.query)
        next_path = f"/oauth/authorize?{full_query}" if full_query else "/oauth/authorize"
        return RedirectResponse(url=f"/login?next={urllib.parse.quote(next_path)}", status_code=303)

    # 6. Generate CSRF token for consent form
    csrf_tok = generate_csrf_token(user.id, clean_client_id, clean_redirect_uri, clean_challenge)

    return templates.TemplateResponse(
        request=request,
        name="oauth_authorize.html",
        context={
            "page_title": "Cấp Quyền Ứng Dụng AI - OlokaTTS",
            "user": user,
            "client_name": client_obj.client_name or f"MCP Client ({clean_client_id[:12]})",
            "client_id": clean_client_id,
            "redirect_uri": clean_redirect_uri,
            "state": state or "",
            "scope": scope or "mcp:all speech:generate",
            "code_challenge": clean_challenge,
            "code_challenge_method": "S256",
            "csrf_token": csrf_tok,
            "error": None
        }
    )


@router.post("/oauth/authorize/consent")
def oauth_authorize_consent(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: Optional[str] = Form(None),
    scope: Optional[str] = Form("mcp:all speech:generate"),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    csrf_token: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    Processes user approval consent, issues authorization code bound to client and redirect URI,
    and safely redirects back to client.
    """
    clean_client_id = client_id.strip()
    clean_redirect_uri = redirect_uri.strip()
    clean_challenge = code_challenge.strip()
    clean_method = code_challenge_method.strip().upper()

    # Re-validate client and redirect_uri
    client_obj = db.query(OAuthClient).filter(OAuthClient.client_id == clean_client_id).first()
    if not client_obj:
        raise HTTPException(status_code=400, detail="invalid_client")

    if not validate_redirect_uri_against_client(client_obj, clean_redirect_uri):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri: URI mismatch")

    if clean_method != "S256" or not validate_pkce_challenge(clean_challenge, clean_method):
        raise HTTPException(status_code=400, detail="invalid_pkce: S256 required")

    # Verify CSRF token
    if csrf_token and not verify_csrf_token(csrf_token, user.id, clean_client_id, clean_redirect_uri, clean_challenge):
        raise HTTPException(status_code=400, detail="invalid_request: CSRF verification failed")


    auth_code = f"olk_auth_{secrets.token_urlsafe(32)}"

    sess = McpPairingSession(
        code=auth_code,
        client_name=client_obj.client_name or f"OAuth:{clean_client_id}",
        user_id=user.id,
        status="authorized",
        client_id=clean_client_id,
        redirect_uri=clean_redirect_uri,
        code_challenge=clean_challenge,
        code_challenge_method="S256",
        scope=scope.strip() if scope else "mcp:all speech:generate",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=5)  # 5-minute authorization code window
    )
    db.add(sess)
    db.commit()

    AuthService.log_audit(
        db,
        action="oauth_authorized",
        message=f"Đã cấp quyền OAuth cho ứng dụng {clean_client_id}",
        user_id=user.id
    )

    sep = "&" if "?" in clean_redirect_uri else "?"
    redirect_target = f"{clean_redirect_uri}{sep}code={urllib.parse.quote(auth_code)}"
    if state and state.strip():
        redirect_target += f"&state={urllib.parse.quote(state.strip())}"

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/oauth/token")
async def oauth_token(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    RFC 6749 / RFC 7636 OAuth 2.0 Token Endpoint with Mandatory PKCE S256 Verification.
    Atomic one-time-use authorization code consumption (Anti-Replay / Race Condition Protected).
    Issues dedicated OAuth 2.1 Access Tokens (olk_atk_...) and Refresh Tokens with rotation.
    """
    content_type = request.headers.get("content-type", "")
    params = {}
    if "application/json" in content_type:
        try:
            params = await request.json()
        except Exception:
            pass
    if not params:
        form = await request.form()
        params = dict(form)

    grant_type = params.get("grant_type", "authorization_code")

    # -------------------------------------------------------------------------
    # 1. Authorization Code Grant with Mandatory PKCE S256 & Atomic Consumption
    # -------------------------------------------------------------------------
    if grant_type == "authorization_code":
        code = params.get("code")
        code_verifier = params.get("code_verifier")
        redirect_uri = params.get("redirect_uri")
        client_id = params.get("client_id")

        if not code or not str(code).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing required 'code' parameter"}
            )
        if not code_verifier or not str(code_verifier).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing required 'code_verifier' parameter (PKCE S256 mandatory)"}
            )
        if not redirect_uri or not str(redirect_uri).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing required 'redirect_uri' parameter"}
            )

        clean_verifier = str(code_verifier).strip()
        if not validate_pkce_verifier(clean_verifier):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "PKCE code_verifier format invalid"}
            )

        clean_code = str(code).strip()
        clean_redirect_uri = str(redirect_uri).strip()
        clean_client_id = str(client_id).strip() if client_id else None

        sess = db.query(McpPairingSession).filter(
            McpPairingSession.code == clean_code,
            McpPairingSession.status == "authorized"
        ).first()

        now = datetime.utcnow()
        if not sess or (sess.expires_at and sess.expires_at < now):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Authorization code expired or invalid"}
            )

        # Client binding verification
        if clean_client_id and sess.client_id and sess.client_id != clean_client_id:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Client ID mismatch with authorization code"}
            )

        # Redirect URI binding verification (exact match)
        if sess.redirect_uri != clean_redirect_uri:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "redirect_uri mismatch with authorization code"}
            )

        # PKCE S256 Verification (Constant-Time)
        if not sess.code_challenge or (sess.code_challenge_method or "").upper() != "S256":
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Authorization code is not protected by PKCE S256"}
            )

        digest = hashlib.sha256(clean_verifier.encode("ascii")).digest()
        computed_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        target_challenge = sess.code_challenge.rstrip("=")
        if not hmac.compare_digest(computed_challenge, target_challenge):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "PKCE code_verifier verification failed"}
            )

        # Atomic one-time-use consumption (Anti-replay & Double Exchange Protection)
        affected = db.query(McpPairingSession).filter(
            McpPairingSession.id == sess.id,
            McpPairingSession.status == "authorized",
            McpPairingSession.expires_at > now
        ).update({"status": "consumed", "last_used_at": now})
        db.commit()

        if affected != 1:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Authorization code has already been consumed or expired"}
            )

        user = db.query(User).filter(User.id == sess.user_id, User.is_active == True).first()
        if not user:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "User associated with code not found or inactive"}
            )

        # Issue dedicated OAuth 2.1 Access Token & Refresh Token
        family_id = f"fam_{secrets.token_hex(12)}"
        raw_access_token = f"olk_atk_{secrets.token_urlsafe(32)}"
        raw_refresh_token = f"olk_rtk_{secrets.token_urlsafe(36)}"

        atk_record = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="access_token",
            token_hash=McpAuthService.hash_token(raw_access_token),
            token_masked=f"{raw_access_token[:12]}...{raw_access_token[-4:]}",
            user_id=user.id,
            client_id=sess.client_id or "mcp_client",
            scope=sess.scope or "mcp:all speech:generate",
            audience="https://tts.oloka.net/mcp",
            family_id=family_id,
            created_at=now,
            expires_at=now + timedelta(hours=1)  # 1 hour access token
        )
        rtk_record = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="refresh_token",
            token_hash=McpAuthService.hash_token(raw_refresh_token),
            token_masked=f"{raw_refresh_token[:12]}...{raw_refresh_token[-4:]}",
            user_id=user.id,
            client_id=sess.client_id or "mcp_client",
            scope=sess.scope or "mcp:all speech:generate",
            audience="https://tts.oloka.net/mcp",
            family_id=family_id,
            created_at=now,
            expires_at=now + timedelta(days=90)  # 90 days refresh token
        )
        db.add(atk_record)
        db.add(rtk_record)
        db.commit()

        return JSONResponse(
            status_code=200,
            content={
                "access_token": raw_access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": raw_refresh_token,
                "scope": sess.scope or "mcp:all speech:generate"
            }
        )

    # -------------------------------------------------------------------------
    # 2. Refresh Token Grant with Rotation & Reuse Detection
    # -------------------------------------------------------------------------
    elif grant_type == "refresh_token":
        raw_rt = params.get("refresh_token")
        if not raw_rt or not str(raw_rt).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing required 'refresh_token' parameter"}
            )

        rt_hash = McpAuthService.hash_token(str(raw_rt).strip())
        target_rtk = db.query(OAuthToken).filter(
            OAuthToken.token_hash == rt_hash,
            OAuthToken.token_type == "refresh_token"
        ).first()

        now = datetime.utcnow()
        if not target_rtk:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Refresh token expired or invalid"}
            )

        # Refresh Token Reuse Detection (RFC 6749 Section 10.4)
        if target_rtk.revoked_at is not None:
            # Compromised token family: terminate all tokens in this family immediately!
            if target_rtk.family_id:
                db.query(OAuthToken).filter(
                    OAuthToken.family_id == target_rtk.family_id,
                    OAuthToken.revoked_at.is_(None)
                ).update({"revoked_at": now})
                db.commit()
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Refresh token reuse detected. Session terminated."}
            )

        if target_rtk.expires_at < now:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Refresh token expired"}
            )

        user = db.query(User).filter(User.id == target_rtk.user_id, User.is_active == True).first()
        if not user:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "User associated with refresh token inactive"}
            )

        # Refresh Token Rotation: Revoke current refresh token
        target_rtk.revoked_at = now
        target_rtk.last_used_at = now

        # Issue new Access Token and new Refresh Token
        new_atk_raw = f"olk_atk_{secrets.token_urlsafe(32)}"
        new_rtk_raw = f"olk_rtk_{secrets.token_urlsafe(36)}"

        new_atk = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="access_token",
            token_hash=McpAuthService.hash_token(new_atk_raw),
            token_masked=f"{new_atk_raw[:12]}...{new_atk_raw[-4:]}",
            user_id=user.id,
            client_id=target_rtk.client_id,
            scope=target_rtk.scope,
            audience=target_rtk.audience,
            family_id=target_rtk.family_id,
            created_at=now,
            expires_at=now + timedelta(hours=1)
        )
        new_rtk = OAuthToken(
            token_id=f"tok_{secrets.token_hex(8)}",
            token_type="refresh_token",
            token_hash=McpAuthService.hash_token(new_rtk_raw),
            token_masked=f"{new_rtk_raw[:12]}...{new_rtk_raw[-4:]}",
            user_id=user.id,
            client_id=target_rtk.client_id,
            scope=target_rtk.scope,
            audience=target_rtk.audience,
            family_id=target_rtk.family_id,
            created_at=now,
            expires_at=now + timedelta(days=90)
        )
        db.add(new_atk)
        db.add(new_rtk)
        db.commit()

        return JSONResponse(
            status_code=200,
            content={
                "access_token": new_atk_raw,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": new_rtk_raw,
                "scope": target_rtk.scope
            }
        )

    else:
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_grant_type", "error_description": f"Grant type '{grant_type}' not supported"}
        )


@router.post("/oauth/revoke")
async def oauth_revoke_token(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    RFC 7009 OAuth 2.0 Token Revocation.
    Revokes access tokens, refresh tokens, and linked token families.
    """
    content_type = request.headers.get("content-type", "")
    token = None
    if "application/json" in content_type:
        try:
            body = await request.json()
            token = body.get("token")
        except Exception:
            pass
    if not token:
        form = await request.form()
        token = form.get("token")

    if token and str(token).strip():
        McpAuthService.revoke_oauth_token(db, str(token).strip())

    return JSONResponse(status_code=200, content={"status": "revoked"})


