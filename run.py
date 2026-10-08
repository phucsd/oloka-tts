import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import uvicorn
from app.config import settings

def main():
    print("=" * 65)
    print("🎙️  VieNeu-TTS Gateway & Web Studio (48kHz)")
    print("⚡ Powered by Kaggle Dual Tesla T4 x 2 & FastAPI Fullstack")
    print(f"🌐 Server Address: http://{settings.HOST}:{settings.PORT}")
    print(f"📖 Swagger Docs:   http://{settings.HOST}:{settings.PORT}/docs")
    print("=" * 65)
    
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )

if __name__ == "__main__":
    main()
