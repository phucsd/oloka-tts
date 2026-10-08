from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

class TTSJobCreate(BaseModel):
    prompt: str = Field(..., min_length=1, description="Nội dung văn bản cần đọc")
    voice_type: str = Field("preset", description="'preset' hoặc 'clone'")
    voice_id: Optional[str] = Field(None, description="Tên giọng preset hoặc ID mẫu clone")
    voice: Optional[str] = Field(None, description="Alias cho voice_id")
    ref_sample_id: Optional[str] = Field(None, description="ID mẫu giọng nếu là clone")
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Tốc độ đọc")
    temperature: float = Field(0.7, ge=0.1, le=1.5, description="Độ biến thiên cảm xúc")
    silence_p: float = Field(0.15, ge=0.0, le=1.0, description="Thời gian ngắt nghỉ giữa các câu")

    @property
    def resolved_voice_id(self) -> str:
        return self.voice_id or self.voice or "Hải Đăng"

class TTSJobResponse(BaseModel):
    id: str
    prompt: str
    voice_type: str
    voice_id: str
    status: str
    error_message: Optional[str] = None
    audio_url: Optional[str] = None
    duration: Optional[float] = None
    sample_rate: int = 48000
    worker_id: Optional[str] = None
    execution_time: Optional[float] = None
    speed: Optional[float] = 1.0
    temperature: Optional[float] = 0.7
    silence_p: Optional[float] = 0.15
    created_at: datetime

    class Config:
        from_attributes = True

class VoicePresetResponse(BaseModel):
    id: str
    name: str
    voice_id: str
    gender: str
    region: str
    description: Optional[str] = None
    is_editors_pick: bool = False
    preview_audio_url: Optional[str] = None

    class Config:
        from_attributes = True

class VoiceSampleResponse(BaseModel):
    id: str
    name: str
    duration: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True

# Worker schemas
class WorkerRegisterRequest(BaseModel):
    worker_id: str
    gpu_index: int = 0
    gpu_name: str = "Tesla T4"
    vram_total_mb: int = 15360

class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    vram_used_mb: int = 0
    status: str = "ready"
    current_job_id: Optional[str] = None

class WorkerJobCompleteRequest(BaseModel):
    job_id: str
    worker_id: str
    duration: float
    sample_rate: int = 48000
    execution_time: float

class WorkerJobFailRequest(BaseModel):
    job_id: str
    worker_id: str
    error_message: str

class OpenAISpeechRequest(BaseModel):
    model: str = "olokatts-v3-turbo"
    input: str
    voice: str = "Phạm Tuyên"
    response_format: str = "wav"
    speed: float = 1.0
