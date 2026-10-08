import os
import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.config import settings
from app.models import TTSJob, VoicePreset, VoiceSample, WorkerSession, User, KaggleExecutionAccount
from app.services.kaggle_orchestrator import KaggleOrchestrator
from app.services.kaggle_account_service import KaggleAccountService
from app.services.local_engine import LocalEngine

DEFAULT_PRESETS = [
    # 11 Editors' Picks (Featured)
    {"id": "vp_haidang", "name": "Hải Đăng", "voice_id": "Hải Đăng", "gender": "Nam", "region": "Nam", "description": "Trẻ trung, hiện đại, phong cách tự nhiên (Mặc định)", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_haidang.wav"},
    {"id": "vp_adambua", "name": "Adam bựa", "voice_id": "Adam bựa", "gender": "Nam", "region": "Bắc", "description": "Hài hước, dí dỏm, độc đáo", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_adambua.wav"},
    {"id": "vp_trucly", "name": "Trúc Ly", "voice_id": "Trúc Ly", "gender": "Nữ", "region": "Bắc", "description": "Trong trẻo, lôi cuốn, phong cách tự nhiên", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_trucly.wav"},
    {"id": "vp_anhkhoi", "name": "Anh Khôi", "voice_id": "Anh Khôi", "gender": "Nam", "region": "Bắc", "description": "Trầm ấm, truyền cảm, phong cách kể chuyện", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_anhkhoi.wav"},
    {"id": "vp_maianh", "name": "Mai Anh", "voice_id": "Mai Anh", "gender": "Nữ", "region": "Bắc", "description": "Dịu dàng, chuẩn phát thanh, phong cách tin tức", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_maianh.wav"},
    {"id": "vp_minhquan", "name": "Minh Quân Pro", "voice_id": "Minh Quân Pro", "gender": "Nam", "region": "Bắc", "description": "Đĩnh đạc, rõ ràng, phong cách tự nhiên", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_minhquan.wav"},
    {"id": "vp_thuydung", "name": "Thùy Dung", "voice_id": "Thùy Dung", "gender": "Nữ", "region": "Nam", "description": "Thanh thoát, chuyên nghiệp, phong cách tin tức", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_thuydung.wav"},
    {"id": "vp_thientamduc", "name": "Thiền Tâm Đức", "voice_id": "Thiền Tâm Đức", "gender": "Nam", "region": "Bắc", "description": "Thong dong, an nhiên, phong cách kể chuyện", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_thientamduc.wav"},
    {"id": "vp_ngochuyen", "name": "Ngọc Huyền", "voice_id": "Ngọc Huyền", "gender": "Nữ", "region": "Bắc", "description": "Tự nhiên, trong sáng, thân thiện", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_ngochuyen.wav"},
    {"id": "vp_quangson", "name": "Quang Sơn", "voice_id": "Quang Sơn", "gender": "Nam", "region": "Trung", "description": "Đậm đà, chân thực, phong cách tự nhiên", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_quangson.wav"},
    {"id": "vp_ngoctran", "name": "Ngọc Trân", "voice_id": "Ngọc Trân", "gender": "Nữ", "region": "Trung", "description": "Sâu lắng, ấm áp, phong cách tự nhiên", "is_editors_pick": True, "preview_audio_url": "/static/samples/presets/vp_ngoctran.wav"},

    # 14 Standard Voices
    {"id": "vp_minhduc", "name": "Minh Đức", "voice_id": "Minh Đức", "gender": "Nam", "region": "Bắc", "description": "Trang trọng, chuẩn mực, phong cách tin tức", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_minhduc.wav"},
    {"id": "vp_phamtuyen", "name": "Phạm Tuyên", "voice_id": "Phạm Tuyên", "gender": "Nam", "region": "Bắc", "description": "Trầm ấm, phong thái tự nhiên", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_phamtuyen.wav"},
    {"id": "vp_thaison", "name": "Thái Sơn", "voice_id": "Thái Sơn", "gender": "Nam", "region": "Nam", "description": "Cuốn hút, phong cách kể chuyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_thaison.wav"},
    {"id": "vp_xuanvinh", "name": "Xuân Vĩnh", "voice_id": "Xuân Vĩnh", "gender": "Nam", "region": "Bắc", "description": "Mạnh mẽ, phong cách tự nhiên", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_xuanvinh.wav"},
    {"id": "vp_thanhbinh", "name": "Thanh Bình", "voice_id": "Thanh Bình", "gender": "Nam", "region": "Bắc", "description": "Điềm tĩnh, phong cách kể chuyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_thanhbinh.wav"},
    {"id": "vp_ngoclinh", "name": "Ngọc Linh", "voice_id": "Ngọc Linh", "gender": "Nữ", "region": "Bắc", "description": "Nhẹ nhàng, phong cách kể chuyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_ngoclinh.wav"},
    {"id": "vp_doantrang", "name": "Đoan Trang", "voice_id": "Đoan Trang", "gender": "Nữ", "region": "Bắc", "description": "Nữ tính, đằm thắm, phong cách tự nhiên", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_doantrang.wav"},
    {"id": "vp_thucdoan", "name": "Thục Đoan", "voice_id": "Thục Đoan", "gender": "Nữ", "region": "Nam", "description": "Ngọt ngào, phong cách kể chuyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_thucdoan.wav"},
    {"id": "vp_minhtriet", "name": "Minh Triết", "voice_id": "Minh Triết", "gender": "Nam", "region": "Nam", "description": "Sắc bén, phong cách tin tức", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_minhtriet.wav"},
    {"id": "vp_myduyen", "name": "Mỹ Duyên", "voice_id": "Mỹ Duyên", "gender": "Nữ", "region": "Nam", "description": "Êm ái, phong cách đọc truyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_myduyen.wav"},
    {"id": "vp_quynhanh", "name": "Quỳnh Anh", "voice_id": "Quỳnh Anh", "gender": "Nữ", "region": "Bắc", "description": "Truyền cảm, phong cách đọc truyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_quynhanh.wav"},
    {"id": "vp_ductri", "name": "Đức Trí", "voice_id": "Đức Trí", "gender": "Nam", "region": "Nam", "description": "Dày dặn, phong cách đọc truyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_ductri.wav"},
    {"id": "vp_kimthanh", "name": "Kim Thanh", "voice_id": "Kim Thanh", "gender": "Nữ", "region": "Nam", "description": "Ấm cúng, phong cách đọc truyện", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_kimthanh.wav"},
    {"id": "vp_manhdung", "name": "Mạnh Dũng", "voice_id": "Mạnh Dũng", "gender": "Nam", "region": "Bắc", "description": "Hào sảng, phong cách tự nhiên", "is_editors_pick": False, "preview_audio_url": "/static/samples/presets/vp_manhdung.wav"}
]

class JobService:
    @staticmethod
    def seed_presets(db: Session):
        # Clear out obsolete seeds or ensure all 25 current presets exist
        current_voice_ids = {p["voice_id"] for p in DEFAULT_PRESETS}
        db.query(VoicePreset).filter(~VoicePreset.voice_id.in_(current_voice_ids)).delete(synchronize_session=False)

        for p in DEFAULT_PRESETS:
            preset = db.query(VoicePreset).filter(VoicePreset.voice_id == p["voice_id"]).first()
            if not preset:
                preset = VoicePreset(
                    id=p["id"],
                    name=p["name"],
                    voice_id=p["voice_id"],
                    gender=p["gender"],
                    region=p["region"],
                    description=p["description"],
                    is_editors_pick=p["is_editors_pick"],
                    preview_audio_url=p["preview_audio_url"]
                )
                db.add(preset)
            else:
                preset.id = p["id"]
                preset.name = p["name"]
                preset.gender = p["gender"]
                preset.region = p["region"]
                preset.description = p["description"]
                preset.is_editors_pick = p["is_editors_pick"]
                preset.preview_audio_url = p["preview_audio_url"]
        db.commit()

    @staticmethod
    def create_job(db: Session, prompt: str, voice_type: str = "preset", voice_id: str = "Phạm Tuyên",
                   ref_sample_id: str = None, speed: float = 1.0, temperature: float = 0.7,
                   silence_p: float = 0.15, force_local: bool = False, user_id: str = None,
                   execution_account_id: str = None) -> TTSJob:
        
        ref_audio_path = None
        if voice_type == "clone" and ref_sample_id:
            sample = db.query(VoiceSample).filter(VoiceSample.id == ref_sample_id).first()
            if sample:
                ref_audio_path = sample.file_path

        # Resolve execution account if not explicitly passed
        if user_id and not execution_account_id:
            user = db.query(User).filter(User.id == user_id).first()
            acc = KaggleAccountService.get_execution_account_for_user(db, user)
            if acc:
                execution_account_id = acc.id
        elif execution_account_id and not user_id:
            acc = db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == execution_account_id).first()
            if acc:
                user_id = acc.owner_user_id

        job = TTSJob(
            user_id=user_id,
            execution_account_id=execution_account_id,
            prompt=prompt,
            voice_type=voice_type,
            voice_id=voice_id,
            ref_audio_path=ref_audio_path,
            speed=speed,
            temperature=temperature,
            silence_p=silence_p,
            status="queued"
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Check if local execution is forced
        if force_local:
            if not LocalEngine.is_available():
                job.status = "failed"
                job.error_message = "Local CPU engine hiện không khả dụng trên Gateway."
                db.commit()
                return job

            try:
                job.status = "processing"
                job.worker_id = "local_cpu"
                db.commit()

                audio, duration, exec_time = LocalEngine.infer(prompt, voice_type, voice_id, ref_audio_path)
                out_path = os.path.join(settings.AUDIO_DIR, f"{job.id}.wav")
                
                # Save WAV file
                import soundfile as sf
                sf.write(out_path, audio, 48000)

                job.status = "completed"
                job.audio_path = out_path
                job.audio_url = f"/v1/tts/jobs/{job.id}/audio"
                job.duration = duration
                job.execution_time = exec_time
                job.completed_at = datetime.utcnow()
                db.commit()
                return job
            except Exception as e:
                print(f"⚠️ Local engine failed for job {job.id}: {e}")
                job.status = "failed"
                job.error_message = f"Local engine execution error: {e}"
                db.commit()
                return job

        # Multi-Tenant BYOK GPU check:
        # User must have an execution account configured
        if not execution_account_id:
            print(f"⚠️ [JobService] Refusing to process GPU job without execution account for user_id={user_id}")
            job.status = "failed"
            job.error_message = "Bạn chưa cài đặt Kaggle API Key. Vui lòng vào Cài Đặt để cấu hình tài khoản Kaggle của bạn."
            db.commit()
            return job

        # Check if worker is live FOR THIS SPECIFIC EXECUTION ACCOUNT
        is_worker_alive = KaggleOrchestrator.has_live_worker(db, execution_account_id=execution_account_id)
        if not is_worker_alive:
            job.status = "booting_kaggle"
            db.commit()
            KaggleOrchestrator.ensure_worker_running(db, execution_account_id=execution_account_id, user_id=user_id)

        return job

    @staticmethod
    def cleanup_stale_jobs(db: Session, max_age_minutes: int = 15):
        """Marks abandoned queued/booting jobs older than max_age_minutes as failed."""
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
            stale_jobs = db.query(TTSJob).filter(
                TTSJob.status.in_(["queued", "booting_kaggle"]),
                func.coalesce(TTSJob.updated_at, TTSJob.created_at) < cutoff
            ).all()
            if stale_jobs:
                count = len(stale_jobs)
                for sj in stale_jobs:
                    sj.status = "failed"
                    sj.error_message = f"Hết hạn chờ xử lý (Stale timeout > {max_age_minutes}m)"
                db.commit()
                print(f"🧹 [JobService] Cleaned up {count} stale pending jobs older than {max_age_minutes}m.")
        except Exception as e:
            print(f"⚠️ [JobService] Error during stale job cleanup: {e}")

    @staticmethod
    def pull_pending_job(
        db: Session,
        worker_id: str,
        execution_account_id: str,
        owner_user_id: str = None
    ) -> Optional[dict]:
        if not execution_account_id or not execution_account_id.strip():
            return None

        # 1. Clean up abandoned pending jobs older than 15 minutes
        JobService.cleanup_stale_jobs(db, max_age_minutes=15)

        # 2. Reclaim jobs stuck in 'processing' for > 3 minutes (e.g. network timeout or crashed worker)
        proc_cutoff = datetime.utcnow() - timedelta(minutes=3)
        stuck_processing = db.query(TTSJob).filter(
            TTSJob.execution_account_id == execution_account_id,
            TTSJob.status == "processing",
            func.coalesce(TTSJob.updated_at, TTSJob.created_at) < proc_cutoff
        ).all()
        if stuck_processing:
            for sp in stuck_processing:
                retries = 0
                if sp.error_message and "Thử lại tự động" in sp.error_message:
                    try:
                        retries = int(sp.error_message.split("lần ")[1].split(")")[0])
                    except Exception:
                        retries = 1
                
                if retries < 2:
                    sp.status = "queued"
                    sp.worker_id = None
                    sp.lease_token = None
                    sp.lease_expires_at = None
                    sp.error_message = f"Thử lại tự động (lần {retries + 1})"
                    sp.updated_at = datetime.utcnow()
                    print(f"🔄 [JobService] Reclaimed stuck job #{sp.id} for account {execution_account_id}. Re-queued for retry {retries + 1}/2.")
                else:
                    sp.status = "failed"
                    sp.lease_token = None
                    sp.lease_expires_at = None
                    sp.error_message = "Quá thời gian xử lý GPU (Worker processing timeout > 3m sau 2 lần thử lại)"
            db.commit()

        # 3. Pull oldest active pending job strictly belonging to THIS execution account
        active_cutoff = datetime.utcnow() - timedelta(minutes=15)
        candidate = db.query(TTSJob).filter(
            TTSJob.execution_account_id == execution_account_id,
            TTSJob.status.in_(["queued", "booting_kaggle"]),
            func.coalesce(TTSJob.updated_at, TTSJob.created_at) >= active_cutoff
        ).order_by(TTSJob.created_at.asc()).first()

        if not candidate:
            return None

        import secrets
        lease_token = f"lease_{secrets.token_hex(16)}"
        lease_expires_at = datetime.utcnow() + timedelta(minutes=5)

        # Atomic claim: only succeeds if job status is still queued/booting_kaggle
        rows_updated = db.query(TTSJob).filter(
            TTSJob.id == candidate.id,
            TTSJob.status.in_(["queued", "booting_kaggle"])
        ).update({
            "status": "processing",
            "worker_id": worker_id,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "updated_at": datetime.utcnow()
        }, synchronize_session=False)
        db.commit()

        if rows_updated != 1:
            # Another worker for this account claimed it concurrently
            return None

        job = db.query(TTSJob).filter(TTSJob.id == candidate.id).first()

        ref_audio_url = None
        if job.voice_type == "clone" and job.ref_audio_path:
            # Use authenticated worker prompt-audio endpoint
            ref_audio_url = f"{settings.PUBLIC_API_BASE_URL.rstrip('/')}/api/worker/jobs/{job.id}/prompt-audio"

        return {
            "id": job.id,
            "prompt": job.prompt,
            "voice_type": job.voice_type,
            "voice_id": job.voice_id,
            "ref_audio_url": ref_audio_url,
            "speed": job.speed,
            "temperature": job.temperature,
            "silence_p": getattr(job, "silence_p", 0.15) or 0.15,
            "lease_token": lease_token
        }

    @staticmethod
    def complete_job(
        db: Session,
        job_id: str,
        worker_id: str,
        duration: float,
        sample_rate: int,
        execution_time: float,
        audio_bytes: bytes,
        lease_token: Optional[str] = None,
        execution_account_id: Optional[str] = None
    ) -> TTSJob:
        import secrets
        from fastapi import HTTPException
        job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
        if not job:
            return None

        # Enforce fail-closed tenant ownership
        if not job.execution_account_id or not execution_account_id or job.execution_account_id != execution_account_id:
            raise HTTPException(status_code=403, detail="Tác vụ này thuộc về tài khoản thực thi khác hoặc thiếu tài khoản sở hữu")

        # Verify worker lease and ownership
        if job.worker_id and job.worker_id != worker_id:
            raise HTTPException(status_code=403, detail="Worker ID không khớp với worker đang sở hữu tác vụ này")

        if not lease_token or not job.lease_token:
            raise HTTPException(status_code=403, detail="Thiếu lease token cho tác vụ này")

        if not secrets.compare_digest(job.lease_token, lease_token):
            raise HTTPException(status_code=403, detail="Lease token không hợp lệ cho tác vụ này")

        if job.lease_expires_at and job.lease_expires_at < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Lease token của tác vụ này đã hết hạn")

        if job.status != "processing":
            raise HTTPException(status_code=400, detail="Trạng thái tác vụ không hợp lệ (không phải đang xử lý)")

        out_path = os.path.join(settings.AUDIO_DIR, f"{job_id}.wav")
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

        job.status = "completed"
        job.worker_id = worker_id
        job.lease_token = None  # Revoke lease to prevent replay
        job.lease_expires_at = None
        job.audio_path = out_path
        job.audio_url = f"/v1/tts/jobs/{job_id}/audio"
        job.duration = duration
        job.sample_rate = sample_rate
        job.execution_time = execution_time
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=False)
        except Exception:
            pass
        return job

    @staticmethod
    def fail_job(
        db: Session,
        job_id: str,
        worker_id: str,
        error_message: str,
        lease_token: Optional[str] = None,
        execution_account_id: Optional[str] = None
    ) -> TTSJob:
        import secrets
        from fastapi import HTTPException
        job = db.query(TTSJob).filter(TTSJob.id == job_id).first()
        if not job:
            return None

        # Enforce fail-closed tenant ownership
        if not job.execution_account_id or not execution_account_id or job.execution_account_id != execution_account_id:
            raise HTTPException(status_code=403, detail="Tác vụ này thuộc về tài khoản thực thi khác hoặc thiếu tài khoản sở hữu")

        if job.worker_id and job.worker_id != worker_id:
            raise HTTPException(status_code=403, detail="Worker ID không khớp với worker đang sở hữu tác vụ này")

        if not lease_token or not job.lease_token:
            raise HTTPException(status_code=403, detail="Thiếu lease token cho tác vụ này")

        if not secrets.compare_digest(job.lease_token, lease_token):
            raise HTTPException(status_code=403, detail="Lease token không hợp lệ cho tác vụ này")

        if job.lease_expires_at and job.lease_expires_at < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Lease token của tác vụ này đã hết hạn")

        job.status = "failed"
        job.worker_id = worker_id
        job.lease_token = None
        job.lease_expires_at = None
        job.error_message = error_message[:1000]
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
        try:
            from app.services.db_sync_service import DbSyncService
            DbSyncService.backup_database(immediate=False)
        except Exception:
            pass
        return job

