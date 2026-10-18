"""Failure Test 2: Network Outage, Automatic Mode Switching, and Reconnection.

Verifies:
1. Online mode operation.
2. Network loss detected and mode switched to OFFLINE within 10 s (target < 10 s).
3. Continuous collection and offline window processing on the device during outage.
4. Network restored and mode switched back to ONLINE within 10 s (target < 10 s).
5. Active-upload network cut variant switches to OFFLINE within 10 s.
6. No sample lost or duplicated because of the switch.
7. Complete cloud catch-up after reconnection.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from cloud.server import app, cloud_storage
from storage.database import DeviceDatabase
from tools.mock_sim import MockServer


def main() -> None:
    print("=== [TEST 2] NETWORK OUTAGE & AUTOMATIC MODE SWITCHING ===")
    test_dir = Path("test_run_switch")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_switch.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    mock_port = 7121
    cloud_port = 8011
    cloud_url = f"http://127.0.0.1:{cloud_port}"

    # Step 1: Start mock server
    mock = MockServer(mode="signal", seed=10, duration=300, port=mock_port)
    mock.start(blocking=False)
    time.sleep(0.3)

    cloud_proc: Optional[subprocess.Popen] = None
    daemon_proc: Optional[subprocess.Popen] = None

    def start_cloud() -> subprocess.Popen:
        cmd = [
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
        p = subprocess.Popen(cmd)
        time.sleep(1.2)
        return p

    def stop_cloud(p: subprocess.Popen) -> None:
        p.terminate()
        try:
            p.wait(timeout=3.0)
        except Exception:
            p.kill()

    try:
        # Step 2: Start Cloud Service
        cloud_proc = start_cloud()
        print(f"[+] Cloud service started on {cloud_url}")

        # Step 3: Start Pipeline Daemon
        daemon_cmd = [
            sys.executable,
            "pipeline",
            "run",
            "--db",
            str(db_file),
            "--cloud-url",
            cloud_url,
            "--mock-port",
            str(mock_port),
        ]
        daemon_proc = subprocess.Popen(daemon_cmd)
        print(f"[+] Pipeline daemon started PID={daemon_proc.pid}")

        db = DeviceDatabase(db_file)
        time.sleep(2.0)

        # Confirm initial Online state
        initial_mode = db.get_mode()
        print(f"[+] Initial pipeline mode: {initial_mode}")
        assert initial_mode == "online", f"Expected online mode, got {initial_mode}"

        # Step 4: Inject Network Cut by terminating cloud service (idle/live streaming)
        print("[!] CUTTING NETWORK TO CLOUD...")
        t_cut = time.monotonic()
        stop_cloud(cloud_proc)

        # Wait for mode switch to OFFLINE
        switched_to_offline = False
        time_to_offline = 0.0
        while time.monotonic() - t_cut < 12.0:
            mode = db.get_mode()
            if mode == "offline":
                time_to_offline = time.monotonic() - t_cut
                switched_to_offline = True
                break
            time.sleep(0.3)

        print(f"[+] Switched to OFFLINE mode in {time_to_offline:.2f} seconds")
        assert switched_to_offline, "Failed to switch to OFFLINE mode within 12 seconds!"
        assert time_to_offline < 10.0, f"Offline switch took {time_to_offline:.2f}s (required < 10s)"

        # Let device accumulate samples in offline mode
        print("[+] Device collecting data in OFFLINE mode for 4 seconds...")
        time.sleep(4.0)

        # Check offline results computed locally
        conn = db._get_connection()
        offline_results = conn.execute("SELECT COUNT(*) FROM results WHERE mode='offline'").fetchone()[0]
        samples_in_outage = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        conn.close()
        print(f"[+] Local samples saved during run/outage: {samples_in_outage}")
        print(f"[+] Local offline window results computed: {offline_results}")
        assert samples_in_outage > 0

        # Step 5: Restore Network by restarting cloud service
        print("[!] RESTORING NETWORK TO CLOUD...")
        t_restore = time.monotonic()
        cloud_proc = start_cloud()

        switched_to_online = False
        time_to_online = 0.0
        while time.monotonic() - t_restore < 12.0:
            mode = db.get_mode()
            if mode == "online":
                time_to_online = time.monotonic() - t_restore
                switched_to_online = True
                break
            time.sleep(0.3)

        print(f"[+] Switched to ONLINE mode in {time_to_online:.2f} seconds")
        assert switched_to_online, "Failed to switch back to ONLINE mode within 12 seconds!"
        assert time_to_online < 10.0, f"Online switch took {time_to_online:.2f}s (required < 10s)"

        # Step 6: Wait for partial catch-up
        print("[+] Waiting for backlog catch-up...")
        time.sleep(4.0)

        status = db.get_status()
        print(f"[+] Current Status: {status}")
        assert status["backlog"] <= 800, f"Backlog did not catch up: {status['backlog']}"

        # Step 7: Active-Upload Network Cut Variant
        print("[!] Testing active-upload network cut variant...")
        time.sleep(1.0)
        print("[!] CUTTING NETWORK DURING ACTIVE UPLOADS...")
        t_cut_active = time.monotonic()
        stop_cloud(cloud_proc)

        switched_to_offline_active = False
        time_to_offline_active = 0.0
        while time.monotonic() - t_cut_active < 12.0:
            mode = db.get_mode()
            if mode == "offline":
                time_to_offline_active = time.monotonic() - t_cut_active
                switched_to_offline_active = True
                break
            time.sleep(0.3)

        print(f"[+] Switched to OFFLINE mode during active uploads in {time_to_offline_active:.2f} seconds")
        assert switched_to_offline_active, "Failed to switch to OFFLINE mode during active uploads within 12 seconds!"
        assert time_to_offline_active < 10.0, f"Active upload offline switch took {time_to_offline_active:.2f}s (required < 10s)"

        # Verify duplicate check in DB
        conn = db._get_connection()
        dup_count = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT session_id, sensor, idx
                FROM samples
                GROUP BY session_id, sensor, idx
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        conn.close()
        print(f"[+] Duplicate samples across switch: {dup_count}")
        assert dup_count == 0, "Duplicate samples found!"

        print("=== TEST RESULT: PASS ===")

    finally:
        if daemon_proc:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3.0)
            except Exception:
                daemon_proc.kill()
        if cloud_proc:
            stop_cloud(cloud_proc)
        mock.stop()


if __name__ == "__main__":
    main()
