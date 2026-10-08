"""
VieNeu-TTS Kaggle Dual Tesla T4 Worker Daemon
Autonomous worker connecting to VieNeu Gateway at https://phucsd-vieneu-gateway.hf.space
"""
import os
import sys
import time
import argparse
import subprocess
import traceback
import tempfile

GATEWAY_URL = os.environ.get("PUBLIC_API_BASE_URL", "https://phucsd-vieneu-gateway.hf.space")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "vieneu_secure_worker_token_2026")

def report(worker_name, message):
    import requests
    msg_str = str(message).strip()
    print(f"[{worker_name}] {msg_str}")
    try:
        headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
        requests.post(
            f"{GATEWAY_URL}/api/worker/log",
            json={"worker_id": worker_name, "message": msg_str[:300]},
            headers=headers,
            timeout=5
        )
    except Exception:
        pass

def install_dependencies():
    t_inst_0 = time.time()
    print("[INIT] Fast-Bootstrap: Checking dependencies...")
    try:
        import torch
        print(f"[INIT] Kaggle PyTorch: {torch.__version__}, CUDA available: {torch.cuda.is_available()}")
    except Exception as e:
        print(f"[INIT] PyTorch note: {e}")

    need_install = False
    try:
        import vieneu
        import sea_g2p
        import kaldi_native_fbank
        import soxr
        import onnxruntime
        print("[INIT] All dependencies already satisfied! Skipping pip install.")
    except ImportError as ie:
        print(f"[INIT] Missing package: {ie}. Proceeding with Fast-Bootstrap (17s)...")
        need_install = True

    if need_install:
        print("[INIT] Fast-Bootstrap: Installing vieneu (--no-deps) to preserve Kaggle pre-installed PyTorch...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "vieneu", "--no-deps"
        ])

        print("[INIT] Fast-Bootstrap: Installing lightweight packages (sea-g2p, kaldi-native-fbank, soxr, onnxruntime, requests, soundfile)...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "sea-g2p", "kaldi-native-fbank", "soxr", "onnxruntime", "requests", "soundfile"
        ])
        elapsed = time.time() - t_inst_0
        print(f"[INIT] Fast-Bootstrap finished in {elapsed:.2f}s!")
        report("supervisor", f"Fast-Bootstrap completed in {elapsed:.2f}s!")

    try:
        import torch
        import vieneu
        from vieneu import Vieneu
        print(f"[INIT] SUCCESS! PyTorch {torch.__version__} & Vieneu ready in {time.time() - t_inst_0:.2f}s!")
    except Exception as e:
        print(f"[INIT] Verification warning: {e}")
        traceback.print_exc()

def resolve_voice_for_infer(tts, voice_name):
    try:
        if hasattr(tts, "get_preset_voice"):
            v = tts.get_preset_voice(voice_name)
            if v is not None:
                return v
    except Exception:
        pass
    return voice_name

def run_worker_process(device_id: int, worker_name: str):
    if device_id >= 0:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)

    import requests
    import torch
    from vieneu import Vieneu

    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    cuda_ok = torch.cuda.is_available() and device_id >= 0
    gpu_name = torch.cuda.get_device_name(0) if cuda_ok else "CPU Engine"
    vram_mb = int(torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)) if cuda_ok else 0

    report(worker_name, f"Starting on physical GPU {device_id} ({gpu_name}, {vram_mb} MB VRAM)")

    try:
        reg_payload = {
            "worker_id": worker_name,
            "gpu_index": device_id,
            "gpu_name": gpu_name,
            "vram_total_mb": vram_mb
        }
        resp = requests.post(f"{GATEWAY_URL}/api/worker/register", json=reg_payload, headers=headers, timeout=10)
        report(worker_name, f"Registered with Gateway: HTTP {resp.status_code}")
    except Exception as e:
        report(worker_name, f"Registration warning: {e}")

    tts = None
    report(worker_name, "Loading Vieneu(mode=\'v3turbo\')...")
    t0 = time.time()
    try:
        tts = Vieneu(mode="v3turbo")
        report(worker_name, f"Vieneu(mode=\'v3turbo\') ready in {time.time() - t0:.2f}s!")
    except Exception as e:
        tb = traceback.format_exc()
        report(worker_name, f"v3turbo init error: {e} -> {tb[-200:]}")
        try:
            report(worker_name, "Trying fallback Vieneu()...")
            tts = Vieneu()
            report(worker_name, f"Fallback Vieneu() ready in {time.time() - t0:.2f}s!")
        except Exception as e2:
            tb2 = traceback.format_exc()
            report(worker_name, f"Vieneu() error: {e2} -> {tb2[-200:]}")
            time.sleep(60)
            return

    voices = []
    try:
        voices = tts.list_preset_voices()
        report(worker_name, f"Loaded {len(voices)} voices. Ready!")
    except Exception as e:
        report(worker_name, f"list_preset_voices warning: {e}")

    default_voice = "Minh QuÃ¢n"
    if voices:
        first_item = voices[0]
        if isinstance(first_item, (list, tuple)):
            default_voice = first_item[0]
        elif isinstance(first_item, str):
            default_voice = first_item

    report(worker_name, f"Warming up CUDA graph with \'{default_voice}\'...")
    try:
        v_warm = resolve_voice_for_infer(tts, default_voice)
        try:
            _ = tts.infer("Xin chÃ o Viá»‡t Nam.", voice=v_warm)
        except Exception:
            _ = tts.infer("Xin chÃ o Viá»‡t Nam.")
        report(worker_name, "Warmup completed! Entering poll loop.")
    except Exception as e:
        report(worker_name, f"Warmup warning: {e}")

    last_hb = 0
    poll_interval = 2.5
    tmp_dir = tempfile.gettempdir()
    last_activity = time.time()
    idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "600"))

    while True:
        now = time.time()
        # 1. Auto-shutdown after 10 minutes of inactivity to protect Kaggle GPU quota
        if now - last_activity > idle_timeout:
            report(worker_name, f"Idle for {int(now - last_activity)}s. Auto-stopping to release GPU quota.")
            try:
                requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json={"worker_id": worker_name, "vram_used_mb": 0, "status": "stopped"}, headers=headers, timeout=5)
            except Exception:
                pass
            sys.exit(0)
        now = time.time()
        if now - last_hb > 15:
            try:
                vram_used = int(torch.cuda.memory_allocated(0) / (1024 * 1024)) if cuda_ok else 0
                hb_payload = {
                    "worker_id": worker_name,
                    "vram_used_mb": vram_used,
                    "status": "ready"
                }
                requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json=hb_payload, headers=headers, timeout=5)
                last_hb = now
            except Exception:
                pass

        try:
            pull_resp = requests.post(
                f"{GATEWAY_URL}/api/worker/jobs/pull",
                json={"worker_id": worker_name},
                headers=headers,
                timeout=35
            )

            if pull_resp.status_code == 200:
                job_data = pull_resp.json()
                if job_data:
                    if job_data.get("action") == "shutdown":
                        report(worker_name, "Received SHUTDOWN signal from Gateway. Exiting...")
                        try:
                            requests.post(f"{GATEWAY_URL}/api/worker/heartbeat", json={"worker_id": worker_name, "vram_used_mb": 0, "status": "stopped"}, headers=headers, timeout=5)
                        except Exception:
                            pass
                        sys.exit(0)
                    if "id" in job_data:
                        last_activity = time.time()
                    job_id = job_data["id"]
                    prompt = job_data["prompt"]
                    voice_type = job_data.get("voice_type", "preset")
                    voice_id = job_data.get("voice_id", default_voice)
                    ref_audio_url = job_data.get("ref_audio_url")

                    report(worker_name, f"Processing Job {job_id} ({voice_id})...")
                    job_t0 = time.time()

                    try:
                        ref_local_path = None
                        if voice_type == "clone" and ref_audio_url:
                            ref_local_path = os.path.join(tmp_dir, f"ref_{job_id}.wav")
                            r_audio = requests.get(ref_audio_url, headers=headers, timeout=30)
                            with open(ref_local_path, "wb") as f_ref:
                                f_ref.write(r_audio.content)

                        if ref_local_path:
                            audio = tts.infer(prompt, ref_audio=ref_local_path)
                        else:
                            v_to_use = resolve_voice_for_infer(tts, voice_id)
                            try:
                                audio = tts.infer(prompt, voice=v_to_use)
                            except Exception:
                                audio = tts.infer(prompt, voice=voice_id)

                        out_path = os.path.join(tmp_dir, f"out_{job_id}.wav")
                        tts.save(audio, out_path)
                        exec_time = time.time() - job_t0
                        duration = len(audio) / 48000.0

                        report(worker_name, f"Synthesized {duration:.2f}s audio in {exec_time:.2f}s! Uploading...")

                        with open(out_path, "rb") as f_out:
                            files = {"audio_file": (f"{job_id}.wav", f_out, "audio/wav")}
                            complete_payload = {
                                "job_id": job_id,
                                "worker_id": worker_name,
                                "duration": duration,
                                "sample_rate": 48000,
                                "execution_time": exec_time
                            }
                            up_resp = requests.post(
                                f"{GATEWAY_URL}/api/worker/jobs/{job_id}/complete",
                                data=complete_payload,
                                files=files,
                                headers=headers,
                                timeout=30
                            )
                            report(worker_name, f"Job {job_id} uploaded: HTTP {up_resp.status_code}")
                        last_activity = time.time()

                        if os.path.exists(out_path):
                            os.remove(out_path)
                        if ref_local_path and os.path.exists(ref_local_path):
                            os.remove(ref_local_path)

                    except Exception as infer_err:
                        report(worker_name, f"Job {job_id} ERROR: {infer_err}")
                        fail_payload = {
                            "job_id": job_id,
                            "worker_id": worker_name,
                            "error_message": str(infer_err)
                        }
                        requests.post(
                            f"{GATEWAY_URL}/api/worker/jobs/{job_id}/fail",
                            json=fail_payload,
                            headers=headers,
                            timeout=10
                        )

            elif pull_resp.status_code != 200:
                report(worker_name, f"Pull warning: HTTP {pull_resp.status_code}")

        except requests.exceptions.RequestException as req_err:
            report(worker_name, f"Network error during pull: {req_err}")
        except Exception as e:
            report(worker_name, f"Loop error: {e}")

        time.sleep(poll_interval)

def main():
    install_dependencies()

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

    print(f"[INFO] Total CUDA devices detected: {device_count}")

    workers = []
    if device_count >= 2:
        print("[SUPERVISOR] Dual GPU setup: Launching 2 workers for Tesla T4 x 2!")
        workers.append(("0", "kaggle_worker_t4_0"))
        workers.append(("1", "kaggle_worker_t4_1"))
    elif device_count == 1:
        print("[SUPERVISOR] Single GPU setup: Launching 1 worker for Tesla T4!")
        workers.append(("0", "kaggle_worker_t4_0"))
    else:
        print("[SUPERVISOR] No GPU detected! Running CPU mode worker...")
        workers.append(("-1", "kaggle_worker_cpu"))

    processes = {}
    for dev_id, name in workers:
        cmd = [sys.executable, "-u", "worker.py", "--device", dev_id, "--name", name]
        p = subprocess.Popen(cmd)
        processes[name] = (dev_id, p)
        print(f"[SUPERVISOR] Started worker {name} (PID: {p.pid})")

    while True:
        all_stopped_cleanly = True
        for name, (dev_id, p) in list(processes.items()):
            ret = p.poll()
            if ret is None:
                all_stopped_cleanly = False
            elif ret == 0:
                # Graceful stop from idle timeout or shutdown signal
                pass
            else:
                print(f"[SUPERVISOR] Worker {name} crashed with code {ret}. Restarting in 10s...")
                time.sleep(10)
                cmd = [sys.executable, "-u", "worker.py", "--device", dev_id, "--name", name]
                new_p = subprocess.Popen(cmd)
                processes[name] = (dev_id, new_p)
                all_stopped_cleanly = False
                print(f"[SUPERVISOR] Restarted worker {name} (new PID: {new_p.pid})")

        if all_stopped_cleanly and len(processes) > 0:
            print("[SUPERVISOR] All workers stopped gracefully. Exiting kernel to release GPU quota.")
            sys.exit(0)

        time.sleep(5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--name", type=str, default=None)
    args = parser.parse_args()

    if args.device is not None:
        run_worker_process(args.device, args.name or f"kaggle_worker_{args.device}")
    else:
        main()
