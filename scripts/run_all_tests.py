"""Master Test Suite Runner for MayaCardio Hiring Case Assessment.

Executes all failure tests and prints a comprehensive pass/fail summary matrix.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

SCRIPTS = [
    ("Abrupt Daemon Kill (-9) & Resumption", "test_kill_daemon.py"),
    ("Network Loss & 10s Automatic Mode Switch", "test_network_switch.py"),
    ("Backlog Catch-Up Rate (> 6,500 samples/s)", "test_catchup_rate.py"),
    ("Fault Proxy (Corrupt CRC, Dups, Stall)", "test_fault_proxy.py"),
    ("Disk Full (ENOSPC) & Emergency Allowance", "test_disk_full.py"),
    ("System Clock Jump Resilience", "test_clock_jump.py"),
    ("Ground Truth Export Comparison", "test_ground_truth.py"),
]


def main() -> None:
    print("=================================================================")
    print("       MAYACARDIO PIPELINE - COMPREHENSIVE TEST SUITE           ")
    print("=================================================================\n")

    scripts_dir = Path(__file__).resolve().parent
    results = []

    for name, script_name in SCRIPTS:
        script_path = scripts_dir / script_name
        print(f"\n>>> RUNNING: {name} ({script_name})...")
        t0 = time.time()
        try:
            res = subprocess.run([sys.executable, str(script_path)], check=True)
            elapsed = time.time() - t0
            results.append((name, "PASS", f"{elapsed:.1f}s"))
        except subprocess.CalledProcessError:
            elapsed = time.time() - t0
            results.append((name, "FAIL", f"{elapsed:.1f}s"))

    print("\n\n=================================================================")
    print("                     TEST EXECUTION MATRIX                       ")
    print("=================================================================")
    all_passed = True
    for name, status, duration in results:
        status_str = f"[{status}]"
        print(f" {name:<45} {status_str:>8}  ({duration})")
        if status != "PASS":
            all_passed = False
    print("=================================================================")

    if all_passed:
        print("ALL TESTS PASSED SUCCESSFULLY! Submission criteria satisfied.")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED! Review logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
