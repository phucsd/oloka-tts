import os
import uuid
import soundfile as sf
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import VoicePreset, VoiceSample, User
from app.schemas import VoicePresetResponse, VoiceSampleResponse
from app.services.auth_service import get_current_user, get_current_user_optional
from typing import Optional

router = APIRouter(prefix="/v1/voices", tags=["Voices"])

MAX_SAMPLE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB cap

@router.get("", response_model=list[VoicePresetResponse])
def list_presets(region: str = None, gender: str = None, editors_pick: bool = None, db: Session = Depends(get_db)):
    query = db.query(VoicePreset)
    if region:
        query = query.filter(VoicePreset.region == region)
    if gender:
        query = query.filter(VoicePreset.gender == gender)
    if editors_pick is not None:
        query = query.filter(VoicePreset.is_editors_pick == editors_pick)
    return query.all()

@router.get("/samples", response_model=list[VoiceSampleResponse])
def list_voice_samples(db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user_optional)):
    if not user:
        return []
    if user.role == "admin":
        return db.query(VoiceSample).order_by(VoiceSample.created_at.desc()).all()
    return db.query(VoiceSample).filter(VoiceSample.user_id == user.id).order_by(VoiceSample.created_at.desc()).all()

@router.post("/samples", response_model=VoiceSampleResponse)
async def upload_voice_sample(
    request: Request,
    name: str = Form(...),
    ref_text: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    # Enforce API Key scope if invoked via API Key
    api_key_obj = getattr(request.state, "api_key", None)
    if api_key_obj:
        granted_scopes = [s.strip() for s in (api_key_obj.scopes or "").split(",")]
        if "full:access" not in granted_scopes and "voices:write" not in granted_scopes:
            raise HTTPException(
                status_code=403,
                detail="API Key không có quyền tải lên mẫu giọng nói (cần scope 'voices:write' hoặc 'full:access')"
            )

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".wav", ".mp3", ".ogg", ".flac", ".m4a"]:
        raise HTTPException(status_code=400, detail="Định dạng âm thanh không hỗ trợ. Hỗ trợ: wav, mp3, ogg, flac, m4a")

    sample_id = f"vs_{uuid.uuid4().hex[:12]}"
    filename = f"{sample_id}{ext}"
    dest_path = os.path.join(settings.SAMPLES_DIR, filename)

    # Stream read with size enforcement to prevent RAM exhaustion and disk flooding
    content = bytearray()
    while chunk := await file.read(512 * 1024):
        content.extend(chunk)
        if len(content) > MAX_SAMPLE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="Tệp âm thanh vượt quá dung lượng cho phép (tối đa 15MB)")

    with open(dest_path, "wb") as f:
        f.write(content)

    duration = 0.0
    try:
        data, sr = sf.read(dest_path)
        duration = len(data) / float(sr)
        if duration > 120.0:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise HTTPException(status_code=400, detail="Thời lượng mẫu giọng quá dài. Tối đa 120 giây.")
    except HTTPException:
        raise
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise HTTPException(status_code=400, detail="Tệp âm thanh tải lên bị hỏng hoặc không đúng chuẩn PCM audio")

    sample = VoiceSample(
        id=sample_id,
        user_id=user.id,
        name=name.strip()[:120],
        file_path=dest_path,
        ref_text=ref_text.strip() if ref_text else None,
        duration=duration
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample

@router.get("/samples/{filename}")
def get_sample_audio(
    filename: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional)
):
    clean_filename = Path(filename).name
    samples_dir = Path(settings.SAMPLES_DIR).resolve()
    target_path = (samples_dir / clean_filename).resolve()

    if not target_path.is_relative_to(samples_dir) or not target_path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp mẫu giọng nói")

    sample = db.query(VoiceSample).filter(VoiceSample.file_path.like(f"%{clean_filename}")).first()
    if sample:
        if not sample.user_id or not user or (sample.user_id != user.id and user.role != "admin"):
            raise HTTPException(status_code=403, detail="Bạn không có quyền nghe hoặc tải mẫu giọng nói này")

    return FileResponse(str(target_path), media_type="audio/wav")

