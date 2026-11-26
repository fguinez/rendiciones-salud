"""Portal accounts shown in the UI: saved credentials, login history and login jobs.

Passwords stay in the keychain (``rendiciones.credentials``). This module only keeps a
non-secret log of login attempts (``data/accounts-state.json``) and runs the headed
"Iniciar sesión" browser in a background thread.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from rendiciones.config import FILE_MODE
from rendiciones.credentials import (
    PORTALS,
    Credential,
    CredentialStore,
    CredentialStoreError,
    check_portal,
    mask_username,
    validate,
)
from rendiciones.locking import AutomationBusyError, AutomationLock, Lease

logger = logging.getLogger(__name__)

STATE_FILE = "accounts-state.json"
PORTAL_LABELS = {
    "colmena": "Isapre Colmena",
    "chubb": "Chubb (seguro complementario)",
    "bancochile": "Banco de Chile (verificación de abonos)",
}
USERNAME_HELP = {"colmena": "RUT", "chubb": "RUT o correo", "bancochile": "RUT"}


class LoginBusyError(RuntimeError):
    """Another interactive login window is already open."""


class LoginResult(StrEnum):
    """Outcome of a login attempt."""

    OK = "ok"
    REJECTED = "rejected"
    NEEDS_USER = "needs_user"
    FAILED = "failed"


# After these, unattended logins pause until the user saves a new password or logs in.
BLOCKING_RESULTS = frozenset({LoginResult.REJECTED, LoginResult.NEEDS_USER})


class LoginLog:
    """Last login attempt per portal, stored as 0600 JSON (no secrets)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, dict[str, str]]:
        """All entries keyed by portal."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, portal: str, result: LoginResult, detail: str) -> None:
        """Store the latest attempt for ``portal``."""
        with self._lock:
            data = self.read()
            data[check_portal(portal)] = {
                "result": result.value,
                "detail": detail,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        logger.info("%s login result: %s", portal, result.value)

    def blocked(self, portal: str, credential: Credential) -> bool:
        """True when the last attempt with this credential needs the user's attention."""
        entry = self.read().get(portal)
        if not entry or entry.get("result") not in BLOCKING_RESULTS:
            return False
        return (credential.updated_at or "") <= entry.get("at", "")


LoginRunner = Callable[[str, Credential | None], tuple[bool, str]]


class JobState(StrEnum):
    """State of the interactive login started from the UI."""

    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


@dataclass
class LoginJob:
    """One interactive login."""

    state: JobState
    message: str
    started_at: str


class AccountService:
    """Credentials CRUD plus interactive logins for the Cuentas screen."""

    def __init__(
        self,
        store: CredentialStore,
        log: LoginLog,
        runner: LoginRunner,
        lock: AutomationLock | None = None,
    ) -> None:
        self.store = store
        self.log = log
        self.runner = runner
        self.lock = lock
        self._jobs: dict[str, LoginJob] = {}
        self._lock = threading.Lock()

    def describe(self, portal: str) -> dict[str, Any]:
        """Public view of one account: never includes the password."""
        error = None
        credential = None
        try:
            credential = self.store.get(portal)
        except CredentialStoreError as exc:
            error = str(exc)
        job = self._jobs.get(portal)
        return {
            "portal": portal,
            "label": PORTAL_LABELS[portal],
            "username_help": USERNAME_HELP[portal],
            "configured": credential is not None,
            "username_hint": mask_username(credential.username) if credential else None,
            "updated_at": credential.updated_at if credential else None,
            "store_error": error,
            "last_login": self.log.read().get(portal),
            "auto_login_paused": bool(credential and self.log.blocked(portal, credential)),
            "login_job": job.__dict__ if job else None,
        }

    def list(self) -> list[dict[str, Any]]:
        """All portals."""
        return [self.describe(portal) for portal in PORTALS]

    def save(self, portal: str, username: str, password: str) -> dict[str, Any]:
        """Validate and store a credential in the keychain."""
        self.store.set(check_portal(portal), validate(username, password))
        return self.describe(portal)

    def delete(self, portal: str) -> bool:
        """Forget the credential of ``portal``."""
        return self.store.delete(check_portal(portal))

    def start_login(self, portal: str) -> dict[str, Any]:
        """Open the portal in a visible browser (auto-filled when a credential exists)."""
        check_portal(portal)
        with self._lock:
            if any(job.state is JobState.RUNNING for job in self._jobs.values()):
                raise LoginBusyError("Ya hay una ventana de inicio de sesión abierta")
            try:
                lease = self.lock.acquire(f"inicio de sesión en {portal}") if self.lock else None
            except AutomationBusyError as exc:
                raise LoginBusyError(str(exc)) from exc
            self._jobs[portal] = LoginJob(
                JobState.RUNNING,
                "Se abrió una ventana del navegador. Completa ahí lo que falte.",
                datetime.now(UTC).isoformat(timespec="seconds"),
            )
        thread = threading.Thread(target=self._run_login, args=(portal, lease), daemon=True)
        thread.start()
        return self.describe(portal)

    def _run_login(self, portal: str, lease: Lease | None) -> None:
        try:
            credential = self.store.get(portal)
        except CredentialStoreError:
            credential = None
        try:
            ok, message = self.runner(portal, credential)
        except Exception as exc:
            logger.warning("%s interactive login failed: %s", portal, type(exc).__name__)
            ok, message = False, f"No se pudo abrir el navegador ({type(exc).__name__})"
        finally:
            if lease is not None:
                lease.release()
        self.log.record(portal, LoginResult.OK if ok else LoginResult.FAILED, message)
        job = self._jobs[portal]
        job.state = JobState.OK if ok else JobState.FAILED
        job.message = message


def browser_login_runner(portal: str, credential: Credential | None) -> tuple[bool, str]:
    """Headed browser on the project profile; fills the form once, then waits for the user.

    For the bank there is nothing to wait for: it logs in headless with the saved
    credential and reads the cartola once, as a connection test.
    """
    from playwright.sync_api import Error as PlaywrightError

    if portal == "bancochile":
        from rendiciones.bank.bancochile import test_connection

        return test_connection(credential)

    from rendiciones.automation.browser import BrowserOptions, BrowserSession, profile_dir
    from rendiciones.automation.browser.login import wait_for_login

    try:
        with BrowserSession(BrowserOptions(profile_dir=profile_dir(), headless=False)) as session:
            ok = wait_for_login(session, portal, credential=credential)
    except PlaywrightError:
        return False, (
            "No se pudo abrir el navegador: puede que el envío automático lo esté usando. "
            "Intenta de nuevo en unos minutos."
        )
    if ok:
        return True, "Sesión iniciada y guardada en el perfil del navegador"
    return False, "No se detectó una sesión iniciada (ventana cerrada o tiempo agotado)"
