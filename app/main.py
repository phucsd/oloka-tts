import os
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy.orm import Session
from app.config import settings
from app.database import engine, Base, SessionLocal, get_db
from app.services.job_service import JobService
from app.services.kaggle_notebook_builder import KaggleNotebookBuilder
from app.routers import web, tts, voices, internal_worker, openai_speech, admin, auth, user_settings, admin_views, mcp_auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks: Restore database from cloud if running in ephemeral container
    from app.services.db_sync_service import DbSyncService
    try:
        DbSyncService.restore_database()
    except Exception as e_restore:
        print(f"[Startup] Database restore check: {e_restore}")

    print("[Startup] Initializing VieNeu Gateway Database...")
    Base.metadata.create_all(bind=engine)

    # Ensure schema migrations for SQLite if needed
    try:
        with engine.connect() as conn:
            res = conn.exec_driver_sql("PRAGMA table_info(tts_jobs)")
            cols = [row[1] for row in res.fetchall()]
            if "silence_p" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN silence_p FLOAT DEFAULT 0.15")
                print("[Startup] Auto-migrated schema: Added silence_p to tts_jobs.")
            if "user_id" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN user_id VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added user_id to tts_jobs.")
            
            res_v = conn.exec_driver_sql("PRAGMA table_info(voice_presets)")
            cols_v = [row[1] for row in res_v.fetchall()]
            if "is_enabled" not in cols_v and len(cols_v) > 0:
                conn.exec_driver_sql("ALTER TABLE voice_presets ADD COLUMN is_enabled BOOLEAN DEFAULT 1")
                print("[Startup] Auto-migrated schema: Added is_enabled to voice_presets.")

            res_u = conn.exec_driver_sql("PRAGMA table_info(users)")
            cols_u = [row[1] for row in res_u.fetchall()]
            if "avatar_url" not in cols_u and len(cols_u) > 0:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512)")
                print("[Startup] Auto-migrated schema: Added avatar_url to users.")
            if "google_id" not in cols_u and len(cols_u) > 0:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN google_id VARCHAR(128)")
                print("[Startup] Auto-migrated schema: Added google_id to users.")
    except Exception as em:
        print(f"[Startup] Migration check info: {em}")

    # Seed default voice presets & admin account
    db = SessionLocal()
    try:
        JobService.seed_presets(db)
        print("[Startup] Seeded 25 default voice presets.")
        JobService.cleanup_stale_jobs(db, max_age_minutes=5)
        from app.services.auth_service import AuthService
        AuthService.seed_default_admin(db)
        # Ensure database is synced to private backup dataset
        DbSyncService.backup_database(immediate=False)
    finally:
        db.close()

    # Pre-generate Kaggle worker script and metadata
    try:
        KaggleNotebookBuilder.generate_kernel_metadata()
        KaggleNotebookBuilder.generate_worker_script()
        print("[Startup] Pre-generated Kaggle worker files in kaggle_worker/.")
    except Exception as e:
        print(f"[Startup] Could not pre-generate Kaggle files: {e}")

    # Start background periodic cloud database sync (every 3 minutes)
    try:
        DbSyncService.start_periodic_sync(interval_seconds=180)
    except Exception as e_ps:
        print(f"[Startup] Periodic sync notice: {e_ps}")

    # Start FastMCP Streamable HTTP session manager for AI agents (ChatGPT 2026, etc.)
    mcp_ctx = None
    try:
        from app.mcp_server import mcp
        mcp_ctx = mcp.session_manager.run()
        await mcp_ctx.__aenter__()
        print("[Startup] FastMCP Streamable HTTP session manager running.")
    except Exception as e_mcp:
        print(f"[Startup] FastMCP session manager notice: {e_mcp}")

    yield

    if mcp_ctx:
        try:
            await mcp_ctx.__aexit__(None, None, None)
        except Exception:
            pass

    # Shutdown tasks
    print("[Shutdown] Stopping VieNeu Gateway...")
    try:
        from app.services.db_sync_service import DbSyncService
        print("[Shutdown] Flushing final database state to cloud before exit...")
        DbSyncService._do_upload()
    except Exception as e_flush:
        print(f"[Shutdown] Final DB sync notice: {e_flush}")

app = FastAPI(
    title="OlokaTTS Gateway",
    description="High-fidelity 48kHz Vietnamese Neural TTS Gateway powered by Kaggle Dual Tesla T4 Workers",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Jinja2 filter
web.templates.env.filters["basename"] = os.path.basename

# Static and Storage Mounts
STATIC_DIR = Path(__file__).resolve().parent / "static"
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

os.makedirs(settings.AUDIO_DIR, exist_ok=True)
app.mount("/audio_files", StaticFiles(directory=settings.AUDIO_DIR), name="audio_files")

# Include routers
app.include_router(web.router)
app.include_router(auth.router)
app.include_router(user_settings.router)
app.include_router(admin_views.router)
app.include_router(tts.router)
app.include_router(voices.router)
app.include_router(internal_worker.router)
app.include_router(openai_speech.router)
app.include_router(admin.router)
app.include_router(mcp_auth.router)

# Mount Model Context Protocol (MCP) Server for Remote AI Agents (ChatGPT, Claude, etc.)
try:
    from app.mcp_server import mcp, streamable_http_app
    from starlette.routing import Route

    # Direct routes for /mcp and /mcp/ (avoids 307 redirect on POST /mcp from ChatGPT 2026)
    st_endpoint = streamable_http_app.routes[0].endpoint
    app.routes.insert(0, Route("/mcp", endpoint=st_endpoint, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"]))
    app.routes.insert(1, Route("/mcp/", endpoint=st_endpoint, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"]))

    # Also mount SSE app at /mcp/sse and /sse for SSE clients
    sse_subapp = mcp.sse_app()
    app.mount("/mcp/sse", sse_subapp)
    app.mount("/sse", sse_subapp)
    print("[Startup] Mounted Model Context Protocol (MCP) Streamable HTTP at /mcp and SSE at /mcp/sse")
except Exception as _mcp_err:
    print(f"[Startup] Warning: Could not mount MCP server: {_mcp_err}")

@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "app": "OlokaTTS Gateway", "version": "1.0.0"}

@app.get("/api/status", tags=["Status"])
def public_system_status(db: Session = Depends(get_db)):
    """Lightweight public status check for AI clients and MCP."""
    from datetime import datetime, timedelta
    from app.models import WorkerSession, TTSJob
    from app.services.local_engine import LocalEngine

    cutoff = datetime.utcnow() - timedelta(seconds=90)
    live_workers = db.query(WorkerSession).filter(
        WorkerSession.status.in_(["starting", "ready", "busy"]),
        WorkerSession.last_heartbeat_at >= cutoff
    ).count()
    pending = db.query(TTSJob).filter(TTSJob.status.in_(["queued", "booting_kaggle", "processing"])).count()
    
    return {
        "status": "ready" if (live_workers > 0 or LocalEngine.is_available()) else "standby",
        "live_worker_count": live_workers,
        "pending_jobs": pending,
        "local_engine": LocalEngine.is_available()
    }
