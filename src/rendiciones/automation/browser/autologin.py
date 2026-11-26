"""Fill a portal's login form with the credential saved in the keychain.

Generic selectors find the password and RUT / e-mail / username inputs (portal specifics
in docs/portals.md). One submission per attempt, so a rejection pauses auto-login until a
new password is saved and the account never locks; every failure leaves a screenshot.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from rendiciones import rut
from rendiciones.accounts import LoginLog, LoginResult
from rendiciones.automation.browser.common import stamp, take_screenshot
from rendiciones.credentials import Credential, CredentialStore, CredentialStoreError

logger = logging.getLogger(__name__)

PASSWORD_INPUT = "input[type=password]"  # noqa: S105
USERNAME_INPUTS = (
    "#signInName",
    "#email",
    "input[autocomplete=username]",
    "input[name*=rut i]",
    "input[id*=rut i]",
    "input[formcontrolname*=rut i]",
    "input[type=email]",
    "input[name*=user i]",
    "input[name*=mail i]",
)
TEXT_INPUTS = "input:not([type]), input[type=text], input[type=tel], input[type=email]"
SUBMIT_BUTTONS = "#next, button[type=submit], input[type=submit]"
ENTRY_RE = re.compile(r"^\s*(Iniciar sesi[oó]n|Ingresar|Ingresa|Acceder)\s*$", re.IGNORECASE)
SUBMIT_RE = re.compile(r"^\s*(Ingresar|Iniciar sesi[oó]n|Continuar|Siguiente|Entrar)\s*$", re.I)
# A password field still on screen this long after submitting means the portal refused it.
REJECTED_AFTER_S = 10.0
# Without a password field, wait this long before guessing (the SSO may still be restoring).
GUESS_AFTER_S = 5.0


@dataclass(frozen=True)
class LoginFailure:
    """A portal-specific reason why a submitted login did not go through."""

    result: LoginResult
    detail: str


FailureCheck = Callable[[Page], LoginFailure | None]
NOT_LOADED = LoginFailure(LoginResult.FAILED, "El portal no cargó (¿sin conexión o portal caído?)")


class Step(StrEnum):
    """What ``LoginFiller.step`` did on this poll."""

    IDLE = "idle"
    ACTED = "acted"
    REJECTED = "rejected"


def _first_visible(page: Page, selector: str) -> Locator | None:
    candidates = page.locator(selector)
    for index in range(min(candidates.count(), 10)):
        candidate = candidates.nth(index)
        with contextlib.suppress(Exception):
            if candidate.is_visible() and candidate.is_editable():
                return candidate
    return None


def find_password(page: Page) -> Locator | None:
    """The visible password input, if any."""
    return _first_visible(page, PASSWORD_INPUT)


def find_username(page: Page) -> Locator | None:
    """The visible username / RUT / e-mail input, if any."""
    for selector in USERNAME_INPUTS:
        found = _first_visible(page, selector)
        if found is not None:
            return found
    return _first_visible(page, TEXT_INPUTS)


def fit_username(field: Locator, username: str) -> str:
    """``username`` as it fits ``field``: a RUT longer than its ``maxlength`` goes in compact."""
    limit = field.get_attribute("maxlength") or ""
    canonical = rut.normalize(username)
    if not limit.isdigit() or len(username) <= int(limit) or canonical is None:
        return username
    return canonical.replace("-", "")


def describe_page(page: Page) -> str:
    """Host, path and SPA route of ``page``, never parameters (they may carry sign-in codes)."""
    parts = urlsplit(page.url)
    route = parts.fragment.split("?", 1)[0].split("&", 1)[0]
    shown = f"#{route}" if route.startswith("/") else ""
    host = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname or ""
    return f"{host}{parts.path}{shown}"


def _submit(page: Page, field: Locator) -> None:
    button = _first_visible(page, SUBMIT_BUTTONS)
    if button is None:
        named = page.get_by_role("button", name=SUBMIT_RE)
        button = named.first if named.count() and named.first.is_visible() else None
    if button is not None and button.is_enabled():
        button.click()
    else:
        field.press("Enter")


def _click_entry(page: Page) -> bool:
    for role in ("button", "link"):
        entry = page.get_by_role(role, name=ENTRY_RE)
        for index in range(min(entry.count(), 5)):
            with contextlib.suppress(Exception):
                if entry.nth(index).is_visible():
                    entry.nth(index).click()
                    return True
    return False


class LoginFiller:
    """Stateful helper called on every poll while waiting for a logged-in page."""

    def __init__(
        self,
        credential: Credential,
        *,
        rejected_after_s: float = REJECTED_AFTER_S,
        guess_after_s: float = GUESS_AFTER_S,
    ) -> None:
        self.credential = credential
        self.rejected_after_s = rejected_after_s
        self.guess_after_s = guess_after_s
        self.submitted_at: float | None = None
        self.username_only_done = False
        self.entry_clicked = False
        self.created_at = time.monotonic()

    def step(self, page: Page) -> Step:
        """Act on the current page at most once per kind of action."""
        password = find_password(page)
        if self.submitted_at is not None:
            waited = time.monotonic() - self.submitted_at
            if password is not None and waited > self.rejected_after_s:
                return Step.REJECTED
            return Step.IDLE
        if password is not None:
            username = find_username(page)
            if username is not None:
                value = fit_username(username, self.credential.username)
                if username.input_value() != value:
                    username.fill(value)
            password.fill(self.credential.password)
            _submit(page, password)
            self.submitted_at = time.monotonic()
            logger.info("login form submitted")
            return Step.ACTED
        if time.monotonic() - self.created_at < self.guess_after_s:
            return Step.IDLE
        username = find_username(page)
        if username is not None and not self.username_only_done:
            # Two-step forms ask for the RUT first and show the password afterwards.
            username.fill(fit_username(username, self.credential.username))
            _submit(page, username)
            self.username_only_done = True
            return Step.ACTED
        if not self.entry_clicked and _click_entry(page):
            self.entry_clicked = True
            return Step.ACTED
        return Step.IDLE


@dataclass
class AutoLogin:
    """Unattended login used by the pipeline when a portal session has expired."""

    store: CredentialStore
    log: LoginLog
    screens_dir: Path | None = None
    timeout_s: float = 60
    poll_s: float = 0.5
    rejected_after_s: float = REJECTED_AFTER_S
    guess_after_s: float = GUESS_AFTER_S

    def attempt(
        self,
        page: Page,
        portal: str,
        logged_in: Callable[[Page], bool],
        *,
        check_failure: FailureCheck | None = None,
        load_failed: bool = False,
    ) -> bool:
        """Try once to log in on ``page``; True when ``logged_in`` turns true.

        ``check_failure`` reads the portal's own answer once the form was submitted;
        ``load_failed`` says the caller could not open the login page.
        """
        try:
            credential = self.store.get(portal)
        except CredentialStoreError as exc:
            logger.warning("%s auto-login skipped: %s", portal, exc)
            return False
        if credential is None:
            return False
        if self.log.blocked(portal, credential):
            logger.warning("%s auto-login skipped: last attempt was rejected", portal)
            return False
        if load_failed:
            self._fail(page, portal, NOT_LOADED)
            return False
        filler = LoginFiller(
            credential, rejected_after_s=self.rejected_after_s, guess_after_s=self.guess_after_s
        )
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            failure: LoginFailure | None = None
            try:
                if logged_in(page):
                    self.log.record(portal, LoginResult.OK, "Inicio de sesión automático")
                    return True
                if filler.submitted_at is not None and check_failure is not None:
                    failure = check_failure(page)
                step = Step.IDLE if failure else filler.step(page)
            except Exception as exc:
                logger.warning("%s auto-login error: %s", portal, type(exc).__name__)
                step = Step.IDLE
            # A portal that reads its own answer decides; a slow reply is not a rejection.
            if step is Step.REJECTED and check_failure is None:
                failure = LoginFailure(
                    LoginResult.REJECTED, "El portal rechazó el usuario o la contraseña guardados"
                )
            if failure is not None:
                self._fail(page, portal, failure)
                return False
            page.wait_for_timeout(self.poll_s * 1000)
        self._fail(
            page,
            portal,
            self._build_timeout_failure(page, submitted=filler.submitted_at is not None),
        )
        return False

    @staticmethod
    def _build_timeout_failure(page: Page, *, submitted: bool) -> LoginFailure:
        if submitted:
            return LoginFailure(
                LoginResult.NEEDS_USER,
                "El portal pidió algo más (¿código de verificación?): inicia sesión desde Cuentas",
            )
        if not page.url.startswith(("http://", "https://")):
            return NOT_LOADED
        return LoginFailure(
            LoginResult.FAILED,
            f"No se encontró el formulario de inicio de sesión en {describe_page(page)}",
        )

    def _fail(self, page: Page, portal: str, failure: LoginFailure) -> None:
        shot = self._take_screenshot(page, portal)
        detail = failure.detail
        if shot is not None:
            detail = f"{detail} (captura: {shot.name})"
        self.log.record(portal, failure.result, detail)

    def _take_screenshot(self, page: Page, portal: str) -> Path | None:
        """Best effort: a crashed page must not hide the login result being recorded."""
        if self.screens_dir is None:
            return None
        try:
            return take_screenshot(
                page, self.screens_dir, f"login-{portal}-{stamp(datetime.now())}"
            )
        except (PlaywrightError, OSError) as exc:
            logger.warning("%s login screenshot failed: %s", portal, type(exc).__name__)
            return None
