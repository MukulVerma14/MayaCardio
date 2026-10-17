"""Unit tests for biosignal mock raw protocol and CRC verification."""

import json
import zlib
from pathlib import Path
import pytest
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
