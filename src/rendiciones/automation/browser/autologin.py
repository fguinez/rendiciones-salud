"""Fill a portal's login form with the credential saved in the keychain.

Neither portal's login form was mapped (docs/portals.md), so fields are found with
generic selectors: the visible password input, plus the RUT / e-mail / username input
next to it. One submission per attempt only, so a wrong password never locks the account:
after a rejection, automatic logins stay off until the user saves a new password.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from playwright.sync_api import Locator, Page

from rendiciones.accounts import LoginLog, LoginResult
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
            if username is not None and username.input_value() != self.credential.username:
                username.fill(self.credential.username)
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
            username.fill(self.credential.username)
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
    timeout_s: float = 60
    poll_s: float = 0.5
    rejected_after_s: float = REJECTED_AFTER_S
    guess_after_s: float = GUESS_AFTER_S

    def attempt(self, page: Page, portal: str, logged_in: Callable[[Page], bool]) -> bool:
        """Try once to log in on ``page``; True when ``logged_in`` turns true."""
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
            try:
                if logged_in(page):
                    self.log.record(portal, LoginResult.OK, "Inicio de sesión automático")
                    return True
                step = filler.step(page)
            except Exception as exc:
                logger.warning("%s auto-login error: %s", portal, type(exc).__name__)
                step = Step.IDLE
            if step is Step.REJECTED:
                self.log.record(
                    portal,
                    LoginResult.REJECTED,
                    "El portal rechazó el usuario o la contraseña guardados",
                )
                return False
            page.wait_for_timeout(self.poll_s * 1000)
        if filler.submitted_at is not None:
            self.log.record(
                portal,
                LoginResult.NEEDS_USER,
                "El portal pidió algo más (¿código de verificación?): inicia sesión desde Cuentas",
            )
        else:
            self.log.record(
                portal, LoginResult.FAILED, "No se encontró el formulario de inicio de sesión"
            )
        return False
