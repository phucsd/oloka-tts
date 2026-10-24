import os
import shutil
import sqlite3
import time
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

db_url = settings.DATABASE_URL
is_mounted_volume = db_url.startswith("sqlite") and "/data/" in db_url.replace("\\", "/")

if db_url.startswith("sqlite"):
    raw_db_path = db_url.replace("sqlite:///", "").split("?")[0]
    if is_mounted_volume:
        for suffix in ("-shm", "-wal"):
            lock_file = raw_db_path + suffix
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass
    if os.path.exists(raw_db_path) and os.path.getsize(raw_db_path) > 0:
        try:
            conn = sqlite3.connect(raw_db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            res = cur.fetchone()
            conn.close()
            if not res or res[0] != "ok":
                raise RuntimeError(f"integrity_check={res}")
        except Exception as e_corrupt:
            corrupt_path = f"{raw_db_path}.corrupt_{int(time.time())}"
            print(f"⚠️ [Database] Detected corrupted SQLite file ({e_corrupt}). Renaming to {corrupt_path}")
            try:
                os.rename(raw_db_path, corrupt_path)
            except Exception:
                pass
            backup_candidate = os.path.join(os.path.dirname(raw_db_path), "vieneu_gateway.backup.db")
            if os.path.exists(backup_candidate) and os.path.getsize(backup_candidate) > 4096:
                try:
                    shutil.copyfile(backup_candidate, raw_db_path)
                    print(f"[Database] Restored database from local bucket snapshot {backup_candidate}")
                except Exception:
                    pass

connect_args = {"check_same_thread": False, "timeout": 60 if is_mounted_volume else 30} if db_url.startswith("sqlite") else {}

engine = create_engine(
    db_url,
    connect_args=connect_args,
    echo=False
)

if db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        if is_mounted_volume:
            # Force DELETE journal mode on FUSE/Bucket volume mounts to prevent .shm/.wal lock corruption
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
        else:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Auto-sync to HF Storage Bucket / Dataset whenever database changes are committed
@event.listens_for(SessionLocal, "after_commit")
def auto_cloud_backup_on_commit(session):
    try:
        from app.services.db_sync_service import DbSyncService
        DbSyncService.backup_database(immediate=False)
    except Exception:
        pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
