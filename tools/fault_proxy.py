"""Fault Injection TCP Proxy for Biosignal Mock testing.

Sits between the pipeline reader and biosignal-mock (or mock_sim.py).
Listens on a proxy port (e.g. 7101) and forwards to the mock (e.g. 7100),
with dynamic fault injection controlled via HTTP or CLI flags.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import sys
import threading
import time
from typing import Any, Dict, Optional


class FaultState:
    """Thread-safe state holding active fault configurations."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.corrupt_crc_count: int = 0
        self.duplicate_count: int = 0
        self.delay_seconds: float = 0.0
        self.drop_next_connection: bool = False
        self.stall_sensor: Optional[int] = None
        self.stall_sensor_until: float = 0.0

    def reset(self) -> None:
        with self.lock:
            self.corrupt_crc_count = 0
            self.duplicate_count = 0
            self.delay_seconds = 0.0
            self.drop_next_connection = False
            self.stall_sensor = None
            self.stall_sensor_until = 0.0

    def set_fault(self, fault_type: str, params: Dict[str, Any]) -> None:
        with self.lock:
            if fault_type == "corrupt_crc":
                self.corrupt_crc_count = int(params.get("count", 1))
            elif fault_type == "duplicate":
                self.duplicate_count = int(params.get("count", 1))
            elif fault_type == "delay":
                self.delay_seconds = float(params.get("seconds", 2.0))
            elif fault_type == "stall_sensor":
                self.stall_sensor = int(params.get("sensor", 1))
                duration = float(params.get("duration", 5.0))
                self.stall_sensor_until = time.monotonic() + duration
            elif fault_type == "drop_connection":
                self.drop_next_connection = True


class FaultControlHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler allowing test scripts to inject faults programmatically."""

    fault_state: FaultState = FaultState()

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            data = json.loads(post_body.decode("utf-8")) if post_body else {}
        except Exception:
            data = {}

        if self.path == "/fault":
            action = data.get("action", "")
            self.fault_state.set_fault(action, data)
            self._send_json({"status": "ok", "action": action})
        elif self.path == "/reset":
            self.fault_state.reset()
            self._send_json({"status": "ok", "message": "faults reset"})
        else:
            self.send_error(404, "Not Found")

    def do_GET(self) -> None:
        if self.path == "/status":
            with self.fault_state.lock:
                status = {
                    "corrupt_crc_count": self.fault_state.corrupt_crc_count,
                    "duplicate_count": self.fault_state.duplicate_count,
                    "delay_seconds": self.fault_state.delay_seconds,
                    "stall_sensor": self.fault_state.stall_sensor,
                }
            self._send_json(status)
        else:
            self.send_error(404, "Not Found")

    def _send_json(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP request logging
        return


class FaultProxy:
    """TCP proxy that forwards requests and replies with injected faults."""

    def __init__(
        self,
        listen_port: int = 7101,
        target_port: int = 7100,
        target_host: str = "127.0.0.1",
        control_port: int = 7199,
        fault_state: Optional[FaultState] = None,
    ) -> None:
        self.listen_port = listen_port
        self.target_port = target_port
        self.target_host = target_host
        self.control_port = control_port
        self.fault_state = fault_state or FaultState()
        self.running: bool = False
        self.proxy_sock: Optional[socket.socket] = None
        self.http_server: Optional[http.server.HTTPServer] = None

    def start(self, blocking: bool = True) -> None:
        self.running = True

        # Start control HTTP server
        FaultControlHandler.fault_state = self.fault_state
        self.http_server = http.server.HTTPServer(("127.0.0.1", self.control_port), FaultControlHandler)
        t_http = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        t_http.start()

        self.proxy_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.proxy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.proxy_sock.bind(("0.0.0.0", self.listen_port))
        self.proxy_sock.listen(5)
        print(f"[fault_proxy] Listening on port {self.listen_port} -> forwarding to {self.target_host}:{self.target_port}")
        print(f"[fault_proxy] Control API at http://127.0.0.1:{self.control_port}")

        if blocking:
            self._serve_loop()
        else:
            t_proxy = threading.Thread(target=self._serve_loop, daemon=True)
            t_proxy.start()

    def stop(self) -> None:
        self.running = False
        if self.proxy_sock:
            try:
                self.proxy_sock.close()
            except Exception:
                pass
        if self.http_server:
            try:
                self.http_server.shutdown()
            except Exception:
                pass

    def _serve_loop(self) -> None:
        while self.running:
            try:
                client_conn, client_addr = self.proxy_sock.accept()
            except Exception:
                break
            t = threading.Thread(target=self._handle_client, args=(client_conn,), daemon=True)
            t.start()

    def _handle_client(self, client_conn: socket.socket) -> None:
        with self.fault_state.lock:
            if self.fault_state.drop_next_connection:
                self.fault_state.drop_next_connection = False
                client_conn.close()
                return

        try:
            target_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_conn.connect((self.target_host, self.target_port))
        except Exception as e:
            print(f"[fault_proxy] Failed to connect to target mock {self.target_host}:{self.target_port}: {e}")
            client_conn.close()
            return

        client_file = client_conn.makefile("rb")
        target_file = target_conn.makefile("rb")

        try:
            while self.running:
                req_line = client_file.readline()
                if not req_line:
                    break

                # Parse request
                sensor = 1
                try:
                    req_obj = json.loads(req_line.decode("utf-8").strip())
                    sensor = int(req_obj.get("sensor", 1))
                except Exception:
                    pass

                # Check sensor stall
                with self.fault_state.lock:
                    if self.fault_state.stall_sensor == sensor:
                        if time.monotonic() < self.fault_state.stall_sensor_until:
                            # Stalled: drop request or don't reply
                            time.sleep(0.5)
                            continue

                # Check general delay
                with self.fault_state.lock:
                    delay = self.fault_state.delay_seconds
                    self.fault_state.delay_seconds = 0.0

                if delay > 0:
                    time.sleep(delay)

                # Forward request to mock
                target_conn.sendall(req_line)

                # Read reply from mock
                reply_line = target_file.readline()
                if not reply_line:
                    break

                # Inspect and possibly mutate reply
                to_send = [reply_line]

                with self.fault_state.lock:
                    if self.fault_state.corrupt_crc_count > 0:
                        self.fault_state.corrupt_crc_count -= 1
                        # Replace crc32 in reply with bad crc
                        try:
                            rep_str = reply_line.decode("utf-8")
                            # Inject wrong crc
                            import re
                            mutated = re.sub(r'"crc32":"[0-9a-fA-F]{8}"', '"crc32":"00000000"', rep_str)
                            to_send = [mutated.encode("utf-8")]
                            print("[fault_proxy] Injected corrupt CRC")
                        except Exception:
                            pass

                    if self.fault_state.duplicate_count > 0:
                        self.fault_state.duplicate_count -= 1
                        to_send.append(reply_line)
                        print("[fault_proxy] Injected duplicate reply")

                for line in to_send:
                    client_conn.sendall(line)

        except Exception as e:
            pass
        finally:
            try:
                client_conn.close()
            except Exception:
                pass
            try:
                target_conn.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Fault Injection Proxy (Dev Tool)")
    parser.add_argument("--listen-port", type=int, default=7101, help="Proxy port for pipeline to connect")
    parser.add_argument("--target-port", type=int, default=7100, help="Mock port to forward to")
    parser.add_argument("--target-host", default="127.0.0.1", help="Target host")
    parser.add_argument("--control-port", type=int, default=7199, help="HTTP control API port")

    args = parser.parse_args()
    proxy = FaultProxy(
        listen_port=args.listen_port,
        target_port=args.target_port,
        target_host=args.target_host,
        control_port=args.control_port,
    )
    try:
        proxy.start(blocking=True)
    except KeyboardInterrupt:
        print("\n[fault_proxy] Stopped by user")
    finally:
        proxy.stop()


if __name__ == "__main__":
    main()
