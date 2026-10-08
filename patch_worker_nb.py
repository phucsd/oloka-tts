import json

nb_path = 'kaggle_worker_remote/vieneu-tts-dual-t4-worker.ipynb'
with open(nb_path, 'r', encoding='utf-8') as f:
    nb = json.load(f)

cell_code = ''.join(nb['cells'][1]['source'])

# 1. Add idle shutdown variables and checks
old_hb = 'last_hb = 0\n    poll_interval = 2.5'
new_hb = 'last_hb = 0\n    last_job_time = time.time()\n    IDLE_TIMEOUT_SECONDS = 900  # 15 minutes\n    poll_interval = 2.5'
assert old_hb in cell_code, 'old_hb not found'
cell_code = cell_code.replace(old_hb, new_hb)

old_loop_start = 'while True:\n        now = time.time()'
new_loop_start = '''while True:
        now = time.time()
        # 15-minute idle auto-shutdown to save Kaggle GPU quota
        if now - last_job_time > IDLE_TIMEOUT_SECONDS:
            report(worker_name, f"[IDLE_SHUTDOWN] No jobs received for 15 minutes. Exiting worker to save GPU quota.")
            sys.exit(0)'''
assert old_loop_start in cell_code, 'old_loop_start not found'
cell_code = cell_code.replace(old_loop_start, new_loop_start)

# 2. Add remote shutdown check and last_job_time reset
old_pull_check = '''            if pull_resp.status_code == 200:
                job_data = pull_resp.json()
                if job_data and "id" in job_data:'''
new_pull_check = '''            if pull_resp.status_code == 200:
                job_data = pull_resp.json()
                if job_data.get("action") == "shutdown":
                    report(worker_name, "[SHUTDOWN] Remote shutdown command received from Gateway. Exiting cleanly.")
                    sys.exit(0)
                if job_data and "id" in job_data:
                    last_job_time = time.time()  # Reset idle timer'''
assert old_pull_check in cell_code, 'old_pull_check not found'
cell_code = cell_code.replace(old_pull_check, new_pull_check)

# 3. Update supervisor clean exit logic
old_supervisor_loop = '''    while True:
        for name, (dev_id, p) in list(processes.items()):
            ret = p.poll()
            if ret is not None:
                print(f"[SUPERVISOR] Worker {name} exited with code {ret}. Restarting in 10s...")
                time.sleep(10)
                cmd = [sys.executable, "-u", "worker.py", "--device", dev_id, "--name", name]
                new_p = subprocess.Popen(cmd)
                processes[name] = (dev_id, new_p)
                print(f"[SUPERVISOR] Restarted worker {name} (new PID: {new_p.pid})")
        time.sleep(5)'''

new_supervisor_loop = '''    while True:
        all_clean_stopped = True
        for name, (dev_id, p) in list(processes.items()):
            ret = p.poll()
            if ret is None:
                all_clean_stopped = False
            else:
                if ret == 0:
                    print(f"[SUPERVISOR] Worker {name} stopped cleanly (code 0).")
                else:
                    all_clean_stopped = False
                    print(f"[SUPERVISOR] Worker {name} crashed with code {ret}. Restarting in 10s...")
                    time.sleep(10)
                    cmd = [sys.executable, "-u", "worker.py", "--device", dev_id, "--name", name]
                    new_p = subprocess.Popen(cmd)
                    processes[name] = (dev_id, new_p)
                    print(f"[SUPERVISOR] Restarted worker {name} (new PID: {new_p.pid})")
        if all_clean_stopped:
            print("[SUPERVISOR] All workers stopped cleanly. Exiting notebook session to free Kaggle GPU.")
            sys.exit(0)
        time.sleep(5)'''
assert old_supervisor_loop in cell_code, 'old_supervisor_loop not found'
cell_code = cell_code.replace(old_supervisor_loop, new_supervisor_loop)

# Split back to lines
nb['cells'][1]['source'] = [line + '\n' for line in cell_code.splitlines()]

with open(nb_path, 'w', encoding='utf-8') as f:
    json.dump(nb, f, indent=2)

print('Updated Kaggle notebook with 15-minute idle shutdown & remote stop command successfully!')
