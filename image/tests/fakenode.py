"""A stand-in for the NodeUI server (and the TequilAPI behind it) used by the tests."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "opt" / "mystnode"))

PASSWORD = "pw"
IDENTITY = "0x00000000000000000000000000000000000000aa"


class FakeNode:
    def __init__(self):
        self.token = "token-1"
        self.auth_calls = 0
        self.requests: list[dict] = []
        self.services = [
            {"id": "s1", "type": "dvpn", "status": "Running"},
            {"id": "s2", "type": "wireguard", "status": "Running"},
            {"id": "s3", "type": "scraping", "status": "Running"},
            {"type": "data_transfer", "status": "NotRunning"},
            {"id": "s5", "type": "monitoring", "status": "Running"},
        ]
        self.identity = {
            "id": IDENTITY,
            "earnings_tokens": {"wei": "1234560000000000000", "ether": "1.23456", "human": "1.2346"},
            "earnings_total_tokens": {"wei": "12500000000000000000", "ether": "12.5", "human": "12.5000"},
        }
        self.sse_second_sent_at: float | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = urlsplit(self.path).path
                fake.requests.append({
                    "method": self.command, "path": self.path, "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "host": self.headers.get("Host"),
                })
                if path == "/tequilapi/auth/authenticate":
                    fake.auth_calls += 1
                    creds = json.loads(body or b"{}")
                    if creds.get("username") == "myst" and creds.get("password") == PASSWORD:
                        self.send(200, {"token": fake.token, "expires_at": "2099-01-01T00:00:00.123456789Z"})
                    else:
                        self.send(401, {"error": {"code": "unauthorized", "message": "bad credentials"}})
                    return
                if path == "/":
                    self.send(200, b"<html>NodeUI</html>", "text/html")
                    return
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.send(401, {"error": {"code": "unauthorized", "message": "invalid token"}})
                    return
                if path == "/tequilapi/healthcheck":
                    self.send(200, {"version": "1.39.7", "uptime": "1m"})
                elif path == "/tequilapi/identities":
                    self.send(200, {"identities": [{"id": IDENTITY}]})
                elif path == f"/tequilapi/identities/{IDENTITY}":
                    self.send(200, fake.identity)
                elif path == "/tequilapi/services" and self.command == "GET":
                    self.send(200, fake.services)
                elif path == "/tequilapi/services" and self.command == "POST":
                    self.send(201, {"id": "new", "status": "Starting"})
                elif path == "/tequilapi/events/sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.chunk(b"data: one\n\n")
                    time.sleep(0.5)
                    fake.sse_second_sent_at = time.monotonic()
                    self.chunk(b"data: two\n\n")
                    self.chunk(b"")
                else:
                    self.send(404, {"error": {"code": "not_found", "message": "not found"}})

            def chunk(self, data: bytes):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            do_GET = do_POST = do_DELETE = do_PUT = handle_any

        return Handler
