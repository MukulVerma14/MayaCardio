"""CLI interface for the MayaCardio pipeline daemon and tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

import requests

from connectivity.monitor import ConnectivityMonitor
from reader.client import ReaderClient
from storage.database import DeviceDatabase
from storage.offline_processor import DeviceOfflineProcessor
from uploader.client import UploaderClient

DEFAULT_DB_PATH = os.environ.get("PIPELINE_DB_PATH", "pipeline.db")
DEFAULT_MOCK_HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
DEFAULT_MOCK_PORT = int(os.environ.get("MOCK_PORT", "7100"))
DEFAULT_CLOUD_URL = os.environ.get("CLOUD_URL", "http://127.0.0.1:8000")


def cmd_run(args: argparse.Namespace) -> None:
    """Run the pipeline daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("pipeline.daemon")
    logger.info("Starting MayaCardio Bedside Data Pipeline Daemon...")

    db = DeviceDatabase(args.db)
    conn_monitor = ConnectivityMonitor(
        db=db,
        cloud_url=args.cloud_url,
    )
    uploader = UploaderClient(
        db=db,
        conn_monitor=conn_monitor,
        cloud_url=args.cloud_url,
    )
    reader = ReaderClient(
        db=db,
        mock_host=args.mock_host,
        mock_port=args.mock_port,
    )
    offline_proc = DeviceOfflineProcessor(db=db)

    # Start all background components
    conn_monitor.start()
    uploader.start()
    offline_proc.start()
    reader.start()

    logger.info("All pipeline subsystems active. Entering event loop.")

    stop_event = False

    def handle_signal(sig: int, frame: Any) -> None:
        nonlocal stop_event
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        stop_event = True

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    try:
        while not stop_event:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        logger.info("Stopping pipeline components...")
        reader.stop()
        offline_proc.stop()
        uploader.stop()
        conn_monitor.stop()
        logger.info("Shutdown complete.")


def cmd_status(args: argparse.Namespace) -> None:
    """Print one JSON line with current system status."""
    db = DeviceDatabase(args.db)
    session_id = getattr(args, "session", None)
    status_info = db.get_status(session_id=session_id)
    print(json.dumps(status_info, separators=(",", ":")))


def cmd_export(args: argparse.Namespace) -> None:
    """Export samples and losses for a session from device or cloud."""
    session_id = args.session
    source = args.source.lower()

    if source == "device":
        db = DeviceDatabase(args.db)
        for line in db.export_samples_stream(session_id):
            sys.stdout.write(line)
        sys.stdout.flush()

    elif source == "cloud":
        cloud_url = (args.cloud_url or os.environ.get("CLOUD_URL", "http://127.0.0.1:8000")).rstrip("/")
        url = f"{cloud_url}/api/v1/export/samples?session_id={session_id}"
        try:
            with requests.get(url, stream=True, timeout=30.0) as resp:
                if resp.status_code != 200:
                    sys.stderr.write(f"Error fetching from cloud: HTTP {resp.status_code}\n")
                    sys.exit(1)
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except Exception as e:
            sys.stderr.write(f"Cloud export failed: {e}\n")
            sys.exit(1)
    else:
        sys.stderr.write(f"Unknown source: {source}. Must be 'device' or 'cloud'.\n")
        sys.exit(1)


def cmd_results(args: argparse.Namespace) -> None:
    """Print results for a session from local database."""
    session_id = args.session
    db = DeviceDatabase(args.db)
    for line in db.export_results_stream(session_id):
        sys.stdout.write(line)
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database file")
    parent_parser.add_argument("--cloud-url", default=DEFAULT_CLOUD_URL, help="Cloud service URL")

    parser = argparse.ArgumentParser(prog="pipeline", description="MayaCardio Bedside Data Pipeline", parents=[parent_parser])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pipeline run
    run_parser = subparsers.add_parser("run", help="Run the continuous pipeline daemon", parents=[parent_parser])
    run_parser.add_argument("--mock-host", default=DEFAULT_MOCK_HOST, help="Mock service host")
    run_parser.add_argument("--mock-port", type=int, default=DEFAULT_MOCK_PORT, help="Mock service port")

    # pipeline status
    status_parser = subparsers.add_parser("status", help="Print single JSON line with current system status", parents=[parent_parser])
    status_parser.add_argument("--session", default=None, help="Optional session ID to query status for")

    # pipeline export --session ID --from device|cloud
    export_parser = subparsers.add_parser("export", help="Export session data", parents=[parent_parser])
    export_parser.add_argument("--session", required=True, help="Session ID to export")
    export_parser.add_argument("--from", dest="source", choices=["device", "cloud"], default="device", help="Source")

    # pipeline results --session ID
    results_parser = subparsers.add_parser("results", help="Print window results for session", parents=[parent_parser])
    results_parser.add_argument("--session", required=True, help="Session ID to query")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "results":
        cmd_results(args)


if __name__ == "__main__":
    main()
