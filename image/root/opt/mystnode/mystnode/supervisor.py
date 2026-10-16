"""Node service entry point: `python3 -m mystnode.supervisor`.

Runs the upstream entrypoint (docker-entrypoint.sh -> myst) once the user has
accepted the Mysterium terms in the dashboard, keeps the NodeUI password equal
to Umbrel's APP_PASSWORD, and restarts myst in place after a clean exit. After
TequilAPI's POST /stop (the dashboard's "Restart node") myst 1.39.7 exits with
code 1, so that restart goes through Docker's `restart: on-failure` instead.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import bcrypt

from .common import env, resolve_ipv4, setup_logging

log = logging.getLogger("mystnode.supervisor")


def ensure_password(path: Path, password: str) -> bool:
    """Make `path` hold a bcrypt hash of `password`. myst reads this file on
    every login (core/auth/credentials.go), so rewriting it takes effect at once.
    Returns True when the file was (re)written."""
    try:
        with open(path, encoding="utf-8") as f:
            current = f.readline().strip()
        if current and bcrypt.checkpw(password.encode(), current.encode()):
            return False
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(hashed)
    os.replace(tmp, path)
    return True


def wait_for_marker(marker: Path, poll: float, stop: threading.Event) -> bool:
    if marker.exists():
        return True
    log.info("waiting for the Mysterium terms to be accepted in the dashboard")
    while not stop.is_set():
        if marker.exists():
            return True
        stop.wait(poll)
    return False


def build_command(entrypoint: str, bind_ip: str, data: Path) -> list[str]:
    # Global flags (config.RegisterFlagsNode) precede the `service` command;
    # --agreed-terms-and-conditions belongs to `service`.
    return [
        entrypoint,
        f"--ui.address={bind_ip}",
        f"--log-dir={data}/logs",
        f"--node-ui-dir={data}/nodeui",
        "--transactor.provider.try-free-registration",
        "service",
        "--agreed-terms-and-conditions",
    ]


def run(
    cmd: list[str],
    stop: threading.Event,
    restart_delay: float = 2.0,
    on_spawn: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    while True:
        p = subprocess.Popen(cmd)
        if on_spawn is not None:
            on_spawn(p)
        p.wait()
        if stop.is_set():
            return 0
        if p.returncode == 0:
            log.info("myst exited cleanly (restart requested); restarting")
            time.sleep(restart_delay)
            continue
        log.error("myst exited with code %s", p.returncode)
        return p.returncode


def _guard(path: Path, password: str, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            if ensure_password(path, password):
                log.info("restored NodeUI password")
        except OSError as e:
            log.warning("cannot check NodeUI password file: %s", e)


def main() -> None:
    setup_logging("supervisor")
    # Keeps myst's log files readable by the dashboard (uid 1000).
    os.umask(0o022)

    password = env("MYSTNODE_PASSWORD")
    data = Path(env("MYSTNODE_NODE_DATA", "/var/lib/mysterium-node"))
    web = Path(env("MYSTNODE_WEB_DATA", "/umbrel-web"))
    entrypoint = env("MYSTNODE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")
    guard_interval = float(env("MYSTNODE_GUARD_INTERVAL", "15"))
    marker_poll = float(env("MYSTNODE_MARKER_POLL", "2"))

    bind_ip = resolve_ipv4(env("MYSTNODE_BIND_HOST", "host.docker.internal"))
    (data / "logs").mkdir(parents=True, exist_ok=True)
    (data / "nodeui").mkdir(parents=True, exist_ok=True)
    pass_file = data / "nodeui-pass"
    ensure_password(pass_file, password)

    stop = threading.Event()
    current: list[subprocess.Popen | None] = [None]

    def on_signal(signum, _frame):
        stop.set()
        child = current[0]
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if not wait_for_marker(web / "terms-accepted.json", marker_poll, stop):
        sys.exit(0)

    threading.Thread(
        target=_guard, args=(pass_file, password, guard_interval, stop), daemon=True
    ).start()

    def on_spawn(p: subprocess.Popen) -> None:
        current[0] = p
        # A signal that arrived between Popen and here found no child to stop.
        if stop.is_set():
            p.terminate()

    log.info("starting myst (NodeUI on %s:4449)", bind_ip)
    sys.exit(run(build_command(entrypoint, bind_ip, data), stop, on_spawn=on_spawn))


if __name__ == "__main__":
    main()
