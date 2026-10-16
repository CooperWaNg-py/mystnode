"""Dashboard server tests: gates, local routes and the NodeUI reverse proxy.

Run from the repo root:  python3 -m unittest discover -s image/tests -v
"""

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakenode import IDENTITY, PASSWORD, FakeNode

from mystnode.nodeclient import NodeClient
from mystnode.web import make_server

STATIC = Path(__file__).resolve().parents[1] / "root" / "opt" / "mystnode" / "web"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNode()
        self.addCleanup(self.stop_fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.logs = Path(self.tmp.name) / "logs"
        self.data.mkdir()
        self.logs.mkdir()
        self.server = self.start(NodeClient("127.0.0.1", self.fake.port, PASSWORD), {"127.0.0.1"})

    def stop_fake(self):
        if self.fake is not None:
            self.fake.stop()
            self.fake = None

    def start(self, client, trusted):
        server = make_server(0, client, self.data, self.logs, STATIC, trusted)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, method, path, body=None, headers=None, server=None):
        server = server or self.server
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = {"Content-Type": "application/json"} if data is not None else {}
            hdrs.update(headers or {})
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def upstream(self, path):
        return [r for r in self.fake.requests if r["path"] == path]

    # ------------------------------------------------------------ proxy

    def test_injects_bearer_token(self):
        status, _, body = self.call("GET", "/tequilapi/identities")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["identities"][0]["id"], IDENTITY)
        (req,) = self.upstream("/tequilapi/identities")
        self.assertEqual(req["authorization"], "Bearer token-1")

    def test_reauthenticates_once_after_token_rotation(self):
        self.assertEqual(self.call("GET", "/tequilapi/identities")[0], 200)
        self.assertEqual(self.fake.auth_calls, 1)
        self.fake.token = "token-2"
        status, _, _ = self.call("GET", "/tequilapi/identities")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.auth_calls, 2)
        auths = [r["authorization"] for r in self.upstream("/tequilapi/identities")]
        self.assertEqual(auths, ["Bearer token-1", "Bearer token-1", "Bearer token-2"])

    def test_forwards_body(self):
        payload = {"provider_id": IDENTITY, "type": "dvpn"}
        status, _, _ = self.call("POST", "/tequilapi/services", payload,
                                 {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201)
        (req,) = [r for r in self.upstream("/tequilapi/services") if r["method"] == "POST"]
        self.assertEqual(json.loads(req["body"]), payload)

    def test_blocks_cross_site_post(self):
        status, _, body = self.call("POST", "/tequilapi/services", {"provider_id": IDENTITY, "type": "dvpn"},
                                    {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "cross-site request blocked"})
        self.assertEqual(self.fake.requests, [])

    def test_blocks_same_site_api_get(self):
        status, _, _ = self.call("GET", "/tequilapi/x", headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.requests, [])
        # Page navigations from elsewhere (e.g. the Umbrel dashboard) stay open.
        self.assertEqual(self.call("GET", "/", headers={"Sec-Fetch-Site": "same-site"})[0], 200)

    def test_strips_cors_headers(self):
        status, headers, _ = self.call("GET", "/tequilapi/healthcheck")
        self.assertEqual(status, 200)
        self.assertFalse([k for k in headers if k.lower().startswith("access-control-")])

    def test_streams_sse(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/tequilapi/events/sse")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        first = resp.read1(65536)
        first_at = time.monotonic()
        self.assertIn(b"data: one", first)
        rest = resp.read()
        self.assertIn(b"data: two", first + rest)
        self.assertIsNotNone(self.fake.sse_second_sent_at)
        self.assertLess(first_at, self.fake.sse_second_sent_at)

    def test_node_unreachable(self):
        self.stop_fake()
        status, _, body = self.call("GET", "/tequilapi/healthcheck")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "node_unreachable"})
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 502)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"/umbrel/", body)

    def test_wrong_password(self):
        server = self.start(NodeClient("127.0.0.1", self.fake.port, "wrong"), {"127.0.0.1"})
        status, _, body = self.call("GET", "/tequilapi/identities", server=server)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "node_auth_failed"})

    # ------------------------------------------------------------ local routes

    def test_accept_terms(self):
        status, _, body = self.call("GET", "/umbrel/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"terms_accepted": False, "terms_accepted_at": None,
                                            "node_reachable": True, "node_version": "1.39.7"})
        self.assertEqual(self.call("POST", "/umbrel/api/accept-terms", {"accept": False})[0], 400)
        self.assertFalse((self.data / "terms-accepted.json").exists())
        status, _, body = self.call("POST", "/umbrel/api/accept-terms", {"accept": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["terms_accepted"])
        marker = json.loads((self.data / "terms-accepted.json").read_text())
        status, _, body = self.call("GET", "/umbrel/api/status")
        self.assertEqual(json.loads(body)["terms_accepted"], True)
        self.assertEqual(json.loads(body)["terms_accepted_at"], marker["accepted_at"])

    def test_accept_terms_cross_site_blocked(self):
        status, _, _ = self.call("POST", "/umbrel/api/accept-terms", {"accept": True},
                                 {"Sec-Fetch-Site": "same-site"})
        self.assertEqual(status, 403)
        self.assertFalse((self.data / "terms-accepted.json").exists())

    def test_static(self):
        status, headers, _ = self.call("GET", "/umbrel")
        self.assertEqual((status, headers["Location"]), (301, "/umbrel/"))
        status, headers, body = self.call("GET", "/umbrel/")
        self.assertEqual(status, 200)
        self.assertIn(b"static/app.js", body)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        status, headers, _ = self.call("GET", "/umbrel/static/app.js")
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/javascript"))
        self.assertEqual(self.call("GET", "/umbrel/static/..%2Fweb.py")[0], 404)
        self.assertEqual(self.call("GET", "/umbrel/static/missing.js")[0], 404)
        self.assertEqual(self.call("GET", "/umbrel/nope")[0], 404)

    def test_logs(self):
        status, _, body = self.call("GET", "/umbrel/api/logs")
        self.assertEqual((status, json.loads(body)["exists"]), (200, False))
        self.assertEqual(self.call("GET", "/umbrel/api/logs/download")[0], 404)
        text = "2026-10-16T08:00:00.000 DBG noise\n2026-10-16T08:00:01.000 INF hello\n"
        (self.logs / "mysterium-node.log").write_text(text)
        status, _, body = self.call("GET", "/umbrel/api/logs?lines=10&level=info")
        self.assertEqual(json.loads(body)["lines"], ["2026-10-16T08:00:01.000 INF hello"])
        self.assertEqual(self.call("GET", "/umbrel/api/logs?level=loud")[0], 400)
        status, headers, body = self.call("GET", "/umbrel/api/logs/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(body.decode(), text)

    def test_widget(self):
        status, _, body = self.call("GET", "/umbrel/widgets/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["text"], "Setup")

    def test_untrusted_source(self):
        server = self.start(NodeClient("127.0.0.1", self.fake.port, PASSWORD), {"10.255.255.1"})
        status, _, body = self.call("GET", "/tequilapi/identities", server=server)
        self.assertEqual((status, body), (403, b"forbidden source"))
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
