import os
import shutil
from pathlib import Path
import pytest

# Enforce TESTING environment variable immediately
os.environ["TESTING"] = "1"

# Determine project base dir and test db path
BASE_DIR = Path(__file__).resolve().parent.parent
TEST_DB_PATH = BASE_DIR / "storage" / "test_isolated.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

from app.config import settings
settings.DATABASE_URL = f"sqlite:///{TEST_DB_PATH}"

from app.database import engine, Base, SessionLocal
from app.main import init_database
from app.services.job_service import JobService
from app.services.auth_service import AuthService

@pytest.fixture(scope="session", autouse=True)
def initialize_isolated_test_database():
    """
    Session fixture:
    - Cleans up any existing test_isolated.db files.
    - Creates tables and runs migrations on test_isolated.db ONLY.
    - Ensures production database (vieneu_gateway.db) is untouched.
    - Cleans up test_isolated.db on session completion.
    """
    # 1. Clean existing test files
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        p = Path(f"{TEST_DB_PATH}{ext}")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    # 2. Run migrations on the isolated test database
    init_database()

    # 3. Seed presets and admin in test database
    db = SessionLocal()
    try:
        JobService.seed_presets(db)
        AuthService.seed_default_admin(db)
    finally:
        db.close()

    yield

    # 4. Teardown
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        p = Path(f"{TEST_DB_PATH}{ext}")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
