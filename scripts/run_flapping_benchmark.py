"""Benchmark script to measure connectivity flapping, heartbeat latency, and upload throughput

Tests against the live cloud deployment under sustained ingestion load.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectivity.monitor import ConnectivityMonitor
from reader.client import ReaderClient
from storage.database import DeviceDatabase
from tools.mock_sim import MockServer
from uploader.client import UploaderClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("flapping_benchmark")


def populate_backlog(db: DeviceDatabase, session_id: str, count_per_sensor: int = 50000) -> None:
    """Populate local database with unacked samples to create a backlog >= 100,000."""
    logger.info(f"Populating initial backlog: {count_per_sensor} samples per sensor ({count_per_sensor * 2} total)...")
    conn = db._get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for sensor in (1, 2):
            batch = [
                (session_id, sensor, i, i % 100, (i + 1) % 100, (i + 2) % 100)
                for i in range(count_per_sensor)
            ]
            conn.executemany(
                "INSERT INTO samples (session_id, sensor, idx, ch0, ch1, ch2) VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            now = time.time()
            conn.execute(
                """
                INSERT INTO cursors (session_id, sensor, last_read_idx, last_acked_idx, updated_at)
                VALUES (?, ?, ?, -1, ?)
                """,
                (session_id, sensor, count_per_sensor - 1, now),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()

    status = db.get_status(session_id)
    logger.info(f"Backlog populated successfully. DB Status: {status}")


def run_flapping_benchmark(
    cloud_url: str = "https://mayacardio-z481.onrender.com",
    duration_sec: int = 1200,
    backlog_count: int = 100000,
) -> Dict[str, Any]:
    test_dir = Path("test_flapping_run")
    test_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_dir / "pipeline_flapping.db"
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass

    mock_port = 7188
    # Start fresh mock server session
    mock = MockServer(mode="signal", seed=17, duration=duration_sec + 600, port=mock_port)
    mock.start(blocking=False)
    time.sleep(0.5)

    session_id = mock.session_id
    logger.info(f"Started fresh mock session: {session_id}")

    db = DeviceDatabase(db_file)
    count_per_sensor = backlog_count // 2
    populate_backlog(db, session_id, count_per_sensor=count_per_sensor)

    # Set mock server cursors so live reader continues from count_per_sensor
    mock.client_cursors[1] = count_per_sensor
    mock.client_cursors[2] = count_per_sensor
    # Set mock server start time to reflect already produced samples
    mock.start_monotonic = time.monotonic() - (count_per_sensor / 400.0)

    # Instrument ConnectivityMonitor to collect detailed telemetry
    monitor = ConnectivityMonitor(db=db, cloud_url=cloud_url, probe_interval_sec=2.0, probe_timeout_sec=2.0)

    heartbeat_latencies_draining: List[float] = []
    heartbeat_latencies_idle: List[float] = []
    mode_switches: List[Dict[str, Any]] = []

    current_mode = monitor.current_mode

    def on_mode_change(old_mode: str, new_mode: str) -> None:
        mode_switches.append({
            "timestamp": time.time(),
            "from": old_mode,
            "to": new_mode,
        })
        logger.warning(f"MODE SWITCH: {old_mode} -> {new_mode}")

    uploader = UploaderClient(db=db, conn_monitor=monitor, cloud_url=cloud_url, request_timeout_sec=10.0)
    reader = ReaderClient(db=db, mock_host="127.0.0.1", mock_port=mock_port)

    # Instrument uploader to track ingest calls
    ingest_attempts = 0
    ingest_successes = 0
    ingest_failures = 0
    samples_uploaded_total = 0

    original_upload_step = uploader._upload_batch_step

    def instrumented_upload_step() -> int:
        nonlocal ingest_attempts, ingest_successes, ingest_failures, samples_uploaded_total
        cur1 = db.get_cursor(1, session_id)
        cur2 = db.get_cursor(2, session_id)
        sess = cur1[0] if cur1 else (cur2[0] if cur2 else None)
        if not sess:
            return 0
        s1 = db.get_unacked_samples(sess, 1, limit=2500)
        s2 = db.get_unacked_samples(sess, 2, limit=2500)
        if not s1 and not s2:
            return 0

        ingest_attempts += 1
        t_start = time.monotonic()
        count = original_upload_step()
        if count > 0:
            ingest_successes += 1
            samples_uploaded_total += count
        else:
            ingest_failures += 1
        return count

    uploader._upload_batch_step = instrumented_upload_step  # type: ignore

    # Run background probe timer to record heartbeat latency
    probe_running = True
    session = requests.Session()

    def probe_loop():
        nonlocal current_mode
        while probe_running:
            t0 = time.monotonic()
            try:
                resp = session.post(f"{cloud_url}/api/v1/heartbeat", timeout=2.5)
                rtt = time.monotonic() - t0
                status = db.get_status(session_id)
                if status.get("backlog", 0) > 800:
                    heartbeat_latencies_draining.append(rtt)
                else:
                    heartbeat_latencies_idle.append(rtt)
            except Exception:
                rtt = time.monotonic() - t0
                status = db.get_status(session_id)
                if status.get("backlog", 0) > 800:
                    heartbeat_latencies_draining.append(rtt)
                else:
                    heartbeat_latencies_idle.append(rtt)

            new_mode = monitor.current_mode
            if new_mode != current_mode:
                on_mode_change(current_mode, new_mode)
                current_mode = new_mode

            time.sleep(2.0)

    probe_thread = threading.Thread(target=probe_loop, daemon=True)

    logger.info(f"Starting pipeline components against {cloud_url} for {duration_sec}s...")
    monitor.start()
    uploader.start()
    reader.start()
    probe_thread.start()

    start_time = time.monotonic()
    t_drain_finished: float | None = None

    try:
        while time.monotonic() - start_time < duration_sec:
            status = db.get_status(session_id)
            cur_backlog = status.get("backlog", 0)
            if cur_backlog <= 800 and t_drain_finished is None:
                t_drain_finished = time.monotonic()
                logger.info(f"Initial backlog drained to live levels in {t_drain_finished - start_time:.1f}s")
            logger.info(
                f"Elapsed: {int(time.monotonic() - start_time)}s / {duration_sec}s | "
                f"Mode: {monitor.current_mode} | Backlog: {cur_backlog} | "
                f"Mode switches: {len(mode_switches)} | Uploaded: {samples_uploaded_total}"
            )
            time.sleep(15.0)
    finally:
        probe_running = False
        reader.stop()
        uploader.stop()
        monitor.stop()
        mock.stop()

    total_time = time.monotonic() - start_time

    def calc_stats(latencies: List[float]) -> Dict[str, float]:
        if not latencies:
            return {"count": 0, "median": 0.0, "p95": 0.0, "max": 0.0}
        s = sorted(latencies)
        med = s[len(s) // 2]
        p95 = s[int(0.95 * len(s))]
        mx = max(s)
        return {
            "count": len(s),
            "median": round(med, 3),
            "p95": round(p95, 3),
            "max": round(mx, 3),
        }

    drain_duration = (t_drain_finished - start_time) if t_drain_finished else total_time
    throughput = (samples_uploaded_total / total_time) if total_time > 0 else 0.0
    success_rate = (ingest_successes / ingest_attempts * 100.0) if ingest_attempts > 0 else 0.0

    report = {
        "cloud_url": cloud_url,
        "session_id": session_id,
        "duration_sec": round(total_time, 1),
        "initial_backlog": backlog_count,
        "samples_uploaded": samples_uploaded_total,
        "ingest_attempts": ingest_attempts,
        "ingest_successes": ingest_successes,
        "ingest_failures": ingest_failures,
        "ingest_success_rate_percent": round(success_rate, 2),
        "upload_throughput_samples_per_sec": round(throughput, 1),
        "mode_switches_count": len(mode_switches),
        "mode_switches": mode_switches,
        "heartbeat_latencies_draining": calc_stats(heartbeat_latencies_draining),
        "heartbeat_latencies_idle": calc_stats(heartbeat_latencies_idle),
    }

    print("\n================== FLAPPING BENCHMARK REPORT ==================")
    import json
    print(json.dumps(report, indent=2))
    print("===============================================================\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://mayacardio-z481.onrender.com")
    parser.add_argument("--duration", type=int, default=1200)
    parser.add_argument("--backlog", type=int, default=100000)
    args = parser.parse_args()
    run_flapping_benchmark(cloud_url=args.url, duration_sec=args.duration, backlog_count=args.backlog)
