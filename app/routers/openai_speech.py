import os
import time
import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import TTSJob, User
from app.schemas import OpenAISpeechRequest
from app.services.job_service import JobService
from app.services.auth_service import get_current_user_optional
from app.services.api_key_service import ApiKeyService
from app.services.settings_service import SettingsService

router = APIRouter(prefix="/v1/audio", tags=["OpenAI Compatibility"])

@router.post("/speech")
async def openai_speech(
    req: OpenAISpeechRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    """
    OpenAI-compatible speech endpoint: POST /v1/audio/speech
    """
    # Track API key usage if called via Bearer API Key
    api_key_obj = getattr(request.state, "api_key", None)
    if api_key_obj:
        ApiKeyService.record_usage(db, api_key_obj.id, chars=len(req.input or ""))

    user_id = user.id if user else None
    if not user_id:
        require_auth = SettingsService.get_bool(db, "mcp_require_auth", default=True)
        if require_auth:
            raise HTTPException(
                status_code=401,
                detail="Yêu cầu xác thực tài khoản. Vui lòng cung cấp API Key hợp lệ hoặc mã ghép đôi phiên (Pair Code)."
            )
        # Fallback to system admin only if mcp_require_auth is explicitly disabled
        admin_user = db.query(User).filter(User.role == "admin").first()
        if admin_user:
            user_id = admin_user.id

    job = JobService.create_job(
        db=db,
        prompt=req.input,
        voice_type="preset",
        voice_id=req.voice,
        speed=req.speed,
        user_id=user_id
    )

    # Wait up to 110 seconds for completion (covers cold start if worker booting)
    start_time = time.time()
    while time.time() - start_time < 110:
        await asyncio.sleep(1)
        db.rollback()  # Releases any open SQLite read transaction/locks
        row = db.query(TTSJob.status, TTSJob.error_message, TTSJob.audio_path).filter(TTSJob.id == job.id).first()
        if row:
            st, err_msg, a_path = row
            if st == "completed" and a_path and os.path.exists(a_path):
                return FileResponse(
                    a_path,
                    media_type="audio/wav",
                    filename=f"speech_{job.id}.wav"
                )
            elif st == "failed":
                raise HTTPException(status_code=500, detail=f"TTS generation failed: {err_msg}")

    raise HTTPException(status_code=504, detail="TTS generation timed out waiting for worker")
