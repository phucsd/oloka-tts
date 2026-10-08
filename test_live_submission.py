import requests
import time
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://phucsd-vieneu-gateway.hf.space"

payload = {
    "prompt": "Xin chào Việt Nam! [cười] Tôi là mô hình VieNeu-TTS v3 Turbo đang xử lý siêu tốc trên GPU Tesla T4 của Kaggle. Âm thanh bốn mươi tám kilohertz cực kỳ sống động!",
    "voice_id": "Phạm Tuyên",
    "voice_type": "preset",
    "speed": 1.0
}

res = requests.post(f"{BASE_URL}/v1/tts/jobs", json=payload, timeout=10)
job = res.json()
job_id = job["id"]
print(f"Created Job: {job_id}, status: {job.get('status')}")

for i in range(35):
    time.sleep(1)
    status_res = requests.get(f"{BASE_URL}/v1/tts/jobs/{job_id}", timeout=10)
    data = status_res.json()
    st = data.get("status")
    wk = data.get("worker_id")
    print(f"[{i+1}s] Status: {st}, Worker: {wk}")
    if st == "completed":
        audio_url = f"{BASE_URL}/v1/tts/jobs/{job_id}/audio"
        print(f"🎉 SUCCESS! Audio URL: {audio_url}")
        print(f"   Duration: {data.get('duration')}s, Exec: {data.get('execution_time')}s")
        # Download audio
        audio_bytes = requests.get(audio_url, timeout=30).content
        out_name = f"success_{job_id}.wav"
        with open(out_name, "wb") as f:
            f.write(audio_bytes)
        print(f"💾 Saved {out_name} ({len(audio_bytes)} bytes)")
        break
    elif st == "failed":
        print(f"❌ Failed: {data.get('error_message')}")
        break
