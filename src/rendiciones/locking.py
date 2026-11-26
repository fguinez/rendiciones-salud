"""Cross-process lock for anything that drives the portals' shared browser profile.

Pipeline passes (from the server or ``run-pipeline``) and interactive logins all take it,
so two of them never open ``browser-profile/`` or process the same rendiciones at once.
It is an ``flock`` on ``data/automation.lock``: the OS releases it if the holder dies.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rendiciones.config import FILE_MODE

LOCK_FILE = "automation.lock"


class AutomationBusyError(RuntimeError):
    """Another pipeline pass or login is using the browser."""


class Lease:
    """A held lock; call ``release`` (from any thread) when done."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        """Free the lock. Safe to call twice."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.ftruncate(fd, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class AutomationLock:
    """Non-blocking exclusive lock; the holder's description is kept in the lock file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> AutomationLock:
        """The lock shared by everything using ``data_dir``."""
        return cls(data_dir / LOCK_FILE)

    def acquire(self, holder: str) -> Lease:
        """Take the lock or raise ``AutomationBusyError`` naming the current holder."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            current = self.holder()
            raise AutomationBusyError(
                f"El navegador está en uso ({current or 'otro proceso'}). "
                "Espera a que termine e intenta de nuevo."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{holder}, pid {os.getpid()}".encode())
        return Lease(fd)

    @contextmanager
    def hold(self, holder: str) -> Iterator[None]:
        """``with lock.hold("run-pipeline"):`` around browser work."""
        lease = self.acquire(holder)
        try:
            yield
        finally:
            lease.release()

    def holder(self) -> str | None:
        """Who holds the lock right now, or ``None`` when it is free."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return os.read(fd, 200).decode(errors="replace") or "otro proceso"
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)
