"""Supervisor tests: password file, command line, restart loop, terms gate.

Run from the repo root:  python3 -m unittest discover -s image/tests -v
"""

import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import bcrypt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "opt" / "mystnode"))

from mystnode import supervisor  # noqa: E402

# Stands in for docker-entrypoint.sh: counts launches, then exits with the code
# listed for that launch (the last code repeats).
FAKE_ENTRYPOINT = """#!/bin/sh
count_file="{count}"
n=$(($(cat "$count_file" 2>/dev/null || echo 0) + 1))
echo "$n" > "$count_file"
case "$n" in
  1|2) exit 0 ;;
  *) exit 3 ;;
esac
"""


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nodeui-pass"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_hash(self):
        self.assertTrue(supervisor.ensure_password(self.path, "secret"))
        stored = self.path.read_text().strip()
        self.assertTrue(bcrypt.checkpw(b"secret", stored.encode()))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_replaces_other_password(self):
        self.path.write_text(bcrypt.hashpw(b"changed-in-nodeui", bcrypt.gensalt(4)).decode())
        self.assertTrue(supervisor.ensure_password(self.path, "secret"))
        self.assertTrue(bcrypt.checkpw(b"secret", self.path.read_text().encode()))

    def test_replaces_garbage(self):
        self.path.write_text("not a hash\n")
        self.assertTrue(supervisor.ensure_password(self.path, "secret"))
        self.assertTrue(bcrypt.checkpw(b"secret", self.path.read_text().encode()))

    def test_keeps_matching_hash(self):
        supervisor.ensure_password(self.path, "secret")
        past = time.time() - 100
        os.utime(self.path, (past, past))
        before = self.path.stat().st_mtime_ns
        self.assertFalse(supervisor.ensure_password(self.path, "secret"))
        self.assertEqual(self.path.stat().st_mtime_ns, before)


class CommandTests(unittest.TestCase):
    def test_build_command(self):
        self.assertEqual(
            supervisor.build_command("/ep.sh", "172.17.0.1", Path("/var/lib/mysterium-node")),
            [
                "/ep.sh",
                "--ui.address=172.17.0.1",
                "--log-dir=/var/lib/mysterium-node/logs",
                "--node-ui-dir=/var/lib/mysterium-node/nodeui",
                "--transactor.provider.try-free-registration",
                "service",
                "--agreed-terms-and-conditions",
            ],
        )


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, text: str) -> str:
        path = self.dir / "entrypoint.sh"
        path.write_text(text)
        path.chmod(0o755)
        return str(path)

    def test_restarts_after_clean_exit(self):
        count = self.dir / "count"
        cmd = [self.script(FAKE_ENTRYPOINT.format(count=count))]
        rc = supervisor.run(cmd, threading.Event(), restart_delay=0)
        self.assertEqual(rc, 3)
        self.assertEqual(count.read_text().strip(), "3")

    def test_stop_terminates_child(self):
        cmd = [self.script("#!/bin/sh\nexec sleep 60\n")]
        stop = threading.Event()
        spawned = []
        started = threading.Event()

        def on_spawn(p):
            spawned.append(p)
            started.set()

        def stopper():
            started.wait(5)
            stop.set()
            spawned[0].terminate()

        threading.Thread(target=stopper).start()
        t0 = time.monotonic()
        rc = supervisor.run(cmd, stop, restart_delay=0, on_spawn=on_spawn)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(len(spawned), 1)
        self.assertLess(time.monotonic() - t0, 10)


class MarkerTests(unittest.TestCase):
    def test_returns_true_when_marker_appears(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "terms-accepted.json"
            threading.Timer(0.3, marker.write_text, args=("{}",)).start()
            self.assertTrue(supervisor.wait_for_marker(marker, 0.05, threading.Event()))

    def test_returns_false_when_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()
            threading.Timer(0.3, stop.set).start()
            self.assertFalse(supervisor.wait_for_marker(Path(d) / "missing", 0.05, stop))


if __name__ == "__main__":
    unittest.main()
