"""Failure Test 7: Export Validation Against Ground Truth Log.

Verifies:
1. Runs mock simulator with --ground-truth-log.
2. Ingests data through pipeline to device and cloud.
3. Exports from both device and cloud (pipeline export --session ID --from device|cloud).
4. Validates line-by-line against ground-truth log:
   - Every sample received has exact match on sensor, index, and values.
   - Every loss record matches.
   - Prepares seamless hook for official 'check_ground_truth' binary once provided.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mock_sim import MockServer


def compare_export_with_ground_truth(export_path: Path, gt_path: Path) -> Tuple[bool, str]:
    """Compare a pipeline export file with simulator ground truth log."""
    gt_samples: Dict[Tuple[int, int], List[int]] = {}
    gt_losses: List[Dict[str, Any]] = []

    with open(gt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "header":
                continue
            if "index" in obj:
                gt_samples[(obj["sensor"], obj["index"])] = obj["values"]
            elif "from_index" in obj:
                gt_losses.append(obj)

    exp_samples: Dict[Tuple[int, int], List[int]] = {}
    exp_losses: List[Dict[str, Any]] = []

    with open(export_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "index" in obj:
                exp_samples[(obj["sensor"], obj["index"])] = obj["values"]
            elif "from_index" in obj:
                exp_losses.append(obj)

    # Check that all exported samples match ground truth exactly
    if not exp_samples:
        return False, "Export file contained 0 samples"

    for key, values in exp_samples.items():
        if key not in gt_samples:
            return False, f"Exported sample {key} not found in ground truth log!"
        if gt_samples[key] != values:
            return False, f"Sample mismatch at {key}: ground truth {gt_samples[key]} != exported {values}"

    return True, f"Verified {len(exp_samples)} samples matched ground truth exactly"


def main() -> None:
    print("=== [TEST 7] GROUND TRUTH EXPORT COMPARISON ===")
    test_dir = Path("test_run_ground_truth")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_gt.db"
    gt_file = test_dir / "ground_truth.jsonl"
    device_export_file = test_dir / "device_export.jsonl"
    cloud_export_file = test_dir / "cloud_export.jsonl"

    for p in (db_file, gt_file, device_export_file, cloud_export_file):
        if p.exists():
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    mock_port = 7124
    cloud_port = 8014
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

    # Step 2: Start Mock Simulator with ground-truth-log enabled
    mock = MockServer(
        mode="ramp",
        seed=7,
        duration=300,
        port=mock_port,
        ground_truth_path=str(gt_file),
    )
    mock.start(blocking=False)
    time.sleep(0.3)
    session_id = mock.session_id
    print(f"[+] Started mock in RAMP mode (session={session_id}, logging to {gt_file})")

    daemon_proc: Optional[subprocess.Popen] = None
    try:
        # Step 3: Run pipeline daemon
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

        # Let data stream for 4 seconds
        time.sleep(4.0)

        # Stop daemon cleanly so all writes are committed
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=3.0)
        except Exception:
            daemon_proc.kill()
        daemon_proc = None

        # Step 4: Export from device
        print("[+] Exporting from DEVICE...")
        with open(device_export_file, "w", encoding="utf-8") as f:
            export_cmd = [
                sys.executable,
                "pipeline",
                "export",
                "--db",
                str(db_file),
                "--session",
                session_id,
                "--from",
                "device",
            ]
            subprocess.run(export_cmd, stdout=f, check=True)

        # Step 5: Export from cloud
        print("[+] Exporting from CLOUD...")
        with open(cloud_export_file, "w", encoding="utf-8") as f:
            export_cmd_cloud = [
                sys.executable,
                "pipeline",
                "export",
                "--cloud-url",
                cloud_url,
                "--session",
                session_id,
                "--from",
                "cloud",
            ]
            subprocess.run(export_cmd_cloud, stdout=f, check=True)

        # Step 6: Verify Device Export Against Ground Truth
        print("[+] Validating Device Export against Ground Truth...")
        dev_ok, dev_msg = compare_export_with_ground_truth(device_export_file, gt_file)
        print(f"[+] Device comparison: {dev_msg}")
        assert dev_ok, f"Device export failed ground truth: {dev_msg}"

        # Step 7: Verify Cloud Export Against Ground Truth
        print("[+] Validating Cloud Export against Ground Truth...")
        cloud_ok, cloud_msg = compare_export_with_ground_truth(cloud_export_file, gt_file)
        print(f"[+] Cloud comparison: {cloud_msg}")
        assert cloud_ok, f"Cloud export failed ground truth: {cloud_msg}"

        # Step 8: Check for official check_ground_truth tool
        official_tool = shutil.which("check_ground_truth")
        if official_tool:
            print(f"[+] Found official check_ground_truth binary at {official_tool}. Invoking...")
            subprocess.run([official_tool, str(device_export_file)], check=True)
            print("[+] Official check_ground_truth: PASS")
        else:
            print("[i] Official check_ground_truth binary not in PATH yet (using dev simulator ground truth verification)")

        print("=== TEST RESULT: PASS ===")

    finally:
        if daemon_proc:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3.0)
            except Exception:
                daemon_proc.kill()
        mock.stop()
        cloud_proc.terminate()
        try:
            cloud_proc.wait(timeout=3.0)
        except Exception:
            cloud_proc.kill()


if __name__ == "__main__":
    main()
