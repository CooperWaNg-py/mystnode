"""Reading myst's log file (zerolog ConsoleWriter, NoColor, logconfig/config.go)."""

import os
import re
from pathlib import Path

LOG_FILE = "mysterium-node.log"
LEVELS = ["TRC", "DBG", "INF", "WRN", "ERR", "FTL", "PNC"]
LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} (TRC|DBG|INF|WRN|ERR|FTL|PNC) ")
THRESHOLDS = {"all": None, "info": "INF", "warn": "WRN", "error": "ERR"}
BLOCK = 64 * 1024
MAX_RAW_LINES = 20000


def tail_lines(path: Path, n: int) -> list[str]:
    if n <= 0:
        return []
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return []
    with f:
        end = f.seek(0, os.SEEK_END)
        pos = end
        chunks: list[bytes] = []
        newlines = 0
        # n + 1 newlines guarantee n complete lines even with a trailing newline.
        while pos > 0 and newlines <= n:
            size = min(BLOCK, pos)
            pos -= size
            f.seek(pos)
            chunk = f.read(size)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


def filter_level(lines: list[str], minimum: str) -> list[str]:
    threshold = THRESHOLDS[minimum]
    if threshold is None:
        return list(lines)
    floor = LEVELS.index(threshold)
    kept = []
    keep = False  # leading continuation lines have no known level
    for line in lines:
        m = LINE_RE.match(line)
        if m:
            keep = LEVELS.index(m.group(1)) >= floor
        if keep:
            kept.append(line)
    return kept


def read_logs(log_dir: Path, lines: int, level: str) -> dict:
    path = log_dir / LOG_FILE
    try:
        size = path.stat().st_size
        exists = True
    except FileNotFoundError:
        size, exists = 0, False
    raw = tail_lines(path, min(lines * 4, MAX_RAW_LINES)) if exists else []
    kept = filter_level(raw, level)[-lines:] if lines > 0 else []
    return {"file": LOG_FILE, "exists": exists, "size": size, "lines": kept}
