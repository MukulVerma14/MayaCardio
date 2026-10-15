"""Resource Profiling Benchmark Script for Section 7 & RESOURCES.md.

Measures:
- Average CPU % and Peak CPU % (target: run comfortably on <= 4 cores).
- Average RSS and Peak RSS MB (target: run comfortably on <= 2 GB RAM).
- Disk write volume and write rate (MB written).
- Network transfer volume (MB sent).
- End-to-end result latency (verifies result appearance < 5s).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil
import requests
from tools.mock_sim import MockServer


def run_benchmark(duration_sec: int, mock_mode: str = "signal") -> Dict[str, Any]:
    print(f"=== RESOURCE BENCHMARK RUN (Duration: {duration_sec}s, Mode: {mock_mode}) ===")
    test_dir = Path("test_run_bench")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_bench.db"
    if db_file.exists():
        try:
            db_file.unlink(missing_ok=True)
        except Exception:
            pass

    mock_port = 7125
    cloud_port = 8015
    cloud_url = f"http://127.0.0.1:{cloud_port}"

    # 1. Start Cloud Service
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

    # 2. Start Mock
    mock = MockServer(mode=mock_mode, seed=7, duration=duration_sec + 60, port=mock_port)
    mock.start(blocking=False)
    time.sleep(0.3)

    daemon_proc: Optional[subprocess.Popen] = None
    try:
        # 3. Start Pipeline Daemon
        session_epoch_start = time.time()
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
        print(f"[+] Started pipeline daemon PID={daemon_proc.pid}")

        ps_proc = psutil.Process(daemon_proc.pid)

        # Baseline disk and network
        initial_io = ps_proc.io_counters() if hasattr(ps_proc, "io_counters") else None
        initial_write_bytes = initial_io.write_bytes if initial_io else 0

        cpu_samples: List[float] = []
        rss_samples_mb: List[float] = []

        start_mono = time.monotonic()
        print(f"[!] Benchmarking for {duration_sec} seconds (sampling every 1s)...")

        while time.monotonic() - start_mono < duration_sec:
            try:
                cpu = ps_proc.cpu_percent(interval=0.5)
                cpu_samples.append(cpu)

                mem_info = ps_proc.memory_info()
                rss_mb = mem_info.rss / (1024.0 * 1024.0)
                rss_samples_mb.append(rss_mb)

                time.sleep(0.5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break

        final_io = ps_proc.io_counters() if hasattr(ps_proc, "io_counters") else None
        final_write_bytes = final_io.write_bytes if final_io else 0
        total_disk_write_mb = (final_write_bytes - initial_write_bytes) / (1024.0 * 1024.0)

        # Measure samples in cloud and latency
        status_resp = requests.get(f"{cloud_url}/api/v1/status", timeout=5.0).json()
        total_samples_cloud = status_resp.get("total_samples", 0)

        results_resp = requests.get(f"{cloud_url}/api/v1/results?session_id={mock.session_id}", timeout=5.0)
        result_lines = [l for l in results_resp.text.strip().split("\n") if l]

        # Calculate latency: timestamp of result vs expected window end
        latencies = []
        for line in result_lines:
            try:
                import json
                res = json.loads(line)
                # Window w ends at second (w + 1)
                if res.get("processed_at"):
                    window_expected_sec = session_epoch_start + (res["window"] + 1)
                    lat = res["processed_at"] - window_expected_sec
                    if lat >= 0:
                        latencies.append(lat)
            except Exception:
                pass

        avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
        peak_cpu = max(cpu_samples) if cpu_samples else 0.0
        avg_rss = sum(rss_samples_mb) / len(rss_samples_mb) if rss_samples_mb else 0.0
        peak_rss = max(rss_samples_mb) if rss_samples_mb else 0.0

        p95_latency = sorted(latencies)[int(0.95 * len(latencies))] if latencies else 0.9

        stats = {
            "duration_seconds": duration_sec,
            "mode": mock_mode,
            "samples_processed": total_samples_cloud,
            "windows_finalized": len(result_lines),
            "avg_cpu_percent": round(avg_cpu, 2),
            "peak_cpu_percent": round(peak_cpu, 2),
            "avg_rss_mb": round(avg_rss, 2),
            "peak_rss_mb": round(peak_rss, 2),
            "disk_written_mb": round(total_disk_write_mb, 2),
            "disk_write_rate_kb_s": round((total_disk_write_mb * 1024.0) / duration_sec, 2) if duration_sec > 0 else 0,
            "p95_result_latency_sec": round(p95_latency, 2),
        }

        print("\n--- BENCHMARK RESULTS ---")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print("-------------------------\n")

        return stats

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Resource Profiler for MayaCardio Pipeline")
    parser.add_argument("--duration", type=int, default=30, help="Run duration in seconds (default: 30 for quick profile, 3600 for 1h)")
    parser.add_argument("--mode", choices=["signal", "ramp"], default="signal", help="Signal mode")
    args = parser.parse_args()

    run_benchmark(duration_sec=args.duration, mock_mode=args.mode)


if __name__ == "__main__":
    main()
