import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional
from app.config import settings

try:
    from huggingface_hub import HfApi, hf_hub_download
except Exception:
    HfApi = None
    hf_hub_download = None

class DbSyncService:
    """
    Automated Database Persistence Service for Hugging Face Spaces.
    Syncs the SQLite database with a free Private Hugging Face Dataset so that
    user accounts, admin passwords, and API keys survive container rebuilds,
    restarts, and sleep cycles.
    """
    _lock = threading.Lock()
    _timer_lock = threading.Lock()
    _last_backup_time = 0
    _min_interval_seconds = 120  # Minimum 2 minutes between cloud backups to eliminate HF CPU spikes
    _pending_timer = None
    _is_uploading = False
    _has_pending = False
    _repo_verified = False

    @classmethod
    def get_token(cls) -> Optional[str]:
        # HF_TOKEN is injected on HF Spaces as a secret/env variable
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        if not token:
            try:
                import huggingface_hub
                token = huggingface_hub.get_token()
            except Exception:
                pass
        return token

    @classmethod
    def get_backup_repo_id(cls) -> str:
        space_id = os.environ.get("SPACE_ID", "phucsd/vieneu-gateway")
        owner = space_id.split("/")[0] if "/" in space_id else "phucsd"
        return os.environ.get("HF_SYNC_DATASET", f"{owner}/vieneu-gateway-db")

    @classmethod
    def restore_database(cls) -> bool:
        """
        Runs on Gateway startup before DB connection is initialized.
        Restores the latest database from the private HF dataset if local DB is fresh.
        """
        if os.environ.get("TESTING") == "1":
            return False

        if not settings.DATABASE_URL.startswith("sqlite"):
            print("[DbSync] External database in use (e.g. PostgreSQL), skipping SQLite sync.")
            return False

        token = cls.get_token()
        if not token:
            print("[DbSync] No HF_TOKEN found, skipping cloud restore.")
            return False

        repo_id = cls.get_backup_repo_id()
        db_path = Path(settings.DATABASE_URL.replace("sqlite:///", ""))

        # If db already exists and has healthy size, check its contents first
        if db_path.exists() and db_path.stat().st_size > 4096:
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {r[0] for r in cur.fetchall()}
                user_count = 0
                if "users" in tables:
                    cur.execute("SELECT count(*) FROM users")
                    user_count = cur.fetchone()[0]
                conn.close()
                if user_count > 0:
                    print(f"[DbSync] Local database already exists and is healthy ({user_count} users, {db_path.stat().st_size} bytes).")
                    return True
            except Exception as e_exist:
                print(f"[DbSync] Existing local db check notice: {e_exist}")

        try:
            from huggingface_hub import HfApi, hf_hub_download
            api = HfApi(token=token)

            try:
                files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
            except Exception:
                api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
                files = []

            if "vieneu_gateway.db" in files:
                print(f"[DbSync] Downloading database backup from private dataset {repo_id}...")
                downloaded_file = hf_hub_download(
                    repo_id=repo_id,
                    filename="vieneu_gateway.db",
                    repo_type="dataset",
                    token=token,
                    force_download=True
                )

                # Validate downloaded file integrity before copying
                try:
                    import sqlite3
                    conn = sqlite3.connect(downloaded_file)
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = {r[0] for r in cur.fetchall()}
                    user_count = 0
                    if "users" in tables:
                        cur.execute("SELECT count(*) FROM users")
                        user_count = cur.fetchone()[0]
                    conn.close()

                    if "users" not in tables or user_count == 0:
                        print(f"⚠️ [DbSync] Downloaded database from {repo_id} has no users or is unseeded. Retaining fresh seeding fallback.")
                        if db_path.exists() and db_path.stat().st_size > 4096:
                            return True
                except Exception as e_val:
                    print(f"⚠️ [DbSync] Verification error on downloaded DB: {e_val}")

                db_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(downloaded_file, str(db_path))
                try:
                    os.chmod(str(db_path), 0o666)
                    os.chmod(str(db_path.parent), 0o777)
                except Exception:
                    pass
                print(f"[DbSync] Successfully restored database ({db_path.stat().st_size} bytes) from {repo_id}!")
                return True
            else:
                print(f"[DbSync] No previous database found in {repo_id}. A fresh database will be seeded.")
                return False
        except Exception as e:
            print(f"⚠️ [DbSync] Could not restore database from HF Dataset: {e}")
            return False

    @classmethod
    def start_periodic_sync(cls, interval_seconds: int = 180):
        """Starts a background daemon thread that periodically checkpoints the database."""
        if os.environ.get("TESTING") == "1":
            return

        def _loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    cls._do_upload()
                except Exception:
                    pass
        t = threading.Thread(target=_loop, daemon=True, name="DbSyncPeriodicThread")
        t.start()
        print(f"[DbSync] Background periodic sync started (interval: {interval_seconds}s).")

    @classmethod
    def backup_database(cls, immediate: bool = False):
        """
        Non-blocking cloud backup request.
        Debounces commits to prevent CPU/network saturation and request latency spikes on Hugging Face Spaces.
        """
        if os.environ.get("TESTING") == "1":
            return

        if not settings.DATABASE_URL.startswith("sqlite"):
            return

        token = cls.get_token()
        if not token:
            return

        now = time.time()
        time_since_last = now - cls._last_backup_time

        with cls._timer_lock:
            cls._has_pending = True
            if immediate or time_since_last >= cls._min_interval_seconds:
                delay = 0.5
            else:
                delay = max(5.0, cls._min_interval_seconds - time_since_last)

            if cls._pending_timer:
                cls._pending_timer.cancel()
            cls._pending_timer = threading.Timer(delay, cls._trigger_upload_thread)
            cls._pending_timer.daemon = True
            cls._pending_timer.start()

    @classmethod
    def _trigger_upload_thread(cls):
        threading.Thread(target=cls._do_upload, daemon=True, name="DbSyncUploadThread").start()

    @classmethod
    def _do_upload(cls):
        with cls._lock:
            if cls._is_uploading:
                cls._has_pending = True
                return
            cls._is_uploading = True

        try:
            with cls._timer_lock:
                cls._has_pending = False

            db_path = Path(settings.DATABASE_URL.replace("sqlite:///", ""))
            if not db_path.exists() or db_path.stat().st_size == 0:
                return

            token = cls.get_token()
            if not token:
                return

            repo_id = cls.get_backup_repo_id()

            # Safe copy using sqlite3.backup to flush WAL journal cleanly
            temp_copy = db_path.with_suffix(".tmp_backup")
            try:
                import sqlite3
                src_conn = sqlite3.connect(str(db_path))
                dst_conn = sqlite3.connect(str(temp_copy))
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
            except Exception as e_bk:
                print(f"⚠️ [DbSync] Native sqlite backup notice: {e_bk}, falling back to copyfile...")
                shutil.copyfile(str(db_path), str(temp_copy))

            # Integrity verification before pushing to cloud
            try:
                import sqlite3
                conn = sqlite3.connect(str(temp_copy))
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {r[0] for r in cur.fetchall()}
                user_count = 0
                if "users" in tables:
                    cur.execute("SELECT count(*) FROM users")
                    user_count = cur.fetchone()[0]
                conn.close()

                if "users" not in tables or user_count == 0:
                    print(f"⚠️ [DbSync] Skipping cloud upload: local database has no users table or 0 users. Cloud backup preserved!")
                    if temp_copy.exists():
                        temp_copy.unlink()
                    return
            except Exception as e_chk:
                print(f"⚠️ [DbSync] Pre-upload verification error: {e_chk}")
                if temp_copy.exists():
                    temp_copy.unlink()
                return

            api = HfApi(token=token) if HfApi else None
            if not api:
                from huggingface_hub import HfApi as HubApi
                api = HubApi(token=token)

            if not cls._repo_verified:
                try:
                    api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
                    cls._repo_verified = True
                except Exception:
                    cls._repo_verified = True

            api.upload_file(
                path_or_fileobj=str(temp_copy),
                path_in_repo="vieneu_gateway.db",
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=f"Auto-sync database ({time.strftime('%Y-%m-%d %H:%M:%S')})"
            )
            if temp_copy.exists():
                temp_copy.unlink()
            cls._last_backup_time = time.time()
            print(f"[DbSync] Successfully synced database backup ({user_count} users) to {repo_id}!")
        except Exception as e:
            print(f"⚠️ [DbSync] Failed to backup database to {repo_id}: {e}")
        finally:
            with cls._lock:
                cls._is_uploading = False
            if cls._has_pending:
                cls.backup_database(immediate=False)
