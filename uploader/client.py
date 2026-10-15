"""Dual-channel Ack-based Uploader for live streaming and rapid backlog catch-up."""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from connectivity.monitor import ConnectivityMonitor
from core.types import LossRecord, Sample
from storage.database import DeviceDatabase

logger = logging.getLogger("uploader.client")

LIVE_BATCH_INTERVAL_SEC = 0.5
BACKLOG_BATCH_SIZE = 2500  # Up to 2500 samples per backlog request


class UploaderClient:
    """Manages transmission of samples, losses, and offline results to the cloud.

    Prioritizes live data to keep latency < 5s while draining backlog
    at sustained rates > 6,500 samples/s to meet the 15-minute catch-up target.
    """

    def __init__(
        self,
        db: DeviceDatabase,
        conn_monitor: ConnectivityMonitor,
        cloud_url: str = "http://127.0.0.1:8000",
        request_timeout_sec: float = 5.0,
    ) -> None:
        self.db = db
        self.conn_monitor = conn_monitor
        self.cloud_url = cloud_url.rstrip("/")
        self.request_timeout_sec = request_timeout_sec

        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.http_session = requests.Session()

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="uploader-loop", daemon=True)
        self.thread.start()
        logger.info(f"Uploader started streaming to {self.cloud_url}")

    def stop(self) -> None:
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        logger.info("Uploader stopped")

    def _run_loop(self) -> None:
        while self.running:
            if not self.conn_monitor.is_online():
                time.sleep(0.5)
                continue

            try:
                # 1. Sync any pending offline results first
                self._sync_offline_results()

                # 2. Upload pending samples & losses
                uploaded_count = self._upload_batch_step()

                # If there is a large backlog, yield briefly without long sleeping
                # to sustain high throughput (> 6,500 samples/sec)
                if uploaded_count > 0:
                    time.sleep(0.01)
                else:
                    time.sleep(LIVE_BATCH_INTERVAL_SEC)

            except Exception as e:
                logger.warning(f"Upload iteration error: {e}")
                time.sleep(0.5)

    def _upload_batch_step(self) -> int:
        """Fetch next unacked samples and losses across sensors, gzip-compress, and send."""
        # Find active session from cursors
        cur1 = self.db.get_cursor(1)
        cur2 = self.db.get_cursor(2)
        session_id = cur1[0] if cur1 else (cur2[0] if cur2 else None)

        if not session_id:
            return 0

        # Gather unacked samples for sensor 1 and sensor 2
        s1_samples = self.db.get_unacked_samples(session_id, sensor=1, limit=BACKLOG_BATCH_SIZE)
        s2_samples = self.db.get_unacked_samples(session_id, sensor=2, limit=BACKLOG_BATCH_SIZE)

        if not s1_samples and not s2_samples:
            return 0

        all_samples = s1_samples + s2_samples

        # Gather losses
        s1_losses = self.db.get_unacked_losses(session_id, sensor=1, limit=200)
        s2_losses = self.db.get_unacked_losses(session_id, sensor=2, limit=200)
        all_losses = s1_losses + s2_losses

        # Build ingestion payload
        payload = {
            "session_id": session_id,
            "samples": [
                {"sensor": s.sensor, "index": s.idx, "values": s.values}
                for s in all_samples
            ],
            "losses": [
                {
                    "sensor": l.sensor,
                    "from_index": l.from_index,
                    "to_index": l.to_index,
                    "reason": l.reason,
                    "detected_at": l.detected_at,
                }
                for l in all_losses
            ],
            "offline_results": [],
        }

        # Gzip compress payload
        json_bytes = json.dumps(payload).encode("utf-8")
        compressed_body = gzip.compress(json_bytes)

        headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }

        resp = self.http_session.post(
            f"{self.cloud_url}/api/v1/ingest",
            data=compressed_body,
            headers=headers,
            timeout=self.request_timeout_sec,
        )

        if resp.status_code == 200:
            res_data = resp.json()
            acked_cursors = res_data.get("acked_cursors", {})

            # Advance local cursors on confirmation
            for sensor_str, ack_idx in acked_cursors.items():
                sensor = int(sensor_str)
                if ack_idx >= 0:
                    self.db.advance_acked_cursor(sensor, session_id, int(ack_idx))

            return len(all_samples)
        else:
            logger.warning(f"Cloud ingestion returned HTTP {resp.status_code}: {resp.text}")
            return 0

    def _sync_offline_results(self) -> None:
        """Upload any locally computed offline results to the cloud."""
        cur1 = self.db.get_cursor(1)
        session_id = cur1[0] if cur1 else None
        if not session_id:
            return

        offline_results = self.db.get_offline_results_to_sync(session_id, limit=100)
        if not offline_results:
            return

        payload = {
            "session_id": session_id,
            "samples": [],
            "losses": [],
            "offline_results": [r.to_dict() for r in offline_results],
        }

        json_bytes = json.dumps(payload).encode("utf-8")
        compressed_body = gzip.compress(json_bytes)

        headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }

        try:
            self.http_session.post(
                f"{self.cloud_url}/api/v1/ingest",
                data=compressed_body,
                headers=headers,
                timeout=self.request_timeout_sec,
            )
        except Exception as e:
            logger.warning(f"Failed to sync offline results: {e}")
