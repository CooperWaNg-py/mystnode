"""Log reader tests.

Run from the repo root:  python3 -m unittest discover -s image/tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "opt" / "mystnode"))

from mystnode import logs  # noqa: E402


def line(level: str, msg: str, n: int = 0) -> str:
    return f"2026-10-16T08:00:{n % 60:02d}.123 {level} {msg}"


class TailTests(unittest.TestCase):
    def test_tail_spans_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.log"
            # ~3 x 64 KiB of lines, with a trailing newline.
            all_lines = [f"line {i:06d} " + "x" * 90 for i in range(2100)]
            path.write_text("\n".join(all_lines) + "\n")
            self.assertGreater(path.stat().st_size, 3 * logs.BLOCK)
            self.assertEqual(logs.tail_lines(path, 1500), all_lines[-1500:])
            self.assertEqual(logs.tail_lines(path, 3), all_lines[-3:])
            self.assertEqual(logs.tail_lines(path, 5000), all_lines)

    def test_tail_without_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.log"
            path.write_text("a\nb\nc")
            self.assertEqual(logs.tail_lines(path, 2), ["b", "c"])

    def test_missing_file(self):
        self.assertEqual(logs.tail_lines(Path("/nonexistent/x.log"), 10), [])


class FilterTests(unittest.TestCase):
    LINES = [
        "continuation before any header",
        line("INF", "starting", 1),
        line("WRN", "slow peer", 2),
        "  warn detail",
        line("DBG", "noise", 3),
        "  debug detail",
        line("ERR", "failed", 4),
        "  error detail",
        line("INF", "ok", 5),
    ]

    def test_warn(self):
        self.assertEqual(logs.filter_level(self.LINES, "warn"), [
            line("WRN", "slow peer", 2), "  warn detail",
            line("ERR", "failed", 4), "  error detail",
        ])

    def test_error(self):
        self.assertEqual(logs.filter_level(self.LINES, "error"),
                         [line("ERR", "failed", 4), "  error detail"])

    def test_info_drops_debug(self):
        kept = logs.filter_level(self.LINES, "info")
        self.assertNotIn(line("DBG", "noise", 3), kept)
        self.assertNotIn("  debug detail", kept)
        self.assertNotIn("continuation before any header", kept)
        self.assertIn(line("INF", "ok", 5), kept)

    def test_all_keeps_everything(self):
        self.assertEqual(logs.filter_level(self.LINES, "all"), self.LINES)


class ReadLogsTests(unittest.TestCase):
    def test_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(logs.read_logs(Path(d), 100, "info"),
                             {"file": "mysterium-node.log", "exists": False, "size": 0, "lines": []})

    def test_filters_then_limits(self):
        with tempfile.TemporaryDirectory() as d:
            content = [line("DBG", f"d{i}", i) for i in range(50)] + [line("INF", f"i{i}", i) for i in range(5)]
            path = Path(d) / "mysterium-node.log"
            path.write_text("\n".join(content) + "\n")
            result = logs.read_logs(Path(d), 3, "info")
            self.assertTrue(result["exists"])
            self.assertEqual(result["size"], path.stat().st_size)
            self.assertEqual(result["lines"], content[-3:])


if __name__ == "__main__":
    unittest.main()
