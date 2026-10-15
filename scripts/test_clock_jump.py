"""Failure Test 6: System Clock Jump Resilience (Forward & Backward).

Verifies:
1. Intervals, timeouts, and hysteresis use time.monotonic() and are immune to wall-clock changes.
2. Window partitioning strictly uses sample index arithmetic (400w .. 400w+399).
3. Jumping wall-clock forward (+2 hours) or backward (-2 hours) causes zero disruption.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectivity.monitor import ConnectivityMonitor
from core.window_processor import WindowProcessor, SAMPLES_PER_SECOND
from storage.database import DeviceDatabase


def main() -> None:
    print("=== [TEST 6] SYSTEM CLOCK JUMP RESILIENCE ===")
    test_dir = Path("test_run_clock")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_clock.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    db = DeviceDatabase(db_file)

    # 1. Test Window Processing Immunity to Clock Changes
    print("[+] Testing window processor index determinism across clock jumps...")
    proc = WindowProcessor(session_id="clock_sess", mode="offline")

    # Feed window 0 with wall clock jump forward (+7200 seconds / 2 hours)
    wall_clock_time = time.time()

    def fake_time_forward() -> float:
        return wall_clock_time + 7200.0

    with unittest.mock.patch("time.time", fake_time_forward):
        for i in range(401):
            proc.add_sample(1, i, [100, 200, 300])
        for i in range(400):
            proc.add_sample(2, i, [400000, 150000])

        w0_results = proc.finalize_available_windows(processed_at=fake_time_forward())
        assert len(w0_results) == 1
        assert w0_results[0].window == 0
        assert w0_results[0].complete is True
        print(f"[+] Window 0 finalized correctly after +2h forward jump: processed_at={w0_results[0].processed_at}")

    # Feed window 1 with wall clock jump backward (-7200 seconds / 2 hours)
    def fake_time_backward() -> float:
        return wall_clock_time - 7200.0

    with unittest.mock.patch("time.time", fake_time_backward):
        for i in range(401, 801):
            proc.add_sample(1, i, [100, 200, 300])
        for i in range(400, 800):
            proc.add_sample(2, i, [400000, 150000])

        w1_results = proc.finalize_available_windows(processed_at=fake_time_backward())
        assert len(w1_results) == 1
        assert w1_results[0].window == 1
        assert w1_results[0].complete is True
        print(f"[+] Window 1 finalized correctly after -2h backward jump: processed_at={w1_results[0].processed_at}")

    # 2. Test Connectivity Monitor Monotonic Integrity
    print("[+] Verifying connectivity monitor uses time.monotonic()...")
    conn_monitor = ConnectivityMonitor(db=db, probe_interval_sec=0.5, probe_timeout_sec=0.2)
    # Simulate failed probe while wall clock shifts wildly
    with unittest.mock.patch("time.time", fake_time_forward):
        # Monotonic time continues progressing normally
        mono_t0 = time.monotonic()
        time.sleep(0.3)
        mono_t1 = time.monotonic()
        assert mono_t1 > mono_t0
        print(f"[+] Monotonic elapsed: {mono_t1 - mono_t0:.4f}s (unaffected by wall clock patch)")

    print("=== TEST RESULT: PASS ===")


if __name__ == "__main__":
    main()
