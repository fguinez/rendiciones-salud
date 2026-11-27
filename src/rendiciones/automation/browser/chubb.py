"""Chubb (seguro complementario) browser client: submit, check_status, download_liquidacion.

Flow and selectors follow docs/portals.md §2. The portal keeps its session in
sessionStorage per tab and redirects /private/* deep links to home, so every action starts
at /private/home and navigates by clicking inside the app. API responses are encrypted,
so status comes from the refund-history table in the DOM.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Download, Locator, Page, Request, Response
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from rendiciones.automation.base import (
    PortalError,
    PortalState,
    SessionExpiredError,
    StatusResult,
    SubmissionResult,
)
from rendiciones.automation.browser.autologin import AutoLogin
from rendiciones.automation.browser.common import (
    MARKER_DONE,
    MARKER_FINAL_CLICK,
    SubmissionLedger,
    private_tmpdir,
    stamp,
    take_screenshot,
)
from rendiciones.automation.browser.files import UploadFile, prepare_chubb
from rendiciones.automation.browser.mappings import (
    ChubbCard,
    ChubbPrestacion,
    assign_chubb_documents,
    check_chubb_date,
    chubb_date_input,
    chubb_date_shown,
    chubb_prestacion,
    norm,
    parse_chubb_status,
    parse_multipart_field,
    pick_label,
)
from rendiciones.automation.browser.selectors import ChubbSel, ChubbUrls
from rendiciones.automation.browser.session import BrowserSession
from rendiciones.domain import DocumentView, RendicionView
from rendiciones.rut import normalize as normalize_rut

logger = logging.getLogger(__name__)

PORTAL = "chubb"
_CARD_NOISE = frozenset({"obligatorio", "opcional", "adjuntar documentos", "ayuda", "help"})


def _pdf_from_response(response: Response) -> bytes | None:
    """The PDF in a liquidation-document reply (``archivoBase64``), or None if unreadable."""
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        payload = payload["response"]
    encoded = payload.get("archivoBase64") if isinstance(payload, dict) else None
    if not isinstance(encoded, str):
        return None
    try:
        pdf = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        return None
    return pdf if pdf.startswith(b"%PDF") else None


def session_state(page: Page, urls: ChubbUrls) -> str:
    """``"in"``, ``"out"`` or ``"loading"`` for the Chubb tab."""
    url = page.url
    host = urlparse(url).hostname or ""
    try:
        if any(part in host for part in ChubbSel.AUTH_HOST_PARTS):
            visible = page.locator("input[type=password]").first.is_visible()
            return "out" if visible else "loading"
        if not url.startswith(urls.base):
            return "loading"
        relative = "/" + url[len(urls.base) :]
        if urlparse(relative).path.rstrip("/") == ChubbSel.LOGIN_PATH:
            return "loading"
        if not relative.startswith(ChubbSel.PRIVATE_PREFIX):
            return "out" if page.locator("app-root *").count() > 0 else "loading"
        for text in (ChubbSel.REFUND_TILE_TEXT, ChubbSel.HISTORY_BUTTON_TEXT):
            if page.get_by_text(text).first.is_visible():
                return "in"
        if (
            page.locator(ChubbSel.BENEFICIARY_GROUP).count()
            or page.locator(ChubbSel.HISTORY_TABLE).count()
        ):
            return "in"
    except Exception:
        return "loading"
    return "loading"


def is_logged_in(page: Page, urls: ChubbUrls | None = None) -> bool:
    """Used by ``rendiciones login chubb``."""
    return session_state(page, urls or ChubbUrls()) == "in"


class ChubbBrowserClient:
    """``PortalClient`` for Chubb implemented with Playwright."""

    name = PORTAL

    def __init__(
        self,
        session: BrowserSession,
        ledger: SubmissionLedger,
        *,
        data_dir: Path,
        urls: ChubbUrls | None = None,
        dry_run: bool = False,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = datetime.now,
        auto_login: AutoLogin | None = None,
        login_timeout_s: float = 45,
        submit_timeout_s: float = 120,
    ) -> None:
        self.session = session
        self.ledger = ledger
        self.urls = urls or ChubbUrls()
        self.dry_run = dry_run
        self.today = today
        self.now = now
        self.screens_dir = data_dir / "screenshots"
        self.tmp_dir = data_dir / "tmp"
        self.auto_login = auto_login
        self._login_tried_at: float | None = None
        self.login_timeout_s = login_timeout_s
        self.submit_timeout_s = submit_timeout_s

    def close(self) -> None:
        """Close the shared browser (it restarts lazily on next use)."""
        self.session.close()

    # ----------------------------------------------------------------- navigation

    def _home(self) -> Page:
        page = self.session.page(PORTAL)
        page.goto(self.urls.home, wait_until="domcontentloaded")
        deadline = time.monotonic() + self.login_timeout_s
        while True:
            state = session_state(page, self.urls)
            if state == "in":
                return page
            if state == "out" or time.monotonic() > deadline:
                logger.warning("chubb session not active")
                if not self._log_in(page):
                    raise SessionExpiredError(PORTAL)
                page.goto(self.urls.home, wait_until="domcontentloaded")
                deadline = time.monotonic() + self.login_timeout_s
            page.wait_for_timeout(500)

    def _log_in(self, page: Page) -> bool:
        """Log in with the saved credential (at most once a minute, so never in a loop)."""
        recent = self._login_tried_at is not None and time.monotonic() - self._login_tried_at < 60
        if self.auto_login is None or recent:
            return False
        self._login_tried_at = time.monotonic()
        return self.auto_login.attempt(page, PORTAL, lambda p: is_logged_in(p, self.urls))

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

    def _ensure_private(self, page: Page) -> None:
        if session_state(page, self.urls) == "out":
            raise SessionExpiredError(PORTAL)

    # --------------------------------------------------------------------- submit

    def submit(self, rendicion: RendicionView) -> SubmissionResult:
        """Fill the refund form and send it (stops before 'Enviar solicitud' in dry-run)."""
        return self._retry_after_login(lambda: self._submit(rendicion))

    def _submit(self, rendicion: RendicionView) -> SubmissionResult:
        prestacion = chubb_prestacion(rendicion.tipo_prestacion, rendicion.id)
        fecha = check_chubb_date(rendicion.fecha_atencion, self.today())
        bonos = rendicion.bonificacion_documents
        if not bonos:
            raise PortalError("Falta el comprobante de bonificación de Colmena")
        recovered = self._reconcile(rendicion)
        if recovered is not None:
            return recovered
        with private_tmpdir(self.tmp_dir) as work:
            page = self._home()
            try:
                page.get_by_text(ChubbSel.REFUND_TILE_TEXT).first.click()
                page.locator(ChubbSel.BENEFICIARY_GROUP).first.wait_for(state="visible")
                self._ensure_private(page)
                self._select_beneficiary(page, rendicion.support_documents)
                self._select_benefit(page, prestacion)
                self._fill_date(page, fecha)
                self._set_previously_reimbursed(page)
                cards = self._read_cards(page)
                assignment = assign_chubb_documents(
                    [card for card, _ in cards], rendicion.support_documents, bonos
                )
                prepared = self._prepare(assignment, work)
                # Chubb keeps files client-side until the final submit (no upload on select).
                for card, locator in cards:
                    if card.title in prepared:
                        locator.locator(ChubbSel.FILE_INPUT).first.set_input_files(
                            [str(f.path) for f in prepared[card.title]]
                        )
                files = sum(len(v) for v in prepared.values())
                logger.info(
                    "rendicion %d: chubb %d files attached to %d cards",
                    rendicion.id,
                    files,
                    len(prepared),
                )
                send = self._ready_to_send(page)
                if self.dry_run:
                    logger.info("rendicion %d: chubb dry-run stopped before sending", rendicion.id)
                    return SubmissionResult(
                        ok=True,
                        message=f"dry-run: formulario Chubb completo ({prestacion.benefit}, "
                        f"{files} archivos) sin enviar",
                        raw={"dry_run": True, "chubb_benefit": prestacion.benefit},
                    )
                self.ledger.set_marker(rendicion.id, PORTAL, MARKER_FINAL_CLICK)
                request = self._send(page, send)
                request_id = self._request_id(page, request)
                self.ledger.set_marker(rendicion.id, PORTAL, MARKER_DONE, request_id)
            except SessionExpiredError:
                raise
            except Exception:
                self._save_failure_screenshot(page, rendicion.id)
                raise
            shot = take_screenshot(
                page, self.screens_dir, f"chubb-{rendicion.id}-{stamp(self.now())}"
            )
        logger.info("rendicion %d: chubb request sent (%d files)", rendicion.id, files)
        return SubmissionResult(
            ok=True,
            request_id=request_id,
            raw={"chubb_benefit": prestacion.benefit, "screenshot": shot.name},
        )

    def _save_failure_screenshot(self, page: Page, rendicion_id: int) -> None:
        name = f"chubb-{rendicion_id}-error-{stamp(self.now())}"
        try:
            take_screenshot(page, self.screens_dir, name)
        except (PlaywrightError, OSError):
            # Best effort: the submit error being re-raised matters more than its screenshot.
            logger.warning("rendicion %d: chubb failure screenshot not saved", rendicion_id)

    def _reconcile(self, rendicion: RendicionView) -> SubmissionResult | None:
        marker = self.ledger.get_marker(rendicion.id, PORTAL)
        if marker is None:
            return None
        if marker.request_id:
            return SubmissionResult(ok=True, request_id=marker.request_id, raw={"recuperado": True})
        raise PortalError(
            "Un envío anterior a Chubb quedó interrumpido y no se pudo confirmar. Revisa el "
            "historial de reembolsos: si la solicitud existe, registra su N° por la API; si no, "
            f"ejecuta `rendiciones clear-marker {rendicion.id} chubb` y reintenta."
        )

    def _select_beneficiary(self, page: Page, docs: Sequence[DocumentView]) -> None:
        cards = page.locator(ChubbSel.BENEFICIARY_GROUP).first.locator(ChubbSel.BENEFICIARY_CARD)
        count = cards.count()
        patient_ruts = {
            normalize_rut(d.extraction.patient_rut) for d in docs if d.extraction.patient_rut
        } - {None}
        matches = []
        for index in range(count):
            value = cards.nth(index).locator("input[type=radio]").first.get_attribute("value")
            if value and normalize_rut(value) in patient_ruts:
                matches.append(index)
        if len(matches) == 1:
            chosen = matches[0]
        elif count == 1:
            chosen = 0
        else:
            raise PortalError(
                "No se pudo determinar el beneficiario en Chubb: el RUT del paciente de los "
                "documentos no coincide con un único asegurado"
            )
        card = cards.nth(chosen)
        clickable = card.locator(".card")
        (clickable.first if clickable.count() else card).click()
        page.locator(ChubbSel.PRODUCT_SELECT).first.wait_for(state="visible")
        logger.info("chubb: beneficiary card %d of %d selected", chosen + 1, count)

    def _mat_select(self, page: Page, selector: str, wanted: str) -> None:
        page.locator(selector).first.click()
        options = page.get_by_role("option")
        options.first.wait_for(state="visible")
        index = pick_label(options.all_inner_texts(), wanted)
        if index is None:
            page.keyboard.press("Escape")
            raise PortalError(f"Chubb no ofrece la opción '{wanted}'")
        options.nth(index).click()

    def _select_benefit(self, page: Page, prestacion: ChubbPrestacion) -> None:
        self._mat_select(page, ChubbSel.PRODUCT_SELECT, prestacion.product.value)
        self._mat_select(page, ChubbSel.BENEFIT_SELECT, prestacion.benefit)

    def _fill_date(self, page: Page, fecha: date) -> None:
        field = page.locator(ChubbSel.FECHA_INPUT).first
        field.fill(chubb_date_input(fecha))
        field.press("Tab")
        if (
            not chubb_date_shown(field.input_value(), fecha)
            or field.get_attribute("aria-invalid") == "true"
        ):
            raise PortalError("Chubb no aceptó la fecha de atención")

    def _set_previously_reimbursed(self, page: Page) -> None:
        radio = page.locator(ChubbSel.PREVIO_YES).first
        button = (
            page.locator(ChubbSel.PREVIO_GROUP)
            .locator("mat-radio-button")
            .filter(has=page.locator('input[value="si"]'))
        )
        (button.first if button.count() else radio).click()
        if not radio.is_checked():
            radio.check(force=True)
        if not radio.is_checked():
            raise PortalError("No se pudo marcar 'reembolsado previamente' en Chubb")

    def _read_cards(self, page: Page) -> list[tuple[ChubbCard, Locator]]:
        locator = page.locator(ChubbSel.DOC_CARD)
        locator.first.wait_for(state="visible")
        page.wait_for_timeout(300)  # cards re-render after the reembolsado choice
        cards = []
        for index in range(locator.count()):
            card = locator.nth(index)
            lines = [line.strip() for line in card.inner_text().splitlines() if line.strip()]
            title = next((line for line in lines if norm(line) not in _CARD_NOISE), "")
            required = any(norm(line) == "obligatorio" for line in lines)
            cards.append((ChubbCard(title=title, required=required), card))
        logger.info("chubb: %d document cards found", len(cards))
        return cards

    def _prepare(
        self, assignment: dict[str, list[DocumentView]], work: Path
    ) -> dict[str, list[UploadFile]]:
        return {
            title: prepare_chubb([(d.id, d.path, d.mime) for d in docs], work)
            for title, docs in assignment.items()
        }

    def _ready_to_send(self, page: Page) -> Locator:
        send = page.locator(ChubbSel.SEND_BUTTON).first
        deadline = time.monotonic() + 20
        while not (send.is_visible() and send.is_enabled()):
            if time.monotonic() > deadline:
                raise PortalError("El formulario de Chubb no quedó válido para enviar")
            # Step 3 only opens once Chubb finishes validating the files, so keep trying.
            self._advance_to_payment(page)
            self._require_bank_account(page)
            page.wait_for_timeout(500)
        self._require_bank_account(page)
        return send

    def _advance_to_payment(self, page: Page) -> None:
        next_buttons = page.get_by_role(
            "button", name=re.compile(ChubbSel.NEXT_BUTTON_RE, re.IGNORECASE)
        )
        for index in range(next_buttons.count()):
            if next_buttons.nth(index).is_visible() and next_buttons.nth(index).is_enabled():
                next_buttons.nth(index).click()
                return
        headers = page.locator(ChubbSel.STEP_HEADER)
        if headers.count() >= 3:
            # The real portal leaves this header aria-disabled even once step 2 is valid.
            headers.nth(2).click(force=True)

    def _require_bank_account(self, page: Page) -> None:
        bank = page.locator(ChubbSel.BANK_SELECT)
        account = page.locator(ChubbSel.ACCOUNT_NUMBER)
        if (
            bank.count()
            and bank.first.is_visible()
            and (not account.count() or not (account.first.input_value() or "").strip())
        ):
            raise PortalError("Chubb pide una cuenta bancaria: regístrala primero en el portal")

    def _send(self, page: Page, send: Locator) -> Request:
        def is_refund(request: Request) -> bool:
            return request.method == "POST" and urlparse(request.url).path.endswith(
                ChubbSel.REFUND_API_PATH
            )

        refunds: list[Request] = []

        def record(request: Request) -> None:
            if is_refund(request):
                refunds.append(request)

        timeout = self.submit_timeout_s * 1000
        page.on("request", record)
        try:
            with page.expect_request(is_refund, timeout=timeout) as info:
                send.click()
                dialog = page.locator(ChubbSel.DIALOG).first
                dialog.wait_for(state="visible", timeout=timeout)
                dialog.get_by_role(
                    "button", name=re.compile(ChubbSel.CONFIRM_BUTTON_RE, re.IGNORECASE)
                ).last.click(timeout=timeout)
        except PlaywrightTimeoutError as exc:
            if refunds:
                raise PortalError(
                    "Chubb alcanzó a recibir el envío, pero la página no terminó de confirmarlo; "
                    "busca la solicitud en el historial y registra su N° antes de reintentar"
                ) from exc
            raise PortalError(
                "Chubb no envió la solicitud a tiempo (no se vio ningún envío al portal); revisa "
                "la captura del error en screenshots/ y el historial antes de reintentar"
            ) from exc
        finally:
            page.remove_listener("request", record)
        request = info.value
        response = request.response()
        if response is None or response.status >= 400:
            status = response.status if response is not None else "sin respuesta"
            raise PortalError(f"Chubb rechazó el envío (HTTP {status}); revisa el portal")
        return request

    def _request_id(self, page: Page, request: Request) -> str:
        body = request.post_data_buffer or b""
        for field in ("denuncio", "solicitud"):
            value = parse_multipart_field(body, field)
            if value:
                return value
        page.wait_for_timeout(1500)
        match = re.search(
            r"(?:denuncio|solicitud|reembolso)\D{0,15}(\d{4,})", page.inner_text("body"), re.I
        )
        if match:
            return match.group(1)
        raise PortalError("Chubb recibió la solicitud pero no se pudo leer su número")

    # --------------------------------------------------------------------- status

    def check_status(self, request_id: str) -> StatusResult:
        """Read the refund-history table (all pages) and map the Estado column."""
        return self._retry_after_login(lambda: self._check_status(request_id))

    def _check_status(self, request_id: str) -> StatusResult:
        page = self._open_history()
        found = self._find_row(page, request_id)
        if found is None:
            logger.info("chubb: request not yet in refund history")
            return StatusResult(
                state=PortalState.PENDING,
                message="Aún no aparece en el historial de Chubb (puede tardar 48 h hábiles)",
            )
        _, cells, columns = found
        estado = cells[columns[ChubbSel.COL_ESTADO] or 0]
        monto_col = columns[ChubbSel.COL_MONTO]
        monto = cells[monto_col] if monto_col is not None and monto_col < len(cells) else None
        return parse_chubb_status(estado, monto)

    def download_liquidacion(self, request_id: str, dest: Path) -> Path | None:
        """Download the liquidación PDF into ``dest`` (0600); None until paid or rejected."""
        return self._retry_after_login(lambda: self._download_liquidacion(request_id, dest))

    def _download_liquidacion(self, request_id: str, dest: Path) -> Path | None:
        page = self._open_history()
        found = self._find_row(page, request_id)
        if found is None:
            return None
        row, _, _ = found
        button = row.locator(ChubbSel.LIQUIDATION_BUTTON)
        if not button.count() or not button.first.is_visible():
            logger.info("chubb: liquidación not offered yet")
            return None
        pdf = self._capture_liquidacion(page, button.first)
        safe = re.sub(r"[^0-9A-Za-z_-]", "", request_id) or "solicitud"
        target = dest / f"liquidacion-chubb-{safe}.pdf"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pdf)
        logger.info("chubb: liquidación downloaded (%d bytes)", len(pdf))
        return target

    def _capture_liquidacion(self, page: Page, button: Locator) -> bytes:
        """Click the button and take the PDF from the download or the API's base64 reply."""
        downloads: list[Download] = []
        responses: list[Response] = []

        def on_download(download: Download) -> None:
            downloads.append(download)

        def on_response(response: Response) -> None:
            if ChubbSel.LIQUIDATION_API_FRAGMENT in response.url:
                responses.append(response)

        page.on("download", on_download)
        page.on("response", on_response)
        try:
            button.click()
            deadline = time.monotonic() + 30
            while not downloads and time.monotonic() < deadline:
                for response in responses:
                    pdf = _pdf_from_response(response)
                    if pdf is not None:
                        return pdf
                page.wait_for_timeout(200)
        finally:
            page.remove_listener("download", on_download)
            page.remove_listener("response", on_response)
        if not downloads:
            raise PortalError("Chubb no entregó el informe de liquidación a tiempo")
        path = downloads[0].path()
        pdf = Path(path).read_bytes() if path else b""
        if not pdf.startswith(b"%PDF"):
            raise PortalError("Chubb no entregó un informe de liquidación en PDF")
        return pdf

    def _open_history(self) -> Page:
        page = self._home()
        page.get_by_text(ChubbSel.HISTORY_BUTTON_TEXT).first.click()
        page.locator(ChubbSel.HISTORY_TABLE).first.wait_for(state="visible")
        self._ensure_private(page)
        return page

    def _find_row(
        self, page: Page, request_id: str
    ) -> tuple[Locator, list[str], dict[str, int | None]] | None:
        """The history row of ``request_id`` (walking every page), its cells and columns."""
        table = page.locator(ChubbSel.HISTORY_TABLE).first
        headers = table.locator("thead th").all_inner_texts()
        columns = {
            key: pick_label(headers, key)
            for key in (ChubbSel.COL_ID, ChubbSel.COL_MONTO, ChubbSel.COL_ESTADO)
        }
        if columns[ChubbSel.COL_ESTADO] is None:
            raise PortalError("La tabla de historial de Chubb cambió (no hay columna Estado)")
        wanted = request_id.strip()
        if not wanted:
            return None
        # Whole-token match: "4455" must not pick the row of request "44556677".
        pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(wanted)}(?![0-9A-Za-z])")
        rows = table.locator(ChubbSel.HISTORY_DATA_ROW)
        try:
            rows.first.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            logger.info("chubb: refund history is empty")
        for _ in range(50):
            for index in range(rows.count()):
                row = rows.nth(index)
                cells = [" ".join(c.split()) for c in row.locator("td").all_inner_texts()]
                id_col = columns[ChubbSel.COL_ID]
                haystack = (
                    cells[id_col] if id_col is not None and id_col < len(cells) else " ".join(cells)
                )
                if pattern.search(haystack):
                    return row, cells, columns
            next_button = page.locator(ChubbSel.PAGINATOR_NEXT).first
            if not (next_button.count() and next_button.is_enabled()):
                break
            first_row = rows.first.inner_text()
            next_button.click()
            self._wait_page_change(rows, first_row)
        return None

    @staticmethod
    def _wait_page_change(rows: Locator, previous_first_row: str, timeout_s: float = 10) -> None:
        """Wait until the paginator has replaced the rows shown before clicking next."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if rows.count() and rows.first.inner_text() != previous_first_row:
                return
            rows.page.wait_for_timeout(100)
        raise PortalError("El historial de Chubb no avanzó de página")
