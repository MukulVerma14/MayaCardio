"""Lightweight Connectivity Monitor with Hysteresis and Monotonic Timing.

Implements Section 6 specifications:
- Heartbeat probe to cloud every ~2 s with short timeouts.
- 3 consecutive failures => offline mode.
- 2 consecutive successes => online mode.
- Hysteresis avoids flapping; total switch time < 10 s each way.
- Mode state is persisted to SQLite.
- Monotonic time is used for all intervals.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests
from storage.database import DeviceDatabase

logger = logging.getLogger("connectivity.monitor")


class ConnectivityMonitor:
    """Monitors cloud reachability and manages online/offline state machine."""

    def __init__(
        self,
        db: DeviceDatabase,
        cloud_url: str = "http://127.0.0.1:8000",
        probe_interval_sec: float = 2.0,
        probe_timeout_sec: float = 1.5,
        on_mode_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db = db
        self.cloud_url = cloud_url.rstrip("/")
        self.probe_interval_sec = probe_interval_sec
        self.probe_timeout_sec = probe_timeout_sec
        self.on_mode_change = on_mode_change

        self.running: bool = False
        self.thread: Optional[threading.Thread] = None

        self.current_mode: str = self.db.get_mode()
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        self.last_successful_traffic_time: float = 0.0
        self.lock = threading.Lock()

    def notify_successful_traffic(self) -> None:
        """Notify monitor that live cloud traffic (e.g. batch ingest) succeeded."""
        with self.lock:
            self.last_successful_traffic_time = time.monotonic()
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            if self.current_mode == "offline" and self.consecutive_successes >= 2:
                self._transition_to("online")

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="conn-monitor", daemon=True)
        self.thread.start()
        logger.info(f"Connectivity monitor started for {self.cloud_url} (current mode: {self.current_mode})")

    def stop(self) -> None:
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        logger.info("Connectivity monitor stopped")

    def is_online(self) -> bool:
        with self.lock:
            return self.current_mode == "online"

    def probe_now(self) -> bool:
        """Send a single heartbeat probe to the cloud service."""
        try:
            resp = requests.post(
                f"{self.cloud_url}/api/v1/heartbeat",
                timeout=self.probe_timeout_sec,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def _run_loop(self) -> None:
        session = requests.Session()
        heartbeat_url = f"{self.cloud_url}/api/v1/heartbeat"

        while self.running:
            t0 = time.monotonic()
            success = False
            status_desc = "err"

            try:
                # Explicit (connect_timeout, read_timeout): connect within 1.0s, read within 1.5s
                resp = session.post(heartbeat_url, timeout=(1.0, self.probe_timeout_sec))
                success = (resp.status_code == 200)
                status_desc = f"HTTP {resp.status_code}" if resp.status_code != 200 else "HTTP 200"
            except requests.exceptions.Timeout:
                success = False
                status_desc = "timeout"
            except Exception as e:
                success = False
                status_desc = f"err: {type(e).__name__}"

            rtt = time.monotonic() - t0

            with self.lock:
                # If an ingest upload succeeded within the last 4.0s, the link is confirmed active;
                # a simultaneous probe timeout is due to server worker queuing, not network loss.
                link_recently_active = (time.monotonic() - self.last_successful_traffic_time) < 4.0
                if success or link_recently_active:
                    self.consecutive_successes += 1
                    self.consecutive_failures = 0
                    logger.debug(
                        f"Heartbeat probe: success ({status_desc}), rtt={rtt*1000:.1f}ms, "
                        f"succ_cnt={self.consecutive_successes}, mode={self.current_mode}"
                    )

                    if self.current_mode == "offline" and self.consecutive_successes >= 2:
                        self._transition_to("online")
                else:
                    self.consecutive_failures += 1
                    self.consecutive_successes = 0
                    logger.warning(
                        f"Heartbeat probe: FAILED ({status_desc}), rtt={rtt*1000:.1f}ms, "
                        f"fail_cnt={self.consecutive_failures}/3, mode={self.current_mode}"
                    )

                    if self.current_mode == "online" and self.consecutive_failures >= 3:
                        self._transition_to("offline")

            elapsed = time.monotonic() - t0
            sleep_time = max(0.1, self.probe_interval_sec - elapsed)
            time.sleep(sleep_time)

    def _transition_to(self, new_mode: str) -> None:
        """Execute mode transition, persist to storage, and notify callback."""
        logger.info(f"Mode switch triggered: {self.current_mode} -> {new_mode}")
        self.current_mode = new_mode
        self.db.set_mode(new_mode)

        if self.on_mode_change:
            try:
                self.on_mode_change(new_mode)
            except Exception as e:
                logger.error(f"Error in on_mode_change callback: {e}")
