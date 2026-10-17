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


def test_status_consistency_with_gaps_and_non_zero_start(tmp_path: Path) -> None:
    """Verify that get_status calculates samples_in_cloud based on physical rows,
    preventing non-zero start index or loss gaps from inflating samples_in_cloud."""
    db_file = tmp_path / "test_status.db"
    db = DeviceDatabase(db_file)

    sess_1 = "sess_with_gap"
    # Sensor 1: gap 0..4999, samples 5000..5099 (100 samples)
    s1_samples = [Sample(sensor=1, idx=i, values=[10, 20, 30]) for i in range(5000, 5100)]
    s1_losses = [LossRecord(sensor=1, from_index=0, to_index=4999, reason="buffer_overflow")]
    db.save_reply_batch(sess_1, sensor=1, samples=s1_samples, losses=s1_losses, next_read_idx=5100)

    # Sensor 2: gap 0..4999, samples 5000..5099 (100 samples)
    s2_samples = [Sample(sensor=2, idx=i, values=[400000, 150000]) for i in range(5000, 5100)]
    s2_losses = [LossRecord(sensor=2, from_index=0, to_index=4999, reason="buffer_overflow")]
    db.save_reply_batch(sess_1, sensor=2, samples=s2_samples, losses=s2_losses, next_read_idx=5100)

    # Initially nothing acked
    status_0 = db.get_status(session_id=sess_1)
    assert status_0["samples_saved"] == 200
    assert status_0["samples_in_cloud"] == 0
    assert status_0["backlog"] == 200

    # Ack up to 5049 for sensor 1 (50 samples: 5000..5049) and up to 5049 for sensor 2 (50 samples: 5000..5049)
    db.advance_acked_cursor(sensor=1, session_id=sess_1, acked_idx=5049)
    db.advance_acked_cursor(sensor=2, session_id=sess_1, acked_idx=5049)

    status_acked = db.get_status(session_id=sess_1)
    assert status_acked["samples_saved"] == 200
    # Must NOT be (5049 + 1) * 2 = 10100!
    assert status_acked["samples_in_cloud"] == 100
    assert status_acked["backlog"] == 100
    assert status_acked["samples_in_cloud"] <= status_acked["samples_saved"]
    assert status_acked["samples_in_cloud"] + status_acked["backlog"] == status_acked["samples_saved"]

    # Active session status (without specifying session_id) must also reflect sess_1 correctly
    status_auto = db.get_status()
    assert status_auto["samples_saved"] == 200
    assert status_auto["samples_in_cloud"] == 100
    assert status_auto["backlog"] == 100

    # Switch active session to sess_2
    sess_2 = "sess_other"
    db.save_reply_batch(sess_2, sensor=1, samples=[Sample(sensor=1, idx=0, values=[1, 2, 3])], losses=[], next_read_idx=1)
    db.save_reply_batch(sess_2, sensor=2, samples=[Sample(sensor=2, idx=0, values=[4, 5])], losses=[], next_read_idx=1)

    # Active session status automatically filters to new active session sess_2
    status_active = db.get_status()
    assert status_active["samples_saved"] == 2
    assert status_active["samples_in_cloud"] == 0
    assert status_active["backlog"] == 2
