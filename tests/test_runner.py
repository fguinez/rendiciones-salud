"""Tests for the automation lock and pipeline passes started from the web UI."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rendiciones.accounts import STATE_FILE, AccountService, LoginBusyError, LoginLog
from rendiciones.api import create_app
from rendiciones.automation.base import ColmenaClient, PortalClient
from rendiciones.automation.fake import FakeColmenaClient, FakePortalClient
from rendiciones.config import CSRF_HEADER, CSRF_VALUE, Settings
from rendiciones.credentials import MemoryStore
from rendiciones.domain import Status
from rendiciones.locking import AutomationBusyError, AutomationLock
from rendiciones.recognition.rules import RuleBasedClassifier
from rendiciones.services import RendicionService

Factory = Callable[[bool], tuple[ColmenaClient, PortalClient]]


@pytest.fixture
def lock(settings: Settings) -> AutomationLock:
    """The lock the app uses for ``settings``."""
    return AutomationLock.for_data_dir(settings.data_dir)


@pytest.fixture
def factory_calls() -> list[bool]:
    """``dry_run`` flags received by the fake clients factory."""
    return []


@pytest.fixture
def fake_factory(factory_calls: list[bool]) -> Factory:
    """Fake portal clients that approve everything."""

    def build(dry_run: bool) -> tuple[ColmenaClient, PortalClient]:
        factory_calls.append(dry_run)
        return FakeColmenaClient(), FakePortalClient("chubb")

    return build


def make_client(settings: Settings, factory: Factory) -> TestClient:
    """API client whose UI passes use ``factory``."""
    app = create_app(settings, RuleBasedClassifier(), portal_clients=factory)
    return TestClient(app, headers={CSRF_HEADER: CSRF_VALUE})


@pytest.fixture
def api(settings: Settings, fake_factory: Factory) -> Iterator[TestClient]:
    """API client wired to the fake portals."""
    with make_client(settings, fake_factory) as test_client:
        yield test_client


def wait_finished(client: TestClient) -> dict[str, Any]:
    """Poll ``/api/pipeline`` until the pass started from the UI ends."""
    deadline = time.monotonic() + 5
    while (data := client.get("/api/pipeline").json())["job"]["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    job: dict[str, Any] = data["job"]
    return job


def test_lock_is_exclusive_and_names_holder(lock: AutomationLock) -> None:
    """A second holder is refused with the first holder's name until it releases."""
    lease = lock.acquire("run-pipeline")

    with pytest.raises(AutomationBusyError, match="run-pipeline"):
        lock.acquire("otro")
    lease.release()

    assert lock.holder() is None
    lock.acquire("otro").release()


def test_login_refused_while_pipeline_holds_lock(settings: Settings, lock: AutomationLock) -> None:
    """ "Iniciar sesión ahora" does not open the browser during a pipeline pass."""
    accounts = AccountService(
        MemoryStore(), LoginLog(settings.data_dir / STATE_FILE), lambda p, c: (True, ""), lock
    )

    with lock.hold("run-pipeline"), pytest.raises(LoginBusyError, match="en uso"):
        accounts.start_login("colmena")


def test_submit_pass_moves_rendicion_through_portals(
    api: TestClient, service: RendicionService, lista_rendicion: int
) -> None:
    """A ``submit`` pass from the UI runs the pipeline and reports the ids it moved."""
    started = api.post("/api/pipeline/runs", json={"mode": "submit"})
    job = wait_finished(api)

    assert started.status_code == 202
    assert job["state"] == "ok"
    assert job["report"]["chubb_approved"] == [lista_rendicion]
    assert service.repo.get_rendicion(lista_rendicion).status == Status.CHUBB_APROBADA
    assert api.get("/api/pipeline").json()["busy_by"] is None


@pytest.mark.parametrize(("mode", "dry_run"), [("dry_run", True), ("submit", False)])
def test_mode_sets_dry_run(
    api: TestClient, factory_calls: list[bool], mode: str, dry_run: bool
) -> None:
    """Only ``submit`` builds clients that really send."""
    api.post("/api/pipeline/runs", json={"mode": mode})
    wait_finished(api)

    assert factory_calls == [dry_run]


def test_start_refused_while_browser_in_use(api: TestClient, lock: AutomationLock) -> None:
    """A pass cannot start while the CLI (or a login) holds the browser."""
    with lock.hold("run-pipeline"):
        response = api.post("/api/pipeline/runs", json={"mode": "submit"})
        status = api.get("/api/pipeline").json()

    assert response.status_code == 409
    assert status["busy_by"].startswith("run-pipeline")


def test_failed_pass_reports_and_releases_lock(settings: Settings, lock: AutomationLock) -> None:
    """A factory error ends the job as failed and frees the browser."""

    def broken(dry_run: bool) -> tuple[ColmenaClient, PortalClient]:
        raise RuntimeError("sin navegador")

    with make_client(settings, broken) as client:
        client.post("/api/pipeline/runs", json={"mode": "submit"})
        job = wait_finished(client)

    assert job["state"] == "failed"
    assert "sin navegador" in job["message"]
    assert lock.holder() is None


def test_start_requires_valid_mode(api: TestClient) -> None:
    """Unknown modes are rejected before anything runs."""
    response = api.post("/api/pipeline/runs", json={"mode": "everything"})

    assert response.status_code == 422
