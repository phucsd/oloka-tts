from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import WorkerSession, TTSJob, User
from app.services.kaggle_orchestrator import KaggleOrchestrator
from app.services.local_engine import LocalEngine
from app.services.auth_service import get_current_admin

router = APIRouter(prefix="/api/admin", tags=["Admin & Monitor"])

@router.get("/status")
def get_system_status(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    cutoff = datetime.utcnow() - timedelta(seconds=90)
    live_workers = db.query(WorkerSession).filter(
        WorkerSession.status.in_(["starting", "ready", "busy"]),
        WorkerSession.last_heartbeat_at >= cutoff
    ).all()

    kaggle_configured = KaggleOrchestrator.is_configured()
    kaggle_status = KaggleOrchestrator.get_kernel_status() if kaggle_configured else "NOT_CONFIGURED"

    pending_jobs = db.query(TTSJob).filter(TTSJob.status.in_(["queued", "booting_kaggle", "processing"])).count()
    completed_jobs = db.query(TTSJob).filter(TTSJob.status == "completed").count()

    from app.routers.internal_worker import RECENT_WORKER_LOGS

    return {
        "status": "healthy",
        "live_worker_count": len(live_workers),
        "workers": [
            {
                "worker_id": w.worker_id,
                "gpu_index": w.gpu_index,
                "gpu_name": w.gpu_name,
                "vram_total_mb": w.vram_total_mb,
                "vram_used_mb": w.vram_used_mb,
                "status": w.status,
                "last_heartbeat_seconds_ago": int((datetime.utcnow() - w.last_heartbeat_at).total_seconds()) if w.last_heartbeat_at else None
            }
            for w in live_workers
        ],
        "kaggle": {
            "configured": kaggle_configured,
            "kernel_ref": settings.KAGGLE_KERNEL_REF,
            "kernel_status": kaggle_status
        },
        "local_engine": {
            "available": LocalEngine.is_available(),
            "enabled": settings.FALLBACK_TO_LOCAL_CPU
        },
        "queue": {
            "pending_jobs": pending_jobs,
            "completed_jobs": completed_jobs
        },
        "recent_logs": RECENT_WORKER_LOGS[-30:]
    }

@router.post("/kaggle/push")
def trigger_kaggle_push(force: bool = Query(False), admin: User = Depends(get_current_admin)):
    from app.routers import internal_worker
    internal_worker.SHUTDOWN_SIGNAL = False
    return KaggleOrchestrator.trigger_push(force=force)

@router.post("/kaggle/stop")
def trigger_kaggle_stop(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    from app.routers import internal_worker
    internal_worker.SHUTDOWN_SIGNAL = True
    db.query(WorkerSession).filter(WorkerSession.status.in_(["ready", "busy", "starting"])).update({"status": "stopping"})
    db.commit()
    return {"status": "stopping", "message": "Đã gửi lệnh ngắt kết nối tới các Kaggle GPU Worker để giải phóng GPU."}
