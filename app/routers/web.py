import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional
from app.database import get_db
from app.models import VoicePreset, VoiceSample, TTSJob, User
from app.services.job_service import JobService
from app.services.local_engine import LocalEngine
from app.services.auth_service import get_current_user_optional
from app.config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["basename"] = os.path.basename

router = APIRouter(include_in_schema=False)

@router.get("/")
def studio_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user_optional)):
    presets = db.query(VoicePreset).all()
    samples = []
    recent_jobs = []
    
    if user:
        samples = db.query(VoiceSample).all()
        if user.role == "admin":
            recent_jobs = db.query(TTSJob).order_by(TTSJob.created_at.desc()).limit(10).all()
        else:
            recent_jobs = db.query(TTSJob).filter(
                TTSJob.user_id == user.id
            ).order_by(TTSJob.created_at.desc()).limit(10).all()

    kaggle_configured = bool(user and user.kaggle_username and user.kaggle_key)
    local_cpu_available = LocalEngine.is_available()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "presets": presets,
            "samples": samples,
            "recent_jobs": recent_jobs,
            "page_title": "OlokaTTS Studio",
            "user": user,
            "kaggle_configured": kaggle_configured,
            "local_cpu_available": local_cpu_available
        }
    )

@router.get("/voices")
def voices_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user_optional)):
    presets = db.query(VoicePreset).all()
    samples = db.query(VoiceSample).order_by(VoiceSample.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="voices.html",
        context={
            "presets": presets,
            "samples": samples,
            "page_title": "Thư Viện Giọng & Voice Clone - OlokaTTS",
            "user": user
        }
    )

@router.get("/conversation")
def conversation_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user_optional)):
    presets = db.query(VoicePreset).all()
    return templates.TemplateResponse(
        request=request,
        name="conversation.html",
        context={
            "presets": presets,
            "page_title": "Hội Thoại Đa Nhân Vật - OlokaTTS",
            "user": user
        }
    )

@router.get("/monitor")
def monitor_page(request: Request, user: Optional[User] = Depends(get_current_user_optional)):
    if not user:
        return RedirectResponse(url="/login?next=/monitor", status_code=303)
    if user.role != "admin":
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="monitor.html",
        context={
            "page_title": "Giám Sát Worker - OlokaTTS",
            "user": user
        }
    )

@router.get("/docs/mcp")
def mcp_docs_page(request: Request, user: Optional[User] = Depends(get_current_user_optional)):
    return templates.TemplateResponse(
        request=request,
        name="mcp_docs.html",
        context={
            "page_title": "Tài Liệu Kết Nối MCP & ChatGPT - OlokaTTS",
            "user": user,
            "gateway_url": settings.PUBLIC_API_BASE_URL.rstrip("/")
        }
    )

@router.get("/docs/api")
def api_docs_page(request: Request, user: Optional[User] = Depends(get_current_user_optional)):
    return templates.TemplateResponse(
        request=request,
        name="api_docs.html",
        context={
            "page_title": "API Documentation & Reference - OlokaTTS 48kHz",
            "user": user,
            "gateway_url": settings.PUBLIC_API_BASE_URL.rstrip("/")
        }
    )
