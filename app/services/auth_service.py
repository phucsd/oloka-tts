import os
import re
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from typing import Optional
from fastapi import Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User, AuditLog

class AuthService:
    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            100000
        )
        return f"{salt}${key.hex()}"

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        try:
            salt, key_hex = hashed_password.split("$")
            new_key = hashlib.pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                salt.encode("utf-8"),
                100000
            )
            return hmac.compare_digest(new_key.hex(), key_hex)
        except Exception:
            return False

    @staticmethod
    def create_token(user_id: str, username: str, role: str, expires_in_days: int = 7) -> str:
        exp = int(time.time()) + (expires_in_days * 86400)
        payload = {
            "sub": user_id,
            "username": username,
            "role": role,
            "exp": exp
        }
        raw_json = json.dumps(payload, separators=(',', ':')).encode("utf-8")
        b64_payload = base64.urlsafe_b64encode(raw_json).decode("utf-8").rstrip("=")
        
        signature = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            b64_payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        return f"{b64_payload}.{signature}"

    @staticmethod
    def decode_token(token: str) -> Optional[dict]:
        try:
            parts = token.split(".")
            if len(parts) != 2:
                return None
            b64_payload, signature = parts
            
            # Verify signature
            expected_sig = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                b64_payload.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(expected_sig, signature):
                return None

            # Add padding
            rem = len(b64_payload) % 4
            padded = b64_payload + ("=" * (4 - rem) if rem else "")
            payload_bytes = base64.urlsafe_b64decode(padded)
            payload = json.loads(payload_bytes.decode("utf-8"))

            if payload.get("exp", 0) < time.time():
                return None  # Token expired

            return payload
        except Exception:
            return None

    @staticmethod
    def authenticate_user(db: Session, username_or_email: str, password: str) -> Optional[User]:
        user = db.query(User).filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        ).first()
        if not user:
            return None
        if not AuthService.verify_password(password, user.hashed_password):
            return None
        if not user.is_active:
            return None
        
        user.last_login_at = datetime.utcnow()
        db.commit()
        return user

    @staticmethod
    def create_user(db: Session, username: str, email: str, password: str, role: str = "user") -> User:
        user = User(
            username=username.strip(),
            email=email.strip().lower(),
            hashed_password=AuthService.hash_password(password),
            role=role,
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        AuthService.log_audit(db, action="register", message=f"Người dùng mới {user.username} đã đăng ký", user_id=user.id)
        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=True)
        except Exception:
            pass
        return user

    @staticmethod
    def get_google_redirect_uri(request: Request, db: Session) -> str:
        import re
        from app.services.settings_service import SettingsService
        custom_uri = SettingsService.get_setting(db, "google_redirect_uri") or settings.GOOGLE_REDIRECT_URI
        if custom_uri and custom_uri.strip():
            return custom_uri.strip()

        # Prioritize explicitly configured PUBLIC_API_BASE_URL
        if settings.PUBLIC_API_BASE_URL and "localhost" not in settings.PUBLIC_API_BASE_URL:
            return f"{settings.PUBLIC_API_BASE_URL.rstrip('/')}/auth/google/callback"

        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        proto = request.headers.get("x-forwarded-proto")

        if "hf.space" in host:
            proto = "https"
        elif not proto:
            proto = "https" if request.url.scheme == "https" else "http"

        # Host Header Injection Protection: only allow legitimate domain suffixes or local dev
        allowed_hosts_pattern = r"^([a-zA-Z0-9-]+\.)*(oloka\.net|hf\.space|localhost|127\.0\.0\.1)(:\d+)?$"
        if host and re.match(allowed_hosts_pattern, host):
            return f"{proto}://{host}/auth/google/callback"

        return f"{str(request.base_url).rstrip('/')}/auth/google/callback"


    @staticmethod
    def get_or_create_google_user(
        db: Session,
        email: str,
        name: str = "",
        avatar_url: str = None,
        google_id: str = None,
        designated_admin_email: str = "phucsd@gmail.com"
    ) -> tuple[User, bool]:
        """
        Returns (user, is_created).
        Guarantees that designated_admin_email always gets role='admin'.
        """
        email = email.strip().lower()
        is_admin_email = (email == designated_admin_email.strip().lower())

        user = db.query(User).filter(User.email == email).first()
        if user:
            # User already exists
            if is_admin_email and user.role != "admin":
                user.role = "admin"
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
            if google_id and not user.google_id:
                user.google_id = google_id
            db.commit()
            return user, False

        # If this is the designated admin, link to existing default admin account if available
        if is_admin_email:
            placeholder_admin = db.query(User).filter(User.username == "admin").first()
            if placeholder_admin:
                placeholder_admin.email = email
                placeholder_admin.role = "admin"
                placeholder_admin.is_active = True
                if avatar_url:
                    placeholder_admin.avatar_url = avatar_url
                if google_id:
                    placeholder_admin.google_id = google_id
                db.commit()
                print(f"[Auth] Linked default admin account to Google email: {email}")
                return placeholder_admin, False

        # Generate unique username
        base_username = re.sub(r'[^a-zA-Z0-9_]', '', email.split('@')[0])
        if len(base_username) < 3:
            base_username = f"user_{secrets.token_hex(3)}"

        cand = base_username
        suffix = 1
        while db.query(User).filter(User.username == cand).first():
            cand = f"{base_username}_{suffix}"
            suffix += 1

        random_pwd = secrets.token_urlsafe(32)
        new_user = User(
            username=cand,
            email=email,
            hashed_password=AuthService.hash_password(random_pwd),
            role="admin" if is_admin_email else "user",
            is_active=True,
            avatar_url=avatar_url,
            google_id=google_id,
            kaggle_username=settings.KAGGLE_USERNAME if is_admin_email else None,
            kaggle_key=settings.KAGGLE_KEY if is_admin_email else None
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        AuthService.log_audit(
            db,
            action="google_register",
            message=f"Đăng ký tài khoản mới qua Google ({email}) với quyền {new_user.role}",
            user_id=new_user.id
        )

        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=True)
        except Exception:
            pass

        return new_user, True

    @staticmethod
    def seed_default_admin(db: Session) -> User:
        import secrets
        from app.services.settings_service import SettingsService
        
        default_pwd = settings.ADMIN_DEFAULT_PASSWORD
        generated = False
        if not default_pwd:
            default_pwd = secrets.token_urlsafe(16)
            generated = True

        force_reset = os.environ.get("RESET_ADMIN_PASSWORD", "false").lower() in ("true", "1", "yes")
        admin_google_email = (SettingsService.get_setting(db, "admin_google_email") or settings.ADMIN_GOOGLE_EMAIL or "phucsd@gmail.com").strip().lower()

        admin = db.query(User).filter((User.username == "admin") | (User.role == "admin") | (User.email == admin_google_email)).first()
        if not admin:
            admin = User(
                username="admin",
                email=admin_google_email,
                hashed_password=AuthService.hash_password(default_pwd),
                role="admin",
                is_active=True,
                kaggle_username=settings.KAGGLE_USERNAME,
                kaggle_key=settings.KAGGLE_KEY
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            if generated:
                print(f"🔒 [SECURITY ALERT] Bootstrapped initial admin user 'admin' with random password: {default_pwd}")
                print(f"👉 Please save this password or sign in via Google OAuth ({admin_google_email}) and change it immediately!")
            else:
                print(f"[Auth] Seeded initial admin user (admin / {admin_google_email})")
        else:
            # Upgrade placeholder email to admin_google_email
            if admin.email in ("admin@olokatts.com", "", None):
                admin.email = admin_google_email
                admin.role = "admin"
                db.commit()
                print(f"[Auth] Updated admin email to {admin_google_email}")
            if force_reset:
                admin.hashed_password = AuthService.hash_password(default_pwd)
                admin.is_active = True
                db.commit()
                if generated:
                    print(f"🔒 [SECURITY ALERT] Force-reset admin password to new random password: {default_pwd}")
                else:
                    print(f"[Auth] Force-reset admin password via RESET_ADMIN_PASSWORD")
        return admin


    @staticmethod
    def log_audit(db: Session, action: str, message: str, user_id: str = None, level: str = "INFO", details: str = None) -> AuditLog:
        try:
            log = AuditLog(
                user_id=user_id,
                level=level,
                action=action,
                message=message,
                details=details
            )
            db.add(log)
            db.commit()
            try:
                from app.services.db_sync_service import DbSyncService
                DbSyncService.backup_database(immediate=False)
            except Exception:
                pass
            return log
        except Exception as e:
            print(f"⚠️ Failed to write audit log: {e}")
            return None

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """
    Extracts user from:
    1. HTTP-Only Cookie 'session_token'
    2. Authorization header: Bearer <session_token> OR Bearer <api_key>
    (Query parameters are strictly disallowed to prevent token leakage in logs/history/referers)
    """
    token = request.cookies.get("session_token")
    auth_header = request.headers.get("Authorization")
    
    if not token and auth_header:
        parts = auth_header.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]

    if not token:
        return None

    # Check if token is a signed session token
    payload = AuthService.decode_token(token)
    if payload and "sub" in payload:
        user = db.query(User).filter(User.id == payload["sub"], User.is_active == True).first()
        if user:
            return user

    # Check if token is a validated ApiKey
    try:
        from app.services.api_key_service import ApiKeyService
        api_key_obj = ApiKeyService.validate_api_key(db, token)
        if api_key_obj:
            user = db.query(User).filter(User.id == api_key_obj.user_id, User.is_active == True).first()
            if user:
                request.state.api_key = api_key_obj
                return user
    except Exception as _e:
        pass

    return None

def get_current_user(request: Request, user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if not user:
        if request.url.path.startswith("/api/") or request.url.path.startswith("/v1/"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Yêu cầu đăng nhập hoặc cung cấp API Key hợp lệ qua Header Authorization: Bearer <key>"
            )
        # HTML request: redirect to login
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={request.url.path}"}
        )
    return user

def get_current_admin(request: Request, user: User = Depends(get_current_user)) -> User:
    # If caller authenticated via API Key, enforce explicit admin scope and block web HTML access
    api_key_obj = getattr(request.state, "api_key", None)
    if api_key_obj:
        granted_scopes = [s.strip() for s in (api_key_obj.scopes or "").split(",")]
        if "admin:access" not in granted_scopes and "full:access" not in granted_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key không có quyền Quản trị viên (yêu cầu scope 'admin:access' hoặc 'full:access')"
            )
        # Block API keys from navigating HTML admin panel pages
        if not request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Giao diện quản trị HTML chỉ cho phép truy cập qua phiên đăng nhập Cookie, không dùng API Key."
            )

    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền truy cập trang Quản trị viên!"
        )
    return user

