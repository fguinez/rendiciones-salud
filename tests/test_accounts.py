"""Tests for portal accounts: keychain store wrapper, login log and the /api/accounts API."""

from __future__ import annotations

import stat
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from rendiciones.accounts import STATE_FILE, AccountService, LoginLog, LoginResult
from rendiciones.api import create_app
from rendiciones.config import CSRF_HEADER, CSRF_VALUE, Settings
from rendiciones.credentials import (
    Credential,
    CredentialStoreError,
    KeychainStore,
    MemoryStore,
    mask_username,
    validate,
)
from rendiciones.recognition.rules import RuleBasedClassifier

SECRET = "clave-super-secreta"


@pytest.fixture
def store() -> MemoryStore:
    """Credential store kept in memory (the real keychain is never touched)."""
    return MemoryStore()


@pytest.fixture
def runner_calls() -> list[tuple[str, Credential | None]]:
    """Calls received by the fake interactive-login runner."""
    return []


@pytest.fixture
def accounts(
    settings: Settings, store: MemoryStore, runner_calls: list[tuple[str, Credential | None]]
) -> AccountService:
    """Account service whose login runner records its call and succeeds."""

    def runner(portal: str, credential: Credential | None) -> tuple[bool, str]:
        runner_calls.append((portal, credential))
        return True, "Sesión iniciada"

    return AccountService(store, LoginLog(settings.data_dir / STATE_FILE), runner)


@pytest.fixture
def api(settings: Settings, accounts: AccountService) -> Iterator[TestClient]:
    """API client wired to the in-memory accounts."""
    app = create_app(settings, RuleBasedClassifier(), accounts)
    with TestClient(app, headers={CSRF_HEADER: CSRF_VALUE}) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("username", "expected"),
    [("11.111.111-1", "•••11-1"), ("ana@example.com", "a•••@example.com"), ("abc", "•••")],
)
def test_mask_username(username: str, expected: str) -> None:
    """The hint shows just enough to recognise the account."""
    assert mask_username(username) == expected


@pytest.mark.parametrize(
    ("username", "password"), [("", "x"), ("  ", "x"), ("u", ""), ("u" * 201, "x")]
)
def test_validate_rejects_bad_input(username: str, password: str) -> None:
    """Empty or oversized values are refused."""
    with pytest.raises(ValueError, match="Usuario"):
        validate(username, password)


def test_credential_repr_hides_password() -> None:
    """Logging or printing a credential never leaks the password."""
    assert SECRET not in repr(Credential("u", SECRET))


def test_keychain_store_round_trip(mocker: MockerFixture) -> None:
    """The store writes one JSON item per portal under the app's keychain service."""
    items: dict[tuple[str, str], str] = {}
    backend = mocker.Mock()
    backend.set_password.side_effect = lambda s, a, v: items.__setitem__((s, a), v)
    backend.get_password.side_effect = lambda s, a: items.get((s, a))
    keychain = KeychainStore()
    mocker.patch.object(keychain, "_keyring", return_value=backend)

    keychain.set("chubb", Credential("ana@example.com", SECRET))

    assert keychain.get("chubb") == Credential("ana@example.com", SECRET)
    assert list(items) == [("rendiciones-salud", "portal:chubb")]


def test_keychain_store_wraps_backend_errors(mocker: MockerFixture) -> None:
    """Backend failures surface as a safe ``CredentialStoreError``."""
    backend = mocker.Mock()
    backend.get_password.side_effect = OSError("denied")
    keychain = KeychainStore()
    mocker.patch.object(keychain, "_keyring", return_value=backend)

    with pytest.raises(CredentialStoreError, match="OSError"):
        keychain.get("colmena")


def test_login_log_blocks_until_credential_changes(settings: Settings) -> None:
    """A rejection pauses auto-login only for the credential that was rejected."""
    log = LoginLog(settings.data_dir / STATE_FILE)
    old = Credential("u", "p", "2000-01-01T00:00:00+00:00")
    log.record("colmena", LoginResult.REJECTED, "rechazada")

    new = Credential("u", "p2", "2999-01-01T00:00:00+00:00")

    assert log.blocked("colmena", old)
    assert not log.blocked("colmena", new)
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_api_saves_account_without_echoing_password(api: TestClient, store: MemoryStore) -> None:
    """PUT stores the credential; neither PUT nor GET returns the password or full RUT."""
    saved = api.put("/api/accounts/colmena", json={"username": "11.111.111-1", "password": SECRET})
    listed = api.get("/api/accounts")

    assert saved.status_code == 200
    assert store.get("colmena").password == SECRET  # type: ignore[union-attr]
    for body in (saved.text, listed.text):
        assert SECRET not in body
        assert "11.111.111-1" not in body
    assert [a["configured"] for a in listed.json()] == [True, False, False]


def test_api_save_requires_csrf_header(settings: Settings, accounts: AccountService) -> None:
    """Cross-site requests cannot overwrite a credential."""
    with TestClient(create_app(settings, RuleBasedClassifier(), accounts)) as bare:
        response = bare.put("/api/accounts/chubb", json={"username": "u", "password": SECRET})

    assert response.status_code == 403


@pytest.mark.parametrize("body", [{"username": "", "password": "x"}, {"username": "u"}])
def test_api_save_validates_body(api: TestClient, body: dict[str, str]) -> None:
    """Missing fields are rejected."""
    assert api.put("/api/accounts/chubb", json=body).status_code == 422


def test_api_unknown_portal_is_rejected(api: TestClient) -> None:
    """Only colmena and chubb exist."""
    response = api.put("/api/accounts/fonasa", json={"username": "u", "password": "p"})

    assert response.status_code == 422


def test_api_deletes_account(api: TestClient, store: MemoryStore) -> None:
    """DELETE forgets the credential."""
    store.set("chubb", Credential("u", SECRET))

    response = api.delete("/api/accounts/chubb")

    assert response.json() == {"deleted": True}
    assert store.get("chubb") is None


def test_api_login_runs_with_saved_credential(
    api: TestClient, store: MemoryStore, runner_calls: list[tuple[str, Credential | None]]
) -> None:
    """ "Iniciar sesión ahora" hands the saved credential to the browser and logs the result."""
    store.set("chubb", Credential("u", SECRET))

    started = api.post("/api/accounts/chubb/login")
    deadline = time.monotonic() + 5
    while api.get("/api/accounts").json()[1]["login_job"]["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    chubb = api.get("/api/accounts").json()[1]
    assert started.status_code == 200
    assert runner_calls == [("chubb", Credential("u", SECRET))]
    assert chubb["login_job"]["state"] == "ok"
    assert chubb["last_login"]["result"] == "ok"


def test_api_reports_keychain_unavailable(
    settings: Settings, mocker: MockerFixture, tmp_path: Path
) -> None:
    """A keychain failure while saving becomes a readable 503."""
    store = mocker.Mock()
    store.set.side_effect = CredentialStoreError("No hay un llavero seguro disponible")
    service = AccountService(store, LoginLog(tmp_path / STATE_FILE), lambda p, c: (True, ""))
    app = create_app(settings, RuleBasedClassifier(), service)

    with TestClient(app, headers={CSRF_HEADER: CSRF_VALUE}) as test_client:
        response = test_client.put("/api/accounts/chubb", json={"username": "u", "password": "p"})

    assert response.status_code == 503
    assert "llavero" in response.json()["detail"]
