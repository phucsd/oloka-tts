import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
import requests
from sqlalchemy.orm import Session
from app.config import settings
from app.models import KaggleExecutionAccount, WorkerToken, User, WorkerSession

class KaggleAccountService:
    @staticmethod
    def get_execution_account_for_user(db: Session, user: Optional[User]) -> Optional[KaggleExecutionAccount]:
        """
        Resolves the dedicated KaggleExecutionAccount for the given user.
        Strict multi-tenant BYOK isolation:
        - User B gets KaggleExecutionAccount with User B's credentials.
        - Admin gets KaggleExecutionAccount with Admin's credentials.
        - Normal users without Kaggle credentials NEVER fallback to Admin!
        """
        if not user:
            return None

        # 1. Check existing execution account
        acc = db.query(KaggleExecutionAccount).filter(
            KaggleExecutionAccount.owner_user_id == user.id,
            KaggleExecutionAccount.is_enabled == True
        ).first()

        if acc:
            # Sync user's latest credentials from User table if updated
            if user.kaggle_username and user.kaggle_key:
                changed = False
                if acc.kaggle_username != user.kaggle_username.strip():
                    acc.kaggle_username = user.kaggle_username.strip()
                    acc.kernel_ref = f"{acc.kaggle_username}/{acc.kernel_slug}"
                    changed = True
                if acc.kaggle_key != user.kaggle_key.strip():
                    acc.kaggle_key = user.kaggle_key.strip()
                    changed = True
                if changed:
                    acc.updated_at = datetime.utcnow()
                    db.commit()
            return acc

        # 2. If no account yet, create from user's BYOK credentials
        if user.kaggle_username and user.kaggle_key:
            k_user = user.kaggle_username.strip()
            k_key = user.kaggle_key.strip()
            slug = f"vieneu-worker-{user.username.lower()[:16]}"
            acc = KaggleExecutionAccount(
                owner_user_id=user.id,
                kaggle_username=k_user,
                kaggle_key=k_key,
                kernel_slug=slug,
                kernel_ref=f"{k_user}/{slug}",
                kernel_title=f"VieNeu Worker {user.username.lower()[:16]}",
                is_enabled=True,
                last_status="configured"
            )
            db.add(acc)
            db.commit()
            db.refresh(acc)
            return acc

        # 3. If admin user and system credentials configured in settings
        if user.role == "admin" and settings.KAGGLE_USERNAME and settings.KAGGLE_KEY:
            k_user = settings.KAGGLE_USERNAME.strip()
            k_key = settings.KAGGLE_KEY.strip()
            slug = settings.KAGGLE_KERNEL_SLUG or "vieneu-tts-dual-t4-worker"
            ref = settings.KAGGLE_KERNEL_REF or f"{k_user}/{slug}"
            acc = KaggleExecutionAccount(
                owner_user_id=user.id,
                kaggle_username=k_user,
                kaggle_key=k_key,
                kernel_slug=slug,
                kernel_ref=ref,
                kernel_title=settings.KAGGLE_KERNEL_TITLE or "VieNeu TTS Dual T4 Worker",
                is_enabled=True,
                last_status="configured"
            )
            db.add(acc)
            db.commit()
            db.refresh(acc)
            return acc

        # 4. Strict isolation: non-admin without credentials has NO execution account
        return None

    @staticmethod
    def create_or_update_account(
        db: Session,
        user_id: str,
        kaggle_username: str,
        kaggle_key: str,
        kernel_slug: Optional[str] = None,
        kernel_title: Optional[str] = None
    ) -> KaggleExecutionAccount:
        k_user = kaggle_username.strip()
        k_key = kaggle_key.strip()
        user = db.query(User).filter(User.id == user_id).first()

        acc = db.query(KaggleExecutionAccount).filter(
            KaggleExecutionAccount.owner_user_id == user_id
        ).first()

        slug = kernel_slug or (f"vieneu-worker-{user.username.lower()[:16]}" if user else "vieneu-worker")
        title = kernel_title or (f"VieNeu TTS Dual T4 Worker ({user.username})" if user else "VieNeu TTS Dual T4 Worker")

        if acc:
            acc.kaggle_username = k_user
            acc.kaggle_key = k_key
            acc.kernel_slug = slug
            acc.kernel_ref = f"{k_user}/{slug}"
            acc.kernel_title = title
            acc.is_enabled = True
            acc.last_status = "configured"
            acc.updated_at = datetime.utcnow()
        else:
            acc = KaggleExecutionAccount(
                owner_user_id=user_id,
                kaggle_username=k_user,
                kaggle_key=k_key,
                kernel_slug=slug,
                kernel_ref=f"{k_user}/{slug}",
                kernel_title=title,
                is_enabled=True,
                last_status="configured"
            )
            db.add(acc)

        # Keep User table in sync for backward compatibility
        if user:
            user.kaggle_username = k_user
            user.kaggle_key = k_key

        db.commit()
        db.refresh(acc)
        return acc

    @staticmethod
    def validate_account_credentials(db: Session, account_id: str) -> Tuple[bool, str]:
        acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == account_id).first()
        if not acc:
            return False, "Không tìm thấy tài khoản thực thi Kaggle"

        try:
            resp = requests.get(
                "https://www.kaggle.com/api/v1/datasets/list?pageSize=1",
                auth=(acc.kaggle_username, acc.kaggle_key),
                timeout=10
            )
            if resp.status_code == 200:
                acc.last_validation_at = datetime.utcnow()
                acc.last_status = "validated"
                db.commit()
                return True, f"Kết nối Kaggle thành công: @{acc.kaggle_username} (Hợp lệ)"
            elif resp.status_code == 401:
                acc.last_status = "unauthorized"
                db.commit()
                return False, "Kaggle báo lỗi 401: Username hoặc API Key không hợp lệ."
            else:
                acc.last_status = f"error_{resp.status_code}"
                db.commit()
                return False, f"Kaggle phản hồi mã HTTP {resp.status_code}"
        except Exception as e:
            return False, f"Lỗi kết nối tới Kaggle: {str(e)}"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Computes SHA-256 hash of a raw worker token string."""
        return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def issue_worker_token(
        db: Session,
        owner_user_id: str,
        execution_account_id: str,
        expires_days: int = 7
    ) -> Tuple[WorkerToken, str]:
        """
        Issues a cryptographically secure, tenant-scoped worker token.
        Stores SHA-256 hash in database; returns the raw token string for worker bootstrap.
        """
        raw_token = f"oloka_wkr_{secrets.token_hex(24)}"
        token_hash = KaggleAccountService.hash_token(raw_token)
        token_prefix = raw_token[:16] + "..."
        expires_at = datetime.utcnow() + timedelta(days=expires_days)

        wtk = WorkerToken(
            token_hash=token_hash,
            token_prefix=token_prefix,
            owner_user_id=owner_user_id,
            execution_account_id=execution_account_id,
            is_active=True,
            expires_at=expires_at,
            created_at=datetime.utcnow()
        )
        db.add(wtk)
        db.commit()
        db.refresh(wtk)
        return wtk, raw_token

    @staticmethod
    def verify_raw_worker_token(db: Session, raw_token: str) -> Optional[WorkerToken]:
        """
        Verifies a Bearer token supplied by a worker daemon.
        Returns the matching WorkerToken record with tenant ownership.
        """
        if not raw_token:
            return None

        clean_token = raw_token.strip()
        token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()

        # 1. Lookup WorkerToken record by token_hash (active or inactive)
        wtk = db.query(WorkerToken).filter(
            WorkerToken.token_hash == token_hash
        ).first()

        if wtk:
            # If the token has been revoked, deactivated, or expired, reject immediately
            if not wtk.is_active or wtk.revoked_at is not None:
                return None
            if wtk.expires_at and wtk.expires_at < datetime.utcnow():
                return None
            wtk.last_used_at = datetime.utcnow()
            db.commit()
            return wtk

        # 2. Backward compatibility for admin's legacy WORKER_TOKEN (only if no record exists yet)
        if settings.WORKER_TOKEN and secrets.compare_digest(clean_token, settings.WORKER_TOKEN):
            admin_user = db.query(User).filter(User.role == "admin").first()
            if admin_user:
                admin_acc = KaggleAccountService.get_execution_account_for_user(db, admin_user)
                if admin_acc:
                    admin_wtk = WorkerToken(
                        token_hash=token_hash,
                        token_prefix=clean_token[:12] + "...",
                        owner_user_id=admin_user.id,
                        execution_account_id=admin_acc.id,
                        is_active=True,
                        expires_at=datetime.utcnow() + timedelta(days=365)
                    )
                    db.add(admin_wtk)
                    db.commit()
                    db.refresh(admin_wtk)
                    return admin_wtk

        return None

    @staticmethod
    def revoke_worker_token(db: Session, token_id_or_raw: str, owner_user_id: str = None) -> bool:
        clean = (token_id_or_raw or "").strip()
        t_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        query = db.query(WorkerToken).filter(
            (WorkerToken.id == clean) | (WorkerToken.token_hash == t_hash)
        )
        if owner_user_id:
            query = query.filter(WorkerToken.owner_user_id == owner_user_id)
        wtk = query.first()
        if not wtk:
            return False
        wtk.is_active = False
        wtk.revoked_at = datetime.utcnow()
        db.commit()
        return True

    @staticmethod
    def revoke_tokens_for_account(db: Session, execution_account_id: str):
        db.query(WorkerToken).filter(
            WorkerToken.execution_account_id == execution_account_id
        ).update({"is_active": False, "revoked_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()
