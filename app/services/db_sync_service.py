import os
import shutil
import sqlite3
import tempfile
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
    Automated Storage & Database Persistence Service for Hugging Face Spaces.
    Uses Hugging Face Pro Storage Bucket (`phucsd/vieneu-gateway-storage`) mounted at `/data`
    or accessed via `HfApi.batch_bucket_files` / `download_bucket_files` (zero Git commit overhead),
    with automatic fallback to the private Dataset (`phucsd/vieneu-gateway-db`).
    """
    _lock = threading.Lock()
    _timer_lock = threading.Lock()
    _last_backup_time = 0
    _min_interval_seconds = 120  # Minimum 2 minutes between cloud backups to eliminate CPU spikes
    _pending_timer = None
    _is_uploading = False
    _has_pending = False
    _bucket_verified = False
    _repo_verified = False

    @classmethod
    def get_token(cls) -> Optional[str]:
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        if not token:
            try:
                import huggingface_hub
                token = huggingface_hub.get_token()
            except Exception:
                pass
        return token

    @classmethod
    def get_bucket_id(cls) -> str:
        space_id = os.environ.get("SPACE_ID", "phucsd/vieneu-gateway")
        owner = space_id.split("/")[0] if "/" in space_id else "phucsd"
        return os.environ.get(
            "HF_STORAGE_BUCKET",
            getattr(settings, "HF_STORAGE_BUCKET", f"{owner}/vieneu-gateway-storage")
        )

    @classmethod
    def get_backup_repo_id(cls) -> str:
        space_id = os.environ.get("SPACE_ID", "phucsd/vieneu-gateway")
        owner = space_id.split("/")[0] if "/" in space_id else "phucsd"
        return os.environ.get("HF_SYNC_DATASET", f"{owner}/vieneu-gateway-db")

    @classmethod
    def _is_sqlite_healthy(cls, file_path: str | Path) -> tuple[bool, int]:
        """Returns (is_healthy, user_count) for a SQLite database file."""
        p = Path(file_path)
        if not p.exists() or p.stat().st_size <= 4096:
            return False, 0
        conn = None
        try:
            conn = sqlite3.connect(str(p), timeout=10)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            res = cur.fetchone()
            if not res or res[0] != "ok":
                conn.close()
                return False, 0
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            user_count = 0
            if "users" in tables:
                cur.execute("SELECT count(*) FROM users")
                user_count = cur.fetchone()[0]
            conn.close()
            return ("users" in tables and user_count > 0), user_count
        except Exception:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return False, 0

    @classmethod
    def restore_database(cls) -> bool:
        """
        Runs on Gateway startup before DB connection is initialized.
        Restores the latest database from `/data` Bucket mount, HF Storage Bucket API,
        or fallback private HF Dataset if local DB is fresh or corrupted.
        """
        if os.environ.get("TESTING") == "1":
            return False

        if not settings.DATABASE_URL.startswith("sqlite"):
            print("[DbSync] External database in use (e.g. PostgreSQL), skipping SQLite sync.")
            return False

        db_path = Path(settings.DATABASE_URL.replace("sqlite:///", "").split("?")[0])

        # 1. Check if active DB path already exists and is healthy
        healthy, user_count = cls._is_sqlite_healthy(db_path)
        if healthy:
            print(f"[DbSync] Local database already exists and is healthy ({user_count} users, {db_path.stat().st_size} bytes).")
            return True

        # 2. Check if local bucket snapshot `vieneu_gateway.backup.db` exists next to db_path or in /data/storage
        for snapshot_candidate in [
            db_path.parent / "vieneu_gateway.backup.db",
            Path("/data/storage/vieneu_gateway.backup.db"),
            Path("/data/storage/vieneu_gateway.db"),
        ]:
            if snapshot_candidate != db_path and snapshot_candidate.exists():
                ok, u_cnt = cls._is_sqlite_healthy(snapshot_candidate)
                if ok:
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(str(snapshot_candidate), str(db_path))
                    print(f"[DbSync] Restored database ({u_cnt} users) from mounted bucket snapshot {snapshot_candidate}!")
                    return True

        token = cls.get_token()
        if not token:
            print("[DbSync] No HF_TOKEN found, skipping cloud restore.")
            return False

        bucket_id = cls.get_bucket_id()
        repo_id = cls.get_backup_repo_id()

        try:
            from huggingface_hub import HfApi, hf_hub_download
            api = HfApi(token=token)

            # 3. Try restoring from HF Storage Bucket first
            if hasattr(api, "download_bucket_files"):
                for remote_db_key in ("storage/vieneu_gateway.db", "storage/vieneu_gateway.backup.db"):
                    tmp_dl = Path(tempfile.gettempdir()) / f"restore_{int(time.time())}.db"
                    try:
                        api.download_bucket_files(
                            bucket_id=bucket_id,
                            files=[(remote_db_key, str(tmp_dl))],
                            raise_on_missing_files=True,
                            token=token
                        )
                        ok, u_cnt = cls._is_sqlite_healthy(tmp_dl)
                        if ok:
                            db_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(str(tmp_dl), str(db_path))
                            tmp_dl.unlink(missing_ok=True)
                            print(f"[DbSync] Successfully restored database ({u_cnt} users) from HF Bucket {bucket_id}:{remote_db_key}!")
                            return True
                    except Exception:
                        pass
                    finally:
                        if tmp_dl.exists():
                            try:
                                tmp_dl.unlink()
                            except Exception:
                                pass

            # 4. Fallback to private HF Dataset
            try:
                files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
            except Exception:
                api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
                files = []

            if "vieneu_gateway.db" in files:
                print(f"[DbSync] Downloading database backup from fallback dataset {repo_id}...")
                downloaded_file = hf_hub_download(
                    repo_id=repo_id,
                    filename="vieneu_gateway.db",
                    repo_type="dataset",
                    token=token,
                    force_download=True
                )
                ok, u_cnt = cls._is_sqlite_healthy(downloaded_file)
                if not ok:
                    print(f"⚠️ [DbSync] Downloaded database from {repo_id} has no users or is unseeded.")
                    return False

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
                print(f"[DbSync] No previous database found in {bucket_id} or {repo_id}. A fresh database will be seeded.")
                return False
        except Exception as e:
            print(f"⚠️ [DbSync] Could not restore database from HF Storage: {e}")
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
        Debounces uploads to prevent CPU/network saturation and request latency spikes.
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

        bucket_id = cls.get_bucket_id()
        try:
            with cls._timer_lock:
                cls._has_pending = False

            db_path = Path(settings.DATABASE_URL.replace("sqlite:///", "").split("?")[0])
            if not db_path.exists() or db_path.stat().st_size == 0:
                return

            token = cls.get_token()
            if not token:
                return

            # Safe copy using sqlite3.backup into temp directory
            temp_copy = Path(tempfile.gettempdir()) / f"vieneu_gateway_sync_{int(time.time())}.db"
            try:
                src_conn = sqlite3.connect(str(db_path), timeout=30)
                dst_conn = sqlite3.connect(str(temp_copy), timeout=30)
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
            except Exception as e_bk:
                print(f"⚠️ [DbSync] Native sqlite backup notice: {e_bk}, falling back to copyfile...")
                shutil.copyfile(str(db_path), str(temp_copy))

            # Integrity verification before pushing to cloud
            healthy, user_count = cls._is_sqlite_healthy(temp_copy)
            if not healthy:
                print("⚠️ [DbSync] Skipping cloud upload: local database failed integrity check or has 0 users. Cloud backup preserved!")
                temp_copy.unlink(missing_ok=True)
                return

            # Save rolling backup snapshot inside mounted /data/storage if present
            try:
                snapshot_path = db_path.parent / "vieneu_gateway.backup.db"
                shutil.copyfile(str(temp_copy), str(snapshot_path))
            except Exception:
                pass

            api = HfApi(token=token) if HfApi else None
            if not api:
                from huggingface_hub import HfApi as HubApi
                api = HubApi(token=token)

            # Upload to HF Storage Bucket (no Git commit overhead!)
            if hasattr(api, "batch_bucket_files"):
                if not cls._bucket_verified:
                    try:
                        api.create_bucket(bucket_id, private=True, exist_ok=True)
                    except Exception:
                        pass
                    cls._bucket_verified = True

                is_mounted_on_data = "/data/" in str(db_path).replace("\\", "/")
                to_add = [(str(temp_copy), "storage/vieneu_gateway.backup.db")]
                if not is_mounted_on_data:
                    to_add.append((str(temp_copy), "storage/vieneu_gateway.db"))
                api.batch_bucket_files(bucket_id, add=to_add)
                print(f"[DbSync] Successfully synced database snapshot ({user_count} users) to HF Bucket {bucket_id}!")
            else:
                repo_id = cls.get_backup_repo_id()
                if not cls._repo_verified:
                    try:
                        api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)
                    except Exception:
                        pass
                    cls._repo_verified = True
                api.upload_file(
                    path_or_fileobj=str(temp_copy),
                    path_in_repo="vieneu_gateway.db",
                    repo_id=repo_id,
                    repo_type="dataset",
                    commit_message=f"Auto-sync database ({time.strftime('%Y-%m-%d %H:%M:%S')})"
                )
                print(f"[DbSync] Successfully synced database backup ({user_count} users) to {repo_id}!")

            temp_copy.unlink(missing_ok=True)
            cls._last_backup_time = time.time()
        except Exception as e:
            print(f"⚠️ [DbSync] Failed to backup database to {bucket_id}: {e}")
        finally:
            with cls._lock:
                cls._is_uploading = False
            if cls._has_pending:
                cls.backup_database(immediate=False)

    @classmethod
    def backup_audio_file(cls, local_path: str, job_id: str):
        """
        Persists a generated TTS job audio file (`storage/audio/{job_id}.wav`) to the
        HF Storage Bucket (`phucsd/vieneu-gateway-storage`).
        If `/data` is already mounted from the bucket, the file is already in the bucket on disk.
        """
        if os.environ.get("TESTING") == "1":
            return
        if not local_path or not os.path.exists(local_path):
            return
        try:
            if os.path.getsize(local_path) <= 100:
                return
        except Exception:
            return

        # If already written directly onto the mounted /data bucket volume, no extra upload needed
        norm_path = str(Path(local_path).resolve()).replace("\\", "/")
        if norm_path.startswith("/data/"):
            return

        token = cls.get_token()
        if not token:
            return

        def _upload_audio():
            try:
                bucket_id = cls.get_bucket_id()
                api = HfApi(token=token) if HfApi else None
                if not api:
                    from huggingface_hub import HfApi as HubApi
                    api = HubApi(token=token)

                if hasattr(api, "batch_bucket_files"):
                    if not cls._bucket_verified:
                        try:
                            api.create_bucket(bucket_id, private=True, exist_ok=True)
                        except Exception:
                            pass
                        cls._bucket_verified = True
                    api.batch_bucket_files(
                        bucket_id,
                        add=[(str(local_path), f"storage/audio/{job_id}.wav")]
                    )
                    print(f"[DbSync] Backed up storage/audio/{job_id}.wav to HF Bucket {bucket_id}.")
                else:
                    repo_id = cls.get_backup_repo_id()
                    api.upload_file(
                        path_or_fileobj=str(local_path),
                        path_in_repo=f"audio/{job_id}.wav",
                        repo_id=repo_id,
                        repo_type="dataset",
                        commit_message=f"Backup job audio {job_id}"
                    )
            except Exception as e:
                print(f"⚠️ [DbSync] Audio backup notice for {job_id}: {e}")

        threading.Thread(target=_upload_audio, daemon=True, name=f"AudioSync_{job_id}").start()

    @classmethod
    def restore_audio_file(cls, job_id: str, target_path: Optional[str] = None) -> Optional[str]:
        """
        Resolves or restores a job's WAV file from `/data/storage/audio`, `settings.AUDIO_DIR`,
        or the HF Storage Bucket (`phucsd/vieneu-gateway-storage`).
        """
        if os.environ.get("TESTING") == "1":
            return None

        candidates = []
        if target_path:
            candidates.append(Path(target_path))
        std_dest = Path(settings.AUDIO_DIR) / f"{job_id}.wav"
        candidates.append(std_dest)
        candidates.append(Path("/data/storage/audio") / f"{job_id}.wav")

        for cand in candidates:
            try:
                if cand.exists() and cand.stat().st_size > 100:
                    return str(cand)
            except Exception:
                pass

        token = cls.get_token()
        if not token:
            return None

        try:
            api = HfApi(token=token) if HfApi else None
            if not api:
                from huggingface_hub import HfApi as HubApi
                api = HubApi(token=token)

            std_dest.parent.mkdir(parents=True, exist_ok=True)
            bucket_id = cls.get_bucket_id()

            if hasattr(api, "download_bucket_files"):
                try:
                    api.download_bucket_files(
                        bucket_id=bucket_id,
                        files=[(f"storage/audio/{job_id}.wav", str(std_dest))],
                        raise_on_missing_files=True,
                        token=token
                    )
                    if std_dest.exists() and std_dest.stat().st_size > 100:
                        print(f"[DbSync] Restored storage/audio/{job_id}.wav from HF Bucket {bucket_id} -> {std_dest}")
                        return str(std_dest)
                except Exception:
                    pass

            # Legacy Dataset fallback
            from huggingface_hub import hf_hub_download as hub_dl
            dl_fn = hf_hub_download or hub_dl
            repo_id = cls.get_backup_repo_id()
            downloaded = dl_fn(
                repo_id=repo_id,
                filename=f"audio/{job_id}.wav",
                repo_type="dataset",
                token=token
            )
            shutil.copyfile(downloaded, str(std_dest))
            print(f"[DbSync] Restored audio/{job_id}.wav from Dataset {repo_id} -> {std_dest}")
            return str(std_dest)
        except Exception:
            return None

    @classmethod
    def backup_sample_file(cls, local_path: str, filename: str):
        """Persists a cloned voice sample (`storage/samples/{filename}`) to the HF Storage Bucket."""
        if os.environ.get("TESTING") == "1":
            return
        if not local_path or not os.path.exists(local_path):
            return
        norm_path = str(Path(local_path).resolve()).replace("\\", "/")
        if norm_path.startswith("/data/"):
            return

        token = cls.get_token()
        if not token:
            return

        clean_name = Path(filename).name

        def _upload_sample():
            try:
                bucket_id = cls.get_bucket_id()
                api = HfApi(token=token) if HfApi else None
                if not api:
                    from huggingface_hub import HfApi as HubApi
                    api = HubApi(token=token)
                if hasattr(api, "batch_bucket_files"):
                    api.batch_bucket_files(
                        bucket_id,
                        add=[(str(local_path), f"storage/samples/{clean_name}")]
                    )
                    print(f"[DbSync] Backed up storage/samples/{clean_name} to HF Bucket {bucket_id}.")
            except Exception as e:
                print(f"⚠️ [DbSync] Sample backup notice for {clean_name}: {e}")

        threading.Thread(target=_upload_sample, daemon=True, name=f"SampleSync_{clean_name}").start()

    @classmethod
    def restore_sample_file(cls, filename: str, target_path: Optional[str] = None) -> Optional[str]:
        """Resolves or restores a cloned voice sample from `/data/storage/samples` or HF Storage Bucket."""
        if os.environ.get("TESTING") == "1":
            return None

        clean_name = Path(filename).name
        std_dest = Path(settings.SAMPLES_DIR) / clean_name
        candidates = []
        if target_path:
            candidates.append(Path(target_path))
        candidates.append(std_dest)
        candidates.append(Path("/data/storage/samples") / clean_name)

        for cand in candidates:
            try:
                if cand.exists() and cand.stat().st_size > 100:
                    return str(cand)
            except Exception:
                pass

        token = cls.get_token()
        if not token:
            return None

        try:
            api = HfApi(token=token) if HfApi else None
            if not api:
                from huggingface_hub import HfApi as HubApi
                api = HubApi(token=token)
            std_dest.parent.mkdir(parents=True, exist_ok=True)
            bucket_id = cls.get_bucket_id()
            if hasattr(api, "download_bucket_files"):
                api.download_bucket_files(
                    bucket_id=bucket_id,
                    files=[(f"storage/samples/{clean_name}", str(std_dest))],
                    raise_on_missing_files=True,
                    token=token
                )
                if std_dest.exists() and std_dest.stat().st_size > 100:
                    print(f"[DbSync] Restored storage/samples/{clean_name} from HF Bucket {bucket_id} -> {std_dest}")
                    return str(std_dest)
        except Exception:
            pass
        return None
