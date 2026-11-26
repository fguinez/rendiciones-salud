"""Fill a portal's login form with the credential saved in the keychain.

Neither portal's login form was mapped (docs/portals.md), so fields are found with
generic selectors: the visible password input, plus the RUT / e-mail / username input
next to it. One submission per attempt only, so a wrong password never locks the account:
after a rejection, automatic logins stay off until the user saves a new password. Every
failed attempt leaves a screenshot so the next one can be diagnosed.
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
    """Host, path and route of ``page`` (never its query string)."""
    parts = urlsplit(page.url)
    route = f"#{parts.fragment}" if parts.fragment else ""
    return f"{parts.netloc}{parts.path}{route}"


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
    ) -> bool:
        """Try once to log in on ``page``; True when ``logged_in`` turns true.

        ``check_failure`` reads the portal's own answer once the form was submitted.
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
            if step is Step.REJECTED:
                failure = LoginFailure(
                    LoginResult.REJECTED, "El portal rechazó el usuario o la contraseña guardados"
                )
            if failure is not None:
                self._fail(page, portal, failure)
                return False
            page.wait_for_timeout(self.poll_s * 1000)
        self._fail(
            page, portal, self._timeout_failure(page, submitted=filler.submitted_at is not None)
        )
        return False

    @staticmethod
    def _timeout_failure(page: Page, *, submitted: bool) -> LoginFailure:
        if submitted:
            return LoginFailure(
                LoginResult.NEEDS_USER,
                "El portal pidió algo más (¿código de verificación?): inicia sesión desde Cuentas",
            )
        if not page.url.startswith(("http://", "https://")):
            return LoginFailure(
                LoginResult.FAILED, "El portal no cargó (¿sin conexión o portal caído?)"
            )
        return LoginFailure(
            LoginResult.FAILED,
            f"No se encontró el formulario de inicio de sesión en {describe_page(page)}",
        )

    def _fail(self, page: Page, portal: str, failure: LoginFailure) -> None:
        detail = failure.detail
        shot = self._screenshot(page, portal)
        if shot is not None:
            detail = f"{detail} (captura: {shot.name})"
        self.log.record(portal, failure.result, detail)

    def _screenshot(self, page: Page, portal: str) -> Path | None:
        """Best effort: a crashed page must not hide the login result being recorded."""
        if self.screens_dir is None:
            return None
        try:
            return take_screenshot(
                page, self.screens_dir, f"login-{portal}-{stamp(datetime.now())}"
            )
        except PlaywrightError as exc:
            logger.warning("%s login screenshot failed: %s", portal, type(exc).__name__)
            return None
