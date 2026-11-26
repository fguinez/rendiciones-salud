"""Banco de Chile: read the CLP checking cartola's abonos with Playwright (read-only).

Ported from el-chanchito's ``banchile_web.py`` / ``banchile_movements.py``, reduced to
what a reimbursement check needs: log in with RUT + password, open the movements view,
pick the CLP account in the "Seleccione una cuenta" dialog and capture the SPA's own
``movimientos/getCartola`` response. For the abonos whose glosa is wanted, the
operation id ("ID Transacción") is read inline or with one ``cartola/detalle-glosa``
POST. Nothing is ever submitted.

The session (cookies and storage) is cached in ``browser-profile/bancochile-session.json``
(0600) so most runs skip the login. A rejected login pauses unattended logins (see
``LoginLog.blocked``) so a wrong password never locks the account.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Request,
    Response,
    sync_playwright,
)

from rendiciones.accounts import STATE_FILE, LoginLog, LoginResult
from rendiciones.bank import BANK_CREDENTIAL, BankError, BankMovement
from rendiciones.bank import cartola as ct
from rendiciones.config import FILE_MODE, Settings
from rendiciones.credentials import Credential, CredentialStore, CredentialStoreError

logger = logging.getLogger(__name__)

LOGIN_URL = "https://sitiospublicos.bancochile.cl/personas"
PORTAL_HOST = "portalpersonas.bancochile.cl"
PORTAL_HOME = f"https://{PORTAL_HOST}/mibancochile-web/front/persona/index.html#/home"
MOVIMIENTOS_ROUTE = "#/movimientos/cuenta/saldos-movimientos"
CARTOLA_FRAGMENT = "movimientos/getCartola"
DETALLE_GLOSA_URL = (
    f"https://{PORTAL_HOST}/mibancochile/rest/persona/bff-pper-prd-cta-movimientos/"
    "movimientos/cartola/detalle-glosa"
)
SESSION_FILE = "bancochile-session.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_MS = 15_000
LOGIN_TIMEOUT_MS = 45_000
SESSION_CHECK_MS = 30_000
CARTOLA_TIMEOUT_MS = 45_000
POLL_MS = 250
SETTLE_MS = 600
REOPEN_CLICKS = 3
ATTEMPTS = 2
MAX_ACCOUNTS = 4
MAX_ITEMS = 20
MAX_DETAIL_CALLS = 40
DETAIL_PAUSE_MS = 300

_RUT_SELECTORS = (
    'input[placeholder*="RUT"]',
    'input[placeholder*="rut"]',
    'input[autocomplete="username"]',
    'input[name="username"]',
    'input[name="rut"]',
    "#rut",
)
_PASSWORD_SELECTORS = (
    'input[type="password"]:visible',
    'input[autocomplete="current-password"]',
    'input[name="password"]',
    "#password",
)
_SUBMIT_SELECTORS = (
    'button[type="submit"]:visible',
    'button:has-text("Ingresar")',
    'button:has-text("Entrar")',
    'button:has-text("Acceder")',
)
_LOGIN_ACCESS_SELECTORS = (
    'a:has-text("Banco en Línea")',
    'a:has-text("Ingresar")',
    'button:has-text("Ingresar")',
)
_POPUP_CLOSE_SELECTORS = (
    "button.close",
    "[aria-label='Close']",
    "[aria-label='Cerrar']",
    "[aria-label='cerrar']",
    ".modal-close",
)
_LOGIN_SUCCESS_JS = f"() => location.hostname === '{PORTAL_HOST}'"
_REJECTED_RE = re.compile(
    r"(rut|clave|contrase[ñn]a)[^.\n]{0,60}(incorrect|inv[aá]lid|err[oó]ne)|bloquead", re.I
)
_LOGGED_IN_RE = re.compile(r"cerrar\s+sesi[oó]n|mis\s+productos", re.I)
_CURRENCY_SELECT = "mat-select[name=monedas]"
_CURRENCY_OPTION = "mat-option"
_ACCOUNT_RADIO = "mat-radio-button"
_MODAL_ACCEPT = "#modalPrimaryBtn button"
_REOPEN_RE = re.compile(r"SELECCIONAR\s+OTRA\s+CUENTA", re.I)
_CLP_RE = re.compile(r"\bCLP\b", re.I)
_SET_HASH_JS = "route => { window.location.hash = route; return true; }"
_POST_JSON_JS = """
async ({url, body}) => {
  const response = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
  });
  let payload = null;
  try { payload = await response.json(); } catch (error) { payload = null; }
  return {status: response.status, payload: payload};
}
"""


class LoginRejectedError(BankError):
    """The bank refused the saved RUT/password (or asked for an extra step)."""


def _session_path() -> Path:
    from rendiciones.automation.browser import profile_dir

    return profile_dir() / SESSION_FILE


def default_channel() -> str | None:
    """Installed Chrome when available (fewer bot checks than the bundled shell)."""
    from rendiciones.automation.browser.session import default_channel as portal_channel

    return portal_channel()


# ------------------------------------------------------------------ page helpers


def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> Locator | None:
    per_selector = max(1000, timeout_ms // len(selectors))
    for selector in selectors:
        try:
            element = page.locator(selector).first
            element.wait_for(state="visible", timeout=per_selector)
            return element
        except Exception:  # noqa: S112 - try the next selector
            continue
    return None


def _click_first(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> bool:
    element = _first_visible(page, selectors, timeout_ms)
    if element is None:
        return False
    try:
        element.click()
        return True
    except Exception:
        return False


def _on_portal(page: Page) -> bool:
    """On the portal itself: the login page carries the portal host in its redirect_uri."""
    try:
        return urlparse(page.url or "").hostname == PORTAL_HOST
    except Exception:
        return False


def _body_text(page: Page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def _visible_now(page: Page, selector: str) -> bool:
    try:
        return bool(page.locator(selector).first.is_visible())
    except Exception:
        return False


def _visible_count(locator: Locator) -> int:
    count = 0
    while count < MAX_ITEMS and locator.nth(count).is_visible():
        count += 1
    return count


def _wait_until(page: Page, condition: Callable[[], bool], timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(POLL_MS)


def _go_home(page: Page) -> None:
    try:
        page.goto(PORTAL_HOME, timeout=LOGIN_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception:
        logger.warning("bancochile: could not open the portal home")


def _dismiss_popup(page: Page) -> None:
    try:
        _click_first(page, _POPUP_CLOSE_SELECTORS, 4000)
    except Exception:
        logger.debug("bancochile: no popup to dismiss")


def _press_escape(page: Page) -> None:
    try:
        page.locator("body").press("Escape", timeout=5_000)
    except Exception:
        logger.debug("bancochile: escape failed")


# ------------------------------------------------------------------ capture


@dataclass
class _Capture:
    """The SPA's own ``getCartola`` responses (and their request bodies)."""

    page: Page
    payloads: list[dict[str, Any]] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)
    started: int = 0
    finished: int = 0

    def in_flight(self) -> bool:
        return self.started > self.finished

    def clear(self) -> None:
        self.payloads.clear()
        self.requests.clear()
        self.started = self.finished = 0

    def _on_request(self, request: Request) -> None:
        try:
            if CARTOLA_FRAGMENT in (request.url or ""):
                self.started += 1
        except Exception:
            logger.debug("bancochile: request hook failed")

    def _on_failed(self, request: Request) -> None:
        try:
            if CARTOLA_FRAGMENT in (request.url or ""):
                self.finished += 1
        except Exception:
            logger.debug("bancochile: failure hook failed")

    def _on_response(self, response: Response) -> None:
        try:
            if CARTOLA_FRAGMENT not in (response.url or ""):
                return
            self.finished += 1
            if response.status != 200:
                return
            payload = response.json()
            if isinstance(payload, dict):
                try:
                    body = response.request.post_data_json
                except Exception:
                    body = None
                self.payloads.append(payload)
                self.requests.append(body)
        except Exception:
            logger.debug("bancochile: response hook failed")

    def __enter__(self) -> _Capture:
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_failed)
        return self

    def __exit__(self, *exc: object) -> None:
        for event, handler in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfailed", self._on_failed),
        ):
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                logger.debug("bancochile: could not remove a listener")


# ------------------------------------------------------------------ the client


class BancoChileClient:
    """Reads abonos from the Banco de Chile checking account (CLP)."""

    name = BANK_CREDENTIAL

    def __init__(
        self,
        store: CredentialStore,
        log: LoginLog | None = None,
        *,
        session_file: Path | None = None,
        headless: bool = True,
        channel: str | None = None,
        detail_calls: int = MAX_DETAIL_CALLS,
    ) -> None:
        self.detail_calls = detail_calls
        self.store = store
        self.log = log
        self.session_file = session_file or _session_path()
        self.headless = headless
        self.channel = channel if channel is not None else default_channel()

    # ---------------------------------------------------------------- public

    def credits(self, since: date, wanted: Callable[[str], bool]) -> list[BankMovement]:
        """Abonos on or after ``since`` whose glosa satisfies ``wanted``.

        Runs in its own thread: Playwright's sync API refuses to start while the caller's
        thread already drives a portal session (the pipeline does).
        """
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bancochile") as pool:
            return pool.submit(self._credits, since, wanted).result()

    def _credits(self, since: date, wanted: Callable[[str], bool]) -> list[BankMovement]:
        credential = self._credential()
        with self._session(credential) as page:
            payloads = self._read_cartolas(page)
            movements = self._credits_from(page, payloads, wanted)
        found = [m for m in movements if m.occurred_on >= since]
        logger.info("bancochile: %d wanted abonos read", len(found))
        return found

    # ---------------------------------------------------------------- session

    def _credential(self) -> Credential:
        try:
            credential = self.store.get(BANK_CREDENTIAL)
        except CredentialStoreError as exc:
            raise BankError(str(exc)) from exc
        if credential is None:
            raise BankError("Guarda la cuenta del Banco de Chile en Cuentas")
        if self.log is not None and self.log.blocked(BANK_CREDENTIAL, credential):
            raise BankError(
                "El inicio de sesión en el Banco de Chile está en pausa (clave rechazada): "
                "guarda la clave de nuevo en Cuentas"
            )
        return credential

    def _record(self, result: LoginResult, detail: str) -> None:
        if self.log is not None:
            self.log.record(BANK_CREDENTIAL, result, detail)

    def _new_context(self, browser: Browser, state: dict[str, Any] | None) -> BrowserContext:
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="es-CL",
            timezone_id="America/Santiago",
            storage_state=state,  # type: ignore[arg-type]
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        return context

    def _load_state(self) -> dict[str, Any] | None:
        try:
            state = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def _save_state(self, context: BrowserContext) -> None:
        try:
            payload = json.dumps(context.storage_state())
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.session_file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            tmp.replace(self.session_file)
        except Exception as exc:
            logger.warning("bancochile: session not cached (%s)", type(exc).__name__)

    def _session_live(self, page: Page) -> bool:
        _go_home(page)
        deadline = time.monotonic() + SESSION_CHECK_MS / 1000
        streak = 0
        while _on_portal(page) and time.monotonic() < deadline:
            streak = streak + 1 if _LOGGED_IN_RE.search(_body_text(page)) else 0
            if streak >= 2:
                return True
            page.wait_for_timeout(1000)
        return False

    def _login(self, page: Page, credential: Credential) -> None:
        page.goto(LOGIN_URL, timeout=LOGIN_TIMEOUT_MS, wait_until="domcontentloaded")
        if _click_first(page, _LOGIN_ACCESS_SELECTORS, 5000):
            page.wait_for_timeout(3000)
        rut = _first_visible(page, _RUT_SELECTORS, 15_000)
        if rut is None:
            raise BankError("No se encontró el campo RUT del Banco de Chile")
        rut.fill(credential.username)
        password = _first_visible(page, _PASSWORD_SELECTORS, 8000)
        if password is None:
            raise BankError("No se encontró el campo de clave del Banco de Chile")
        password.fill(credential.password)
        if not _click_first(page, _SUBMIT_SELECTORS, 5000):
            password.press("Enter")
        try:
            page.wait_for_function(_LOGIN_SUCCESS_JS, timeout=LOGIN_TIMEOUT_MS)
        except Exception as exc:
            if _REJECTED_RE.search(_body_text(page)):
                self._record(LoginResult.REJECTED, "El banco rechazó el RUT o la clave")
                raise LoginRejectedError("El Banco de Chile rechazó el RUT o la clave") from exc
            self._record(LoginResult.FAILED, "No se llegó al portal tras iniciar sesión")
            raise BankError(
                "No se pudo iniciar sesión en el Banco de Chile (mantención, verificación "
                "adicional o cambio de diseño)"
            ) from exc
        self._record(LoginResult.OK, "Sesión iniciada")
        logger.info("bancochile: logged in")

    @contextmanager
    def _session(self, credential: Credential) -> Iterator[Page]:
        with sync_playwright() as playwright:
            kwargs: dict[str, Any] = {"headless": self.headless}
            if self.channel:
                kwargs["channel"] = self.channel
            browser = playwright.chromium.launch(**kwargs)
            try:
                context: BrowserContext | None = None
                state = self._load_state()
                if state is not None:
                    context = self._new_context(browser, state)
                    page = context.new_page()
                    if self._session_live(page):
                        logger.info("bancochile: cached session reused")
                    else:
                        context.close()
                        context = None
                if context is None:
                    self.session_file.unlink(missing_ok=True)
                    context = self._new_context(browser, None)
                    page = context.new_page()
                    self._login(page, credential)
                _dismiss_popup(page)
                self._save_state(context)
                yield page
                self._save_state(context)
            finally:
                browser.close()

    # ---------------------------------------------------------------- cartola

    def _read_cartolas(self, page: Page) -> list[dict[str, Any]]:
        last_error = "sin respuesta"
        for attempt in range(ATTEMPTS):
            payloads, error = self._read_accounts(page)
            if payloads is not None:
                return payloads
            last_error = error
            logger.info("bancochile: cartola attempt %d failed: %s", attempt + 1, error)
            page.wait_for_timeout(2000 * (attempt + 1))
        raise BankError(f"No se pudo leer la cartola del Banco de Chile ({last_error})")

    def _read_accounts(self, page: Page) -> tuple[list[dict[str, Any]] | None, str]:
        payloads: list[dict[str, Any]] = []
        seen: set[str] = set()
        accounts, index = 1, 0
        try:
            while index < min(accounts, MAX_ACCOUNTS):
                payload, count, error = self._drive(page, index, frozenset(seen))
                if count:
                    accounts = count
                if payload is None:
                    if index == 0:
                        return None, error
                    logger.warning("bancochile: account %d not read: %s", index + 1, error)
                    break
                payloads.append(payload)
                seen.add(ct.account_number(payload))
                index += 1
        finally:
            for _ in range(2):
                _press_escape(page)
            _go_home(page)
        return payloads, ""

    def _drive(
        self, page: Page, index: int, read_accounts: frozenset[str]
    ) -> tuple[dict[str, Any] | None, int, str]:
        """Route to the movements view, pick CLP account ``index`` and capture its cartola."""
        _go_home(page)
        if not _on_portal(page):
            return None, 0, "la sesión no está en el portal"
        with _Capture(page) as capture:
            try:
                page.wait_for_function(_SET_HASH_JS, arg=MOVIMIENTOS_ROUTE, timeout=10_000)
            except Exception:
                return None, 0, "no se pudo abrir Movimientos"
            if not self._open_dialog(page, capture):
                return None, 0, "no se abrió el selector de cuenta"
            if not self._select_clp(page):
                return None, 0, "no apareció la opción CLP"
            radios = page.locator(_ACCOUNT_RADIO)
            try:
                count = _visible_count(radios)
            except Exception:
                count = 0
            if not count:
                return None, 0, "no apareció ninguna cuenta"
            if index >= count:
                return None, count, "no hay más cuentas"
            radio = radios.nth(index)
            radio_id = radio.get_attribute("id")
            (page.locator(f"#{radio_id} label").first if radio_id else radio).click()
            page.wait_for_timeout(SETTLE_MS)
            _wait_until(page, lambda: not capture.in_flight(), 10_000)
            capture.clear()
            if not _click_first(page, (_MODAL_ACCEPT,), CARTOLA_TIMEOUT_MS):
                return None, count, "no se pudo aceptar la cuenta"
            payload = self._wait_cartola(page, capture, read_accounts)
            if payload is None:
                return None, count, "el banco no entregó la cartola"
            return payload, count, ""

    def _open_dialog(self, page: Page, capture: _Capture) -> bool:
        deadline = time.monotonic() + CARTOLA_TIMEOUT_MS / 1000
        reopen = page.get_by_role("button", name=_REOPEN_RE).filter(visible=True).first
        while not _visible_now(page, _CURRENCY_SELECT):
            try:
                if reopen.is_visible():
                    break
            except Exception:
                logger.debug("bancochile: reopen probe failed")
            if not _on_portal(page) or time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(POLL_MS)
        else:
            return True
        _wait_until(page, lambda: bool(capture.payloads), 4000)
        page.wait_for_timeout(SETTLE_MS)
        for _ in range(REOPEN_CLICKS):
            try:
                reopen.click(timeout=5000)
            except Exception:
                page.wait_for_timeout(SETTLE_MS)
                continue
            if _wait_until(page, lambda: _visible_now(page, _CURRENCY_SELECT), 5000):
                return True
        return False

    def _select_clp(self, page: Page) -> bool:
        try:
            select = page.locator(_CURRENCY_SELECT).first
            select.wait_for(state="visible", timeout=CARTOLA_TIMEOUT_MS)
            select.click()
            page.wait_for_timeout(SETTLE_MS)
            options = page.locator(_CURRENCY_OPTION)
            for i in range(_visible_count(options)):
                if _CLP_RE.search(options.nth(i).inner_text() or ""):
                    options.nth(i).click()
                    page.wait_for_timeout(SETTLE_MS)
                    return True
        except Exception:
            logger.debug("bancochile: currency select failed", exc_info=True)
        return False

    @staticmethod
    def _wait_cartola(
        page: Page, capture: _Capture, read_accounts: frozenset[str]
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + CARTOLA_TIMEOUT_MS / 1000
        checked = 0
        while True:
            while checked < len(capture.payloads):
                payload, body = capture.payloads[checked], capture.requests[checked]
                checked += 1
                account = ct.account_number(payload)
                if ct.is_clp(payload, body) and not (account and account in read_accounts):
                    return payload
                deadline = min(deadline, time.monotonic() + 8)
            if not _on_portal(page) or time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(500)

    def _credits_from(
        self, page: Page, payloads: list[dict[str, Any]], wanted: Callable[[str], bool]
    ) -> list[BankMovement]:
        movements: dict[str, BankMovement] = {}
        calls = self.detail_calls
        for payload in payloads:
            for raw in ct.rows(payload):
                if not ct.is_credit(raw) or not wanted(ct.description(raw)):
                    continue
                op_id = ct.operation_id_from_glosa(raw.get("detalleGlosa"))
                if op_id is None and calls > 0 and ct.needs_detail(raw):
                    calls -= 1
                    op_id = self._detail_id(page, raw)
                movement = ct.parse_credit(raw, op_id)
                if movement is not None:
                    movements.setdefault(movement.key, movement)
        return list(movements.values())

    @staticmethod
    def _detail_id(page: Page, raw: dict[str, Any]) -> str | None:
        try:
            handle = page.wait_for_function(
                _POST_JSON_JS,
                arg={"url": DETALLE_GLOSA_URL, "body": ct.detail_body(raw)},
                timeout=25_000,
            )
            result = handle.json_value()
            handle.dispose()
        except Exception:
            logger.debug("bancochile: detalle-glosa failed")
            return None
        finally:
            page.wait_for_timeout(DETAIL_PAUSE_MS)
        if not isinstance(result, dict) or result.get("status") != 200:
            return None
        return ct.operation_id_from_detail(result.get("payload"))


def bank_client(settings: Settings | None = None, *, headless: bool = True) -> BancoChileClient:
    """The real client, with the keychain credential and the shared login log."""
    from rendiciones.credentials import KeychainStore

    settings = settings or Settings.from_env()
    return BancoChileClient(
        KeychainStore(), LoginLog(settings.data_dir / STATE_FILE), headless=headless
    )


def test_connection(credential: Credential | None) -> tuple[bool, str]:
    """Log in and read the cartola once (Cuentas → "Probar conexión")."""
    if credential is None:
        return False, "Guarda primero el RUT y la clave del Banco de Chile"
    from rendiciones.credentials import MemoryStore

    store = MemoryStore()
    store.set(BANK_CREDENTIAL, credential)
    client = BancoChileClient(store, detail_calls=0)
    try:
        abonos = client.credits(date.min, lambda _: True)
    except BankError as exc:
        return False, str(exc)
    except Exception as exc:
        logger.warning("bancochile: connection test failed: %s", type(exc).__name__)
        return False, f"No se pudo consultar el banco ({type(exc).__name__})"
    return True, f"Conexión correcta: la cartola trae {len(abonos)} abono(s)"
