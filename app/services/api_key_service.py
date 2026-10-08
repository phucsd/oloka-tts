import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session
from app.models import ApiKey, User, AuditLog, generate_id

class ApiKeyService:
    PREFIX = "oloka_live_"

    @staticmethod
    def hash_secret(secret: str) -> str:
        """Computes SHA-256 hash of raw API secret."""
        return hashlib.sha256(secret.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def generate_secret() -> str:
        """Generates standard industrial format secret: oloka_live_<32_hex_chars>"""
        return f"{ApiKeyService.PREFIX}{secrets.token_hex(16)}"

    @staticmethod
    def format_prefix(secret: str) -> str:
        """Returns safe masked representation for UI, e.g. oloka_live_a1b2...c3d4"""
        if len(secret) > 18:
            return f"{secret[:15]}...{secret[-4:]}"
        return f"{secret[:6]}...{secret[-2:]}"

    @classmethod
    def create_api_key(
        cls,
        db: Session,
        user_id: str,
        name: str = "API Key",
        scopes: str = "tts:generate,voices:read",
        expires_days: Optional[int] = None
    ) -> Tuple[ApiKey, str]:
        """
        Creates a new API key for user.
        Returns (ApiKey record, raw_secret_string).
        NOTE: raw_secret_string must be presented to the user ONCE, as it cannot be retrieved again.
        """
        raw_secret = cls.generate_secret()
        key_hash = cls.hash_secret(raw_secret)
        key_prefix = cls.format_prefix(raw_secret)

        expires_at = None
        if expires_days and expires_days > 0:
            expires_at = datetime.utcnow() + timedelta(days=expires_days)

        api_key = ApiKey(
            id=generate_id("key"),
            user_id=user_id,
            name=name.strip() or "API Key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes.strip(),
            is_active=True,
            expires_at=expires_at,
            created_at=datetime.utcnow()
        )
        db.add(api_key)

        # Also sync to user.api_key for backward compatibility
        user = db.query(User).filter(User.id == user_id).first()
        if user and not user.api_key:
            user.api_key = raw_secret

        db.commit()
        db.refresh(api_key)

        # Audit log
        AuditLog_rec = AuditLog(
            user_id=user_id,
            action="create_api_key",
            level="INFO",
            message=f"Đã tạo API Key mới: '{api_key.name}' ({key_prefix})"
        )
        db.add(AuditLog_rec)
        db.commit()

        return api_key, raw_secret

    @classmethod
    def validate_api_key(
        cls,
        db: Session,
        raw_key: str,
        required_scope: Optional[str] = None
    ) -> Optional[ApiKey]:
        """
        Validates raw API key secret against DB.
        Returns ApiKey object if valid, None if invalid or expired.
        """
        if not raw_key or not isinstance(raw_key, str):
            return None

        clean_key = raw_key.strip()
        key_hash = cls.hash_secret(clean_key)

        # 1. Check in api_keys table by hash
        api_key = db.query(ApiKey).filter(
            ApiKey.key_hash == key_hash,
            ApiKey.is_active == True
        ).first()

        # 2. Backward compatibility fallback: check legacy user.api_key
        if not api_key:
            legacy_user = db.query(User).filter(
                User.api_key == clean_key,
                User.is_active == True
            ).first()
            if legacy_user:
                # Auto-migrate legacy key into api_keys table
                prefix = cls.format_prefix(clean_key)
                api_key = ApiKey(
                    id=generate_id("key"),
                    user_id=legacy_user.id,
                    name="Legacy API Key",
                    key_prefix=prefix,
                    key_hash=key_hash,
                    scopes="full:access",
                    is_active=True,
                    created_at=datetime.utcnow()
                )
                db.add(api_key)
                db.commit()
                db.refresh(api_key)

        if not api_key:
            return None

        # Check expiration
        if api_key.expires_at and api_key.expires_at < datetime.utcnow():
            return None

        # Check scope if required
        if required_scope:
            granted_scopes = [s.strip() for s in (api_key.scopes or "").split(",")]
            if "full:access" not in granted_scopes and required_scope not in granted_scopes:
                return None

        return api_key

    @classmethod
    def record_usage(cls, db: Session, api_key_id: str, chars: int = 0):
        """Asynchronously updates last used timestamp and counters."""
        try:
            api_key = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
            if api_key:
                api_key.last_used_at = datetime.utcnow()
                api_key.total_requests = (api_key.total_requests or 0) + 1
                api_key.total_characters = (api_key.total_characters or 0) + max(0, chars)
                db.commit()
        except Exception as e:
            db.rollback()
            print(f"⚠️ Could not record API key usage: {e}")

    @classmethod
    def revoke_api_key(cls, db: Session, user_id: str, key_id: str, is_admin: bool = False) -> bool:
        """Deactivates an API key."""
        query = db.query(ApiKey).filter(ApiKey.id == key_id)
        if not is_admin:
            query = query.filter(ApiKey.user_id == user_id)
        
        api_key = query.first()
        if not api_key:
            return False

        api_key.is_active = False
        db.commit()

        AuditLog_rec = AuditLog(
            user_id=user_id,
            action="revoke_api_key",
            level="WARNING",
            message=f"Đã thu hồi API Key: '{api_key.name}' ({api_key.key_prefix})"
        )
        db.add(AuditLog_rec)
        db.commit()
        return True

    @classmethod
    def delete_api_key(cls, db: Session, user_id: str, key_id: str, is_admin: bool = False) -> bool:
        """Permanently removes an API key record."""
        query = db.query(ApiKey).filter(ApiKey.id == key_id)
        if not is_admin:
            query = query.filter(ApiKey.user_id == user_id)

        api_key = query.first()
        if not api_key:
            return False

        name = api_key.name
        prefix = api_key.key_prefix
        db.delete(api_key)
        db.commit()

        AuditLog_rec = AuditLog(
            user_id=user_id,
            action="delete_api_key",
            level="WARNING",
            message=f"Đã xóa API Key: '{name}' ({prefix})"
        )
        db.add(AuditLog_rec)
        db.commit()
        return True

    @classmethod
    def list_user_api_keys(cls, db: Session, user_id: str) -> List[ApiKey]:
        """Returns all keys owned by user ordered by creation time descending."""
        # Ensure user has at least one key if they have legacy user.api_key
        user = db.query(User).filter(User.id == user_id).first()
        keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc()).all()
        
        if not keys and user and user.api_key:
            # Auto-seed legacy key into table
            prefix = cls.format_prefix(user.api_key)
            key_hash = cls.hash_secret(user.api_key)
            legacy_key = ApiKey(
                id=generate_id("key"),
                user_id=user.id,
                name="Default API Key",
                key_prefix=prefix,
                key_hash=key_hash,
                scopes="full:access",
                is_active=True,
                created_at=user.created_at or datetime.utcnow()
            )
            db.add(legacy_key)
            db.commit()
            db.refresh(legacy_key)
            keys = [legacy_key]

        return keys
