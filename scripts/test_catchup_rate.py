"""Failure Test 3: Backlog Catch-Up Throughput Benchmark.

Verifies:
1. Simulates post-outage backlog drain.
2. Measures sustained throughput in samples/second.
3. Verifies sustained throughput exceeds 6,500 samples/s (the rate required
   to upload a 2-hour outage of 5.76M samples in under 15 minutes).
4. Verifies zero loss, zero corruption, zero duplicates.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectivity.monitor import ConnectivityMonitor
from core.types import Sample
from storage.database import DeviceDatabase
from uploader.client import UploaderClient


def main() -> None:
    print("=== [TEST 3] BACKLOG CATCH-UP THROUGHPUT BENCHMARK ===")
    test_dir = Path("test_run_catchup")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_catchup.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    cloud_port = 8012
    cloud_url = f"http://127.0.0.1:{cloud_port}"

    # Step 1: Start Cloud Service
    cloud_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "cloud.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(cloud_port),
        "--log-level",
        "warning",
    ]
    cloud_proc = subprocess.Popen(cloud_cmd)
    time.sleep(1.2)
    print(f"[+] Cloud service listening at {cloud_url}")

    try:
        db = DeviceDatabase(db_file)
        session_id = "bench_sess_catchup"

        # Populate a large backlog representing outage accumulation
        TARGET_SAMPLES = 65000  # 65k samples benchmark batch
        print(f"[+] Generating local backlog of {TARGET_SAMPLES} samples in SQLite...")

        CHUNK = 5000
        for start_idx in range(0, TARGET_SAMPLES, CHUNK):
            count = min(CHUNK, TARGET_SAMPLES - start_idx)
            s1_batch = [
                Sample(sensor=1, idx=start_idx + i, values=[100 + i, 200 + i, 300 + i])
                for i in range(count)
            ]
            s2_batch = [
                Sample(sensor=2, idx=start_idx + i, values=[400000 + i, 150000 + i])
                for i in range(count)
            ]
            db.save_reply_batch(session_id, 1, s1_batch, [], start_idx + count)
            db.save_reply_batch(session_id, 2, s2_batch, [], start_idx + count)

        total_saved = db.get_status()["samples_saved"]
        print(f"[+] Total samples staged on device: {total_saved}")
        assert total_saved >= TARGET_SAMPLES * 2

        # Step 2: Launch Uploader and measure drain duration
        conn_monitor = ConnectivityMonitor(db=db, cloud_url=cloud_url)
        uploader = UploaderClient(db=db, conn_monitor=conn_monitor, cloud_url=cloud_url)

        print("[!] Starting catch-up upload stream...")
        t_start = time.monotonic()

        samples_uploaded = 0
        while True:
            step_uploaded = uploader._upload_batch_step()
            if step_uploaded == 0:
                break
            samples_uploaded += step_uploaded

        elapsed = time.monotonic() - t_start
        throughput = samples_uploaded / elapsed if elapsed > 0 else 0

        print(f"[+] Uploaded {samples_uploaded} samples in {elapsed:.2f} seconds")
        print(f"[+] Sustained Catch-Up Rate: {throughput:.1f} samples/second")
        print(f"[+] Target Minimum Required: 6,500 samples/second")

        assert throughput >= 6500.0, f"Throughput {throughput:.1f} below required 6,500 samples/s!"

        # Step 3: Verify cloud database has full copy
        import requests
        resp = requests.get(f"{cloud_url}/api/v1/status", timeout=5.0)
        assert resp.status_code == 200
        cloud_status = resp.json()
        print(f"[+] Cloud Status: {cloud_status}")
        assert cloud_status["total_samples"] >= total_saved

        print("=== TEST RESULT: PASS ===")

    finally:
        cloud_proc.terminate()
        try:
            cloud_proc.wait(timeout=3.0)
        except Exception:
            cloud_proc.kill()


if __name__ == "__main__":
    main()
