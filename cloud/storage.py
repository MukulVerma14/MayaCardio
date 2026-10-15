"""Cloud storage layer for ingesting and serving biosignal sessions."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from core.types import LossRecord, Sample, WindowResult
from core.window_processor import WindowProcessor
from storage.schema import SCHEMA_SQL

logger = logging.getLogger("cloud.storage")


class CloudStorage:
    """Storage engine for the cloud backend.

    Supports storing samples, losses, device-side offline results,
    and online finalized window results computed by the shared core processor.
    """

    def __init__(self, db_path: str | Path = "cloud_store.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # In-memory window processors per active session
        self.processors: Dict[str, WindowProcessor] = {}

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
            conn.executescript(SCHEMA_SQL)
        finally:
            conn.close()

    def get_or_create_processor(self, session_id: str) -> WindowProcessor:
        if session_id not in self.processors:
            self.processors[session_id] = WindowProcessor(session_id=session_id, mode="online")
        return self.processors[session_id]

    def ingest_batch(
        self,
        session_id: str,
        samples: List[Sample],
        losses: List[LossRecord],
        offline_results: List[Dict[str, Any]],
        server_timestamp: float,
    ) -> Dict[int, int]:
        """Ingest samples, losses, and offline results idempotently.

        Computes online window metrics and returns the highest contiguous acked index per sensor.
        """
        conn = self._get_connection()
        proc = self.get_or_create_processor(session_id)

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Ingest samples idempotently
            for s in samples:
                ch0 = s.values[0] if len(s.values) > 0 else 0
                ch1 = s.values[1] if len(s.values) > 1 else 0
                ch2 = s.values[2] if len(s.values) > 2 else None

                conn.execute(
                    """
                    INSERT INTO samples (session_id, sensor, idx, ch0, ch1, ch2)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, sensor, idx) DO NOTHING
                    """,
                    (session_id, s.sensor, s.idx, ch0, ch1, ch2),
                )
                # Feed sample into core processor
                proc.add_sample(s.sensor, s.idx, s.values)

            # 2. Ingest losses
            for l in losses:
                det_at = l.detected_at if l.detected_at is not None else server_timestamp
                conn.execute(
                    """
                    INSERT INTO losses (session_id, sensor, from_idx, to_idx, reason, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, l.sensor, l.from_index, l.to_index, l.reason, det_at),
                )
                proc.add_loss_range(l.sensor, l.from_index, l.to_index)

            # 3. Finalize online windows that have become complete
            online_results = proc.finalize_available_windows(processed_at=server_timestamp)
            for res in online_results:
                self._save_result_row(conn, res)

            # 4. Ingest device-side offline results if any
            for r in offline_results:
                self._save_offline_result_dict(conn, r)

            conn.execute("COMMIT")

            # Compute highest contiguous index acked per sensor
            acked_cursors: Dict[int, int] = {}
            for sensor in (1, 2):
                acked_cursors[sensor] = self._get_highest_contiguous_index(conn, session_id, sensor)

            return acked_cursors

        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _save_result_row(self, conn: sqlite3.Connection, result: WindowResult) -> None:
        conn.execute(
            """
            INSERT INTO results (
                session_id, window, mode, missing_1, missing_2, complete,
                mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                magnitude, heart_rate, processed_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(session_id, window, mode) DO UPDATE SET
                missing_1=excluded.missing_1,
                missing_2=excluded.missing_2,
                complete=excluded.complete,
                mean_s1c1=excluded.mean_s1c1,
                mean_s1c2=excluded.mean_s1c2,
                mean_s1c3=excluded.mean_s1c3,
                mean_s2c1=excluded.mean_s2c1,
                mean_s2c2=excluded.mean_s2c2,
                min_s1c1=excluded.min_s1c1,
                min_s1c2=excluded.min_s1c2,
                min_s1c3=excluded.min_s1c3,
                min_s2c1=excluded.min_s2c1,
                min_s2c2=excluded.min_s2c2,
                max_s1c1=excluded.max_s1c1,
                max_s1c2=excluded.max_s1c2,
                max_s1c3=excluded.max_s1c3,
                max_s2c1=excluded.max_s2c1,
                max_s2c2=excluded.max_s2c2,
                magnitude=excluded.magnitude,
                heart_rate=excluded.heart_rate,
                processed_at=excluded.processed_at
            """,
            (
                result.session_id,
                result.window,
                result.mode,
                result.missing_1,
                result.missing_2,
                1 if result.complete else 0,
                result.mean.get("s1c1"),
                result.mean.get("s1c2"),
                result.mean.get("s1c3"),
                result.mean.get("s2c1"),
                result.mean.get("s2c2"),
                result.min.get("s1c1"),
                result.min.get("s1c2"),
                result.min.get("s1c3"),
                result.min.get("s2c1"),
                result.min.get("s2c2"),
                result.max.get("s1c1"),
                result.max.get("s1c2"),
                result.max.get("s1c3"),
                result.max.get("s2c1"),
                result.max.get("s2c2"),
                result.magnitude,
                result.heart_rate,
                result.processed_at,
            ),
        )

    def _save_offline_result_dict(self, conn: sqlite3.Connection, r: Dict[str, Any]) -> None:
        mean = r.get("mean", {})
        mins = r.get("min", {})
        maxs = r.get("max", {})

        conn.execute(
            """
            INSERT INTO results (
                session_id, window, mode, missing_1, missing_2, complete,
                mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                magnitude, heart_rate, processed_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(session_id, window, mode) DO UPDATE SET
                missing_1=excluded.missing_1,
                missing_2=excluded.missing_2,
                complete=excluded.complete,
                mean_s1c1=excluded.mean_s1c1,
                mean_s1c2=excluded.mean_s1c2,
                mean_s1c3=excluded.mean_s1c3,
                mean_s2c1=excluded.mean_s2c1,
                mean_s2c2=excluded.mean_s2c2,
                min_s1c1=excluded.min_s1c1,
                min_s1c2=excluded.min_s1c2,
                min_s1c3=excluded.min_s1c3,
                min_s2c1=excluded.min_s2c1,
                min_s2c2=excluded.min_s2c2,
                max_s1c1=excluded.max_s1c1,
                max_s1c2=excluded.max_s1c2,
                max_s1c3=excluded.max_s1c3,
                max_s2c1=excluded.max_s2c1,
                max_s2c2=excluded.max_s2c2,
                magnitude=excluded.magnitude,
                heart_rate=excluded.heart_rate,
                processed_at=excluded.processed_at
            """,
            (
                r["session_id"],
                r["window"],
                r.get("mode", "offline"),
                r["missing_1"],
                r["missing_2"],
                1 if r.get("complete") else 0,
                mean.get("s1c1"),
                mean.get("s1c2"),
                mean.get("s1c3"),
                mean.get("s2c1"),
                mean.get("s2c2"),
                mins.get("s1c1"),
                mins.get("s1c2"),
                mins.get("s1c3"),
                mins.get("s2c1"),
                mins.get("s2c2"),
                maxs.get("s1c1"),
                maxs.get("s1c2"),
                maxs.get("s1c3"),
                maxs.get("s2c1"),
                maxs.get("s2c2"),
                r.get("magnitude"),
                r.get("heart_rate"),
                r.get("processed_at"),
            ),
        )

    def _get_highest_contiguous_index(self, conn: sqlite3.Connection, session_id: str, sensor: int) -> int:
        """Find the highest sample index such that all indices 0..max are received or lost."""
        # For simplicity and performance, find the maximum index present
        row = conn.execute(
            "SELECT MAX(idx) AS max_idx FROM samples WHERE session_id=? AND sensor=?",
            (session_id, sensor),
        ).fetchone()
        return row["max_idx"] if (row and row["max_idx"] is not None) else -1

    def get_status(self) -> Dict[str, Any]:
        conn = self._get_connection()
        try:
            total_samples = conn.execute("SELECT COUNT(*) AS total FROM samples").fetchone()["total"]
            total_windows = conn.execute("SELECT COUNT(*) AS total FROM results").fetchone()["total"]
            sessions = [r["session_id"] for r in conn.execute("SELECT DISTINCT session_id FROM samples").fetchall()]
            return {
                "sessions": sessions,
                "total_samples": total_samples,
                "processed_windows": total_windows,
            }
        finally:
            conn.close()

    def export_samples_stream(self, session_id: str) -> Generator[str, None, None]:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT sensor, idx, ch0, ch1, ch2
                FROM samples
                WHERE session_id=?
                ORDER BY idx ASC, sensor ASC
                """,
                (session_id,),
            )
            for r in cur:
                vals = [r["ch0"], r["ch1"]]
                if r["ch2"] is not None:
                    vals.append(r["ch2"])
                obj = {
                    "sensor": r["sensor"],
                    "index": r["idx"],
                    "values": vals,
                }
                yield json.dumps(obj) + "\n"

            cur_l = conn.execute(
                """
                SELECT sensor, from_idx, to_idx, reason
                FROM losses
                WHERE session_id=?
                ORDER BY from_idx ASC, sensor ASC
                """,
                (session_id,),
            )
            for r in cur_l:
                obj = {
                    "sensor": r["sensor"],
                    "from_index": r["from_idx"],
                    "to_index": r["to_idx"],
                    "reason": r["reason"],
                }
                yield json.dumps(obj) + "\n"
        finally:
            conn.close()

    def export_results_stream(self, session_id: str) -> Generator[str, None, None]:
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT * FROM results
                WHERE session_id=?
                ORDER BY window ASC, mode ASC
                """,
                (session_id,),
            )
            for r in cur:
                obj = {
                    "session_id": r["session_id"],
                    "window": r["window"],
                    "mode": r["mode"],
                    "missing_1": r["missing_1"],
                    "missing_2": r["missing_2"],
                    "complete": bool(r["complete"]),
                    "mean": {
                        "s1c1": r["mean_s1c1"],
                        "s1c2": r["mean_s1c2"],
                        "s1c3": r["mean_s1c3"],
                        "s2c1": r["mean_s2c1"],
                        "s2c2": r["mean_s2c2"],
                    },
                    "min": {
                        "s1c1": r["min_s1c1"],
                        "s1c2": r["min_s1c2"],
                        "s1c3": r["min_s1c3"],
                        "s2c1": r["min_s2c1"],
                        "s2c2": r["min_s2c2"],
                    },
                    "max": {
                        "s1c1": r["max_s1c1"],
                        "s1c2": r["max_s1c2"],
                        "s1c3": r["max_s1c3"],
                        "s2c1": r["max_s2c1"],
                        "s2c2": r["max_s2c2"],
                    },
                    "magnitude": r["magnitude"],
                    "heart_rate": r["heart_rate"],
                    "processed_at": r["processed_at"],
                }
                yield json.dumps(obj) + "\n"
        finally:
            conn.close()
