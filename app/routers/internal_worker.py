from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import WorkerSession, TTSJob
from app.schemas import (
    WorkerRegisterRequest, WorkerHeartbeatRequest,
    WorkerJobFailRequest
)
from app.services.job_service import JobService

router = APIRouter(prefix="/api/worker", tags=["Internal Worker"])

def verify_worker_token(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid token scheme")
    token = parts[1]
    if token != settings.WORKER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid worker token")
    return True

RECENT_WORKER_LOGS = []

def add_log(worker_id: str, message: str):
    now_str = datetime.utcnow().strftime("%H:%M:%S")
    entry = f"[{now_str}] [{worker_id}] {message}"
    RECENT_WORKER_LOGS.append(entry)
    if len(RECENT_WORKER_LOGS) > 100:
        RECENT_WORKER_LOGS.pop(0)

SHUTDOWN_SIGNAL = False

@router.post("/log")
def log_worker_event(payload: dict, _auth: bool = Depends(verify_worker_token)):
    worker_id = payload.get("worker_id", "unknown")
    msg = payload.get("message", "")
    add_log(worker_id, msg)
    return {"status": "ok"}

@router.post("/register")
def register_worker(req: WorkerRegisterRequest, db: Session = Depends(get_db), _auth: bool = Depends(verify_worker_token)):
    global SHUTDOWN_SIGNAL
    SHUTDOWN_SIGNAL = False
    session = db.query(WorkerSession).filter(WorkerSession.worker_id == req.worker_id).first()
    if not session:
        session = WorkerSession(
            worker_id=req.worker_id,
            gpu_index=req.gpu_index,
            gpu_name=req.gpu_name,
            vram_total_mb=req.vram_total_mb,
            status="ready",
            last_heartbeat_at=datetime.utcnow()
        )
        db.add(session)
    else:
        session.gpu_index = req.gpu_index
        session.gpu_name = req.gpu_name
        session.vram_total_mb = req.vram_total_mb
        session.status = "ready"
        session.started_at = datetime.utcnow()
        session.last_heartbeat_at = datetime.utcnow()
    db.commit()
    add_log(req.worker_id, f"REGISTERED on GPU {req.gpu_index} ({req.gpu_name})")
    return {"status": "registered", "worker_id": req.worker_id}

@router.post("/heartbeat")
def worker_heartbeat(req: WorkerHeartbeatRequest, db: Session = Depends(get_db), _auth: bool = Depends(verify_worker_token)):
    session = db.query(WorkerSession).filter(WorkerSession.worker_id == req.worker_id).first()
    if session:
        session.vram_used_mb = req.vram_used_mb
        session.status = "offline" if req.status in ("stopped", "offline") else req.status
        session.current_job_id = req.current_job_id
        session.last_heartbeat_at = datetime.utcnow()
        db.commit()
    return {"status": "ok"}

@router.post("/jobs/pull")
def pull_job(payload: dict = None, db: Session = Depends(get_db), _auth: bool = Depends(verify_worker_token)):
    global SHUTDOWN_SIGNAL
    worker_id = (payload or {}).get("worker_id", "unknown_worker")
    if SHUTDOWN_SIGNAL:
        add_log(worker_id, "Sent SHUTDOWN command to worker")
        return {"action": "shutdown"}

    # Server-Side Auto-Idle Check
    from app.services.settings_service import SettingsService
    idle_min = SettingsService.get_int(db, "worker_idle_timeout_minutes", default=10)
    idle_sec = max(60, idle_min * 60)

    active_jobs = db.query(TTSJob).filter(TTSJob.status.in_(["queued", "booting_kaggle", "processing"])).count()
    if active_jobs == 0:
        sess = db.query(WorkerSession).filter(WorkerSession.worker_id == worker_id).first()
        last_worker_job = db.query(TTSJob).filter(
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

    job = JobService.pull_pending_job(db, worker_id)
    if not job:
        return {}
    
    add_log(worker_id, f"PULLED job {job['id']} ('{job['prompt'][:35]}...')")
    
    # Return both key conventions for maximum worker compatibility
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
        "lease_token": f"lease_{job['id']}"
    }

@router.get("/jobs/{job_id}/prompt-audio")
def get_prompt_audio(job_id: str, db: Session = Depends(get_db), _auth: bool = Depends(verify_worker_token)):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job or not job.ref_audio_path or not os.path.exists(job.ref_audio_path):
        raise HTTPException(status_code=404, detail="Prompt audio not found")
    return FileResponse(job.ref_audio_path, media_type="audio/wav")

@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    worker_id: str = Form(...),
    duration: float = Form(None),
    audio_duration_seconds: float = Form(None),
    sample_rate: int = Form(48000),
    execution_time: float = Form(None),
    processing_time_seconds: float = Form(None),
    audio_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_worker_token)
):
    dur = duration if duration is not None else (audio_duration_seconds or 0.0)
    exec_t = execution_time if execution_time is not None else (processing_time_seconds or 0.0)

    content = await audio_file.read()
    job = JobService.complete_job(
        db=db,
        job_id=job_id,
        worker_id=worker_id,
        duration=dur,
        sample_rate=sample_rate,
        execution_time=exec_t,
        audio_bytes=content
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    add_log(worker_id, f"COMPLETED job {job_id} ({dur:.2f}s audio generated in {exec_t:.2f}s)")
    return {"status": "completed", "job_id": job.id, "audio_url": job.audio_url}

@router.post("/jobs/{job_id}/fail")
def fail_job(job_id: str, payload: dict, db: Session = Depends(get_db), _auth: bool = Depends(verify_worker_token)):
    worker_id = payload.get("worker_id", "unknown")
    error_message = payload.get("error_message", "Unknown error")
    job = JobService.fail_job(
        db=db,
        job_id=job_id,
        worker_id=worker_id,
        error_message=error_message
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    add_log(worker_id, f"FAILED job {job_id}: {error_message}")
    return {"status": "failed", "job_id": job.id}
