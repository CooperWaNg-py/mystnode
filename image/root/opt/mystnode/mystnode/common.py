"""Helpers shared by the supervisor (node service) and the dashboard (web service)."""

import logging
import os
import socket
import struct
import time

RTF_GATEWAY = 0x2


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if default is None:
            raise SystemExit(f"missing required environment variable {name}")
        return default
    return value


def resolve_ipv4(host: str, attempts: int = 30, delay: float = 1.0) -> str:
    """First IPv4 address of `host`. Docker's embedded DNS and /etc/hosts
    entries can lag container start, so it retries before giving up."""
    for attempt in range(attempts):
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            if infos:
                return infos[0][4][0]
        except OSError:
            pass
        if attempt < attempts - 1:
            time.sleep(delay)
    raise SystemExit(f"cannot resolve {host}")


def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """IPv4 default gateway from the kernel routing table. Inside a bridge-network
    container that is the bridge's host-side address."""
    try:
        with open(route_file, encoding="ascii") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            destination = fields[1]
            gateway = int(fields[2], 16)
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if destination == "00000000" and flags & RTF_GATEWAY:
            return socket.inet_ntoa(struct.pack("<I", gateway))
    return None


def setup_logging(name: str) -> None:
    logging.basicConfig(
        level=os.environ.get("MYSTNODE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s " + name + ": %(message)s",
    )
