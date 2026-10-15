"""Spec-compliant Biosignal Mock Simulator for development and testing.

NOTE: This is a dev simulator written for the hiring case assessment to emulate
the official 'biosignal-mock' binary. It must be clearly identified as a
simulator and never claimed to be the official mock.

Implements:
- 400 Hz sampling for Sensor 1 (3 channels) and Sensor 2 (2 channels).
- Modes: 'signal' (60 -> 90 -> 72 bpm cycle) and 'ramp'.
- Newline-delimited JSON over TCP, one client at a time.
- 10-second ring buffer (4000 samples) per sensor.
- Exact CRC-32 computed over the raw 'data' array byte slice.
- Support for 'from_index' re-reads and 'lost' ranges on buffer overflow.
- Ground truth file export for validation.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
import uuid
import zlib
from typing import Any, Dict, List, Optional, Tuple

SAMPLES_PER_SECOND = 400
BUFFER_SECONDS = 10
BUFFER_SIZE = BUFFER_SECONDS * SAMPLES_PER_SECOND  # 4000 samples
MOD_24BIT = 8388608  # 2^23


class BiosignalGenerator:
    """Generates synthetic signals for Sensor 1 and Sensor 2."""

    def __init__(self, mode: str = "signal", seed: int = 7) -> None:
        self.mode = mode
        self.seed = seed

    def sample_sensor1(self, idx: int) -> List[int]:
        if self.mode == "ramp":
            return [(3 * idx + c) % MOD_24BIT for c in range(3)]

        # Signal mode: ECG-like signal with heart rate cycle:
        # Minutes 0-2 (0..120s): 60 bpm
        # Minutes 2-4 (120..240s): 90 bpm
        # Minutes 4-6 (240..360s): 72 bpm
        # repeating every 6 minutes (360 seconds = 144,000 samples)
        t_sec = idx / float(SAMPLES_PER_SECOND)
        cycle_sec = t_sec % 360.0

        if cycle_sec < 120.0:
            bpm = 60.0
        elif cycle_sec < 240.0:
            bpm = 90.0
        else:
            bpm = 72.0

        # Heartbeat gap in samples
        gap = SAMPLES_PER_SECOND * 60.0 / bpm
        phase = (idx % gap)

        baseline = 200
        peak_val = 1500
        # QRS peak pattern
        if abs(phase - 0.0) < 0.5:
            ch2 = peak_val
        elif abs(phase - 1.0) < 0.5:
            ch2 = int(baseline + (peak_val - baseline) * 0.35)
        elif abs(phase - (gap - 1.0)) < 0.5:
            ch2 = int(baseline + (peak_val - baseline) * 0.45)
        else:
            # Baseline with mild respiratory variation
            ch2 = int(baseline + 20 * math.sin(2 * math.pi * t_sec * 0.2))

        ch1 = int(round(ch2 * 0.6))
        ch3 = int(round(ch2 * 1.3))
        return [ch1, ch2, ch3]

    def sample_sensor2(self, idx: int) -> List[int]:
        if self.mode == "ramp":
            return [(2 * idx + c) % MOD_24BIT for c in range(2)]

        # Sensor 2: ch1 ~ 400,000, ch2 ~ 150,000, slowly varying
        t_sec = idx / float(SAMPLES_PER_SECOND)
        ch1 = int(400000 + 5000 * math.sin(2 * math.pi * t_sec * 0.05))
        ch2 = int(150000 + 3000 * math.cos(2 * math.pi * t_sec * 0.07))
        return [ch1, ch2]


class MockServer:
    """TCP Server implementing the biosignal-mock protocol."""

    def __init__(
        self,
        mode: str = "signal",
        seed: int = 7,
        duration: int = 3600,
        port: int = 7100,
        host: str = "127.0.0.1",
        ground_truth_path: Optional[str] = None,
        corrupt_crc_prob: float = 0.0,
        duplicate_prob: float = 0.0,
    ) -> None:
        self.mode = mode
        self.seed = seed
        self.duration = duration
        self.port = port
        self.host = host
        self.ground_truth_path = ground_truth_path
        self.corrupt_crc_prob = corrupt_crc_prob
        self.duplicate_prob = duplicate_prob

        self.session_id = uuid.uuid4().hex[:8]
        self.generator = BiosignalGenerator(mode=mode, seed=seed)
        self.start_monotonic: float = 0.0
        self.running: bool = False
        self.server_sock: Optional[socket.socket] = None

        # Per-sensor client read cursors: sensor -> next_index_to_deliver
        self.client_cursors: Dict[int, int] = {1: 0, 2: 0}
        self.lock = threading.Lock()

    def get_current_max_sample_index(self) -> int:
        """Calculate the latest generated sample index based on elapsed monotonic time."""
        if not self.running:
            return 0
        elapsed = time.monotonic() - self.start_monotonic
        max_idx = int(elapsed * SAMPLES_PER_SECOND)
        total_limit = self.duration * SAMPLES_PER_SECOND
        return min(max_idx, total_limit)

    def start(self, blocking: bool = True) -> None:
        self.running = True
        self.start_monotonic = time.monotonic()
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(1)
        print(f"[mock_sim] Started biosignal mock simulator (session={self.session_id}, mode={self.mode}, port={self.port})")

        if self.ground_truth_path:
            self._write_ground_truth_header()

        if blocking:
            self._serve_loop()
        else:
            t = threading.Thread(target=self._serve_loop, daemon=True)
            t.start()

    def stop(self) -> None:
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _serve_loop(self) -> None:
        while self.running:
            try:
                conn, addr = self.server_sock.accept()
            except Exception:
                break
            print(f"[mock_sim] Client connected from {addr}")
            self._handle_client(conn)

    def _handle_client(self, conn: socket.socket) -> None:
        conn_file = conn.makefile("rb")
        try:
            while self.running:
                line = conn_file.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8").strip())
                except Exception:
                    continue

                if req.get("cmd") == "read":
                    sensor = int(req.get("sensor", 1))
                    from_index = req.get("from_index")
                    reply_bytes = self._build_reply(sensor, from_index)
                    conn.sendall(reply_bytes)
        except Exception as e:
            print(f"[mock_sim] Connection error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _build_reply(self, sensor: int, from_index: Optional[int]) -> bytes:
        with self.lock:
            max_avail_idx = self.get_current_max_sample_index()
            # 10s ring buffer earliest index
            earliest_in_buffer = max(0, max_avail_idx - BUFFER_SIZE)

            cursor = self.client_cursors.get(sensor, 0)
            if from_index is not None:
                req_idx = int(from_index)
            else:
                req_idx = cursor

            lost_ranges = []

            if req_idx < earliest_in_buffer:
                # Buffer overflow: the mock discarded older samples
                lost_ranges.append({
                    "from_index": req_idx,
                    "to_index": earliest_in_buffer - 1,
                    "reason": "buffer_overflow",
                })
                req_idx = earliest_in_buffer

            # Deliver at most 1 second of data (400 samples)
            count = min(SAMPLES_PER_SECOND, max(0, max_avail_idx - req_idx))
            end_idx = req_idx + count

            data: List[List[int]] = []
            if sensor == 1:
                for i in range(req_idx, end_idx):
                    data.append(self.generator.sample_sensor1(i))
            else:
                for i in range(req_idx, end_idx):
                    data.append(self.generator.sample_sensor2(i))

            # Advance cursor
            self.client_cursors[sensor] = end_idx

            is_session_end = (end_idx >= self.duration * SAMPLES_PER_SECOND)

            # CRITICAL PROTOCOL RULE:
            # crc32 = CRC-32 (zlib) of the data text EXACTLY AS SENT,
            # from the "[" to the matching "]" of the data field.
            data_json_str = json.dumps(data, separators=(",", ":"))
            data_bytes = data_json_str.encode("utf-8")
            crc_val = zlib.crc32(data_bytes) & 0xFFFFFFFF
            crc_hex = f"{crc_val:08x}"

            # Fault injection: corrupt CRC
            if self.corrupt_crc_prob > 0:
                import random
                if random.random() < self.corrupt_crc_prob:
                    crc_hex = "deadbeef"

            reply_dict = {
                "session_id": self.session_id,
                "sensor": sensor,
                "first_index": req_idx,
                "count": count,
                "lost": lost_ranges,
                "session_end": is_session_end,
                "crc32": crc_hex,
            }

            # Assemble JSON line with raw data_json_str to guarantee byte match
            prefix = json.dumps(reply_dict, separators=(",", ":"))[:-1]  # strip trailing '}'
            full_line = f"{prefix},\"data\":{data_json_str}}}\n".encode("utf-8")

            if self.ground_truth_path and count > 0:
                self._record_ground_truth(sensor, req_idx, data, lost_ranges)

            return full_line

    def _write_ground_truth_header(self) -> None:
        try:
            with open(self.ground_truth_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "header",
                    "session_id": self.session_id,
                    "mode": self.mode,
                    "seed": self.seed,
                    "duration": self.duration,
                }) + "\n")
        except Exception as e:
            print(f"[mock_sim] Failed to write ground truth header: {e}")

    def _record_ground_truth(
        self,
        sensor: int,
        first_idx: int,
        data: List[List[int]],
        lost: List[Dict[str, Any]],
    ) -> None:
        try:
            with open(self.ground_truth_path, "a", encoding="utf-8") as f:
                for offset, vals in enumerate(data):
                    f.write(json.dumps({
                        "sensor": sensor,
                        "index": first_idx + offset,
                        "values": vals,
                    }) + "\n")
                for l in lost:
                    f.write(json.dumps({
                        "sensor": sensor,
                        "from_index": l["from_index"],
                        "to_index": l["to_index"],
                        "reason": l["reason"],
                    }) + "\n")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Biosignal Mock Simulator (Dev Tool)")
    parser.add_argument("--mode", choices=["signal", "ramp"], default="signal", help="Signal generation mode")
    parser.add_argument("--seed", type=int, default=7, help="Random seed")
    parser.add_argument("--duration", type=int, default=3600, help="Session duration in seconds")
    parser.add_argument("--port", type=int, default=7100, help="TCP port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to")
    parser.add_argument("--ground-truth-log", dest="ground_truth", default=None, help="File to write ground truth data")

    args = parser.parse_args()
    server = MockServer(
        mode=args.mode,
        seed=args.seed,
        duration=args.duration,
        port=args.port,
        host=args.host,
        ground_truth_path=args.ground_truth,
    )
    try:
        server.start(blocking=True)
    except KeyboardInterrupt:
        print("\n[mock_sim] Stopped by user")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
