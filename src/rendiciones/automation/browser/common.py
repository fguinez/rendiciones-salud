"""Pieces shared by the Colmena and Chubb browser clients."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from playwright.sync_api import Page

from rendiciones.config import FILE_MODE, ensure_private_dir
from rendiciones.db import SubmissionMarker

logger = logging.getLogger(__name__)

MARKER_FINAL_CLICK = "final_click"
MARKER_DONE = "done"


class SubmissionLedger(Protocol):
    """Idempotency store (implemented by ``rendiciones.db.Repository``)."""

    def get_marker(self, rendicion_id: int, portal: str) -> SubmissionMarker | None: ...

    def set_marker(
        self, rendicion_id: int, portal: str, state: str, request_id: str | None = None
    ) -> None: ...

    def known_request_ids(self, portal: str) -> set[str]: ...


class Clock(Protocol):
    """Injectable 'now' for tests."""

    def __call__(self) -> datetime: ...


def take_screenshot(page: Page, directory: Path, name: str) -> Path:
    """Full-page screenshot saved with 0600 permissions; returns its path."""
    ensure_private_dir(directory)
    path = directory / f"{name}.png"
    data = page.screenshot(full_page=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    path.chmod(FILE_MODE)
    return path


@contextmanager
def private_tmpdir(parent: Path) -> Iterator[Path]:
    """Temporary 0700 directory under ``parent`` for converted uploads."""
    ensure_private_dir(parent)
    with tempfile.TemporaryDirectory(dir=parent) as tmp:
        path = Path(tmp)
        path.chmod(0o700)
        yield path


def stamp(now: datetime) -> str:
    """Filesystem-safe timestamp."""
    return now.strftime("%Y%m%dT%H%M%S")
