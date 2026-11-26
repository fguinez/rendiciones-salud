"""Text extraction: embedded PDF text via pypdf, OCR via the tesseract binary."""

from __future__ import annotations

import functools
import io
import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

TESSERACT_CANDIDATES = ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract")
MIN_PDF_TEXT_CHARS = 40
MAX_OCR_PAGES = 5
OCR_TIMEOUT_S = 120


@functools.cache
def tesseract_path() -> str | None:
    """Locate the tesseract binary."""
    for candidate in TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("tesseract")


@functools.cache
def ocr_language() -> str:
    """Use Spanish if its traineddata is installed, otherwise English."""
    binary = tesseract_path()
    if binary is None:
        return "eng"
    try:
        out = subprocess.run(
            [binary, "--list-langs"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "eng"
    langs = {line.strip() for line in (out.stdout + out.stderr).splitlines()}
    return "spa" if "spa" in langs else "eng"


def _register_heif() -> None:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:  # pragma: no cover - optional at runtime
        logger.warning("pillow-heif not available; HEIC files cannot be read")


_register_heif()


def load_image(path: Path) -> Image.Image:
    """Open an image (incl. HEIC), apply EXIF rotation and convert to RGB."""
    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def ocr_image(image: Image.Image) -> str:
    """Run tesseract on a PIL image and return the recognised text."""
    binary = tesseract_path()
    if binary is None:
        logger.warning("tesseract not found; OCR skipped")
        return ""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    try:
        result = subprocess.run(
            [binary, "stdin", "stdout", "-l", ocr_language(), "--psm", "3"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=OCR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("tesseract failed to run")
        return ""
    if result.returncode != 0:
        logger.warning("tesseract exited with code %d", result.returncode)
    return result.stdout.decode("utf-8", errors="replace")


def pdf_text(path: Path) -> str:
    """Extract embedded text from a PDF (empty string on failure)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    except Exception:
        logger.warning("pypdf could not read a document")
        return ""


def pdf_ocr(path: Path) -> str:
    """Render the first pages of a scanned PDF and OCR them."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover
        return ""
    texts: list[str] = []
    try:
        pdf = pdfium.PdfDocument(str(path))
        for index in range(min(len(pdf), MAX_OCR_PAGES)):
            bitmap = pdf[index].render(scale=200 / 72)
            texts.append(ocr_image(bitmap.to_pil().convert("RGB")))
        pdf.close()
    except Exception:
        logger.warning("pdf rendering for OCR failed")
    return "\n".join(texts)


def extract_text(path: Path, mime: str) -> tuple[str, str]:
    """Return ``(text, method)`` where method is ``pdf_text``, ``ocr`` or ``none``."""
    if mime == "application/pdf":
        text = pdf_text(path)
        if len(text.strip()) >= MIN_PDF_TEXT_CHARS:
            return text, "pdf_text"
        ocr = pdf_ocr(path)
        return (ocr, "ocr") if ocr.strip() else (text, "pdf_text")
    if mime.startswith("image/"):
        try:
            return ocr_image(load_image(path)), "ocr"
        except Exception:
            logger.warning("image could not be decoded for OCR")
            return "", "none"
    return "", "none"
