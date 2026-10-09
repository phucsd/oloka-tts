import hashlib
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.models import McpPairingSession, User, OAuthToken
from app.services.api_key_service import ApiKeyService

class McpAuthService:
    @staticmethod
    def generate_pair_code() -> str:
        """Generates a memorable, human-friendly code, e.g. OLK-7H92FA"""
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        rand_str = "".join(secrets.choice(chars) for _ in range(6))
        return f"OLK-{rand_str}"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Computes SHA-256 hash of a session token."""
        return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def create_pairing_session(
        db: Session,
        client_name: str = "ChatGPT",
        transport_session_id: Optional[str] = None
    ) -> McpPairingSession:
        """
        Creates or retrieves an active pending pairing session strictly bound to this transport session.
        Guarantees idempotency: multiple calls from the same transport reuse the same active code.
        Stores secret session token securely via SHA-256 hash.
        """
        # Idempotency check: if this transport already has an active pending session, reuse it
        if transport_session_id and transport_session_id.strip():
            existing = McpAuthService.get_pending_session_for_transport(db, transport_session_id.strip())
            if existing:
                return existing

        for _ in range(10):
            code = McpAuthService.generate_pair_code()
            if not db.query(McpPairingSession).filter(McpPairingSession.code == code).first():
                break

        raw_session_token = f"mcp_tok_{secrets.token_urlsafe(32)}"
        token_hash = McpAuthService.hash_token(raw_session_token)
        # Store masked prefix in session_token for auditing, never full plaintext
        masked_token = f"{raw_session_token[:12]}...{raw_session_token[-4:]}"

        session = McpPairingSession(
            code=code,
            session_token=masked_token,
            session_token_hash=token_hash,
            transport_session_id=transport_session_id.strip() if transport_session_id else None,
            status="pending",
            client_name=client_name,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=15)
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        # Attach in-memory raw token for one-time return if needed by API callers
        session._raw_token = raw_session_token
        return session

    @staticmethod
    def get_pending_session_for_transport(
        db: Session,
        transport_session_id: str
    ) -> Optional[McpPairingSession]:
        """
        Returns active pending pairing session strictly bound to this transport session.
        Never performs global lookups across different clients.
        """
        if not transport_session_id or not transport_session_id.strip():
            return None
        return db.query(McpPairingSession).filter(
            McpPairingSession.transport_session_id == transport_session_id.strip(),
            McpPairingSession.status == "pending",
            McpPairingSession.expires_at > datetime.utcnow()
        ).order_by(McpPairingSession.created_at.desc()).first()

    @staticmethod
    def get_pairing_session(db: Session, code: str) -> Optional[McpPairingSession]:
        """Fetches and checks validity of a pairing session by code or token."""
        if not code:
            return None
        clean_code = code.strip().upper()
        session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
        if not session:
            # Check by token hash
            t_hash = McpAuthService.hash_token(code)
            session = db.query(McpPairingSession).filter(
                (McpPairingSession.session_token_hash == t_hash) |
                (McpPairingSession.session_token == code.strip())
            ).first()
        if not session:
            return None
        
        # Expire if pending and beyond window
        if session.status == "pending" and session.expires_at < datetime.utcnow():
            session.status = "expired"
            db.commit()

        return session

    @staticmethod
    def get_session_by_token(db: Session, session_token: str) -> Optional[McpPairingSession]:
        """Fetches pairing session by session_token (via SHA-256 hash or legacy plaintext match)."""
        if not session_token or not session_token.strip():
            return None
        raw_tok = session_token.strip()
        t_hash = McpAuthService.hash_token(raw_tok)
        return db.query(McpPairingSession).filter(
            (McpPairingSession.session_token_hash == t_hash) |
            (McpPairingSession.session_token == raw_tok)
        ).first()

    @staticmethod
    def approve_pairing_session(db: Session, code: str, user_id: str) -> Tuple[bool, str, Optional[str]]:
        """Approves and binds the pairing session to a logged-in user. Returns (success, message, session_token)."""
        session = McpAuthService.get_pairing_session(db, code)
        if not session:
            return False, "Mã ghép đôi không tồn tại hoặc không hợp lệ.", None
        if session.status == "authorized":
            return True, "Phiên này đã được cấp quyền trước đó.", session.session_token
        if session.status == "expired":
            return False, "Mã ghép đôi đã hết hạn. Vui lòng tạo yêu cầu mới từ AI client.", None
        if session.status == "revoked":
            return False, "Mã ghép đôi này đã bị hủy bỏ.", None

        session.user_id = user_id
        session.status = "authorized"
        session.authorized_at = datetime.utcnow()
        session.last_used_at = datetime.utcnow()
        # Keep authorized session active for 30 days
        session.expires_at = datetime.utcnow() + timedelta(days=30)
        db.commit()

        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=True)
        except Exception:
            pass

        return True, "Cấp quyền thành công!", session.session_token

    @staticmethod
    def revoke_pairing_session(db: Session, identifier: str, user_id: str = None, is_admin: bool = False) -> bool:
        """Revokes an active pairing session by code, session_token, or ID."""
        clean_id = identifier.strip()
        t_hash = McpAuthService.hash_token(clean_id)
        query = db.query(McpPairingSession).filter(
            (McpPairingSession.code == clean_id.upper()) |
            (McpPairingSession.session_token_hash == t_hash) |
            (McpPairingSession.session_token == clean_id) |
            (McpPairingSession.id == clean_id)
        )
        if not is_admin and user_id:
            query = query.filter(McpPairingSession.user_id == user_id)
        
        session = query.first()
        if not session:
            return False

        session.status = "revoked"
        db.commit()
        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=True)
        except Exception:
            pass
        return True

    @staticmethod
    def revoke_oauth_token(db: Session, raw_token_or_hash: str) -> bool:
        """
        Revokes an OAuth 2.1 access token or refresh token and all tokens in its family.
        RFC 7009 compliant.
        """
        if not raw_token_or_hash or not raw_token_or_hash.strip():
            return False
        clean = raw_token_or_hash.strip()
        t_hash = McpAuthService.hash_token(clean)
        
        target = db.query(OAuthToken).filter(
            (OAuthToken.token_hash == t_hash) |
            (OAuthToken.id == clean) |
            (OAuthToken.token_id == clean)
        ).first()

        now = datetime.utcnow()
        if target:
            target.revoked_at = now
            if target.family_id:
                db.query(OAuthToken).filter(
                    OAuthToken.family_id == target.family_id,
                    OAuthToken.revoked_at.is_(None)
                ).update({"revoked_at": now})
            db.commit()
            return True

        sessions = db.query(McpPairingSession).filter(
            (McpPairingSession.session_token_hash == t_hash) |
            (McpPairingSession.session_token == clean) |
            (McpPairingSession.code == clean.upper())
        ).all()
        if sessions:
            for s in sessions:
                s.status = "revoked"
            db.commit()
            return True

        return False

    @staticmethod
    def validate_bearer_token(
        db: Session,
        raw_token: str
    ) -> Tuple[Optional[User], Optional[OAuthToken], Optional[str]]:
        """
        Validates an OAuth 2.1 access token or API key for MCP.
        Returns (user, oauth_token_obj, error_reason).
        Strictly rejects generic web JWTs and expired/revoked tokens.
        """
        if not raw_token or not raw_token.strip():
            return None, None, "missing_token"

        token_clean = raw_token.strip()

        # 1. Dedicated OAuth 2.1 Access Token (olk_atk_...)
        if token_clean.startswith("olk_atk_"):
            t_hash = McpAuthService.hash_token(token_clean)
            otok = db.query(OAuthToken).filter(
                OAuthToken.token_hash == t_hash,
                OAuthToken.token_type == "access_token"
            ).first()
            if not otok:
                return None, None, "invalid_token"
            if otok.revoked_at is not None:
                return None, None, "token_revoked"
            if otok.expires_at < datetime.utcnow():
                return None, None, "token_expired"
            user = db.query(User).filter(User.id == otok.user_id, User.is_active == True).first()
            if not user:
                return None, None, "user_inactive"
            otok.last_used_at = datetime.utcnow()
            db.commit()
            return user, otok, None

        # 2. Oloka API Key (oloka_live_...)
        if token_clean.startswith("oloka_live_"):
            ak = ApiKeyService.validate_api_key(db, token_clean)
            if ak:
                user = db.query(User).filter(User.id == ak.user_id, User.is_active == True).first()
                if user:
                    return user, None, None
            return None, None, "invalid_api_key"

        # Web-login JWTs or unknown formats are strictly rejected for MCP
        return None, None, "unsupported_token_type"

    @staticmethod
    def resolve_caller(
        db: Session,
        pair_code: Optional[str] = None,
        session_token: Optional[str] = None,
        api_key: Optional[str] = None,
        transport_session_id: Optional[str] = None
    ) -> Optional[User]:
        """
        Resolves caller identity to an active User.
        Fail-closed priority:
        1. Validated OAuth 2.1 Access Token (olk_atk_...) OR API Key (oloka_live_...)
        2. Authorized Transport Session ID (bound at MCP connection/transport layer)
        3. Authorized Session Token (long-term session credential hash)
        4. Authorized Pair Code (explicit code verification)
        """
        # 1. API Key or Bearer Token resolution
        if api_key and api_key.strip():
            user, otok, _ = McpAuthService.validate_bearer_token(db, api_key.strip())
            if user:
                if otok:
                    user._oauth_token = otok
                return user
            return None

        # 2. Long-term Session Token resolution (explicit credential via hash or legacy plaintext)
        if session_token and session_token.strip():
            s_tok = session_token.strip()
            t_hash = McpAuthService.hash_token(s_tok)
            s_sess = db.query(McpPairingSession).filter(
                (McpPairingSession.session_token_hash == t_hash) |
                (McpPairingSession.session_token == s_tok),
                McpPairingSession.status == "authorized"
            ).first()
            if s_sess:
                # Disregard legacy bogus 'sess_' IDs (from memory address hashing)
                stored_tid = s_sess.transport_session_id
                if stored_tid and stored_tid.startswith("sess_"):
                    stored_tid = None
                curr_tid = transport_session_id.strip() if transport_session_id else None
                if curr_tid and curr_tid.startswith("sess_"):
                    curr_tid = None

                # Only verify cross-transport matching if BOTH are real transport identifiers
                if stored_tid and curr_tid and stored_tid != curr_tid:
                    return None
                if s_sess.expires_at and s_sess.expires_at < datetime.utcnow():
                    s_sess.status = "expired"
                    db.commit()
                    return None
                user = db.query(User).filter(User.id == s_sess.user_id, User.is_active == True).first()
                if user:
                    # Auto-bind transport session if not yet bound
                    if curr_tid and not s_sess.transport_session_id:
                        s_sess.transport_session_id = curr_tid
                    s_sess.last_used_at = datetime.utcnow()
                    db.commit()
                    return user
            return None

        # 3. Pair Code resolution (explicit one-time / session code)
        if pair_code and pair_code.strip():
            clean_code = pair_code.strip().upper()
            session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
            if session and session.status == "authorized" and session.user_id:
                curr_tid = transport_session_id.strip() if transport_session_id else None
                if curr_tid and curr_tid.startswith("sess_"):
                    curr_tid = None

                if session.expires_at and session.expires_at < datetime.utcnow():
                    session.status = "expired"
                    db.commit()
                    return None
                user = db.query(User).filter(User.id == session.user_id, User.is_active == True).first()
                if user:
                    # Dynamically bind / update transport session so future ambient calls from this client succeed
                    if curr_tid and not curr_tid.startswith("sess_"):
                        session.transport_session_id = curr_tid
                    session.last_used_at = datetime.utcnow()
                    db.commit()
                    return user
            return None

        # 4. Ambient Transport Session resolution (when no explicit credential supplied)
        if transport_session_id and transport_session_id.strip():
            curr_tid = transport_session_id.strip()
            # Only resolve real persistent transport IDs (not transient memory sess_ addresses)
            if not curr_tid.startswith("sess_"):
                t_session = db.query(McpPairingSession).filter(
                    McpPairingSession.transport_session_id == curr_tid,
                    McpPairingSession.status == "authorized"
                ).first()
                if t_session:
                    if t_session.expires_at and t_session.expires_at < datetime.utcnow():
                        t_session.status = "expired"
                        db.commit()
                    else:
                        user = db.query(User).filter(User.id == t_session.user_id, User.is_active == True).first()
                        if user:
                            t_session.last_used_at = datetime.utcnow()
                            db.commit()
                            return user

        # 5. Fail-closed: Never auto-fallback to another user's session!
        return None

    @staticmethod

    @staticmethod
    def list_user_sessions(db: Session, user_id: str) -> list[McpPairingSession]:
        """Lists active and recent MCP pairing sessions for a user."""
        return db.query(McpPairingSession).filter(
            McpPairingSession.user_id == user_id
        ).order_by(McpPairingSession.created_at.desc()).limit(20).all()

    @staticmethod
    def get_user_gpu_status_description(db: Session, user: User) -> str:
        """Returns accurate, verifiable description of tenant's GPU worker state."""
        from app.models import TTSJob, WorkerSession
        from app.services.kaggle_account_service import KaggleAccountService
        from app.services.kaggle_orchestrator import KaggleOrchestrator
        from app.config import settings

        if user.role == "admin":
            workers = KaggleOrchestrator.get_live_workers(db, user_id=user.id)
            k_user = settings.KAGGLE_USERNAME or user.kaggle_username or "Admin"
            if workers:
                return f"Master Admin GPU (@{k_user}) — {len(workers)} GPU Tesla T4 Đang Hoạt Động"
            return f"Master Admin GPU (@{k_user}) — Sẵn sàng (Tự khởi động khi có job)"

        acc = KaggleAccountService.get_execution_account_for_user(db, user)
        if not acc or not acc.kaggle_username or not acc.kaggle_key:
            return "Chưa cấu hình Kaggle API Key (Cần thiết lập tại https://tts.oloka.net/settings để sử dụng GPU)"

        workers = KaggleOrchestrator.get_live_workers(db, execution_account_id=acc.id)
        if workers:
            return f"Kaggle Cá Nhân (BYOK: @{acc.kaggle_username}) — {len(workers)} GPU Tesla T4 Đang Hoạt Động (Sẵn sàng)"

        booting = db.query(TTSJob).filter(
            TTSJob.execution_account_id == acc.id,
            TTSJob.status == "booting_kaggle"
        ).first()
        if booting:
            return f"Kaggle Cá Nhân (BYOK: @{acc.kaggle_username}) — Kernel đang khởi động GPU..."

        val_str = " (Đã xác minh)" if acc.last_validation_at else " (Chưa kiểm tra kết nối)"
        return f"Kaggle Cá Nhân (BYOK: @{acc.kaggle_username}){val_str} — Worker đang ngủ (Tự khởi động khi có job)"
