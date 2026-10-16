"""Widget tests against a fake NodeUI.

Run from the repo root:  python3 -m unittest discover -s image/tests -v
"""

import socket
import unittest

from fakenode import PASSWORD, FakeNode

from mystnode.nodeclient import NodeClient
from mystnode.widget import build_widget


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WidgetTests(unittest.TestCase):
    def assertShape(self, widget):
        self.assertEqual(widget["type"], "four-stats")
        self.assertEqual(widget["refresh"], "30s")
        self.assertEqual(widget["link"], "")
        self.assertEqual(len(widget["items"]), 4)

    def test_terms_not_accepted(self):
        widget = build_widget(NodeClient("127.0.0.1", free_port(), PASSWORD, timeout=2), False)
        self.assertShape(widget)
        self.assertEqual(widget["items"][0]["text"], "Setup")

    def test_node_down(self):
        widget = build_widget(NodeClient("127.0.0.1", free_port(), PASSWORD, timeout=2), True)
        self.assertShape(widget)
        self.assertEqual(widget["items"][0]["text"], "Offline")
        self.assertEqual([i["text"] for i in widget["items"][1:]], ["–", "–", "–"])

    def test_online(self):
        fake = FakeNode()
        self.addCleanup(fake.stop)
        widget = build_widget(NodeClient("127.0.0.1", fake.port, PASSWORD), True)
        self.assertShape(widget)
        self.assertEqual(widget["items"], [
            {"title": "Node", "text": "Online", "subtext": "1.39.7"},
            {"title": "Services", "text": "3/4", "subtext": "running"},
            {"title": "Unsettled", "text": "1.234", "subtext": "MYST"},
            {"title": "Lifetime", "text": "12.500", "subtext": "MYST"},
        ])

    def test_identity_failure_keeps_online(self):
        fake = FakeNode()
        self.addCleanup(fake.stop)
        fake.identity = None  # serialises as `null`
        widget = build_widget(NodeClient("127.0.0.1", fake.port, PASSWORD), True)
        self.assertShape(widget)
        self.assertEqual(widget["items"][0]["text"], "Online")
        self.assertEqual(widget["items"][2]["text"], "–")
        self.assertEqual(widget["items"][3]["text"], "–")


if __name__ == "__main__":
    unittest.main()
