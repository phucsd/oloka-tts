import os
import time
from app.config import settings

class LocalEngine:
    _instance = None
    _is_loading = False
    _load_error = None

    @classmethod
    def get_instance(cls):
        if not settings.FALLBACK_TO_LOCAL_CPU:
            return None

        if cls._instance is not None:
            return cls._instance

        if cls._is_loading:
            return None

        cls._is_loading = True
        try:
            print("⏳ [LocalEngine] Loading local VieNeu ONNX CPU model...")
            from vieneu import Vieneu
            cls._instance = Vieneu(backend="onnx")
            print("✅ [LocalEngine] Local CPU engine loaded successfully!")
        except Exception as e:
            cls._load_error = str(e)
            print(f"⚠️ [LocalEngine] Local engine could not be loaded: {e}")
        finally:
            cls._is_loading = False

        return cls._instance

    @classmethod
    def is_available(cls) -> bool:
        if not settings.FALLBACK_TO_LOCAL_CPU:
            return False
        try:
            import vieneu
            return True
        except ImportError:
            return False

    @classmethod
    def infer(cls, prompt: str, voice_type: str = "preset", voice_id: str = "Phạm Tuyên", ref_audio_path: str = None):
        engine = cls.get_instance()
        if not engine:
            raise RuntimeError("Local CPU engine is not available or failed to load")

        from app.services.tts_script_parser import TtsScriptParser
        from app.services.audio_composer import AudioComposer

        parsed = TtsScriptParser.parse(prompt)

        t0 = time.time()
        if not parsed.has_pauses:
            if voice_type == "clone" and ref_audio_path and os.path.exists(ref_audio_path):
                audio = engine.infer(prompt, ref_audio=ref_audio_path)
            else:
                audio = engine.infer(prompt, voice=voice_id)
        else:
            speech_audios = []
            for seg in parsed.get_speech_segments():
                if voice_type == "clone" and ref_audio_path and os.path.exists(ref_audio_path):
                    part_audio = engine.infer(seg.text, ref_audio=ref_audio_path)
                else:
                    part_audio = engine.infer(seg.text, voice=voice_id)
                speech_audios.append(part_audio)

            audio = AudioComposer.compose_from_parsed_script(
                segments=parsed.segments,
                speech_audios=speech_audios,
                sample_rate=getattr(engine, "sample_rate", 48000)
            )

        exec_time = time.time() - t0
        duration = len(audio) / float(getattr(engine, "sample_rate", 48000))
        return audio, duration, exec_time

