"""Device-side offline window processor.

Computes window metrics locally on the device using the shared core logic,
storing results with mode='offline' in SQLite.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

from core.types import Sample
from core.window_processor import WindowProcessor
from storage.database import DeviceDatabase

logger = logging.getLogger("storage.offline_processor")


class DeviceOfflineProcessor:
    """Continuously evaluates finalized windows on the device."""

    def __init__(
        self,
        db: DeviceDatabase,
        poll_interval_sec: float = 0.5,
    ) -> None:
        self.db = db
        self.poll_interval_sec = poll_interval_sec

        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.processors: Dict[str, WindowProcessor] = {}

        # Track last processed index per sensor to avoid re-reading everything
        self.last_fed_idx: Dict[Tuple[str, int], int] = {}

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="offline-processor", daemon=True)
        self.thread.start()
        logger.info("Device offline processor started")

    def stop(self) -> None:
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        logger.info("Device offline processor stopped")

    def _get_processor(self, session_id: str) -> WindowProcessor:
        if session_id not in self.processors:
            self.processors[session_id] = WindowProcessor(session_id=session_id, mode="offline")
        return self.processors[session_id]

    def _run_loop(self) -> None:
        while self.running:
            try:
                self.process_step()
            except Exception as e:
                logger.warning(f"Error in offline processor step: {e}")
            time.sleep(self.poll_interval_sec)

    def process_step(self) -> int:
        """Fetch newly arrived samples and finalize any completed windows.

        Returns:
            Number of newly finalized windows.
        """
        cur1 = self.db.get_cursor(1)
        cur2 = self.db.get_cursor(2)
        session_id = cur1[0] if cur1 else (cur2[0] if cur2 else None)

        if not session_id:
            return 0

        proc = self._get_processor(session_id)

        # Feed new samples for Sensor 1 and Sensor 2
        for sensor in (1, 2):
            last_fed = self.last_fed_idx.get((session_id, sensor), -1)
            conn = self.db._get_connection()
            try:
                rows = conn.execute(
                    """
                    SELECT idx, ch0, ch1, ch2
                    FROM samples
                    WHERE session_id=? AND sensor=? AND idx > ?
                    ORDER BY idx ASC
                    LIMIT 2000
                    """,
                    (session_id, sensor, last_fed),
                ).fetchall()

                for r in rows:
                    vals = [r["ch0"], r["ch1"]]
                    if r["ch2"] is not None:
                        vals.append(r["ch2"])
                    proc.add_sample(sensor, r["idx"], vals)
                    if r["idx"] > last_fed:
                        last_fed = r["idx"]

                # Feed loss records
                losses = conn.execute(
                    """
                    SELECT sensor, from_idx, to_idx
                    FROM losses
                    WHERE session_id=? AND sensor=?
                    """,
                    (session_id, sensor),
                ).fetchall()
                for l in losses:
                    proc.add_loss_range(l["sensor"], l["from_idx"], l["to_idx"])

                self.last_fed_idx[(session_id, sensor)] = last_fed
            finally:
                conn.close()

        # Finalize available windows
        new_results = proc.finalize_available_windows(processed_at=time.time())
        for res in new_results:
            self.db.save_window_result(res)

        return len(new_results)
