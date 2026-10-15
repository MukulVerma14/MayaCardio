"""Integration tests for uploader client and cloud ingestion."""

import gzip
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from cloud.server import app
from cloud.storage import CloudStorage
import cloud.server


def test_cloud_ingest_and_window_computation(tmp_path: Path) -> None:
    # Use isolated test database
    test_db = tmp_path / "test_cloud.db"
    cloud.server.cloud_storage = CloudStorage(db_path=test_db)

    client = TestClient(app)

    # Health check
    resp = client.post("/api/v1/heartbeat")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    session_id = "cloud_test_sess"

    # Ingest 401 samples for Sensor 1 and 400 for Sensor 2 to finalize window 0
    s1_samples = [
        {"sensor": 1, "index": i, "values": [100, 200, 300]}
        for i in range(401)
    ]
    s2_samples = [
        {"sensor": 2, "index": i, "values": [400000, 150000]}
        for i in range(400)
    ]

    payload = {
        "session_id": session_id,
        "samples": s1_samples + s2_samples,
        "losses": [],
        "offline_results": [],
    }

    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}

    ingest_resp = client.post("/api/v1/ingest", content=body, headers=headers)
    assert ingest_resp.status_code == 200
    data = ingest_resp.json()
    assert data["status"] == "ok"
    assert data["acked_cursors"]["1"] == 400
    assert data["acked_cursors"]["2"] == 399

    # Verify cloud status
    status_resp = client.get("/api/v1/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["total_samples"] == 801

    # Verify cloud results export
    res_resp = client.get(f"/api/v1/results?session_id={session_id}")
    assert res_resp.status_code == 200
    lines = [line for line in res_resp.text.strip().split("\n") if line]
    assert len(lines) >= 1

    first_win = json.loads(lines[0])
    assert first_win["session_id"] == session_id
    assert first_win["window"] == 0
    assert first_win["mode"] == "online"
    assert first_win["complete"] is True
    assert first_win["missing_1"] == 0
    assert first_win["missing_2"] == 0
    assert first_win["mean"]["s1c1"] == 100.0
