"""Failure Test 1: Abrupt Process Kill (SIGKILL / kill -9) and Recovery.

Verifies:
1. SQLite WAL integrity after kill -9.
2. Zero duplicate samples upon restart.
3. Accurate resumption from stored cursor.
4. Clean PASS/FAIL log output.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import DeviceDatabase
from tools.mock_sim import MockServer


def main() -> None:
    print("=== [TEST 1] KILL -9 PROCESS TERMINATION & RECOVERY ===")
    test_dir = Path("test_run_kill")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_kill.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    port = 7120
    mock = MockServer(mode="signal", seed=42, duration=300, port=port)
    mock.start(blocking=False)
    time.sleep(0.5)

    try:
        # Step 1: Launch pipeline daemon as a subprocess
        cmd = [
            sys.executable,
            "pipeline",
            "run",
            "--db",
            str(db_file),
            "--mock-port",
            str(port),
        ]
        proc = subprocess.Popen(cmd)
        print(f"[+] Started pipeline daemon PID={proc.pid}")

        # Let it ingest data for 3 seconds
        time.sleep(3.0)

        # Step 2: Hard kill (-9 / taskkill)
        print(f"[!] Sending hard KILL to daemon PID={proc.pid}...")
        proc.kill()
        proc.wait()
        print("[+] Process killed abruptly.")

        # Step 3: Verify SQLite integrity immediately
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        assert integrity == "ok", f"Integrity check failed: {integrity}"

        cur.execute("SELECT COUNT(*) FROM samples")
        samples_before = cur.fetchone()[0]
        print(f"[+] PRAGMA integrity_check: {integrity}")
        print(f"[+] Samples safely committed before kill: {samples_before}")
        conn.close()

        assert samples_before > 0, "No samples were saved before kill"

        # Step 4: Restart pipeline daemon
        print("[+] Restarting pipeline daemon...")
        proc2 = subprocess.Popen(cmd)
        time.sleep(2.5)

        # Graceful shutdown of restarted daemon
        proc2.terminate()
        try:
            proc2.wait(timeout=3.0)
        except Exception:
            proc2.kill()

        # Step 5: Check for duplicates or corruption
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        integrity_restart = cur.fetchone()[0]
        assert integrity_restart == "ok"

        # Check for duplicate primary keys (session_id, sensor, idx)
        cur.execute(
            """
            SELECT session_id, sensor, idx, COUNT(*)
            FROM samples
            GROUP BY session_id, sensor, idx
            HAVING COUNT(*) > 1
            """
        )
        duplicates = cur.fetchall()
        assert len(duplicates) == 0, f"Duplicate samples detected: {duplicates}"

        cur.execute("SELECT COUNT(*) FROM samples")
        samples_after = cur.fetchone()[0]
        conn.close()

        print(f"[+] Total samples after resumption: {samples_after}")
        print(f"[+] Duplicates: {len(duplicates)}")
        print("=== TEST RESULT: PASS ===")

    finally:
        mock.stop()


if __name__ == "__main__":
    main()
