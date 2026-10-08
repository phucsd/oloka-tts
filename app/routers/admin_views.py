import os
import glob
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database import get_db
from app.models import User, TTSJob, WorkerSession, AuditLog, VoicePreset, VoiceSample
from app.services.auth_service import AuthService, get_current_admin
from app.services.settings_service import SettingsService
from app.routers.internal_worker import RECENT_WORKER_LOGS
from app.config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["basename"] = os.path.basename

router = APIRouter(prefix="/admin", tags=["Admin Portal Views"])

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_root():
    return RedirectResponse(url="/admin/analytics", status_code=302)

# ==============================================================================
# 1. ANALYTICS & DASHBOARD
# ==============================================================================
@router.get("/analytics", response_class=HTMLResponse)
def admin_analytics_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.is_active == True).count()
    users_with_kaggle = db.query(User).filter(User.kaggle_username.isnot(None), User.kaggle_key.isnot(None)).count()

    total_jobs = db.query(TTSJob).count()
    completed_jobs = db.query(TTSJob).filter(TTSJob.status == "completed").count()
    failed_jobs = db.query(TTSJob).filter(TTSJob.status == "failed").count()
    pending_jobs = db.query(TTSJob).filter(TTSJob.status.in_(["queued", "booting_kaggle", "processing"])).count()

    success_rate = (completed_jobs / total_jobs * 100) if total_jobs > 0 else 100.0

    total_duration_sec = db.query(func.sum(TTSJob.duration)).filter(TTSJob.status == "completed").scalar() or 0.0
    total_audio_hours = total_duration_sec / 3600.0

    # Calculate average RTF for completed jobs
    avg_exec_time = db.query(func.avg(TTSJob.execution_time)).filter(TTSJob.status == "completed").scalar() or 0.0
    avg_duration = db.query(func.avg(TTSJob.duration)).filter(TTSJob.status == "completed").scalar() or 1.0
    avg_rtf = (avg_exec_time / avg_duration) if avg_duration > 0 else 0.5

    top_voices_query = db.query(
        TTSJob.voice_id,
        func.count(TTSJob.id).label("count")
    ).filter(TTSJob.status == "completed").group_by(TTSJob.voice_id).order_by(func.count(TTSJob.id).desc()).limit(5).all()

    cutoff = datetime.utcnow() - timedelta(seconds=90)
    live_workers = db.query(WorkerSession).filter(
        WorkerSession.status.in_(["starting", "ready", "busy"]),
        WorkerSession.last_heartbeat_at >= cutoff
    ).all()

    # Calculate audio storage directory size
    audio_dir = Path("app/static/audio")
    total_storage_mb = 0.0
    if audio_dir.exists():
        total_storage_mb = sum(f.stat().st_size for f in audio_dir.glob("*.wav") if f.is_file()) / (1024 * 1024)

    return templates.TemplateResponse(
        request=request,
        name="admin/analytics.html",
        context={
            "page_title": "Báo Cáo & Thống Kê - OlokaTTS Admin",
            "admin": admin,
            "total_users": total_users,
            "active_users": active_users,
            "users_with_kaggle": users_with_kaggle,
            "total_jobs": total_jobs,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "pending_jobs": pending_jobs,
            "success_rate": f"{success_rate:.1f}",
            "total_duration_minutes": f"{(total_duration_sec / 60):.1f}",
            "total_audio_hours": f"{total_audio_hours:.2f}",
            "avg_rtf": f"{avg_rtf:.2f}",
            "storage_mb": f"{total_storage_mb:.1f}",
            "top_voices": top_voices_query,
            "live_workers": live_workers
        }
    )

# ==============================================================================
# 2. DYNAMIC SYSTEM SETTINGS & CONFIGURATION
# ==============================================================================
@router.get("/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    msg: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    all_settings = SettingsService.get_all_settings(db)
    all_voices = db.query(VoicePreset).filter(VoicePreset.is_enabled == True).all()
    google_redirect_uri = AuthService.get_google_redirect_uri(request, db)

    return templates.TemplateResponse(
        request=request,
        name="admin/settings.html",
        context={
            "page_title": "Cấu Hình Hệ Thống - OlokaTTS Admin",
            "admin": admin,
            "settings": all_settings,
            "all_voices": all_voices,
            "google_redirect_uri": google_redirect_uri,
            "msg": msg
        }
    )

@router.post("/settings")
def update_admin_settings(
    request: Request,
    max_chars_per_job: str = Form("3000"),
    max_concurrency: str = Form("3"),
    worker_idle_timeout_minutes: str = Form("15"),
    allow_public_registration: str = Form("false"),
    allow_guest_demo: str = Form("false"),
    default_voice: str = Form("Hải Đăng"),
    default_speed: str = Form("1.0"),
    default_temp: str = Form("0.7"),
    fallback_to_local_cpu: str = Form("false"),
    master_kaggle_username: str = Form(""),
    master_kaggle_key: str = Form(""),
    mcp_require_auth: str = Form("false"),
    google_client_id: str = Form(""),
    google_client_secret: str = Form(""),
    admin_google_email: str = Form("phucsd@gmail.com"),
    google_redirect_uri: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    updates = {
        "max_chars_per_job": max_chars_per_job.strip(),
        "max_concurrency": max_concurrency.strip(),
        "worker_idle_timeout_minutes": worker_idle_timeout_minutes.strip(),
        "allow_public_registration": "true" if allow_public_registration == "true" else "false",
        "allow_guest_demo": "true" if allow_guest_demo == "true" else "false",
        "default_voice": default_voice.strip(),
        "default_speed": default_speed.strip(),
        "default_temp": default_temp.strip(),
        "fallback_to_local_cpu": "true" if fallback_to_local_cpu == "true" else "false",
        "master_kaggle_username": master_kaggle_username.strip(),
        "master_kaggle_key": master_kaggle_key.strip(),
        "mcp_require_auth": "true" if mcp_require_auth == "true" else "false",
        "google_client_id": google_client_id.strip(),
        "google_client_secret": google_client_secret.strip(),
        "admin_google_email": admin_google_email.strip().lower(),
        "google_redirect_uri": google_redirect_uri.strip()
    }
    SettingsService.update_bulk(db, updates)
    
    # Sync default admin account email if appropriate
    clean_admin_email = admin_google_email.strip().lower()
    if clean_admin_email:
        admin_account = db.query(User).filter(User.username == "admin").first()
        if admin_account and admin_account.email != clean_admin_email:
            existing_target = db.query(User).filter(User.email == clean_admin_email, User.id != admin_account.id).first()
            if not existing_target:
                admin_account.email = clean_admin_email
                db.commit()

    AuthService.log_audit(db, action="admin_update_settings", message=f"Admin {admin.username} đã cập nhật tham số cấu hình hệ thống", user_id=admin.id)
    return RedirectResponse(url="/admin/settings?msg=settings_saved", status_code=302)

# ==============================================================================
# 3. JOB QUEUE & ORCHESTRATION MANAGEMENT
# ==============================================================================
@router.get("/jobs", response_class=HTMLResponse)
def admin_jobs_page(
    request: Request,
    q: str = "",
    status: str = "ALL",
    page: int = 1,
    msg: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    query = db.query(TTSJob)
    if status != "ALL":
        query = query.filter(TTSJob.status == status)
    if q:
        query = query.filter((TTSJob.prompt.ilike(f"%{q}%")) | (TTSJob.id.ilike(f"%{q}%")) | (TTSJob.user_id.ilike(f"%{q}%")))

    total_jobs = query.count()
    jobs = query.order_by(TTSJob.created_at.desc()).offset((page - 1) * 20).limit(20).all()

    # Pre-fetch user dictionary for username mapping
    user_ids = list(set(j.user_id for j in jobs if j.user_id))
    user_map = {}
    if user_ids:
        users = db.query(User).filter(User.id.in_(user_ids)).all()
        user_map = {u.id: u.username for u in users}

    return templates.TemplateResponse(
        request=request,
        name="admin/jobs.html",
        context={
            "page_title": "Quản Lý Hàng Đợi & Job - OlokaTTS Admin",
            "admin": admin,
            "jobs": jobs,
            "user_map": user_map,
            "total_jobs": total_jobs,
            "selected_status": status,
            "q": q,
            "page": page,
            "msg": msg
        }
    )

@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    
    job.status = "queued"
    job.error_message = None
    job.worker_id = None
    job.created_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    db.commit()
    AuthService.log_audit(db, action="admin_retry_job", message=f"Admin {admin.username} đã đưa job #{job.id} trở lại hàng đợi", user_id=admin.id)
    return RedirectResponse(url="/admin/jobs?msg=retry_success", status_code=302)

@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    
    job.status = "failed"
    job.error_message = f"Hủy thủ công bởi quản trị viên {admin.username}"
    db.commit()
    AuthService.log_audit(db, action="admin_cancel_job", message=f"Admin {admin.username} đã hủy job #{job.id}", user_id=admin.id)
    return RedirectResponse(url="/admin/jobs?msg=cancel_success", status_code=302)

@router.post("/jobs/purge-storage")
def purge_audio_storage(
    days: int = Form(7),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    old_jobs = db.query(TTSJob).filter(TTSJob.created_at < cutoff, TTSJob.audio_path.isnot(None)).all()
    deleted_count = 0
    for j in old_jobs:
        if j.audio_path and os.path.exists(j.audio_path):
            try:
                os.remove(j.audio_path)
                deleted_count += 1
            except Exception:
                pass
        j.audio_path = None
        j.audio_url = None
    db.commit()
    AuthService.log_audit(db, action="admin_purge_storage", message=f"Admin {admin.username} đã dọn dẹp {deleted_count} file audio cũ hơn {days} ngày", user_id=admin.id)
    return RedirectResponse(url=f"/admin/jobs?msg=purged_{deleted_count}_files", status_code=302)

@router.post("/queue/flush")
def flush_pending_queue(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    count = db.query(TTSJob).filter(TTSJob.status.in_(["queued", "booting_kaggle"])).update(
        {"status": "failed", "error_message": "Hàng đợi đã bị xóa bởi Quản trị viên"}
    )
    db.commit()
    AuthService.log_audit(db, action="admin_flush_queue", message=f"Admin {admin.username} đã xóa toàn bộ hàng đợi ({count} jobs)", user_id=admin.id)
    return RedirectResponse(url="/admin/jobs?msg=queue_flushed", status_code=302)

# ==============================================================================
# 4. VOICE CATALOG & PRESET MANAGEMENT
# ==============================================================================
@router.get("/voices", response_class=HTMLResponse)
def admin_voices_page(
    request: Request,
    msg: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    presets = db.query(VoicePreset).order_by(VoicePreset.name.asc()).all()
    clone_samples = db.query(VoiceSample).order_by(VoiceSample.created_at.desc()).all()

    return templates.TemplateResponse(
        request=request,
        name="admin/voices.html",
        context={
            "page_title": "Quản Lý Danh Mục Giọng - OlokaTTS Admin",
            "admin": admin,
            "presets": presets,
            "clone_samples": clone_samples,
            "msg": msg
        }
    )

@router.post("/voices/{voice_id}/toggle-pick")
def toggle_voice_editor_pick(
    voice_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    preset = db.query(VoicePreset).filter(or_(VoicePreset.id == voice_id, VoicePreset.voice_id == voice_id)).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Không tìm thấy giọng")
    
    preset.is_editors_pick = not preset.is_editors_pick
    db.commit()
    AuthService.log_audit(db, action="admin_toggle_voice_pick", message=f"Admin {admin.username} đã đổi cờ Nổi Bật cho giọng {preset.name}", user_id=admin.id)
    return RedirectResponse(url="/admin/voices?msg=pick_updated", status_code=302)

@router.post("/voices/{voice_id}/toggle-enable")
def toggle_voice_enable(
    voice_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    preset = db.query(VoicePreset).filter(or_(VoicePreset.id == voice_id, VoicePreset.voice_id == voice_id)).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Không tìm thấy giọng")
    
    preset.is_enabled = not preset.is_enabled
    db.commit()
    AuthService.log_audit(db, action="admin_toggle_voice_enable", message=f"Admin {admin.username} đã {'bật' if preset.is_enabled else 'tắt'} giọng {preset.name}", user_id=admin.id)
    return RedirectResponse(url="/admin/voices?msg=enable_updated", status_code=302)

@router.post("/voices/{voice_id}/update")
def update_voice_metadata(
    voice_id: str,
    name: str = Form(...),
    region: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    preset = db.query(VoicePreset).filter(or_(VoicePreset.id == voice_id, VoicePreset.voice_id == voice_id)).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Không tìm thấy giọng")
    
    preset.name = name.strip()
    preset.region = region.strip()
    preset.description = description.strip()
    db.commit()
    AuthService.log_audit(db, action="admin_update_voice", message=f"Admin {admin.username} đã cập nhật thông tin giọng {preset.name}", user_id=admin.id)
    return RedirectResponse(url="/admin/voices?msg=metadata_updated", status_code=302)

@router.post("/voices/samples/{sample_id}/delete")
def delete_clone_sample(
    sample_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    sample = db.query(VoiceSample).filter(VoiceSample.id == sample_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu clone")
    
    if sample.file_path and os.path.exists(sample.file_path):
        try:
            os.remove(sample.file_path)
        except Exception:
            pass
    db.delete(sample)
    db.commit()
    AuthService.log_audit(db, action="admin_delete_sample", message=f"Admin {admin.username} đã xóa mẫu clone {sample.name}", user_id=admin.id)
    return RedirectResponse(url="/admin/voices?msg=sample_deleted", status_code=302)

# ==============================================================================
# 5. USER MANAGEMENT & PROVISIONING
# ==============================================================================
@router.get("/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    q: str = "",
    page: int = 1,
    msg: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    query = db.query(User)
    if q:
        query = query.filter((User.username.ilike(f"%{q}%")) | (User.email.ilike(f"%{q}%")))
    
    total_users = query.count()
    users = query.order_by(User.created_at.desc()).offset((page - 1) * 20).limit(20).all()

    # Calculate job counts and total audio generated per user
    user_job_stats = {}
    for u in users:
        j_count = db.query(TTSJob).filter(TTSJob.user_id == u.id).count()
        total_sec = db.query(func.sum(TTSJob.duration)).filter(TTSJob.user_id == u.id, TTSJob.status == "completed").scalar() or 0.0
        user_job_stats[u.id] = {
            "job_count": j_count,
            "audio_minutes": f"{(total_sec / 60):.1f}"
        }

    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={
            "page_title": "Quản Lý Người Dùng - OlokaTTS Admin",
            "admin": admin,
            "users": users,
            "user_job_stats": user_job_stats,
            "total_users": total_users,
            "q": q,
            "page": page,
            "msg": msg
        }
    )

@router.post("/users/create")
def admin_create_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    existing = db.query(User).filter((User.username == username.strip()) | (User.email == email.strip().lower())).first()
    if existing:
        return RedirectResponse(url="/admin/users?error=username_or_email_exists", status_code=302)
    
    new_user = User(
        username=username.strip(),
        email=email.strip().lower(),
        hashed_password=AuthService.hash_password(password),
        role="admin" if role == "admin" else "user",
        is_active=True
    )
    db.add(new_user)
    db.commit()
    AuthService.log_audit(db, action="admin_create_user", message=f"Admin {admin.username} đã tạo tài khoản mới: {new_user.username}", user_id=admin.id)
    return RedirectResponse(url="/admin/users?msg=user_created", status_code=302)

@router.post("/users/{user_id}/toggle-active")
def toggle_user_active(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Không thể tự khóa tài khoản của chính mình!")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    
    target.is_active = not target.is_active
    db.commit()
    AuthService.log_audit(db, action="admin_toggle_active", message=f"Admin {admin.username} đã {'mở khóa' if target.is_active else 'khóa'} tài khoản {target.username}", user_id=admin.id)
    return RedirectResponse(url="/admin/users", status_code=302)

@router.post("/users/{user_id}/toggle-role")
def toggle_user_role(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Không thể tự hạ quyền tài khoản của chính mình!")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    
    target.role = "user" if target.role == "admin" else "admin"
    db.commit()
    AuthService.log_audit(db, action="admin_toggle_role", message=f"Admin {admin.username} đã chuyển quyền {target.username} thành {target.role}", user_id=admin.id)
    return RedirectResponse(url="/admin/users", status_code=302)

@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: str,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")
    
    target.hashed_password = AuthService.hash_password(new_password)
    db.commit()
    AuthService.log_audit(db, action="admin_reset_password", message=f"Admin {admin.username} đã đặt lại mật khẩu cho {target.username}", user_id=admin.id)
    return RedirectResponse(url="/admin/users?msg=password_reset_success", status_code=302)

# ==============================================================================
# 6. OBSERVABILITY & AUDIT LOGS
# ==============================================================================
@router.get("/logs", response_class=HTMLResponse)
def admin_logs_page(
    request: Request,
    level: str = "ALL",
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    query = db.query(AuditLog)
    if level != "ALL":
        query = query.filter(AuditLog.level == level)
    
    logs = query.order_by(AuditLog.created_at.desc()).limit(50).all()
    failed_jobs = db.query(TTSJob).filter(TTSJob.status == "failed").order_by(TTSJob.created_at.desc()).limit(20).all()

    return templates.TemplateResponse(
        request=request,
        name="admin/logs.html",
        context={
            "page_title": "Nhật Ký Lỗi & Giám Sát - OlokaTTS Admin",
            "admin": admin,
            "logs": logs,
            "failed_jobs": failed_jobs,
            "worker_logs": RECENT_WORKER_LOGS[-40:],
            "selected_level": level
        }
    )

# ==============================================================================
# 7. API KEYS MANAGEMENT
# ==============================================================================
@router.get("/api-keys", response_class=HTMLResponse)
def admin_api_keys_page(
    request: Request,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    from app.models import ApiKey
    query = db.query(ApiKey, User).join(User, ApiKey.user_id == User.id)
    if q:
        query = query.filter(or_(ApiKey.name.ilike(f"%{q}%"), User.username.ilike(f"%{q}%"), ApiKey.key_prefix.ilike(f"%{q}%")))
    
    key_tuples = query.order_by(ApiKey.created_at.desc()).all()
    
    total_keys = db.query(ApiKey).count()
    active_keys = db.query(ApiKey).filter(ApiKey.is_active == True).count()
    total_requests = db.query(func.sum(ApiKey.total_requests)).scalar() or 0
    total_chars = db.query(func.sum(ApiKey.total_characters)).scalar() or 0

    return templates.TemplateResponse(
        request=request,
        name="admin/api_keys.html",
        context={
            "page_title": "Quản Lý API Keys - OlokaTTS Admin",
            "admin": admin,
            "key_tuples": key_tuples,
            "total_keys": total_keys,
            "active_keys": active_keys,
            "total_requests": total_requests,
            "total_chars": total_chars,
            "search_query": q or ""
        }
    )

@router.post("/api-keys/{key_id}/toggle-status")
def admin_toggle_key_status(
    key_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    from app.models import ApiKey
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Không tìm thấy API Key.")
    
    key.is_active = not key.is_active
    db.commit()
    AuthService.log_audit(db, action="admin_toggle_api_key", level="WARNING", message=f"Admin {admin.username} đã {'kích hoạt' if key.is_active else 'khóa'} key '{key.name}' ({key.key_prefix})")
    return RedirectResponse(url="/admin/api-keys?msg=status_updated", status_code=303)

@router.post("/api-keys/{key_id}/delete")
def admin_delete_key(
    key_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin)
):
    from app.services.api_key_service import ApiKeyService
    ApiKeyService.delete_api_key(db, admin.id, key_id, is_admin=True)
    return RedirectResponse(url="/admin/api-keys?msg=key_deleted", status_code=303)
