from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional
from app.database import get_db
from app.models import User
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
# OAUTH 2.0 ENDPOINTS (Standard protocol for ChatGPT / Remote AI Agents)
# ==============================================================================

@router.get("/oauth/authorize")
def oauth_authorize(
    request: Request,
    response_type: str = "code",
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    """
    RFC 6749 OAuth 2.0 Authorization Endpoint.
    Directs the user to log in and redirects back to AI client with authorization code.
    """
    if not user:
        # Prompt login and redirect back
        full_query = str(request.url.query)
        next_path = f"/oauth/authorize?{full_query}" if full_query else "/oauth/authorize"
        return RedirectResponse(url=f"/login?next={next_path}", status_code=303)

    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing redirect_uri parameter")

    # Generate one-time authorization code
    import secrets
    from datetime import datetime, timedelta
    from app.models import McpPairingSession
    auth_code = f"olk_auth_{secrets.token_urlsafe(24)}"

    sess = McpPairingSession(
        code=auth_code,
        client_name=f"OAuth:{client_id or 'ChatGPT'}",
        user_id=user.id,
        status="authorized",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=10)
    )
    db.add(sess)
    db.commit()

    # Build redirect URL
    separator = "&" if "?" in redirect_uri else "?"
    redirect_target = f"{redirect_uri}{separator}code={auth_code}"
    if state:
        redirect_target += f"&state={state}"

    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/oauth/token")
async def oauth_token(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    RFC 6749 OAuth 2.0 Token Endpoint.
    Exchanges one-time authorization code for long-term bearer access token.
    Supports both JSON and application/x-www-form-urlencoded payloads.
    """
    from datetime import datetime
    from app.models import McpPairingSession

    # Parse request parameters
    content_type = request.headers.get("content-type", "")
    code = None
    if "application/json" in content_type:
        try:
            body = await request.json()
            code = body.get("code")
        except Exception:
            pass
    if not code:
        form = await request.form()
        code = form.get("code")

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

    user = db.query(User).filter(User.id == sess.user_id, User.is_active == True).first()
    if not user:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "User associated with code not found"}
        )

    # Consume the one-time authorization code
    sess.status = "consumed"
    db.commit()

    # Generate long-term bearer token (30 days)
    access_token = AuthService.create_token(
        user.id,
        user.username,
        user.role or "user",
        expires_in_days=30
    )

    return JSONResponse(
        status_code=200,
        content={
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 2592000,
            "scope": "tts:generate"
        }
    )


@router.get("/.well-known/oauth-authorization-server")
def oauth_metadata(request: Request):
    """RFC 8414 OAuth 2.0 Authorization Server Metadata."""
    from app.mcp_server import get_base_url
    base = get_base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"]
    }

