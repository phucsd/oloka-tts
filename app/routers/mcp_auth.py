from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional
from app.database import get_db
from app.models import User, McpPairingSession, OAuthClient
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
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "code_challenge_methods_supported": ["S256", "plain"],
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
    """
    import json
    import secrets
    from app.models import OAuthClient

    client_data = {}
    try:
        client_data = await request.json()
    except Exception:
        pass

    client_id = client_data.get("client_id") or f"mcp_cli_{secrets.token_urlsafe(16)}"
    client_secret = secrets.token_urlsafe(32)
    client_name = client_data.get("client_name") or "ChatGPT Remote MCP Client"
    redirect_uris = client_data.get("redirect_uris") or []

    # Save or update client in database
    existing_cli = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
    if not existing_cli:
        new_cli = OAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            client_name=client_name,
            redirect_uris=json.dumps(redirect_uris)
        )
        db.add(new_cli)
        db.commit()

    return JSONResponse(
        status_code=201,
        content={
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none"
        }
    )


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
    RFC 6749 / RFC 7636 OAuth 2.0 Authorization Endpoint with PKCE.
    Prompts user login and renders consent page.
    """
    # 1. Require user login first
    if not user:
        full_query = str(request.url.query)
        next_path = f"/oauth/authorize?{full_query}" if full_query else "/oauth/authorize"
        import urllib.parse
        return RedirectResponse(url=f"/login?next={urllib.parse.quote(next_path)}", status_code=303)

    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing required redirect_uri parameter")

    from app.models import OAuthClient
    client_name = "ChatGPT Remote MCP"
    if client_id:
        c_obj = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
        if c_obj and c_obj.client_name:
            client_name = c_obj.client_name
        else:
            client_name = f"MCP Client ({client_id[:12]})"

    return templates.TemplateResponse(
        request=request,
        name="oauth_authorize.html",
        context={
            "page_title": "Cấp Quyền Ứng Dụng AI - OlokaTTS",
            "user": user,
            "client_name": client_name,
            "client_id": client_id or "chatgpt",
            "redirect_uri": redirect_uri,
            "state": state or "",
            "scope": scope or "mcp:all speech:generate",
            "code_challenge": code_challenge or "",
            "code_challenge_method": code_challenge_method or "S256",
            "error": None
        }
    )


@router.post("/oauth/authorize/consent")
def oauth_authorize_consent(
    request: Request,
    client_id: str = Form("chatgpt"),
    redirect_uri: str = Form(...),
    state: Optional[str] = Form(None),
    scope: Optional[str] = Form("mcp:all speech:generate"),
    code_challenge: Optional[str] = Form(None),
    code_challenge_method: Optional[str] = Form("S256"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    Processes user approval consent, issues authorization code, and redirects back to AI client.
    """
    import secrets
    from datetime import datetime, timedelta
    from app.models import McpPairingSession

    auth_code = f"olk_auth_{secrets.token_urlsafe(32)}"

    sess = McpPairingSession(
        code=auth_code,
        client_name=f"OAuth:{client_id}",
        user_id=user.id,
        status="authorized",
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge.strip() if code_challenge else None,
        code_challenge_method=code_challenge_method.strip() if code_challenge_method else "S256",
        scope=scope.strip() if scope else "mcp:all speech:generate",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=10)
    )
    db.add(sess)
    db.commit()

    AuthService.log_audit(
        db,
        action="oauth_authorized",
        message=f"Đã cấp quyền OAuth cho ứng dụng {client_id}",
        user_id=user.id
    )

    separator = "&" if "?" in redirect_uri else "?"
    redirect_target = f"{redirect_uri}{separator}code={auth_code}"
    if state and state.strip():
        redirect_target += f"&state={state.strip()}"

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/oauth/token")
async def oauth_token(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    RFC 6749 / RFC 7636 OAuth 2.0 Token Endpoint with PKCE Verification.
    Exchanges authorization code for Bearer access token and refresh token.
    Supports both JSON and application/x-www-form-urlencoded payloads.
    """
    import hmac
    import hashlib
    import base64
    import secrets
    from datetime import datetime, timedelta
    from app.models import McpPairingSession

    # Parse request parameters
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
    # 1. Authorization Code Grant with PKCE
    # -------------------------------------------------------------------------
    if grant_type == "authorization_code":
        code = params.get("code")
        code_verifier = params.get("code_verifier")

        if not code or not str(code).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing code parameter"}
            )

        clean_code = str(code).strip()
        sess = db.query(McpPairingSession).filter(
            McpPairingSession.code == clean_code,
            McpPairingSession.status == "authorized"
        ).first()

        if not sess or (sess.expires_at and sess.expires_at < datetime.utcnow()):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Authorization code expired or invalid"}
            )

        # PKCE Verification (RFC 7636)
        if sess.code_challenge:
            if not code_verifier or not str(code_verifier).strip():
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_grant", "error_description": "Missing required PKCE code_verifier"}
                )
            clean_verifier = str(code_verifier).strip()

            if sess.code_challenge_method == "S256":
                digest = hashlib.sha256(clean_verifier.encode("ascii")).digest()
                computed_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
                target_challenge = sess.code_challenge.rstrip("=")
                if not hmac.compare_digest(computed_challenge, target_challenge):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid_grant", "error_description": "PKCE code_verifier verification failed"}
                    )
            elif sess.code_challenge_method == "plain":
                if not hmac.compare_digest(clean_verifier, sess.code_challenge):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid_grant", "error_description": "PKCE plain verification failed"}
                    )

        user = db.query(User).filter(User.id == sess.user_id, User.is_active == True).first()
        if not user:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "User associated with code not found"}
            )

        # Consume the authorization code
        sess.status = "consumed"

        # Generate long-term bearer access token (30 days) and refresh token
        access_token = AuthService.create_token(
            user.id,
            user.username,
            user.role or "user",
            expires_in_days=30
        )
        refresh_token = f"olk_rt_{secrets.token_urlsafe(36)}"
        rt_hash = McpAuthService.hash_token(refresh_token)

        # Store refresh token session
        rt_sess = McpPairingSession(
            code=f"RT-{secrets.token_hex(6).upper()}",
            session_token=f"{refresh_token[:12]}...{refresh_token[-4:]}",
            session_token_hash=rt_hash,
            client_name=sess.client_name,
            user_id=user.id,
            status="authorized",
            scope=sess.scope or "mcp:all speech:generate",
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=90)
        )
        db.add(rt_sess)
        db.commit()

        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 2592000,
                "refresh_token": refresh_token,
                "scope": sess.scope or "mcp:all speech:generate"
            }
        )

    # -------------------------------------------------------------------------
    # 2. Refresh Token Grant
    # -------------------------------------------------------------------------
    elif grant_type == "refresh_token":
        raw_rt = params.get("refresh_token")
        if not raw_rt or not str(raw_rt).strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "Missing refresh_token parameter"}
            )

        rt_hash = McpAuthService.hash_token(str(raw_rt).strip())
        rt_sess = db.query(McpPairingSession).filter(
            McpPairingSession.session_token_hash == rt_hash,
            McpPairingSession.status == "authorized"
        ).first()

        if not rt_sess or (rt_sess.expires_at and rt_sess.expires_at < datetime.utcnow()):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Refresh token expired or invalid"}
            )

        user = db.query(User).filter(User.id == rt_sess.user_id, User.is_active == True).first()
        if not user:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "User associated with refresh token not found"}
            )

        # Rotate refresh token
        new_access_token = AuthService.create_token(
            user.id,
            user.username,
            user.role or "user",
            expires_in_days=30
        )
        new_rt = f"olk_rt_{secrets.token_urlsafe(36)}"
        rt_sess.session_token = f"{new_rt[:12]}...{new_rt[-4:]}"
        rt_sess.session_token_hash = McpAuthService.hash_token(new_rt)
        rt_sess.expires_at = datetime.utcnow() + timedelta(days=90)
        db.commit()

        return JSONResponse(
            status_code=200,
            content={
                "access_token": new_access_token,
                "token_type": "Bearer",
                "expires_in": 2592000,
                "refresh_token": new_rt,
                "scope": rt_sess.scope or "mcp:all speech:generate"
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
        t_clean = str(token).strip()
        t_hash = McpAuthService.hash_token(t_clean)
        sessions = db.query(McpPairingSession).filter(
            (McpPairingSession.session_token_hash == t_hash) |
            (McpPairingSession.code == t_clean)
        ).all()
        for s in sessions:
            s.status = "revoked"
        db.commit()

    return JSONResponse(status_code=200, content={"status": "revoked"})

