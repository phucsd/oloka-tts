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

        # Only store hash in api_keys table; do NOT store plaintext in user.api_key
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
    def migrate_legacy_keys(cls, db: Session) -> int:
        """
        Idempotent migration: finds any users with legacy plaintext user.api_key,
        migrates them into hashed ApiKey records with standard permissions (NOT full:access),
        and safely nullifies the legacy plaintext column.
        """
        migrated_count = 0
        try:
            legacy_users = db.query(User).filter(
                User.api_key.isnot(None),
                User.api_key != ""
            ).all()

            for u in legacy_users:
                raw_k = u.api_key.strip()
                if not raw_k:
                    u.api_key = None
                    continue

                k_hash = cls.hash_secret(raw_k)
                existing = db.query(ApiKey).filter(ApiKey.key_hash == k_hash).first()
                if not existing:
                    prefix = cls.format_prefix(raw_k)
                    # Grant standard non-admin scopes
                    scopes = "tts:generate,voices:read" if u.role != "admin" else "tts:generate,voices:read,admin:access"
                    migrated_key = ApiKey(
                        id=generate_id("key"),
                        user_id=u.id,
                        name="Default API Key",
                        key_prefix=prefix,
                        key_hash=k_hash,
                        scopes=scopes,
                        is_active=True,
                        created_at=u.created_at or datetime.utcnow()
                    )
                    db.add(migrated_key)
                    migrated_count += 1
                
                # Nullify legacy plaintext column to eliminate fallback attack surface
                u.api_key = None

            if legacy_users:
                db.commit()
                if migrated_count > 0:
                    print(f"🔒 [ApiKeyService] Successfully migrated {migrated_count} legacy API keys to hashed storage.")
        except Exception as e:
            db.rollback()
            print(f"⚠️ [ApiKeyService] Error during legacy API key migration: {e}")

        return migrated_count

    @classmethod
    def validate_api_key(
        cls,
        db: Session,
        raw_key: str,
        required_scope: Optional[str] = None
    ) -> Optional[ApiKey]:
        """
        Validates raw API key secret against DB.
        Fail-closed:
        - Must exist in api_keys table by hash
        - Must be active (is_active == True)
        - Must not be expired
        - Associated user must be active
        - Must satisfy required_scope
        - ZERO fallback to users.api_key
        - NEVER recreates revoked or deleted keys
        """
        if not raw_key or not isinstance(raw_key, str):
            return None

        clean_key = raw_key.strip()
        key_hash = cls.hash_secret(clean_key)

        # 1. Check in api_keys table by hash
        api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
        if not api_key:
            return None

        # 2. Enforce active status (revoked/deactivated keys rejected immediately)
        if not api_key.is_active:
            return None

        # 3. Check expiration
        if api_key.expires_at and api_key.expires_at < datetime.utcnow():
            return None

        # 4. Check user account active status
        user = db.query(User).filter(User.id == api_key.user_id, User.is_active == True).first()
        if not user:
            return None

        # 5. Check scope if required
        if required_scope:
            granted_scopes = [s.strip() for s in (api_key.scopes or "").split(",") if s.strip()]
            if "full:access" not in granted_scopes:
                # Standardize TTS scopes: tts:generate and tts:write are mutually acceptable
                if required_scope in ("tts:generate", "tts:write"):
                    if "tts:generate" not in granted_scopes and "tts:write" not in granted_scopes:
                        return None
                elif required_scope not in granted_scopes:
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
        return db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc()).all()

