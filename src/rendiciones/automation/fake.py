"""In-memory portal clients for tests and demos."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

from rendiciones.automation.base import PortalState, StatusResult, SubmissionResult
from rendiciones.domain import RendicionView


def minimal_pdf(note: str, title: str = "COMPROBANTE DE BONIFICACION") -> bytes:
    """Build a tiny, well-formed one-page PDF (with xref) whose page shows ``title note``."""
    safe = "".join(c for c in note if c.isalnum() or c in " -_:")
    stream = f"BT /F1 12 Tf 20 100 Td ({title} {safe}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj".encode() + body + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FakePortalClient:
    """Records submissions and returns scripted statuses.

    ``decide`` maps a request id to the status returned by ``check_status``; by default
    every request is approved with 70% of the rendición amount.
    """

    def __init__(
        self,
        name: str,
        decide: Callable[[str], StatusResult] | None = None,
        fail_submit: bool = False,
    ) -> None:
        self.name = name
        self.submitted: list[RendicionView] = []
        self.amounts: dict[str, int] = {}
        self._decide = decide
        self._fail_submit = fail_submit
        self._counter = itertools.count(1)

    def submit(self, rendicion: RendicionView) -> SubmissionResult:
        """Accept the rendición and assign a fake request id."""
        if self._fail_submit:
            return SubmissionResult(ok=False, message="Portal no disponible")
        self.submitted.append(rendicion)
        request_id = f"{self.name.upper()}-{next(self._counter):05d}"
        self.amounts[request_id] = rendicion.monto or 0
        return SubmissionResult(ok=True, request_id=request_id, raw={"fake": True})

    def check_status(self, request_id: str) -> StatusResult:
        """Return the scripted status for ``request_id``."""
        if self._decide is not None:
            return self._decide(request_id)
        monto = int(self.amounts.get(request_id, 0) * 0.7)
        return StatusResult(state=PortalState.APPROVED, monto=monto, raw={"fake": True})


class FakeColmenaClient(FakePortalClient):
    """Fake Colmena that also produces a comprobante PDF."""

    def __init__(
        self,
        decide: Callable[[str], StatusResult] | None = None,
        fail_submit: bool = False,
    ) -> None:
        super().__init__("colmena", decide=decide, fail_submit=fail_submit)
        self.downloads: list[str] = []

    def download_bonificacion(self, request_id: str, dest: Path) -> Path:
        """Write a small unique PDF named after the request id."""
        self.downloads.append(request_id)
        target = dest / f"bonificacion-{request_id}.pdf"
        target.write_bytes(minimal_pdf(request_id))
        return target


class FakeChubbClient(FakePortalClient):
    """Fake Chubb that also produces a liquidación PDF once a request is resolved."""

    def __init__(
        self,
        decide: Callable[[str], StatusResult] | None = None,
        fail_submit: bool = False,
    ) -> None:
        super().__init__("chubb", decide=decide, fail_submit=fail_submit)
        self.downloads: list[str] = []

    def download_liquidacion(self, request_id: str, dest: Path) -> Path | None:
        """Write a small unique PDF, or None while the request is still pending."""
        if self.check_status(request_id).state == PortalState.PENDING:
            return None
        self.downloads.append(request_id)
        target = dest / f"liquidacion-chubb-{request_id}.pdf"
        target.write_bytes(minimal_pdf(request_id, title="LIQUIDACION DE REEMBOLSO"))
        return target


def fake_factory() -> tuple[FakeColmenaClient, FakeChubbClient]:
    """Factory usable as ``RENDICIONES_PORTAL_FACTORY=rendiciones.automation.fake:fake_factory``."""
    return FakeColmenaClient(), FakeChubbClient()
