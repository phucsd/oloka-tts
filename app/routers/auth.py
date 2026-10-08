import secrets
import urllib.parse
import httpx
from fastapi import APIRouter, Depends, Request, Form, HTTPException, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional
from app.config import settings
from app.database import get_db
from app.models import User
from app.services.auth_service import AuthService, get_current_user, get_current_user_optional
from app.services.settings_service import SettingsService

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["Authentication"])

AUTH_ERROR_MESSAGES = {
    "google_not_configured": "Chưa cấu hình Google Client ID / Secret trên máy chủ. Vui lòng thiết lập biến môi trường hoặc nhập tại trang Cấu hình Quản trị.",
    "google_cancelled": "Đăng nhập Google đã bị hủy bỏ hoặc từ chối cấp quyền.",
    "invalid_oauth_state": "Phiên xác thực Google không hợp lệ hoặc đã hết hạn. Vui lòng thử lại.",
    "missing_code": "Không nhận được mã xác thực phản hồi từ Google. Vui lòng thử lại.",
    "google_token_failed": "Lỗi khi trao đổi token xác thực với Google. Vui lòng thử lại.",
    "google_userinfo_failed": "Không thể lấy thông tin tài khoản từ Google. Vui lòng thử lại.",
    "registration_disabled": "Hệ thống hiện tại đang tắt tính năng đăng ký tài khoản tự do.",
    "account_disabled": "Tài khoản của bạn đã bị khóa. Vui lòng liên hệ Quản trị viên.",
}

from urllib.parse import urlparse


def is_safe_redirect_url(url: Optional[str]) -> bool:
    if not url:
        return False
    clean = url.strip()
    if not clean.startswith("/"):
        return False
    # Prevent protocol-relative URLs (//attacker.com) and backslash bypasses (\\attacker.com, /\attacker.com)
    if clean.startswith("//") or clean.startswith("/\\") or clean.startswith("\\"):
        return False
    # Prevent open redirect to authentication loop endpoints
    if clean.startswith("/login") or clean.startswith("/auth/"):
        return False
    try:
        parsed = urlparse(clean)
        if parsed.scheme or parsed.netloc:
            return False
    except Exception:
        return False
    return True

def set_session_cookie(resp: Response, request: Request, token: str):
    is_https = (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" or settings.APP_ENV == "production")
    resp.set_cookie(
        key="session_token",
        value=token,
        max_age=7 * 86400,
        httponly=True,
        samesite="lax",
        secure=is_https
    )

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = None, user: Optional[User] = Depends(get_current_user_optional)):
    safe_next = next if is_safe_redirect_url(next) else "/"
    if user:
        return RedirectResponse(url=safe_next, status_code=302)
    error_msg = AUTH_ERROR_MESSAGES.get(error, error)
    return templates.TemplateResponse(
        request=request,
        name="auth/login.html",
        context={"page_title": "Đăng Nhập - OlokaTTS", "next": safe_next, "error": error_msg}
    )


@router.post("/login")
def login_action(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db)
):
    user = AuthService.authenticate_user(db, username.strip(), password)
    is_json = "application/json" in request.headers.get("accept", "").lower()

    if not user:
        if is_json:
            raise HTTPException(status_code=400, detail="Tên đăng nhập hoặc mật khẩu không chính xác")
        return templates.TemplateResponse(
            request=request,
            name="auth/login.html",
            context={
                "page_title": "Đăng Nhập - OlokaTTS",
                "next": next,
                "error": "Tên đăng nhập hoặc mật khẩu không chính xác!",
                "username": username
            },
            status_code=400
        )

    # Create session token
    token = AuthService.create_token(user.id, user.username, user.role, expires_in_days=7)
    AuthService.log_audit(db, action="login", message=f"Đăng nhập thành công", user_id=user.id)

    if is_json:
        return {
            "status": "success",
            "token": token,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "kaggle_configured": bool(user.kaggle_username and user.kaggle_key)
            }
        }

    redirect_url = next if is_safe_redirect_url(next) else "/"
    resp = RedirectResponse(url=redirect_url, status_code=303)
    set_session_cookie(resp, request, token)
    return resp

@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    error: str = None,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    if user:
        return RedirectResponse(url="/", status_code=302)
    if not SettingsService.get_bool(db, "allow_public_registration", True):
        return RedirectResponse(url="/login?error=registration_disabled", status_code=303)
    error_msg = AUTH_ERROR_MESSAGES.get(error, error)
    return templates.TemplateResponse(
        request=request,
        name="auth/register.html",
        context={"page_title": "Đăng Ký Tài Khoản - OlokaTTS", "error": error_msg}
    )


# ==============================================================================
# GOOGLE OAUTH 2.0 (SIGN-IN & SIGN-UP)
# ==============================================================================
@router.get("/auth/google")
def google_oauth_login(
    request: Request,
    next: str = "/",
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    if user:
        return RedirectResponse(url=next or "/", status_code=302)

    client_id = SettingsService.get_setting(db, "google_client_id") or settings.GOOGLE_CLIENT_ID
    client_secret = SettingsService.get_setting(db, "google_client_secret") or settings.GOOGLE_CLIENT_SECRET

    if not client_id or not client_secret:
        return RedirectResponse(url="/login?error=google_not_configured", status_code=303)

    redirect_uri = AuthService.get_google_redirect_uri(request, db)
    state = secrets.token_urlsafe(32)

    params = {
        "client_id": client_id.strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account"
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    resp = RedirectResponse(url=auth_url, status_code=303)
    resp.set_cookie(key="google_oauth_state", value=state, max_age=600, httponly=True, samesite="lax")
    if next:
        resp.set_cookie(key="google_oauth_next", value=next, max_age=600, httponly=True, samesite="lax")
    return resp

@router.get("/auth/google/callback")
async def google_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db)
):
    if error:
        return RedirectResponse(url="/login?error=google_cancelled", status_code=303)

    stored_state = request.cookies.get("google_oauth_state")
    raw_next = request.cookies.get("google_oauth_next") or "/"
    next_url = raw_next if is_safe_redirect_url(raw_next) else "/"

    if not state or not stored_state or state != stored_state:
        return RedirectResponse(url="/login?error=invalid_oauth_state", status_code=303)

    if not code:
        return RedirectResponse(url="/login?error=missing_code", status_code=303)

    client_id = SettingsService.get_setting(db, "google_client_id") or settings.GOOGLE_CLIENT_ID
    client_secret = SettingsService.get_setting(db, "google_client_secret") or settings.GOOGLE_CLIENT_SECRET
    redirect_uri = AuthService.get_google_redirect_uri(request, db)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id.strip(),
                    "client_secret": client_secret.strip(),
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"}
            )
            if token_resp.status_code != 200:
                print(f"[Google OAuth] Token exchange error: {token_resp.text}")
                return RedirectResponse(url="/login?error=google_token_failed", status_code=303)

            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return RedirectResponse(url="/login?error=google_token_failed", status_code=303)

            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if userinfo_resp.status_code != 200:
                print(f"[Google OAuth] Userinfo error: {userinfo_resp.text}")
                return RedirectResponse(url="/login?error=google_userinfo_failed", status_code=303)

            userinfo = userinfo_resp.json()
    except Exception as e:
        print(f"[Google OAuth] Exception during OAuth: {e}")
        return RedirectResponse(url="/login?error=google_token_failed", status_code=303)

    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse(url="/login?error=google_userinfo_failed", status_code=303)

    if not userinfo.get("email_verified", True):
        return RedirectResponse(url="/login?error=google_userinfo_failed", status_code=303)

    name = userinfo.get("name", "")
    avatar_url = userinfo.get("picture", "")
    google_id = userinfo.get("sub", "")

    admin_email = (SettingsService.get_setting(db, "admin_google_email") or settings.ADMIN_GOOGLE_EMAIL or "phucsd@gmail.com").strip().lower()
    is_admin = (email == admin_email)

    # Check registration policy if new non-admin user
    existing_user = db.query(User).filter(User.email == email).first()
    if not existing_user and not is_admin:
        if not SettingsService.get_bool(db, "allow_public_registration", True):
            return RedirectResponse(url="/login?error=registration_disabled", status_code=303)

    user, is_created = AuthService.get_or_create_google_user(
        db=db,
        email=email,
        name=name,
        avatar_url=avatar_url,
        google_id=google_id,
        designated_admin_email=admin_email
    )

    if not user.is_active:
        return RedirectResponse(url="/login?error=account_disabled", status_code=303)

    token = AuthService.create_token(user.id, user.username, user.role, expires_in_days=7)
    AuthService.log_audit(
        db,
        action="google_login",
        message=f"Đăng nhập thành công qua Google ({email}) - Vai trò: {user.role}",
        user_id=user.id
    )

    resp = RedirectResponse(url=next_url, status_code=303)
    set_session_cookie(resp, request, token)
    resp.delete_cookie("google_oauth_state")
    resp.delete_cookie("google_oauth_next")
    return resp

@router.post("/register")
def register_action(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    username = username.strip()
    email = email.strip().lower()
    is_json = "application/json" in request.headers.get("accept", "").lower()

    # Enforce registration policy
    if not SettingsService.get_bool(db, "allow_public_registration", True):
        err = "Hệ thống hiện tại đang tắt tính năng đăng ký tài khoản tự do."
        if is_json: raise HTTPException(status_code=403, detail=err)
        return RedirectResponse(url="/login?error=registration_disabled", status_code=303)

    # Validation
    if len(username) < 3 or not username.isalnum():
        err = "Tên đăng nhập phải từ 3 ký tự trở lên và chỉ chứa chữ/số!"
        if is_json: raise HTTPException(status_code=400, detail=err)
        return templates.TemplateResponse(request=request, name="auth/register.html", context={"error": err, "username": username, "email": email}, status_code=400)

    if len(password) < 8:
        err = "Mật khẩu phải có ít nhất 8 ký tự!"
        if is_json: raise HTTPException(status_code=400, detail=err)
        return templates.TemplateResponse(request=request, name="auth/register.html", context={"error": err, "username": username, "email": email}, status_code=400)

    if password != confirm_password:
        err = "Mật khẩu xác nhận không khớp!"
        if is_json: raise HTTPException(status_code=400, detail=err)
        return templates.TemplateResponse(request=request, name="auth/register.html", context={"error": err, "username": username, "email": email}, status_code=400)

    existing_user = db.query(User).filter((User.username == username) | (User.email == email)).first()
    if existing_user:
        err = "Tên đăng nhập hoặc Email này đã tồn tại trên hệ thống!"
        if is_json: raise HTTPException(status_code=400, detail=err)
        return templates.TemplateResponse(request=request, name="auth/register.html", context={"error": err, "username": username, "email": email}, status_code=400)

    # Create user
    user = AuthService.create_user(db, username=username, email=email, password=password, role="user")
    token = AuthService.create_token(user.id, user.username, user.role, expires_in_days=7)

    if is_json:
        return {"status": "success", "token": token, "user_id": user.id, "message": "Đăng ký thành công!"}

    # Redirect to settings to configure Kaggle key immediately
    resp = RedirectResponse(url="/settings?welcome=1", status_code=303)
    set_session_cookie(resp, request, token)
    return resp


@router.get("/logout")
@router.post("/logout")
def logout_action():
    resp = RedirectResponse(url="/login?msg=logged_out", status_code=303)
    resp.delete_cookie("session_token")
    return resp

@router.get("/api/auth/me")
def get_current_user_profile(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "api_key": user.api_key,
        "kaggle_username": user.kaggle_username,
        "kaggle_configured": bool(user.kaggle_username and user.kaggle_key),
        "created_at": user.created_at
    }
