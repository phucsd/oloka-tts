import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, Float, Integer, Boolean, DateTime, ForeignKey
from app.database import Base

def generate_id(prefix: str = "job") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

class TTSJob(Base):
    __tablename__ = "tts_jobs"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("job"))
    user_id = Column(String(64), nullable=True, index=True)
    execution_account_id = Column(String(64), ForeignKey("kaggle_execution_accounts.id"), nullable=True, index=True)
    prompt = Column(Text, nullable=False)
    voice_type = Column(String(32), default="preset")  # "preset" | "clone"
    voice_id = Column(String(128), default="Phạm Tuyên")
    ref_audio_path = Column(String(512), nullable=True)
    ref_text = Column(Text, nullable=True)
    speed = Column(Float, default=1.0)
    temperature = Column(Float, default=0.7)
    silence_p = Column(Float, default=0.15)
    
    status = Column(String(32), default="queued")  # queued, booting_kaggle, processing, completed, failed
    error_message = Column(Text, nullable=True)
    audio_path = Column(String(512), nullable=True)
    audio_url = Column(String(512), nullable=True)
    duration = Column(Float, nullable=True)
    sample_rate = Column(Integer, default=48000)
    worker_id = Column(String(64), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    execution_time = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class VoicePreset(Base):
    __tablename__ = "voice_presets"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    voice_id = Column(String(128), nullable=False, unique=True)
    gender = Column(String(16), default="Nữ")  # "Nam" | "Nữ"
    region = Column(String(32), default="Bắc")  # "Bắc" | "Trung" | "Nam"
    description = Column(String(256), nullable=True)
    is_editors_pick = Column(Boolean, default=False)
    is_enabled = Column(Boolean, default=True)
    preview_audio_url = Column(String(512), nullable=True)

class VoiceSample(Base):
    __tablename__ = "voice_samples"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("vs"))
    user_id = Column(String(64), nullable=True, index=True)
    name = Column(String(128), nullable=False)
    file_path = Column(String(512), nullable=False)
    ref_text = Column(Text, nullable=True)
    duration = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkerSession(Base):
    __tablename__ = "worker_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    worker_id = Column(String(64), unique=True, index=True)
    owner_user_id = Column(String(64), ForeignKey("users.id"), index=True, nullable=True)
    execution_account_id = Column(String(64), ForeignKey("kaggle_execution_accounts.id"), index=True, nullable=True)
    kernel_ref = Column(String(256), nullable=True)
    gpu_index = Column(Integer, default=0)
    gpu_name = Column(String(64), default="Tesla T4")
    vram_total_mb = Column(Integer, default=15360)
    vram_used_mb = Column(Integer, default=0)
    status = Column(String(32), default="starting")  # starting, ready, busy, offline, stopping
    current_job_id = Column(String(64), nullable=True)
    ip_address = Column(String(64), nullable=True)
    last_heartbeat_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, default=datetime.utcnow)

class KaggleExecutionAccount(Base):
    __tablename__ = "kaggle_execution_accounts"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("kacc"))
    owner_user_id = Column(String(64), ForeignKey("users.id"), index=True, nullable=False)
    kaggle_username = Column(String(128), nullable=False)
    kaggle_key = Column(String(256), nullable=False)
    kernel_slug = Column(String(128), default="vieneu-tts-dual-t4-worker")
    kernel_ref = Column(String(256), nullable=False)
    kernel_title = Column(String(128), default="VieNeu TTS Dual T4 Worker")
    is_enabled = Column(Boolean, default=True)
    last_validation_at = Column(DateTime, nullable=True)
    last_status = Column(String(64), default="configured")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class WorkerToken(Base):
    __tablename__ = "worker_tokens"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("wtk"))
    token_hash = Column(String(256), unique=True, index=True, nullable=False)
    token_prefix = Column(String(32), nullable=False)
    owner_user_id = Column(String(64), ForeignKey("users.id"), index=True, nullable=False)
    execution_account_id = Column(String(64), ForeignKey("kaggle_execution_accounts.id"), index=True, nullable=True)
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    description = Column(String(256), nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("usr"))
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(32), default="user")  # "admin" | "user"
    is_active = Column(Boolean, default=True)
    
    # Bring-Your-Own-Kaggle (BYOK) per-user credentials
    kaggle_username = Column(String(128), nullable=True)
    kaggle_key = Column(String(256), nullable=True)
    
    avatar_url = Column(String(512), nullable=True)
    google_id = Column(String(128), nullable=True, index=True)

    api_key = Column(String(64), unique=True, index=True, default=lambda: generate_id("key"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("log"))
    user_id = Column(String(64), nullable=True, index=True)
    level = Column(String(16), default="INFO")  # "INFO", "WARNING", "ERROR"
    action = Column(String(64), nullable=False)  # "login", "register", "tts_generate", "kaggle_boot", "error"
    message = Column(String(512), nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("key"))
    user_id = Column(String(64), ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(128), nullable=False, default="Default API Key")
    key_prefix = Column(String(32), nullable=False)    # e.g. "oloka_live_9f8a...1c0d"
    key_hash = Column(String(256), nullable=False, unique=True, index=True) # SHA-256
    scopes = Column(String(256), default="tts:generate,voices:read")
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    total_requests = Column(Integer, default=0)
    total_characters = Column(Integer, default=0)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class McpPairingSession(Base):
    __tablename__ = "mcp_pairing_sessions"

    id = Column(String(64), primary_key=True, default=lambda: generate_id("mcp_sess"))
    code = Column(String(32), unique=True, index=True, nullable=False)
    session_token = Column(String(128), index=True, nullable=True)  # Legacy or masked reference
    session_token_hash = Column(String(256), unique=True, index=True, nullable=True)  # SHA-256 hash of secret token
    transport_session_id = Column(String(128), index=True, nullable=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=True, index=True)
    status = Column(String(32), default="pending")  # "pending", "authorized", "revoked", "expired"
    client_name = Column(String(128), default="ChatGPT")
    code_challenge = Column(String(256), nullable=True)
    code_challenge_method = Column(String(32), nullable=True)  # "S256" or "plain"
    redirect_uri = Column(String(512), nullable=True)
    client_id = Column(String(128), nullable=True)
    scope = Column(String(256), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    authorized_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id = Column(String(128), primary_key=True)
    client_secret = Column(String(256), nullable=True)
    client_name = Column(String(256), nullable=True)
    redirect_uris = Column(Text, nullable=True)  # JSON-encoded list of allowed redirect URIs
    created_at = Column(DateTime, default=datetime.utcnow)

