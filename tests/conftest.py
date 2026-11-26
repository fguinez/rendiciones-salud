"""Shared fixtures. All documents are synthetic (fake names, RUTs and amounts)."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from rendiciones.api import create_app
from rendiciones.config import CSRF_HEADER, CSRF_VALUE, Settings
from rendiciones.domain import Status
from rendiciones.recognition.rules import RuleBasedClassifier
from rendiciones.services import RendicionService

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEXTS_DIR = FIXTURES_DIR / "texts"

PdfFactory = Callable[[str], bytes]


def fixture_text(name: str) -> str:
    """Return the content of ``tests/fixtures/texts/<name>.txt``."""
    return (TEXTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def render_pdf(text: str) -> bytes:
    """Render ``text`` into a deterministic one-page PDF with embedded text."""
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, invariant=True)
    y = 800
    for line in text.splitlines():
        pdf.drawString(40, y, line)
        y -= 16
    pdf.save()
    return buf.getvalue()


@pytest.fixture
def pdf_of() -> PdfFactory:
    """Build the PDF bytes of a text fixture by name."""
    return lambda name: render_pdf(fixture_text(name))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at an isolated temporary data directory."""
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def service(settings: Settings) -> RendicionService:
    """Service using the offline rule-based classifier."""
    return RendicionService(settings, RuleBasedClassifier())


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """API client that sends the anti-CSRF header by default."""
    app = create_app(settings, RuleBasedClassifier())
    with TestClient(app, headers={CSRF_HEADER: CSRF_VALUE}) as test_client:
        yield test_client


@pytest.fixture
def ingest(service: RendicionService, pdf_of: PdfFactory) -> Callable[..., int]:
    """Ingest a text fixture as a PDF and return the new document id."""

    def _ingest(name: str, relative_path: str | None = None, batch: str = "b1") -> int:
        result = service.ingest(io.BytesIO(pdf_of(name)), f"{name}.pdf", relative_path, batch)
        assert result.ok
        assert result.document_id is not None
        return result.document_id

    return _ingest


@pytest.fixture
def lista_rendicion(service: RendicionService, ingest: Callable[..., int]) -> int:
    """A rendición (boleta de laboratorio + orden médica) already marked ``lista``."""
    ingest("boleta_laboratorio")
    ingest("orden_medica")
    grouping = service.group_pending("b1")
    rid = grouping.created[0]
    service.set_status(rid, Status.LISTA)
    return rid
