"""Pure mapping tables and parsers for the portals (no browser code, fully unit-tested).

Everything that translates our domain (``TipoPrestacion``, ``DocType``, dates, amounts)
into portal vocabulary, or portal data back into ``StatusResult``, lives here.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from rendiciones.automation.base import PortalError, PortalState, StatusResult
from rendiciones.domain import DocType, DocumentView, TipoPrestacion

logger = logging.getLogger(__name__)


def norm(text: str) -> str:
    """Casefold, strip accents and collapse whitespace (for label matching)."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).strip().casefold()


def pick_label(options: Sequence[str], wanted: str) -> int | None:
    """Index of the option matching ``wanted``: exact (normalised), then prefix, then contains."""
    target = norm(wanted)
    normalised = [norm(o) for o in options]
    passes: tuple[Callable[[str], bool], ...] = (
        lambda o: o == target,
        lambda o: o.startswith(target),
        lambda o: target in o,
    )
    for predicate in passes:
        for index, option in enumerate(normalised):
            if option and predicate(option):
                return index
    return None


# --------------------------------------------------------------------------- Colmena


class Req(StrEnum):
    """Colmena document requirement (``glosaRetorno``)."""

    OBLIGATORIO = "O"
    OPCIONAL = "P"
    NO_SOLICITADO = "-"


class ColmenaSlot(StrEnum):
    """Upload blocks of the Colmena form."""

    BOLETA = "boleta"
    DETALLE = "ordenPago"
    ORDEN = "ordenMedica"


SLOT_LABELS = {
    ColmenaSlot.BOLETA: "Boleta o factura",
    ColmenaSlot.DETALLE: "Detalle de Prestación",
    ColmenaSlot.ORDEN: "Orden Médica",
}


@dataclass(frozen=True)
class ColmenaPrestacion:
    """Path through Colmena's cascading dropdowns plus the documents it requires."""

    tipo_label: str
    tipo_id: int
    sub_labels: tuple[str, ...]
    leaf_id: int
    boleta: Req
    detalle: Req
    orden: Req
    note: str = ""  # non-empty when the mapping is a best guess

    def requirement(self, slot: ColmenaSlot) -> Req:
        """Requirement for one upload block."""
        return {
            ColmenaSlot.BOLETA: self.boleta,
            ColmenaSlot.DETALLE: self.detalle,
            ColmenaSlot.ORDEN: self.orden,
        }[slot]


OB, OP, NS = Req.OBLIGATORIO, Req.OPCIONAL, Req.NO_SOLICITADO
_CONSULTAS = "Consultas (médicas, psicológicas, psiquiátricas)"
_EXAMENES = "Exámenes de laboratorio, Imágenes y Procedimientos"
_OTRAS = "Otras Atenciones"
_KINE_FONO_NUTRI = "Kinesiología, fonoaudiología o Nutricionista"

COLMENA_CONSULTA = ColmenaPrestacion(_CONSULTAS, 3448, (), 3448, OB, OP, NS)
COLMENA_KINE = ColmenaPrestacion(_OTRAS, 3450, (_KINE_FONO_NUTRI, "Kinesiología"), 3488, OB, OB, OB)
COLMENA_FONO = ColmenaPrestacion(
    _OTRAS, 3450, (_KINE_FONO_NUTRI, "Fonoaudiología"), 3489, OB, OB, OB
)
COLMENA_NUTRI = ColmenaPrestacion(
    _OTRAS, 3450, (_KINE_FONO_NUTRI, "Nutricionista"), 3490, OB, OP, OP
)
COLMENA_TERAPIA_OCUPACIONAL = ColmenaPrestacion(
    _OTRAS, 3450, ("Terapia Ocupacional",), 3482, OB, OB, OB
)

# The single table mapping our prestaciones to Colmena (docs/portals.md §1.1).
# ``None`` means Colmena has no web reimbursement for it.
COLMENA_PRESTACIONES: dict[TipoPrestacion, ColmenaPrestacion | None] = {
    TipoPrestacion.CONSULTA_MEDICA: COLMENA_CONSULTA,
    TipoPrestacion.EXAMENES_LABORATORIO: ColmenaPrestacion(
        _EXAMENES, 3449, ("Laboratorio", "Otros Exámenes"), 3470, OB, OB, OB
    ),
    TipoPrestacion.IMAGENOLOGIA: ColmenaPrestacion(
        _EXAMENES, 3449, ("Imágenes",), 3454, OB, OB, OB
    ),
    TipoPrestacion.PROCEDIMIENTO: ColmenaPrestacion(
        _EXAMENES,
        3449,
        ("Procedimiento", "Otro procedimiento"),
        3457,
        OB,
        OB,
        OB,
        note="procedimiento → 'Otro procedimiento' (Colonoscopía/Endoscopía no se infieren)",
    ),
    TipoPrestacion.OTROS_PROFESIONALES: ColmenaPrestacion(
        COLMENA_KINE.tipo_label,
        COLMENA_KINE.tipo_id,
        COLMENA_KINE.sub_labels,
        COLMENA_KINE.leaf_id,
        OB,
        OB,
        OB,
        note="otros_profesionales → Kinesiología salvo que el prestador indique otra cosa",
    ),
    TipoPrestacion.LENTES: ColmenaPrestacion(_OTRAS, 3450, ("Lentes Ópticos",), 3480, OB, OB, OB),
    TipoPrestacion.MEDICAMENTOS: None,
    TipoPrestacion.DENTAL: None,
    TipoPrestacion.HOSPITALIZACION: None,
    TipoPrestacion.OTRO: None,
}

COLMENA_UNSUPPORTED_REASON: dict[TipoPrestacion | None, str] = {
    TipoPrestacion.MEDICAMENTOS: "Colmena no tiene reembolso web de medicamentos (salvo GES)",
    TipoPrestacion.DENTAL: "Colmena no tiene categoría dental en el reembolso web",
    TipoPrestacion.HOSPITALIZACION: "Colmena exige presentar cuentas hospitalarias en sucursal",
    TipoPrestacion.OTRO: "Define un tipo de prestación concreto antes de enviar",
    None: "Define el tipo de prestación antes de enviar",
}

# Provider-name / service-description keywords that refine ``otros_profesionales``.
_REFINEMENTS: tuple[tuple[str, ColmenaPrestacion], ...] = (
    ("fonoaudiolog", COLMENA_FONO),
    ("nutricion", COLMENA_NUTRI),
    ("terapia ocupacional", COLMENA_TERAPIA_OCUPACIONAL),
    ("kinesiolog", COLMENA_KINE),
    ("psicolog", COLMENA_CONSULTA),
    ("psicoterap", COLMENA_CONSULTA),
    ("psiquiatr", COLMENA_CONSULTA),
)


def colmena_hints(documents: Iterable[DocumentView]) -> list[str]:
    """Provider names, plus payment documents' glosas, used to refine the prestación."""
    hints: list[str] = []
    for document in documents:
        extraction = document.extraction
        glosa = extraction.extras.get("descripcion") if extraction.doc_type.is_payment else None
        for hint in (extraction.provider_name, glosa):
            if isinstance(hint, str) and hint:
                hints.append(hint)
    return hints


def colmena_prestacion(
    tipo: TipoPrestacion | None, hints: Iterable[str] = (), rendicion_id: int | None = None
) -> ColmenaPrestacion:
    """Colmena path for ``tipo``; ``hints`` (provider names, glosas) refine ambiguous cases.

    Raises ``PortalError`` when Colmena has no web equivalent. Best-guess choices are
    logged by id only.
    """
    base = COLMENA_PRESTACIONES.get(tipo) if tipo is not None else None
    if base is None:
        raise PortalError(
            COLMENA_UNSUPPORTED_REASON.get(tipo, "Tipo de prestación sin equivalente en Colmena")
        )
    if tipo == TipoPrestacion.OTROS_PROFESIONALES:
        text = " ".join(norm(h) for h in hints)
        for keyword, refined in _REFINEMENTS:
            if keyword in text:
                logger.info(
                    "rendicion %s: colmena prestacion %d chosen by keyword",
                    rendicion_id,
                    refined.leaf_id,
                )
                return refined
    if base.note:
        logger.info(
            "rendicion %s: colmena prestacion %d is a best-guess mapping for %s",
            rendicion_id,
            base.leaf_id,
            tipo,
        )
    return base


_SLOT_TYPES: dict[ColmenaSlot, tuple[DocType, ...]] = {
    ColmenaSlot.BOLETA: (DocType.BOLETA_ELECTRONICA, DocType.BOLETA_HONORARIOS, DocType.FACTURA),
    ColmenaSlot.ORDEN: (DocType.ORDEN_MEDICA, DocType.RECETA),
    ColmenaSlot.DETALLE: (DocType.PROGRAMA_MEDICO, DocType.OTRO),
}
_NEVER_TO_COLMENA = frozenset(
    {DocType.COMPROBANTE_BONIFICACION, DocType.DOCUMENTO_CHUBB, DocType.INFORME_EXAMEN}
)


def assign_colmena_documents(
    prestacion: ColmenaPrestacion, docs: Sequence[DocumentView]
) -> dict[ColmenaSlot, list[DocumentView]]:
    """Route support documents into Colmena's upload blocks and check obligatorios.

    Boleta and orden médica blocks take one file each; extra payment documents go to the
    multi-file "Detalle de Prestación" block. Raises ``PortalError`` if an obligatorio
    block would stay empty.
    """
    slots: dict[ColmenaSlot, list[DocumentView]] = {s: [] for s in ColmenaSlot}
    for doc in docs:
        doc_type = doc.extraction.doc_type
        if doc_type in _NEVER_TO_COLMENA:
            continue
        for slot, types in _SLOT_TYPES.items():
            if doc_type in types:
                slots[slot].append(doc)
                break
    # Single-file blocks: keep the first, prefer orden médica over receta.
    slots[ColmenaSlot.ORDEN].sort(key=lambda d: d.extraction.doc_type != DocType.ORDEN_MEDICA)
    if len(slots[ColmenaSlot.BOLETA]) > 1:
        slots[ColmenaSlot.DETALLE].extend(slots[ColmenaSlot.BOLETA][1:])
        slots[ColmenaSlot.BOLETA] = slots[ColmenaSlot.BOLETA][:1]
    slots[ColmenaSlot.ORDEN] = slots[ColmenaSlot.ORDEN][:1]
    for slot in ColmenaSlot:
        req = prestacion.requirement(slot)
        if req == Req.NO_SOLICITADO:
            slots[slot] = []
        elif req == Req.OBLIGATORIO and not slots[slot]:
            raise PortalError(f"Falta documento obligatorio para Colmena: {SLOT_LABELS[slot]}")
    return slots


class ColmenaStatus(StrEnum):
    """Colmena request states (``glsEstado``)."""

    INGRESADA = "ingresada"
    EN_REVISION = "en revision"
    POR_CORREGIR = "por corregir"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"


_COLMENA_ESTADO_CODES = {6: ColmenaStatus.APROBADA, 7: ColmenaStatus.RECHAZADA}


def parse_amount(value: Any) -> int | None:
    """Parse ``12345``, ``"12.345"``, ``"$ 12.345"`` or ``"12345.0"`` into whole pesos."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+\.\d{1,2}", text):  # decimal point, not thousands separator
        return round(float(text))
    digits = re.sub(r"[^\d]", "", text.split(",")[0])
    return int(digits) if digits else None


def parse_portal_date(value: Any) -> date | None:
    """Parse dates as the portals return them (dd/mm/yyyy, dd-mm-yyyy, ISO, epoch ms)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC).date()
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        return date(year, month, day)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return date(year, month, day)
    return None


def colmena_state(item: Mapping[str, Any]) -> ColmenaStatus | None:
    """Normalised state of one entry of ``consulta/ultimas``."""
    label = norm(str(item.get("glsEstado") or ""))
    for status in ColmenaStatus:
        if label.startswith(status.value):
            return status
    code = item.get("estado")
    if isinstance(code, int | str) and str(code).isdigit():
        return _COLMENA_ESTADO_CODES.get(int(code))
    return None


def parse_colmena_status(item: Mapping[str, Any]) -> StatusResult:
    """Translate one ``consulta/ultimas`` entry into a ``StatusResult``.

    Only non-personal fields are copied into ``raw`` (it ends up in the DB and the UI).
    """
    state = colmena_state(item)
    raw = {"estado": item.get("estado"), "glsEstado": item.get("glsEstado")}
    if state == ColmenaStatus.APROBADA:
        return StatusResult(
            state=PortalState.APPROVED,
            monto=parse_amount(item.get("solMonto")),
            raw={**raw, "fechaPago": item.get("solFechaPago")},
        )
    if state == ColmenaStatus.RECHAZADA:
        reason = item.get("solObservacionesCliente") or item.get("docMotivo") or None
        return StatusResult(
            state=PortalState.REJECTED,
            message=str(reason)[:500] if reason else "Rechazada por Colmena",
            raw=raw,
        )
    message = None
    if state == ColmenaStatus.POR_CORREGIR:
        message = "Colmena pide corregir documentos (hazlo en el portal)"
    elif state is None:
        logger.warning("colmena: unknown estado %r treated as pending", item.get("estado"))
    return StatusResult(state=PortalState.PENDING, message=message, raw=raw)


def find_colmena_request(
    items: Iterable[Mapping[str, Any]], request_id: str
) -> Mapping[str, Any] | None:
    """Entry with ``solIdn == request_id``."""
    for item in items:
        if str(item.get("solIdn", "")).strip() == str(request_id).strip():
            return item
    return None


@dataclass(frozen=True)
class ColmenaComprobante:
    """One comprobante to ask the report service for."""

    folio: int
    reembolso: bool  # ``codDocTipo`` 1; otherwise Colmena files it as an "otro egreso"


def colmena_comprobantes(body: Any) -> list[ColmenaComprobante]:
    """Comprobantes listed by ``reembolsos/`` (one per reembolso of the solicitud)."""
    items = body if isinstance(body, list) else []
    result: list[ColmenaComprobante] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        folio = str(item.get("folio") or "").strip()
        if not folio.isdigit():
            continue
        result.append(
            ColmenaComprobante(folio=int(folio), reembolso=str(item.get("codDocTipo")) == "1")
        )
    return result


def colmena_duplicates(
    items: Iterable[Mapping[str, Any]],
    *,
    monto: int | None,
    since: date | None,
    known_ids: set[str],
) -> list[str]:
    """Request ids that look like an earlier submission of the same rendición.

    Same amount, requested on/after ``since`` (fecha de atención or marker date), not
    rejected and not already linked to another rendición.
    """
    if monto is None:
        return []
    found = []
    for item in items:
        request_id = str(item.get("solIdn", "")).strip()
        if not request_id or request_id in known_ids:
            continue
        if parse_amount(item.get("solMonto")) != monto:
            continue
        requested = parse_portal_date(item.get("solFecha"))
        if since is not None and requested is not None and requested < since:
            continue
        if colmena_state(item) == ColmenaStatus.RECHAZADA:
            continue
        found.append(request_id)
    return found


# ----------------------------------------------------------------------------- Chubb

CHUBB_MAX_AGE_DAYS = 60


class ChubbProduct(StrEnum):
    """Products seen in the Chubb form (labels depend on the user's policies)."""

    SALUD = "Salud"
    DENTAL = "Colectivo Dental"


@dataclass(frozen=True)
class ChubbPrestacion:
    """Product and benefit to pick in the Chubb form."""

    product: ChubbProduct
    benefit: str
    note: str = ""


_CONSULTA_EXAMENES = "Consulta médica y exámenes"

# The single table mapping our prestaciones to Chubb (docs/portals.md §2.1).
CHUBB_PRESTACIONES: dict[TipoPrestacion, ChubbPrestacion] = {
    TipoPrestacion.CONSULTA_MEDICA: ChubbPrestacion(ChubbProduct.SALUD, _CONSULTA_EXAMENES),
    TipoPrestacion.EXAMENES_LABORATORIO: ChubbPrestacion(ChubbProduct.SALUD, _CONSULTA_EXAMENES),
    TipoPrestacion.IMAGENOLOGIA: ChubbPrestacion(ChubbProduct.SALUD, _CONSULTA_EXAMENES),
    TipoPrestacion.PROCEDIMIENTO: ChubbPrestacion(
        ChubbProduct.SALUD, _CONSULTA_EXAMENES, note="procedimiento ambulatorio"
    ),
    TipoPrestacion.OTROS_PROFESIONALES: ChubbPrestacion(
        ChubbProduct.SALUD, _CONSULTA_EXAMENES, note="otros profesionales"
    ),
    TipoPrestacion.MEDICAMENTOS: ChubbPrestacion(ChubbProduct.SALUD, "Medicamentos"),
    TipoPrestacion.LENTES: ChubbPrestacion(ChubbProduct.SALUD, "Lentes y Marcos"),
    TipoPrestacion.HOSPITALIZACION: ChubbPrestacion(
        ChubbProduct.SALUD, "Cirugías y hospitalizaciones"
    ),
    TipoPrestacion.DENTAL: ChubbPrestacion(ChubbProduct.DENTAL, "Reembolso de gastos dentales"),
    TipoPrestacion.OTRO: ChubbPrestacion(
        ChubbProduct.SALUD, _CONSULTA_EXAMENES, note="tipo 'otro'"
    ),
}


def chubb_prestacion(
    tipo: TipoPrestacion | None, rendicion_id: int | None = None
) -> ChubbPrestacion:
    """Chubb product/benefit for ``tipo`` (best guesses logged by id)."""
    if tipo is None:
        raise PortalError("Define el tipo de prestación antes de enviar a Chubb")
    mapping = CHUBB_PRESTACIONES[tipo]
    if mapping.note:
        logger.info(
            "rendicion %s: chubb benefit for %s is a best-guess mapping", rendicion_id, tipo
        )
    return mapping


def chubb_date_input(value: date) -> str:
    """Text typed into Chubb's datepicker (the native adapter parses MM/DD/YYYY)."""
    return value.strftime("%m/%d/%Y")


def chubb_date_shown(shown: str, value: date) -> bool:
    """Whether Chubb's datepicker shows ``value`` (D/M/YYYY, with or without leading zeros)."""
    parts = shown.strip().split("/")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return False
    day, month, year = (int(part) for part in parts)
    return (day, month, year) == (value.day, value.month, value.year)


def check_chubb_date(value: date | None, today: date) -> date:
    """Validate the fecha de atención against Chubb's window (today-60 .. today)."""
    if value is None:
        raise PortalError("Falta la fecha de atención (obligatoria en Chubb)")
    if value > today:
        raise PortalError("La fecha de atención está en el futuro")
    if value < today - timedelta(days=CHUBB_MAX_AGE_DAYS):
        raise PortalError(
            f"La fecha de atención tiene más de {CHUBB_MAX_AGE_DAYS} días: "
            "Chubb ya no acepta el reembolso web"
        )
    return value


@dataclass(frozen=True)
class ChubbCard:
    """A document card of the Chubb form."""

    title: str
    required: bool


def _card_kind(title: str) -> str:
    t = norm(title)
    if "multiples documentos" in t:
        return "all"
    if "bono" in t or "bonificacion" in t:
        return "bono"
    if "receta" in t:
        return "receta"
    if "orden medica" in t or "derivacion" in t:
        return "orden"
    if "detalle de cristales" in t or "detalle atencion" in t or "prefactura" in t:
        return "detalle"
    if "boleta" in t or "factura" in t:
        return "pago"
    if t.startswith("otros"):
        return "otros"
    return "unknown"


_KIND_TYPES: dict[str, frozenset[DocType]] = {
    "pago": frozenset({DocType.BOLETA_ELECTRONICA, DocType.BOLETA_HONORARIOS, DocType.FACTURA}),
    "orden": frozenset({DocType.ORDEN_MEDICA}),
    "receta": frozenset({DocType.RECETA}),
    "detalle": frozenset({DocType.PROGRAMA_MEDICO}),
}


def assign_chubb_documents(
    cards: Sequence[ChubbCard],
    support: Sequence[DocumentView],
    bonificaciones: Sequence[DocumentView],
) -> dict[str, list[DocumentView]]:
    """Map documents onto Chubb document cards (keyed by card title).

    The Colmena comprobantes go to the "Bono" card; each support document goes to the
    first card of its kind, leftovers (a previous Chubb liquidación included) to "Otros".
    Raises ``PortalError`` listing any obligatorio card that would stay empty.
    """
    by_kind: dict[str, str] = {}
    for card in cards:
        by_kind.setdefault(_card_kind(card.title), card.title)
    result: dict[str, list[DocumentView]] = {card.title: [] for card in cards}
    if "all" in by_kind:
        result[by_kind["all"]] = [*bonificaciones, *support]
        placed = True
    else:
        placed = False
    if not placed:
        bono_card = by_kind.get("bono") or by_kind.get("otros")
        if bono_card is None:
            raise PortalError("El formulario de Chubb no tiene dónde adjuntar el bono Isapre")
        result[bono_card].extend(bonificaciones)
        leftovers: list[DocumentView] = []
        for doc in support:
            doc_type = doc.extraction.doc_type
            if doc_type == DocType.COMPROBANTE_BONIFICACION:
                continue
            target = next(
                (
                    by_kind[k]
                    for k, types in _KIND_TYPES.items()
                    if doc_type in types and k in by_kind
                ),
                None,
            )
            if target is None:
                leftovers.append(doc)
            else:
                result[target].append(doc)
        # Lentes: the óptica's factura usually itemises cristales/marcos.
        detalle = by_kind.get("detalle")
        pago = by_kind.get("pago")
        if detalle and not result[detalle] and pago and result[pago]:
            result[detalle].append(result[pago][0])
        if leftovers:
            otros = by_kind.get("otros")
            if otros is None:
                raise PortalError("Chubb no tiene tarjeta 'Otros' para documentos adicionales")
            result[otros].extend(leftovers)
    missing = [c.title for c in cards if c.required and not result[c.title]]
    if missing:
        raise PortalError("Faltan documentos obligatorios para Chubb: " + ", ".join(missing))
    return {title: docs for title, docs in result.items() if docs}


class ChubbStatus(StrEnum):
    """Labels shown in the Estado column of the refund history."""

    PAGADO = "pagado"
    RECHAZADA = "rechazada"
    EN_PROCESO = "en proceso"


def parse_chubb_status(estado: str, monto: str | None = None) -> StatusResult:
    """Translate a refund-history row into a ``StatusResult``."""
    label = norm(estado)
    raw = {"estado": estado}
    if label.startswith(ChubbStatus.PAGADO) or label.startswith("aceptada"):
        return StatusResult(state=PortalState.APPROVED, monto=parse_amount(monto), raw=raw)
    if label.startswith(ChubbStatus.RECHAZADA):
        return StatusResult(state=PortalState.REJECTED, message="Rechazada por Chubb", raw=raw)
    if not (label.startswith(ChubbStatus.EN_PROCESO) or label.startswith("pendiente")):
        logger.warning("chubb: unknown estado treated as pending")
    return StatusResult(state=PortalState.PENDING, raw=raw)


def parse_multipart_field(body: bytes, name: str) -> str | None:
    """Value of a text field in a multipart/form-data body (used for Chubb's denuncio)."""
    pattern = rb'name="' + re.escape(name.encode()) + rb'"\r?\n(?:[^\r\n]+\r?\n)*\r?\n([^\r\n]*)'
    match = re.search(pattern, body)
    if match is None:
        return None
    value = match.group(1).decode("utf-8", "replace").strip()
    return value or None
