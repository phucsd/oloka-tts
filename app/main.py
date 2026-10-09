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

def init_database():
    import app.models  # Ensure all models are registered in Base.metadata
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
            if "lease_token" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN lease_token VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added lease_token to tts_jobs.")
            if "lease_expires_at" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN lease_expires_at DATETIME")
                print("[Startup] Auto-migrated schema: Added lease_expires_at to tts_jobs.")
            if "execution_account_id" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN execution_account_id VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added execution_account_id to tts_jobs.")
            if "completed_at" not in cols and len(cols) > 0:
                conn.exec_driver_sql("ALTER TABLE tts_jobs ADD COLUMN completed_at DATETIME")
                print("[Startup] Auto-migrated schema: Added completed_at to tts_jobs.")

            res_w = conn.exec_driver_sql("PRAGMA table_info(worker_sessions)")
            cols_w = [row[1] for row in res_w.fetchall()]
            if "owner_user_id" not in cols_w and len(cols_w) > 0:
                conn.exec_driver_sql("ALTER TABLE worker_sessions ADD COLUMN owner_user_id VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added owner_user_id to worker_sessions.")
            if "execution_account_id" not in cols_w and len(cols_w) > 0:
                conn.exec_driver_sql("ALTER TABLE worker_sessions ADD COLUMN execution_account_id VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added execution_account_id to worker_sessions.")
            if "kernel_ref" not in cols_w and len(cols_w) > 0:
                conn.exec_driver_sql("ALTER TABLE worker_sessions ADD COLUMN kernel_ref VARCHAR(256)")
                print("[Startup] Auto-migrated schema: Added kernel_ref to worker_sessions.")

            # Safe policy: deactivate legacy unowned worker sessions
            conn.exec_driver_sql("UPDATE worker_sessions SET status = 'offline' WHERE owner_user_id IS NULL")

            res_s = conn.exec_driver_sql("PRAGMA table_info(voice_samples)")
            cols_s = [row[1] for row in res_s.fetchall()]
            if "user_id" not in cols_s and len(cols_s) > 0:
                conn.exec_driver_sql("ALTER TABLE voice_samples ADD COLUMN user_id VARCHAR(64)")
                print("[Startup] Auto-migrated schema: Added user_id to voice_samples.")
            
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

            res_mcp = conn.exec_driver_sql("PRAGMA table_info(mcp_pairing_sessions)")
            cols_mcp = [row[1] for row in res_mcp.fetchall()]
            if "session_token" not in cols_mcp and len(cols_mcp) > 0:
                conn.exec_driver_sql("ALTER TABLE mcp_pairing_sessions ADD COLUMN session_token VARCHAR(128)")
                print("[Startup] Auto-migrated schema: Added session_token to mcp_pairing_sessions.")
            if "session_token_hash" not in cols_mcp and len(cols_mcp) > 0:
                conn.exec_driver_sql("ALTER TABLE mcp_pairing_sessions ADD COLUMN session_token_hash VARCHAR(256)")
                print("[Startup] Auto-migrated schema: Added session_token_hash to mcp_pairing_sessions.")
            if "transport_session_id" not in cols_mcp and len(cols_mcp) > 0:
                conn.exec_driver_sql("ALTER TABLE mcp_pairing_sessions ADD COLUMN transport_session_id VARCHAR(128)")
                print("[Startup] Auto-migrated schema: Added transport_session_id to mcp_pairing_sessions.")
            for oauth_col in ["code_challenge", "code_challenge_method", "redirect_uri", "client_id", "scope"]:
                if oauth_col not in cols_mcp and len(cols_mcp) > 0:
                    conn.exec_driver_sql(f"ALTER TABLE mcp_pairing_sessions ADD COLUMN {oauth_col} VARCHAR(512)")
                    print(f"[Startup] Auto-migrated schema: Added {oauth_col} to mcp_pairing_sessions.")

            res_cli = conn.exec_driver_sql("PRAGMA table_info(oauth_clients)")
            cols_cli = [row[1] for row in res_cli.fetchall()]
            if "is_confidential" not in cols_cli and len(cols_cli) > 0:
                conn.exec_driver_sql("ALTER TABLE oauth_clients ADD COLUMN is_confidential BOOLEAN DEFAULT 0")
            if "token_endpoint_auth_method" not in cols_cli and len(cols_cli) > 0:
                conn.exec_driver_sql("ALTER TABLE oauth_clients ADD COLUMN token_endpoint_auth_method VARCHAR(64) DEFAULT 'none'")

            conn.exec_driver_sql("""
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    id VARCHAR(64) PRIMARY KEY,
                    token_id VARCHAR(64),
                    token_type VARCHAR(32) DEFAULT 'access_token',
                    token_hash VARCHAR(256) NOT NULL UNIQUE,
                    token_masked VARCHAR(64),
                    user_id VARCHAR(64) NOT NULL,
                    client_id VARCHAR(128) NOT NULL,
                    scope VARCHAR(256) DEFAULT 'mcp:all speech:generate',
                    audience VARCHAR(256) DEFAULT 'https://tts.oloka.net/mcp',
                    family_id VARCHAR(64),
                    created_at DATETIME,
                    expires_at DATETIME NOT NULL,
                    revoked_at DATETIME,
                    last_used_at DATETIME,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_oauth_tokens_token_hash ON oauth_tokens(token_hash)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_oauth_tokens_user_id ON oauth_tokens(user_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_oauth_tokens_client_id ON oauth_tokens(client_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_oauth_tokens_family_id ON oauth_tokens(family_id)")


            # Auto-hash any legacy plaintext session tokens
            try:
                import hashlib
                res_unhashed = conn.exec_driver_sql(
                    "SELECT id, session_token FROM mcp_pairing_sessions WHERE session_token_hash IS NULL AND session_token IS NOT NULL"
                ).fetchall()
                for row_u in res_unhashed:
                    s_id, r_tok = row_u[0], row_u[1]
                    if r_tok and not r_tok.endswith("..."):
                        t_hash = hashlib.sha256(r_tok.strip().encode("utf-8")).hexdigest()
                        conn.exec_driver_sql(
                            "UPDATE mcp_pairing_sessions SET session_token_hash = :thash WHERE id = :sid",
                            {"thash": t_hash, "sid": s_id}
                        )
            except Exception as _eh:
                pass

            conn.commit()
    except Exception as em:
        print(f"[Startup] Migration check info: {em}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks: Restore database from cloud if running in ephemeral container
    from app.services.db_sync_service import DbSyncService
    try:
        DbSyncService.restore_database()
    except Exception as e_restore:
        print(f"[Startup] Database restore check: {e_restore}")

    init_database()

    # Seed default voice presets & admin account
    db = SessionLocal()
    try:
        JobService.seed_presets(db)
        print("[Startup] Seeded 25 default voice presets.")
        JobService.cleanup_stale_jobs(db, max_age_minutes=5)
        from app.services.auth_service import AuthService
        AuthService.seed_default_admin(db)
        
        # Idempotent migration of any legacy plaintext API keys to hashed ApiKey table
        from app.services.api_key_service import ApiKeyService
        ApiKeyService.migrate_legacy_keys(db)

        # Auto-seed KaggleExecutionAccounts for configured users
        from app.services.kaggle_account_service import KaggleAccountService
        from app.models import User
        users_with_creds = db.query(User).filter(User.kaggle_username.isnot(None), User.kaggle_key.isnot(None)).all()
        for u in users_with_creds:
            KaggleAccountService.get_execution_account_for_user(db, u)
        admin_user = db.query(User).filter(User.role == "admin").first()
        if admin_user:
            KaggleAccountService.get_execution_account_for_user(db, admin_user)

        # Clean up any legacy bogus 'sess_%' transport_session_ids
        try:
            from sqlalchemy import text
            db.execute(text("UPDATE mcp_pairing_sessions SET transport_session_id = NULL WHERE transport_session_id LIKE 'sess_%'"))
            db.commit()
        except Exception:
            pass

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
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        if getattr(mcp, "_session_manager", None) is not None:
            sm = mcp._session_manager
            if getattr(sm, "_has_started", False) and getattr(sm, "_task_group", None) is None:
                mcp._session_manager = StreamableHTTPSessionManager(
                    app=mcp._mcp_server,
                    event_store=mcp._event_store,
                    retry_interval=mcp._retry_interval,
                    json_response=mcp.settings.json_response,
                    stateless=mcp.settings.stateless_http,
                    security_settings=mcp.settings.transport_security,
                )
        if getattr(mcp.session_manager, "_task_group", None) is None:
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

# CORS Middleware (Strict explicit origin configuration)
cors_allowed_origins = [
    "https://tts.oloka.net",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
]
if settings.PUBLIC_API_BASE_URL:
    pub_base = settings.PUBLIC_API_BASE_URL.rstrip("/")
    if pub_base not in cors_allowed_origins:
        cors_allowed_origins.append(pub_base)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins,
    allow_origin_regex=r"^https://.*\.hf\.space$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)

# Register Jinja2 filter
web.templates.env.filters["basename"] = os.path.basename

# Static Mounts (Only public static assets like CSS/JS/images/preset previews)
STATIC_DIR = Path(__file__).resolve().parent / "static"
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Note: Generated private audio files are accessed strictly through authenticated /v1/tts/jobs/{job_id}/audio
# Direct unauthenticated static file mounting of AUDIO_DIR has been removed for privacy and security.


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
    from app.mcp_server import mcp, streamable_http_app, get_base_url
    from app.services.mcp_auth_service import McpAuthService
    from app.database import SessionLocal
    from starlette.responses import Response
    from starlette.routing import Route
    import json

    st_endpoint = streamable_http_app.routes[0].endpoint

    class McpOAuthSecurityEndpoint:
        def __init__(self, raw_endpoint):
            self.raw_endpoint = raw_endpoint

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.raw_endpoint(scope, receive, send)
                return

            method = scope.get("method", "GET")

            # Extract Authorization header if present
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("latin-1").strip()
            bearer_token = None
            if auth_header.lower().startswith("bearer "):
                bearer_token = auth_header[7:].strip()

            base = get_base_url()
            protected_meta_url = f"{base}/.well-known/oauth-protected-resource"

            # 1. If Bearer token is provided: validate it strictly against DB
            if bearer_token:
                db = SessionLocal()
                try:
                    user, otok, err_reason = McpAuthService.validate_bearer_token(db, bearer_token)
                    if not user:
                        # Return HTTP 401 with standard RFC 6750 / RFC 9728 header
                        response = Response(
                            status_code=401,
                            headers={
                                "WWW-Authenticate": f'Bearer error="invalid_token", error_description="The access token expired, was revoked, or is invalid", resource_metadata="{protected_meta_url}"',
                                "Content-Type": "application/json"
                            },
                            content=json.dumps({
                                "jsonrpc": "2.0",
                                "error": {
                                    "code": -32001,
                                    "message": "Unauthorized: Invalid or expired access token"
                                }
                            }).encode("utf-8")
                        )
                        await response(scope, receive, send)
                        return
                finally:
                    db.close()

            # 2. For GET/HEAD/OPTIONS (handshakes, SSE connect): pass through
            if method != "POST":
                await self.raw_endpoint(scope, receive, send)
                return

            # 3. For POST /mcp: Read and inspect JSON-RPC payload
            body_chunks = []
            more_body = True
            while more_body:
                message = await receive()
                body_chunks.append(message.get("body", b""))
                more_body = message.get("more_body", False)
            full_body = b"".join(body_chunks)

            # Reconstruct receive callable for downstream
            body_consumed = False
            async def new_receive():
                nonlocal body_consumed
                if not body_consumed:
                    body_consumed = True
                    return {"type": "http.request", "body": full_body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            # Inspect JSON-RPC
            try:
                rpc_data = json.loads(full_body.decode("utf-8")) if full_body else {}
            except Exception:
                rpc_data = {}

            rpc_method = rpc_data.get("method")
            rpc_id = rpc_data.get("id")

            # Check if calling protected tool
            is_unauth_generate = False
            if rpc_method == "tools/call":
                tool_params = rpc_data.get("params", {})
                tool_name = tool_params.get("name")
                tool_args = tool_params.get("arguments", {})

                # If calling generate_speech without any bearer token AND without in-tool credentials:
                if tool_name == "generate_speech" and not bearer_token:
                    has_pair_code = bool(tool_args.get("pair_code") and str(tool_args["pair_code"]).strip())
                    has_sess_tok = bool(tool_args.get("session_token") and str(tool_args["session_token"]).strip())
                    has_api_key = bool(tool_args.get("api_key") and str(tool_args["api_key"]).strip())

                    if not (has_pair_code or has_sess_tok or has_api_key):
                        # Check if client explicitly requested HTTP-level challenge (e.g. via header or query param)
                        req_headers = dict(scope.get("headers", []))
                        force_http_challenge = (
                            req_headers.get(b"x-mcp-challenge-level", b"").decode("latin-1").lower() == "http" or
                            b"challenge=http" in scope.get("query_string", b"")
                        )

                        if force_http_challenge:
                            # Challenge with HTTP 401 & WWW-Authenticate header according to RFC 9728
                            response = Response(
                                status_code=401,
                                headers={
                                    "WWW-Authenticate": f'Bearer resource_metadata="{protected_meta_url}", scope="speech:generate"',
                                    "Content-Type": "application/json"
                                },
                                content=json.dumps({
                                    "jsonrpc": "2.0",
                                    "id": rpc_id,
                                    "error": {
                                        "code": -32001,
                                        "message": "Authentication required. Please authenticate via OAuth 2.1 to access this protected tool."
                                    }
                                }).encode("utf-8")
                            )
                            await response(scope, receive, send)
                            return

                        is_unauth_generate = True

            # If this is unauthenticated generate_speech, wrap send to attach WWW-Authenticate header to HTTP response
            if is_unauth_generate:
                async def wrapped_send(msg):
                    if msg.get("type") == "http.response.start":
                        resp_headers = list(msg.get("headers", []))
                        auth_val = f'Bearer resource_metadata="{protected_meta_url}", scope="speech:generate"'
                        resp_headers.append((b"www-authenticate", auth_val.encode("latin-1")))
                        msg = dict(msg)
                        msg["headers"] = resp_headers
                    await send(msg)

                try:
                    await mcp.session_manager.handle_request(scope, new_receive, wrapped_send)
                except Exception:
                    await self.raw_endpoint(scope, new_receive, wrapped_send)
                return

            # All other methods (initialize, tools/list, list_voices, get_system_status, link_account)
            # or authenticated requests pass through to FastMCP streamable HTTP
            try:
                await mcp.session_manager.handle_request(scope, new_receive, send)
            except Exception:
                await self.raw_endpoint(scope, new_receive, send)

    secured_endpoint = McpOAuthSecurityEndpoint(st_endpoint)

    # Direct routes for /mcp and /mcp/ (avoids 307 redirect on POST /mcp from ChatGPT 2026)
    app.routes.insert(0, Route("/mcp", endpoint=secured_endpoint, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"]))
    app.routes.insert(1, Route("/mcp/", endpoint=secured_endpoint, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"]))

    # Also mount SSE app at /mcp/sse and /sse for SSE clients
    sse_subapp = mcp.sse_app()
    app.mount("/mcp/sse", sse_subapp)
    app.mount("/sse", sse_subapp)
    print("[Startup] Mounted Model Context Protocol (MCP) Streamable HTTP with OAuth 2.1 Challenge at /mcp")
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
    from app.services.job_service import JobService

    # Clean up any stale pending/booting jobs (> 5 minutes)
    JobService.cleanup_stale_jobs(db, max_age_minutes=5)

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
