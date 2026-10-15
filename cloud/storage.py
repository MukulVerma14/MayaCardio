"""Cloud storage layer for ingesting and serving biosignal sessions.

Supports free PostgreSQL (Neon / Supabase) via CLOUD_DATABASE_URL,
with SQLite fallback for local offline testing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from core.types import LossRecord, Sample, WindowResult
from core.window_processor import WindowProcessor

logger = logging.getLogger("cloud.storage")

DEFAULT_DB_URL = os.environ.get(
    "CLOUD_DATABASE_URL",
    "sqlite:///cloud_store.db"
)


def normalize_db_url(url: str) -> str:
    """Normalize database URL for SQLAlchemy and psycopg driver."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class CloudStorage:
    """Storage engine for the cloud backend supporting Neon PostgreSQL and SQLite."""

    def __init__(self, db_url: Optional[str] = None) -> None:
        raw_url = db_url or DEFAULT_DB_URL
        self.db_url = normalize_db_url(raw_url)
        self.is_postgres = self.db_url.startswith("postgresql")

        # Configure engine with connection pooling appropriate for serverless PostgreSQL
        if self.is_postgres:
            self.engine: Engine = create_engine(
                self.db_url,
                pool_pre_ping=True,
                pool_recycle=300,
            )
        else:
            self.engine: Engine = create_engine(
                self.db_url,
                connect_args={"timeout": 30.0} if "sqlite" in self.db_url else {},
            )

        self._init_db()
        self.processors: Dict[str, WindowProcessor] = {}

    def _init_db(self) -> None:
        """Create tables using dialect-appropriate DDL."""
        with self.engine.begin() as conn:
            if not self.is_postgres:
                conn.execute(text("PRAGMA journal_mode = WAL;"))
                conn.execute(text("PRAGMA synchronous = FULL;"))

            # DDL for samples table
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS samples (
                    session_id VARCHAR(64) NOT NULL,
                    sensor INTEGER NOT NULL,
                    idx INTEGER NOT NULL,
                    ch0 INTEGER NOT NULL,
                    ch1 INTEGER NOT NULL,
                    ch2 INTEGER,
                    PRIMARY KEY (session_id, sensor, idx)
                );
            """))

            # DDL for losses table
            id_col = "id SERIAL PRIMARY KEY" if self.is_postgres else "id INTEGER PRIMARY KEY AUTOINCREMENT"
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS losses (
                    {id_col},
                    session_id VARCHAR(64) NOT NULL,
                    sensor INTEGER NOT NULL,
                    from_idx INTEGER NOT NULL,
                    to_idx INTEGER,
                    reason VARCHAR(128) NOT NULL,
                    detected_at DOUBLE PRECISION NOT NULL
                );
            """ if self.is_postgres else f"""
                CREATE TABLE IF NOT EXISTS losses (
                    {id_col},
                    session_id TEXT NOT NULL,
                    sensor INTEGER NOT NULL,
                    from_idx INTEGER NOT NULL,
                    to_idx INTEGER,
                    reason TEXT NOT NULL,
                    detected_at REAL NOT NULL
                );
            """))

            # DDL for cursors table
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS cursors (
                    sensor INTEGER PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    last_read_idx INTEGER NOT NULL DEFAULT -1,
                    last_acked_idx INTEGER NOT NULL DEFAULT -1,
                    updated_at DOUBLE PRECISION NOT NULL
                );
            """ if self.is_postgres else """
                CREATE TABLE IF NOT EXISTS cursors (
                    sensor INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    last_read_idx INTEGER NOT NULL DEFAULT -1,
                    last_acked_idx INTEGER NOT NULL DEFAULT -1,
                    updated_at REAL NOT NULL
                );
            """))

            # DDL for results table
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS results (
                    session_id VARCHAR(64) NOT NULL,
                    window INTEGER NOT NULL,
                    mode VARCHAR(32) NOT NULL,
                    missing_1 INTEGER NOT NULL,
                    missing_2 INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    mean_s1c1 DOUBLE PRECISION,
                    mean_s1c2 DOUBLE PRECISION,
                    mean_s1c3 DOUBLE PRECISION,
                    mean_s2c1 DOUBLE PRECISION,
                    mean_s2c2 DOUBLE PRECISION,
                    min_s1c1 DOUBLE PRECISION,
                    min_s1c2 DOUBLE PRECISION,
                    min_s1c3 DOUBLE PRECISION,
                    min_s2c1 DOUBLE PRECISION,
                    min_s2c2 DOUBLE PRECISION,
                    max_s1c1 DOUBLE PRECISION,
                    max_s1c2 DOUBLE PRECISION,
                    max_s1c3 DOUBLE PRECISION,
                    max_s2c1 DOUBLE PRECISION,
                    max_s2c2 DOUBLE PRECISION,
                    magnitude DOUBLE PRECISION,
                    heart_rate DOUBLE PRECISION,
                    processed_at DOUBLE PRECISION,
                    PRIMARY KEY (session_id, window, mode)
                );
            """ if self.is_postgres else """
                CREATE TABLE IF NOT EXISTS results (
                    session_id TEXT NOT NULL,
                    window INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    missing_1 INTEGER NOT NULL,
                    missing_2 INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    mean_s1c1 REAL,
                    mean_s1c2 REAL,
                    mean_s1c3 REAL,
                    mean_s2c1 REAL,
                    mean_s2c2 REAL,
                    min_s1c1 REAL,
                    min_s1c2 REAL,
                    min_s1c3 REAL,
                    min_s2c1 REAL,
                    min_s2c2 REAL,
                    max_s1c1 REAL,
                    max_s1c2 REAL,
                    max_s1c3 REAL,
                    max_s2c1 REAL,
                    max_s2c2 REAL,
                    magnitude REAL,
                    heart_rate REAL,
                    processed_at REAL,
                    PRIMARY KEY (session_id, window, mode)
                );
            """))

            # Indexes
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_samples_export ON samples (session_id, sensor, idx);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_losses_export ON losses (session_id, sensor, from_idx);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_results_lookup ON results (session_id, window, mode);"))

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
        """Ingest samples, losses, and offline results idempotently."""
        proc = self.get_or_create_processor(session_id)

        with self.engine.begin() as conn:
            # 1. Ingest samples
            for s in samples:
                ch0 = s.values[0] if len(s.values) > 0 else 0
                ch1 = s.values[1] if len(s.values) > 1 else 0
                ch2 = s.values[2] if len(s.values) > 2 else None

                conn.execute(
                    text("""
                        INSERT INTO samples (session_id, sensor, idx, ch0, ch1, ch2)
                        VALUES (:sess, :sensor, :idx, :ch0, :ch1, :ch2)
                        ON CONFLICT (session_id, sensor, idx) DO NOTHING
                    """),
                    {"sess": session_id, "sensor": s.sensor, "idx": s.idx, "ch0": ch0, "ch1": ch1, "ch2": ch2},
                )
                proc.add_sample(s.sensor, s.idx, s.values)

            # 2. Ingest losses
            for l in losses:
                det_at = l.detected_at if l.detected_at is not None else server_timestamp
                conn.execute(
                    text("""
                        INSERT INTO losses (session_id, sensor, from_idx, to_idx, reason, detected_at)
                        VALUES (:sess, :sensor, :f_idx, :t_idx, :reason, :det_at)
                    """),
                    {
                        "sess": session_id,
                        "sensor": l.sensor,
                        "f_idx": l.from_index,
                        "t_idx": l.to_index,
                        "reason": l.reason,
                        "det_at": det_at,
                    },
                )
                proc.add_loss_range(l.sensor, l.from_index, l.to_index)

            # 3. Finalize online windows
            online_results = proc.finalize_available_windows(processed_at=server_timestamp)
            for res in online_results:
                self._save_result_row(conn, res)

            # 4. Ingest device-side offline results
            for r in offline_results:
                self._save_offline_result_dict(conn, r)

            # Compute highest contiguous index acked per sensor
            acked_cursors: Dict[int, int] = {}
            for sensor in (1, 2):
                res = conn.execute(
                    text("SELECT MAX(idx) AS max_idx FROM samples WHERE session_id=:sess AND sensor=:sensor"),
                    {"sess": session_id, "sensor": sensor},
                ).fetchone()
                acked_cursors[sensor] = res[0] if (res and res[0] is not None) else -1

            return acked_cursors

    def _save_result_row(self, conn: Any, result: WindowResult) -> None:
        params = {
            "sess": result.session_id,
            "win": result.window,
            "mode": result.mode,
            "m1": result.missing_1,
            "m2": result.missing_2,
            "comp": 1 if result.complete else 0,
            "mean_s1c1": result.mean.get("s1c1"),
            "mean_s1c2": result.mean.get("s1c2"),
            "mean_s1c3": result.mean.get("s1c3"),
            "mean_s2c1": result.mean.get("s2c1"),
            "mean_s2c2": result.mean.get("s2c2"),
            "min_s1c1": result.min.get("s1c1"),
            "min_s1c2": result.min.get("s1c2"),
            "min_s1c3": result.min.get("s1c3"),
            "min_s2c1": result.min.get("s2c1"),
            "min_s2c2": result.min.get("s2c2"),
            "max_s1c1": result.max.get("s1c1"),
            "max_s1c2": result.max.get("s1c2"),
            "max_s1c3": result.max.get("s1c3"),
            "max_s2c1": result.max.get("s2c1"),
            "max_s2c2": result.max.get("s2c2"),
            "mag": result.magnitude,
            "hr": result.heart_rate,
            "proc_at": result.processed_at,
        }
        conn.execute(
            text("""
                INSERT INTO results (
                    session_id, window, mode, missing_1, missing_2, complete,
                    mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                    min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                    max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                    magnitude, heart_rate, processed_at
                ) VALUES (
                    :sess, :win, :mode, :m1, :m2, :comp,
                    :mean_s1c1, :mean_s1c2, :mean_s1c3, :mean_s2c1, :mean_s2c2,
                    :min_s1c1, :min_s1c2, :min_s1c3, :min_s2c1, :min_s2c2,
                    :max_s1c1, :max_s1c2, :max_s1c3, :max_s2c1, :max_s2c2,
                    :mag, :hr, :proc_at
                )
                ON CONFLICT (session_id, window, mode) DO UPDATE SET
                    missing_1 = EXCLUDED.missing_1,
                    missing_2 = EXCLUDED.missing_2,
                    complete = EXCLUDED.complete,
                    mean_s1c1 = EXCLUDED.mean_s1c1,
                    mean_s1c2 = EXCLUDED.mean_s1c2,
                    mean_s1c3 = EXCLUDED.mean_s1c3,
                    mean_s2c1 = EXCLUDED.mean_s2c1,
                    mean_s2c2 = EXCLUDED.mean_s2c2,
                    min_s1c1 = EXCLUDED.min_s1c1,
                    min_s1c2 = EXCLUDED.min_s1c2,
                    min_s1c3 = EXCLUDED.min_s1c3,
                    min_s2c1 = EXCLUDED.min_s2c1,
                    min_s2c2 = EXCLUDED.min_s2c2,
                    max_s1c1 = EXCLUDED.max_s1c1,
                    max_s1c2 = EXCLUDED.max_s1c2,
                    max_s1c3 = EXCLUDED.max_s1c3,
                    max_s2c1 = EXCLUDED.max_s2c1,
                    max_s2c2 = EXCLUDED.max_s2c2,
                    magnitude = EXCLUDED.magnitude,
                    heart_rate = EXCLUDED.heart_rate,
                    processed_at = EXCLUDED.processed_at
            """),
            params,
        )

    def _save_offline_result_dict(self, conn: Any, r: Dict[str, Any]) -> None:
        mean = r.get("mean", {})
        mins = r.get("min", {})
        maxs = r.get("max", {})

        params = {
            "sess": r["session_id"],
            "win": r["window"],
            "mode": r.get("mode", "offline"),
            "m1": r["missing_1"],
            "m2": r["missing_2"],
            "comp": 1 if r.get("complete") else 0,
            "mean_s1c1": mean.get("s1c1"),
            "mean_s1c2": mean.get("s1c2"),
            "mean_s1c3": mean.get("s1c3"),
            "mean_s2c1": mean.get("s2c1"),
            "mean_s2c2": mean.get("s2c2"),
            "min_s1c1": mins.get("s1c1"),
            "min_s1c2": mins.get("s1c2"),
            "min_s1c3": mins.get("s1c3"),
            "min_s2c1": mins.get("s2c1"),
            "min_s2c2": mins.get("s2c2"),
            "max_s1c1": maxs.get("s1c1"),
            "max_s1c2": maxs.get("s1c2"),
            "max_s1c3": maxs.get("s1c3"),
            "max_s2c1": maxs.get("s2c1"),
            "max_s2c2": maxs.get("s2c2"),
            "mag": r.get("magnitude"),
            "hr": r.get("heart_rate"),
            "proc_at": r.get("processed_at"),
        }
        conn.execute(
            text("""
                INSERT INTO results (
                    session_id, window, mode, missing_1, missing_2, complete,
                    mean_s1c1, mean_s1c2, mean_s1c3, mean_s2c1, mean_s2c2,
                    min_s1c1, min_s1c2, min_s1c3, min_s2c1, min_s2c2,
                    max_s1c1, max_s1c2, max_s1c3, max_s2c1, max_s2c2,
                    magnitude, heart_rate, processed_at
                ) VALUES (
                    :sess, :win, :mode, :m1, :m2, :comp,
                    :mean_s1c1, :mean_s1c2, :mean_s1c3, :mean_s2c1, :mean_s2c2,
                    :min_s1c1, :min_s1c2, :min_s1c3, :min_s2c1, :min_s2c2,
                    :max_s1c1, :max_s1c2, :max_s1c3, :max_s2c1, :max_s2c2,
                    :mag, :hr, :proc_at
                )
                ON CONFLICT (session_id, window, mode) DO UPDATE SET
                    missing_1 = EXCLUDED.missing_1,
                    missing_2 = EXCLUDED.missing_2,
                    complete = EXCLUDED.complete,
                    mean_s1c1 = EXCLUDED.mean_s1c1,
                    mean_s1c2 = EXCLUDED.mean_s1c2,
                    mean_s1c3 = EXCLUDED.mean_s1c3,
                    mean_s2c1 = EXCLUDED.mean_s2c1,
                    mean_s2c2 = EXCLUDED.mean_s2c2,
                    min_s1c1 = EXCLUDED.min_s1c1,
                    min_s1c2 = EXCLUDED.min_s1c2,
                    min_s1c3 = EXCLUDED.min_s1c3,
                    min_s2c1 = EXCLUDED.min_s2c1,
                    min_s2c2 = EXCLUDED.min_s2c2,
                    max_s1c1 = EXCLUDED.max_s1c1,
                    max_s1c2 = EXCLUDED.max_s1c2,
                    max_s1c3 = EXCLUDED.max_s1c3,
                    max_s2c1 = EXCLUDED.max_s2c1,
                    max_s2c2 = EXCLUDED.max_s2c2,
                    magnitude = EXCLUDED.magnitude,
                    heart_rate = EXCLUDED.heart_rate,
                    processed_at = EXCLUDED.processed_at
            """),
            params,
        )

    def get_status(self) -> Dict[str, Any]:
        with self.engine.connect() as conn:
            total_samples = conn.execute(text("SELECT COUNT(*) FROM samples")).scalar() or 0
            total_windows = conn.execute(text("SELECT COUNT(*) FROM results")).scalar() or 0
            sessions_res = conn.execute(text("SELECT DISTINCT session_id FROM samples")).fetchall()
            sessions = [r[0] for r in sessions_res]
            return {
                "backend": "postgresql" if self.is_postgres else "sqlite",
                "sessions": sessions,
                "total_samples": total_samples,
                "processed_windows": total_windows,
            }

    def export_samples_stream(self, session_id: str) -> Generator[str, None, None]:
        with self.engine.connect() as conn:
            # 1. Samples
            cur = conn.execute(
                text("""
                    SELECT sensor, idx, ch0, ch1, ch2
                    FROM samples
                    WHERE session_id = :sess
                    ORDER BY idx ASC, sensor ASC
                """),
                {"sess": session_id},
            )
            for r in cur:
                vals = [r[2], r[3]]
                if r[4] is not None:
                    vals.append(r[4])
                obj = {"sensor": r[0], "index": r[1], "values": vals}
                yield json.dumps(obj) + "\n"

            # 2. Losses
            cur_l = conn.execute(
                text("""
                    SELECT sensor, from_idx, to_idx, reason
                    FROM losses
                    WHERE session_id = :sess
                    ORDER BY from_idx ASC, sensor ASC
                """),
                {"sess": session_id},
            )
            for r in cur_l:
                obj = {"sensor": r[0], "from_index": r[1], "to_index": r[2], "reason": r[3]}
                yield json.dumps(obj) + "\n"

    def export_results_stream(self, session_id: str) -> Generator[str, None, None]:
        with self.engine.connect() as conn:
            cur = conn.execute(
                text("""
                    SELECT * FROM results
                    WHERE session_id = :sess
                    ORDER BY window ASC, mode ASC
                """),
                {"sess": session_id},
            )
            for r in cur.mappings():
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
