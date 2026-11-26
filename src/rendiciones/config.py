"""Runtime settings and secure data-directory setup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_REQUEST = 200
CSRF_HEADER = "X-Rendiciones-Client"
CSRF_VALUE = "1"
DIR_MODE = 0o700
FILE_MODE = 0o600

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Paths and limits used by the app."""

    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_upload_bytes: int = MAX_UPLOAD_BYTES

    @property
    def db_path(self) -> Path:
        """SQLite database location."""
        return self.data_dir / "rendiciones.sqlite"

    @property
    def files_dir(self) -> Path:
        """Content-addressed document store."""
        return self.data_dir / "files"

    @property
    def tmp_dir(self) -> Path:
        """Scratch space for uploads and portal downloads."""
        return self.data_dir / "tmp"

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``RENDICIONES_*`` environment variables."""
        data_dir = Path(os.environ.get("RENDICIONES_DATA_DIR", _PROJECT_ROOT / "data"))
        port = int(os.environ.get("RENDICIONES_PORT", DEFAULT_PORT))
        return cls(data_dir=data_dir.resolve(), port=port)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) with 0700 permissions and enforce the mode."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path.chmod(DIR_MODE)
    return path


def prepare_data_dir(settings: Settings) -> None:
    """Create the data directory tree with private permissions."""
    os.umask(0o077)
    for directory in (settings.data_dir, settings.files_dir, settings.tmp_dir):
        ensure_private_dir(directory)
    if settings.db_path.exists():
        settings.db_path.chmod(FILE_MODE)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure logging. Callers must only log ids and counts, never document content."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
