"""Shared Playwright persistent browser context.

One persistent Chromium profile (``browser-profile/`` in the project, 0700) holds the
portal sessions, so the user logs in once (Cuentas in the UI or ``rendiciones login
<portal>``) and the pipeline reuses the cookies/localStorage. When a session expires, the
clients can log in again with the credential saved in the keychain (``autologin``).

Chromium drops cookies without an expiry when the context closes. Chubb's Azure B2C
SSO relies on such cookies, so on close we copy the portal-domain session cookies into
``browser-profile/session-cookies.json`` (0600, same sensitivity as the profile's own
cookie database) and restore them on the next start.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, Route, sync_playwright

from rendiciones.config import FILE_MODE, ensure_private_dir

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "browser-profile"
SESSION_COOKIES_FILE = "session-cookies.json"
PORTAL_COOKIE_DOMAINS = ("colmena.cl", "chubb.com", "chubbdigital.com")
CHROME_APP = Path("/Applications/Google Chrome.app")


def default_channel() -> str | None:
    """``RENDICIONES_BROWSER_CHANNEL`` if set (``chromium`` = bundled), else Chrome if installed."""
    configured = os.environ.get("RENDICIONES_BROWSER_CHANNEL")
    if configured:
        return None if configured == "chromium" else configured
    if CHROME_APP.exists() or shutil.which("google-chrome"):
        return "chrome"
    return None


@dataclass
class BrowserOptions:
    """How to launch the browser."""

    profile_dir: Path = DEFAULT_PROFILE_DIR
    headless: bool = True
    channel: str | None = field(default_factory=default_channel)
    # Tests set this to {"127.0.0.1"}: requests to any other host are aborted.
    allowed_hosts: frozenset[str] | None = None
    slow_mo_ms: float = 0
    default_timeout_ms: float = 30_000


class BrowserSession:
    """Lazily started persistent context with one tab per portal."""

    def __init__(self, options: BrowserOptions | None = None) -> None:
        self.options = options or BrowserOptions()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}

    # ------------------------------------------------------------------ lifecycle

    @property
    def context(self) -> BrowserContext:
        """The running persistent context (started on first use)."""
        if self._context is None:
            self._start()
        assert self._context is not None
        return self._context

    def _start(self) -> None:
        profile = ensure_private_dir(self.options.profile_dir)
        self._playwright = sync_playwright().start()
        kwargs: dict[str, Any] = {
            "headless": self.options.headless,
            "accept_downloads": True,
            "locale": "es-CL",
            "timezone_id": "America/Santiago",
            "viewport": {"width": 1366, "height": 900},
            "slow_mo": self.options.slow_mo_ms,
        }
        if self.options.channel:
            kwargs["channel"] = self.options.channel
        self._context = self._playwright.chromium.launch_persistent_context(str(profile), **kwargs)
        self._context.set_default_timeout(self.options.default_timeout_ms)
        if self.options.allowed_hosts is not None:
            self._context.route("**/*", self._guard)
        self._restore_session_cookies()
        logger.info("browser started (headless=%s)", self.options.headless)

    def _guard(self, route: Route) -> None:
        host = urlparse(route.request.url).hostname or ""
        allowed = self.options.allowed_hosts or frozenset()
        if route.request.url.startswith(("data:", "blob:", "about:")) or host in allowed:
            route.continue_()
        else:
            logger.warning("blocked request to a non-allowed host")
            route.abort()

    def page(self, portal: str) -> Page:
        """The tab dedicated to ``portal`` (Chubb keeps its session in sessionStorage per tab)."""
        page = self._pages.get(portal)
        if page is None or page.is_closed():
            existing = [
                p for p in self.context.pages if not p.is_closed() and p.url == "about:blank"
            ]
            page = existing[0] if existing and not self._pages else self.context.new_page()
            self._pages[portal] = page
        return page

    def close(self) -> None:
        """Persist session cookies and shut the browser down (safe to call twice)."""
        if self._context is not None:
            try:
                self._save_session_cookies()
            except Exception as exc:  # never fail a run because of cookie persistence
                logger.warning("could not persist session cookies: %s", type(exc).__name__)
            try:
                self._context.close()
            except Exception as exc:
                logger.warning("browser close failed: %s", type(exc).__name__)
        if self._playwright is not None:
            self._playwright.stop()
        self._context = None
        self._playwright = None
        self._pages = {}

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------ session cookies

    @property
    def _cookie_file(self) -> Path:
        return self.options.profile_dir / SESSION_COOKIES_FILE

    @staticmethod
    def _is_portal_cookie(cookie: dict[str, Any]) -> bool:
        domain = str(cookie.get("domain", "")).lstrip(".")
        return any(domain == d or domain.endswith("." + d) for d in PORTAL_COOKIE_DOMAINS)

    def _save_session_cookies(self) -> None:
        assert self._context is not None
        cookies = [
            dict(c)
            for c in self._context.cookies()
            if c.get("expires", -1) in (-1, None) and self._is_portal_cookie(dict(c))
        ]
        path = self._cookie_file
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cookies, fh)
        path.chmod(FILE_MODE)
        logger.info("persisted %d session cookies", len(cookies))

    def _restore_session_cookies(self) -> None:
        assert self._context is not None
        path = self._cookie_file
        if not path.exists():
            return
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
            present = {(c["name"], c["domain"], c["path"]) for c in self._context.cookies()}
            missing = [
                c
                for c in cookies
                if self._is_portal_cookie(c) and (c["name"], c["domain"], c["path"]) not in present
            ]
            if missing:
                self._context.add_cookies(missing)
            logger.info("restored %d session cookies", len(missing))
        except Exception as exc:
            logger.warning("could not restore session cookies: %s", type(exc).__name__)
