"""Minimal sd_notify for systemd Type=notify units — no third-party deps.

The trader runs as a systemd unit with Type=notify and WatchdogSec=90: systemd expects a READY=1
datagram once the service is up, then a WATCHDOG=1 ping at least every 90s or it kills and restarts
the process. This module sends those datagrams over the NOTIFY_SOCKET AF_UNIX socket systemd passes
in the environment.

Off systemd (Windows/dev, or any run where NOTIFY_SOCKET is unset) every function is a harmless
no-op returning False — AF_UNIX may not even exist on the platform, so its use is guarded. Nothing
here ever raises: a notification failure must not take the trader down.
"""
from __future__ import annotations

import os
import socket

# AF_UNIX is absent on some platforms (e.g. older Windows Pythons); guard its use.
_AF_UNIX = getattr(socket, "AF_UNIX", None)


def sd_notify(state: str) -> bool:
    """Send a single sd_notify datagram (e.g. "READY=1") to systemd. Returns True on success, False
    on any failure or when not running under systemd. Never raises."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False                       # not under systemd (or notify disabled) → no-op
    if _AF_UNIX is None:
        return False                       # platform has no AF_UNIX (e.g. Windows)
    try:
        # A leading '@' denotes the Linux abstract namespace → encode as a NUL byte.
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(_AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode(), addr)
        return True
    except Exception:
        return False                       # any failure is non-fatal


def notify_ready() -> bool:
    """Tell systemd the service is up (READY=1)."""
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    """Pet the systemd watchdog (WATCHDOG=1); must be called within WatchdogSec."""
    return sd_notify("WATCHDOG=1")


def notify_stopping() -> bool:
    """Tell systemd the service is shutting down (STOPPING=1)."""
    return sd_notify("STOPPING=1")
