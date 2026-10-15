"""Unit tests for the SQLite storage engine."""

import json
from pathlib import Path
import sqlite3
import pytest
from core.types import LossRecord, Sample, WindowResult
from storage.database import DeviceDatabase


def test_database_initialization(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pipeline.db"
    db = DeviceDatabase(db_file)

    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    # Check journal mode
    cur.execute("PRAGMA journal_mode")
    assert cur.fetchone()[0].lower() == "wal"
    # Check synchronous
    cur.execute("PRAGMA synchronous")
    # 2 corresponds to FULL
    assert cur.fetchone()[0] == 2
    conn.close()

    assert db.reserve_path.exists()
    assert db.reserve_path.stat().st_size > 0


def test_save_reply_batch_and_duplicates(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pipeline.db"
    db = DeviceDatabase(db_file)

    session_id = "sess_001"
    samples = [
        Sample(sensor=1, idx=0, values=[100, 200, 300]),
        Sample(sensor=1, idx=1, values=[105, 205, 305]),
    ]
    losses = [
        LossRecord(sensor=1, from_index=10, to_index=15, reason="test_loss"),
    ]

    accepted, rejected = db.save_reply_batch(
        session_id=session_id,
        sensor=1,
        samples=samples,
        losses=losses,
        next_read_idx=2,
    )
    assert accepted == 2
    assert rejected == 0

    # Test identical duplicate insertion (should be rejected/ignored without error)
    accepted_dup, rejected_dup = db.save_reply_batch(
        session_id=session_id,
        sensor=1,
        samples=samples,
        losses=[],
        next_read_idx=2,
    )
    assert accepted_dup == 0
    assert rejected_dup == 2

    # Verify cursors updated
    cursor = db.get_cursor(1)
    assert cursor is not None
    assert cursor[0] == session_id
    assert cursor[1] == 1  # next_read_idx - 1


def test_export_streams(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pipeline.db"
    db = DeviceDatabase(db_file)

    session_id = "export_sess"
    db.save_reply_batch(
        session_id=session_id,
        sensor=1,
        samples=[Sample(sensor=1, idx=0, values=[10, 20, 30])],
        losses=[LossRecord(sensor=1, from_index=5, to_index=10, reason="overflow")],
        next_read_idx=1,
    )

    lines = list(db.export_samples_stream(session_id))
    assert len(lines) == 2

    sample_obj = json.loads(lines[0])
    assert sample_obj["sensor"] == 1
    assert sample_obj["index"] == 0
    assert sample_obj["values"] == [10, 20, 30]

    loss_obj = json.loads(lines[1])
    assert loss_obj["sensor"] == 1
    assert loss_obj["from_index"] == 5
    assert loss_obj["to_index"] == 10
    assert loss_obj["reason"] == "overflow"
