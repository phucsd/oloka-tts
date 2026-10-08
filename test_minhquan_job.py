import requests
import time
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import os
BASE_URL = os.environ.get("BASE_URL", "https://tts.oloka.net")

payload = {
    "prompt": "Xin chào bạn! Đây là bài kiểm tra giọng nói tiếng Việt thời gian thực với mô hình VieNeu-TTS v3 Turbo. Âm thanh bốn mươi tám kilohertz siêu mượt!",
    "voice_id": "Minh Quân",
    "voice_type": "preset",
    "speed": 1.0
}

print("Submitting job...")
res = requests.post(f"{BASE_URL}/v1/tts/jobs", json=payload, timeout=10)
job = res.json()
job_id = job["id"]
print(f"Created Job {job_id}, status: {job.get('status')}")

for i in range(35):
    time.sleep(1)
    status_res = requests.get(f"{BASE_URL}/v1/tts/jobs/{job_id}", timeout=10)
    data = status_res.json()
    st = data.get("status")
    wk = data.get("worker_id")
    print(f"[{i+1:02d}s] Status: {st}, Worker: {wk}")
    
    if st == "completed":
        audio_url = f"{BASE_URL}/v1/tts/jobs/{job_id}/audio"
        print(f"\n🎉 JOB COMPLETED SUCCESSFULLY!")
        print(f"   Worker: {wk}")
        print(f"   Duration: {data.get('duration')}s")
        print(f"   Execution Time: {data.get('execution_time')}s")
        print(f"   Audio URL: {audio_url}")
        
        # Download audio
        audio = requests.get(audio_url, timeout=30).content
        out_name = f"tts_{job_id}.wav"
        with open(out_name, "wb") as f:
            f.write(audio)
        print(f"💾 Audio saved to {out_name} ({len(audio)} bytes)")
        break
    elif st == "failed":
        print(f"\n❌ Job failed: {data.get('error_message')}")
        break
