"""Authenticated access to the node's TequilAPI through the NodeUI server.

TequilAPI itself (127.0.0.1:4050) has no authentication in desktop myst, so it
is never exposed. NodeUI's /tequilapi/* reverse proxy checks a JWT, which this
client obtains with the NodeUI password and injects as a Bearer token.
"""

import http.client
import json
import re
import threading
from datetime import datetime, timedelta, timezone

USERNAME = "myst"
# Tokens live 48 h; refresh well before expiry.
REFRESH_MARGIN = timedelta(hours=1)
FALLBACK_LIFETIME = timedelta(hours=1)


class NodeUnavailable(Exception):
    """The NodeUI server cannot be reached (node stopped or starting)."""


class NodeAuthError(Exception):
    """NodeUI rejected the dashboard's password."""


class NodeHTTPError(Exception):
    def __init__(self, status: int, body: bytes):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def parse_rfc3339(value: str) -> datetime | None:
    # Go emits up to nanosecond precision, which datetime cannot hold.
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)", value or "")
    if not m:
        return None
    zone = "+00:00" if m.group(2) == "Z" else m.group(2)
    try:
        return datetime.fromisoformat(m.group(1) + zone)
    except ValueError:
        return None


class NodeClient:
    def __init__(self, host_ip: str, port: int, password: str, timeout: float = 15.0):
        self.host_ip = host_ip
        self.port = port
        self.password = password
        self.timeout = timeout
        self.base = f"http://{host_ip}:{port}"
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def _connect(self, timeout: float | None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host_ip, self.port, timeout=timeout)

    def _authenticate(self) -> None:
        body = json.dumps({"username": USERNAME, "password": self.password}).encode()
        conn = self._connect(self.timeout)
        try:
            conn.request("POST", "/tequilapi/auth/authenticate", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise NodeUnavailable(str(e)) from e
        finally:
            conn.close()
        # NodeUI answers 5xx while TequilAPI behind it is still starting.
        if resp.status >= 500:
            raise NodeUnavailable(f"authenticate returned HTTP {resp.status}")
        if resp.status != 200:
            raise NodeAuthError(f"authenticate returned HTTP {resp.status}")
        try:
            payload = json.loads(data)
            token = payload["token"]
        except (ValueError, KeyError, TypeError) as e:
            raise NodeAuthError("malformed authenticate response") from e
        now = datetime.now(timezone.utc)
        self._token = token
        self._expires_at = parse_rfc3339(payload.get("expires_at", "")) or now + FALLBACK_LIFETIME

    def token(self) -> str:
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._token is None or self._expires_at is None or now >= self._expires_at - REFRESH_MARGIN:
                self._authenticate()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, timeout: float | None = None) -> http.client.HTTPResponse:
        """Send a request; the caller reads the response and closes `resp.conn`.
        `timeout=None` uses the client default; pass 0 for no read timeout."""
        headers = dict(headers or {})
        inject = not any(k.lower() == "authorization" for k in headers)
        effective = self.timeout if timeout is None else (timeout or None)
        for attempt in range(2):
            if inject:
                headers["Authorization"] = f"Bearer {self.token()}"
            conn = self._connect(self.timeout)
            try:
                conn.connect()
                # Connect with the default timeout; reads use `effective`.
                conn.sock.settimeout(effective)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise NodeUnavailable(str(e)) from e
            if resp.status == 401 and inject and attempt == 0:
                resp.read()
                conn.close()
                self.invalidate()
                continue
            resp.conn = conn
            return resp
        raise AssertionError("unreachable")

    def get_json(self, path: str, timeout: float = 5.0):
        resp = self.request("GET", path, timeout=timeout)
        try:
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise NodeUnavailable(str(e)) from e
        finally:
            resp.conn.close()
        if not 200 <= resp.status < 300:
            raise NodeHTTPError(resp.status, data)
        try:
            return json.loads(data)
        except ValueError as e:
            raise NodeHTTPError(resp.status, data) from e
