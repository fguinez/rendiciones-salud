"""Tests for ingesting, grouping and editing rendiciones through the service layer."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import date

import pytest

from conftest import PdfFactory, fixture_text, render_pdf
from rendiciones.config import Settings
from rendiciones.db import Repository
from rendiciones.domain import DocType, DocumentEdit, Status, TipoPrestacion
from rendiciones.services import MoveResult, NotEditableError, RendicionService


def test_ingest_classifies_and_stores(service: RendicionService, pdf_of: PdfFactory) -> None:
    """An uploaded PDF is stored, classified and left ungrouped."""
    data = pdf_of("boleta_laboratorio")

    result = service.ingest(io.BytesIO(data), "boleta.pdf", "Octubre/boleta.pdf", "b1")

    assert result.ok
    assert result.doc_type == DocType.BOLETA_ELECTRONICA.value
    doc = service.repo.get_document(result.document_id or 0)
    assert doc.folder == "Octubre"
    assert doc.path.is_absolute()
    assert doc.path.exists()
    assert doc.rendicion_id is None


def test_ingest_duplicate(service: RendicionService, pdf_of: PdfFactory) -> None:
    """Uploading identical bytes twice returns the existing document."""
    data = pdf_of("receta")
    first = service.ingest(io.BytesIO(data), "receta.pdf", None, "b1")

    second = service.ingest(io.BytesIO(data), "copia.pdf", None, "b2")

    assert second.duplicate
    assert second.document_id == first.document_id


def test_group_pending_builds_rendiciones(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Related fixtures form one rendición; unrelated ones get their own."""
    boleta = ingest("boleta_laboratorio")
    orden = ingest("orden_medica")
    comprobante = ingest("comprobante_bonificacion")
    lentes = ingest("factura_lentes")

    grouping = service.group_pending("b1")

    assert len(grouping.created) == 2
    lab = service.repo.get_rendicion(service.repo.get_document(boleta).rendicion_id or 0)
    assert {d.id for d in lab.documents} == {boleta, orden, comprobante}
    assert lab.monto == 24000
    assert lab.fecha_atencion == date(2026, 10, 3)
    assert lab.tipo_prestacion == TipoPrestacion.EXAMENES_LABORATORIO
    assert service.repo.get_document(lentes).rendicion_id not in (None, lab.id)


def test_weekly_boletas_honorarios_get_one_rendicion_each(service: RendicionService) -> None:
    """Weekly SII boletas de honorarios in one folder become separate rendiciones with monto."""
    template = fixture_text("boleta_honorarios_sii")
    for folio, day in [("900", "05"), ("901", "12"), ("902", "19"), ("903", "26")]:
        text = template.replace("900", folio).replace("05", day)
        service.ingest(io.BytesIO(render_pdf(text)), f"{folio}.pdf", f"Terapia/{folio}.pdf", "b1")

    grouping = service.group_pending("b1")

    rendiciones = [service.repo.get_rendicion(rid) for rid in grouping.created]
    assert len(rendiciones) == 4
    assert all(len(r.documents) == 1 for r in rendiciones)
    assert sorted(r.fecha_atencion.day for r in rendiciones if r.fecha_atencion) == [5, 12, 19, 26]
    assert {r.monto for r in rendiciones} == {40000}


def test_group_pending_extends_existing_borrador(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A later upload that matches an existing borrador joins it."""
    ingest("boleta_laboratorio", batch="b1")
    first = service.group_pending("b1")
    ingest("comprobante_bonificacion", batch="b2")

    second = service.group_pending("b2")

    assert second.created == []
    assert second.extended == first.created


def test_edit_locks_fields(service: RendicionService, ingest: Callable[..., int]) -> None:
    """User edits persist and are not overwritten by later recomputation."""
    ingest("boleta_laboratorio")
    rid = service.group_pending("b1").created[0]

    service.edit(rid, monto=30000, tipo_prestacion=TipoPrestacion.CONSULTA_MEDICA)
    service.refresh_summary(rid)

    view = service.repo.get_rendicion(rid)
    assert view.monto == 30000
    assert view.tipo_prestacion == TipoPrestacion.CONSULTA_MEDICA
    assert view.fields_locked


def test_edit_rejects_negative_amount(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Negative amounts are rejected."""
    ingest("receta")
    rid = service.group_pending("b1").created[0]

    with pytest.raises(ValueError, match="negativo"):
        service.edit(rid, monto=-1)


def test_move_document_to_new_rendicion(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Moving a document to a new rendición recalculates both sides."""
    boleta = ingest("boleta_laboratorio")
    orden = ingest("orden_medica")
    source = service.group_pending("b1").created[0]

    target = service.move_document(orden, None)

    assert target.id != source
    assert [d.id for d in target.documents] == [orden]
    assert [d.id for d in service.repo.get_rendicion(source).documents] == [boleta]


def test_moving_last_document_removes_empty_rendicion(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A rendición left without documents is deleted."""
    receta = ingest("receta")
    lentes = ingest("factura_lentes")
    service.group_pending("b1")
    source = service.repo.get_document(receta).rendicion_id or 0
    target = service.repo.get_document(lentes).rendicion_id

    service.move_document(receta, target)

    assert source not in {r.id for r in service.repo.list_rendiciones()}


def test_moving_monto_document_out_returns_lista_to_borrador(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A lista rendición that loses its only monto-bearing document goes back to borrador."""
    boleta = ingest("boleta_laboratorio")
    ingest("orden_medica")
    source = service.group_pending("b1").created[0]
    service.set_status(source, Status.LISTA)

    service.move_document(boleta, None)

    view = service.repo.get_rendicion(source)
    assert (view.status, view.monto) == (Status.BORRADOR, None)


def test_delete_document_removes_file(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Deleting a document removes the stored file too."""
    doc_id = ingest("receta")
    path = service.repo.get_document(doc_id).path

    service.delete_document(doc_id)

    assert not path.exists()


def test_submitted_rendicion_is_not_editable(
    service: RendicionService, lista_rendicion: int
) -> None:
    """Rendiciones already sent to a portal cannot be edited or deleted."""
    service.repo.transition(lista_rendicion, Status.COLMENA_ENVIADA)

    with pytest.raises(NotEditableError):
        service.edit(lista_rendicion, monto=1)
    with pytest.raises(NotEditableError):
        service.delete_rendicion(lista_rendicion)

    assert service.repo.get_rendicion(lista_rendicion).monto == 24000


def test_edit_document_marks_fields_and_recomputes_rendicion(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Correcting a boleta marks the fields as edited and updates the rendición's summary."""
    boleta = ingest("boleta_laboratorio")
    ingest("orden_medica")
    rid = service.group_pending("b1").created[0]

    doc = service.edit_document(boleta, DocumentEdit.build(monto=30000, fecha=date(2026, 10, 1)))

    view = service.repo.get_rendicion(rid)
    assert doc.edited_fields == {"monto", "fecha"}
    assert (view.monto, view.fecha_atencion) == (30000, date(2026, 10, 1))
    assert [e.kind for e in service.repo.events(rid)][-1] == "documento_editado"


def test_edit_document_keeps_locked_rendicion_values(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Document edits never overwrite values the user fixed on the rendición."""
    boleta = ingest("boleta_laboratorio")
    rid = service.group_pending("b1").created[0]
    service.edit(rid, monto=5000)

    service.edit_document(boleta, DocumentEdit.build(monto=30000))

    assert service.repo.get_rendicion(rid).monto == 5000


def test_clearing_document_monto_returns_lista_to_borrador(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A lista rendición whose only document loses its monto goes back to borrador."""
    boleta = ingest("boleta_laboratorio")
    rid = service.group_pending("b1").created[0]
    service.set_status(rid, Status.LISTA)

    service.edit_document(boleta, DocumentEdit.build(monto=None))

    view = service.repo.get_rendicion(rid)
    assert (view.status, view.monto) == (Status.BORRADOR, None)
    assert service.repo.events(rid)[-1].to_status == Status.BORRADOR


def test_recompute_unlocks_and_follows_documents(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Recompute drops the manual values and derives them from the documents again."""
    ingest("boleta_laboratorio")
    rid = service.group_pending("b1").created[0]
    service.edit(rid, monto=5000)

    view = service.recompute(rid)

    assert view.monto == 24000
    assert not view.fields_locked


def test_recompute_returns_incomplete_lista_to_borrador(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Recomputing a lista rendición whose documents have no monto returns it to borrador."""
    boleta = ingest("boleta_laboratorio")
    rid = service.group_pending("b1").created[0]
    service.edit(rid, monto=5000)
    service.set_status(rid, Status.LISTA)
    service.edit_document(boleta, DocumentEdit.build(monto=None))

    view = service.recompute(rid)

    assert (view.status, view.monto) == (Status.BORRADOR, None)
    assert service.repo.events(rid)[-1].detail["via"] == "recalculado"


def test_move_documents_to_existing_rendicion(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Documents join the target; a source that still has documents is kept."""
    boleta = ingest("boleta_laboratorio")
    orden = ingest("orden_medica")
    lentes = ingest("factura_lentes")
    service.group_pending("b1")
    source = service.repo.get_document(boleta).rendicion_id or 0
    target = service.repo.get_document(lentes).rendicion_id or 0

    result = service.move_documents([boleta], target)

    assert result == MoveResult(rendicion_id=target, deleted_rendiciones=[])
    assert {d.id for d in service.repo.get_rendicion(target).documents} == {boleta, lentes}
    assert [d.id for d in service.repo.get_rendicion(source).documents] == [orden]


def test_restore_grouping_after_undoing_a_move(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """Undoing a move can put back the confidence and reason the grouping had."""
    boleta = ingest("boleta_laboratorio")
    ingest("orden_medica")
    lentes = ingest("factura_lentes")
    service.group_pending("b1")
    source = service.repo.get_rendicion(service.repo.get_document(boleta).rendicion_id or 0)
    target = service.repo.get_document(lentes).rendicion_id or 0
    service.move_documents([boleta], target)
    service.move_documents([boleta], source.id)

    view = service.restore_grouping(source.id, source.grouping_confidence, source.grouping_reason)

    assert (view.grouping_confidence, view.grouping_reason) == (
        source.grouping_confidence,
        source.grouping_reason,
    )
    assert service.repo.events(source.id)[-1].kind == "agrupacion_restaurada"


def test_move_documents_to_new_rendicion_deletes_emptied_sources(
    service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A null target creates one rendición for all documents and drops emptied sources."""
    receta = ingest("receta")
    lentes = ingest("factura_lentes")
    service.group_pending("b1")
    sources = {service.repo.get_document(d).rendicion_id for d in (receta, lentes)}

    result = service.move_documents([receta, lentes], None)

    target = service.repo.get_rendicion(result.rendicion_id)
    assert [d.id for d in target.documents] == [receta, lentes]
    assert set(result.deleted_rendiciones) == sources
    assert result.rendicion_id not in sources


@pytest.mark.parametrize("move", ["from_sent", "to_sent", "from_sent_to_new"])
def test_move_documents_rejects_sent_rendicion(
    service: RendicionService, ingest: Callable[..., int], lista_rendicion: int, move: str
) -> None:
    """Nothing changes when a source or the target was already sent to a portal."""
    lentes = ingest("factura_lentes", batch="b2")
    other = service.group_pending("b2").created[0]
    service.repo.transition(lista_rendicion, Status.COLMENA_ENVIADA)
    sent_doc = service.repo.get_rendicion(lista_rendicion).documents[0].id
    doc, target = {
        "from_sent": (sent_doc, other),
        "to_sent": (lentes, lista_rendicion),
        "from_sent_to_new": (sent_doc, None),
    }[move]

    with pytest.raises(NotEditableError):
        service.move_documents([doc], target)

    assert {r.id for r in service.repo.list_rendiciones()} == {lista_rendicion, other}
    assert [d.id for d in service.repo.get_rendicion(other).documents] == [lentes]


def test_repository_adds_columns_missing_from_older_databases(
    settings: Settings, service: RendicionService, ingest: Callable[..., int]
) -> None:
    """A database created before ``edited_fields`` existed gets the column on startup."""
    doc_id = ingest("receta")
    with service.repo.connect() as conn:
        conn.execute("ALTER TABLE documents DROP COLUMN edited_fields_json")

    repo = Repository(settings.db_path)

    assert repo.get_document(doc_id).edited_fields == frozenset()
