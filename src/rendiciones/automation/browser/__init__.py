"""Playwright clients for the real Colmena and Chubb portals.

Load with ``run-pipeline --clients playwright`` or
``RENDICIONES_PORTAL_FACTORY=rendiciones.automation.browser:factory``.
"""

from __future__ import annotations

import os
from pathlib import Path

from rendiciones.accounts import STATE_FILE, LoginLog
from rendiciones.automation.browser.autologin import AutoLogin
from rendiciones.automation.browser.chubb import ChubbBrowserClient
from rendiciones.automation.browser.colmena import ColmenaBrowserClient
from rendiciones.automation.browser.session import (
    DEFAULT_PROFILE_DIR,
    BrowserOptions,
    BrowserSession,
)
from rendiciones.config import Settings, prepare_data_dir
from rendiciones.credentials import KeychainStore
from rendiciones.db import Repository

__all__ = [
    "BrowserOptions",
    "BrowserSession",
    "ChubbBrowserClient",
    "ColmenaBrowserClient",
    "factory",
    "profile_dir",
]


def profile_dir() -> Path:
    """``RENDICIONES_BROWSER_PROFILE`` or ``browser-profile/`` in the project."""
    configured = os.environ.get("RENDICIONES_BROWSER_PROFILE")
    return Path(configured).resolve() if configured else DEFAULT_PROFILE_DIR


def factory(
    *,
    dry_run: bool = False,
    headed: bool = False,
    allow_duplicates: frozenset[int] = frozenset(),
    settings: Settings | None = None,
) -> tuple[ColmenaBrowserClient, ChubbBrowserClient]:
    """Build both clients sharing one persistent browser (started lazily, closed via ``close``)."""
    settings = settings or Settings.from_env()
    prepare_data_dir(settings)
    repo = Repository(settings.db_path)
    session = BrowserSession(BrowserOptions(profile_dir=profile_dir(), headless=not headed))
    auto_login = AutoLogin(KeychainStore(), LoginLog(settings.data_dir / STATE_FILE))
    colmena = ColmenaBrowserClient(
        session,
        repo,
        data_dir=settings.data_dir,
        dry_run=dry_run,
        allow_duplicates=allow_duplicates,
        auto_login=auto_login,
    )
    chubb = ChubbBrowserClient(
        session, repo, data_dir=settings.data_dir, dry_run=dry_run, auto_login=auto_login
    )
    return colmena, chubb
