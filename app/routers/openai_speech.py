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
from app.services.kaggle_account_service import KaggleAccountService

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
    Strict tenant isolation: Requires authenticated user and their own BYOK Kaggle execution account.
    Never falls back to admin account or admin GPU.
    """
    # Track API key usage if called via Bearer API Key
    api_key_obj = getattr(request.state, "api_key", None)
    if api_key_obj:
        granted_scopes = [s.strip() for s in (api_key_obj.scopes or "").split(",")]
        if "full:access" not in granted_scopes and "tts:generate" not in granted_scopes and "tts:write" not in granted_scopes:
            raise HTTPException(
                status_code=403,
                detail="API Key không có quyền tạo âm thanh (yêu cầu scope 'tts:generate', 'tts:write' hoặc 'full:access')"
            )
        ApiKeyService.record_usage(db, api_key_obj.id, chars=len(req.input or ""))

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Yêu cầu xác thực tài khoản. Vui lòng cung cấp API Key hợp lệ (Bearer token) hoặc mã phiên."
        )

    # Resolve tenant's dedicated Kaggle execution account
    acc = KaggleAccountService.get_execution_account_for_user(db, user)
    if not acc:
        raise HTTPException(
            status_code=400,
            detail="Tài khoản của bạn chưa cấu hình Kaggle API Key cá nhân. Vui lòng vào Cài Đặt (https://tts.oloka.net/settings) để kết nối Kaggle GPU."
        )

    job = JobService.create_job(
        db=db,
        prompt=req.input,
        voice_type="preset",
        voice_id=req.voice,
        speed=req.speed,
        user_id=user.id,
        execution_account_id=acc.id
    )

    if job.status == "failed":
        raise HTTPException(status_code=400, detail=f"Không thể khởi chạy GPU: {job.error_message}")

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
