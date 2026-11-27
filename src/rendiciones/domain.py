"""Domain model: document types, prestaciones, rendición states and transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class DocType(StrEnum):
    """Kind of document recognised in an upload."""

    BOLETA_ELECTRONICA = "boleta_electronica"
    BOLETA_HONORARIOS = "boleta_honorarios"
    FACTURA = "factura"
    ORDEN_MEDICA = "orden_medica"
    RECETA = "receta"
    PROGRAMA_MEDICO = "programa_medico"
    INFORME_EXAMEN = "informe_examen"
    COMPROBANTE_BONIFICACION = "comprobante_bonificacion"
    DOCUMENTO_CHUBB = "documento_chubb"
    OTRO = "otro"

    @property
    def is_payment(self) -> bool:
        """True for documents that prove a payment (boletas and facturas)."""
        return self in PAYMENT_TYPES


PAYMENT_TYPES = frozenset({DocType.BOLETA_ELECTRONICA, DocType.BOLETA_HONORARIOS, DocType.FACTURA})
SUPPORT_TYPES = frozenset(
    {DocType.ORDEN_MEDICA, DocType.RECETA, DocType.PROGRAMA_MEDICO, DocType.INFORME_EXAMEN}
)


class TipoPrestacion(StrEnum):
    """Suggested benefit category for a rendición."""

    CONSULTA_MEDICA = "consulta_medica"
    EXAMENES_LABORATORIO = "examenes_laboratorio"
    IMAGENOLOGIA = "imagenologia"
    PROCEDIMIENTO = "procedimiento"
    MEDICAMENTOS = "medicamentos"
    DENTAL = "dental"
    OTROS_PROFESIONALES = "otros_profesionales"
    LENTES = "lentes"
    HOSPITALIZACION = "hospitalizacion"
    OTRO = "otro"


class Status(StrEnum):
    """Lifecycle state of a rendición."""

    BORRADOR = "borrador"
    LISTA = "lista"
    COLMENA_ENVIADA = "colmena_enviada"
    COLMENA_APROBADA = "colmena_aprobada"
    COLMENA_RECHAZADA = "colmena_rechazada"
    CHUBB_ENVIADA = "chubb_enviada"
    CHUBB_APROBADA = "chubb_aprobada"
    CHUBB_RECHAZADA = "chubb_rechazada"
    ERROR = "error"


class DocRole(StrEnum):
    """Why a document is attached to a rendición."""

    SOPORTE = "soporte"
    BONIFICACION_COLMENA = "bonificacion_colmena"
    RESPUESTA_CHUBB = "respuesta_chubb"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.BORRADOR: frozenset({Status.LISTA}),
    Status.LISTA: frozenset({Status.BORRADOR, Status.COLMENA_ENVIADA}),
    Status.COLMENA_ENVIADA: frozenset({Status.COLMENA_APROBADA, Status.COLMENA_RECHAZADA}),
    Status.COLMENA_APROBADA: frozenset({Status.CHUBB_ENVIADA}),
    Status.COLMENA_RECHAZADA: frozenset({Status.BORRADOR}),
    Status.CHUBB_ENVIADA: frozenset({Status.CHUBB_APROBADA, Status.CHUBB_RECHAZADA}),
    Status.CHUBB_APROBADA: frozenset(),
    Status.CHUBB_RECHAZADA: frozenset({Status.COLMENA_APROBADA}),
    Status.ERROR: frozenset(),
}

EDITABLE_STATUSES = frozenset({Status.BORRADOR, Status.LISTA})
EDITABLE_DOCUMENT_FIELDS = frozenset(
    {
        "doc_type",
        "fecha",
        "monto",
        "provider_name",
        "provider_rut",
        "folio",
        "tipo_prestacion",
        "patient_rut",
    }
)


class InvalidTransitionError(ValueError):
    """Raised when a status change is not allowed by the state machine."""

    def __init__(self, current: Status, target: Status) -> None:
        super().__init__(f"Transición no permitida: {current} → {target}")
        self.current = current
        self.target = target


def check_transition(current: Status, target: Status, previous: Status | None = None) -> None:
    """Validate a status change.

    Any non-error state may go to ``error``. From ``error`` the rendición may only
    return to the state it was in before failing (``previous``) or to ``borrador``.
    """
    if target == Status.ERROR:
        if current == Status.ERROR:
            raise InvalidTransitionError(current, target)
        return
    if current == Status.ERROR:
        if target == Status.BORRADOR or (previous is not None and target == previous):
            return
        raise InvalidTransitionError(current, target)
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)


@dataclass(frozen=True)
class Extraction:
    """Fields recognised in one document."""

    doc_type: DocType = DocType.OTRO
    provider_name: str | None = None
    provider_rut: str | None = None
    patient_rut: str | None = None
    fecha: date | None = None
    monto: int | None = None
    folio: str | None = None
    tipo_prestacion: TipoPrestacion | None = None
    confidence: float = 0.0
    classifier: str = "rules"
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentView:
    """Read model of a stored document, with its absolute path on disk."""

    id: int
    rendicion_id: int | None
    sha256: str
    original_name: str
    folder: str
    path: Path
    mime: str
    size: int
    role: DocRole
    extraction: Extraction
    created_at: datetime
    edited_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DocumentEdit:
    """User corrections to a document; only fields named in ``sent`` apply, None clears."""

    doc_type: DocType | None = None
    fecha: date | None = None
    monto: int | None = None
    provider_name: str | None = None
    provider_rut: str | None = None
    folio: str | None = None
    tipo_prestacion: TipoPrestacion | None = None
    patient_rut: str | None = None
    sent: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Reject names in ``sent`` that are not editable fields."""
        unknown = self.sent - EDITABLE_DOCUMENT_FIELDS
        if unknown:
            raise ValueError(f"campos no editables: {sorted(unknown)}")

    @classmethod
    def build(cls, **values: Any) -> DocumentEdit:
        """Build an edit whose sent fields are exactly the given keyword arguments."""
        return cls(**values, sent=frozenset(values))


@dataclass(frozen=True)
class BankPaymentView:
    """What the bank check found for one insurer's reimbursement of a rendición.

    ``state`` is ``abonado`` (the transfer arrived) or ``no_llego`` (the wait ran out
    without it). ``movement_key`` identifies the transfer, which may pay several
    rendiciones at once (``movement_amount`` is then larger than ``monto``).
    """

    account: str
    state: str
    monto: int
    movement_key: str | None
    operation_id: str | None
    movement_amount: int | None
    movement_date: date | None
    updated_at: datetime


@dataclass(frozen=True)
class RendicionView:
    """Read model of a rendición handed to portal clients and the API."""

    id: int
    status: Status
    tipo_prestacion: TipoPrestacion | None
    fecha_atencion: date | None
    monto: int | None
    grouping_confidence: float
    grouping_reason: str
    fields_locked: bool
    error_message: str | None
    status_before_error: Status | None
    colmena_request_id: str | None
    colmena_result: dict[str, Any] | None
    colmena_monto_bonificado: int | None
    bonificacion_document_id: int | None
    chubb_request_id: str | None
    chubb_result: dict[str, Any] | None
    chubb_monto_reembolsado: int | None
    created_at: datetime
    updated_at: datetime
    colmena_submitted_at: datetime | None
    colmena_resolved_at: datetime | None
    chubb_submitted_at: datetime | None
    chubb_resolved_at: datetime | None
    documents: tuple[DocumentView, ...] = ()
    bank_payments: dict[str, BankPaymentView] = field(default_factory=dict)

    @property
    def support_documents(self) -> tuple[DocumentView, ...]:
        """Documents uploaded by the user (excludes portal receipts)."""
        return tuple(d for d in self.documents if d.role == DocRole.SOPORTE)

    @property
    def bonificacion_document(self) -> DocumentView | None:
        """The main Colmena comprobante of the current request, when attached."""
        return next(iter(self.bonificacion_documents), None)

    @property
    def bonificacion_documents(self) -> tuple[DocumentView, ...]:
        """Colmena comprobantes of the current request (one per reembolso), main one first.

        Comprobantes of an earlier request, before the rendición was sent again, are left out;
        the main one (``bonificacion_document_id``, cleared on each Colmena submission) stays.
        """
        docs = [d for d in self.documents if d.role == DocRole.BONIFICACION_COLMENA]
        if self.colmena_submitted_at is not None:
            submitted = self.colmena_submitted_at
            docs = [
                d
                for d in docs
                if d.created_at >= submitted or d.id == self.bonificacion_document_id
            ]
        return tuple(sorted(docs, key=lambda d: (d.id != self.bonificacion_document_id, d.id)))

    @property
    def chubb_response_document(self) -> DocumentView | None:
        """Chubb's liquidación for the current request (older ones answer earlier requests)."""
        responses = [d for d in self.documents if d.role == DocRole.RESPUESTA_CHUBB]
        if self.chubb_submitted_at is not None:
            responses = [d for d in responses if d.created_at >= self.chubb_submitted_at]
        return max(responses, key=lambda d: d.created_at, default=None)


@dataclass(frozen=True)
class Event:
    """One entry of a rendición's history."""

    id: int
    rendicion_id: int
    at: datetime
    kind: str
    from_status: Status | None
    to_status: Status | None
    detail: dict[str, Any]
