"""
VieNeu-TTS Kaggle Dual Tesla T4 Worker
Autonomous polling worker that connects to VieNeu Gateway.
"""
import os
import sys
import time
import subprocess
import threading
import multiprocessing
import traceback

def install_dependencies():
    print("⏳ [Init] Fast-Bootstrap: Checking dependencies...")
    try:
        import torch
        print(f"✅ PyTorch version: {torch.__version__}, CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"✅ CUDA Device Count: {torch.cuda.device_count()}")
            for i in range(torch.cuda.device_count()):
                print(f"   - GPU {i}: {torch.cuda.get_device_name(i)}")
    except Exception as e:
        print(f"⚠️ PyTorch note: {e}")

    need_install = False
    try:
        import vieneu
        import sea_g2p
        import kaldi_native_fbank
        import soxr
        import onnxruntime
        print("✅ All dependencies already satisfied! Skipping pip install.")
    except ImportError as ie:
        print(f"⏳ Missing package: {ie}. Proceeding with Fast-Bootstrap (17s)...")
        need_install = True

    if need_install:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "vieneu", "--no-deps"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", "sea-g2p", "kaldi-native-fbank", "soxr", "onnxruntime", "requests", "soundfile"])
        print("✅ Fast-Bootstrap installed successfully!")

GATEWAY_URL = os.environ.get("PUBLIC_API_BASE_URL", "https://tts.oloka.net")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "vieneu_secure_worker_token_2026")
WORKER_PREFIX = os.environ.get("WORKER_PREFIX", "kaggle_worker")

def run_worker_process(device_id: int, worker_name: str):
    import requests
    import torch
    from vieneu import Vieneu

    os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
    print(f"🚀 [{worker_name}] Starting Worker Process on Physical GPU {device_id}...")

    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}

    # Register worker with Gateway
    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "Unknown GPU"
    vram_mb = int(torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)) if torch.cuda.is_available() else 15360

    try:
        reg_payload = {
            "worker_id": worker_name,
            "gpu_index": device_id,
            "gpu_name": gpu_name,
            "vram_total_mb": vram_mb
        }
        resp = requests.post(f"{GATEWAY_URL}/api/worker/register", json=reg_payload, headers=headers, timeout=10)
        print(f"✅ [{worker_name}] Registered with gateway: {resp.status_code}")
    except Exception as e:
        print(f"⚠️ [{worker_name}] Could not register with gateway: {e}")

    print(f"⏳ [{worker_name}] Loading VieNeu-TTS v3 Turbo model...")
    t0 = time.time()
    tts = Vieneu(mode="v3turbo")
    
    # Warmup CUDA graph
    print(f"🔥 [{worker_name}] Warming up CUDA graph...")
    _ = tts.infer("Xin chào Việt Nam.", voice="Phạm Tuyên")
    print(f"✅ [{worker_name}] Model ready in {time.time() - t0:.2f}s!")

    last_hb = 0
    poll_interval = 1.0
    last_activity = time.time()
    # Auto-stop after 10 minutes (600s) of inactivity to protect Kaggle GPU quota
    idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "600"))

    while True:
        now = time.time()

        # 1. Client-Side Idle Timeout Check
        if now - last_activity > idle_timeout:
            print(f"💤 [{worker_name}] Idle for {int(now - last_activity)}s (limit: {idle_timeout}s). Gracefully stopping to save GPU quota...")
            try:
                hb_payload = {
                    "worker_id": worker_name,
                    "vram_used_mb": 0,
                    "status": "stopped"
                }
                requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json=hb_payload, headers=headers, timeout=5)
            except Exception:
                pass
            sys.exit(0)

        # 2. Send heartbeat every 15 seconds
        if now - last_hb > 15:
            try:
                vram_used = int(torch.cuda.memory_allocated(0) / (1024 * 1024)) if torch.cuda.is_available() else 0
                hb_payload = {
                    "worker_id": worker_name,
                    "vram_used_mb": vram_used,
                    "status": "ready"
                }
                requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json=hb_payload, headers=headers, timeout=5)
                last_hb = now
            except Exception as e:
                print(f"⚠️ [{worker_name}] Heartbeat error: {e}")

        # 3. Pull job from gateway
        try:
            pull_resp = requests.post(
                f"{GATEWAY_URL}/api/worker/jobs/pull",
                json={"worker_id": worker_name},
                headers=headers,
                timeout=10
            )

            if pull_resp.status_code == 200:
                job_data = pull_resp.json()
                if job_data:
                    # Check for explicit shutdown command from Gateway
                    if job_data.get("action") == "shutdown":
                        print(f"🛑 [{worker_name}] Received SHUTDOWN signal from Gateway. Stopping worker...")
                        try:
                            hb_payload = {
                                "worker_id": worker_name,
                                "vram_used_mb": 0,
                                "status": "stopped"
                            }
                            requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json=hb_payload, headers=headers, timeout=5)
                        except Exception:
                            pass
                        sys.exit(0)

                    if "id" in job_data:
                        last_activity = time.time()  # Reset idle timer upon receiving job
                        job_id = job_data["id"]
                    prompt = job_data["prompt"]
                    voice_type = job_data.get("voice_type", "preset")
                    voice_id = job_data.get("voice_id", "Phạm Tuyên")
                    ref_audio_url = job_data.get("ref_audio_url")
                    lease_token = job_data.get("lease_token", "")
                    
                    print(f"⚡ [{worker_name}] Processing Job {job_id} (Type: {voice_type}, Voice: {voice_id})")
                    job_t0 = time.time()
                    
                    try:
                        ref_local_path = None
                        if voice_type == "clone" and ref_audio_url:
                            # Download reference sample
                            ref_local_path = f"/tmp/ref_{job_id}.wav"
                            r_audio = requests.get(ref_audio_url, headers=headers, timeout=30)
                            with open(ref_local_path, "wb") as f_ref:
                                f_ref.write(r_audio.content)

                        infer_kwargs = {}
                        if "temperature" in job_data:
                            try:
                                infer_kwargs["temperature"] = float(job_data["temperature"])
                            except Exception:
                                pass
                        if "silence_p" in job_data:
                            try:
                                infer_kwargs["silence_p"] = float(job_data["silence_p"])
                            except Exception:
                                pass

                        # Inference
                        try:
                            if ref_local_path:
                                audio = tts.infer(prompt, ref_audio=ref_local_path, **infer_kwargs)
                            else:
                                audio = tts.infer(prompt, voice=voice_id, **infer_kwargs)
                        except TypeError:
                            if ref_local_path:
                                audio = tts.infer(prompt, ref_audio=ref_local_path)
                            else:
                                audio = tts.infer(prompt, voice=voice_id)

                        out_path = f"/tmp/out_{job_id}.wav"
                        tts.save(audio, out_path)
                        exec_time = time.time() - job_t0
                        duration = len(audio) / 48000.0

                        print(f"✅ [{worker_name}] Job {job_id} finished in {exec_time:.2f}s (audio duration: {duration:.2f}s)")

                        # Upload result
                        with open(out_path, "rb") as f_out:
                            files = {"audio_file": (f"{job_id}.wav", f_out, "audio/wav")}
                            complete_payload = {
                                "job_id": job_id,
                                "worker_id": worker_name,
                                "duration": duration,
                                "sample_rate": 48000,
                                "execution_time": exec_time,
                                "lease_token": lease_token
                            }
                            requests.post(
                                f"{GATEWAY_URL}/api/worker/jobs/{job_id}/complete",
                                data=complete_payload,
                                files=files,
                                headers=headers,
                                timeout=30
                            )

                        # Cleanup temp files
                        if os.path.exists(out_path): os.remove(out_path)
                        if ref_local_path and os.path.exists(ref_local_path): os.remove(ref_local_path)

                    except Exception as infer_err:
                        err_msg = traceback.format_exc()
                        print(f"❌ [{worker_name}] Job {job_id} failed: {err_msg}")
                        fail_payload = {
                            "job_id": job_id,
                            "worker_id": worker_name,
                            "error_message": str(infer_err),
                            "lease_token": lease_token
                        }
                        requests.post(
                            f"{GATEWAY_URL}/api/worker/jobs/{job_id}/fail",
                            json=fail_payload,
                            headers=headers,
                            timeout=10
                        )

        except requests.exceptions.RequestException as req_err:
            pass
        except Exception as e:
            print(f"⚠️ [{worker_name}] Loop error: {e}")

        time.sleep(poll_interval)

def main():
    install_dependencies()

    current_script = (
        os.path.abspath(__file__)
        if "__file__" in globals() and os.path.exists(__file__)
        else (
            os.path.abspath(sys.argv[0])
            if sys.argv and os.path.exists(sys.argv[0])
            else os.path.abspath("worker.py")
        )
    )
    try:
        worker_target = os.path.abspath("worker.py")
        if os.path.exists(current_script) and worker_target != current_script:
            import shutil
            shutil.copyfile(current_script, worker_target)
    except Exception as e_cp:
        print(f"⚠️ [Init] Note creating worker.py alias: {e_cp}")

    device_count = 0
    try:
        smi_out = subprocess.check_output(["nvidia-smi", "-L"]).decode()
        device_count = len([line for line in smi_out.strip().split("\n") if "GPU" in line])
    except Exception:
        try:
            import torch
            device_count = torch.cuda.device_count() if torch.cuda.is_available() else 0
        except Exception:
            device_count = 0

    print(f"🎯 Total CUDA devices detected: {device_count}")

    workers = []
    if device_count >= 2:
        print(f"⚡ Dual GPU setup: Launching 2 workers for Tesla T4 x 2 (Prefix: {WORKER_PREFIX})")
        workers.append(("0", f"{WORKER_PREFIX}_t4_0"))
        workers.append(("1", f"{WORKER_PREFIX}_t4_1"))
    elif device_count == 1:
        print(f"⚡ Single GPU setup: Launching 1 worker for Tesla T4 (Prefix: {WORKER_PREFIX})")
        workers.append(("0", f"{WORKER_PREFIX}_t4_0"))
    else:
        print(f"⚠️ No GPU detected! Running CPU mode worker (Prefix: {WORKER_PREFIX})")
        workers.append(("-1", f"{WORKER_PREFIX}_cpu"))

    processes = {}
    for dev_id, name in workers:
        cmd = [sys.executable, "-u", current_script, "--device", dev_id, "--name", name]
        p = subprocess.Popen(cmd)
        processes[name] = (dev_id, p)
        print(f"Started worker {name} (PID: {p.pid})")

    while True:
        all_stopped_cleanly = True
        for name, (dev_id, p) in list(processes.items()):
            ret = p.poll()
            if ret is None:
                all_stopped_cleanly = False
            elif ret == 0:
                # Normal graceful exit from idle timeout or shutdown signal
                pass
            else:
                # Unexpected crash: restart worker
                print(f"⚠️ Worker {name} crashed with code {ret}. Restarting...")
                cmd = [sys.executable, "-u", current_script, "--device", dev_id, "--name", name]
                new_p = subprocess.Popen(cmd)
                processes[name] = (dev_id, new_p)
                all_stopped_cleanly = False
                print(f"Restarted worker {name} (new PID: {new_p.pid})")

        if all_stopped_cleanly and len(processes) > 0:
            print("🏁 All workers stopped gracefully. Exiting kernel to release Kaggle GPU quota.")
            sys.exit(0)

        time.sleep(5)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--name", type=str, default=None)
    args = parser.parse_args()

    if args.device is not None:
        run_worker_process(args.device, args.name or f"{WORKER_PREFIX}_{args.device}")
    else:
        main()
