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
