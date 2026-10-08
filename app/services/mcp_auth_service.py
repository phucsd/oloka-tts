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
    def create_pairing_session(db: Session, client_name: str = "ChatGPT") -> McpPairingSession:
        """Creates a new pending pairing session for an AI Agent / MCP client."""
        for _ in range(10):
            code = McpAuthService.generate_pair_code()
            if not db.query(McpPairingSession).filter(McpPairingSession.code == code).first():
                break

        session = McpPairingSession(
            code=code,
            status="pending",
            client_name=client_name,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=30)
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get_pairing_session(db: Session, code: str) -> Optional[McpPairingSession]:
        """Fetches and checks validity of a pairing session."""
        if not code:
            return None
        clean_code = code.strip().upper()
        session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
        if not session:
            return None
        
        # Expire if pending and beyond 30m window
        if session.status == "pending" and session.expires_at < datetime.utcnow():
            session.status = "expired"
            db.commit()

        return session

    @staticmethod
    def approve_pairing_session(db: Session, code: str, user_id: str) -> Tuple[bool, str]:
        """Approves and binds the pairing session to a logged-in user."""
        session = McpAuthService.get_pairing_session(db, code)
        if not session:
            return False, "Mã ghép đôi không tồn tại hoặc không hợp lệ."
        if session.status == "authorized":
            return True, "Phiên này đã được cấp quyền trước đó."
        if session.status == "expired":
            return False, "Mã ghép đôi đã hết hạn. Vui lòng tạo yêu cầu mới từ ChatGPT."
        if session.status == "revoked":
            return False, "Mã ghép đôi này đã bị hủy bỏ."

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

        return True, "Cấp quyền thành công!"

    @staticmethod
    def revoke_pairing_session(db: Session, code: str, user_id: str = None, is_admin: bool = False) -> bool:
        """Revokes an active pairing session."""
        clean_code = code.strip().upper()
        query = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code)
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
    def resolve_caller(db: Session, pair_code: Optional[str] = None, api_key: Optional[str] = None) -> Optional[User]:
        """
        Resolves caller identity to an active User.
        Supports:
        1. API Key (Bearer token or parameter)
        2. Authorized MCP Pairing Session (pair_code)
        """
        # 1. API Key resolution
        if api_key and api_key.strip():
            key_clean = api_key.strip()
            ak = ApiKeyService.validate_api_key(db, key_clean)
            if ak:
                user = db.query(User).filter(User.id == ak.user_id, User.is_active == True).first()
                if user:
                    return user
            # Fallback legacy user.api_key
            user = db.query(User).filter(User.api_key == key_clean, User.is_active == True).first()
            if user:
                return user

        # 2. Pair Code resolution
        if pair_code and pair_code.strip():
            clean_code = pair_code.strip().upper()
            session = db.query(McpPairingSession).filter(McpPairingSession.code == clean_code).first()
            if session and session.status == "authorized" and session.user_id:
                if session.expires_at and session.expires_at < datetime.utcnow():
                    session.status = "expired"
                    db.commit()
                    return None
                user = db.query(User).filter(User.id == session.user_id, User.is_active == True).first()
                if user:
                    session.last_used_at = datetime.utcnow()
                    db.commit()
                    return user

        # 3. Explicit check: Never auto-fallback to another user's session!
        # Every client/session MUST supply its own pair_code or api_key.
        return None

    @staticmethod
    def get_latest_pending_session(db: Session, max_age_minutes: int = 15) -> Optional[McpPairingSession]:
        """Returns the most recent active pending pairing session, if created within window."""
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        return db.query(McpPairingSession).filter(
            McpPairingSession.status == "pending",
            McpPairingSession.created_at >= cutoff,
            McpPairingSession.expires_at > datetime.utcnow()
        ).order_by(McpPairingSession.created_at.desc()).first()

    @staticmethod
    def list_user_sessions(db: Session, user_id: str) -> list[McpPairingSession]:
        """Lists active and recent MCP pairing sessions for a user."""
        return db.query(McpPairingSession).filter(
            McpPairingSession.user_id == user_id
        ).order_by(McpPairingSession.created_at.desc()).limit(20).all()
