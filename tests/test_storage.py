"""Tests for file storage: MIME sniffing, sanitising, size limit and permissions."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from rendiciones.storage import (
    FileStore,
    UploadError,
    sanitize_filename,
    sanitize_relative_folder,
    sniff_mime,
)

PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.7\n", "application/pdf"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (PNG_HEAD, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "image/heic"),
        (b"<html><script>", None),
        (b"MZ\x90\x00", None),
    ],
)
def test_sniff_mime(head: bytes, expected: str | None) -> None:
    """MIME detection uses magic bytes only."""
    result = sniff_mime(head)

    assert result == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\boleta.pdf", "boleta.pdf"),
        ("Boleta Clínica<script>.pdf", "Boleta Clinica_script_.pdf"),
        ("...", "archivo"),
    ],
)
def test_sanitize_filename(name: str, expected: str) -> None:
    """Filenames are reduced to a safe ASCII basename."""
    result = sanitize_filename(name)

    assert result == expected


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (None, ""),
        ("boleta.pdf", ""),
        ("Octubre/Consulta Dra/boleta.pdf", "Octubre/Consulta Dra"),
        ("./a//b/c.pdf", "a/b"),
    ],
)
def test_sanitize_relative_folder(relative: str | None, expected: str) -> None:
    """Browser relative paths keep only their sanitised parent folder."""
    result = sanitize_relative_folder(relative)

    assert result == expected


@pytest.mark.parametrize("relative", ["../x/boleta.pdf", "a/../../b.pdf", "/etc/x.pdf", "C:/x.pdf"])
def test_sanitize_relative_folder_rejects_traversal(relative: str) -> None:
    """Absolute paths and ``..`` components are rejected."""
    with pytest.raises(UploadError):
        sanitize_relative_folder(relative)


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    """A file store with a 1 KB limit."""
    return FileStore(tmp_path / "files", tmp_path / "tmp", max_bytes=1024)


def test_store_saves_with_private_permissions(store: FileStore) -> None:
    """Stored files are content-addressed and readable only by the owner."""
    data = b"%PDF-1.4\nhello"

    stored = store.save_stream(io.BytesIO(data))

    assert stored.path.name == f"{stored.sha256}.pdf"
    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert not stored.already_existed


def test_store_dedupes_identical_content(store: FileStore) -> None:
    """Saving the same bytes twice reuses the stored file."""
    data = b"%PDF-1.4\nsame"
    first = store.save_stream(io.BytesIO(data))

    second = store.save_stream(io.BytesIO(data))

    assert second.already_existed
    assert second.path == first.path


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"%PDF-" + b"x" * 2000, "máximo"),
        (b"", "vacío"),
        (b"#!/bin/sh\necho hi", "no soportado"),
    ],
)
def test_store_rejects_bad_uploads(store: FileStore, data: bytes, message: str) -> None:
    """Oversized, empty and unsupported files are rejected and leave no temp files."""
    with pytest.raises(UploadError, match=message):
        store.save_stream(io.BytesIO(data))

    assert list(store.tmp_dir.iterdir()) == []


def test_store_delete_refuses_outside_paths(store: FileStore, tmp_path: Path) -> None:
    """Deleting a path outside the store is refused."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    with pytest.raises(ValueError, match="outside"):
        store.delete(outside)

    assert outside.exists()
