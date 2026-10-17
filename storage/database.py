"""Durable SQLite storage engine for the MayaCardio bedside device."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from core.rounding import round_metric
from core.types import LossRecord, Sample, WindowResult
from storage.schema import SCHEMA_SQL

logger = logging.getLogger("storage.database")

EMERGENCY_RESERVE_SIZE = 512 * 1024  # 512 KB pre-allocated emergency margin


class DeviceDatabase:
    """Thread-safe, crash-resilient SQLite database engine with WAL and FULL sync."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.reserve_path = self.db_path.with_suffix(".emergency_reserve")

        self._ensure_emergency_reserve()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # Autocommit mode; we manage explicit transactions
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        try:
            conn.executescript(SCHEMA_SQL)
        finally:
            conn.close()

    def _ensure_emergency_reserve(self) -> None:
        """Create a pre-reserved space file so emergency loss records can be saved on ENOSPC."""
        try:
            if not self.reserve_path.exists():
                with open(self.reserve_path, "wb") as f:
                    f.write(b"\0" * EMERGENCY_RESERVE_SIZE)
        except Exception as e:
            logger.warning(f"Could not create emergency reserve file: {e}")

    def _release_emergency_reserve(self) -> None:
        """Release the emergency reserve to allow disk-full loss records to be committed."""
        try:
            if self.reserve_path.exists():
                self.reserve_path.unlink()
                logger.warning("Emergency reserve file released due to ENOSPC")
        except Exception:
            pass

    def save_reply_batch(
        self,
        session_id: str,
        sensor: int,
        samples: List[Sample],
        losses: List[LossRecord],
        next_read_idx: int,
    ) -> Tuple[int, int]:
        """Save a batch of samples and loss records in a single atomic transaction.

        Returns:
            Tuple of (accepted_samples_count, rejected_duplicates_count).
        """
        conn = self._get_connection()
        accepted_count = 0
        rejected_count = 0
        now = time.time()

        try:
            conn.execute("BEGIN IMMEDIATE")

            # Insert samples
            for s in samples:
                ch0 = s.values[0] if len(s.values) > 0 else 0
                ch1 = s.values[1] if len(s.values) > 1 else 0
                ch2 = s.values[2] if len(s.values) > 2 else None

                # Query existing sample to detect conflicting duplicates
                cur = conn.execute(
                    "SELECT ch0, ch1, ch2 FROM samples WHERE session_id=? AND sensor=? AND idx=?",
                    (session_id, sensor, s.idx),
                )
                row = cur.fetchone()
                if row is not None:
                    # Duplicate check
                    if row["ch0"] == ch0 and row["ch1"] == ch1 and row["ch2"] == ch2:
                        rejected_count += 1
                        continue
                    else:
                        logger.error(
                            f"Conflicting duplicate detected at session={session_id}, sensor={sensor}, idx={s.idx}: "
                            f"existing=({row['ch0']},{row['ch1']},{row['ch2']}) incoming=({ch0},{ch1},{ch2})"
                        )
                        rejected_count += 1
                        continue

                conn.execute(
                    "INSERT INTO samples (session_id, sensor, idx, ch0, ch1, ch2) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, sensor, s.idx, ch0, ch1, ch2),
                )
                accepted_count += 1

            # Insert losses
            for l in losses:
                det_at = l.detected_at if l.detected_at is not None else now
                conn.execute(
                    "INSERT INTO losses (session_id, sensor, from_idx, to_idx, reason, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, sensor, l.from_index, l.to_index, l.reason, det_at),
                )

            # Update reader cursor
            cursor_idx = next_read_idx - 1 if next_read_idx > 0 else 0
            conn.execute(
                """
                INSERT INTO cursors (sensor, session_id, last_read_idx, last_acked_idx, updated_at)
                VALUES (?, ?, ?, -1, ?)
                ON CONFLICT(sensor) DO UPDATE SET
                    last_read_idx = CASE WHEN cursors.session_id = excluded.session_id THEN MAX(cursors.last_read_idx, excluded.last_read_idx) ELSE excluded.last_read_idx END,
                    last_acked_idx = CASE WHEN cursors.session_id = excluded.session_id THEN cursors.last_acked_idx ELSE -1 END,
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (sensor, session_id, cursor_idx, now),
            )

            conn.execute("COMMIT")
            return accepted_count, rejected_count

        except sqlite3.OperationalError as e:
            conn.execute("ROLLBACK")
            if "disk is full" in str(e).lower() or "no space" in str(e).lower():
                self._release_emergency_reserve()
                self.record_loss(
                    session_id=session_id,
                    sensor=sensor,
                    from_idx=samples[0].idx if samples else next_read_idx,
                    to_idx=samples[-1].idx if samples else None,
                    reason="disk_full",
                )
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_loss(
        self,
        session_id: str,
        sensor: int,
        from_idx: int,
        to_idx: Optional[int],
        reason: str,
        detected_at: Optional[float] = None,
    ) -> None:
        """Durable record of a sample loss."""
        conn = self._get_connection()
        det_at = detected_at if detected_at is not None else time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO losses (session_id, sensor, from_idx, to_idx, reason, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, sensor, from_idx, to_idx, reason, det_at),
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError as e:
            conn.execute("ROLLBACK")
            if "disk is full" in str(e).lower():
                self._release_emergency_reserve()
                # Retry once after releasing reserve
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO losses (session_id, sensor, from_idx, to_idx, reason, detected_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, sensor, from_idx, to_idx, reason, det_at),
                    )
                    conn.execute("COMMIT")
                except Exception as retry_err:
                    logger.critical(f"Fatal: Failed to write loss even with reserve released: {retry_err}")
            else:
                raise
        finally:
            conn.close()

    def save_window_result(self, result: WindowResult) -> None:
        """Persist a computed window result."""
        conn = self._get_connection()
        now = result.processed_at if result.processed_at is not None else time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO results (\n                    session_id, "window", mode, missing_1, missing_2, complete,
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
                ON CONFLICT(session_id, "window", mode) DO UPDATE SET
                    "window"=excluded."window",
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
                    now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_cursor(self, sensor: int) -> Optional[Tuple[str, int, int]]:
        """Return (session_id, last_read_idx, last_acked_idx) for a sensor."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT session_id, last_read_idx, last_acked_idx FROM cursors WHERE sensor=? ",
                (sensor,),
            ).fetchone()
            if row:
                return row["session_id"], row["last_read_idx"], row["last_acked_idx"]
            return None
        finally:
            conn.close()

    def advance_acked_cursor(self, sensor: int, session_id: str, acked_idx: int) -> None:
        """Update last_acked_idx for a sensor."""
        conn = self._get_connection()
        now = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE cursors
                SET last_acked_idx = MAX(last_acked_idx, ?), updated_at = ?
                WHERE sensor = ? AND session_id = ?
                """,
                (acked_idx, now, sensor, session_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_unacked_samples(
        self,
        session_id: str,
        sensor: int,
        limit: int = 1000,
    ) -> List[Sample]:
        """Fetch samples that have not yet been acknowledged by the cloud."""
        conn = self._get_connection()
        try:
            cursor_row = conn.execute(
                "SELECT last_acked_idx FROM cursors WHERE sensor=? AND session_id=?",
                (sensor, session_id),
            ).fetchone()
            last_acked = cursor_row["last_acked_idx"] if cursor_row else -1

            rows = conn.execute(
                """
                SELECT idx, ch0, ch1, ch2
                FROM samples
                WHERE session_id=? AND sensor=? AND idx > ?
                ORDER BY idx ASC
                LIMIT ?
                """,
                (session_id, sensor, last_acked, limit),
            ).fetchall()

            samples: List[Sample] = []
            for r in rows:
                vals = [r["ch0"], r["ch1"]]
                if r["ch2"] is not None:
                    vals.append(r["ch2"])
                samples.append(Sample(sensor=sensor, idx=r["idx"], values=vals))
            return samples
        finally:
            conn.close()

    def get_unacked_losses(
        self,
        session_id: str,
        sensor: int,
        limit: int = 500,
    ) -> List[LossRecord]:
        """Fetch loss records for upload."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT sensor, from_idx, to_idx, reason, detected_at
                FROM losses
                WHERE session_id=? AND sensor=?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, sensor, limit),
            ).fetchall()

            return [
                LossRecord(
                    sensor=r["sensor"],
                    from_index=r["from_idx"],
                    to_index=r["to_idx"],
                    reason=r["reason"],
                    detected_at=r["detected_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_offline_results_to_sync(self, session_id: str, limit: int = 200) -> List[WindowResult]:
        """Fetch computed offline results to sync to cloud."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    session_id, "window", mode, missing_1, missing_2, complete,
                    mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                    min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                    max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                    magnitude, heart_rate, processed_at
                FROM results
                WHERE session_id=? AND mode='offline'
                ORDER BY "window" ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

            results: List[WindowResult] = []
            for r in rows:
                mean = {
                    "s1c1": r["mean_s1c1"],
                    "s1c2": r["mean_s1c2"],
                    "s1c3": r["mean_s1c3"],
                    "s2c1": r["mean_s2c1"],
                    "s2c2": r["mean_s2c2"],
                }
                min_vals = {
                    "s1c1": r["min_s1c1"],
                    "s1c2": r["min_s1c2"],
                    "s1c3": r["min_s1c3"],
                    "s2c1": r["min_s2c1"],
                    "s2c2": r["min_s2c2"],
                }
                max_vals = {
                    "s1c1": r["max_s1c1"],
                    "s1c2": r["max_s1c2"],
                    "s1c3": r["max_s1c3"],
                    "s2c1": r["max_s2c1"],
                    "s2c2": r["max_s2c2"],
                }
                results.append(
                    WindowResult(
                        session_id=r["session_id"],
                        window=r["window"],
                        mode=r["mode"],
                        missing_1=r["missing_1"],
                        missing_2=r["missing_2"],
                        complete=bool(r["complete"]),
                        mean=mean,
                        min=min_vals,
                        max=max_vals,
                        magnitude=r["magnitude"],
                        heart_rate=r["heart_rate"],
                        processed_at=r["processed_at"],
                    )
                )
            return results
        finally:
            conn.close()

    def get_mode(self) -> str:
        """Get the current system mode ('online' or 'offline')."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key='mode'"
            ).fetchone()
            return row["value"] if row else "online"
        finally:
            conn.close()

    def set_mode(self, mode: str) -> None:
        """Persist system mode."""
        conn = self._get_connection()
        now = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES ('mode', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (mode, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Return status metrics: current mode, samples saved, samples in cloud, backlog.

        Metrics are strictly per-session and consistent:
        - mode: current operational mode ('online' or 'offline').
        - samples_saved: exact count of received samples physically saved on device for the session.
        - samples_in_cloud: exact count of saved device samples confirmed acknowledged by the cloud
          (idx <= last_acked_idx). Always <= samples_saved.
        - backlog: exact count of saved device samples waiting to be acknowledged by the cloud
          (idx > last_acked_idx). Exactly equals samples_saved - samples_in_cloud.
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN TRANSACTION")

            mode_row = cur.execute("SELECT value FROM system_state WHERE key='mode'").fetchone()
            current_mode = mode_row["value"] if mode_row else "online"

            cursor_rows = cur.execute(
                "SELECT sensor, session_id, last_read_idx, last_acked_idx FROM cursors"
            ).fetchall()

            # Identify target session: explicit argument, active cursor session, or latest sample session
            target_session = session_id
            if not target_session and cursor_rows:
                for r in cursor_rows:
                    if r["session_id"]:
                        target_session = r["session_id"]
                        break

            if not target_session:
                sess_row = cur.execute("SELECT session_id FROM samples ORDER BY rowid DESC LIMIT 1").fetchone()
                if sess_row:
                    target_session = sess_row["session_id"]

            if not target_session:
                cur.execute("COMMIT")
                return {
                    "mode": current_mode,
                    "samples_saved": 0,
                    "samples_in_cloud": 0,
                    "backlog": 0,
                }

            # 1. Total samples physically saved on device for this session
            samples_saved = cur.execute(
                "SELECT COUNT(*) FROM samples WHERE session_id = ?",
                (target_session,),
            ).fetchone()[0]

            # 2. Count acknowledged samples on cloud (idx <= last_acked_idx)
            samples_in_cloud = 0
            for r in cursor_rows:
                if r["session_id"] != target_session:
                    continue
                sensor = r["sensor"]
                acked_idx = r["last_acked_idx"]
                if acked_idx >= 0:
                    c_acked = cur.execute(
                        "SELECT COUNT(*) FROM samples WHERE session_id = ? AND sensor = ? AND idx <= ?",
                        (target_session, sensor, acked_idx),
                    ).fetchone()[0]
                    samples_in_cloud += c_acked

            # Backlog is the exact count of saved samples not yet acked by the cloud
            backlog = max(0, samples_saved - samples_in_cloud)
            cur.execute("COMMIT")

            return {
                "mode": current_mode,
                "samples_saved": samples_saved,
                "samples_in_cloud": samples_in_cloud,
                "backlog": backlog,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def export_samples_stream(self, session_id: str) -> Generator[str, None, None]:
        """Export samples and losses for session_id in the exact JSON lines format."""
        conn = self._get_connection()
        try:
            # 1. Stream samples: {"sensor":1,"index":120000,"values":[720,1200,1560]}
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

            # 2. Stream losses: {"sensor":2,"from_index":4000,"to_index":4399,"reason":"..."}
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
        """Export window results for session_id in JSON lines format."""
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT
                    session_id, "window", mode, missing_1, missing_2, complete,
                    mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                    min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                    max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                    magnitude, heart_rate, processed_at
                FROM results
                WHERE session_id=?
                ORDER BY "window" ASC, mode ASC
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
