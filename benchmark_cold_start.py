import time
import requests
import json
import os
import subprocess
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

GATEWAY_URL = "https://phucsd-vieneu-gateway.hf.space"
KAGGLE_KERNEL_REF = "phcnguynhukendykerry/vieneu-tts-dual-t4-worker"

def run_benchmark():
    print("=" * 72)
    print("🚀 OLOKATTS FAST-BOOTSTRAP COLD-START BENCHMARK (KAGGLE DUAL TESLA T4)")
    print("=" * 72)
    print("Mục tiêu: Đánh giá tốc độ khởi động lạnh sau khi áp dụng Fast-Bootstrap")
    print("          (0 lần uninstall, giữ nguyên PyTorch 2.10 có sẵn của Kaggle)")
    print("-" * 72)

    # Pre-step: Send stop to any stale workers and let them clear
    print("\n[CHUẨN BỊ] Gửi lệnh dừng worker cũ để đảm bảo môi trường sạch 100%...")
    try:
        requests.post(f"{GATEWAY_URL}/api/admin/kaggle/stop", timeout=5)
    except Exception:
        pass
    time.sleep(3)

    # Step 1: Push new kernel version 14 with Fast-Bootstrap to Kaggle
    print("\n[BƯỚC 1] Bắt đầu benchmark: Đẩy worker (Version 14 - Fast-Bootstrap) lên Kaggle...")
    t_start = time.time()

    env = os.environ.copy()
    env["KAGGLE_API_TOKEN"] = "KGAT_b36826720acb8581235c6e78f725fae7"
    push_res = subprocess.run(
        ["kaggle", "kernels", "push", "-p", "kaggle_worker_remote"],
        env=env,
        capture_output=True,
        text=True
    )
    t_push_done = time.time()
    push_duration = t_push_done - t_start
    print(f"⏱️ Thời gian đẩy code qua Kaggle CLI: {push_duration:.2f}s")
    print(f"   Kết quả Kaggle CLI: {push_res.stdout.strip()}")

    # Step 2: Submit a realistic benchmark TTS job
    benchmark_prompt = "Xin chào! Đây là bài kiểm tra đo lường hiệu năng Fast-Bootstrap của hệ thống OlokaTTS trên Kaggle Dual Tesla T4."
    print(f"\n[BƯỚC 2] Gửi Job TTS mẫu vào Gateway hàng đợi...")
    job_payload = {
        "prompt": benchmark_prompt,
        "voice": "Phạm Tuyên",
        "speed": 1.0
    }
    t_job_submit = time.time()
    job_res = requests.post(f"{GATEWAY_URL}/v1/tts/jobs", json=job_payload).json()
    job_id = job_res.get("id")
    print(f"   Job ID đã tạo: {job_id} (Status ban đầu: {job_res.get('status')})")

    # Step 3: Detailed Phase Tracking
    print(f"\n[BƯỚC 3] Theo dõi tiến trình khởi động từng giai đoạn...")
    
    t_vm_running = None
    t_packages_installed = None
    t_model_loaded = None
    t_job_picked = None
    t_job_done = None

    last_status_print = 0
    seen_logs = set()

    while time.time() - t_start < 300: # Max 5 minutes timeout
        elapsed = time.time() - t_start

        # Check Kaggle Kernel status
        if not t_vm_running:
            try:
                res_st = subprocess.run(
                    ["kaggle", "kernels", "status", KAGGLE_KERNEL_REF],
                    env=env, capture_output=True, text=True
                )
                k_status = res_st.stdout.strip()
                if "RUNNING" in k_status:
                    t_vm_running = time.time()
                    print(f"   ⚡ [{elapsed:.1f}s] Kaggle đã cấp phát GPU VM & bắt đầu RUNNING!")
            except Exception:
                pass

        # Check Gateway Admin Logs for exact Worker Milestones
        try:
            admin_data = requests.get(f"{GATEWAY_URL}/api/admin/status", timeout=5).json()
            recent_logs = admin_data.get("recent_logs", [])
            
            for log in recent_logs:
                if log not in seen_logs:
                    seen_logs.add(log)
                    print(f"      📋 [{elapsed:.1f}s Log] {log}")

                # Check for fast bootstrap finish in logs
                if "Fast-Bootstrap finished" in log and not t_packages_installed:
                    t_packages_installed = time.time()
                    print(f"   📦 [{elapsed:.1f}s] Fast-Bootstrap hoàn tất cài 4 thư viện siêu nhẹ!")

                if "Vieneu(mode='v3turbo') ready" in log and not t_model_loaded:
                    t_model_loaded = time.time()
                    print(f"   🎉 [{elapsed:.1f}s] Vieneu v3 Turbo đã nạp xong vào VRAM 2x Tesla T4!")

        except Exception:
            pass

        # Check Job Status
        try:
            j_data = requests.get(f"{GATEWAY_URL}/v1/tts/jobs/{job_id}", timeout=5).json()
            j_status = j_data.get("status")

            if j_status == "processing" and not t_job_picked:
                t_job_picked = time.time()
                print(f"   🎙️ [{elapsed:.1f}s] Worker đã kéo Job {job_id} và đang thực hiện TTS Inference...")

            if j_status == "completed":
                t_job_done = time.time()
                print(f"   ✅ [{elapsed:.1f}s] JOB HOÀN THÀNH!")
                print(f"      Audio URL: {j_data.get('audio_url')}")
                print(f"      Thời lượng âm thanh: {j_data.get('duration')}s")
                print(f"      Worker xử lý: {j_data.get('worker_id')}")
                print(f"      Thời gian sinh âm thanh (Inference): {j_data.get('execution_time'):.2f}s")
                break
        except Exception:
            pass

        # Periodic log heartbeat
        if elapsed - last_status_print > 8:
            print(f"   ... [{elapsed:.1f}s] Đang theo dõi tiến trình...")
            last_status_print = elapsed

        time.sleep(1.5)

    # Step 4: Output Comprehensive Benchmark Report
    print("\n" + "=" * 72)
    print("📊 BÁO CÁO KẾT QUẢ BENCHMARK: HIỆU NĂNG FAST-BOOTSTRAP (COLD-START)")
    print("=" * 72)

    total_cold_start = t_job_done - t_start if t_job_done else 0
    vm_prep_time = (t_vm_running - t_start) if t_vm_running else 0
    install_and_load_time = (t_job_picked - t_vm_running) if (t_job_picked and t_vm_running) else 0
    infer_time = (t_job_done - t_job_picked) if (t_job_done and t_job_picked) else 0

    print(f"1. Thời gian đẩy & tiếp nhận lệnh (Push CLI):           {push_duration:.2f} giây")
    print(f"2. Thời gian Kaggle xếp hàng & Cấp phát VM (Queue):      {vm_prep_time:.2f} giây")
    print(f"3. Thời gian cài 4 thư viện nhẹ & Nạp model vào VRAM:    {install_and_load_time:.2f} giây")
    print(f"4. Thời gian tổng hợp âm thanh (TTS Inference):          {infer_time:.2f} giây")
    print("-" * 72)
    print(f"🔥 TỔNG THỜI GIAN COLD-START TOÀN TRÌNH:                  {total_cold_start:.2f} giây (~{total_cold_start/60:.2f} phút)")
    print("=" * 72)

if __name__ == "__main__":
    run_benchmark()
