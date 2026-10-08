import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    BASE_DIR: Path = BASE_DIR
    HOST: str = "0.0.0.0"
    PORT: int = int(os.environ.get("PORT", 8000))
    APP_ENV: str = "development"
    DEBUG: bool = True
    PUBLIC_API_BASE_URL: str = os.environ.get(
        "PUBLIC_API_BASE_URL",
        f"https://{os.environ.get('SPACE_ID', '').replace('/', '-')}.hf.space" if os.environ.get("SPACE_ID") else "http://localhost:8000"
    )
    WORKER_TOKEN: str = os.environ.get("WORKER_TOKEN", "")
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
    ADMIN_DEFAULT_PASSWORD: str = os.environ.get("ADMIN_DEFAULT_PASSWORD", "")
    
    # Google OAuth 2.0 settings
    GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    ADMIN_GOOGLE_EMAIL: str = os.environ.get("ADMIN_GOOGLE_EMAIL", "phucsd@gmail.com")
    GOOGLE_REDIRECT_URI: str = os.environ.get("GOOGLE_REDIRECT_URI", "")

    # Kaggle Orchestrator settings
    KAGGLE_USERNAME: str = os.environ.get("KAGGLE_USERNAME", "")
    KAGGLE_KEY: str = os.environ.get("KAGGLE_KEY", os.environ.get("KAGGLE_API_TOKEN", ""))
    KAGGLE_KERNEL_REF: str = "phcnguynhukendykerry/vieneu-tts-dual-t4-worker"
    KAGGLE_KERNEL_SLUG: str = "vieneu-tts-dual-t4-worker"
    KAGGLE_KERNEL_TITLE: str = "VieNeu TTS Dual T4 Worker"
    KAGGLE_ACCELERATOR: str = "nvidiaTeslaT4"
    KAGGLE_TIMEOUT_SECONDS: int = 36000
    KAGGLE_WORKER_DIR: str = str(BASE_DIR / "kaggle_worker")

    # Storage settings
    DATABASE_URL: str = os.environ.get("DATABASE_URL") or (
        f"sqlite:///{BASE_DIR / 'storage' / 'test_isolated.db'}" if os.environ.get("TESTING") == "1"
        else (
            f"sqlite:///{Path('/data/storage/vieneu_gateway.db')}" if Path("/data").exists() and os.access("/data", os.W_OK)
            else f"sqlite:///{BASE_DIR / 'storage' / 'vieneu_gateway.db'}"
        )
    )
    AUDIO_DIR: str = os.environ.get("AUDIO_DIR") or (
        str(Path("/data/storage/audio")) if Path("/data").exists() and os.access("/data", os.W_OK)
        else str(BASE_DIR / "storage" / "audio")
    )
    SAMPLES_DIR: str = os.environ.get("SAMPLES_DIR") or (
        str(Path("/data/storage/samples")) if Path("/data").exists() and os.access("/data", os.W_OK)
        else str(BASE_DIR / "storage" / "samples")
    )

    # Fallback settings
    FALLBACK_TO_LOCAL_CPU: bool = True
    VIENEU_BACKEND: str = "auto"

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()

# Secure credential checks and runtime generation
import secrets

if not settings.SECRET_KEY:
    if settings.APP_ENV == "production":
        raise RuntimeError("CRITICAL SECURITY ERROR: 'SECRET_KEY' environment variable must be set in production (minimum 32 characters)!")
    settings.SECRET_KEY = secrets.token_urlsafe(32)
    print("[SECURITY NOTICE] No SECRET_KEY provided in environment. Generated dynamic runtime SECRET_KEY.")

if not settings.WORKER_TOKEN:
    settings.WORKER_TOKEN = secrets.token_hex(24)
    print("[SECURITY NOTICE] No WORKER_TOKEN configured in environment. Generated dynamic runtime WORKER_TOKEN.")

# Normalize postgres URL dialect if provided
if settings.DATABASE_URL.startswith("postgres://"):
    settings.DATABASE_URL = settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Ensure directories exist
os.makedirs(settings.AUDIO_DIR, exist_ok=True)
os.makedirs(settings.SAMPLES_DIR, exist_ok=True)
if settings.DATABASE_URL.startswith("sqlite"):
    os.makedirs(Path(settings.DATABASE_URL.replace("sqlite:///", "")).parent, exist_ok=True)

