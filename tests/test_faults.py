"""Integration tests for fault handling: corrupt CRC, duplicates, stalls, and mock restarts."""

from pathlib import Path
import time
import pytest

from core.types import LossRecord, Sample
from reader.client import ReaderClient
from storage.database import DeviceDatabase
from tools.mock_sim import MockServer


def test_corrupt_crc_and_recovery(tmp_path: Path) -> None:
    """Verify that corrupt CRC is rejected and subsequent valid reply is ingested."""
    db_file = tmp_path / "test_corrupt.db"
    db = DeviceDatabase(db_file)

    # Start mock server on port 7110 with corrupt_crc_prob = 0
    mock = MockServer(mode="signal", seed=7, duration=60, port=7110)
    mock.start(blocking=False)
    time.sleep(0.3)

    try:
        reader = ReaderClient(db=db, mock_port=7110, poll_interval_sec=0.1)

        # First simulate corrupt CRC directly in mock
        mock.corrupt_crc_prob = 1.0  # Forces corrupt CRC
        sock = mock.server_sock
        reader.start()

        time.sleep(0.5)
        # Should have 0 samples saved because of corrupt CRC
        assert db.get_status()["samples_saved"] == 0
        assert reader.consecutive_crc_failures[1] > 0

        # Now heal CRC corruption
        mock.corrupt_crc_prob = 0.0
        time.sleep(0.8)

        # Reader should have recovered via from_index and saved samples!
        status = db.get_status()
        assert status["samples_saved"] > 0
        assert reader.consecutive_crc_failures[1] == 0

        reader.stop()
    finally:
        mock.stop()


def test_mock_restart_new_session_loss_logging(tmp_path: Path) -> None:
    """Verify that mock restart logs loss with to_index=null for previous session."""
    db_file = tmp_path / "test_restart.db"
    db = DeviceDatabase(db_file)

    mock1 = MockServer(mode="signal", seed=1, duration=60, port=7111)
    mock1.start(blocking=False)
    sess1 = mock1.session_id
    time.sleep(0.3)

    reader = ReaderClient(db=db, mock_port=7111, poll_interval_sec=0.1)
    reader.start()

    try:
        time.sleep(0.6)
        # Read some samples from sess1
        assert db.get_status()["samples_saved"] > 0

        # Kill mock1 and start mock2 with new session_id
        mock1.stop()
        time.sleep(0.5)

        mock2 = MockServer(mode="signal", seed=2, duration=60, port=7111)
        mock2.start(blocking=False)
        sess2 = mock2.session_id
        assert sess1 != sess2

        time.sleep(0.8)

        # Verify that a loss record for sess1 was written with to_index=None
        conn = db._get_connection()
        try:
            losses = conn.execute(
                "SELECT * FROM losses WHERE session_id=? AND reason='mock_restart_session_change'",
                (sess1,),
            ).fetchall()
            assert len(losses) > 0
            for l in losses:
                assert l["to_idx"] is None
        finally:
            conn.close()

        reader.stop()
        mock2.stop()
    finally:
        try:
            mock1.stop()
        except Exception:
            pass
