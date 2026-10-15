"""FastAPI Cloud Service for MayaCardio pipeline."""

from __future__ import annotations

import gzip
import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from cloud.storage import CloudStorage
from core.types import LossRecord, Sample

app = FastAPI(title="MayaCardio Cloud Ingestion & Processing Service")

DB_PATH = os.environ.get("CLOUD_DB_PATH", "cloud_store.db")
cloud_storage = CloudStorage(db_path=DB_PATH)


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "mayacardio-cloud", "status": "running"}


@app.post("/api/v1/heartbeat")
def heartbeat() -> Dict[str, Any]:
    """Lightweight connectivity probe for the bedside device."""
    return {
        "status": "ok",
        "server_time": time.time(),
    }


@app.post("/api/v1/ingest")
async def ingest_batch(request: Request) -> Dict[str, Any]:
    """Ingest a batch of samples, losses, and offline results.

    Supports gzip compression if Content-Encoding: gzip header is set.
    """
    server_time = time.time()
    body = await request.body()

    if request.headers.get("content-encoding") == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gzip decompression failed: {e}")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    session_id = str(payload.get("session_id", ""))
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")

    # Parse samples
    raw_samples = payload.get("samples", [])
    samples: List[Sample] = [
        Sample(sensor=int(s["sensor"]), idx=int(s["index"]), values=s["values"])
        for s in raw_samples
    ]

    # Parse losses
    raw_losses = payload.get("losses", [])
    losses: List[LossRecord] = [
        LossRecord(
            sensor=int(l["sensor"]),
            from_index=int(l["from_index"]),
            to_index=int(l["to_index"]) if l.get("to_index") is not None else None,
            reason=str(l.get("reason", "unknown")),
            detected_at=float(l["detected_at"]) if "detected_at" in l else None,
        )
        for l in raw_losses
    ]

    # Offline results
    offline_results = payload.get("offline_results", [])

    acked_cursors = cloud_storage.ingest_batch(
        session_id=session_id,
        samples=samples,
        losses=losses,
        offline_results=offline_results,
        server_timestamp=server_time,
    )

    return {
        "status": "ok",
        "session_id": session_id,
        "acked_cursors": acked_cursors,
        "samples_received": len(samples),
        "server_time": server_time,
    }


@app.get("/api/v1/status")
def get_status() -> Dict[str, Any]:
    """Cloud status and summary metrics."""
    return cloud_storage.get_status()


@app.get("/api/v1/export/samples")
def export_samples(session_id: str) -> StreamingResponse:
    """Stream exported samples and losses for session_id in JSON lines format."""
    def byte_stream():
        for line in cloud_storage.export_samples_stream(session_id):
            yield line.encode("utf-8")

    return StreamingResponse(byte_stream(), media_type="application/x-ndjson")


@app.get("/api/v1/results")
def export_results(session_id: str) -> StreamingResponse:
    """Stream exported window results for session_id in JSON lines format."""
    def byte_stream():
        for line in cloud_storage.export_results_stream(session_id):
            yield line.encode("utf-8")

    return StreamingResponse(byte_stream(), media_type="application/x-ndjson")
