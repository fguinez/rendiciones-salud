"""Colmena (Isapre) browser client: submit, check_status, download_bonificacion.

Flow and selectors follow docs/portals.md §1. Important properties of the portal:

* Selecting a file uploads it immediately to a validation "robot", so in dry-run mode we
  stop before touching any file input.
* With T&C ticked, "Continuar" can finalise the request outright. An idempotency marker is
  written to the DB right before that click; a later run that finds the marker without a
  request number reconciles against Colmena's recent-requests JSON instead of resubmitting.
* The excedentes step, when shown, is always answered "S" (use excedentes as an
  additional bonificación).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Request, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from rendiciones.accounts import LoginResult
from rendiciones.automation.base import (
    PortalError,
    PortalState,
    SessionExpiredError,
    StatusResult,
    SubmissionResult,
)
from rendiciones.automation.browser.autologin import AutoLogin, LoginFailure
from rendiciones.automation.browser.common import (
    MARKER_DONE,
    MARKER_FINAL_CLICK,
    SubmissionLedger,
    private_tmpdir,
    stamp,
    take_screenshot,
)
from rendiciones.automation.browser.files import UploadFile, prepare_colmena
from rendiciones.automation.browser.mappings import (
    SLOT_LABELS,
    ColmenaPrestacion,
    ColmenaSlot,
    assign_colmena_documents,
    colmena_duplicates,
    colmena_hints,
    colmena_prestacion,
    find_colmena_request,
    norm,
    parse_colmena_status,
    pick_label,
)
from rendiciones.automation.browser.selectors import ColmenaSel, ColmenaUrls
from rendiciones.automation.browser.session import BrowserSession
from rendiciones.config import FILE_MODE, ensure_private_dir
from rendiciones.domain import RendicionView

logger = logging.getLogger(__name__)

PORTAL = "colmena"

# Both scripts run inside the logged-in page, so the auth token never reaches Python.
# The header choice mirrors Colmena's own HTTP interceptor.
_AUTH_HEADERS = """
const authHeaders = (key) => {
  const raw = window.localStorage.getItem(key);
  if (!raw) { return null; }
  if (raw.startsWith('Bearer ')) { return {'X-Auth-Titular-Vigente': raw}; }
  let token = raw;
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed === 'string') { token = parsed; }
    else if (parsed && typeof parsed === 'object') {
      token = parsed.token || parsed.authToken || parsed.access_token
        || parsed['X-Auth-Token'] || raw;
    }
  } catch (e) { /* plain string token */ }
  return {'X-Auth-Token': token};
};
"""

_FETCH_JSON = (
    "async ([url, key]) => {"
    + _AUTH_HEADERS
    + """
  const auth = authHeaders(key);
  if (!auth) { return {status: 0, body: null}; }
  const r = await fetch(url, {headers: {...auth, 'Accept': 'application/json'},
                              credentials: 'include'});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return {status: r.status, body: body};
}
"""
)

# The same POST the "Comprobante reembolso" button sends; the PDF comes back as base64.
_FETCH_REPORT = (
    "async ([url, key, payload]) => {"
    + _AUTH_HEADERS
    + """
  const auth = authHeaders(key);
  if (!auth) { return {status: 0, pdf: null}; }
  const r = await fetch(url, {method: 'POST', credentials: 'include',
    headers: {...auth, 'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const bytes = new Uint8Array(await r.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return {status: r.status, pdf: btoa(binary)};
}
"""
)


def session_state(page: Page, urls: ColmenaUrls) -> str:
    """``"in"``, ``"out"`` or ``"loading"`` for the Colmena tab."""
    url = page.url
    if not url.startswith(urls.base) or "#/login" in url:
        return "out" if url.startswith(("http://", "https://")) else "loading"
    try:
        if page.locator(ColmenaSel.PASSWORD_INPUT).first.is_visible():
            return "out"
        has_token = page.evaluate("k => !!window.localStorage.getItem(k)", ColmenaSel.TOKEN_KEY)
        app_ready = page.locator(f"{ColmenaSel.APP_ROOT} *").count() > 0
    except Exception:
        return "loading"
    if has_token:
        return "in"
    return "out" if app_ready else "loading"


def is_logged_in(page: Page, urls: ColmenaUrls | None = None) -> bool:
    """Used by ``rendiciones login colmena``."""
    return session_state(page, urls or ColmenaUrls()) == "in"


def read_login_failure(page: Page) -> LoginFailure | None:
    """Colmena's own answer to a submitted login, when it is not a success."""
    if ColmenaSel.PASSWORD_EXPIRED_ROUTE in page.url:
        return LoginFailure(
            LoginResult.NEEDS_USER,
            "La clave de Colmena caducó: cámbiala e inicia sesión desde Cuentas",
        )
    message = page.locator(ColmenaSel.LOGIN_MESSAGE)
    if not message.count():
        return None
    text = " ".join(message.first.inner_text().split())
    if not text:
        return None
    if ColmenaSel.LOGIN_REJECTED_TEXT.casefold() in text.casefold():
        return LoginFailure(
            LoginResult.REJECTED, f"Colmena rechazó el RUT o la clave guardados: {text}"
        )
    return LoginFailure(
        LoginResult.NEEDS_USER, f"Colmena respondió «{text}»: inicia sesión desde Cuentas"
    )


def may_hold_session(page: Page, urls: ColmenaUrls) -> bool:
    """Whether the tab is on the app and may still hold its token (an unreadable one counts)."""
    if not page.url.startswith(urls.base):
        return False
    try:
        return bool(page.evaluate("k => !!window.localStorage.getItem(k)", ColmenaSel.TOKEN_KEY))
    except PlaywrightError:
        return True


def _items(body: Any) -> list[Mapping[str, Any]]:
    if isinstance(body, Mapping):
        body = body.get("response")
    if isinstance(body, Mapping):
        body = [body]
    if not isinstance(body, list):
        return []
    return [item for item in body if isinstance(item, Mapping)]


class ColmenaBrowserClient:
    """``ColmenaClient`` implemented with Playwright."""

    name = PORTAL

    def __init__(
        self,
        session: BrowserSession,
        ledger: SubmissionLedger,
        *,
        data_dir: Path,
        urls: ColmenaUrls | None = None,
        dry_run: bool = False,
        allow_duplicates: frozenset[int] = frozenset(),
        now: Callable[[], datetime] = datetime.now,
        auto_login: AutoLogin | None = None,
        login_timeout_s: float = 25,
        validation_timeout_s: float = 120,
        validation_start_timeout_s: float = 10,
        submit_timeout_s: float = 90,
        continue_timeout_s: float = 15,
        attach_check_timeout_s: float = 5,
        subtype_timeout_s: float = 20,
    ) -> None:
        self.session = session
        self.ledger = ledger
        self.urls = urls or ColmenaUrls()
        self.dry_run = dry_run
        self.allow_duplicates = allow_duplicates
        self.now = now
        self.screens_dir = data_dir / "screenshots"
        self.tmp_dir = data_dir / "tmp"
        self.auto_login = auto_login
        self._login_tried_at: float | None = None
        self.login_timeout_s = login_timeout_s
        self.validation_timeout_s = validation_timeout_s
        self.validation_start_timeout_s = validation_start_timeout_s
        self.submit_timeout_s = submit_timeout_s
        self.continue_timeout_s = continue_timeout_s
        self.attach_check_timeout_s = attach_check_timeout_s
        self.subtype_timeout_s = subtype_timeout_s

    def close(self) -> None:
        """Close the shared browser (it restarts lazily on next use)."""
        self.session.close()

    # ----------------------------------------------------------------- navigation

    def _open(self, url: str) -> Page:
        page = self.session.page(PORTAL)
        if page.url == url:
            page.reload(wait_until="domcontentloaded")
        else:
            page.goto(url, wait_until="domcontentloaded")
        deadline = time.monotonic() + self.login_timeout_s
        while True:
            state = session_state(page, self.urls)
            if state == "in":
                return page
            if state == "out" or time.monotonic() > deadline:
                logger.warning("colmena session not active")
                if not self._log_in(page):
                    raise SessionExpiredError(PORTAL)
                page.goto(url, wait_until="domcontentloaded")
                deadline = time.monotonic() + self.login_timeout_s
            page.wait_for_timeout(300)

    def _log_in(self, page: Page) -> bool:
        """Log in with the saved credential (at most once a minute, so never in a loop).

        It starts from a fresh load of the login route (an expired tab can be stuck on any
        page, and a hash-only ``goto`` would not restart the app) unless the tab may still
        hold a session, since opening the login route logs it out.
        """
        recent = self._login_tried_at is not None and time.monotonic() - self._login_tried_at < 60
        if self.auto_login is None or recent:
            return False
        self._login_tried_at = time.monotonic()
        load_failed = False
        try:
            if not may_hold_session(page, self.urls):
                page.goto(self.urls.login_form, wait_until="domcontentloaded")
                page.reload(wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            logger.warning("colmena login page slow to load; waiting for it")
        except PlaywrightError as exc:
            # ``attempt`` records the failed load with a screenshot instead of raising it.
            logger.warning("colmena login page did not load: %s", type(exc).__name__)
            load_failed = True
        return self.auto_login.attempt(
            page,
            PORTAL,
            lambda p: is_logged_in(p, self.urls),
            check_failure=read_login_failure,
            load_failed=load_failed,
        )

    def _retry_after_login[T](self, operation: Callable[[], T]) -> T:
        """Run ``operation``; if the session dies midway, log in again and run it once more.

        Safe for ``submit``: it reconciles against the idempotency marker before acting.
        """
        tried_before = self._login_tried_at
        try:
            return operation()
        except SessionExpiredError:
            if self.auto_login is None or self._login_tried_at != tried_before:
                raise
            logger.info("%s session expired mid-operation; logging in again", PORTAL)
            return operation()

    def fetch_requests(self, page: Page, url: str | None = None) -> list[Mapping[str, Any]]:
        """Recent requests JSON (``consulta/ultimas/0``) fetched inside the logged-in page."""
        result = page.evaluate(_FETCH_JSON, [url or self.urls.ultimas, ColmenaSel.TOKEN_KEY])
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
        if status in (0, 401, 403):
            if status:
                # The token is dead; without it the app shows its login form on reload.
                page.evaluate("k => window.localStorage.removeItem(k)", ColmenaSel.TOKEN_KEY)
            raise SessionExpiredError(PORTAL)
        if status != 200:
            raise PortalError(f"Colmena respondió HTTP {status} al consultar solicitudes")
        items = _items(result.get("body"))
        logger.info("colmena: %d recent requests fetched", len(items))
        return items

    # --------------------------------------------------------------------- submit

    def submit(self, rendicion: RendicionView) -> SubmissionResult:
        """Fill and send the reimbursement form (or stop before uploads in dry-run)."""
        return self._retry_after_login(lambda: self._submit(rendicion))

    def _submit(self, rendicion: RendicionView) -> SubmissionResult:
        hints = colmena_hints(rendicion.support_documents)
        prestacion = colmena_prestacion(rendicion.tipo_prestacion, hints, rendicion.id)
        slots = assign_colmena_documents(prestacion, rendicion.support_documents)
        with private_tmpdir(self.tmp_dir) as work:
            uploads = self._prepare(slots, work)
            page = self._open(self.urls.solicitar)
            try:
                recovered = self._reconcile(rendicion, self.fetch_requests(page))
                if recovered is not None:
                    return recovered
                self._confirm_bank_account(page)
                self._select_prestacion(page, prestacion)
                blocks = self._upload_blocks(page, uploads)
                files = sum(len(v) for v in uploads.values())
                if self.dry_run:
                    logger.info(
                        "rendicion %d: colmena dry-run stopped before uploading %d files",
                        rendicion.id,
                        files,
                    )
                    return SubmissionResult(
                        ok=True,
                        message=f"dry-run: formulario listo (prestación {prestacion.leaf_id}, "
                        f"{files} archivos) sin adjuntar ni enviar",
                        raw={"dry_run": True, "colmena_prestacion": prestacion.leaf_id},
                    )
                for slot, items in uploads.items():
                    if items:
                        self._attach(page, blocks[slot], slot, items)
                self._accept_terms(page)
                send = page.locator(ColmenaSel.boton("Continuar")).first
                self._check_clickable(send)
                self.ledger.set_marker(rendicion.id, PORTAL, MARKER_FINAL_CLICK)
                send.click()
                excedentes = self._finish(page)
                request_id = self._read_request_id(page)
                self.ledger.set_marker(rendicion.id, PORTAL, MARKER_DONE, request_id)
            except SessionExpiredError:
                raise
            except Exception:
                self._save_failure_screenshot(page, rendicion.id)
                raise
            shot = take_screenshot(
                page, self.screens_dir, f"colmena-{rendicion.id}-{stamp(self.now())}"
            )
        logger.info(
            "rendicion %d: colmena request created (excedentes=%s, files=%d)",
            rendicion.id,
            excedentes,
            files,
        )
        return SubmissionResult(
            ok=True,
            request_id=request_id,
            raw={
                "colmena_prestacion": prestacion.leaf_id,
                "excedentes": "S" if excedentes else "N",
                "screenshot": shot.name,
            },
        )

    def _prepare(
        self, slots: Mapping[ColmenaSlot, Sequence[Any]], work: Path
    ) -> dict[ColmenaSlot, list[UploadFile]]:
        flat = [(slot, d) for slot, docs in slots.items() for d in docs]
        prepared = prepare_colmena([(d.id, d.path, d.mime) for _, d in flat], work)
        result: dict[ColmenaSlot, list[UploadFile]] = {slot: [] for slot in slots}
        for (slot, _), item in zip(flat, prepared, strict=True):
            result[slot].append(item)
        return result

    def _reconcile(
        self, rendicion: RendicionView, items: Sequence[Mapping[str, Any]]
    ) -> SubmissionResult | None:
        """Never double-submit: honour an existing marker and look for an earlier request."""
        known = self.ledger.known_request_ids(PORTAL)
        marker = self.ledger.get_marker(rendicion.id, PORTAL)
        if marker is not None:
            if marker.request_id:
                logger.info("rendicion %d: colmena marker already has a request", rendicion.id)
                return SubmissionResult(
                    ok=True, request_id=marker.request_id, raw={"recuperado": True}
                )
            since = (marker.created_at - timedelta(days=1)).date()
            candidates = colmena_duplicates(
                items, monto=rendicion.monto, since=since, known_ids=known
            )
            if len(candidates) == 1:
                logger.info("rendicion %d: interrupted colmena submit reconciled", rendicion.id)
                self.ledger.set_marker(rendicion.id, PORTAL, MARKER_DONE, candidates[0])
                return SubmissionResult(ok=True, request_id=candidates[0], raw={"recuperado": True})
            raise PortalError(
                "Un envío anterior a Colmena quedó interrumpido y no se pudo confirmar. "
                "Revisa tus reembolsos en el portal: si la solicitud existe, registra su N° "
                "por la API; si no, ejecuta "
                f"`rendiciones clear-marker {rendicion.id} colmena` y vuelve a 'lista'."
            )
        if rendicion.id in self.allow_duplicates:
            return None
        duplicates = colmena_duplicates(
            items, monto=rendicion.monto, since=rendicion.fecha_atencion, known_ids=known
        )
        if duplicates:
            logger.warning("rendicion %d: possible duplicate in colmena", rendicion.id)
            raise PortalError(
                f"Colmena ya tiene la solicitud N° {duplicates[0]} con el mismo monto después "
                "de la fecha de atención. Si es esta rendición, registra ese N° por la API; si "
                f"no, reintenta con `run-pipeline --allow-duplicate {rendicion.id}`."
            )
        return None

    def _confirm_bank_account(self, page: Page) -> None:
        tipo = page.locator(ColmenaSel.TIPO_DROPDOWN)
        page.locator(f"{ColmenaSel.TIPO_DROPDOWN}, {ColmenaSel.BANK_EDIT}").first.wait_for(
            state="visible"
        )
        if not tipo.first.is_visible():
            page.locator(ColmenaSel.boton("Continuar")).first.click()
            tipo.first.wait_for(state="visible")
            logger.info("colmena: bank account step confirmed")

    def _choose(self, page: Page, dropdown: Locator, label: str, level: str) -> None:
        dropdown.locator(ColmenaSel.DROPDOWN_TRIGGER).first.click()
        items = dropdown.locator(ColmenaSel.DROPDOWN_ITEM)
        try:
            items.first.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            items = page.locator(ColmenaSel.DROPDOWN_ITEM)  # panel appended to <body>
            items.first.wait_for(state="visible")
        index = pick_label(items.all_inner_texts(), label)
        if index is None:
            raise PortalError(f"Colmena no ofrece la opción '{label}' ({level})")
        # The upload blocks on screen until this answer arrives are placeholders: the answer
        # re-renders them and drops any file selected before it.
        try:
            with page.expect_response(
                lambda r: ColmenaSel.SUBTYPE_URL_PART in r.url,
                timeout=self.subtype_timeout_s * 1000,
            ) as answer:
                items.nth(index).click()
        except PlaywrightTimeoutError as exc:
            raise PortalError(f"Colmena no respondió al elegir '{label}' ({level})") from exc
        if answer.value.status >= 400:
            raise PortalError(
                f"Colmena respondió con error {answer.value.status} al elegir '{label}' ({level})"
            )
        page.wait_for_timeout(300)  # let Angular render the blocks for this answer

    def _select_prestacion(self, page: Page, prestacion: ColmenaPrestacion) -> None:
        self._choose(
            page, page.locator(ColmenaSel.TIPO_DROPDOWN).first, prestacion.tipo_label, "tipo"
        )
        for level, label in enumerate(prestacion.sub_labels):
            dropdown = page.locator(ColmenaSel.SUBTYPE_DROPDOWN).nth(level)
            dropdown.wait_for(state="visible", timeout=20_000)
            self._choose(page, dropdown, label, f"subtipo {level + 1}")
        logger.info("colmena: prestacion %d selected", prestacion.leaf_id)

    def _upload_blocks(
        self, page: Page, uploads: Mapping[ColmenaSlot, Sequence[UploadFile]]
    ) -> dict[ColmenaSlot, Locator]:
        blocks = page.locator(ColmenaSel.UPLOAD_BLOCK)
        blocks.first.wait_for(state="visible", timeout=20_000)
        found: dict[ColmenaSlot, Locator] = {}
        for index in range(blocks.count()):
            block = blocks.nth(index)
            if not block.is_visible():
                continue
            label_loc = block.locator(ColmenaSel.UPLOAD_LABEL)
            label = norm(label_loc.first.inner_text()) if label_loc.count() else ""
            for key, slot_name in ColmenaSel.BLOCK_LABELS:
                slot = ColmenaSlot(slot_name)
                if key in label and slot not in found:
                    found[slot] = block
                    break
        for slot, items in uploads.items():
            if items and slot not in found:
                raise PortalError(f"Colmena no muestra el bloque '{SLOT_LABELS[slot]}'")
        return found

    def _attach(
        self, page: Page, block: Locator, slot: ColmenaSlot, items: Sequence[UploadFile]
    ) -> None:
        """Select the files and check the page kept them, selecting them once more if not.

        A file the page drops leaves the block empty and Continuar disabled, so fail here,
        before the final-click marker, with nothing sent.
        """
        toast = None
        for attempt in (1, 2):
            toast = self._select_files(page, block, slot, items) or toast
            if self._files_listed(block, len(items)):
                return
            logger.warning(
                "colmena: %s file(s) not kept by the page (attempt %d)", slot.value, attempt
            )
            # Chromium fires no change event when the same files are selected again.
            block.locator(ColmenaSel.FILE_INPUT).first.set_input_files([])
        raise PortalError(
            f"Colmena no tomó el archivo de '{SLOT_LABELS[slot]}', así que la solicitud no se envió"
            + (f" ({toast})" if toast else "")
            + ". Revisa la captura del error en screenshots/, si quedó, antes de reintentar."
        )

    def _files_listed(self, block: Locator, count: int) -> bool:
        listed = block.locator(ColmenaSel.ATTACHED_FILE).nth(count - 1)
        try:
            listed.wait_for(state="visible", timeout=self.attach_check_timeout_s * 1000)
        except PlaywrightTimeoutError:
            return False
        return True

    def _select_files(
        self, page: Page, block: Locator, slot: ColmenaSlot, items: Sequence[UploadFile]
    ) -> str | None:
        """Select the files and wait for the validation robot; return any toast seen meanwhile.

        Colmena's server picks the flow per page load (``tipo-procesamiento``): in
        ``flujo_colmena`` selecting a file posts nothing and the files travel with Continuar.
        Toasts fade after a few seconds, so they are read while waiting.
        """
        toast = None
        requests: list[Request] = []
        responses: list[Response] = []

        def on_request(request: Request) -> None:
            if ColmenaSel.VALIDATION_URL_PART in request.url and request.method == "POST":
                requests.append(request)

        def on_response(response: Response) -> None:
            if ColmenaSel.VALIDATION_URL_PART in response.url and response.request.method == "POST":
                responses.append(response)

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            block.locator(ColmenaSel.FILE_INPUT).first.set_input_files(
                [str(item.path) for item in items]
            )
            start_deadline = time.monotonic() + self.validation_start_timeout_s
            while not requests and time.monotonic() < start_deadline:
                page.wait_for_timeout(250)
                toast = self._growl_text(page, ColmenaSel.GROWL_MESSAGE) or toast
            deadline = time.monotonic() + self.validation_timeout_s
            while len(responses) < len(requests):
                if time.monotonic() > deadline:
                    raise PortalError(
                        f"Colmena no respondió la validación de '{SLOT_LABELS[slot]}' a tiempo"
                    )
                page.wait_for_timeout(250)
            page.wait_for_timeout(800)  # let the growl render
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
        if any(r.status >= 400 for r in responses):
            raise PortalError(f"Colmena rechazó la validación de '{SLOT_LABELS[slot]}'")
        text = self._growl_error(page)
        if text is not None:
            raise PortalError(f"Colmena rechazó '{SLOT_LABELS[slot]}': {text}")
        if requests:
            logger.info("colmena: %d file(s) validated for %s", len(items), slot.value)
        else:
            logger.info("colmena: %d file(s) attached for %s without robot", len(items), slot.value)
        return self._growl_text(page, ColmenaSel.GROWL_MESSAGE) or toast

    def _growl_error(self, page: Page) -> str | None:
        return self._growl_text(page, ColmenaSel.GROWL_ERROR)

    def _growl_text(self, page: Page, selector: str) -> str | None:
        growl = page.locator(selector)
        if growl.count() and growl.first.is_visible():
            return " ".join(growl.first.inner_text().split())[:200]
        return None

    def _accept_terms(self, page: Page) -> None:
        checkbox = page.locator(ColmenaSel.TERMS_CHECKBOX).first
        box_input = checkbox.locator(ColmenaSel.TERMS_INPUT).first
        if not box_input.is_checked():
            checkbox.locator(ColmenaSel.TERMS_BOX).first.click()
        if not box_input.is_checked():
            raise PortalError("No se pudo aceptar términos y condiciones en Colmena")

    def _check_clickable(self, button: Locator) -> None:
        """Fail before the final-click marker when Continuar can't be clicked: nothing was sent."""
        try:
            button.click(trial=True, timeout=self.continue_timeout_s * 1000)
        except PlaywrightTimeoutError as exc:
            text = self._growl_error(button.page)
            raise PortalError(
                "Colmena no dejó presionar Continuar, así que la solicitud no se envió"
                + (f" ({text})" if text else "")
                + ". Revisa la captura del error en screenshots/, si quedó, antes de reintentar."
            ) from exc

    def _save_failure_screenshot(self, page: Page, rendicion_id: int) -> None:
        name = f"colmena-{rendicion_id}-error-{stamp(self.now())}"
        try:
            take_screenshot(page, self.screens_dir, name)
        except (PlaywrightError, OSError):
            # Best effort: the submit error being re-raised matters more than its screenshot.
            logger.warning("rendicion %d: colmena failure screenshot not saved", rendicion_id)

    def _finish(self, page: Page) -> bool:
        """Wait for excedentes or confirmation; answer excedentes "S". True if it was shown."""
        outcome = page.locator(
            f"{ColmenaSel.EXCEDENTES_BOX}:visible, {ColmenaSel.CONFIRMATION}:visible, "
            f"{ColmenaSel.GROWL_ERROR}:visible"
        )
        timeout = self.submit_timeout_s * 1000
        try:
            outcome.first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError as exc:
            raise PortalError(
                "Colmena no confirmó el envío a tiempo; revisa el portal antes de reintentar"
            ) from exc
        confirmation = page.locator(f"{ColmenaSel.CONFIRMATION}:visible")
        box = page.locator(f"{ColmenaSel.EXCEDENTES_BOX}:visible")
        if confirmation.count():
            return False
        if not box.count():
            text = " ".join(page.locator(ColmenaSel.GROWL_ERROR).first.inner_text().split())
            raise PortalError(f"Colmena rechazó la solicitud: {text[:200]}")
        radio = page.locator(ColmenaSel.EXCEDENTES_YES).first
        radio.locator(ColmenaSel.RADIO_BOX).first.click()
        radio_input = radio.locator("input[type=radio]")
        if radio_input.count() and not radio_input.first.is_checked():
            raise PortalError("No se pudo elegir usar excedentes en Colmena")
        scoped = box.first.locator(ColmenaSel.boton("Continuar"))
        (
            scoped.first if scoped.count() else page.locator(ColmenaSel.boton("Continuar")).last
        ).click()
        logger.info("colmena: excedentes step answered S")
        try:
            confirmation.first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError as exc:
            raise PortalError(
                "Colmena no confirmó el envío tras excedentes; revisa el portal antes de reintentar"
            ) from exc
        return True

    def _read_request_id(self, page: Page) -> str:
        text = page.locator(f"{ColmenaSel.CONFIRMATION}:visible").first.inner_text()
        match = re.search(r"Nro\.?\s*de\s*Solicitud\D{0,10}(\d{3,12})", text, re.IGNORECASE)
        if match is None:
            raise PortalError(
                "Colmena confirmó la solicitud pero no se pudo leer su número; revisa el portal"
            )
        return match.group(1)

    # ---------------------------------------------------------------- status/download

    def check_status(self, request_id: str) -> StatusResult:
        """Status from the recent-requests JSON."""
        return self._retry_after_login(lambda: self._check_status(request_id))

    def _check_status(self, request_id: str) -> StatusResult:
        page = self._open(self.urls.consultar)
        item = find_colmena_request(self.fetch_requests(page), request_id)
        if item is None and request_id.isdigit():
            item = find_colmena_request(
                self.fetch_requests(page, self.urls.ultimas_folio(request_id)), request_id
            )
        if item is None:
            logger.warning("colmena: request not found in recent list")
            return StatusResult(
                state=PortalState.PENDING, message="Solicitud aún no aparece en Colmena"
            )
        return parse_colmena_status(item)

    def download_bonificacion(self, request_id: str, dest: Path) -> Path:
        """Download the 'Comprobante reembolso' PDF into ``dest`` (0600)."""
        return self._retry_after_login(lambda: self._download_bonificacion(request_id, dest))

    def _download_bonificacion(self, request_id: str, dest: Path) -> Path:
        folio = request_id.strip()
        if not folio.isdigit():
            raise PortalError("El número de solicitud de Colmena no es numérico")
        page = self._open(self.urls.consultar)
        payload = {
            "pdfName": ColmenaSel.REPORT_PDF_NAME,
            "nombre": "comprobante",
            "opcion": "pdf",
            "parametros": {"folioSolicitud": int(folio)},
        }
        result = page.evaluate(_FETCH_REPORT, [self.urls.report, ColmenaSel.TOKEN_KEY, payload])
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
        if status in (0, 401, 403):
            if status:
                page.evaluate("k => window.localStorage.removeItem(k)", ColmenaSel.TOKEN_KEY)
            raise SessionExpiredError(PORTAL)
        if status != 200:
            raise PortalError(f"Colmena respondió HTTP {status} al pedir el comprobante")
        pdf = base64.b64decode(str(result.get("pdf") or ""))
        if not pdf.startswith(b"%PDF-"):
            raise PortalError("Colmena no entregó un comprobante en PDF para esta solicitud")
        ensure_private_dir(dest)
        target = dest / f"bonificacion-colmena-{folio}.pdf"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf)
        target.chmod(FILE_MODE)
        logger.info("colmena: comprobante downloaded (%d bytes)", len(pdf))
        return target
