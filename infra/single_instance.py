"""PORTED from mbappe src/single_instance.py — cross-process single-instance guard for the live run
loop. Defence-in-depth advisory flock: the first live-run process takes an exclusive, non-blocking
lock for its lifetime; a second run cannot acquire it and is refused. The kernel releases it on death
(even crash/OOM/SIGKILL), so no stale lock blocks a legitimate systemd restart.

SIG adaptation: lock filename (sig-bot-run.lock) + the test-bypass env var (SIG_TEST_RUN_LOCK).
Same dual-runtime failure class it guards in mbappe applies here: never let two processes drive the
same SIG account.
"""
from __future__ import annotations

import os
from pathlib import Path

from infra.config import DATA_DIR


class SingleInstanceError(RuntimeError):
    """Raised when another live trading run already holds the instance lock."""


RUN_LOCK_PATH = DATA_DIR / "sig-bot-run.lock"
_held_fd: int | None = None


def acquire_run_lock(lock_path: Path = RUN_LOCK_PATH) -> None:
    """Acquire the exclusive live-run lock, or raise SingleInstanceError. POSIX-only (flock via
    fcntl); on Windows this is a no-op (production is Linux). Bypassed under pytest unless
    SIG_TEST_RUN_LOCK=1."""
    global _held_fd
    if os.name != "posix":
        return
    if "PYTEST_CURRENT_TEST" in os.environ and os.getenv("SIG_TEST_RUN_LOCK") != "1":
        return
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        holder = ""
        try:
            holder = lock_path.read_text().strip()
        except OSError:
            pass
        raise SingleInstanceError(
            f"another live trading run already holds {lock_path}"
            + (f" (holder pid={holder})" if holder else "")
            + ". Refusing to start a second instance against the same account (dual-runtime guard). "
            "Stop the other instance first."
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    _held_fd = fd


def release_run_lock() -> None:
    """Release the lock. Mainly for tests — the kernel releases it on exit."""
    global _held_fd
    if _held_fd is None:
        return
    import fcntl

    try:
        fcntl.flock(_held_fd, fcntl.LOCK_UN)
    finally:
        os.close(_held_fd)
        _held_fd = None
