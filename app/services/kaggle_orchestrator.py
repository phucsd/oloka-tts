import os
import sys
import json
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from app.config import settings
from app.database import SessionLocal
from app.models import WorkerSession, TTSJob, KaggleExecutionAccount, User
from app.services.kaggle_notebook_builder import KaggleNotebookBuilder
from app.services.kaggle_account_service import KaggleAccountService

class KaggleOrchestrator:
    _meta_lock = threading.Lock()
    _account_locks: Dict[str, threading.Lock] = {}
    _is_pushing_accounts: set = set()
    _last_push_times: Dict[str, float] = {}
    _status_cache: Dict[str, tuple[float, str]] = {}

    @classmethod
    def _get_account_lock(cls, account_id: str) -> threading.Lock:
        with cls._meta_lock:
            if account_id not in cls._account_locks:
                cls._account_locks[account_id] = threading.Lock()
            return cls._account_locks[account_id]

    @staticmethod
    def is_configured(execution_account_id: str = None, db: Session = None) -> bool:
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True
        try:
            if execution_account_id:
                acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == execution_account_id).first()
                return bool(acc and acc.kaggle_username and acc.kaggle_key)
            admin_user = db.query(User).filter(User.role == "admin", User.username == "admin").first() or db.query(User).filter(User.role == "admin").first()
            acc = KaggleAccountService.get_execution_account_for_user(db, admin_user)
            if acc and acc.kaggle_username and acc.kaggle_key:
                return True
            username = settings.KAGGLE_USERNAME or os.environ.get("KAGGLE_USERNAME")
            key = settings.KAGGLE_KEY or os.environ.get("KAGGLE_KEY")
            return bool(username and key)
        finally:
            if own_db:
                db.close()

    @staticmethod
    def has_live_worker(db: Session = None, execution_account_id: str = None, user_id: str = None) -> bool:
        """
        Checks if a live worker exists.
        - If execution_account_id is provided: checks workers strictly for that execution account.
        - If user_id is provided: checks workers strictly for that user.
        - If neither is provided: checks if ANY worker is live (used for system-wide health checks).
        """
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        cutoff = datetime.utcnow() - timedelta(seconds=90)
        try:
            query = db.query(WorkerSession).filter(
                WorkerSession.status.in_(["starting", "ready", "busy"]),
                WorkerSession.last_heartbeat_at >= cutoff
            )
            if execution_account_id:
                query = query.filter(WorkerSession.execution_account_id == execution_account_id)
            elif user_id:
                query = query.filter(WorkerSession.owner_user_id == user_id)

            live = query.first()
            return live is not None
        finally:
            if own_db:
                db.close()

    @staticmethod
    def get_live_workers(db: Session = None, execution_account_id: str = None, user_id: str = None) -> List[WorkerSession]:
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        cutoff = datetime.utcnow() - timedelta(seconds=90)
        try:
            query = db.query(WorkerSession).filter(
                WorkerSession.status.in_(["starting", "ready", "busy"]),
                WorkerSession.last_heartbeat_at >= cutoff
            )
            if execution_account_id:
                query = query.filter(WorkerSession.execution_account_id == execution_account_id)
            elif user_id:
                query = query.filter(WorkerSession.owner_user_id == user_id)
            return query.all()
        finally:
            if own_db:
                db.close()

    @classmethod
    def get_kernel_status(cls, db: Session = None, execution_account_id: str = None, user_id: str = None) -> str:
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        try:
            acc = None
            if execution_account_id:
                acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == execution_account_id).first()
            elif user_id:
                user = db.query(User).filter(User.id == user_id).first()
                acc = KaggleAccountService.get_execution_account_for_user(db, user)
            else:
                admin_user = db.query(User).filter(User.role == "admin", User.username == "admin").first() or db.query(User).filter(User.role == "admin").first()
                acc = KaggleAccountService.get_execution_account_for_user(db, admin_user)

            if not acc or not acc.kaggle_username or not acc.kaggle_key:
                return "NOT_CONFIGURED"

            # Fast-path: if live workers for this account are actively heartbeating, the kernel is RUNNING
            if cls.has_live_worker(db, execution_account_id=acc.id):
                return "Đang Chạy (Dual T4 Ready)"

            if acc.id in cls._is_pushing_accounts:
                return "Đang Đẩy Kernel (Pushing...)"

            now = time.time()
            cached = cls._status_cache.get(acc.id)
            if cached and (now - cached[0] < 30):
                return cached[1]

            try:
                env = os.environ.copy()
                env["KAGGLE_USERNAME"] = acc.kaggle_username
                env["KAGGLE_KEY"] = acc.kaggle_key
                env["KAGGLE_API_TOKEN"] = acc.kaggle_key
                env["PYTHONUTF8"] = "1"

                cmd = [
                    sys.executable, "-c", "from kaggle.cli import main; main()",
                    "kernels", "status", acc.kernel_ref
                ]
                res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=8)
                out = (res.stdout or "") + "\n" + (res.stderr or "")
                status_str = "Đang Nghỉ (Idle)"
                if "RUNNING" in out:
                    status_str = "Đang Chạy (RUNNING)"
                elif "QUEUED" in out:
                    status_str = "Đang Khởi Động (QUEUED)"
                elif "COMPLETE" in out:
                    status_str = "Đang Nghỉ (COMPLETE)"
                elif "ERROR" in out or "CANCEL" in out:
                    status_str = "Đã Dừng (STOPPED)"
                elif acc.last_status == "booting":
                    status_str = "Đang Khởi Động (Booting)"

                cls._status_cache[acc.id] = (now, status_str)
                return status_str
            except Exception:
                fallback_str = "Đang Khởi Động (Booting)" if acc.last_status == "booting" else "Đang Nghỉ (Idle)"
                cls._status_cache[acc.id] = (now, fallback_str)
                return fallback_str
        finally:
            if own_db:
                db.close()

    @classmethod
    def trigger_push(
        cls,
        db: Session = None,
        execution_account_id: str = None,
        user_id: str = None,
        force: bool = False,
        sync: bool = False
    ) -> dict:
        """
        Pushes kernel to Kaggle in a fully isolated, per-tenant workspace.
        Never touches ~/.kaggle/kaggle.json, never mutates global os.environ,
        and never falls back to admin credentials.
        """
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        try:
            acc = None
            if execution_account_id:
                acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == execution_account_id).first()
            elif user_id:
                user = db.query(User).filter(User.id == user_id).first()
                acc = KaggleAccountService.get_execution_account_for_user(db, user)
            else:
                admin_user = db.query(User).filter(User.role == "admin").first()
                acc = KaggleAccountService.get_execution_account_for_user(db, admin_user)

            if not acc or not acc.is_enabled:
                return {
                    "status": "error",
                    "message": "Không có tài khoản Kaggle BYOK hợp lệ được kích hoạt để khởi chạy GPU."
                }

            acc_id = acc.id
            acc_lock = cls._get_account_lock(acc_id)

            with acc_lock:
                now = time.time()
                if acc_id in cls._is_pushing_accounts and not force:
                    return {
                        "status": "busy",
                        "message": f"Quá trình khởi chạy GPU cho tài khoản @{acc.kaggle_username} đang diễn ra."
                    }

                last_push = cls._last_push_times.get(acc_id, 0)
                if now - last_push < 60 and not force:
                    return {
                        "status": "cooldown",
                        "message": "Yêu cầu khởi chạy quá dồn dập. Vui lòng chờ 60 giây."
                    }

                cls._is_pushing_accounts.add(acc_id)

            def _do_push():
                push_db = SessionLocal()
                try:
                    cur_acc = push_db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == acc_id).first()
                    if not cur_acc:
                        return

                    # 1. Prepare isolated workspace directory: separate config from source code
                    account_dir = Path(settings.BASE_DIR) / "storage" / "kaggle_workspaces" / f"account_{cur_acc.id}"
                    config_dir = account_dir / "config"
                    source_dir = account_dir / "src"
                    config_dir.mkdir(parents=True, exist_ok=True)
                    source_dir.mkdir(parents=True, exist_ok=True)

                    # 2. Issue a scoped worker token for this tenant
                    wtk, raw_worker_token = KaggleAccountService.issue_worker_token(
                        push_db,
                        owner_user_id=cur_acc.owner_user_id,
                        execution_account_id=cur_acc.id,
                        expires_days=7
                    )

                    # 3. Write isolated kaggle credentials strictly inside config_dir (NOT inside source_dir)
                    k_file = config_dir / "kaggle.json"
                    with open(k_file, "w", encoding="utf-8") as f_k:
                        json.dump({"username": cur_acc.kaggle_username, "key": cur_acc.kaggle_key}, f_k)
                    try:
                        os.chmod(k_file, 0o600)
                    except Exception:
                        pass

                    # 4. Generate kernel-metadata.json and worker.py strictly inside source_dir for this tenant
                    worker_prefix = f"kw_{cur_acc.id}"
                    KaggleNotebookBuilder.generate_kernel_metadata(
                        output_dir=str(source_dir),
                        kaggle_username=cur_acc.kaggle_username,
                        kernel_slug=cur_acc.kernel_slug,
                        kernel_title=cur_acc.kernel_title
                    )
                    KaggleNotebookBuilder.generate_worker_script(
                        output_dir=str(source_dir),
                        gateway_url=settings.PUBLIC_API_BASE_URL,
                        worker_token=raw_worker_token,
                        worker_prefix=worker_prefix
                    )

                    # 5. Build isolated subprocess environment with KAGGLE_CONFIG_DIR pointing to config_dir
                    env = os.environ.copy()
                    env["KAGGLE_CONFIG_DIR"] = str(config_dir)
                    env["KAGGLE_USERNAME"] = cur_acc.kaggle_username
                    env["KAGGLE_KEY"] = cur_acc.kaggle_key
                    env["KAGGLE_API_TOKEN"] = cur_acc.kaggle_key
                    env["PYTHONUTF8"] = "1"

                    cmd = [
                        sys.executable, "-c", "from kaggle.cli import main; main()",
                        "kernels", "push",
                        "-p", str(source_dir)
                    ]

                    print(f"[KaggleOrchestrator] Pushing kernel for @{cur_acc.kaggle_username} from {source_dir}...")
                    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

                    if res.returncode == 0:
                        try:
                            print(f"[OK] [KaggleOrchestrator] Push succeeded for @{cur_acc.kaggle_username}: {res.stdout.strip()}")
                        except Exception:
                            pass
                        cur_acc.last_status = "booting"
                        # Transition queued jobs owned by this execution account to booting_kaggle
                        push_db.query(TTSJob).filter(
                            TTSJob.execution_account_id == cur_acc.id,
                            TTSJob.status == "queued"
                        ).update({"status": "booting_kaggle"}, synchronize_session=False)
                        push_db.commit()
                    else:
                        err_out = (res.stderr or res.stdout or "").strip()
                        try:
                            print(f"[ERROR] [KaggleOrchestrator] Push failed for @{cur_acc.kaggle_username}: {err_out}")
                        except Exception:
                            pass
                        fail_msg = "Lỗi khởi chạy Kaggle kernel"
                        if "quota" in err_out.lower():
                            cur_acc.last_status = "quota_exceeded"
                            fail_msg = "Tài khoản Kaggle của bạn đã hết hạn mức GPU (Kaggle GPU Quota Exceeded). Vui lòng thử lại vào tuần sau hoặc sử dụng GPU khác."
                        elif "401" in err_out or "unauthorized" in err_out.lower():
                            cur_acc.last_status = "unauthorized"
                            fail_msg = "Kaggle API Key không hợp lệ hoặc đã bị vô hiệu hóa. Vui lòng cập nhật trong Cài Đặt."
                        else:
                            cur_acc.last_status = "push_failed"
                            fail_msg = f"Lỗi khởi chạy Kaggle: {err_out[:180]}"

                        # Mark waiting jobs as failed with the accurate tenant error
                        push_db.query(TTSJob).filter(
                            TTSJob.execution_account_id == cur_acc.id,
                            TTSJob.status.in_(["queued", "booting_kaggle"])
                        ).update({"status": "failed", "error_message": fail_msg}, synchronize_session=False)
                        push_db.commit()

                except Exception as ex:
                    try:
                        print(f"[WARN] [KaggleOrchestrator] Exception in _do_push for account {acc_id}: {ex}")
                    except Exception:
                        pass
                finally:
                    with cls._get_account_lock(acc_id):
                        cls._is_pushing_accounts.discard(acc_id)
                        cls._last_push_times[acc_id] = time.time()
                    push_db.close()

            if sync:
                _do_push()
                return {
                    "status": "completed",
                    "message": f"Tiến trình đẩy kernel cho tài khoản @{acc.kaggle_username} đã thực thi xong."
                }
            else:
                thread = threading.Thread(target=_do_push, daemon=True)
                thread.start()
                return {
                    "status": "started",
                    "message": f"Đã kích hoạt đẩy kernel Kaggle cho tài khoản @{acc.kaggle_username} trong tiến trình nền."
                }

        finally:
            if own_db:
                db.close()

    @classmethod
    def ensure_worker_running(
        cls,
        db: Session = None,
        execution_account_id: str = None,
        user_id: str = None
    ) -> bool:
        """
        Ensures a worker is active for the specified tenant.
        If no live worker exists for that tenant, triggers Kaggle kernel push.
        Strict isolation: Never uses admin credentials for non-admin users.
        """
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        try:
            acc = None
            if execution_account_id:
                acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == execution_account_id).first()
            elif user_id:
                user = db.query(User).filter(User.id == user_id).first()
                acc = KaggleAccountService.get_execution_account_for_user(db, user)

            if not acc or not acc.is_enabled:
                print(f"⚠️ [KaggleOrchestrator] No valid Kaggle credentials for user_id={user_id}, execution_account_id={execution_account_id}. Refusing to boot GPU.")
                return False

            if cls.has_live_worker(db, execution_account_id=acc.id):
                return True

            print(f"⚡ [KaggleOrchestrator] Auto-triggering kernel push for account @{acc.kaggle_username} (ID: {acc.id})...")
            cls.trigger_push(db, execution_account_id=acc.id)
            return False
        finally:
            if own_db:
                db.close()
