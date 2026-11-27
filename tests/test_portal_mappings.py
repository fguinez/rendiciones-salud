"""Unit tests for portal mappings, parsers, size limits and the idempotency guard."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from PIL import Image

from rendiciones.automation.base import (
    PortalError,
    PortalState,
    SessionExpiredError,
    StatusResult,
    SubmissionResult,
)
from rendiciones.automation.browser.files import (
    CHUBB_MAX_FILE,
    COLMENA_MAX_TOTAL,
    prepare_chubb,
    prepare_colmena,
)
from rendiciones.automation.browser.mappings import (
    CHUBB_PRESTACIONES,
    COLMENA_PRESTACIONES,
    ChubbCard,
    ChubbProduct,
    ColmenaComprobante,
    ColmenaSlot,
    assign_chubb_documents,
    assign_colmena_documents,
    check_chubb_date,
    chubb_date_input,
    chubb_date_shown,
    chubb_prestacion,
    colmena_comprobantes,
    colmena_duplicates,
    colmena_hints,
    colmena_prestacion,
    parse_amount,
    parse_chubb_status,
    parse_colmena_status,
    parse_multipart_field,
    parse_portal_date,
    pick_label,
)
from rendiciones.automation.fake import FakeColmenaClient, FakePortalClient
from rendiciones.automation.loader import PLAYWRIGHT_SPEC, resolve_spec
from rendiciones.domain import (
    DocRole,
    DocType,
    DocumentView,
    Extraction,
    RendicionView,
    Status,
    TipoPrestacion,
)
from rendiciones.pipeline import Pipeline, Portal, record_submission
from rendiciones.services import RendicionService


def doc(doc_id: int, doc_type: DocType, role: DocRole = DocRole.SOPORTE) -> DocumentView:
    """Synthetic document view (no file needed)."""
    return DocumentView(
        id=doc_id,
        rendicion_id=1,
        sha256=f"{doc_id:064d}",
        original_name=f"{doc_id}.pdf",
        folder="",
        path=Path(f"/nonexistent/{doc_id}.pdf"),
        mime="application/pdf",
        size=100,
        role=role,
        extraction=Extraction(doc_type=doc_type),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ------------------------------------------------------------------- prestaciones


def test_every_tipo_has_a_mapping_entry() -> None:
    """Both tables cover every TipoPrestacion (None = explicitly unsupported)."""
    assert set(COLMENA_PRESTACIONES) == set(TipoPrestacion)
    assert set(CHUBB_PRESTACIONES) == set(TipoPrestacion)


@pytest.mark.parametrize(
    ("tipo", "leaf", "subs"),
    [
        (TipoPrestacion.CONSULTA_MEDICA, 3448, ()),
        (TipoPrestacion.EXAMENES_LABORATORIO, 3470, ("Laboratorio", "Otros Exámenes")),
        (TipoPrestacion.IMAGENOLOGIA, 3454, ("Imágenes",)),
        (TipoPrestacion.PROCEDIMIENTO, 3457, ("Procedimiento", "Otro procedimiento")),
        (TipoPrestacion.LENTES, 3480, ("Lentes Ópticos",)),
        (
            TipoPrestacion.OTROS_PROFESIONALES,
            3488,
            ("Kinesiología, fonoaudiología o Nutricionista", "Kinesiología"),
        ),
    ],
)
def test_colmena_prestacion_paths(tipo: TipoPrestacion, leaf: int, subs: tuple[str, ...]) -> None:
    """Our tipos map to the documented Colmena leaf ids."""
    mapping = colmena_prestacion(tipo)
    assert mapping.leaf_id == leaf
    assert mapping.sub_labels == subs


def test_colmena_prestacion_refined_by_provider_name() -> None:
    """Provider keywords pick Fonoaudiología / Consultas for otros profesionales."""
    assert (
        colmena_prestacion(TipoPrestacion.OTROS_PROFESIONALES, ["Centro Fonoaudiológico"]).leaf_id
        == 3489
    )
    assert (
        colmena_prestacion(TipoPrestacion.OTROS_PROFESIONALES, ["Psicología Integral"]).leaf_id
        == 3448
    )


@pytest.mark.parametrize(
    ("descripcion", "leaf"),
    [
        ("Sesión de psicología clínica", 3448),
        ("Sesión de psicoterapia", 3448),
        ("Sesión de terapia ocupacional", 3482),
        ("Sesión de terapia", 3488),
    ],
    ids=["psicologia", "psicoterapia", "terapia-ocupacional", "plain-terapia"],
)
def test_colmena_prestacion_refined_by_honorarios_glosa(descripcion: str, leaf: int) -> None:
    """The boleta's glosa refines otros profesionales when the provider is a person's name."""
    boleta = replace(
        doc(1, DocType.BOLETA_HONORARIOS),
        extraction=Extraction(
            doc_type=DocType.BOLETA_HONORARIOS,
            provider_name="Camila Ficticia Pérez",
            extras={"descripcion": descripcion},
        ),
    )

    mapping = colmena_prestacion(TipoPrestacion.OTROS_PROFESIONALES, colmena_hints([boleta]))

    assert mapping.leaf_id == leaf


def test_colmena_hints_ignore_support_document_glosas() -> None:
    """A referral's description does not refine the prestación; only payments' glosas do."""
    orden = replace(
        doc(2, DocType.ORDEN_MEDICA),
        extraction=Extraction(
            doc_type=DocType.ORDEN_MEDICA,
            extras={"descripcion": "Derivación a evaluación fonoaudiológica"},
        ),
    )

    assert colmena_hints([orden]) == []


@pytest.mark.parametrize(
    "tipo",
    [
        None,
        TipoPrestacion.MEDICAMENTOS,
        TipoPrestacion.DENTAL,
        TipoPrestacion.HOSPITALIZACION,
        TipoPrestacion.OTRO,
    ],
)
def test_colmena_unsupported_tipos_raise(tipo: TipoPrestacion | None) -> None:
    """No Colmena web equivalent → a clear PortalError."""
    with pytest.raises(PortalError):
        colmena_prestacion(tipo)


def test_chubb_prestacion_products() -> None:
    """Dental goes to the dental product, everything else to Salud."""
    assert chubb_prestacion(TipoPrestacion.DENTAL).product == ChubbProduct.DENTAL
    assert chubb_prestacion(TipoPrestacion.LENTES).benefit == "Lentes y Marcos"
    assert chubb_prestacion(TipoPrestacion.MEDICAMENTOS).benefit == "Medicamentos"
    assert (
        chubb_prestacion(TipoPrestacion.EXAMENES_LABORATORIO).benefit
        == "Consulta médica y exámenes"
    )
    with pytest.raises(PortalError):
        chubb_prestacion(None)


def test_pick_label_prefers_exact_then_prefix() -> None:
    """Accent/case-insensitive exact match wins over prefix and substring matches."""
    options = ["Seleccione", "Laboratorio clínico", "laboratorio", "Otros"]
    assert pick_label(options, "Laboratorio") == 2
    assert (
        pick_label(
            ["Exámenes de laboratorio, Imágenes y Procedimientos"], "examenes de LABORATORIO"
        )
        == 0
    )
    assert pick_label(options, "Inexistente") is None


# --------------------------------------------------------------------- documents


def test_assign_colmena_documents_routes_blocks() -> None:
    """Payment → boleta (extra ones to detalle), orden → orden, programa → detalle."""
    mapping = colmena_prestacion(TipoPrestacion.EXAMENES_LABORATORIO)
    docs = [
        doc(1, DocType.BOLETA_ELECTRONICA),
        doc(2, DocType.FACTURA),
        doc(3, DocType.ORDEN_MEDICA),
        doc(4, DocType.PROGRAMA_MEDICO),
        doc(5, DocType.INFORME_EXAMEN),
    ]
    slots = assign_colmena_documents(mapping, docs)
    assert [d.id for d in slots[ColmenaSlot.BOLETA]] == [1]
    assert [d.id for d in slots[ColmenaSlot.DETALLE]] == [4, 2]
    assert [d.id for d in slots[ColmenaSlot.ORDEN]] == [3]


def test_assign_colmena_documents_missing_obligatorio() -> None:
    """Exams need a Detalle de Prestación."""
    mapping = colmena_prestacion(TipoPrestacion.EXAMENES_LABORATORIO)
    with pytest.raises(PortalError, match="Detalle de Prestación"):
        assign_colmena_documents(
            mapping, [doc(1, DocType.BOLETA_ELECTRONICA), doc(2, DocType.ORDEN_MEDICA)]
        )


def test_assign_colmena_documents_drops_no_solicitado() -> None:
    """Consultas do not take an orden médica."""
    mapping = colmena_prestacion(TipoPrestacion.CONSULTA_MEDICA)
    slots = assign_colmena_documents(
        mapping, [doc(1, DocType.BOLETA_HONORARIOS), doc(2, DocType.ORDEN_MEDICA)]
    )
    assert slots[ColmenaSlot.ORDEN] == []


def test_assign_chubb_documents() -> None:
    """Comprobantes → Bono, boleta → Boleta, orden → Derivación, rest (liquidación too) → Otros."""
    cards = [
        ChubbCard("Bono o Bonificación Isapre/Fonasa", True),
        ChubbCard("Boleta o factura", False),
        ChubbCard("Derivación del médico", False),
        ChubbCard("Otros", False),
    ]
    bono = doc(9, DocType.COMPROBANTE_BONIFICACION, DocRole.BONIFICACION_COLMENA)
    result = assign_chubb_documents(
        cards,
        [
            doc(1, DocType.BOLETA_ELECTRONICA),
            doc(2, DocType.ORDEN_MEDICA),
            doc(3, DocType.INFORME_EXAMEN),
            doc(4, DocType.DOCUMENTO_CHUBB),
        ],
        [bono, doc(10, DocType.COMPROBANTE_BONIFICACION, DocRole.BONIFICACION_COLMENA)],
    )
    assert {t: [d.id for d in ds] for t, ds in result.items()} == {
        "Bono o Bonificación Isapre/Fonasa": [9, 10],
        "Boleta o factura": [1],
        "Derivación del médico": [2],
        "Otros": [3, 4],
    }


def test_assign_chubb_documents_reports_missing_obligatorios() -> None:
    """Medicamentos need a receta."""
    cards = [
        ChubbCard("Boleta Farmacia", True),
        ChubbCard("Receta Médica", True),
        ChubbCard("Otros", False),
    ]
    bono = doc(9, DocType.COMPROBANTE_BONIFICACION, DocRole.BONIFICACION_COLMENA)
    with pytest.raises(PortalError, match="Receta Médica"):
        assign_chubb_documents(cards, [doc(1, DocType.BOLETA_ELECTRONICA)], [bono])


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            [{"folio": 111222, "codDocTipo": 1}, {"folio": "333444", "codDocTipo": 2}],
            [
                ColmenaComprobante(111222, reembolso=True),
                ColmenaComprobante(333444, reembolso=False),
            ],
        ),
        ({"response": []}, []),
        (None, []),
    ],
)
def test_colmena_comprobantes(body: object, expected: list[ColmenaComprobante]) -> None:
    """Each reembolso with a numeric folio is one comprobante; codDocTipo 1 picks the template."""
    assert colmena_comprobantes(body) == expected


@pytest.mark.parametrize("item", [{"folio": None, "codDocTipo": 1}, {"codDocTipo": 1}, "x"])
def test_colmena_comprobantes_rejects_a_reembolso_without_folio(item: object) -> None:
    """A listed reembolso with no usable folio fails instead of being left out."""
    body = [{"folio": 111222, "codDocTipo": 1}, item]

    with pytest.raises(PortalError, match="sin folio"):
        colmena_comprobantes(body)


# ------------------------------------------------------------------- status/dates


@pytest.mark.parametrize(
    ("item", "state", "monto"),
    [
        ({"estado": 6, "glsEstado": "Aprobada", "solMonto": "16.800"}, PortalState.APPROVED, 16800),
        ({"estado": 6, "solMonto": 5000}, PortalState.APPROVED, 5000),
        ({"estado": 7, "glsEstado": "Rechazada"}, PortalState.REJECTED, None),
        ({"glsEstado": "Ingresada"}, PortalState.PENDING, None),
        ({"glsEstado": "En revisión"}, PortalState.PENDING, None),
        ({"glsEstado": "Por corregir"}, PortalState.PENDING, None),
        ({"estado": 99, "glsEstado": "???"}, PortalState.PENDING, None),
    ],
)
def test_parse_colmena_status(
    item: dict[str, object], state: PortalState, monto: int | None
) -> None:
    """Colmena states map to pending/approved/rejected."""
    result = parse_colmena_status(item)
    assert result.state == state
    assert result.monto == monto


def test_colmena_rejection_reason_and_raw_without_pii() -> None:
    """The rejection reason is kept; bank data never lands in raw."""
    result = parse_colmena_status(
        {
            "estado": 7,
            "glsEstado": "Rechazada",
            "solObservacionesCliente": "Falta detalle",
            "nroCuenta": "123",
        }
    )
    assert result.message == "Falta detalle"
    assert "nroCuenta" not in result.raw


@pytest.mark.parametrize(
    ("estado", "state"),
    [
        ("Pagado", PortalState.APPROVED),
        ("Rechazada", PortalState.REJECTED),
        ("En Proceso", PortalState.PENDING),
    ],
)
def test_parse_chubb_status(estado: str, state: PortalState) -> None:
    """Chubb history labels map to our states."""
    assert parse_chubb_status(estado, "$ 12.345").state == state


def test_parse_amount_and_dates() -> None:
    """Amounts in pesos and the date formats the portals use."""
    assert parse_amount("$ 1.234.567") == 1234567
    assert parse_amount(16800.0) == 16800
    assert parse_amount("16800.00") == 16800
    assert parse_amount(None) is None
    assert parse_portal_date("05/11/2026") == date(2026, 11, 5)
    assert parse_portal_date("05-11-2026") == date(2026, 11, 5)
    assert parse_portal_date("2026-11-05T03:00:00Z") == date(2026, 11, 5)
    assert parse_portal_date(None) is None


def test_chubb_date_input() -> None:
    """Typed as MM/DD/YYYY."""
    assert chubb_date_input(date(2026, 10, 3)) == "10/03/2026"


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        ("3/10/2026", True),
        ("03/10/2026", True),
        ("10/3/2026", False),
        ("²/10/2026", False),
        ("", False),
    ],
)
def test_chubb_date_shown(shown: str, expected: bool) -> None:
    """Chubb shows D/M/YYYY, with or without leading zeros."""
    assert chubb_date_shown(shown, date(2026, 10, 3)) is expected


def test_chubb_date_window() -> None:
    """Older than 60 days, future or missing dates are rejected with clear errors."""
    today = date(2026, 11, 5)
    assert check_chubb_date(date(2026, 9, 6), today) == date(2026, 9, 6)
    with pytest.raises(PortalError, match="60 días"):
        check_chubb_date(date(2026, 9, 5), today)
    with pytest.raises(PortalError, match="futuro"):
        check_chubb_date(date(2026, 11, 6), today)
    with pytest.raises(PortalError, match="Falta"):
        check_chubb_date(None, today)


def test_parse_multipart_field() -> None:
    """The client-generated denuncio is read from the request body."""
    body = (
        b'--x\r\nContent-Disposition: form-data; name="fuente"\r\n\r\nweb\r\n'
        b'--x\r\nContent-Disposition: form-data; name="denuncio"\r\n\r\n4455\r\n--x--\r\n'
    )
    assert parse_multipart_field(body, "denuncio") == "4455"
    assert parse_multipart_field(body, "solicitud") is None


# -------------------------------------------------------------------- file sizes


def _noise_png(path: Path, side: int) -> Path:
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path, "PNG")
    return path


def test_colmena_total_limit_recompresses_images(tmp_path: Path) -> None:
    """A large photo is recompressed to fit Colmena's 5 MB total."""
    big = _noise_png(tmp_path / "big.png", 1800)
    assert big.stat().st_size > COLMENA_MAX_TOTAL
    out = prepare_colmena([(1, big, "image/png")], tmp_path)
    assert sum(f.size for f in out) <= COLMENA_MAX_TOTAL
    assert out[0].mime == "image/jpeg"


def test_colmena_total_limit_fails_for_pdfs(tmp_path: Path) -> None:
    """PDFs cannot be shrunk: fail before any upload with a clear message."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * (COLMENA_MAX_TOTAL + 1))
    with pytest.raises(PortalError, match="5 MB"):
        prepare_colmena([(1, pdf, "application/pdf")], tmp_path)


def test_colmena_rejects_tiny_files(tmp_path: Path) -> None:
    """Colmena's 10-byte minimum."""
    tiny = tmp_path / "t.pdf"
    tiny.write_bytes(b"%PDF")
    with pytest.raises(PortalError):
        prepare_colmena([(1, tiny, "application/pdf")], tmp_path)


def test_chubb_per_file_limit(tmp_path: Path) -> None:
    """Chubb allows 10 MB per file; webp is converted to JPEG."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * (CHUBB_MAX_FILE + 1))
    with pytest.raises(PortalError, match="10 MB"):
        prepare_chubb([(1, pdf, "application/pdf")], tmp_path)
    webp = tmp_path / "a.webp"
    Image.new("RGB", (20, 20)).save(webp, "WEBP")
    assert prepare_chubb([(2, webp, "image/webp")], tmp_path)[0].mime == "image/jpeg"


# ------------------------------------------------------------------- idempotency


def test_colmena_duplicates_filters() -> None:
    """Same amount after the date, not rejected, not already linked."""
    items = [
        {"solIdn": 1, "solMonto": 24000, "solFecha": "10/10/2026", "glsEstado": "Ingresada"},
        {"solIdn": 2, "solMonto": 24000, "solFecha": "01/09/2026", "glsEstado": "Aprobada"},
        {"solIdn": 3, "solMonto": 24000, "solFecha": "11/10/2026", "glsEstado": "Rechazada"},
        {"solIdn": 4, "solMonto": 24000, "solFecha": "12/10/2026", "glsEstado": "Aprobada"},
        {"solIdn": 5, "solMonto": 9999, "solFecha": "12/10/2026", "glsEstado": "Aprobada"},
    ]
    found = colmena_duplicates(items, monto=24000, since=date(2026, 10, 3), known_ids={"4"})
    assert found == ["1"]
    assert colmena_duplicates(items, monto=None, since=None, known_ids=set()) == []


def test_markers_roundtrip_and_cleared_on_submission(
    service: RendicionService, lista_rendicion: int
) -> None:
    """The marker survives until the submission is recorded."""
    repo = service.repo
    repo.set_marker(lista_rendicion, "colmena", "final_click")
    marker = repo.get_marker(lista_rendicion, "colmena")
    assert marker is not None
    assert marker.state == "final_click"
    assert marker.request_id is None
    repo.set_marker(lista_rendicion, "colmena", "done", "123")
    marker = repo.get_marker(lista_rendicion, "colmena")
    assert marker is not None
    assert marker.request_id == "123"
    assert "123" in repo.known_request_ids("colmena")

    record_submission(service, lista_rendicion, Portal.COLMENA, "123")

    assert repo.get_marker(lista_rendicion, "colmena") is None
    assert repo.known_request_ids("colmena") == {"123"}


def test_marker_survives_error_round_trip(service: RendicionService, lista_rendicion: int) -> None:
    """Moving error → lista does not clear the marker (needs an explicit clear-marker)."""
    service.repo.set_marker(lista_rendicion, "colmena", "final_click")
    service.set_status(lista_rendicion, Status.ERROR, error_message="x")
    service.set_status(lista_rendicion, Status.LISTA)
    assert service.repo.get_marker(lista_rendicion, "colmena") is not None
    assert service.repo.clear_marker(lista_rendicion, "colmena")


# ---------------------------------------------------------------------- pipeline


class ExpiredColmena(FakeColmenaClient):
    """Colmena whose session is gone."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def submit(self, rendicion: RendicionView) -> SubmissionResult:
        self.calls += 1
        raise SessionExpiredError("colmena")


def test_session_expiry_does_not_storm(service: RendicionService, lista_rendicion: int) -> None:
    """Only the first rendición hits the portal and goes to error; the rest are skipped."""
    second = service.repo.create_rendicion(confidence=1.0, reason="test")
    service.set_status(second, Status.LISTA)
    colmena = ExpiredColmena()

    report = Pipeline(service, colmena, FakePortalClient("chubb")).run_once()

    assert colmena.calls == 1
    assert len(report.errors) == 1
    assert len(report.skipped) == 1
    assert report.sessions_expired == ["colmena"]
    failed = service.repo.get_rendicion(report.errors[0])
    assert "rendiciones login colmena" in (failed.error_message or "")
    assert service.repo.get_rendicion(report.skipped[0]).status == Status.LISTA


def test_portal_error_message_is_kept(service: RendicionService, lista_rendicion: int) -> None:
    """PortalError text (safe by contract) becomes the rendición's error message."""

    class Picky(FakeColmenaClient):
        def submit(self, rendicion: RendicionView) -> SubmissionResult:
            raise PortalError("Falta documento obligatorio para Colmena: Orden Médica")

    Pipeline(service, Picky(), FakePortalClient("chubb")).run_once()

    view = service.repo.get_rendicion(lista_rendicion)
    assert view.error_message == "Falta documento obligatorio para Colmena: Orden Médica"


def test_dry_run_pipeline_changes_nothing(service: RendicionService, lista_rendicion: int) -> None:
    """In dry-run nothing transitions, even with clients that would submit."""
    colmena = FakeColmenaClient(decide=lambda _: StatusResult(state=PortalState.APPROVED))

    report = Pipeline(service, colmena, FakePortalClient("chubb"), dry_run=True).run_once()

    assert report.dry_run == [lista_rendicion]
    assert service.repo.get_rendicion(lista_rendicion).status == Status.LISTA


def test_playwright_alias() -> None:
    """``--clients playwright`` resolves to the browser factory."""
    assert resolve_spec("playwright") == PLAYWRIGHT_SPEC
