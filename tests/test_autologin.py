"""Unit tests for the auto-login helpers that need no browser."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from rendiciones.accounts import STATE_FILE, LoginLog, LoginResult
from rendiciones.automation.browser.autologin import AutoLogin, describe_page, fit_username
from rendiciones.credentials import Credential, MemoryStore


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.colmena.cl/afiliados/#/login", "www.colmena.cl/afiliados/#/login"),
        ("https://www.colmena.cl/afiliados/#/home?token=abc", "www.colmena.cl/afiliados/#/home"),
        ("https://auth.example.com/cb#code=abc&state=xyz", "auth.example.com/cb"),
        ("https://user:secret@example.com:8443/p?q=1", "example.com:8443/p"),
    ],
)
def test_describe_page_never_shows_parameters_or_credentials(url: str, expected: str) -> None:
    """Only host, path and SPA route reach the login log."""
    page = Mock(url=url)

    shown = describe_page(page)

    assert shown == expected


@pytest.mark.parametrize(
    ("maxlength", "username", "expected"),
    [
        ("10", "11.111.111-1", "111111111"),
        ("12", "11.111.111-1", "11.111.111-1"),
        (None, "11.111.111-1", "11.111.111-1"),
        ("5", "persona@example.com", "persona@example.com"),
    ],
)
def test_fit_username_compacts_only_a_rut_that_does_not_fit(
    maxlength: str | None, username: str, expected: str
) -> None:
    """A RUT longer than the field goes in compact; anything else is typed as saved."""
    field = Mock()
    field.get_attribute.return_value = maxlength

    fitted = fit_username(field, username)

    assert fitted == expected


def test_failed_screenshot_still_records_the_login_result(tmp_path: Path) -> None:
    """A screenshot that cannot be saved never hides the result that pauses auto-login."""
    store = MemoryStore()
    store.set("colmena", Credential("11.111.111-1", "clave", "2026-01-01T00:00:00+00:00"))
    log = LoginLog(tmp_path / STATE_FILE)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    auto_login = AutoLogin(store, log, screens_dir=blocker)
    page = Mock(url="chrome-error://chromewebdata/")
    page.screenshot.return_value = b"png"

    ok = auto_login.attempt(page, "colmena", lambda _: False, load_failed=True)

    assert not ok
    assert log.read()["colmena"]["result"] == LoginResult.FAILED
