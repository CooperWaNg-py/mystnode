"""Web service entry point: `python3 -m mystnode.web`.

Serves the Umbrel dashboard under /umbrel/ and reverse-proxies every other path
to the node's NodeUI server, injecting a Bearer token so neither the dashboard
nor the stock NodeUI ever asks the user to log in. NodeUI's index.html uses
absolute /static/... and /tequilapi/... URLs, so it has to own `/`.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import logs
from .common import default_gateway, env, resolve_ipv4, setup_logging
from .nodeclient import NodeAuthError, NodeClient, NodeHTTPError, NodeUnavailable
from .widget import build_widget

log = logging.getLogger("mystnode.web")

MARKER = "terms-accepted.json"
MAX_BODY = 10 * 1024 * 1024
PROXY_TIMEOUT = 120.0  # /tequilapi/nat/type runs a live STUN probe
WIDGET_TTL = 15.0
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
STATIC_NAME = re.compile(r"[A-Za-z0-9._-]+")
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
}
SAFE_METHODS = {"GET", "HEAD"}
SAFE_FETCH_SITES = {None, "same-origin", "none"}
LOG_LEVELS = set(logs.THRESHOLDS)
OFFLINE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Mysterium Node</title>
<style>body{font:15px system-ui,sans-serif;margin:3em auto;max-width:32em;padding:0 1em}</style>
</head><body><h1>Mysterium Node</h1>
<p>The Mysterium node is not running yet.</p>
<p><a href="/umbrel/">Open the dashboard</a></p></body></html>
"""


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Server(ThreadingHTTPServer):
    daemon_threads = True

    client: NodeClient
    data_dir: Path
    log_dir: Path
    static_dir: Path
    trusted_sources: set[str] | None
    widget_lock: threading.Lock
    widget_cache: tuple[float, dict] | None


def make_server(port: int, client: NodeClient, data_dir: Path, log_dir: Path,
                static_dir: Path, trusted_sources: set[str] | None) -> Server:
    server = Server(("0.0.0.0", port), Handler)
    server.client = client
    server.data_dir = data_dir
    server.log_dir = log_dir
    server.static_dir = static_dir
    server.trusted_sources = trusted_sources
    server.widget_lock = threading.Lock()
    server.widget_cache = None
    return server


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Handler(BaseHTTPRequestHandler):
    # Every response closes the connection, so streamed bodies need no chunking.
    protocol_version = "HTTP/1.0"
    server: Server

    def do_GET(self): self.dispatch()
    def do_HEAD(self): self.dispatch()
    def do_POST(self): self.dispatch()
    def do_PUT(self): self.dispatch()
    def do_DELETE(self): self.dispatch()
    def do_PATCH(self): self.dispatch()
    def do_OPTIONS(self): self.dispatch()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    # ------------------------------------------------------------ responses

    def send_bytes(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload) -> None:
        self.send_bytes(status, json.dumps(payload).encode(), "application/json",
                        {"Cache-Control": "no-store"})

    def send_text(self, status: int, text: str) -> None:
        self.send_bytes(status, text.encode(), "text/plain; charset=utf-8")

    # ------------------------------------------------------------ gates

    def source_allowed(self) -> bool:
        trusted = self.server.trusted_sources
        return trusted is None or self.client_address[0] in trusted

    def cross_site_blocked(self, path: str) -> bool:
        sfs = self.headers.get("Sec-Fetch-Site")
        sfs = sfs.strip().lower() if sfs is not None else None
        if self.command not in SAFE_METHODS:
            return sfs not in SAFE_FETCH_SITES
        if path.startswith("/tequilapi/") or path.startswith("/umbrel/api/"):
            return sfs in ("cross-site", "same-site")
        return False

    # ------------------------------------------------------------ routing

    def dispatch(self) -> None:
        try:
            if not self.source_allowed():
                log.warning("rejected request from %s", self.client_address[0])
                self.send_text(403, "forbidden source")
                return
            url = urlsplit(self.path)
            path = url.path
            if self.cross_site_blocked(path):
                self.send_json(403, {"error": "cross-site request blocked"})
                return
            if path == "/umbrel" or path.startswith("/umbrel/"):
                self.route_local(path, parse_qs(url.query))
            else:
                self.proxy()
        except HTTPError as e:
            self.send_json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route_local(self, path: str, query: dict) -> None:
        method = self.command
        get = method in SAFE_METHODS
        if get and path == "/umbrel":
            self.send_response(301)
            self.send_header("Location", "/umbrel/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif get and path == "/umbrel/":
            self.send_static("index.html")
        elif get and path.startswith("/umbrel/static/"):
            self.send_static(path[len("/umbrel/static/"):])
        elif get and path == "/umbrel/api/status":
            self.send_json(200, self.status_payload())
        elif method == "POST" and path == "/umbrel/api/accept-terms":
            self.accept_terms()
        elif get and path == "/umbrel/api/logs":
            self.get_logs(query)
        elif get and path == "/umbrel/api/logs/download":
            self.download_logs()
        elif get and path == "/umbrel/widgets/stats":
            self.send_json(200, self.widget())
        else:
            self.send_json(404, {"error": "not found"})

    # ------------------------------------------------------------ local routes

    def send_static(self, name: str) -> None:
        suffix = Path(name).suffix
        if not STATIC_NAME.fullmatch(name) or name.startswith(".") or suffix not in CONTENT_TYPES:
            raise HTTPError(404, "not found")
        try:
            body = (self.server.static_dir / name).read_bytes()
        except OSError:
            raise HTTPError(404, "not found") from None
        self.send_bytes(200, body, CONTENT_TYPES[suffix], {"Cache-Control": "no-cache"})

    def terms(self) -> tuple[bool, str | None]:
        try:
            data = json.loads((self.server.data_dir / MARKER).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False, None
        except (OSError, ValueError):
            return True, None
        accepted_at = data.get("accepted_at") if isinstance(data, dict) else None
        return True, accepted_at if isinstance(accepted_at, str) else None

    def status_payload(self) -> dict:
        accepted, accepted_at = self.terms()
        reachable, version = False, None
        try:
            health = self.server.client.get_json("/tequilapi/healthcheck", timeout=3)
            reachable = True
            if isinstance(health, dict) and health.get("version"):
                version = str(health["version"])
        except (NodeUnavailable, NodeAuthError, NodeHTTPError):
            pass
        return {
            "terms_accepted": accepted,
            "terms_accepted_at": accepted_at,
            "node_reachable": reachable,
            "node_version": version,
        }

    def read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raise HTTPError(411, "length required")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError:
            raise HTTPError(400, "invalid Content-Length") from None
        if length < 0:
            raise HTTPError(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise HTTPError(413, "request body too large")
        return self.rfile.read(length)

    def accept_terms(self) -> None:
        try:
            payload = json.loads(self.read_body() or b"null")
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("accept") is not True:
            raise HTTPError(400, 'body must be {"accept": true}')
        marker = self.server.data_dir / MARKER
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accepted_at": utc_now_iso()}), encoding="utf-8")
        os.replace(tmp, marker)
        log.info("Mysterium terms accepted")
        self.send_json(200, self.status_payload())

    def get_logs(self, query: dict) -> None:
        try:
            lines = int(query.get("lines", ["500"])[0])
        except ValueError:
            raise HTTPError(400, "lines must be an integer") from None
        lines = max(1, min(lines, 5000))
        level = query.get("level", ["info"])[0]
        if level not in LOG_LEVELS:
            raise HTTPError(400, "level must be one of all, info, warn, error")
        self.send_json(200, logs.read_logs(self.server.log_dir, lines, level))

    def download_logs(self) -> None:
        path = self.server.log_dir / logs.LOG_FILE
        try:
            f = open(path, "rb")
        except OSError:
            raise HTTPError(404, "no log file yet") from None
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{logs.LOG_FILE}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = size
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def widget(self) -> dict:
        srv = self.server
        with srv.widget_lock:
            now = time.monotonic()
            if srv.widget_cache is not None and now - srv.widget_cache[0] < WIDGET_TTL:
                return srv.widget_cache[1]
            value = build_widget(srv.client, self.terms()[0])
            srv.widget_cache = (time.monotonic(), value)
            return value

    # ------------------------------------------------------------ proxy

    def proxy(self) -> None:
        body = self.read_body()
        path = self.path
        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in ("host", "content-length"):
                continue
            headers[k] = v
        api = path.startswith("/tequilapi/")
        timeout = 0 if path.startswith("/tequilapi/events/") else PROXY_TIMEOUT
        try:
            resp = self.server.client.request(self.command, path, body=body or None,
                                              headers=headers, timeout=timeout)
        except NodeUnavailable:
            if api:
                self.send_json(502, {"error": "node_unreachable"})
            else:
                self.send_bytes(502, OFFLINE_PAGE, "text/html; charset=utf-8")
            return
        except NodeAuthError:
            log.error("NodeUI rejected the dashboard password")
            self.send_json(502, {"error": "node_auth_failed"})
            return
        try:
            self.relay(resp)
        finally:
            resp.conn.close()

    def relay(self, resp) -> None:
        chunked = "chunked" in (resp.getheader("Transfer-Encoding") or "").lower()
        self.send_response_only(resp.status, resp.reason)
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk.startswith("access-control-"):
                continue
            if lk == "content-length" and chunked:
                continue
            self.send_header(k, v)
        self.end_headers()
        self.log_request(resp.status)
        if self.command == "HEAD":
            return
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            # Upstream failed mid-body; the client sees a truncated response.
            log.warning("proxy stream for %s ended: %s", self.path, e)


def main() -> None:
    setup_logging("web")
    password = env("MYSTNODE_PASSWORD")
    node_ip = resolve_ipv4(env("MYSTNODE_NODE_HOST", "host.docker.internal"))
    client = NodeClient(node_ip, int(env("MYSTNODE_NODEUI_PORT", "4449")), password)
    if env("MYSTNODE_TRUST_ANY_SOURCE", "0") == "1":
        trusted = None
    else:
        # umbreld proxies the app and polls widgets from the host, i.e. from this
        # container's bridge gateway.
        trusted = {"127.0.0.1"}
        gateway = default_gateway()
        if gateway:
            trusted.add(gateway)
    port = int(env("MYSTNODE_PORT", "8080"))
    server = make_server(
        port, client,
        Path(env("MYSTNODE_DATA", "/data")),
        Path(env("MYSTNODE_LOG_DIR", "/node-logs")),
        Path(env("MYSTNODE_STATIC", "/opt/mystnode/web")),
        trusted,
    )
    log.info("dashboard on :%d, NodeUI at %s:%d, trusted sources: %s",
             port, node_ip, client.port, "any" if trusted is None else sorted(trusted))
    server.serve_forever()


if __name__ == "__main__":
    main()
