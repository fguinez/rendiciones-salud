"""Every portal URL and DOM selector in one place (source: docs/portals.md).

When a portal changes its markup, only this module should need edits.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColmenaUrls:
    """Colmena entry points (hash-routed Angular app plus its JSON API)."""

    base: str = "https://www.colmena.cl/afiliados/"
    api: str = "https://www.colmena.cl/services/afiliados/private/"

    @property
    def login(self) -> str:
        """Page opened by ``rendiciones login colmena``."""
        return self.base

    @property
    def login_form(self) -> str:
        """Login route the app itself redirects to when there is no token."""
        return f"{self.base}#/login"

    @property
    def solicitar(self) -> str:
        """New reimbursement form."""
        return f"{self.base}#/reembolso/solicitar"

    @property
    def consultar(self) -> str:
        """Request list (status and comprobante)."""
        return f"{self.base}#/reembolso/consultar"

    @property
    def ultimas(self) -> str:
        """JSON with the user's recent requests."""
        return f"{self.api}reembolso-web/consulta/ultimas/0"

    def ultimas_folio(self, folio: str) -> str:
        """JSON for a single request."""
        return f"{self.api}reembolso-web/consulta/ultimas/{folio}"

    @property
    def report(self) -> str:
        """PDF report service behind the 'Comprobante reembolso' button."""
        return f"{self.api}report"


class ColmenaSel:
    """Colmena selectors. Buttons are ``app-boton-colmena`` divs, not ``<button>``."""

    APP_ROOT = "app-root"
    TOKEN_KEY = "coleman"  # noqa: S105 - localStorage key holding the X-Auth-Token (never read in Python)
    PASSWORD_INPUT = "input[type=password]"  # noqa: S105
    # The login component lists the server's ``glosaRetorno`` here after a failed submit.
    LOGIN_MESSAGE = "app-root div.message:visible"
    LOGIN_REJECTED_TEXT = "Usuario o clave inválido"
    PASSWORD_EXPIRED_ROUTE = "#/cambiar-clave"  # noqa: S105 - route, not a secret

    @staticmethod
    def boton(label: str) -> str:
        """Visible custom button by its ``label`` attribute."""
        return f'app-boton-colmena[label="{label}"]:visible .wrapper'

    BANK_EDIT = "div.edit-datos"
    TIPO_DROPDOWN = 'p-dropdown[formcontrolname="tipoPrestacion"]'
    SUBTYPE_DROPDOWN = 'li[formarrayname="SubTiposSelect"] p-dropdown[formcontrolname="subType"]'
    DROPDOWN_TRIGGER = ".ui-dropdown"
    DROPDOWN_ITEM = ".ui-dropdown-item:visible"
    SUBTYPE_URL_PART = "reembolso-web/sub-tipo-prestacion/"
    UPLOAD_BLOCK = ".box-adjunto"
    UPLOAD_LABEL = "label.input-label"
    FILE_INPUT = "input[type=file]"
    # The page lists a selected file here only once its form control holds it.
    ATTACHED_FILE = ".archivos-adjunto .archivo-item"
    VALIDATION_URL_PART = "validacion-documento"
    GROWL_ERROR = "p-growl .ui-growl-message-error"
    GROWL_MESSAGE = f"{GROWL_ERROR}, p-growl .ui-growl-message-warn"
    TERMS_CHECKBOX = ".box-terminos-condiciones p-checkbox[binary=true]"
    TERMS_BOX = ".ui-chkbox-box"
    TERMS_INPUT = "input[type=checkbox]"
    EXCEDENTES_BOX = "div.box-uso-excedentes"
    EXCEDENTES_YES = 'p-radioButton[formcontrolname="excedentesDecision"][value="S"]'
    RADIO_BOX = ".ui-radiobutton-box"
    CONFIRMATION = "div.detalleUsoExcedentes"
    REPORT_PDF_NAME = "DetalleReembolsoWeb"

    # Visible block labels → our upload slots (matched on normalised text).
    BLOCK_LABELS: tuple[tuple[str, str], ...] = (
        ("boleta", "boleta"),
        ("detalle", "ordenPago"),
        ("orden medica", "ordenMedica"),
    )


@dataclass(frozen=True)
class ChubbUrls:
    """Chubb entry points. Deep links to /private/* land on home, so we click from there."""

    base: str = "https://colectivos-empresas.cl.chubb.com/"

    @property
    def login(self) -> str:
        """Page opened by ``rendiciones login chubb``."""
        return f"{self.base}private/home"

    @property
    def home(self) -> str:
        """Logged-in landing page."""
        return f"{self.base}private/home"


class ChubbSel:
    """Chubb selectors (Angular Material)."""

    PRIVATE_PREFIX = "/private/"
    # Passed through for a few seconds while the SSO cookie silently restores the session.
    LOGIN_PATH = "/login"
    AUTH_HOST_PARTS = ("auth.chubb.com", "b2clogin", "login.microsoftonline")
    NO_PERMISSION_TEXT = "No tiene permiso"
    REFUND_TILE_TEXT = "Solicitar un reembolso"
    HISTORY_BUTTON_TEXT = "Historial de Reembolsos"

    BENEFICIARY_GROUP = 'mat-radio-group[formcontrolname="personalIdBeneficiary"]'
    # Each option wraps the radio input and, inside its label, the app-user-select card.
    BENEFICIARY_CARD = "mat-radio-button"
    PRODUCT_SELECT = 'mat-select[formcontrolname="Product"]'
    BENEFIT_SELECT = 'mat-select[formcontrolname="Benefit"]'
    FECHA_INPUT = 'input[formcontrolname="FechaAtencion"]'
    PREVIO_GROUP = 'mat-radio-group[formcontrolname="reembolsadoPreviamente"]'
    PREVIO_YES = 'mat-radio-group[formcontrolname="reembolsadoPreviamente"] input[value="si"]'
    DOC_CARD = "app-document-section .doc-grid .doc-card"
    FILE_INPUT = "input[type=file]"
    BANK_SELECT = 'mat-select[formcontrolname="Bank"]'
    ACCOUNT_NUMBER = '[formcontrolname="AccountNumber"]'
    STEP_HEADER = "mat-step-header"
    NEXT_BUTTON_RE = r"^\s*(Siguiente|Continuar)\s*$"
    SEND_BUTTON = "button.send-request"
    DIALOG = "mat-dialog-container"
    CONFIRM_BUTTON_RE = r"^\s*(Continuar|Confirmar|Aceptar|Enviar|Sí|Si)\b"
    REFUND_API_PATH = "/api/refund"

    HISTORY_TABLE = "table.table"
    # The table first renders a single "Sin Movimientos" cell, then the real rows.
    HISTORY_DATA_ROW = "tbody tr:has(td:nth-child(2))"
    PAGINATOR_NEXT = (
        "button.mat-mdc-paginator-navigation-next, button.mat-paginator-navigation-next"
    )
    COL_ID = "Nombre o ID de reembolso"
    COL_MONTO = "Monto"
    COL_ESTADO = "Estado"
    # Shown on Aceptada, Pagado and Rechazada rows; it fetches the liquidación PDF.
    LIQUIDATION_BUTTON = 'button[mattooltip^="Click para descargar informe de liquidac"]'
    LIQUIDATION_API_FRAGMENT = "/api/charles-taylor/liquidation-document/"
