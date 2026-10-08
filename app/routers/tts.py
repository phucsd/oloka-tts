import os
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.database import get_db
from typing import Optional
from app.models import TTSJob, User
from app.schemas import TTSJobCreate, TTSJobResponse
from app.services.job_service import JobService
from app.services.local_engine import LocalEngine
from app.services.kaggle_orchestrator import KaggleOrchestrator
from app.services.auth_service import get_current_user_optional

router = APIRouter(prefix="/v1/tts", tags=["TTS"])

@router.post("/jobs", response_model=TTSJobResponse)
def create_tts_job(
    req: TTSJobCreate,
    request: Request,
    force_local: bool = Query(False),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Vui lòng đăng nhập hoặc đăng ký tài khoản để tạo giọng nói."
        )

    if force_local:
        if not LocalEngine.is_available():
            raise HTTPException(
                status_code=400,
                detail="Local CPU engine hiện không khả dụng trên máy chủ Gateway. Vui lòng cấu hình Kaggle API Key trong Cài Đặt để sử dụng GPU."
            )
    else:
        if not KaggleOrchestrator.has_live_worker(db):
            has_creds = bool(user.kaggle_username and user.kaggle_key) or (user.role == "admin" and KaggleOrchestrator.is_configured())
            if not has_creds:
                raise HTTPException(
                    status_code=400,
                    detail="Bạn chưa cài đặt Kaggle API Key. Vui lòng vào trang Cài Đặt để cấu hình tài khoản Kaggle của bạn."
                )

    # Track API key usage if called via Bearer API Key
    api_key_obj = getattr(request.state, "api_key", None)
    if api_key_obj:
        from app.services.api_key_service import ApiKeyService
        ApiKeyService.record_usage(db, api_key_obj.id, chars=len(req.prompt or ""))

    job = JobService.create_job(
        db=db,
        prompt=req.prompt,
        voice_type=req.voice_type,
        voice_id=req.resolved_voice_id,
        ref_sample_id=req.ref_sample_id,
        speed=req.speed,
        temperature=req.temperature,
        silence_p=req.silence_p,
        force_local=force_local,
        user_id=user.id
    )
    return job

@router.get("/jobs/{job_id}", response_model=TTSJobResponse)
def get_tts_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@router.get("/jobs/{job_id}/audio")
def get_job_audio(job_id: str, db: Session = Depends(get_db)):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job or not job.audio_path:
        raise HTTPException(status_code=404, detail="Audio not found or job not finished")
    
    if not os.path.exists(job.audio_path):
        raise HTTPException(status_code=404, detail="Audio file missing on disk")

    return FileResponse(
        job.audio_path,
        media_type="audio/wav",
        filename=f"vieneu_{job_id}.wav"
    )

@router.get("/jobs", response_model=list[TTSJobResponse])
def list_recent_jobs(limit: int = 20, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user_optional)):
    if not user:
        return []
    query = db.query(TTSJob)
    if user.role != "admin":
        query = query.filter(TTSJob.user_id == user.id)
    return query.order_by(TTSJob.created_at.desc()).limit(limit).all()
