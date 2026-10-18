"""Unit tests for biosignal mock raw protocol and CRC verification."""

import json
import zlib
from pathlib import Path
import pytest
from core.types import Sample
from reader.protocol import extract_data_slice, verify_data_crc, parse_reply_line
from reader.client import ReaderClient
from storage.database import DeviceDatabase


def test_extract_data_slice_and_crc() -> None:
    data = [[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]
    data_str = json.dumps(data)
    data_bytes = data_str.encode("utf-8")
    crc_hex = f"{zlib.crc32(data_bytes) & 0xFFFFFFFF:08x}"

    raw_line = (
        f'{{"session_id":"5f0c2a9e","sensor":1,"first_index":120000,"count":3,'
        f'"lost":[],"session_end":false,"crc32":"{crc_hex}","data":{data_str}}}\n'
    ).encode("utf-8")

    assert extract_data_slice(raw_line) == data_bytes
    assert verify_data_crc(raw_line, crc_hex) is True

    parsed, valid = parse_reply_line(raw_line)
    assert valid is True
    assert parsed is not None
    assert parsed["session_id"] == "5f0c2a9e"
    assert parsed["sensor"] == 1
    assert parsed["count"] == 3


def test_corrupt_crc_detected() -> None:
    data = [[1, 2, 3]]
    data_str = json.dumps(data)
    bad_crc = "deadbeef"

    raw_line = (
        f'{{"session_id":"test","sensor":1,"first_index":0,"count":1,'
        f'"lost":[],"session_end":false,"crc32":"{bad_crc}","data":{data_str}}}\n'
    ).encode("utf-8")

    parsed, valid = parse_reply_line(raw_line)
    assert parsed is not None
    assert valid is False


def test_whitespace_preservation_crc() -> None:
    # With spaces inside array formatting, raw slice must match
    data_str = "[[ 100,  200 ], [ 300,  400 ]]"
    data_bytes = data_str.encode("utf-8")
    crc_hex = f"{zlib.crc32(data_bytes) & 0xFFFFFFFF:08x}"

    raw_line = (
        f'{{"session_id":"sp","sensor":2,"first_index":10,"count":2,'
        f'"lost":[],"session_end":false,"crc32":"{crc_hex}","data":{data_str}}}\n'
    ).encode("utf-8")

    assert extract_data_slice(raw_line) == data_bytes
    assert verify_data_crc(raw_line, crc_hex) is True


def test_reader_records_initial_unread_gap_on_session_start(tmp_path: Path) -> None:
    """If reader starts or mock restarts when mock has already produced indices (first_index > 0),
    the reader must record 0..first_index-1 as a loss with reason 'not_read_before_pipeline_start'."""
    db_file = tmp_path / "test_initial_gap.db"
    db = DeviceDatabase(db_file)
    reader = ReaderClient(db)

    # Mock replies with first_index=5000, count=2 for sensor 1
    data = [[10, 20, 30], [11, 21, 31]]
    data_bytes = json.dumps(data).encode("utf-8")
    crc_hex = f"{zlib.crc32(data_bytes) & 0xFFFFFFFF:08x}"
    reply = {
        "session_id": "gap_sess",
        "sensor": 1,
        "first_index": 5000,
        "count": 2,
        "lost": [],
        "session_end": False,
        "crc32": crc_hex,
        "data": data,
    }
    raw_line = (json.dumps(reply) + "\n").encode("utf-8")

    class MockFile:
        def readline(self):
            return raw_line

    class MockSocket:
        def sendall(self, b):
            pass

    reader._poll_sensor(1, MockSocket(), MockFile())

    # Check that losses table recorded 0..4999
    conn = db._get_connection()
    losses = conn.execute("SELECT from_idx, to_idx, reason FROM losses WHERE session_id='gap_sess'").fetchall()
    assert len(losses) == 1
    assert losses[0]["from_idx"] == 0
    assert losses[0]["to_idx"] == 4999
    assert losses[0]["reason"] == "not_read_before_pipeline_start"
    conn.close()


def test_reader_restart_across_session_change(tmp_path: Path) -> None:
    """Verify that restarting the reader across a session change does NOT inherit
    the previous session's cursors or send stale from_index requests."""
    db_file = tmp_path / "test_restart_session.db"
    db = DeviceDatabase(db_file)

    # 1. Populate DB with old session 'old_sess' up to index 1,440,000
    db.save_reply_batch("old_sess", 1, [], [], next_read_idx=1440000)
    db.save_reply_batch("old_sess", 2, [], [], next_read_idx=1440000)

    cur1_old = db.get_cursor(1, session_id="old_sess")
    assert cur1_old is not None
    assert cur1_old[1] == 1439999

    # 2. Start new reader
    reader = ReaderClient(db)

    # Capture the outgoing request
    sent_requests = []

    class MockSocket:
        def sendall(self, b):
            sent_requests.append(json.loads(b.decode("utf-8").strip()))

    data = [[10, 20, 30]]
    data_bytes = json.dumps(data).encode("utf-8")
    crc_hex = f"{zlib.crc32(data_bytes) & 0xFFFFFFFF:08x}"

    reply_new_s1 = {
        "session_id": "new_sess",
        "sensor": 1,
        "first_index": 0,
        "count": 1,
        "lost": [],
        "session_end": False,
        "crc32": crc_hex,
        "data": data,
    }

    class MockFile:
        def readline(self):
            return (json.dumps(reply_new_s1) + "\n").encode("utf-8")

    # Poll sensor 1
    reader._poll_sensor(1, MockSocket(), MockFile())

    # Verify: first request must NOT have included from_index=1440000!
    assert "from_index" not in sent_requests[0]
    assert sent_requests[0]["sensor"] == 1

    # Verify: new session cursor starts from index 0 (stored last_read_idx = 0)
    cur1_new = db.get_cursor(1, session_id="new_sess")
    assert cur1_new is not None
    assert cur1_new[1] == 0  # next_read_idx - 1

    # Old session cursor remains preserved
    cur1_old_after = db.get_cursor(1, session_id="old_sess")
    assert cur1_old_after is not None
    assert cur1_old_after[1] == 1439999


def test_sensor_cursors_do_not_diverge_after_restart(tmp_path: Path) -> None:
    """Verify that when the pipeline daemon restarts against a newly started mock,
    sensor 1 and sensor 2 both start from index 0 without diverging."""
    db_file = tmp_path / "test_divergence.db"
    db = DeviceDatabase(db_file)

    # Old session had sensor 1 at 1,440,000 and sensor 2 at 5,000
    db.save_reply_batch("session_prev", 1, [], [], next_read_idx=1440000)
    db.save_reply_batch("session_prev", 2, [], [], next_read_idx=5000)

    reader = ReaderClient(db)

    # First request for sensor 1 against new mock session 'session_curr'
    data_s1 = [[1, 2, 3]]
    crc_s1 = f"{zlib.crc32(json.dumps(data_s1).encode('utf-8')) & 0xFFFFFFFF:08x}"
    reply_s1 = {
        "session_id": "session_curr",
        "sensor": 1,
        "first_index": 0,
        "count": 1,
        "lost": [],
        "session_end": False,
        "crc32": crc_s1,
        "data": data_s1,
    }

    data_s2 = [[4, 5]]
    crc_s2 = f"{zlib.crc32(json.dumps(data_s2).encode('utf-8')) & 0xFFFFFFFF:08x}"
    reply_s2 = {
        "session_id": "session_curr",
        "sensor": 2,
        "first_index": 0,
        "count": 1,
        "lost": [],
        "session_end": False,
        "crc32": crc_s2,
        "data": data_s2,
    }

    class MockSocket:
        def __init__(self):
            self.requests = []
        def sendall(self, b):
            self.requests.append(json.loads(b.decode("utf-8").strip()))

    mock_sock = MockSocket()

    class MockFileS1:
        def readline(self):
            return (json.dumps(reply_s1) + "\n").encode("utf-8")

    class MockFileS2:
        def readline(self):
            return (json.dumps(reply_s2) + "\n").encode("utf-8")

    # Poll sensor 1 then sensor 2
    reader._poll_sensor(1, mock_sock, MockFileS1())
    reader._poll_sensor(2, mock_sock, MockFileS2())

    # Check cursors for session_curr
    cur1 = db.get_cursor(1, session_id="session_curr")
    cur2 = db.get_cursor(2, session_id="session_curr")

    assert cur1 is not None and cur2 is not None
    # Both sensors should be at index 0 (0-indexed sample 0 read)
    assert cur1[1] == 0
    assert cur2[1] == 0
    assert reader.expected_indices[1] == 1
    assert reader.expected_indices[2] == 1
