import os
import sys
import json
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from app.config import settings
from app.database import SessionLocal
from app.models import WorkerSession, TTSJob
from app.services.kaggle_notebook_builder import KaggleNotebookBuilder

class KaggleOrchestrator:
    _lock = threading.Lock()
    _is_pushing = False
    _last_push_time = 0

    @staticmethod
    def is_configured() -> bool:
        username = settings.KAGGLE_USERNAME or os.environ.get("KAGGLE_USERNAME")
        key = settings.KAGGLE_KEY or os.environ.get("KAGGLE_KEY")
        return bool(username and key)

    @staticmethod
    def has_live_worker(db=None) -> bool:
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True

        cutoff = datetime.utcnow() - timedelta(seconds=90)
        try:
            live = db.query(WorkerSession).filter(
                WorkerSession.status.in_(["starting", "ready", "busy"]),
                WorkerSession.last_heartbeat_at >= cutoff
            ).first()
            return live is not None
        finally:
            if own_db:
                db.close()

    @staticmethod
    def get_kernel_status() -> str:
        if not KaggleOrchestrator.is_configured():
            return "NOT_CONFIGURED"

        kernel_ref = settings.KAGGLE_KERNEL_REF or f"{settings.KAGGLE_USERNAME}/{settings.KAGGLE_KERNEL_SLUG}"
        try:
            os.environ["KAGGLE_USERNAME"] = settings.KAGGLE_USERNAME
            os.environ["KAGGLE_KEY"] = settings.KAGGLE_KEY
            os.environ["KAGGLE_API_TOKEN"] = settings.KAGGLE_KEY
            from kaggle.api.kaggle_api_extended import KaggleApi
            api = KaggleApi()
            api.authenticate()
            res = api.kernels_status(kernel_ref)
            status = res.get("status", "UNKNOWN") if isinstance(res, dict) else str(res)
            return status
        except Exception as e:
            return f"ERROR: {e}"

    @classmethod
    def trigger_push(cls, force: bool = False, kaggle_username: str = None, kaggle_key: str = None) -> dict:
        """
        Pushes kernel to Kaggle to boot up dual GPU T4 worker.
        Supports per-user BYOK Kaggle credentials.
        """
        with cls._lock:
            now = time.time()
            if cls._is_pushing and not force:
                return {"status": "busy", "message": "Kernel push is already in progress"}
            
            # Rate limit pushes to at most once per 60s unless forced
            if now - cls._last_push_time < 60 and not force:
                return {"status": "cooldown", "message": "Push requested too quickly. Please wait."}

            cls._is_pushing = True

        def _do_push():
            try:
                k_user = kaggle_username or settings.KAGGLE_USERNAME
                k_key = kaggle_key or settings.KAGGLE_KEY
                if not k_user or not k_key:
                    print("[KaggleOrchestrator] No Kaggle credentials available to push kernel.")
                    return

                print(f"[KaggleOrchestrator] Generating metadata & worker script for Kaggle user: {k_user}...")
                KaggleNotebookBuilder.generate_kernel_metadata()
                KaggleNotebookBuilder.generate_worker_script()

                # Ensure ~/.kaggle/kaggle.json and ~/.kaggle/access_token exist for Kaggle CLI
                try:
                    k_dir = Path.home() / ".kaggle"
                    k_dir.mkdir(parents=True, exist_ok=True)
                    k_file = k_dir / "kaggle.json"
                    with open(k_file, "w", encoding="utf-8") as f_k:
                        json.dump({"username": k_user, "key": k_key}, f_k)
                    try:
                        os.chmod(k_file, 0o600)
                    except Exception:
                        pass
                    token_file = k_dir / "access_token"
                    with open(token_file, "w", encoding="utf-8") as f_t:
                        f_t.write(k_key.strip())
                except Exception as e_kfile:
                    print(f"⚠️ [KaggleOrchestrator] Notice writing kaggle config: {e_kfile}")

                worker_dir = "kaggle_worker_remote" if os.path.isdir("kaggle_worker_remote") else settings.KAGGLE_WORKER_DIR
                worker_dir_abs = os.path.abspath(worker_dir)
                env = os.environ.copy()
                env["KAGGLE_USERNAME"] = k_user
                env["KAGGLE_KEY"] = k_key
                env["KAGGLE_API_TOKEN"] = k_key
                env["PYTHONUTF8"] = "1"

                cmd = [
                    sys.executable, "-c", "from kaggle.cli import main; main()",
                    "kernels", "push",
                    "-p", worker_dir_abs
                ]

                print(f"[KaggleOrchestrator] Pushing kernel via CLI under {k_user}: {' '.join(cmd)}")
                res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
                if res.returncode == 0:
                    print(f"[KaggleOrchestrator] Push succeeded for {k_user}: {res.stdout.strip()}")
                else:
                    print(f"[KaggleOrchestrator] Push stderr for {k_user}: {res.stderr.strip()}")

                # Update any jobs waiting to booting_kaggle
                db = SessionLocal()
                try:
                    db.query(TTSJob).filter(TTSJob.status == "queued").update({"status": "booting_kaggle"})
                    db.commit()
                finally:
                    db.close()

            except Exception as e:
                print(f"[KaggleOrchestrator] Push failed: {e}")
            finally:
                with cls._lock:
                    cls._is_pushing = False
                    cls._last_push_time = time.time()

        thread = threading.Thread(target=_do_push, daemon=True)
        thread.start()
        return {"status": "started", "message": "Kernel push triggered in background"}

    @classmethod
    def ensure_worker_running(cls, db=None, user_id: str = None) -> bool:
        """
        Ensures a worker is active. If no live worker, triggers push using user's Kaggle credentials (if set) or system default if admin.
        """
        if cls.has_live_worker(db):
            return True

        k_user = None
        k_key = None
        if user_id and db:
            from app.models import User
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                if user.kaggle_username and user.kaggle_key:
                    k_user = user.kaggle_username
                    k_key = user.kaggle_key
                elif user.role == "admin" and cls.is_configured():
                    k_user = settings.KAGGLE_USERNAME
                    k_key = settings.KAGGLE_KEY

        if not k_user or not k_key:
            print(f"⚠️ [KaggleOrchestrator] No valid Kaggle credentials found for user_id={user_id}. Refusing to boot GPU.")
            return False

        print(f"⚡ [KaggleOrchestrator] Auto-triggering kernel push for user {k_user}...")
        cls.trigger_push(kaggle_username=k_user, kaggle_key=k_key)
        return False
