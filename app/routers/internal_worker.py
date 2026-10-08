import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import WorkerSession, TTSJob
from app.schemas import (
    WorkerRegisterRequest, WorkerHeartbeatRequest,
    WorkerJobFailRequest
)
from app.services.job_service import JobService
from app.services.kaggle_account_service import KaggleAccountService

router = APIRouter(prefix="/api/worker", tags=["Internal Worker"])

class WorkerAuthContext:
    def __init__(self, owner_user_id: str, execution_account_id: str, token_id: str = None):
        self.owner_user_id = owner_user_id
        self.execution_account_id = execution_account_id
        self.token_id = token_id

def verify_worker_token(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
) -> WorkerAuthContext:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid token scheme")
    token = parts[1]

    wtk = KaggleAccountService.verify_raw_worker_token(db, token)
    if not wtk:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid, expired, or revoked worker token")

    return WorkerAuthContext(
        owner_user_id=wtk.owner_user_id,
        execution_account_id=wtk.execution_account_id,
        token_id=wtk.id
    )

RECENT_WORKER_LOGS = []

def add_log(worker_id: str, message: str):
    now_str = datetime.utcnow().strftime("%H:%M:%S")
    entry = f"[{now_str}] [{worker_id}] {message}"
    RECENT_WORKER_LOGS.append(entry)
    if len(RECENT_WORKER_LOGS) > 100:
        RECENT_WORKER_LOGS.pop(0)

@router.post("/log")
def log_worker_event(payload: dict, auth: WorkerAuthContext = Depends(verify_worker_token)):
    worker_id = payload.get("worker_id", "unknown")
    msg = payload.get("message", "")
    add_log(worker_id, f"{msg} (tenant: {auth.owner_user_id[:8]})")
    return {"status": "ok"}

@router.post("/register")
def register_worker(
    req: WorkerRegisterRequest,
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    session = db.query(WorkerSession).filter(WorkerSession.worker_id == req.worker_id).first()
    if session:
        # Cross-tenant spoofing prevention
        if session.owner_user_id and session.owner_user_id != auth.owner_user_id:
            raise HTTPException(status_code=403, detail="Worker ID belongs to another tenant")
        session.owner_user_id = auth.owner_user_id
        session.execution_account_id = auth.execution_account_id
        session.gpu_index = req.gpu_index
        session.gpu_name = req.gpu_name
        session.vram_total_mb = req.vram_total_mb
        session.status = "ready"
        session.started_at = datetime.utcnow()
        session.last_heartbeat_at = datetime.utcnow()
    else:
        session = WorkerSession(
            worker_id=req.worker_id,
            owner_user_id=auth.owner_user_id,
            execution_account_id=auth.execution_account_id,
            gpu_index=req.gpu_index,
            gpu_name=req.gpu_name,
            vram_total_mb=req.vram_total_mb,
            status="ready",
            last_heartbeat_at=datetime.utcnow()
        )
        db.add(session)
    db.commit()
    add_log(req.worker_id, f"REGISTERED on GPU {req.gpu_index} ({req.gpu_name}) [Tenant: {auth.owner_user_id[:8]}]")
    return {"status": "registered", "worker_id": req.worker_id}

@router.post("/heartbeat")
def worker_heartbeat(
    req: WorkerHeartbeatRequest,
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    session = db.query(WorkerSession).filter(WorkerSession.worker_id == req.worker_id).first()
    if session:
        if session.owner_user_id and session.owner_user_id != auth.owner_user_id:
            raise HTTPException(status_code=403, detail="Worker ID belongs to another tenant")
        session.vram_used_mb = req.vram_used_mb
        session.status = "offline" if req.status in ("stopped", "offline") else req.status
        session.current_job_id = req.current_job_id
        session.last_heartbeat_at = datetime.utcnow()
        db.commit()
    return {"status": "ok"}

@router.post("/jobs/pull")
def pull_job(
    payload: dict = None,
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    worker_id = (payload or {}).get("worker_id", "unknown_worker")

    sess = db.query(WorkerSession).filter(WorkerSession.worker_id == worker_id).first()
    if sess and sess.owner_user_id and sess.owner_user_id != auth.owner_user_id:
        raise HTTPException(status_code=403, detail="Worker ID belongs to another tenant")

    if sess and sess.status == "stopping":
        sess.status = "offline"
        db.commit()
        add_log(worker_id, f"Sent SHUTDOWN command to worker (tenant: {auth.owner_user_id[:8]})")
        return {"action": "shutdown"}

    # Server-Side Auto-Idle Check for this specific execution account
    from app.services.settings_service import SettingsService
    idle_min = SettingsService.get_int(db, "worker_idle_timeout_minutes", default=10)
    idle_sec = max(60, idle_min * 60)

    active_jobs = db.query(TTSJob).filter(
        TTSJob.execution_account_id == auth.execution_account_id,
        TTSJob.status.in_(["queued", "booting_kaggle", "processing"])
    ).count()

    if active_jobs == 0:
        last_worker_job = db.query(TTSJob).filter(
            TTSJob.execution_account_id == auth.execution_account_id,
            TTSJob.worker_id == worker_id,
            TTSJob.status.in_(["completed", "failed"])
        ).order_by(TTSJob.completed_at.desc()).first()

        last_time = None
        if last_worker_job and last_worker_job.completed_at:
            last_time = last_worker_job.completed_at
        elif sess and sess.started_at:
            last_time = sess.started_at

        if last_time:
            elapsed = (datetime.utcnow() - last_time).total_seconds()
            if elapsed > idle_sec:
                add_log(worker_id, f"Auto-idle timeout reached ({int(elapsed)}s > {idle_sec}s). Sending SHUTDOWN signal.")
                return {"action": "shutdown"}

    # Pull job strictly belonging to this tenant's execution account
    job = JobService.pull_pending_job(
        db=db,
        worker_id=worker_id,
        execution_account_id=auth.execution_account_id,
        owner_user_id=auth.owner_user_id
    )
    if not job:
        return {}
    
    add_log(worker_id, f"PULLED job {job['id']} ('{job['prompt'][:35]}...') [Account: {auth.execution_account_id}]")
    
    return {
        "id": job["id"],
        "prompt": job["prompt"],
        "input_text": job["prompt"],
        "voice_type": job.get("voice_type", "preset"),
        "voice": job.get("voice_id", "Phạm Tuyên"),
        "voice_id": job.get("voice_id", "Phạm Tuyên"),
        "sample_rate": 48000,
        "ref_audio_url": job.get("ref_audio_url"),
        "has_prompt_audio": bool(job.get("ref_audio_url")),
        "speed": job.get("speed", 1.0),
        "temperature": job.get("temperature", 0.7),
        "silence_p": job.get("silence_p", 0.15),
        "lease_token": job.get("lease_token")
    }

@router.get("/jobs/{job_id}/prompt-audio")
def get_prompt_audio(
    job_id: str,
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Prompt audio not found")
    
    # Enforce tenant isolation on prompt reference audio access
    if not job.execution_account_id or job.execution_account_id != auth.execution_account_id:
        raise HTTPException(status_code=403, detail="Tác vụ thuộc về tài khoản thực thi khác hoặc thiếu tài khoản sở hữu")

    if not job.ref_audio_path or not os.path.exists(job.ref_audio_path):
        raise HTTPException(status_code=404, detail="Prompt audio not found")

    return FileResponse(job.ref_audio_path, media_type="audio/wav")

MAX_WORKER_AUDIO_BYTES = 50 * 1024 * 1024  # 50 MB cap

@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    worker_id: str = Form(...),
    duration: float = Form(None),
    audio_duration_seconds: float = Form(None),
    sample_rate: int = Form(48000),
    execution_time: float = Form(None),
    processing_time_seconds: float = Form(None),
    lease_token: Optional[str] = Form(None),
    audio_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    dur = duration if duration is not None else (audio_duration_seconds or 0.0)
    exec_t = execution_time if execution_time is not None else (processing_time_seconds or 0.0)

    # Stream read with size enforcement to prevent memory spikes
    content = bytearray()
    while chunk := await audio_file.read(512 * 1024):
        content.extend(chunk)
        if len(content) > MAX_WORKER_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Tệp âm thanh kết quả vượt quá giới hạn tối đa (50MB)")

    job = JobService.complete_job(
        db=db,
        job_id=job_id,
        worker_id=worker_id,
        duration=dur,
        sample_rate=sample_rate,
        execution_time=exec_t,
        audio_bytes=content,
        lease_token=lease_token,
        execution_account_id=auth.execution_account_id
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    add_log(worker_id, f"COMPLETED job {job_id} ({dur:.2f}s audio in {exec_t:.2f}s) [Account: {auth.execution_account_id}]")
    return {"status": "completed", "job_id": job.id, "audio_url": job.audio_url}

@router.post("/jobs/{job_id}/fail")
def fail_job(
    job_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    auth: WorkerAuthContext = Depends(verify_worker_token)
):
    worker_id = payload.get("worker_id", "unknown")
    error_message = payload.get("error_message", "Unknown error")
    lease_token = payload.get("lease_token")
    job = JobService.fail_job(
        db=db,
        job_id=job_id,
        worker_id=worker_id,
        error_message=error_message,
        lease_token=lease_token,
        execution_account_id=auth.execution_account_id
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    add_log(worker_id, f"FAILED job {job_id}: {error_message}")
    return {"status": "failed", "job_id": job.id}
