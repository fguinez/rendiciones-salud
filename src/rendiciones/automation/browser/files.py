"""File preparation for portal uploads: type conversion and size limits.

Colmena accepts JPG/PNG/PDF with **5 MB total**; Chubb accepts PNG/JPEG/GIF/BMP/PDF with
**10 MB per file**. Images that do not fit are recompressed with Pillow into a private
temp directory; PDFs cannot be shrunk, so an oversized PDF is a clear error raised before
anything is uploaded.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from rendiciones.automation.base import PortalError
from rendiciones.config import FILE_MODE

logger = logging.getLogger(__name__)

COLMENA_MAX_TOTAL = 5_000_000  # maxFileSize=5e6 in Colmena's JS
COLMENA_MIN_FILE = 10
CHUBB_MAX_FILE = 10_485_760
COLMENA_MIMES = frozenset({"application/pdf", "image/jpeg", "image/png"})
CHUBB_MIMES = frozenset({"application/pdf", "image/jpeg", "image/png", "image/gif", "image/bmp"})
_IMAGE_STEPS: tuple[tuple[int, int], ...] = ((2400, 85), (2000, 75), (1600, 65), (1200, 55))


@dataclass(frozen=True)
class UploadFile:
    """A file ready to hand to ``set_input_files``."""

    source_id: int  # document id (for logs)
    path: Path
    mime: str
    size: int


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:  # pragma: no cover - optional at runtime
        pass


def to_jpeg(source: Path, target: Path, max_side: int, quality: int) -> Path:
    """Write ``source`` as an RGB JPEG no larger than ``max_side`` px (0600)."""
    _register_heif()
    with Image.open(source) as img:
        image = ImageOps.exif_transpose(img).convert("RGB")
        image.thumbnail((max_side, max_side))
        image.save(target, "JPEG", quality=quality, optimize=True)
    target.chmod(FILE_MODE)
    return target


def _converted(
    doc_id: int, path: Path, mime: str, work_dir: Path, allowed: frozenset[str]
) -> UploadFile:
    if mime in allowed:
        return UploadFile(doc_id, path, mime, path.stat().st_size)
    if mime.startswith("image/"):
        target = to_jpeg(path, work_dir / f"doc-{doc_id}.jpg", 4000, 90)
        return UploadFile(doc_id, target, "image/jpeg", target.stat().st_size)
    raise PortalError(f"Tipo de archivo no aceptado por el portal (documento {doc_id})")


def _shrink(item: UploadFile, work_dir: Path, step: int) -> UploadFile:
    side, quality = _IMAGE_STEPS[step]
    target = to_jpeg(item.path, work_dir / f"doc-{item.source_id}-s{step}.jpg", side, quality)
    return UploadFile(item.source_id, target, "image/jpeg", target.stat().st_size)


def prepare_colmena(files: Sequence[tuple[int, Path, str]], work_dir: Path) -> list[UploadFile]:
    """Convert to JPG/PNG/PDF and fit the 5 MB total, recompressing images if needed."""
    items = [_converted(i, p, m, work_dir, COLMENA_MIMES) for i, p, m in files]
    for item in items:
        if item.size < COLMENA_MIN_FILE:
            raise PortalError(f"Archivo vacío o dañado (documento {item.source_id})")
    step = 0
    while sum(i.size for i in items) > COLMENA_MAX_TOTAL and step < len(_IMAGE_STEPS):
        items = [_shrink(i, work_dir, step) if i.mime.startswith("image/") else i for i in items]
        step += 1
    total = sum(i.size for i in items)
    if total > COLMENA_MAX_TOTAL:
        raise PortalError(
            f"Los documentos suman {total / 1e6:.1f} MB y Colmena acepta 5 MB en total; "
            "reduce o divide los PDF antes de enviar"
        )
    if step:
        logger.info("colmena: images recompressed (%d passes), total %d bytes", step, total)
    return items


def prepare_chubb(files: Sequence[tuple[int, Path, str]], work_dir: Path) -> list[UploadFile]:
    """Convert to accepted types and fit 10 MB per file, recompressing images if needed."""
    result = []
    for doc_id, path, mime in files:
        item = _converted(doc_id, path, mime, work_dir, CHUBB_MIMES)
        step = 0
        while (
            item.size > CHUBB_MAX_FILE
            and item.mime.startswith("image/")
            and step < len(_IMAGE_STEPS)
        ):
            item = _shrink(item, work_dir, step)
            step += 1
        if item.size > CHUBB_MAX_FILE:
            raise PortalError(
                f"El documento {doc_id} pesa más de 10 MB, el máximo de Chubb por archivo"
            )
        result.append(item)
    return result
