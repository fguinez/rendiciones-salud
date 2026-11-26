"""Playwright tests against local look-alike portal pages (tests/fixtures/portals/).

The browser only talks to 127.0.0.1 (``allowed_hosts``); real portals are never touched.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

from portal_server import PortalServer
from rendiciones.accounts import STATE_FILE, LoginLog, LoginResult
from rendiciones.automation.base import PortalError, PortalState, SessionExpiredError
from rendiciones.automation.browser.autologin import AutoLogin
from rendiciones.automation.browser.chubb import ChubbBrowserClient
from rendiciones.automation.browser.colmena import ColmenaBrowserClient
from rendiciones.automation.browser.login import wait_for_login
from rendiciones.automation.browser.selectors import ChubbUrls, ColmenaUrls
from rendiciones.automation.browser.session import BrowserOptions, BrowserSession
from rendiciones.automation.fake import FakePortalClient, minimal_pdf
from rendiciones.config import Settings
from rendiciones.credentials import Credential, MemoryStore
from rendiciones.domain import DocRole, Status, TipoPrestacion
from rendiciones.pipeline import Outcome, Pipeline, Portal, record_result, record_submission
from rendiciones.services import RendicionService

pytestmark = pytest.mark.browser

TODAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 10, 0, 0)


@pytest.fixture
def portals() -> Iterator[PortalServer]:
    """Fake portals on a random local port."""
    with PortalServer() as server:
        yield server


@pytest.fixture
def session(tmp_path: Path) -> Iterator[BrowserSession]:
    """Headless bundled Chromium restricted to 127.0.0.1, with a throwaway profile."""
    options = BrowserOptions(
        profile_dir=tmp_path / "profile",
        headless=True,
        channel=None,
        allowed_hosts=frozenset({"127.0.0.1"}),
        default_timeout_ms=10_000,
    )
    browser = BrowserSession(options)
    try:
        _ = browser.context
    except PlaywrightError as exc:  # pragma: no cover - depends on the machine
        reason = f"Chromium de Playwright no disponible: {type(exc).__name__}"
        if os.environ.get("CI"):  # CI installs Chromium, so a skip there hides real failures
            pytest.fail(reason)
        pytest.skip(reason)
    yield browser
    browser.close()


@pytest.fixture
def colmena_urls(portals: PortalServer) -> ColmenaUrls:
    """URLs of the fake Colmena."""
    return ColmenaUrls(
        base=f"{portals.origin}/afiliados/", api=f"{portals.origin}/services/afiliados/private/"
    )


@pytest.fixture
def make_colmena(
    session: BrowserSession,
    service: RendicionService,
    settings: Settings,
    colmena_urls: ColmenaUrls,
) -> Callable[..., ColmenaBrowserClient]:
    """Build a Colmena client against the fixture portal."""

    def _make(**kwargs: object) -> ColmenaBrowserClient:
        return ColmenaBrowserClient(
            session,
            service.repo,
            data_dir=settings.data_dir,
            urls=colmena_urls,
            now=lambda: NOW,
            login_timeout_s=5,
            validation_timeout_s=10,
            validation_start_timeout_s=1,
            submit_timeout_s=10,
            **kwargs,  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture
def make_chubb(
    session: BrowserSession, service: RendicionService, settings: Settings, portals: PortalServer
) -> Callable[..., ChubbBrowserClient]:
    """Build a Chubb client against the fixture portal."""

    def _make(**kwargs: object) -> ChubbBrowserClient:
        return ChubbBrowserClient(
            session,
            service.repo,
            data_dir=settings.data_dir,
            urls=ChubbUrls(base=f"{portals.origin}/chubb/"),
            today=lambda: TODAY,
            now=lambda: NOW,
            login_timeout_s=5,
            submit_timeout_s=10,
            **kwargs,  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture
def lab_rendicion(
    service: RendicionService, lista_rendicion: int, pdf_of: Callable[[str], bytes], tmp_path: Path
) -> int:
    """Lab rendición (boleta + orden + programa as 'Detalle de Prestación'), ``lista``."""
    path = tmp_path / "programa.pdf"
    path.write_bytes(pdf_of("programa_medico"))
    service.attach_file(lista_rendicion, path, "programa.pdf", DocRole.SOPORTE)
    return lista_rendicion


@pytest.fixture
def approved_rendicion(service: RendicionService, lista_rendicion: int, tmp_path: Path) -> int:
    """Rendición approved by Colmena with its comprobante attached."""
    record_submission(service, lista_rendicion, Portal.COLMENA, "987654")
    record_result(service, lista_rendicion, Portal.COLMENA, Outcome.APPROVED, 16800)
    path = tmp_path / "bono.pdf"
    path.write_bytes(minimal_pdf("987654"))
    service.attach_file(lista_rendicion, path, "bono.pdf", DocRole.BONIFICACION_COLMENA)
    return lista_rendicion


@pytest.fixture
def make_auto_login(settings: Settings) -> Callable[[str], AutoLogin]:
    """AutoLogin whose keychain holds ``password`` for both portals (fast timings)."""

    def _make(password: str) -> AutoLogin:
        store = MemoryStore()
        for portal in ("colmena", "chubb"):
            store.set(portal, Credential("11.111.111-1", password, "2026-01-01T00:00:00+00:00"))
        return AutoLogin(
            store,
            LoginLog(settings.data_dir / STATE_FILE),
            screens_dir=settings.data_dir / "screenshots",
            timeout_s=15,
            poll_s=0.2,
            rejected_after_s=1,
            guess_after_s=0.5,
        )

    return _make


def _finaliza(portals: PortalServer) -> dict[str, object]:
    bodies = portals.state.posted("finaliza-solicitud")
    assert len(bodies) == 1
    result: dict[str, object] = json.loads(bodies[0])
    return result


# ------------------------------------------------------------------------- Colmena


def test_colmena_submit_happy_path(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    settings: Settings,
) -> None:
    """Bank step, cascading prestación, 3 blocks validated, T&C, Continuar → folio."""
    report = Pipeline(service, make_colmena(), FakePortalClient("chubb")).run_once()

    view = service.repo.get_rendicion(lab_rendicion)
    assert report.colmena_submitted == [lab_rendicion]
    assert view.status == Status.COLMENA_ENVIADA
    assert view.colmena_request_id == "987654"
    assert len(portals.state.posted("validacion-documento")) == 3
    assert len(portals.state.posted("reembolso-web/solicitud")) == 1
    assert _finaliza(portals)["excedentes"] == "N"
    shots = list((settings.data_dir / "screenshots").glob("colmena-*.png"))
    assert len(shots) == 1
    assert stat.S_IMODE(shots[0].stat().st_mode) == 0o600
    assert service.repo.get_marker(lab_rendicion, "colmena") is None


def test_colmena_submit_without_validation_robot(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """When Colmena skips the robot (flujo_colmena), the files go with Continuar."""
    portals.state.robot = False

    report = Pipeline(service, make_colmena(), FakePortalClient("chubb")).run_once()

    assert report.colmena_submitted == [lab_rendicion]
    assert service.repo.get_rendicion(lab_rendicion).colmena_request_id == "987654"
    assert portals.state.posted("validacion-documento") == []
    assert len(portals.state.posted("reembolso-web/solicitud")) == 1


def test_colmena_waits_for_the_prestacion_answer_before_attaching(
    service: RendicionService,
    lista_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """A Consultas boleta goes to the block Colmena renders after its answer, not a placeholder."""
    service.edit(lista_rendicion, tipo_prestacion=TipoPrestacion.CONSULTA_MEDICA)
    portals.state.subtype_delay_s = 1

    report = Pipeline(service, make_colmena(), FakePortalClient("chubb")).run_once()

    assert report.colmena_submitted == [lista_rendicion]
    assert len(portals.state.posted("validacion-documento")) == 1


def test_colmena_reattaches_a_file_the_page_dropped(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """A file the page ignores on the first selection is selected again and submitted."""
    portals.state.drop_attaches = 1

    report = Pipeline(
        service,
        make_colmena(attach_check_timeout_s=0.5),
        FakePortalClient("chubb"),
    ).run_once()

    assert report.colmena_submitted == [lab_rendicion]
    assert len(portals.state.posted("validacion-documento")) == 3


def test_colmena_dropped_file_fails_before_the_marker(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """A file the page drops twice fails the rendición with no marker and nothing sent."""
    portals.state.drop_attaches = 2

    report = Pipeline(
        service,
        make_colmena(attach_check_timeout_s=0.5),
        FakePortalClient("chubb"),
    ).run_once()

    view = service.repo.get_rendicion(lab_rendicion)
    assert report.errors == [lab_rendicion]
    assert "no tomó el archivo de 'Boleta o factura'" in (view.error_message or "")
    assert service.repo.get_marker(lab_rendicion, "colmena") is None
    assert portals.state.posted("reembolso-web/solicitud") == []


def test_colmena_blocked_continuar_fails_before_the_marker(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    settings: Settings,
) -> None:
    """A Continuar that can't be clicked fails without a marker and leaves a screenshot."""
    portals.state.continue_blocked = True

    report = Pipeline(
        service, make_colmena(continue_timeout_s=1), FakePortalClient("chubb")
    ).run_once()

    view = service.repo.get_rendicion(lab_rendicion)
    assert report.errors == [lab_rendicion]
    assert "no dejó presionar Continuar" in (view.error_message or "")
    assert service.repo.get_marker(lab_rendicion, "colmena") is None
    assert portals.state.posted("reembolso-web/solicitud") == []
    assert len(list((settings.data_dir / "screenshots").glob("colmena-*-error-*.png"))) == 1


def test_colmena_excedentes_always_yes(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """When the excedentes step appears the client answers 'S' and finalises."""
    portals.state.excedentes = 150_000

    result = make_colmena().submit(service.repo.get_rendicion(lab_rendicion))

    assert result.ok
    assert result.request_id == "987654"
    assert result.raw["excedentes"] == "S"
    assert _finaliza(portals)["excedentes"] == "S"


def test_colmena_dry_run_never_uploads(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """Dry-run fills the prestación but touches no file input and changes no state."""
    pipeline = Pipeline(
        service, make_colmena(dry_run=True), FakePortalClient("chubb"), dry_run=True
    )

    report = pipeline.run_once()

    assert report.dry_run == [lab_rendicion]
    assert portals.state.posts == []
    assert service.repo.get_rendicion(lab_rendicion).status == Status.LISTA
    assert "dry_run" in [e.kind for e in service.repo.events(lab_rendicion)]


def test_colmena_session_expired_goes_to_error(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """An expired session moves the rendición to error with the login hint."""
    portals.state.colmena_expired = True

    report = Pipeline(service, make_colmena(), FakePortalClient("chubb")).run_once()

    view = service.repo.get_rendicion(lab_rendicion)
    assert view.status == Status.ERROR
    assert "rendiciones login colmena" in (view.error_message or "")
    assert report.sessions_expired == ["colmena"]
    assert portals.state.posts == []


def test_colmena_expired_session_logs_in_with_saved_credential(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """With a saved credential the client fills the login form and carries on."""
    portals.state.colmena_expired = True
    client = make_colmena(auto_login=make_auto_login("clave-de-prueba"))

    report = Pipeline(service, client, FakePortalClient("chubb")).run_once()

    assert report.sessions_expired == []
    assert portals.state.logins == ["colmena"]
    assert service.repo.get_rendicion(lab_rendicion).status == Status.COLMENA_ENVIADA


def test_colmena_session_expiring_mid_run_logs_in_and_retries(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """A token rejected after the page loaded triggers one login and the submit goes through."""
    portals.state.colmena_expires_after_load = True
    client = make_colmena(auto_login=make_auto_login("clave-de-prueba"))

    report = Pipeline(service, client, FakePortalClient("chubb")).run_once()

    assert report.sessions_expired == []
    assert portals.state.logins == ["colmena"]
    assert service.repo.get_rendicion(lab_rendicion).status == Status.COLMENA_ENVIADA


@pytest.mark.parametrize("password", [None, "clave-equivocada"])
def test_colmena_session_expiring_mid_run_without_valid_login_goes_to_error(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
    password: str | None,
) -> None:
    """Without a usable saved login the mid-run expiry still fails once, with no resubmission."""
    portals.state.colmena_expires_after_load = True
    auto_login = make_auto_login(password) if password else None
    client = make_colmena(auto_login=auto_login)

    report = Pipeline(service, client, FakePortalClient("chubb")).run_once()

    assert report.sessions_expired == ["colmena"]
    assert portals.state.posted("finaliza-solicitud") == []
    assert service.repo.get_rendicion(lab_rendicion).status == Status.ERROR


def test_colmena_rejected_password_is_not_retried(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    settings: Settings,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """A wrong password is submitted once; later runs pause instead of locking the account."""
    portals.state.colmena_expired = True
    auto_login = make_auto_login("clave-equivocada")

    for _ in range(2):
        report = Pipeline(service, make_colmena(auto_login=auto_login), FakePortalClient("chubb"))
        report.run_once()

    assert portals.state.logins == ["colmena"]
    last = LoginLog(settings.data_dir / STATE_FILE).read()["colmena"]
    assert last["result"] == LoginResult.REJECTED
    assert service.repo.get_rendicion(lab_rendicion).status == Status.ERROR


def test_colmena_logs_in_from_a_page_stuck_after_expiry(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """An expired tab that never reaches the login form is sent to Colmena's login route."""
    portals.state.colmena_expired = True
    portals.state.colmena_stale_when_expired = True
    client = make_colmena(auto_login=make_auto_login("clave-de-prueba"))

    report = Pipeline(service, client, FakePortalClient("chubb")).run_once()

    assert report.sessions_expired == []
    assert portals.state.logins == ["colmena"]
    assert service.repo.get_rendicion(lab_rendicion).status == Status.COLMENA_ENVIADA


@pytest.mark.parametrize(
    ("password", "state", "result", "detail"),
    [
        ("clave-equivocada", {}, LoginResult.REJECTED, "Usuario o clave inválido"),
        ("clave-de-prueba", {"colmena_password_expired": True}, LoginResult.NEEDS_USER, "caducó"),
        (
            "clave-de-prueba",
            {"colmena_login_message": "Servicio no disponible"},
            LoginResult.NEEDS_USER,
            "Servicio no disponible",
        ),
    ],
)
def test_colmena_login_answer_is_recorded_with_screenshot(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    settings: Settings,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
    password: str,
    state: dict[str, object],
    result: LoginResult,
    detail: str,
) -> None:
    """Colmena's own answer decides the result; each failure keeps a screenshot."""
    portals.state.colmena_expired = True
    for name, value in state.items():
        setattr(portals.state, name, value)
    client = make_colmena(auto_login=make_auto_login(password))

    Pipeline(service, client, FakePortalClient("chubb")).run_once()

    last = LoginLog(settings.data_dir / STATE_FILE).read()["colmena"]
    assert (last["result"], portals.state.logins) == (result, ["colmena"])
    assert detail in last["detail"]
    assert list((settings.data_dir / "screenshots").glob("login-colmena-*.png"))


def test_auto_login_reports_a_page_that_never_loaded(
    session: BrowserSession,
    settings: Settings,
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """A tab that never loaded is not reported as a missing login form."""
    auto_login = make_auto_login("clave-de-prueba")
    auto_login.timeout_s = 0

    ok = auto_login.attempt(session.page("colmena"), "colmena", lambda page: False)

    last = LoginLog(settings.data_dir / STATE_FILE).read()["colmena"]
    assert not ok
    assert (last["result"], last["detail"].startswith("El portal no cargó")) == ("failed", True)


def test_colmena_unreachable_login_page_is_recorded(
    settings: Settings,
    session: BrowserSession,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """A login page that fails to load is logged as such, with a screenshot."""
    page = session.page("colmena")
    page.route("**/*", lambda route: route.abort())
    client = make_colmena(auto_login=make_auto_login("clave-de-prueba"))

    ok = client._log_in(page)

    last = LoginLog(settings.data_dir / STATE_FILE).read()["colmena"]
    assert not ok
    assert last["detail"].startswith("El portal no cargó")
    assert list((settings.data_dir / "screenshots").glob("login-colmena-*.png"))


def test_colmena_log_in_keeps_a_live_session(
    portals: PortalServer,
    session: BrowserSession,
    colmena_urls: ColmenaUrls,
    make_colmena: Callable[..., ColmenaBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """A tab that still has its token is not sent to the login route, which would log it out."""
    page = session.page("colmena")
    page.goto(colmena_urls.consultar, wait_until="domcontentloaded")
    client = make_colmena(auto_login=make_auto_login("clave-de-prueba"))

    ok = client._log_in(page)

    assert ok
    assert (portals.state.logins, "#/login" in page.url) == ([], False)


def test_colmena_interrupted_submit_is_reconciled_not_resent(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """A marker without request id + a matching request in Colmena → adopt it, no new submit."""
    service.repo.set_marker(lab_rendicion, "colmena", "final_click")
    marker = service.repo.get_marker(lab_rendicion, "colmena")
    assert marker is not None
    sol_fecha = marker.created_at.strftime("%d/%m/%Y")
    portals.state.colmena_requests = [
        {"solIdn": 555111, "solFecha": sol_fecha, "solMonto": 24000, "glsEstado": "Ingresada"}
    ]

    result = make_colmena().submit(service.repo.get_rendicion(lab_rendicion))

    assert result.request_id == "555111"
    assert result.raw["recuperado"] is True
    assert portals.state.posts == []


def test_colmena_interrupted_submit_without_match_refuses(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """Without a matching request the client refuses instead of resubmitting."""
    service.repo.set_marker(lab_rendicion, "colmena", "final_click")

    with pytest.raises(PortalError, match="clear-marker"):
        make_colmena().submit(service.repo.get_rendicion(lab_rendicion))

    assert portals.state.posts == []


def test_colmena_existing_request_blocks_duplicate(
    service: RendicionService,
    lab_rendicion: int,
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
) -> None:
    """Same amount after the fecha de atención → refuse unless explicitly allowed."""
    portals.state.colmena_requests = [
        {"solIdn": 777, "solFecha": "10/10/2026", "solMonto": 24000, "glsEstado": "En revisión"}
    ]
    rendicion = service.repo.get_rendicion(lab_rendicion)

    with pytest.raises(PortalError, match="777"):
        make_colmena().submit(rendicion)
    assert portals.state.posts == []

    allowed = make_colmena(allow_duplicates=frozenset({lab_rendicion})).submit(rendicion)
    assert allowed.request_id == "987654"


def test_colmena_check_status_and_download(
    portals: PortalServer, make_colmena: Callable[..., ColmenaBrowserClient], tmp_path: Path
) -> None:
    """Status comes from the JSON; the comprobante downloads as a 0600 PDF."""
    portals.state.colmena_requests = [
        {"solIdn": 987654, "estado": 6, "glsEstado": "Aprobada", "solMonto": 16800},
        {"solIdn": 987655, "estado": 7, "glsEstado": "Rechazada", "solObservacionesCliente": "x"},
        {"solIdn": 987656, "estado": 2, "glsEstado": "En revisión"},
    ]
    client = make_colmena()

    approved = client.check_status("987654")
    assert approved.state == PortalState.APPROVED
    assert approved.monto == 16800
    assert client.check_status("987655").state == PortalState.REJECTED
    assert client.check_status("987656").state == PortalState.PENDING

    dest = tmp_path / "dl"
    path = client.download_bonificacion("987654", dest)
    assert path.read_bytes().startswith(b"%PDF-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("request_id", "match"),
    [
        ("987656", "no entregó un comprobante"),
        ("123", "no entregó un comprobante"),
        ("A-1", "numérico"),
    ],
)
def test_colmena_download_without_comprobante_fails(
    portals: PortalServer,
    make_colmena: Callable[..., ColmenaBrowserClient],
    tmp_path: Path,
    request_id: str,
    match: str,
) -> None:
    """A request with no comprobante (pending, unknown or malformed) raises and writes nothing."""
    portals.state.colmena_requests = [{"solIdn": 987656, "estado": 2, "glsEstado": "En revisión"}]
    dest = tmp_path / "dl"

    with pytest.raises(PortalError, match=match):
        make_colmena().download_bonificacion(request_id, dest)

    assert not dest.exists() or not any(dest.iterdir())


def test_colmena_download_with_dead_token_reports_expired_session(
    portals: PortalServer, make_colmena: Callable[..., ColmenaBrowserClient], tmp_path: Path
) -> None:
    """A 401 from the report service is an expired session, not a missing comprobante."""
    portals.state.colmena_requests = [{"solIdn": 987654, "estado": 6, "glsEstado": "Aprobada"}]
    portals.state.colmena_expires_after_load = True

    with pytest.raises(SessionExpiredError, match="rendiciones login colmena"):
        make_colmena().download_bonificacion("987654", tmp_path / "dl")


def test_login_detects_colmena_session(
    session: BrowserSession, portals: PortalServer, colmena_urls: ColmenaUrls
) -> None:
    """``rendiciones login`` returns once the logged-in page is detected."""
    assert wait_for_login(session, "colmena", timeout_s=10, poll_s=0.2, colmena_urls=colmena_urls)


def test_login_window_autofills_saved_credential(
    session: BrowserSession, portals: PortalServer, colmena_urls: ColmenaUrls
) -> None:
    """The login window fills the saved credential so the user only confirms."""
    portals.state.colmena_expired = True
    credential = Credential("11.111.111-1", "clave-de-prueba")

    ok = wait_for_login(
        session,
        "colmena",
        timeout_s=15,
        poll_s=0.2,
        colmena_urls=colmena_urls,
        credential=credential,
    )

    assert ok
    assert portals.state.logins == ["colmena"]


# --------------------------------------------------------------------------- Chubb


def test_chubb_submit_happy_path(
    service: RendicionService,
    approved_rendicion: int,
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
) -> None:
    """In-app navigation, beneficiary by RUT, product/benefit, MM/DD date, docs, confirm."""
    rendicion = service.repo.get_rendicion(approved_rendicion)

    result = make_chubb().submit(rendicion)

    assert result.ok
    assert result.request_id == "4455667788"
    bodies = portals.state.posted("/api/refund")
    assert len(bodies) == 1
    body = bodies[0]
    assert b'name="benefit"\r\n\r\nConsulta m' in body
    assert b"111111111" in body
    assert body.count(b'name="soportes"') == 3
    assert service.repo.get_marker(approved_rendicion, "chubb") is not None


def test_chubb_dry_run_does_not_send(
    service: RendicionService,
    approved_rendicion: int,
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
) -> None:
    """Dry-run fills the form (files stay client-side) and never clicks 'Enviar'."""
    result = make_chubb(dry_run=True).submit(service.repo.get_rendicion(approved_rendicion))

    assert result.raw["dry_run"] is True
    assert portals.state.posted("/api/refund") == []


def test_chubb_waits_through_silent_sso_login_page(
    service: RendicionService,
    approved_rendicion: int,
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
) -> None:
    """A brief stop on /login while the SSO cookie restores the session is not an expiry."""
    portals.state.chubb_sso_pending = True

    result = make_chubb(dry_run=True).submit(service.repo.get_rendicion(approved_rendicion))

    assert portals.state.chubb_sso_pending is False
    assert result.raw["dry_run"] is True


def test_chubb_session_expired(
    service: RendicionService,
    approved_rendicion: int,
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
) -> None:
    """Landing on the public home means the session is gone."""
    portals.state.chubb_expired = True

    with pytest.raises(SessionExpiredError, match="rendiciones login chubb"):
        make_chubb().submit(service.repo.get_rendicion(approved_rendicion))


def test_chubb_expired_session_logs_in_through_b2c(
    service: RendicionService,
    approved_rendicion: int,
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
    make_auto_login: Callable[[str], AutoLogin],
) -> None:
    """From the public home it clicks "Iniciar sesión" and fills the B2C form."""
    portals.state.chubb_expired = True
    client = make_chubb(dry_run=True, auto_login=make_auto_login("clave-de-prueba"))

    result = client.submit(service.repo.get_rendicion(approved_rendicion))

    assert result.raw["dry_run"] is True
    assert portals.state.logins == ["chubb"]


@pytest.mark.parametrize(
    ("estado", "offered"), [("Pagado", True), ("Rechazada", True), ("En Proceso", False)]
)
def test_chubb_download_liquidacion(
    portals: PortalServer,
    make_chubb: Callable[..., ChubbBrowserClient],
    tmp_path: Path,
    estado: str,
    offered: bool,
) -> None:
    """The liquidación downloads as a 0600 PDF only once the request is paid or rejected."""
    portals.state.history = [
        {"fecha": "01-10-2026", "id": f"10{i}", "monto": "$ 1.000", "estado": "En Proceso"}
        for i in range(6)
    ] + [{"fecha": "02-10-2026", "id": "4455667788", "monto": "$ 12.345", "estado": estado}]

    path = make_chubb().download_liquidacion("4455667788", tmp_path)

    if not offered:
        assert path is None
        return
    assert path is not None
    assert path.name == "liquidacion-chubb-4455667788.pdf"
    assert path.read_bytes().startswith(b"%PDF")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_chubb_check_status_reads_paginated_history(
    portals: PortalServer, make_chubb: Callable[..., ChubbBrowserClient], tmp_path: Path
) -> None:
    """The row is found on page 2; Pagado → approved with the amount; ids match whole."""
    portals.state.history = [
        {"fecha": "01-10-2026", "id": f"10{i}", "monto": "$ 1.000", "estado": "En Proceso"}
        for i in range(6)
    ] + [{"fecha": "02-10-2026", "id": "4455667788", "monto": "$ 12.345", "estado": "Pagado"}]
    client = make_chubb()

    paid = client.check_status("4455667788")
    assert paid.state == PortalState.APPROVED
    assert paid.monto == 12345
    assert client.check_status("101").state == PortalState.PENDING
    assert client.check_status("999").state == PortalState.PENDING
    assert client.check_status("10").state == PortalState.PENDING
    assert client.download_liquidacion("44556677", tmp_path) is None
