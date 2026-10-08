import requests
import time
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import os
BASE_URL = os.environ.get("BASE_URL", "https://tts.oloka.net")

payload = {
    "prompt": "Chào bạn! Tôi là hệ thống VieNeu-TTS v3 Turbo đang chạy trực tiếp trên cụm GPU Tesla T4 x 2 của Kaggle. [cười] Giọng nói nghe rất tự nhiên, truyền cảm và đạt chuẩn chất lượng 48 kilohertz!",
    "voice_id": "Phạm Tuyên",
    "voice_type": "preset",
    "speed": 1.0
}

print("Submitting TTS generation job to Gateway...")
t_start = time.time()
res = requests.post(f"{BASE_URL}/v1/tts/jobs", json=payload, timeout=10)
print("Job creation status:", res.status_code)
job = res.json()
job_id = job["id"]
print(f"Job ID: {job_id}, initial status: {job.get('status')}")

for i in range(40):
    time.sleep(1)
    status_res = requests.get(f"{BASE_URL}/v1/tts/jobs/{job_id}", timeout=10)
    if status_res.status_code == 200:
        data = status_res.json()
        status = data.get("status")
        worker = data.get("worker_id")
        print(f"[{i+1}s] Status: {status}, Worker: {worker}")
        if status == "completed":
            duration = data.get("duration_seconds")
            exec_time = data.get("execution_time_seconds")
            audio_url = f"{BASE_URL}/v1/tts/jobs/{job_id}/audio"
            print(f"🎉 Generation succeeded in {time.time() - t_start:.2f}s!")
            print(f"   - Worker ID: {worker}")
            print(f"   - Audio Duration: {duration:.2f}s")
            print(f"   - Inference Time: {exec_time}s")
            print(f"   - Audio URL: {audio_url}")

            audio_data = requests.get(audio_url, timeout=30).content
            out_file = "test_kaggle_t4_audio.wav"
            with open(out_file, "wb") as f:
                f.write(audio_data)
            print(f"💾 Saved audio file to {out_file} ({len(audio_data)} bytes)")
            break
        elif status == "failed":
            print("Job failed with error:", data.get("error_message"))
            break
