import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.models import McpPairingSession, User
from app.services.api_key_service import ApiKeyService

class McpAuthService:
    @staticmethod
    def generate_pair_code() -> str:
        """Generates a memorable, human-friendly code, e.g. OLK-7H92FA"""
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        rand_str = "".join(secrets.choice(chars) for _ in range(6))
        return f"OLK-{rand_str}"

    @staticmethod
    def create_pairing_session(
        db: Session,
        client_name: str = "ChatGPT",
        transport_session_id: Optional[str] = None
    ) -> McpPairingSession:
        """Creates a new unique pairing session with a one-time code and a long-term session token."""
        for _ in range(10):
            code = McpAuthService.generate_pair_code()
            if not db.query(McpPairingSession).filter(McpPairingSession.code == code).first():
                break

        session_token = f"mcp_tok_{secrets.token_urlsafe(32)}"

        session = McpPairingSession(
            code=code,
            session_token=session_token,
            transport_session_id=transport_session_id.strip() if transport_session_id else None,
            status="pending",
            client_name=client_name,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=15)
        )
        db.add(session)
        db.commit()
        db.refresh(session)
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
        """Fetches and checks validity of a pairing session."""
        if not code:
            return None
        clean_code = code.strip().upper()
        session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
        if not session:
            # Check by session_token as fallback identifier
            session = db.query(McpPairingSession).filter(McpPairingSession.session_token == code.strip()).first()
        if not session:
            return None
        
        # Expire if pending and beyond window
        if session.status == "pending" and session.expires_at < datetime.utcnow():
            session.status = "expired"
            db.commit()

        return session

    @staticmethod
    def get_session_by_token(db: Session, session_token: str) -> Optional[McpPairingSession]:
        """Fetches pairing session by session_token."""
        if not session_token or not session_token.strip():
            return None
        return db.query(McpPairingSession).filter(McpPairingSession.session_token == session_token.strip()).first()

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
        query = db.query(McpPairingSession).filter(
            (McpPairingSession.code == clean_id.upper()) |
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
        1. Validated API Key (via ApiKeyService, zero legacy fallback)
        2. Authorized Transport Session ID (bound at MCP connection/transport layer)
        3. Authorized Session Token (long-term session credential)
        4. Authorized Pair Code (explicit code verification)
        """
        # 1. API Key resolution (strict via ApiKeyService)
        if api_key and api_key.strip():
            key_clean = api_key.strip()
            ak = ApiKeyService.validate_api_key(db, key_clean)
            if ak:
                user = db.query(User).filter(User.id == ak.user_id, User.is_active == True).first()
                if user:
                    return user
            return None

        # 2. Long-term Session Token resolution (explicit credential)
        if session_token and session_token.strip():
            s_tok = session_token.strip()
            s_sess = db.query(McpPairingSession).filter(
                McpPairingSession.session_token == s_tok,
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
                    s_sess.last_used_at = datetime.utcnow()
                    db.commit()
                    return user
            return None

        # 3. Pair Code resolution (explicit one-time code)
        if pair_code and pair_code.strip():
            clean_code = pair_code.strip().upper()
            session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
            if session and session.status == "authorized" and session.user_id:
                # Disregard legacy bogus 'sess_' IDs
                stored_tid = session.transport_session_id
                if stored_tid and stored_tid.startswith("sess_"):
                    stored_tid = None
                curr_tid = transport_session_id.strip() if transport_session_id else None
                if curr_tid and curr_tid.startswith("sess_"):
                    curr_tid = None

                # Only verify cross-transport matching if BOTH are real transport identifiers
                if stored_tid and curr_tid and stored_tid != curr_tid:
                    return None
                if session.expires_at and session.expires_at < datetime.utcnow():
                    session.status = "expired"
                    db.commit()
                    return None
                user = db.query(User).filter(User.id == session.user_id, User.is_active == True).first()
                if user:
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
