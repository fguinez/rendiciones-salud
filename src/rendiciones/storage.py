"""Content-addressed file storage with MIME sniffing and path sanitisation."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from rendiciones.config import FILE_MODE, ensure_private_dir

MIME_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
_CHUNK = 1024 * 1024


class UploadError(ValueError):
    """A user-facing upload rejection (message in Spanish)."""


def sniff_mime(head: bytes) -> str | None:
    """Detect a supported MIME type from magic bytes; ignore the file extension."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def sanitize_filename(name: str) -> str:
    """Return a safe display filename (basename only, ASCII, no control chars)."""
    base = PurePosixPath(name.replace("\\", "/")).name
    normalized = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    cleaned = _SAFE_CHARS.sub("_", normalized).strip(" .")
    return cleaned[:120] or "archivo"


def sanitize_relative_folder(relative_path: str | None) -> str:
    """Return the sanitised parent folder of a browser-provided relative path.

    Raises UploadError on absolute paths or ``..`` components (path traversal).
    """
    if not relative_path:
        return ""
    raw = relative_path.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise UploadError("Ruta inválida (absoluta)")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError("Ruta inválida (contiene '..')")
    folder_parts = [sanitize_filename(p) for p in parts[:-1]]
    return "/".join(folder_parts)[:255]


@dataclass(frozen=True)
class StoredFile:
    """Result of persisting an upload."""

    sha256: str
    path: Path
    mime: str
    size: int
    already_existed: bool


class FileStore:
    """Stores files under ``root/<sha[:2]>/<sha><ext>`` with 0600 permissions."""

    def __init__(self, root: Path, tmp_dir: Path, max_bytes: int) -> None:
        self.root = ensure_private_dir(root)
        self.tmp_dir = ensure_private_dir(tmp_dir)
        self.max_bytes = max_bytes

    def path_for(self, sha256: str, mime: str) -> Path:
        """Return the canonical storage path for a digest and MIME type."""
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("invalid digest")
        return self.root / sha256[:2] / f"{sha256}{MIME_EXTENSIONS[mime]}"

    def save_stream(self, stream: BinaryIO) -> StoredFile:
        """Copy ``stream`` to the store, enforcing the size limit and MIME whitelist."""
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self.tmp_dir, prefix="up-")
        tmp_path = Path(tmp_name)
        try:
            head = b""
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        limit_mb = self.max_bytes // (1024 * 1024)
                        raise UploadError(f"Archivo supera el máximo de {limit_mb} MB")
                    if len(head) < 32:
                        head = (head + chunk)[:32]
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise UploadError("Archivo vacío")
            mime = sniff_mime(head)
            if mime is None:
                raise UploadError(
                    "Tipo de archivo no soportado (se aceptan PDF, JPG, PNG, HEIC, WEBP)"
                )
            sha = digest.hexdigest()
            final = self.path_for(sha, mime)
            ensure_private_dir(final.parent)
            existed = final.exists()
            if existed:
                tmp_path.unlink()
            else:
                tmp_path.chmod(FILE_MODE)
                tmp_path.replace(final)
            return StoredFile(sha256=sha, path=final, mime=mime, size=size, already_existed=existed)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def save_path(self, source: Path) -> StoredFile:
        """Store a local file (e.g. a portal download)."""
        with source.open("rb") as fh:
            return self.save_stream(fh)

    def delete(self, path: Path) -> None:
        """Delete a stored file, refusing paths outside the store."""
        resolved = path.resolve()
        if self.root.resolve() not in resolved.parents:
            raise ValueError("path outside store")
        resolved.unlink(missing_ok=True)
