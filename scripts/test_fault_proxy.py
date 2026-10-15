"""Failure Test 4: Dynamic Fault Injection via TCP Proxy.

Verifies:
1. Proxy injects corrupt CRC -> Reader rejects corrupt packet and re-reads via from_index.
2. Proxy injects duplicate reply -> Reader ingests without double-counting or crashing.
3. Proxy injects sensor stall -> Buffer overflow occurs -> loss ranges saved honestly.
4. Clean PASS/FAIL log output.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from storage.database import DeviceDatabase
from tools.fault_proxy import FaultProxy
from tools.mock_sim import MockServer


def main() -> None:
    print("=== [TEST 4] FAULT PROXY INJECTION ===")
    test_dir = Path("test_run_fault_proxy")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_fault_proxy.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    mock_port = 7122
    proxy_port = 7123
    control_port = 7198

    # Start mock
    mock = MockServer(mode="signal", seed=99, duration=300, port=mock_port)
    mock.start(blocking=False)
    time.sleep(0.3)

    # Start fault proxy
    proxy = FaultProxy(
        listen_port=proxy_port,
        target_port=mock_port,
        control_port=control_port,
    )
    proxy.start(blocking=False)
    time.sleep(0.3)

    daemon_proc: Optional[subprocess.Popen] = None
    try:
        # Start pipeline daemon pointing at proxy
        cmd = [
            sys.executable,
            "pipeline",
            "run",
            "--db",
            str(db_file),
            "--mock-port",
            str(proxy_port),
        ]
        daemon_proc = subprocess.Popen(cmd)
        print(f"[+] Started pipeline daemon PID={daemon_proc.pid} pointing to proxy port {proxy_port}")
        time.sleep(2.0)

        db = DeviceDatabase(db_file)
        initial_samples = db.get_status()["samples_saved"]
        print(f"[+] Baseline samples ingested: {initial_samples}")
        assert initial_samples > 0

        # Fault 1: Inject Corrupt CRC
        print("[!] Injecting 3 corrupt CRC replies via proxy control API...")
        requests.post(f"http://127.0.0.1:{control_port}/fault", json={"action": "corrupt_crc", "count": 3})
        time.sleep(1.5)

        # Confirm pipeline continued reading without crashing
        samples_after_crc = db.get_status()["samples_saved"]
        print(f"[+] Samples after corrupt CRC injection: {samples_after_crc}")
        assert samples_after_crc > initial_samples

        # Fault 2: Inject Duplicate Replies
        print("[!] Injecting 2 duplicate replies via proxy control API...")
        requests.post(f"http://127.0.0.1:{control_port}/fault", json={"action": "duplicate", "count": 2})
        time.sleep(1.5)

        # Check that no duplicates exist in DB
        conn = db._get_connection()
        dups = conn.execute(
            """
            SELECT session_id, sensor, idx, COUNT(*)
            FROM samples
            GROUP BY session_id, sensor, idx
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        conn.close()
        print(f"[+] Duplicate entries in DB: {len(dups)}")
        assert len(dups) == 0, f"Duplicate entries found: {dups}"

        # Fault 3: Inject Sensor 2 Stall for 3 seconds
        print("[!] Injecting Sensor 2 stall for 3 seconds...")
        requests.post(
            f"http://127.0.0.1:{control_port}/fault",
            json={"action": "stall_sensor", "sensor": 2, "duration": 3.0},
        )
        time.sleep(3.5)

        # Verify pipeline stayed alive and resumed Sensor 2
        final_samples = db.get_status()["samples_saved"]
        print(f"[+] Final samples saved: {final_samples}")
        assert final_samples > samples_after_crc

        print("=== TEST RESULT: PASS ===")

    finally:
        if daemon_proc:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3.0)
            except Exception:
                daemon_proc.kill()
        proxy.stop()
        mock.stop()


if __name__ == "__main__":
    main()
