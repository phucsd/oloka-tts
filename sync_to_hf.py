import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import huggingface_hub
from huggingface_hub import HfApi

token = os.environ.get("HF_TOKEN") or huggingface_hub.get_token()
if not token and len(sys.argv) > 1:
    token = sys.argv[1]

repo_id = "phucsd/vieneu-gateway"

print(f"Bắt đầu đồng bộ hóa toàn bộ dự án lên Hugging Face Space: {repo_id}...")

api = HfApi(token=token)

commit_info = api.upload_folder(
    folder_path=".",
    repo_id=repo_id,
    repo_type="space",
    commit_message="Fix: In-Process MCP Execution, Auto-Session Inheritance, and Zero-Loss Cloud DB Persistence Guard",
    ignore_patterns=[
        "benchmark_audio_*.wav",
        "tts_job_*.wav",
        "success_job_*.wav",
        "*.pyc",
        "**/__pycache__/**",
        "__pycache__/**",
        "*.db",
        "storage/**",
        "kaggle_backup*/**",
        "kaggle_logs/**",
        "kaggle_pulled/**",
        "patch_*.py",
        "generate_*.py",
        "sync_to_hf.py",
        "benchmark_*.py",
        "test_*.py",
        "vieneu-tts-dual-t4-worker.log",
        ".git/**",
        ".env*"
    ]
)

print("Đã hoàn tất đồng bộ!")
print("Commit URL / Ref:", commit_info)
