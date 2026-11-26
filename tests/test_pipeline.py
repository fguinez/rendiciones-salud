"""Tests for the Colmena → Chubb pipeline using fake portal clients."""

from __future__ import annotations

from pathlib import Path

import pytest

from rendiciones.automation.base import PortalState, StatusResult
from rendiciones.automation.fake import FakeChubbClient, FakeColmenaClient, FakePortalClient
from rendiciones.automation.loader import PortalConfigError, load_clients
from rendiciones.domain import DocRole, InvalidTransitionError, Status
from rendiciones.pipeline import (
    Outcome,
    Pipeline,
    PipelineReport,
    Portal,
    record_result,
    record_submission,
)
from rendiciones.services import RendicionService


def status(state: PortalState, monto: int | None = None) -> StatusResult:
    """Scripted status result."""
    return StatusResult(state=state, monto=monto)


@pytest.fixture
def colmena() -> FakeColmenaClient:
    """Fake Colmena approving everything."""
    return FakeColmenaClient()


@pytest.fixture
def chubb() -> FakePortalClient:
    """Fake Chubb approving everything."""
    return FakePortalClient("chubb")


def test_full_happy_path(
    service: RendicionService,
    lista_rendicion: int,
    colmena: FakeColmenaClient,
    chubb: FakePortalClient,
) -> None:
    """One run takes a ``lista`` rendición through both portals to ``chubb_aprobada``."""
    pipeline = Pipeline(service, colmena, chubb)

    report = pipeline.run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert view.status == Status.CHUBB_APROBADA
    assert report.colmena_submitted == [lista_rendicion]
    assert report.chubb_approved == [lista_rendicion]
    assert view.colmena_request_id == "COLMENA-00001"
    assert view.colmena_monto_bonificado == 16800
    assert view.chubb_request_id == "CHUBB-00001"
    assert view.bonificacion_document is not None
    assert view.bonificacion_document.role == DocRole.BONIFICACION_COLMENA
    assert chubb.submitted[0].bonificacion_document is not None
    kinds = [e.kind for e in service.repo.events(lista_rendicion)]
    assert "comprobante_adjuntado" in kinds


def test_submitted_rendicion_has_absolute_paths(
    service: RendicionService,
    lista_rendicion: int,
    colmena: FakeColmenaClient,
    chubb: FakePortalClient,
) -> None:
    """Portal clients receive documents with absolute, existing file paths."""
    Pipeline(service, colmena, chubb).submit_ready(PipelineReport())

    docs = colmena.submitted[0].support_documents

    assert len(docs) == 2
    assert all(d.path.is_absolute() and d.path.exists() for d in docs)


def test_pending_colmena_stays_enviada(
    service: RendicionService, lista_rendicion: int, chubb: FakePortalClient
) -> None:
    """A pending Colmena request keeps the rendición in ``colmena_enviada``."""
    colmena = FakeColmenaClient(decide=lambda _: status(PortalState.PENDING))

    report = Pipeline(service, colmena, chubb).run_once()

    assert report.pending == [lista_rendicion]
    assert service.repo.get_rendicion(lista_rendicion).status == Status.COLMENA_ENVIADA
    assert chubb.submitted == []


def test_colmena_rejection_stops_pipeline(
    service: RendicionService, lista_rendicion: int, chubb: FakePortalClient
) -> None:
    """A Colmena rejection is recorded and nothing is sent to Chubb."""
    colmena = FakeColmenaClient(decide=lambda _: status(PortalState.REJECTED))

    report = Pipeline(service, colmena, chubb).run_once()

    assert report.colmena_rejected == [lista_rendicion]
    assert service.repo.get_rendicion(lista_rendicion).status == Status.COLMENA_RECHAZADA
    assert chubb.submitted == []


def test_chubb_rejection_is_recorded(
    service: RendicionService, lista_rendicion: int, colmena: FakeColmenaClient
) -> None:
    """A Chubb rejection ends in ``chubb_rechazada`` with the result stored."""
    chubb = FakePortalClient("chubb", decide=lambda _: status(PortalState.REJECTED))

    Pipeline(service, colmena, chubb).run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert view.status == Status.CHUBB_RECHAZADA
    assert view.chubb_result is not None
    assert view.chubb_result["outcome"] == "rejected"


@pytest.mark.parametrize("state", [PortalState.APPROVED, PortalState.REJECTED])
def test_chubb_liquidacion_is_attached(
    service: RendicionService,
    lista_rendicion: int,
    colmena: FakeColmenaClient,
    state: PortalState,
) -> None:
    """Chubb's liquidación is attached once, whether the request was paid or rejected."""
    chubb = FakeChubbClient(decide=lambda _: status(state, 5000))
    pipeline = Pipeline(service, colmena, chubb)

    report = pipeline.run_once()
    pipeline.run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert report.chubb_liquidaciones == [lista_rendicion]
    assert view.chubb_response_document is not None
    assert view.chubb_response_document.role == DocRole.RESPUESTA_CHUBB
    assert chubb.downloads == ["CHUBB-00001"]


def test_chubb_liquidacion_failure_keeps_status(
    service: RendicionService, lista_rendicion: int, colmena: FakeColmenaClient
) -> None:
    """A liquidación that cannot be downloaded leaves the result recorded and is retried."""
    chubb = FakeChubbClient()
    calls: list[str] = []

    def broken(request_id: str, dest: Path) -> Path | None:
        calls.append(request_id)
        raise RuntimeError("boom")

    chubb.download_liquidacion = broken  # type: ignore[method-assign]
    pipeline = Pipeline(service, colmena, chubb)

    pipeline.run_once()
    pipeline.run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert view.status == Status.CHUBB_APROBADA
    assert view.chubb_response_document is None
    assert calls == ["CHUBB-00001", "CHUBB-00001"]


def test_submit_failure_moves_to_error(
    service: RendicionService, lista_rendicion: int, chubb: FakePortalClient
) -> None:
    """A failed submission puts the rendición in ``error`` and it can be retried."""
    colmena = FakeColmenaClient(fail_submit=True)

    report = Pipeline(service, colmena, chubb).run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert report.errors == [lista_rendicion]
    assert view.status == Status.ERROR
    assert view.status_before_error == Status.LISTA
    assert service.set_status(lista_rendicion, Status.LISTA).status == Status.LISTA


def test_client_exception_moves_to_error(
    service: RendicionService, lista_rendicion: int, chubb: FakePortalClient
) -> None:
    """Unexpected client exceptions are caught and recorded without leaking details."""

    class BrokenColmena(FakeColmenaClient):
        def download_bonificacion(self, request_id: str, dest: Path) -> Path:
            raise RuntimeError("secret detail")

    report = Pipeline(service, BrokenColmena(), chubb).run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert report.errors == [lista_rendicion]
    assert view.status == Status.ERROR
    assert view.status_before_error == Status.COLMENA_APROBADA
    assert "secret" not in (view.error_message or "")


def test_record_chubb_submission_requires_bonificacion(
    service: RendicionService, lista_rendicion: int
) -> None:
    """Chubb submissions are refused until the Colmena comprobante is attached."""
    record_submission(service, lista_rendicion, Portal.COLMENA, "C-1")
    record_result(service, lista_rendicion, Portal.COLMENA, Outcome.APPROVED, 1000)

    with pytest.raises(InvalidTransitionError):
        record_submission(service, lista_rendicion, Portal.CHUBB, "X-1")

    assert service.repo.get_rendicion(lista_rendicion).status == Status.COLMENA_APROBADA


@pytest.mark.parametrize("spec", [None, "os:system", "nocolon"])
def test_loader_rejects_bad_specs(spec: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``rendiciones.*`` factories are loaded."""
    monkeypatch.delenv("RENDICIONES_PORTAL_FACTORY", raising=False)

    with pytest.raises(PortalConfigError):
        load_clients(spec)


def test_loader_fake() -> None:
    """``fake`` loads the in-memory clients."""
    colmena, chubb = load_clients("fake")

    assert colmena.name == "colmena"
    assert chubb.name == "chubb"
