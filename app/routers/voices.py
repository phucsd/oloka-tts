import os
import uuid
import soundfile as sf
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import VoicePreset, VoiceSample
from app.schemas import VoicePresetResponse, VoiceSampleResponse

router = APIRouter(prefix="/v1/voices", tags=["Voices"])

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
def list_voice_samples(db: Session = Depends(get_db)):
    return db.query(VoiceSample).order_by(VoiceSample.created_at.desc()).all()

@router.post("/samples", response_model=VoiceSampleResponse)
async def upload_voice_sample(
    name: str = Form(...),
    ref_text: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".wav", ".mp3", ".ogg", ".flac", ".m4a"]:
        raise HTTPException(status_code=400, detail="Invalid audio format. Supported: wav, mp3, ogg, flac, m4a")

    sample_id = f"vs_{uuid.uuid4().hex[:12]}"
    filename = f"{sample_id}{ext}"
    dest_path = os.path.join(settings.SAMPLES_DIR, filename)

    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    duration = 0.0
    try:
        data, sr = sf.read(dest_path)
        duration = len(data) / float(sr)
    except Exception:
        pass

    sample = VoiceSample(
        id=sample_id,
        name=name,
        file_path=dest_path,
        ref_text=ref_text,
        duration=duration
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample

@router.get("/samples/{filename}")
def get_sample_audio(filename: str):
    file_path = os.path.join(settings.SAMPLES_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Sample audio file not found")
    return FileResponse(file_path, media_type="audio/wav")
