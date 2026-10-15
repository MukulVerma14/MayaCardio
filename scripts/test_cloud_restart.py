"""Failure Test 8: Cloud Service Process Restart & Data Persistence.

Verifies:
1. Ingests data into cloud service.
2. Abruptly terminates the cloud service process.
3. Restarts the cloud service with the same persistent database.
4. Confirms all samples, losses, and computed window results remain intact.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests


def main() -> None:
    print("=== [TEST 8] CLOUD SERVICE RESTART & DATA PERSISTENCE ===")
    test_dir = Path("test_run_cloud_restart")
    test_dir.mkdir(parents=True, exist_ok=True)
    cloud_db_file = test_dir / "cloud_persistent.db"
    if cloud_db_file.exists():
        try:
            cloud_db_file.unlink(missing_ok=True)
        except Exception:
            pass

    port = 8019
    cloud_url = f"http://127.0.0.1:{port}"
    db_url = f"sqlite:///{cloud_db_file.resolve()}"

    env = os.environ.copy()
    env["CLOUD_DATABASE_URL"] = db_url

    def start_cloud() -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "cloud.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        p = subprocess.Popen(cmd, env=env)
        time.sleep(1.2)
        return p

    def stop_cloud(p: subprocess.Popen) -> None:
        p.terminate()
        try:
            p.wait(timeout=3.0)
        except Exception:
            p.kill()

    cloud_proc = None
    try:
        # Step 1: Start Cloud Service
        print(f"[+] Starting cloud service instance 1 on {cloud_url}...")
        cloud_proc = start_cloud()

        # Step 2: Ingest test batch
        session_id = "restart_test_sess"
        s1_samples = [{"sensor": 1, "index": i, "values": [100, 200, 300]} for i in range(401)]
        s2_samples = [{"sensor": 2, "index": i, "values": [400000, 150000]} for i in range(400)]
        losses = [{"sensor": 1, "from_index": 500, "to_index": 510, "reason": "overflow"}]

        payload = {
            "session_id": session_id,
            "samples": s1_samples + s2_samples,
            "losses": losses,
            "offline_results": [],
        }

        body = gzip.compress(json.dumps(payload).encode("utf-8"))
        headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}

        ingest_resp = requests.post(f"{cloud_url}/api/v1/ingest", data=body, headers=headers, timeout=5.0)
        assert ingest_resp.status_code == 200
        print(f"[+] Ingested 801 samples and 1 loss record into cloud instance 1")

        # Verify initial status
        st1 = requests.get(f"{cloud_url}/api/v1/status", timeout=5.0).json()
        assert st1["total_samples"] == 801
        print(f"[+] Cloud instance 1 status: total_samples={st1['total_samples']}")

        # Step 3: Hard kill the cloud service process
        print("[!] Terminating cloud service process abruptly...")
        stop_cloud(cloud_proc)
        cloud_proc = None
        time.sleep(0.5)

        # Step 4: Restart cloud service on the exact same database
        print("[+] Starting cloud service instance 2 (restarted)...")
        cloud_proc = start_cloud()

        # Step 5: Verify all data persisted across restart
        st2 = requests.get(f"{cloud_url}/api/v1/status", timeout=5.0).json()
        print(f"[+] Cloud instance 2 status: total_samples={st2['total_samples']}")
        assert st2["total_samples"] == 801, f"Expected 801 samples after restart, got {st2['total_samples']}"

        # Export samples and verify
        exp_resp = requests.get(f"{cloud_url}/api/v1/export/samples?session_id={session_id}", timeout=5.0)
        assert exp_resp.status_code == 200
        lines = [l for l in exp_resp.text.strip().split("\n") if l]
        # 801 samples + 1 loss record = 802 lines
        print(f"[+] Export lines recovered after restart: {len(lines)}")
        assert len(lines) == 802

        # Verify window results persisted
        res_resp = requests.get(f"{cloud_url}/api/v1/results?session_id={session_id}", timeout=5.0)
        assert res_resp.status_code == 200
        res_lines = [l for l in res_resp.text.strip().split("\n") if l]
        assert len(res_lines) >= 1
        w0 = json.loads(res_lines[0])
        assert w0["window"] == 0
        assert w0["complete"] is True
        print(f"[+] Verified window 0 result persisted: complete={w0['complete']}")

        print("=== TEST RESULT: PASS ===")

    finally:
        if cloud_proc:
            stop_cloud(cloud_proc)


if __name__ == "__main__":
    main()
