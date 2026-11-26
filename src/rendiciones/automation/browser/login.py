"""Headed login window (``rendiciones login <portal>`` or "Iniciar sesión" in Cuentas).

It opens the portal in the persistent profile and polls until a logged-in page is
detected (or the user closes the window). With a saved credential it fills the login form
once; anything else (a verification code, a wrong password) is left to the user.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from playwright.sync_api import Page

from rendiciones.automation.browser import chubb, colmena
from rendiciones.automation.browser.autologin import LoginFiller, Step
from rendiciones.automation.browser.selectors import ChubbUrls, ColmenaUrls
from rendiciones.automation.browser.session import BrowserSession
from rendiciones.credentials import Credential

logger = logging.getLogger(__name__)

PORTALS = ("colmena", "chubb")


def login_target(
    portal: str, colmena_urls: ColmenaUrls | None = None, chubb_urls: ChubbUrls | None = None
) -> tuple[str, Callable[[Page], bool]]:
    """Start URL and logged-in detector for ``portal``."""
    if portal == "colmena":
        c_urls = colmena_urls or ColmenaUrls()
        return c_urls.login, lambda page: colmena.is_logged_in(page, c_urls)
    if portal == "chubb":
        h_urls = chubb_urls or ChubbUrls()
        return h_urls.login, lambda page: chubb.is_logged_in(page, h_urls)
    raise ValueError(f"portal desconocido: {portal}")


def wait_for_login(
    session: BrowserSession,
    portal: str,
    *,
    timeout_s: float = 900,
    poll_s: float = 2,
    colmena_urls: ColmenaUrls | None = None,
    chubb_urls: ChubbUrls | None = None,
    stable_checks: int = 2,
    credential: Credential | None = None,
) -> bool:
    """Open the portal and return True once a logged-in page is seen ``stable_checks`` times."""
    url, detector = login_target(portal, colmena_urls, chubb_urls)
    page = session.page(portal)
    page.goto(url, wait_until="domcontentloaded")
    deadline = time.monotonic() + timeout_s
    streak = 0
    filler = LoginFiller(credential) if credential else None
    while time.monotonic() < deadline:
        if page.is_closed():
            logger.warning("%s login window closed before a session was detected", portal)
            return False
        try:
            streak = streak + 1 if detector(page) else 0
        except Exception:
            streak = 0
        if streak >= stable_checks:
            logger.info("%s session detected", portal)
            return True
        if filler is not None and streak == 0:
            try:
                if filler.step(page) is Step.REJECTED:
                    logger.warning("%s rejected the saved credential; waiting for the user", portal)
                    filler = None
            except Exception as exc:
                logger.warning("%s login autofill error: %s", portal, type(exc).__name__)
        try:
            page.wait_for_timeout(poll_s * 1000)
        except Exception:
            return False
    return False
