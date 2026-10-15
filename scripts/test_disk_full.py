"""Failure Test 5: ENOSPC / Disk Full Emergency Allowance and Loss Logging.

Verifies:
1. Pre-reserved emergency file exists on device startup.
2. When disk is full (ENOSPC / sqlite3.OperationalError: database or disk is full),
   emergency allowance is released.
3. Loss record is committed with reason='disk_full'.
4. Pipeline does not crash silently.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.types import Sample
from storage.database import DeviceDatabase


class EnospcSimulatedConnection:
    """Wraps sqlite3.Connection to simulate ENOSPC on sample insertions."""

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._conn = real_conn

    def execute(self, sql: str, *args: any, **kwargs: any) -> any:
        if "INSERT INTO samples" in sql:
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> any:
        return getattr(self._conn, name)


def main() -> None:
    print("=== [TEST 5] DISK FULL (ENOSPC) EMERGENCY ALLOWANCE & LOSS LOGGING ===")
    test_dir = Path("test_run_disk_full")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_enospc.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    db = DeviceDatabase(db_file)

    # Verify reserve exists
    print(f"[+] Verifying emergency reserve file: {db.reserve_path}")
    assert db.reserve_path.exists(), "Emergency reserve file was not created!"
    reserve_size = db.reserve_path.stat().st_size
    print(f"[+] Reserve file size: {reserve_size} bytes")

    session_id = "sess_enospc_test"
    sample = Sample(sensor=1, idx=5000, values=[100, 200, 300])

    # Intercept _get_connection to return our simulated failing connection
    original_get_conn = db._get_connection
    first_call = True

    def failing_get_conn() -> any:
        nonlocal first_call
        real_c = original_get_conn()
        if first_call:
            first_call = False
            return EnospcSimulatedConnection(real_c)
        return real_c

    db._get_connection = failing_get_conn

    print("[!] Simulating ENOSPC on sample insert...")
    try:
        db.save_reply_batch(
            session_id=session_id,
            sensor=1,
            samples=[sample],
            losses=[],
            next_read_idx=5001,
        )
    except sqlite3.OperationalError:
        print("[+] OperationalError handled by database reserve logic")

    # Restore real connection getter
    db._get_connection = original_get_conn

    # Verify emergency reserve file was released
    print("[+] Checking emergency reserve release...")
    assert not db.reserve_path.exists(), "Reserve file should have been released to free space!"

    # Verify loss record with reason 'disk_full' was recorded
    conn = db._get_connection()
    loss_rows = conn.execute(
        "SELECT * FROM losses WHERE session_id=? AND reason='disk_full'",
        (session_id,),
    ).fetchall()
    conn.close()

    print(f"[+] Recorded disk_full loss rows: {len(loss_rows)}")
    assert len(loss_rows) > 0, "No disk_full loss record was written!"
    assert loss_rows[0]["from_idx"] == 5000
    assert loss_rows[0]["reason"] == "disk_full"

    print("=== TEST RESULT: PASS ===")


if __name__ == "__main__":
    main()
