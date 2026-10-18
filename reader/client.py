"""Robust Reader Client polling biosignal-mock and persisting directly to SQLite."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import socket
import threading
import time
from typing import Dict, List, Optional, Tuple

from core.types import LossRecord, Sample
from reader.protocol import parse_reply_line
from storage.database import DeviceDatabase

logger = logging.getLogger("reader.client")


class ReaderClient:
    """Continuous polling client for biosignal-mock.

    Polls Sensor 1 and Sensor 2 independently, validates CRC-32 on raw bytes,
    handles re-reads, mock restarts, buffer overflows, and writes directly
    to local SQLite in single atomic transactions.
    """

    def __init__(
        self,
        db: DeviceDatabase,
        mock_host: str = "127.0.0.1",
        mock_port: int = 7100,
        poll_interval_sec: float = 0.25,
        socket_timeout_sec: float = 2.0,
    ) -> None:
        self.db = db
        self.mock_host = mock_host
        self.mock_port = mock_port
        self.poll_interval_sec = poll_interval_sec
        self.socket_timeout_sec = socket_timeout_sec

        self.running: bool = False
        self.thread: Optional[threading.Thread] = None

        # Per-sensor state
        self.current_session_id: Optional[str] = None
        self.session_identified: bool = False
        self.expected_indices: Dict[int, int] = {1: 0, 2: 0}
        self.consecutive_crc_failures: Dict[int, int] = {1: 0, 2: 0}

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="reader-loop", daemon=True)
        self.thread.start()
        logger.info(f"Reader started connecting to {self.mock_host}:{self.mock_port}")

    def stop(self) -> None:
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        logger.info("Reader stopped")

    def _run_loop(self) -> None:
        """Main polling loop with automatic reconnect and error recovery."""
        while self.running:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.socket_timeout_sec)
                sock.connect((self.mock_host, self.mock_port))
                logger.info(f"Connected to mock at {self.mock_host}:{self.mock_port}")

                sock_file = sock.makefile("rb")

                # Tight loop polling sensor 1 and sensor 2
                while self.running:
                    t_start = time.monotonic()

                    self._poll_sensor(1, sock, sock_file)
                    self._poll_sensor(2, sock, sock_file)

                    elapsed = time.monotonic() - t_start
                    sleep_time = max(0.01, self.poll_interval_sec - elapsed)
                    time.sleep(sleep_time)

            except (ConnectionRefusedError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError) as e:
                logger.warning(f"Connection to mock failed ({e}), retrying in 0.5s...")
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Unexpected reader exception: {e}", exc_info=True)
                time.sleep(0.5)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _poll_sensor(self, sensor: int, sock: socket.socket, sock_file: Any) -> None:
        """Send read request for a sensor, receive reply, verify CRC, and persist."""
        # Only send from_index if session is confirmed and we have a valid index
        if self.session_identified and self.expected_indices[sensor] > 0:
            req = {"cmd": "read", "sensor": sensor, "from_index": self.expected_indices[sensor]}
        else:
            req = {"cmd": "read", "sensor": sensor}

        req_bytes = (json.dumps(req) + "\n").encode("utf-8")
        try:
            sock.sendall(req_bytes)
            raw_line = sock_file.readline()
        except Exception as e:
            logger.warning(f"Failed to communicate with mock for sensor {sensor}: {e}")
            raise

        if not raw_line:
            raise ConnectionResetError("Mock closed connection")

        reply_dict, crc_valid = parse_reply_line(raw_line)
        if reply_dict is None:
            logger.warning(f"Corrupt JSON received for sensor {sensor}")
            return

        # CRC Verification
        if not crc_valid:
            self.consecutive_crc_failures[sensor] += 1
            logger.warning(
                f"Corrupt CRC received for sensor {sensor} at expected index {self.expected_indices[sensor]} "
                f"(consecutive failures: {self.consecutive_crc_failures[sensor]})"
            )
            return

        self.consecutive_crc_failures[sensor] = 0

        # Process valid reply
        session_id = str(reply_dict.get("session_id", ""))
        first_index = int(reply_dict.get("first_index", 0))
        count = int(reply_dict.get("count", 0))
        mock_lost = reply_dict.get("lost", [])
        is_session_end = bool(reply_dict.get("session_end", False))
        data_rows = reply_dict.get("data", [])

        # Session transition or initial identification
        if not self.session_identified or self.current_session_id is None:
            # First reply since startup: identify session and load per-(session, sensor) cursors
            self.session_identified = True
            self.current_session_id = session_id
            for s in (1, 2):
                cursor = self.db.get_cursor(s, session_id=session_id)
                self.expected_indices[s] = max(0, cursor[1] + 1) if cursor is not None else 0
            logger.info(
                f"Identified mock session {session_id}. Initialized cursors: "
                f"sensor 1={self.expected_indices[1]}, sensor 2={self.expected_indices[2]}"
            )

        elif session_id != self.current_session_id:
            # Mock restarted mid-execution
            logger.warning(
                f"Mock restarted! Session changed from {self.current_session_id} to {session_id}. "
                f"Recording unread old data loss with to_index=null."
            )
            for s in (1, 2):
                self.db.record_loss(
                    session_id=self.current_session_id,
                    sensor=s,
                    from_idx=self.expected_indices[s],
                    to_idx=None,
                    reason="mock_restart_session_change",
                )
                self.expected_indices[s] = 0
            self.current_session_id = session_id
            logger.info(f"Reset cursors to 0 for new session {session_id}")

        exp_idx = self.expected_indices[sensor]

        # Prepare loss records
        loss_records: List[LossRecord] = []

        # 1. Losses reported explicitly by the mock (e.g. buffer_overflow)
        for ml in mock_lost:
            loss_records.append(
                LossRecord(
                    sensor=sensor,
                    from_index=int(ml["from_index"]),
                    to_index=int(ml["to_index"]) if ml.get("to_index") is not None else None,
                    reason=str(ml.get("reason", "mock_reported_loss")),
                )
            )

        # 2. Pipeline-detected gaps: if mock returned first_index > expected_index
        # and that gap was not completely accounted for in mock_lost
        if first_index > exp_idx:
            accounted_up_to = exp_idx - 1
            for ml in mock_lost:
                if int(ml["from_index"]) <= exp_idx:
                    t_idx = ml.get("to_index")
                    if t_idx is not None and int(t_idx) > accounted_up_to:
                        accounted_up_to = int(t_idx)

            if accounted_up_to < first_index - 1:
                gap_start = accounted_up_to + 1
                gap_end = first_index - 1
                reason = "not_read_before_pipeline_start" if exp_idx == 0 else "gap_detected_unread"
                loss_records.append(
                    LossRecord(
                        sensor=sensor,
                        from_index=gap_start,
                        to_index=gap_end,
                        reason=reason,
                    )
                )

        # Convert data rows into Sample instances, filtering out any samples already persisted
        samples: List[Sample] = []
        for offset, vals in enumerate(data_rows):
            sample_idx = first_index + offset
            if sample_idx >= exp_idx:
                samples.append(Sample(sensor=sensor, idx=sample_idx, values=vals))

        # Atomic commit to local SQLite
        next_read_idx = max(exp_idx, first_index + count)
        self.db.save_reply_batch(
            session_id=session_id,
            sensor=sensor,
            samples=samples,
            losses=loss_records,
            next_read_idx=next_read_idx,
        )

        # Advance local expected index
        self.expected_indices[sensor] = next_read_idx
